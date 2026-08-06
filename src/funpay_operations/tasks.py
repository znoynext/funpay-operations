"""Safe asyncio background scheduling without GUI or browser automation."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .database import Database
from .funpay import FunPayClient
from .telegram import TelegramLongPollingBot


class BackgroundRunner:
    def __init__(
        self, settings: Settings, database: Database, logger: logging.Logger, *, funpay: FunPayClient | None = None,
        telegram: TelegramLongPollingBot | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        # Injection constructs no session and the background loop does not
        # call it until a later explicitly scheduled read task exists.
        self.funpay = funpay
        self.telegram = telegram

    async def run(self, *, once: bool) -> None:
        while True:
            await self.run_cycle()
            if once:
                return
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_cycle(self) -> None:
        """Run enabled, explicit read-only background services."""

        if self.settings.telegram_enabled and self.telegram is not None:
            await asyncio.to_thread(self.telegram.poll_once)

        self.logger.info(
            "Safe background cycle completed; mode=%s real operations disabled=%s telegram_enabled=%s",
            self.settings.operation_mode,
            not self.settings.operations_enabled,
            self.settings.telegram_enabled,
        )
