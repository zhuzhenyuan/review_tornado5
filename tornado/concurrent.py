## -*- coding: utf-8 -*-

# Copyright 2012 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Utilities for working with ``Future`` objects.

``Futures`` are a pattern for concurrent programming introduced in
Python 3.2 in the `concurrent.futures` package, and also adopted (in a
slightly different form) in Python 3.4's `asyncio` package. This
package defines a ``Future`` class that is an alias for `asyncio.Future`
when available, and a compatible implementation for older versions of
Python. It also includes some utility functions for interacting with
``Future`` objects.

While this package is an important part of Tornado's internal
implementation, applications rarely need to interact with it
directly.
"""
from __future__ import absolute_import, division, print_function

import functools
import platform
import textwrap
import traceback
import sys
import warnings

from tornado.log import app_log
from tornado.stack_context import ExceptionStackContext, wrap
from tornado.util import raise_exc_info, ArgReplacer, is_finalizing

try:
	from concurrent import futures
except ImportError:
	futures = None

try:
	import asyncio
except ImportError:
	asyncio = None

try:
	import typing
except ImportError:
	typing = None

# Can the garbage collector handle cycles that include __del__ methods?
# This is true in cpython beginning with version 3.4 (PEP 442).
_GC_CYCLE_FINALIZERS = (platform.python_implementation() == 'CPython' and
                        sys.version_info >= (3, 4))


# 异常类
class ReturnValueIgnoredError(Exception):
	pass


# This class and associated code in the future object is derived
# from the Trollius project, a backport of asyncio to Python 2.x - 3.x

# <=py3.3 错误信息操作类
class _TracebackLogger(object):
	"""Helper to log a traceback upon destruction if not cleared.

	This solves a nasty problem with Futures and Tasks that have an
	exception set: if nobody asks for the exception, the exception is
	never logged.  This violates the Zen of Python: 'Errors should
	never pass silently.  Unless explicitly silenced.'

	However, we don't want to log the exception as soon as
	set_exception() is called: if the calling code is written
	properly, it will get the exception and handle it properly.  But
	we *do* want to log it if result() or exception() was never called
	-- otherwise developers waste a lot of time wondering why their
	buggy code fails silently.

	An earlier attempt added a __del__() method to the Future class
	itself, but this backfired because the presence of __del__()
	prevents garbage collection from breaking cycles.  A way out of
	this catch-22 is to avoid having a __del__() method on the Future
	class itself, but instead to have a reference to a helper object
	with a __del__() method that logs the traceback, where we ensure
	that the helper object doesn't participate in cycles, and only the
	Future has a reference to it.

	The helper object is added when set_exception() is called.  When
	the Future is collected, and the helper is present, the helper
	object is also collected, and its __del__() method will log the
	traceback.  When the Future's result() or exception() method is
	called (and a helper object is present), it removes the the helper
	object, after calling its clear() method to prevent it from
	logging.

	One downside is that we do a fair amount of work to extract the
	traceback from the exception, even when it is never logged.  It
	would seem cheaper to just store the exception object, but that
	references the traceback, which references stack frames, which may
	reference the Future, which references the _TracebackLogger, and
	then the _TracebackLogger would be included in a cycle, which is
	what we're trying to avoid!  As an optimization, we don't
	immediately format the exception; we only do the work when
	activate() is called, which call is delayed until after all the
	Future's callbacks have run.  Since usually a Future has at least
	one callback (typically set by 'yield From') and usually that
	callback extracts the callback, thereby removing the need to
	format the exception.

	PS. I don't claim credit for this solution.  I first heard of it
	in a discussion about closing files when they are collected.
	"""

	__slots__ = ('exc_info', 'formatted_tb')

	def __init__(self, exc_info):
		self.exc_info = exc_info
		self.formatted_tb = None

	def activate(self):
		exc_info = self.exc_info
		if exc_info is not None:
			self.exc_info = None
			self.formatted_tb = traceback.format_exception(*exc_info)

	def clear(self):
		self.exc_info = None
		self.formatted_tb = None

	def __del__(self, is_finalizing=is_finalizing):
		# >= 3.4 的统一报错在asyncio.Future中定义
		# 此处统一错误日志打印统一日志  # <=py3.3
		if not is_finalizing() and self.formatted_tb:
			app_log.error('Future exception was never retrieved: %s',
			              ''.join(self.formatted_tb).rstrip())


class Future(object):
	"""Placeholder for an asynchronous result.

	A ``Future`` encapsulates the result of an asynchronous
	operation.  In synchronous applications ``Futures`` are used
	to wait for the result from a thread or process pool; in
	Tornado they are normally used with `.IOLoop.add_future` or by
	yielding them in a `.gen.coroutine`.

	`tornado.concurrent.Future` is an alias for `asyncio.Future` when
	that package is available (Python 3.4+). Unlike
	`concurrent.futures.Future`, the ``Futures`` used by Tornado and
	`asyncio` are not thread-safe (and therefore faster for use with
	single-threaded event loops).

	In addition to ``exception`` and ``set_exception``, Tornado's
	``Future`` implementation supports storing an ``exc_info`` triple
	to support better tracebacks on Python 2. To set an ``exc_info``
	triple, use `future_set_exc_info`, and to retrieve one, call
	`result()` (which will raise it).

	.. versionchanged:: 4.0
	   `tornado.concurrent.Future` is always a thread-unsafe ``Future``
	   with support for the ``exc_info`` methods.  Previously it would
	   be an alias for the thread-safe `concurrent.futures.Future`
	   if that package was available and fall back to the thread-unsafe
	   implementation if it was not.

	.. versionchanged:: 4.1
	   If a `.Future` contains an error but that error is never observed
	   (by calling ``result()``, ``exception()``, or ``exc_info()``),
	   a stack trace will be logged when the `.Future` is garbage collected.
	   This normally indicates an error in the application, but in cases
	   where it results in undesired logging it may be necessary to
	   suppress the logging by ensuring that the exception is observed:
	   ``f.add_done_callback(lambda f: f.exception())``.

	.. versionchanged:: 5.0

	   This class was previoiusly available under the name
	   ``TracebackFuture``. This name, which was deprecated since
	   version 4.0, has been removed. When `asyncio` is available
	   ``tornado.concurrent.Future`` is now an alias for
	   `asyncio.Future`. Like `asyncio.Future`, callbacks are now
	   always scheduled on the `.IOLoop` and are never run
	   synchronously.

	"""

	def __init__(self):
		self._done = False
		self._result = None
		self._exc_info = None

		self._log_traceback = False  # Used for Python >= 3.4
		self._tb_logger = None  # Used for Python <= 3.3

		self._callbacks = []

	# Implement the Python 3.5 Awaitable protocol if possible
	# (we can't use return and yield together until py33).
	if sys.version_info >= (3, 3):
		exec(textwrap.dedent("""
        def __await__(self):
            return (yield self)
        """))
	else:
		# Py2-compatible version for use with cython.
		def __await__(self):
			result = yield self
			# StopIteration doesn't take args before py33,
			# but Cython recognizes the args tuple.
			e = StopIteration()
			e.args = (result,)
			raise e

	# 取消，tornado不支持,总是返回False
	def cancel(self):
		"""Cancel the operation, if possible.

		Tornado ``Futures`` do not support cancellation, so this method always
		returns False.
		"""
		return False

	# 是否已取消，tornado不支持,总是返回False
	def cancelled(self):
		"""Returns True if the operation has been cancelled.

		Tornado ``Futures`` do not support cancellation, so this method
		always returns False.
		"""
		return False

	# 是否在运行
	def running(self):
		"""Returns True if this operation is currently running."""
		return not self._done

	# 是否已完成
	def done(self):
		"""Returns True if the future has finished running."""
		return self._done

	# 清理错误日志
	def _clear_tb_log(self):
		self._log_traceback = False
		if self._tb_logger is not None:
			self._tb_logger.clear()
			self._tb_logger = None

	# 获取返回值结果，有错报错
	def result(self, timeout=None):
		"""If the operation succeeded, return its result.  If it failed,
		re-raise its exception.

		This method takes a ``timeout`` argument for compatibility with
		`concurrent.futures.Future` but it is an error to call it
		before the `Future` is done, so the ``timeout`` is never used.
		"""
		self._clear_tb_log()  # 清理 traceback 信息
		if self._result is not None:  # 有结果返回
			return self._result
		if self._exc_info is not None:
			try:
				raise_exc_info(self._exc_info)  # 抛出错误raise error, py2和py3逻辑有点不一样，但都是报错
			finally:
				self = None
		self._check_done()
		return self._result

	# 返回Exception对象
	def exception(self, timeout=None):
		"""If the operation raised an exception, return the `Exception`
		object.  Otherwise returns None.

		This method takes a ``timeout`` argument for compatibility with
		`concurrent.futures.Future` but it is an error to call it
		before the `Future` is done, so the ``timeout`` is never used.
		"""
		self._clear_tb_log()  # 清理错误日志
		if self._exc_info is not None:  # 返回错误
			return self._exc_info[1]
		else:
			self._check_done()  # 检查是否完成
			return None  # 没错就返回 None

	# 添加回调函数，fulture已完成就放入ioloop，未完成就加入当前fulture的回调列表
	def add_done_callback(self, fn):
		"""Attaches the given callback to the `Future`.

		It will be invoked with the `Future` as its argument when the Future
		has finished running and its result is available.  In Tornado
		consider using `.IOLoop.add_future` instead of calling
		`add_done_callback` directly.
		"""
		if self._done:
			from tornado.ioloop import IOLoop
			IOLoop.current().add_callback(fn, self)  # fulture已完成就放入ioloop
		else:
			self._callbacks.append(fn)  # 未完成就加入当前fulture的回调列表

	# 设置返回值，标识完成，回调函数放入ioloop
	def set_result(self, result):
		"""Sets the result of a ``Future``.

		It is undefined to call any of the ``set`` methods more than once
		on the same object.
		"""
		self._result = result  # 设置结果
		self._set_done()  # 标识完成

	# 设置异常信息
	def set_exception(self, exception):
		"""Sets the exception of a ``Future.``"""
		self.set_exc_info(
			(exception.__class__,
			 exception,
			 getattr(exception, '__traceback__', None)))

	# 返回异常信息
	def exc_info(self):
		"""Returns a tuple in the same format as `sys.exc_info` or None.

		.. versionadded:: 4.0
		"""
		self._clear_tb_log()
		return self._exc_info

	# 设置异常信息
	def set_exc_info(self, exc_info):
		"""Sets the exception information of a ``Future.``

		Preserves tracebacks on Python 2.

		.. versionadded:: 4.0
		"""
		self._exc_info = exc_info  # 设置异常信息
		self._log_traceback = True  # 标识有异常 >= py3.3
		if not _GC_CYCLE_FINALIZERS:  # <=py3.3
			self._tb_logger = _TracebackLogger(exc_info)

		try:
			self._set_done()  # 设置完成
		finally:
			# Activate the logger after all callbacks have had a
			# chance to call result() or exception().
			if self._log_traceback and self._tb_logger is not None:  # <=py3.3
				self._tb_logger.activate()
		self._exc_info = exc_info  # 设置异常信息

	# 检查是否完成，未完成报错
	def _check_done(self):
		if not self._done:  # 没完成报错
			raise Exception("DummyFuture does not support blocking for results")

	# 设置完成状态，把回调函数放入ioloop，清空回调序列
	def _set_done(self):
		self._done = True  # 设置完成状态
		if self._callbacks:  # 把回调函数放入ioloop
			from tornado.ioloop import IOLoop
			loop = IOLoop.current()
			for cb in self._callbacks:
				loop.add_callback(cb, self)  # 放入ioloop的回调
			self._callbacks = None  # 清空回调序列

	# On Python 3.3 or older, objects with a destructor part of a reference
	# cycle are never destroyed. It's no longer the case on Python 3.4 thanks to
	# the PEP 442.
	if _GC_CYCLE_FINALIZERS:  # >=py3.4
		def __del__(self, is_finalizing=is_finalizing):
			if is_finalizing() or not self._log_traceback:
				# set_exception() was not called, or result() or exception()
				# has consumed the exception
				# 没有异常信息，or信息可能已经被提取过
				return

			tb = traceback.format_exception(*self._exc_info)

			app_log.error('Future %r exception was never retrieved: %s',
			              self, ''.join(tb).rstrip())


if asyncio is not None:  # >=py3.4 使用 asyncio 库的 Future
	Future = asyncio.Future  # noqa

if futures is None:
	FUTURES = Future  # type: typing.Union[type, typing.Tuple[type, ...]]
else:
	FUTURES = (futures.Future, Future)  # concurrent 库的 Future


# 是否是Future
def is_future(x):
	return isinstance(x, FUTURES)


class DummyExecutor(object):
	def submit(self, fn, *args, **kwargs):
		future = Future()
		try:
			future_set_result_unless_cancelled(future, fn(*args, **kwargs))
		except Exception:
			future_set_exc_info(future, sys.exc_info())
		return future

	def shutdown(self, wait=True):
		pass


# 一个执行器，future操作
dummy_executor = DummyExecutor()


def run_on_executor(*args, **kwargs):
	"""Decorator to run a synchronous method asynchronously on an executor.

	The decorated method may be called with a ``callback`` keyword
	argument and returns a future.

	The executor to be used is determined by the ``executor``
	attributes of ``self``. To use a different attribute name, pass a
	keyword argument to the decorator::

		@run_on_executor(executor='_thread_pool')
		def foo(self):
			pass

	This decorator should not be confused with the similarly-named
	`.IOLoop.run_in_executor`. In general, using ``run_in_executor``
	when *calling* a blocking method is recommended instead of using
	this decorator when *defining* a method. If compatibility with older
	versions of Tornado is required, consider defining an executor
	and using ``executor.submit()`` at the call site.

	.. versionchanged:: 4.2
	   Added keyword arguments to use alternative attributes.

	.. versionchanged:: 5.0
	   Always uses the current IOLoop instead of ``self.io_loop``.

	.. versionchanged:: 5.1
	   Returns a `.Future` compatible with ``await`` instead of a
	   `concurrent.futures.Future`.

	.. deprecated:: 5.1

	   The ``callback`` argument is deprecated and will be removed in
	   6.0. The decorator itself is discouraged in new code but will
	   not be removed in 6.0.
	"""

	def run_on_executor_decorator(fn):
		executor = kwargs.get("executor", "executor")

		@functools.wraps(fn)
		def wrapper(self, *args, **kwargs):
			callback = kwargs.pop("callback", None)
			async_future = Future()
			conc_future = getattr(self, executor).submit(fn, self, *args, **kwargs)
			chain_future(conc_future, async_future)
			if callback:
				warnings.warn("callback arguments are deprecated, use the returned Future instead",
				              DeprecationWarning)
				from tornado.ioloop import IOLoop
				IOLoop.current().add_future(
					async_future, lambda future: callback(future.result()))
			return async_future

		return wrapper

	if args and kwargs:
		raise ValueError("cannot combine positional and keyword args")
	if len(args) == 1:
		return run_on_executor_decorator(args[0])
	elif len(args) != 0:
		raise ValueError("expected 1 argument, got %d", len(args))
	return run_on_executor_decorator


_NO_RESULT = object()


def return_future(f):
	"""Decorator to make a function that returns via callback return a
	`Future`.

	This decorator was provided to ease the transition from
	callback-oriented code to coroutines. It is not recommended for
	new code.

	The wrapped function should take a ``callback`` keyword argument
	and invoke it with one argument when it has finished.  To signal failure,
	the function can simply raise an exception (which will be
	captured by the `.StackContext` and passed along to the ``Future``).

	From the caller's perspective, the callback argument is optional.
	If one is given, it will be invoked when the function is complete
	with ``Future.result()`` as an argument.  If the function fails, the
	callback will not be run and an exception will be raised into the
	surrounding `.StackContext`.

	If no callback is given, the caller should use the ``Future`` to
	wait for the function to complete (perhaps by yielding it in a
	coroutine, or passing it to `.IOLoop.add_future`).

	Usage:

	.. testcode::

		@return_future
		def future_func(arg1, arg2, callback):
			# Do stuff (possibly asynchronous)
			callback(result)

		async def caller():
			await future_func(arg1, arg2)

	..

	Note that ``@return_future`` and ``@gen.engine`` can be applied to the
	same function, provided ``@return_future`` appears first.  However,
	consider using ``@gen.coroutine`` instead of this combination.

	.. versionchanged:: 5.1

	   Now raises a `.DeprecationWarning` if a callback argument is passed to
	   the decorated function and deprecation warnings are enabled.

	.. deprecated:: 5.1

	   This decorator will be removed in Tornado 6.0. New code should
	   use coroutines directly instead of wrapping callback-based code
	   with this decorator. Interactions with non-Tornado
	   callback-based code should be managed explicitly to avoid
	   relying on the `.ExceptionStackContext` built into this
	   decorator.
	"""
	warnings.warn("@return_future is deprecated, use coroutines instead",
	              DeprecationWarning)
	return _non_deprecated_return_future(f, warn=True)


def _non_deprecated_return_future(f, warn=False):
	# Allow auth.py to use this decorator without triggering
	# deprecation warnings. This will go away once auth.py has removed
	# its legacy interfaces in 6.0.
	replacer = ArgReplacer(f, 'callback')

	@functools.wraps(f)
	def wrapper(*args, **kwargs):
		future = Future()
		callback, args, kwargs = replacer.replace(
			lambda value=_NO_RESULT: future_set_result_unless_cancelled(future, value),
			args, kwargs)

		def handle_error(typ, value, tb):
			future_set_exc_info(future, (typ, value, tb))
			return True

		exc_info = None
		esc = ExceptionStackContext(handle_error, delay_warning=True)
		with esc:
			if not warn:
				# HACK: In non-deprecated mode (only used in auth.py),
				# suppress the warning entirely. Since this is added
				# in a 5.1 patch release and already removed in 6.0
				# I'm prioritizing a minimial change instead of a
				# clean solution.
				esc.delay_warning = False
			try:
				result = f(*args, **kwargs)
				if result is not None:
					raise ReturnValueIgnoredError(
						"@return_future should not be used with functions "
						"that return values")
			except:
				exc_info = sys.exc_info()
				raise
		if exc_info is not None:
			# If the initial synchronous part of f() raised an exception,
			# go ahead and raise it to the caller directly without waiting
			# for them to inspect the Future.
			future.result()

		# If the caller passed in a callback, schedule it to be called
		# when the future resolves.  It is important that this happens
		# just before we return the future, or else we risk confusing
		# stack contexts with multiple exceptions (one here with the
		# immediate exception, and again when the future resolves and
		# the callback triggers its exception by calling future.result()).
		if callback is not None:
			warnings.warn("callback arguments are deprecated, use the returned Future instead",
			              DeprecationWarning)

			def run_callback(future):
				result = future.result()
				if result is _NO_RESULT:
					callback()
				else:
					callback(future.result())

			future_add_done_callback(future, wrap(run_callback))
		return future

	return wrapper

# 绑定两个 futures，a完成了，那么把b也设置成完成
# a会把成功或失败的结果 复制到b
# todo zzy 看操作
def chain_future(a, b):
	"""Chain two futures together so that when one completes, so does the other.

	The result (success or failure) of ``a`` will be copied to ``b``, unless
	``b`` has already been completed or cancelled by the time ``a`` finishes.
	a会把成功或失败的结果 复制到b

	.. versionchanged:: 5.0

	   Now accepts both Tornado/asyncio `Future` objects and
	   `concurrent.futures.Future`.

	"""

	def copy(future):
		assert future is a
		if b.done():
			return
		if (hasattr(a, 'exc_info') and
				a.exc_info() is not None):
			future_set_exc_info(b, a.exc_info())
		elif a.exception() is not None:
			b.set_exception(a.exception())
		else:
			b.set_result(a.result())

	if isinstance(a, Future):
		future_add_done_callback(a, copy)
	else:
		# concurrent.futures.Future
		from tornado.ioloop import IOLoop
		IOLoop.current().add_future(a, copy)


# 为future设置result
def future_set_result_unless_cancelled(future, value):
	"""Set the given ``value`` as the `Future`'s result, if not cancelled.

	Avoids asyncio.InvalidStateError when calling set_result() on
	a cancelled `asyncio.Future`.

	.. versionadded:: 5.0
	"""
	if not future.cancelled():
		future.set_result(value)


# 为future设置exception
def future_set_exc_info(future, exc_info):
	"""Set the given ``exc_info`` as the `Future`'s exception.

	Understands both `asyncio.Future` and Tornado's extensions to
	enable better tracebacks on Python 2.

	.. versionadded:: 5.0
	"""
	if hasattr(future, 'set_exc_info'):
		# Tornado's Future
		future.set_exc_info(exc_info)
	else:
		# asyncio.Future
		future.set_exception(exc_info[1])


# 设置 当future完成时，回调
def future_add_done_callback(future, callback):
	"""Arrange to call ``callback`` when ``future`` is complete.

	``callback`` is invoked with one argument, the ``future``.
	回调带有一个参数future调用

	If ``future`` is already done, ``callback`` is invoked immediately.
	This may differ from the behavior of ``Future.add_done_callback``,
	which makes no such guarantee.

	.. versionadded:: 5.0
	"""
	if future.done():
		callback(future)
	else:
		future.add_done_callback(callback)
