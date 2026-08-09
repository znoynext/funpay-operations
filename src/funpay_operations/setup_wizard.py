"""Windows DPAPI local setup wizard. Secret values never enter configuration or logs."""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import shutil
from ctypes import wintypes
from pathlib import Path
from typing import Any


class SecretStoreError(RuntimeError):
    """Raised for unavailable or malformed local DPAPI secret storage."""


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


CRYPTPROTECT_UI_FORBIDDEN = 0x1


def mask_secret(value: str | None) -> str:
    """Return a non-sensitive diagnostic representation."""

    if not value:
        return "<missing>"
    return "<masked>"


def _blob(payload: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(payload)
    return DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt32() -> Any:
    if os.name != "nt":
        raise SecretStoreError("Windows DPAPI is only available on Windows")
    return ctypes.windll.crypt32


def protect_for_current_user(payload: bytes) -> bytes:
    """Encrypt bytes with Windows DPAPI and no user-interface prompt."""

    source, source_buffer = _blob(payload)
    target = DataBlob()
    crypt32 = _crypt32()
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


def unprotect_for_current_user(payload: bytes) -> bytes:
    """Decrypt bytes protected for the same Windows user with DPAPI."""

    source, source_buffer = _blob(payload)
    target = DataBlob()
    crypt32 = _crypt32()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


class SecretStore:
    """A per-Windows-user encrypted JSON mapping in an ignored local file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, key: str) -> str | None:
        values = self._read_all()
        return values.get(key)

    def set(self, key: str, value: str) -> None:
        if not key.replace("_", "").isalnum() or not value:
            raise SecretStoreError("secret key and value must be non-empty")
        values = self._read_all()
        values[key] = value
        encrypted = protect_for_current_user(json.dumps(values).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        temporary_path.write_text(base64.b64encode(encrypted).decode("ascii"), encoding="ascii")
        temporary_path.replace(self.path)

    def diagnostics(self, keys: tuple[str, ...]) -> dict[str, str]:
        values = self._read_all()
        return {key: mask_secret(values.get(key)) for key in keys}

    def _read_all(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            encrypted = base64.b64decode(self.path.read_text(encoding="ascii"), validate=True)
            loaded = json.loads(unprotect_for_current_user(encrypted).decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SecretStoreError("unable to read the local DPAPI secret store") from error
        if not isinstance(loaded, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in loaded.items()):
            raise SecretStoreError("local DPAPI secret store has an invalid format")
        return loaded


def _initialise_files(config_path: Path, env_path: Path) -> None:
    for target, source in ((config_path, Path("config.example.yaml")), (env_path, Path(".env.example"))):
        if target.exists():
            raise SecretStoreError(f"refusing to overwrite existing file: {target}")
        if not source.is_file():
            raise SecretStoreError(f"template is missing: {source}")
        shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows DPAPI local setup for funpay-operations.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialise = subcommands.add_parser("init", help="create config.yaml and .env from safe templates")
    initialise.add_argument("--config", type=Path, default=Path("config.yaml"))
    initialise.add_argument("--env-file", type=Path, default=Path(".env"))
    set_value_command = subcommands.add_parser("set", help="prompt for and store a secret with DPAPI")
    set_value_command.add_argument("key", help="logical secret key, for example telegram_bot_token")
    set_value_command.add_argument("--store", type=Path, default=Path("data") / "secrets.dpapi")
    set_funpay_session = subcommands.add_parser(
        "set-funpay-session", help="prompt for the two FunPay session cookies and store one DPAPI value"
    )
    set_funpay_session.add_argument("--key", default="funpay_session", help="logical DPAPI key for the session")
    set_funpay_session.add_argument("--store", type=Path, default=Path("data") / "secrets.dpapi")
    diagnostics = subcommands.add_parser("diagnostics", help="show only masked secret presence")
    diagnostics.add_argument("--store", type=Path, default=Path("data") / "secrets.dpapi")
    diagnostics.add_argument("keys", nargs="*", default=["telegram_bot_token", "funpay_session"])
    args = parser.parse_args()

    if args.command == "init":
        _initialise_files(args.config, args.env_file)
        print("Created safe local configuration templates. Add secrets only with the set command.")
        return 0
    if args.command == "set":
        value = getpass.getpass(f"Secret value for {args.key}: ")
        SecretStore(args.store).set(args.key, value)
        print(f"Stored {args.key} with Windows DPAPI for the current user.")
        return 0
    if args.command == "set-funpay-session":
        golden_key = getpass.getpass("FunPay golden_key: ")
        golden_seal = getpass.getpass("FunPay golden_seal: ")
        if not golden_key or not golden_seal:
            raise SecretStoreError("FunPay session cookies must be non-empty")
        SecretStore(args.store).set(args.key, json.dumps({"golden_key": golden_key, "golden_seal": golden_seal}))
        print(f"Stored {args.key} with Windows DPAPI for the current user.")
        return 0
    for key, state in SecretStore(args.store).diagnostics(tuple(args.keys)).items():
        print(f"{key}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
