"""Windows-only install, diagnostics, wizard, and Task Scheduler helpers."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .database import Database
from .runtime import SingletonProcessLock
from .setup_wizard import SecretStore, SecretStoreError

TASK_NAME = "FunPay Operations Background"

@dataclass(frozen=True)
class WindowsPaths:
    application: Path
    config: Path
    data: Path
    secrets: Path
    database: Path
    logs: Path
    backups: Path

def resolve_windows_paths(local_app_data: Path | None = None) -> WindowsPaths:
    root = local_app_data or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    base = root / "FunPay Operations"
    data = base / "data"
    return WindowsPaths(base / "app", base / "config", data, data / "secrets.dpapi", data / "funpay.sqlite3", data / "logs", data / "backups")

def initialise_windows_install(paths: WindowsPaths) -> None:
    for directory in (paths.application, paths.config, paths.data, paths.logs, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)
    Database(paths.database).initialize()

def diagnostics(paths: WindowsPaths) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        initialise_windows_install(paths)
        result["directories"] = "ok"
        result["write_permissions"] = "ok"
        database = Database(paths.database)
        with database.session() as connection:
            result["database"] = "ok" if connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else "error"
        result["migrations"] = "ok"
        lock = SingletonProcessLock(paths.data / "funpay-operations.lock")
        lock.acquire(); lock.release(); result["singleton"] = "ok"
    except (OSError, sqlite3.Error):
        result.update({"directories": "error", "write_permissions": "error", "database": "error", "migrations": "error", "singleton": "error"})
    result["dpapi"] = "available" if os.name == "nt" else "unavailable"
    result["catalog"] = "not_configured"
    result["hard_floors"] = "not_configured"
    result["funpay_adapter"] = "available"
    result["telegram"] = "not_configured"
    result["autostart"] = autostart_status()
    return result

def first_run(paths: WindowsPaths, *, configure_autostart: bool = False, executable: Path | None = None) -> dict[str, str]:
    initialise_windows_install(paths)
    result = diagnostics(paths)
    result.update({"service_catalog": "not_configured", "hard_floor": "not_configured", "funpay": "skipped", "telegram": "skipped", "trusted_sellers": "skipped"})
    if configure_autostart and executable is not None:
        install_autostart(executable); result["autostart"] = "installed"
    return result

def task_scheduler_command(executable: Path, *, action: str) -> list[str]:
    if action == "install":
        return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/DELAY", "0000:30", "/RL", "LIMITED", "/F", "/TR", f'"{executable}" --background']
    if action == "remove": return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if action == "status": return ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"]
    raise ValueError("unsupported autostart action")

def install_autostart(executable: Path, runner: Callable[..., object] = subprocess.run) -> None:
    runner(task_scheduler_command(executable, action="install"), check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
def remove_autostart(runner: Callable[..., object] = subprocess.run) -> None:
    runner(task_scheduler_command(Path("funpay-operations.exe"), action="remove"), check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
def autostart_status(runner: Callable[..., object] = subprocess.run) -> str:
    try:
        runner(task_scheduler_command(Path("funpay-operations.exe"), action="status"), check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)); return "installed"
    except (OSError, subprocess.CalledProcessError): return "not_configured"
