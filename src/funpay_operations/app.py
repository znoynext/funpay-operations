"""Application composition root."""

from __future__ import annotations

from pathlib import Path

from .config import Settings, load_settings
from .auto_reply import AutoReplyService
from .database import Database
from .funpay import build_read_client, build_reply_client
from .logging_setup import configure_logging
from .notifications import FunPayMessageNotifier
from .replies import FunPayReplyRouter
from .repositories import AutoReplyRepository, DialogRepository, ReplyRepository, TaskStateRepository, TelegramMessageLinkRepository
from .session_health import FunPaySessionGuard, SESSION_EXPIRED_MARKUP, SESSION_EXPIRED_TEXT
from .setup_wizard import SecretStore
from .telegram import build_telegram_bot
from .telegram import TelegramError
from .telegram_control import CompositeTelegramRouter, EmergencyStopGate, MockControlService, TelegramControlRouter
from .telegram_auth import LocalFunPayAuthRequest, TelegramFunPayAuthRouter
from .tasks import BackgroundRunner
from .runtime import SingletonProcessLock
from .windows_infra import configured_telegram_owner_id, resolve_windows_paths


class Application:
    """Composes local services without initiating external operations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = configure_logging(settings.logs_directory, settings.log_level)
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.task_states = TaskStateRepository(self.database)
        confirmed_owner = configured_telegram_owner_id(self.database)
        self.allowed_telegram_user_ids = (confirmed_owner,) if confirmed_owner is not None else settings.allowed_telegram_user_ids
        self.telegram_enabled = settings.telegram_enabled or confirmed_owner is not None
        self.telegram_notification_user_id = confirmed_owner if confirmed_owner is not None else settings.telegram_notification_user_id
        secret_store = SecretStore(settings.data_directory / "secrets.dpapi")
        self.session_guard = FunPaySessionGuard(self.task_states, secret_store.path)
        self.funpay = build_read_client(
            settings, secret_store
        )
        self.funpay_replies = build_reply_client(settings, self.funpay)
        self.telegram = build_telegram_bot(
            settings, secret_store, self.task_states, self.logger, allowed_user_ids=self.allowed_telegram_user_ids,
        )
        self.emergency_stop = EmergencyStopGate(self.task_states)
        self.local_funpay_auth = LocalFunPayAuthRequest(resolve_windows_paths(), self.task_states)
        self.telegram.set_interaction_router(
            CompositeTelegramRouter(
                TelegramFunPayAuthRouter(self.allowed_telegram_user_ids, self.local_funpay_auth),
                TelegramControlRouter(self.allowed_telegram_user_ids, self.task_states, MockControlService(), self.emergency_stop),
                FunPayReplyRouter(
                    self.allowed_telegram_user_ids, TelegramMessageLinkRepository(self.database),
                    ReplyRepository(self.database), self.funpay_replies, outbound_allowed=self._outbound_funpay_permitted,
                ),
            )
        )
        self.auto_replies = AutoReplyService(
            self.funpay_replies, AutoReplyRepository(self.database), self.task_states, self.logger,
            default_enabled=settings.funpay_auto_reply_enabled, outbound_allowed=self._outbound_funpay_permitted,
        )
        self.notifications = (
            FunPayMessageNotifier(
                self.funpay, self.telegram, DialogRepository(self.database), TelegramMessageLinkRepository(self.database),
                self.task_states, self.telegram_notification_user_id, self.logger, self.auto_replies,
            )
            if settings.funpay_message_notifications_enabled and self.telegram_notification_user_id is not None
            else None
        )
        self.runner = BackgroundRunner(
            settings, self.database, self.logger, funpay=self.funpay, telegram=self.telegram,
            notifications=self.notifications, session_guard=self.session_guard,
            session_expired_callback=self._notify_funpay_session_expired,
            telegram_enabled=self.telegram_enabled,
        )
        self.process_lock = SingletonProcessLock(settings.data_directory / "funpay-operations.lock")

    def _outbound_funpay_permitted(self, operation: str) -> bool:
        return self.emergency_stop.permits(operation) and self.session_guard.permits(operation)

    def _notify_funpay_session_expired(self) -> None:
        if not self.telegram_enabled or self.telegram_notification_user_id is None:
            return
        try:
            self.telegram.send_private_notification(
                self.telegram_notification_user_id, SESSION_EXPIRED_TEXT, SESSION_EXPIRED_MARKUP
            )
        except (TelegramError, PermissionError):
            self.logger.warning("FunPay session expiry notification could not be delivered")

    @classmethod
    def from_files(cls, config_path: Path, env_path: Path) -> "Application":
        return cls(load_settings(config_path=config_path, env_path=env_path))

    async def run(self, *, once: bool) -> None:
        self.settings.backups_directory.mkdir(parents=True, exist_ok=True)
        self.database.initialize()
        self.logger.info("Application started in %s environment", self.settings.environment)
        try:
            with self.process_lock:
                await self.runner.run(once=once)
        finally:
            await self.runner.shutdown()
            self.funpay.close()
