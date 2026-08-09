"""Safe composition of local background tasks and disabled adapters."""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .database import Database
from .funpay import FunPayClient
from .notifications import FunPayMessageNotifier
from .runtime import (
    BackgroundSupervisor,
    BackgroundTask,
    DisabledAdapter,
    ExponentialBackoff,
    ProcessPriority,
    RecoveryCoordinator,
    RecoveryStep,
    SleepResumeHandler,
    SQLiteMaintenance,
    TaskDisposition,
    WindowsNetworkRecoveryHandler,
    apply_windows_process_priority,
)
from .telegram import TelegramLongPollingBot


class BackgroundRunner:
    """Runtime composition without requiring FunPay or Telegram credentials in tests."""

    def __init__(
        self, settings: Settings, database: Database, logger: logging.Logger, *, funpay: FunPayClient | None = None,
        telegram: TelegramLongPollingBot | None = None, notifications: FunPayMessageNotifier | None = None,
    ) -> None:
        self.settings, self.database, self.logger = settings, database, logger
        self.funpay, self.telegram, self.notifications = funpay, telegram, notifications
        disabled = DisabledAdapter()
        recovery = RecoveryCoordinator(tuple(
            RecoveryStep(name, disabled.run) for name in RecoveryCoordinator.ORDER
        ), logger)
        self.sleep_resume = SleepResumeHandler(recovery)
        self.network_recovery = WindowsNetworkRecoveryHandler(recovery)
        self.maintenance = SQLiteMaintenance(
            database, settings.backups_directory, retention_count=settings.backup_retention_count,
        )
        self.supervisor = BackgroundSupervisor(
            (
                BackgroundTask("funpay-message-poller", self._poll_messages, settings.funpay_message_poll_interval_seconds),
                BackgroundTask("telegram-polling-service", self._poll_telegram, 0.2),
                BackgroundTask("price-scheduler", disabled.run, settings.poll_interval_seconds),
                BackgroundTask("raise-scheduler", disabled.run, settings.poll_interval_seconds),
                BackgroundTask("recovery-coordinator", disabled.run, settings.poll_interval_seconds),
                BackgroundTask("sqlite-maintenance", self._maintain_storage, settings.backup_interval_seconds),
            ),
            logger=logger,
            backoff=ExponentialBackoff(settings.reconnect_initial_seconds, settings.reconnect_max_seconds),
            recovery=recovery,
        )

    async def run(self, *, once: bool) -> None:
        applied = apply_windows_process_priority(ProcessPriority.BELOW_NORMAL)
        self.logger.info("Background runtime started; windows_below_normal_priority=%s", applied)
        if once:
            await self.run_cycle()
            return
        await self.supervisor.run_forever()

    async def shutdown(self) -> None:
        await self.supervisor.shutdown()

    async def run_cycle(self) -> None:
        results = await self.supervisor.run_once()
        failed = tuple(item.name for item in results if item.disposition is TaskDisposition.FAILED)
        self.logger.info("Safe background cycle completed; failed_tasks=%s", ",".join(failed) or "none")

    async def on_resume(self) -> None:
        await self.sleep_resume.on_resume()

    async def on_windows_network_change(self, online: bool) -> None:
        await self.network_recovery.on_network_change(online)

    async def _poll_messages(self) -> TaskDisposition | None:
        if self.notifications is None:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.notifications.sync)
        return None

    async def _poll_telegram(self) -> TaskDisposition | None:
        if not self.settings.telegram_enabled or self.telegram is None or self.telegram.is_stopped:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.telegram.poll_once)
        return None

    async def _maintain_storage(self) -> None:
        await asyncio.to_thread(self.maintenance.integrity_check)
        await asyncio.to_thread(self.maintenance.backup)
