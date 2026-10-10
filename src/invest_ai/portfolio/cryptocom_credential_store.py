"""Opt-in storage for read-only Crypto.com credentials in Windows Credential Manager."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB = 5 * 1024
_DEFAULT_TARGET = "INVEST-AI/Crypto.com Exchange/Read-only"


class CryptoComCredentialStoreError(ValueError):
    """Credential Manager is unavailable or its entry cannot be read safely."""


@dataclass(frozen=True, slots=True)
class CryptoComCredentials:
    api_key: str
    api_secret: str


class CredentialStore(Protocol):
    @property
    def available(self) -> bool: ...
    def has_credentials(self) -> bool: ...
    def store(self, api_key: str, api_secret: str) -> None: ...
    def load(self) -> CryptoComCredentials | None: ...
    def delete(self) -> None: ...


class _CredentialAttributeW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(wintypes.BYTE)),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttributeW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCryptoComCredentialStore:
    """Store one read-only API key pair under the current Windows user profile."""

    def __init__(self, target: str = _DEFAULT_TARGET) -> None:
        if not isinstance(target, str) or not target or len(target) > 256:
            raise ValueError("A valid Windows credential target is required.")
        self._target = target

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def has_credentials(self) -> bool:
        return self.load() is not None

    def store(self, api_key: str, api_secret: str) -> None:
        if not self.available:
            raise CryptoComCredentialStoreError("Le coffre d’identifiants Windows n’est pas disponible.")
        self._validate(api_key, api_secret)
        raw = bytearray(json.dumps(
            {"api_key": api_key, "api_secret": api_secret},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if len(raw) > _MAX_CREDENTIAL_BLOB:
            raw[:] = b"\0" * len(raw)
            raise CryptoComCredentialStoreError("Les identifiants dépassent la taille du coffre Windows.")
        buffer = ctypes.create_string_buffer(bytes(raw))
        try:
            api = self._api()
            entry = _CredentialW()
            entry.Type = _CRED_TYPE_GENERIC
            entry.TargetName = self._target
            entry.CredentialBlobSize = len(raw)
            entry.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
            entry.Persist = _CRED_PERSIST_LOCAL_MACHINE
            entry.UserName = "Crypto.com Exchange read-only"
            if not api.CredWriteW(ctypes.byref(entry), 0):
                raise CryptoComCredentialStoreError("Windows n’a pas pu enregistrer l’identifiant dans son coffre.")
        finally:
            ctypes.memset(buffer, 0, len(buffer))
            raw[:] = b"\0" * len(raw)

    def load(self) -> CryptoComCredentials | None:
        if not self.available:
            return None
        api = self._api()
        pointer = ctypes.POINTER(_CredentialW)()
        if not api.CredReadW(self._target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return None
            raise CryptoComCredentialStoreError("Windows n’a pas pu lire l’identifiant du coffre.")
        raw = bytearray()
        try:
            entry = pointer.contents
            if entry.CredentialBlobSize <= 0 or entry.CredentialBlobSize > _MAX_CREDENTIAL_BLOB:
                raise CryptoComCredentialStoreError("L’entrée du coffre Windows a une taille invalide.")
            raw = bytearray(ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise CryptoComCredentialStoreError("L’entrée du coffre Windows est invalide.") from None
            if not isinstance(payload, dict) or set(payload) != {"api_key", "api_secret"}:
                raise CryptoComCredentialStoreError("L’entrée du coffre Windows est invalide.")
            credentials = CryptoComCredentials(payload["api_key"], payload["api_secret"])
            self._validate(credentials.api_key, credentials.api_secret)
            return credentials
        finally:
            if raw:
                raw[:] = b"\0" * len(raw)
            api.CredFree(pointer)

    def delete(self) -> None:
        if not self.available:
            raise CryptoComCredentialStoreError("Le coffre d’identifiants Windows n’est pas disponible.")
        api = self._api()
        if not api.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return
            raise CryptoComCredentialStoreError("Windows n’a pas pu supprimer l’identifiant du coffre.")

    @staticmethod
    def _validate(api_key: str, api_secret: str) -> None:
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 256 or "\n" in api_key
                or not isinstance(api_secret, str) or not api_secret or len(api_secret) > 512
                or "\n" in api_secret):
            raise CryptoComCredentialStoreError("Les identifiants Crypto.com ne sont pas valides.")

    @staticmethod
    def _api():
        if os.name != "nt":
            raise CryptoComCredentialStoreError("Le coffre d’identifiants Windows n’est pas disponible.")
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CredentialW))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api


__all__ = [
    "CredentialStore", "CryptoComCredentialStoreError", "CryptoComCredentials",
    "WindowsCryptoComCredentialStore",
]
