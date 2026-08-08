"""Safe asyncio background scheduling without GUI or browser automation."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .database import Database
from .funpay import FunPayClient
from .notifications import FunPayMessageNotifier
from .telegram import TelegramLongPollingBot


class BackgroundRunner:
    def __init__(
        self, settings: Settings, database: Database, logger: logging.Logger, *, funpay: FunPayClient | None = None,
        telegram: TelegramLongPollingBot | None = None,
        notifications: FunPayMessageNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger
        # Injection constructs no session and the background loop does not
        # call it until a later explicitly scheduled read task exists.
        self.funpay = funpay
        self.telegram = telegram
        self.notifications = notifications

    async def run(self, *, once: bool) -> None:
        if once:
            await self.run_cycle()
            return
        background_tasks = []
        if self.notifications is not None:
            background_tasks.append(asyncio.create_task(self._run_notifications()))
        if self.settings.telegram_enabled and self.telegram is not None:
            background_tasks.append(asyncio.create_task(self._run_telegram_long_polling()))
        if background_tasks:
            await asyncio.gather(*background_tasks)
            return
        while True:
            await self.run_cycle()
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_cycle(self) -> None:
        """Run enabled, explicit read-only background services."""

        await asyncio.gather(*[
            asyncio.to_thread(service)
            for service in (
                self.notifications.sync if self.notifications is not None else None,
                self.telegram.poll_once if self.settings.telegram_enabled and self.telegram is not None else None,
            )
            if service is not None
        ])

        self.logger.info(
            "Safe background cycle completed; mode=%s real operations disabled=%s telegram_enabled=%s",
            self.settings.operation_mode,
            not self.settings.operations_enabled,
            self.settings.telegram_enabled,
        )

    async def _run_notifications(self) -> None:
        while True:
            await asyncio.to_thread(self.notifications.sync)
            await asyncio.sleep(self.settings.funpay_message_poll_interval_seconds)

    async def _run_telegram_long_polling(self) -> None:
        while not self.telegram.is_stopped:
            await asyncio.to_thread(self.telegram.poll_once)
            await asyncio.sleep(0.2)
