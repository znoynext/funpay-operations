"""Configuration loading with no secret values persisted in YAML or logs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when the local configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class Settings:
    environment: str
    log_level: str
    data_directory: Path
    database_path: Path
    operations_enabled: bool
    poll_interval_seconds: int
    funpay_credential_key: str
    telegram_token_key: str
    allowed_telegram_user_ids: tuple[int, ...]
    default_currency: str
    hard_floor: int | None


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be a mapping")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a boolean")
    return value


def load_settings(*, config_path: Path, env_path: Path) -> Settings:
    """Load non-sensitive settings from YAML and optional `.env` overrides."""

    load_dotenv(dotenv_path=env_path, override=False)
    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as config_file:
        document = yaml.safe_load(config_file) or {}
    root = _mapping(document, "root")
    app = _mapping(root.get("app", {}), "app")
    storage = _mapping(root.get("storage", {}), "storage")
    operations = _mapping(root.get("operations", {}), "operations")
    funpay = _mapping(root.get("funpay", {}), "funpay")
    telegram = _mapping(root.get("telegram", {}), "telegram")
    lots = _mapping(root.get("lots", {}), "lots")

    data_directory = Path(os.getenv("FUNPAY_MANAGER_DATA_DIRECTORY", app.get("data_directory", "data")))
    database_file = storage.get("database_file", "funpay.sqlite3")
    if not isinstance(database_file, str) or Path(database_file).name != database_file:
        raise ConfigurationError("storage.database_file must be a filename, not a path")

    raw_allowed_users = telegram.get("allowed_user_ids", [])
    if not isinstance(raw_allowed_users, list) or not all(isinstance(user_id, int) for user_id in raw_allowed_users):
        raise ConfigurationError("telegram.allowed_user_ids must be a list of integers")
    hard_floor = lots.get("hard_floor")
    if hard_floor is not None:
        hard_floor = _positive_int(hard_floor, "lots.hard_floor")

    poll_interval = _positive_int(operations.get("poll_interval_seconds", 30), "operations.poll_interval_seconds")
    return Settings(
        environment=str(app.get("environment", "development")),
        log_level=os.getenv("FUNPAY_MANAGER_LOG_LEVEL", str(app.get("log_level", "INFO"))).upper(),
        data_directory=data_directory,
        database_path=data_directory / database_file,
        operations_enabled=_bool(operations.get("enabled", False), "operations.enabled"),
        poll_interval_seconds=poll_interval,
        funpay_credential_key=str(funpay.get("credential_key", "funpay_session")),
        telegram_token_key=str(telegram.get("token_key", "telegram_bot_token")),
        allowed_telegram_user_ids=tuple(raw_allowed_users),
        default_currency=str(lots.get("default_currency", "RUB")),
        hard_floor=hard_floor,
    )
