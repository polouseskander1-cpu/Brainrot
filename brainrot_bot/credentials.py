"""API keys and login tokens for the posting platforms.

Kept out of config.yaml (so you can share your settings safely). On Windows the file is encrypted
with your Windows account (DPAPI): copied to another PC or user, it can't be read. Elsewhere it is
a JSON file only your user can open.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("brainrot")

MAGIC = b"BRAINROT-DPAPI-1\n"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    func.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    ui_forbidden = 0x01
    description = "Brainrot bot" if protect else None
    if not func(ctypes.byref(blob_in), description, None, None, None, ui_forbidden, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "Windows could not " + ("encrypt" if protect else "decrypt") + " the credentials")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


class Credentials:
    def __init__(self, base: Path, encrypt: bool | None = None):
        self.encrypt = (os.name == "nt") if encrypt is None else encrypt
        self.path = base.with_suffix(".dat" if self.encrypt else ".json")
        self.data: dict[str, dict] = {}
        self.load()

    def _mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def reload_if_changed(self) -> None:
        """Pick up accounts connected from the menu while the bot keeps running."""
        if self._mtime() != self._loaded_mtime:
            self.load()

    def load(self) -> None:
        self.data = {}
        self._loaded_mtime = self._mtime()
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
            if raw.startswith(MAGIC):
                raw = _dpapi(base64.b64decode(raw[len(MAGIC):]), protect=False)
            loaded = json.loads(raw.decode("utf-8"))
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            log.warning("Saved account logins could not be read (%s). Connect the accounts again in Settings.", exc)

    def save(self) -> None:
        raw = json.dumps(self.data, indent=2).encode("utf-8")
        if self.encrypt:
            raw = MAGIC + base64.b64encode(_dpapi(raw, protect=True))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(raw)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._loaded_mtime = self._mtime()

    def get(self, platform: str) -> dict:
        return self.data.get(platform) or {}

    def set(self, platform: str, values: dict) -> None:
        self.data[platform] = dict(values)
        self.save()

    def update(self, platform: str, **values) -> None:
        self.data.setdefault(platform, {}).update(values)
        self.save()

    def remove(self, platform: str) -> None:
        if self.data.pop(platform, None) is not None:
            self.save()
