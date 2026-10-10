"""Windows session named mutex used to prevent accidental duplicate runtimes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from invest_ai.core.errors import AlreadyRunningError


class InstanceGuard:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        digest = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:24]
        self.name = f"Local\\INVEST_AI_RUNTIME_{digest}"
        self._handle = None
        self._owned = False

    def acquire(self) -> "InstanceGuard":
        if os.name != "nt":
            raise OSError("InstanceGuard V0 est implémenté uniquement pour Windows.")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW a échoué")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            raise AlreadyRunningError("Une instance INVEST-AI est déjà active.")
        self._handle = handle
        self._owned = True
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if self._owned:
            kernel32.ReleaseMutex(self._handle)
        kernel32.CloseHandle(self._handle)
        self._handle = None
        self._owned = False

    def __enter__(self) -> "InstanceGuard":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
