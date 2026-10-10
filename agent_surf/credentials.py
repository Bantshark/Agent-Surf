"""Fix 27: the Anthropic API key from the Windows Credential Manager.

Stdlib ctypes only (advapi32 CredReadW / CredWriteW / CredDeleteW / CredFree).
A Generic credential named ``agent-surf/anthropic``; its blob is decoded as
UTF-16-LE (what Windows tools write) or UTF-8, with a trailing NUL stripped.
Values are never printed, logged or put in an exception message. Elsewhere
than Windows only the environment (ANTHROPIC_API_KEY) is used.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any

log = logging.getLogger("agent_surf.credentials")

ANTHROPIC_TARGET = "agent-surf/anthropic"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
DISABLE_ENV = "AGENT_SURF_NO_CREDMAN"   # =1: never read the Credential Manager (tests set it)

DWORD = ctypes.c_uint32
LPWSTR = ctypes.c_wchar_p


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", DWORD),
        ("Type", DWORD),
        ("TargetName", LPWSTR),
        ("Comment", LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", DWORD),
        ("AttributeCount", DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", LPWSTR),
        ("UserName", LPWSTR),
    ]


class CredentialError(RuntimeError):
    """Carries an error code only, never a value."""


def is_windows() -> bool:
    return sys.platform == "win32"


def _advapi32() -> Any:
    lib = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    lib.CredReadW.argtypes = [LPWSTR, DWORD, DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    lib.CredReadW.restype = ctypes.c_int
    lib.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), DWORD]
    lib.CredWriteW.restype = ctypes.c_int
    lib.CredDeleteW.argtypes = [LPWSTR, DWORD, DWORD]
    lib.CredDeleteW.restype = ctypes.c_int
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


def _last_error() -> int:
    return ctypes.get_last_error()  # type: ignore[attr-defined]  (Windows only)


def available(env: Any = None) -> bool:
    env = os.environ if env is None else env
    return is_windows() and env.get(DISABLE_ENV) != "1"


def decode_blob(blob: bytes) -> str | None:
    """UTF-16-LE when the blob looks like it (even length, NUL high bytes),
    else UTF-8; a trailing NUL and surrounding whitespace are stripped."""
    if not blob:
        return None
    text = None
    if len(blob) % 2 == 0 and blob[1::2].count(0) * 2 >= len(blob) // 2:
        try:
            text = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            text = None
    if text is None:
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("credential blob is neither UTF-16-LE nor UTF-8") from None
    text = text.rstrip("\x00").strip()
    return text or None


def read_generic(target: str) -> str | None:
    """The Generic credential's secret, or None if there is none."""
    api = _advapi32()
    pcred = ctypes.POINTER(CREDENTIALW)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        err = _last_error()
        if err == ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"CredReadW failed (Windows error {err})")
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        api.CredFree(pcred)
    return decode_blob(blob)


def write_generic(target: str, secret: str, user: str = "agent-surf") -> None:
    data = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not _advapi32().CredWriteW(ctypes.byref(cred), 0):
        raise CredentialError(f"CredWriteW failed (Windows error {_last_error()})")


def delete_generic(target: str) -> bool:
    """True if a credential was deleted, False if there was none."""
    if _advapi32().CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    err = _last_error()
    if err == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"CredDeleteW failed (Windows error {err})")


def anthropic_from_credman(env: Any = None) -> str | None:
    if not available(env):
        return None
    try:
        return read_generic(ANTHROPIC_TARGET)
    except (CredentialError, OSError) as e:
        log.warning("could not read the Anthropic key from the Credential Manager (%s)",
                    e if isinstance(e, CredentialError) else type(e).__name__)
        return None
