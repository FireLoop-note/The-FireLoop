"""单实例互斥（ctypes 直调 Win32 `CreateMutexW`，零第三方依赖）。

命名互斥体固定为 ``Local\\FireLoopAgent``（见 docs/fire-loop-方案.md 第 1 节）。
第二个实例启动时，``CreateMutexW`` 仍然成功返回句柄，但 ``GetLastError()``
为 ``ERROR_ALREADY_EXISTS``，据此判断"已经有实例在跑"。

注意：这里用 ``bInitialOwner=False``。单实例检测只依赖内核对象是否已存在，
不依赖"谁拥有互斥体"，所以拿到句柄后既不 Wait 也不 ReleaseMutex，
进程退出 / release() 里 ``CloseHandle`` 即可。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from . import log

DEFAULT_MUTEX_NAME = r"Local\FireLoopAgent"

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CreateMutexW = _kernel32.CreateMutexW
_CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_CreateMutexW.restype = wintypes.HANDLE

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """命名互斥体单实例锁。

    acquire() -> True 表示抢到（当前进程是唯一实例）；
    False 表示已有实例，调用方应直接退出。
    """

    def __init__(self, name: str = DEFAULT_MUTEX_NAME):
        self.name = name
        # 句柄必须挂在实例上：绝不能让临时变量被回收后被 GC 关掉。
        self._handle = None

    @property
    def held(self) -> bool:
        """当前是否持有互斥体句柄。"""
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True

        handle = _CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(last_error)

        if last_error == ERROR_ALREADY_EXISTS:
            # 打开的是已有互斥体的句柄，自己用不上，立刻关掉，避免影响对方生命周期。
            _CloseHandle(handle)
            log.log_event(
                "single_instance_busy",
                level="warning",
                name=self.name,
                pid=os.getpid(),
            )
            return False

        self._handle = handle
        log.log_event("single_instance_acquired", name=self.name, pid=os.getpid())
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        _CloseHandle(handle)
        log.log_event("single_instance_released", name=self.name, pid=os.getpid())
