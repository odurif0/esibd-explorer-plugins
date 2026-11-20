"""Windows account of the network share: connect it, and keep it in the Windows Credential Manager.

The password is never written in Explorer's files (``conf`` is itself copied to the share): it is given to Windows
once, which uses it for every later access to the server, as ``cmdkey /add:<server>`` does. Only the share and the
account name are kept by the plugin.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ctypes
import sys

CRED_TYPE_DOMAIN_PASSWORD = 2  # The kind of credential Windows uses for a file server.
CRED_PERSIST_LOCAL_MACHINE = 2  # Kept for this Windows user on this PC, after logging off.
RESOURCETYPE_DISK = 1
MESSAGES = {
    5: "access denied: this account cannot open the folder",
    53: "server not found on the network",
    67: "shared folder not found on the server",
    86: "wrong password",
    1219: "Windows already uses another account for this server; disconnect it (net use) or log off first",
    1311: "no logon server available",
    1326: "unknown account or wrong password",
    1909: "account locked",
    2202: "invalid account name",
}


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):  # CREDENTIALW
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", _FILETIME), ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.c_void_p), ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


class _NETRESOURCE(ctypes.Structure):  # NETRESOURCEW
    _fields_ = [("dwScope", ctypes.c_uint32), ("dwType", ctypes.c_uint32), ("dwDisplayType", ctypes.c_uint32),
                ("dwUsage", ctypes.c_uint32), ("lpLocalName", ctypes.c_wchar_p), ("lpRemoteName", ctypes.c_wchar_p),
                ("lpComment", ctypes.c_wchar_p), ("lpProvider", ctypes.c_wchar_p)]


class AccountError(Exception):
    pass


def server(share: str) -> str:
    """The server of a share: \\\\hive\\esi -> hive. Raises AccountError for anything else."""
    text = share.strip().replace("/", "\\")
    parts = [p for p in text.split("\\") if p]
    if not text.startswith("\\\\") or len(parts) < 2:
        raise AccountError(f"{share!r} is not a network folder (\\\\server\\folder)")
    return parts[0]


def message(code: int) -> str:
    return f"{MESSAGES.get(code, 'Windows error')} ({code})"


def available() -> bool:
    return sys.platform == "win32"


def connect(share: str, account: str, password: str) -> None:
    """Open the share with this account now; raises AccountError with the reason."""
    if not available():
        raise AccountError("network accounts are handled on Windows only")
    resource = _NETRESOURCE(dwType=RESOURCETYPE_DISK, lpRemoteName=share.strip().rstrip("\\"))
    add = ctypes.WinDLL("mpr").WNetAddConnection2W
    add.argtypes = [ctypes.POINTER(_NETRESOURCE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    add.restype = ctypes.c_uint32
    code = add(ctypes.byref(resource), password, account or None, 0)
    if code:
        raise AccountError(message(code))


def remember(share: str, account: str, password: str) -> None:
    """Keep the account in the Windows Credential Manager, for every later access to the server."""
    if not available():
        raise AccountError("network accounts are handled on Windows only")
    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _CREDENTIAL(Type=CRED_TYPE_DOMAIN_PASSWORD, TargetName=server(share), UserName=account,
                             CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(buffer, ctypes.c_void_p),
                             Persist=CRED_PERSIST_LOCAL_MACHINE)
    write = ctypes.WinDLL("advapi32", use_last_error=True).CredWriteW
    write.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
    write.restype = ctypes.c_int
    if not write(ctypes.byref(credential), 0):
        raise AccountError(f"the account could not be kept in Windows ({ctypes.get_last_error()})")
