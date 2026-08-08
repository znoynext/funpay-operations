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
from .setup_wizard import SecretStore
from .telegram import build_telegram_bot
from .tasks import BackgroundRunner


class Application:
    """Composes local services without initiating external operations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = configure_logging(settings.logs_directory, settings.log_level)
        self.database = Database(settings.database_path)
        self.task_states = TaskStateRepository(self.database)
        secret_store = SecretStore(settings.data_directory / "secrets.dpapi")
        self.funpay = build_read_client(
            settings, secret_store
        )
        self.funpay_replies = build_reply_client(settings, secret_store)
        self.telegram = build_telegram_bot(settings, secret_store, self.task_states, self.logger)
        self.telegram.set_interaction_router(
            FunPayReplyRouter(
                settings.allowed_telegram_user_ids, TelegramMessageLinkRepository(self.database),
                ReplyRepository(self.database), self.funpay_replies,
            )
        )
        self.auto_replies = AutoReplyService(
            self.funpay_replies, AutoReplyRepository(self.database), self.task_states, self.logger,
            default_enabled=settings.funpay_auto_reply_enabled,
        )
        self.notifications = (
            FunPayMessageNotifier(
                self.funpay, self.telegram, DialogRepository(self.database), TelegramMessageLinkRepository(self.database),
                self.task_states, settings.telegram_notification_user_id, self.logger, self.auto_replies,
            )
            if settings.funpay_message_notifications_enabled and settings.telegram_notification_user_id is not None
            else None
        )
        self.runner = BackgroundRunner(
            settings, self.database, self.logger, funpay=self.funpay, telegram=self.telegram,
            notifications=self.notifications,
        )

    @classmethod
    def from_files(cls, config_path: Path, env_path: Path) -> "Application":
        return cls(load_settings(config_path=config_path, env_path=env_path))

    async def run(self, *, once: bool) -> None:
        self.settings.backups_directory.mkdir(parents=True, exist_ok=True)
        self.database.initialize()
        self.logger.info("Application started in %s environment", self.settings.environment)
        await self.runner.run(once=once)
