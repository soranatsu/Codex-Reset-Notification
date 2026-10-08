"""Only this application's two credential targets; no enumeration or browser access."""
import ctypes
import hashlib
import os
from ctypes import wintypes


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsVault:
    def __init__(self, directory):
        digest = hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:24]
        self.prefix = "ResetNotice/" + digest + "/"
        if os.name != "nt":
            raise RuntimeError("Windows credential storage unavailable")
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]
        self.api.CredFree.restype = None

    def target(self, key):
        if key not in ("smtp", "bark"):
            raise ValueError("Unknown credential target")
        return self.prefix + key

    def read(self, key):
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not self.api.CredReadW(self.target(key), 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return ""
            raise OSError("Credential storage unavailable")
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob,
                                    credential.CredentialBlobSize).decode("utf-16-le")
        finally:
            self.api.CredFree(pointer)

    def write(self, key, value):
        encoded = value.encode("utf-16-le")
        if len(encoded) > 5120:
            raise ValueError("Credential too long")
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = CREDENTIAL()
        credential.Type, credential.Persist = 1, 2
        credential.TargetName = self.target(key)
        credential.UserName = "ResetNotice"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = blob
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            raise OSError("Credential storage unavailable")

    def delete(self, key):
        if not self.api.CredDeleteW(self.target(key), 1, 0) and ctypes.get_last_error() != 1168:
            raise OSError("Credential storage unavailable")


class Secrets:
    def __init__(self, vault):
        self.vault, self.memory = vault, {}

    def get(self, key, config):
        if key in self.memory:
            return self.memory[key]
        return self.vault.read(key) if config[key + "_saved"] else ""

    def set(self, key, value, remember):
        if remember:
            self.vault.write(key, value)
        else:
            self.vault.delete(key)
        self.memory[key] = value

    def clear(self):
        self.memory.clear()
