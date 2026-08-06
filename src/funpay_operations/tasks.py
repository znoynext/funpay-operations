"""Safe asyncio background scheduling without GUI or browser automation."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .database import Database
from .funpay import FunPayClient
from .telegram import TelegramClient


class BackgroundRunner:
    def __init__(self, settings: Settings, database: Database, logger: logging.Logger) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        self.funpay = FunPayClient(settings.funpay_credential_key, settings.operations_enabled)
        self.telegram = TelegramClient(
            settings.telegram_token_key,
            settings.allowed_telegram_user_ids,
            settings.operations_enabled,
        )

    async def run(self, *, once: bool) -> None:
        while True:
            await self.run_cycle()
            if once:
                return
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_cycle(self) -> None:
        """Reserve the boundary; this implementation performs no network operation."""

        self.logger.info(
            "Safe background cycle completed; mode=%s real operations disabled=%s",
            self.settings.operation_mode,
            not self.settings.operations_enabled,
        )
