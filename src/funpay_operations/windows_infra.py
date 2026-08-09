"""Windows-only install, diagnostics, wizard, and Task Scheduler helpers."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .database import Database
from .runtime import DuplicateProcessError, SingletonProcessLock

TASK_NAME = "FunPay Operations Background"
SAFE_CONFIG_NAME = "config.yaml"

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

def safe_config_path(paths: WindowsPaths) -> Path:
    return paths.config / SAFE_CONFIG_NAME

def ensure_safe_config(paths: WindowsPaths) -> tuple[Path, bool]:
    """Create a secret-free safe-mode config once and preserve owner edits."""

    path = safe_config_path(paths)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "app": {
            "environment": "production",
            "log_level": "INFO",
            "data_directory": str(paths.data),
        },
        "storage": {
            "database_file": paths.database.name,
            "logs_directory": paths.logs.name,
            "backups_directory": paths.backups.name,
            "backup_retention_count": 7,
            "backup_interval_seconds": 3600,
        },
        "operations": {
            "mode": "safe",
            "enabled": False,
            "poll_interval_seconds": 30,
            "reconnect_initial_seconds": 5,
            "reconnect_max_seconds": 60,
        },
        "funpay": {
            "credential_key": "funpay_session",
            "request_timeout_seconds": 15,
            "min_request_interval_seconds": 1.0,
            "retry_attempts": 3,
            "message_notifications_enabled": False,
            "message_poll_interval_seconds": 5,
            "auto_reply_enabled": False,
        },
        "telegram": {
            "enabled": False,
            "token_key": "telegram_bot_token",
            "allowed_user_ids": [],
            "long_poll_timeout_seconds": 25,
            "notification_user_id": None,
        },
        "lots": {"default_currency": "RUB", "hard_floor": None},
    }
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path, True

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
    except (OSError, sqlite3.Error):
        result.update({
            "directories": "error", "write_permissions": "error",
            "database": "error", "migrations": "error",
        })
    try:
        lock = SingletonProcessLock(paths.data / "funpay-operations.lock")
        lock.acquire()
        lock.release()
        result["singleton"] = "ok"
    except DuplicateProcessError:
        result["singleton"] = "in_use"
    except OSError:
        result["singleton"] = "error"
    result["dpapi"] = "available" if os.name == "nt" else "unavailable"
    result["catalog"] = "not_configured"
    result["hard_floors"] = "not_configured"
    result["funpay_adapter"] = "available"
    result["telegram"] = "not_configured"
    result["autostart"] = autostart_status()
    return result

def first_run(paths: WindowsPaths, *, configure_autostart: bool = False, executable: Path | None = None) -> dict[str, str]:
    initialise_windows_install(paths)
    _, config_created = ensure_safe_config(paths)
    result = diagnostics(paths)
    result.update({
        "config": "created" if config_created else "existing",
        "service_catalog": "not_configured", "hard_floor": "not_configured",
        "funpay": "skipped", "telegram": "skipped", "trusted_sellers": "skipped",
    })
    if configure_autostart and executable is not None:
        install_autostart(executable)
        result["autostart"] = "installed"
    return result

def task_scheduler_command(executable: Path, *, action: str) -> list[str]:
    if action == "install":
        if any(character in str(executable) for character in ('"', "\r", "\n")):
            raise ValueError("autostart executable path contains unsafe characters")
        return [
            "schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
            "/DELAY", "0000:30", "/RL", "LIMITED", "/F", "/TR",
            f'"{executable}" --background',
        ]
    if action == "remove":
        return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if action == "status":
        return ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"]
    raise ValueError("unsupported autostart action")

def resolve_background_executable(current_executable: Path | None = None) -> Path:
    """Select the noconsole sibling from a standalone build."""

    current = (current_executable or Path(sys.executable)).resolve()
    if current.name.casefold() == "funpay-operations.exe":
        return current
    if current.name.casefold() == "funpay-operations-cli.exe":
        background = current.with_name("funpay-operations.exe")
        if background.is_file():
            return background
        raise FileNotFoundError("background executable is missing next to the CLI executable")
    raise RuntimeError("autostart installation requires the standalone Windows executable")

def install_autostart(executable: Path, runner: Callable[..., object] = subprocess.run) -> None:
    runner(
        task_scheduler_command(executable, action="install"), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def remove_autostart(runner: Callable[..., object] = subprocess.run) -> None:
    runner(
        task_scheduler_command(Path("funpay-operations.exe"), action="remove"), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def autostart_status(runner: Callable[..., object] = subprocess.run) -> str:
    try:
        runner(
            task_scheduler_command(Path("funpay-operations.exe"), action="status"),
            check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "installed"
    except (OSError, subprocess.CalledProcessError):
        return "not_configured"
