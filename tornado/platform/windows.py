# -*- coding: utf-8 -*-
'''win32支持，还不好用'''
# NOTE: win32 support is currently experimental, and not recommended
# for production use.


from __future__ import absolute_import, division, print_function
import ctypes  # type: ignore
import ctypes.wintypes  # type: ignore

# See: http://msdn.microsoft.com/en-us/library/ms724935(VS.85).aspx
SetHandleInformation = ctypes.windll.kernel32.SetHandleInformation
SetHandleInformation.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD)  # noqa: E501
SetHandleInformation.restype = ctypes.wintypes.BOOL

HANDLE_FLAG_INHERIT = 0x00000001


def set_close_exec(fd):
    success = SetHandleInformation(fd, HANDLE_FLAG_INHERIT, 0)
    if not success:
        raise ctypes.WinError()
