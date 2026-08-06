"""Windows DPAPI-backed local secret setup; the generated store is Git-ignored."""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
from ctypes import wintypes
from pathlib import Path


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(payload: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(payload)
    return DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_for_current_user(payload: bytes) -> bytes:
    """Encrypt bytes with Windows DPAPI and no user-interface prompt."""

    if os.name != "nt":
        raise OSError("Windows DPAPI is only available on Windows")
    source, source_buffer = _blob(payload)
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "funpay-operations", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def main() -> int:
    parser = argparse.ArgumentParser(description="Store one secret with Windows DPAPI for the current user.")
    parser.add_argument("set", help="literal command: set")
    parser.add_argument("key", help="logical secret name, for example telegram_bot_token")
    parser.add_argument("--store", type=Path, default=Path("data") / "secrets.dpapi")
    args = parser.parse_args()
    if args.set != "set" or not args.key.replace("_", "").isalnum():
        parser.error("use: funpay-setup set <alphanumeric_or_underscore_key>")

    value = getpass.getpass(f"Secret value for {args.key}: ")
    if not value:
        parser.error("empty secrets are not allowed")
    args.store.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({args.key: value}, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(protect_for_current_user(payload)).decode("ascii")
    args.store.write_text(encoded, encoding="ascii")
    print(f"Stored {args.key} with Windows DPAPI for the current user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
