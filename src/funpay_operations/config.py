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
    logs_directory: Path
    backups_directory: Path
    operation_mode: str
    operations_enabled: bool
    poll_interval_seconds: int
    reconnect_initial_seconds: int
    reconnect_max_seconds: int
    funpay_credential_key: str
    telegram_token_key: str
    allowed_telegram_user_ids: tuple[int, ...]
    default_currency: str
    hard_floor: int | None
    funpay_request_timeout_seconds: int = 15
    funpay_min_request_interval_seconds: float = 1.0
    funpay_retry_attempts: int = 3
    funpay_read_endpoints: tuple[tuple[str, str], ...] = ()
    telegram_enabled: bool = False
    telegram_long_poll_timeout_seconds: int = 25
    telegram_notification_user_id: int | None = None
    funpay_message_notifications_enabled: bool = False
    funpay_message_poll_interval_seconds: int = 5
    funpay_reply_endpoint: str = ""
    funpay_auto_reply_enabled: bool = False
    funpay_dialog_inactive_after_seconds: int = 86400


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


def _child_directory(parent: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ConfigurationError(f"{field} must be a simple directory name")
    return parent / value


def _secret_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or not value.replace("_", "").isalnum():
        raise ConfigurationError(f"{field} must be an alphanumeric or underscore secret key")
    return value


def _read_endpoints(value: Any) -> tuple[tuple[str, str], ...]:
    endpoints = _mapping(value, "funpay.read_endpoints")
    supported = {"profile", "own_lots", "seller_lots", "dialogs", "new_messages", "bump_availability"}
    unexpected = set(endpoints) - supported
    if unexpected:
        raise ConfigurationError(f"unsupported FunPay read endpoint: {sorted(unexpected)[0]}")
    result: list[tuple[str, str]] = []
    for name, path in endpoints.items():
        if not isinstance(path, str) or not path.startswith("/") or "//" in path or ":" in path:
            raise ConfigurationError(f"funpay.read_endpoints.{name} must be a relative path")
        result.append((name, path))
    return tuple(sorted(result))


def _optional_relative_path(value: Any, field: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not value.startswith("/") or "//" in value or ":" in value:
        raise ConfigurationError(f"{field} must be an empty or relative path")
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
    logs_directory = _child_directory(data_directory, storage.get("logs_directory", "logs"), "storage.logs_directory")
    backups_directory = _child_directory(
        data_directory, storage.get("backups_directory", "backups"), "storage.backups_directory"
    )

    raw_allowed_users = telegram.get("allowed_user_ids", [])
    if not isinstance(raw_allowed_users, list) or not all(isinstance(user_id, int) for user_id in raw_allowed_users):
        raise ConfigurationError("telegram.allowed_user_ids must be a list of integers")
    telegram_enabled = _bool(telegram.get("enabled", False), "telegram.enabled")
    telegram_timeout = _positive_int(
        telegram.get("long_poll_timeout_seconds", 25), "telegram.long_poll_timeout_seconds"
    )
    if telegram_timeout > 50:
        raise ConfigurationError("telegram.long_poll_timeout_seconds must not exceed 50")
    notification_user_id = telegram.get("notification_user_id")
    if notification_user_id is not None and (
        not isinstance(notification_user_id, int) or isinstance(notification_user_id, bool)
    ):
        raise ConfigurationError("telegram.notification_user_id must be an integer or null")
    hard_floor = lots.get("hard_floor")
    if hard_floor is not None:
        hard_floor = _positive_int(hard_floor, "lots.hard_floor")

    poll_interval = _positive_int(operations.get("poll_interval_seconds", 30), "operations.poll_interval_seconds")
    reconnect_initial = _positive_int(
        operations.get("reconnect_initial_seconds", 5), "operations.reconnect_initial_seconds"
    )
    reconnect_max = _positive_int(operations.get("reconnect_max_seconds", 60), "operations.reconnect_max_seconds")
    if reconnect_max < reconnect_initial:
        raise ConfigurationError("operations.reconnect_max_seconds must be at least the initial interval")
    operation_mode = os.getenv("FUNPAY_MANAGER_MODE", str(operations.get("mode", "safe"))).lower()
    if operation_mode not in {"safe", "dry_run", "live"}:
        raise ConfigurationError("operations.mode must be safe, dry_run, or live")
    operations_enabled = _bool(operations.get("enabled", False), "operations.enabled")
    if operation_mode != "live" and operations_enabled:
        raise ConfigurationError("operations.enabled may only be true in live mode")
    read_endpoints = _read_endpoints(funpay.get("read_endpoints", {}))
    request_timeout = _positive_int(
        funpay.get("request_timeout_seconds", 15), "funpay.request_timeout_seconds"
    )
    request_interval = funpay.get("min_request_interval_seconds", 1.0)
    if not isinstance(request_interval, (int, float)) or isinstance(request_interval, bool) or request_interval < 0:
        raise ConfigurationError("funpay.min_request_interval_seconds must be a non-negative number")
    retry_attempts = _positive_int(funpay.get("retry_attempts", 3), "funpay.retry_attempts")
    reply_endpoint = _optional_relative_path(funpay.get("reply_endpoint", ""), "funpay.reply_endpoint")
    if operations_enabled and not reply_endpoint:
        raise ConfigurationError("live operations require funpay.reply_endpoint")
    message_notifications_enabled = _bool(
        funpay.get("message_notifications_enabled", False), "funpay.message_notifications_enabled"
    )
    message_poll_interval = _positive_int(
        funpay.get("message_poll_interval_seconds", 5), "funpay.message_poll_interval_seconds"
    )
    if message_poll_interval < request_interval:
        raise ConfigurationError("funpay.message_poll_interval_seconds must respect the request interval")
    auto_reply_enabled = _bool(funpay.get("auto_reply_enabled", False), "funpay.auto_reply_enabled")
    inactive_after = _positive_int(
        funpay.get("dialog_inactive_after_seconds", 86400), "funpay.dialog_inactive_after_seconds"
    )
    if message_notifications_enabled and (
        not telegram_enabled or notification_user_id is None or notification_user_id not in raw_allowed_users
    ):
        raise ConfigurationError(
            "enabled FunPay message notifications require an enabled Telegram bot and allowlisted notification_user_id"
        )
    return Settings(
        environment=str(app.get("environment", "development")),
        log_level=os.getenv("FUNPAY_MANAGER_LOG_LEVEL", str(app.get("log_level", "INFO"))).upper(),
        data_directory=data_directory,
        database_path=data_directory / database_file,
        logs_directory=logs_directory,
        backups_directory=backups_directory,
        operation_mode=operation_mode,
        operations_enabled=operations_enabled,
        poll_interval_seconds=poll_interval,
        reconnect_initial_seconds=reconnect_initial,
        reconnect_max_seconds=reconnect_max,
        funpay_credential_key=_secret_key(funpay.get("credential_key", "funpay_session"), "funpay.credential_key"),
        telegram_token_key=_secret_key(telegram.get("token_key", "telegram_bot_token"), "telegram.token_key"),
        allowed_telegram_user_ids=tuple(raw_allowed_users),
        default_currency=str(lots.get("default_currency", "RUB")),
        hard_floor=hard_floor,
        funpay_request_timeout_seconds=request_timeout,
        funpay_min_request_interval_seconds=float(request_interval),
        funpay_retry_attempts=retry_attempts,
        funpay_read_endpoints=read_endpoints,
        telegram_enabled=telegram_enabled,
        telegram_long_poll_timeout_seconds=telegram_timeout,
        telegram_notification_user_id=notification_user_id,
        funpay_message_notifications_enabled=message_notifications_enabled,
        funpay_message_poll_interval_seconds=message_poll_interval,
        funpay_reply_endpoint=reply_endpoint,
        funpay_auto_reply_enabled=auto_reply_enabled,
        funpay_dialog_inactive_after_seconds=inactive_after,
    )
