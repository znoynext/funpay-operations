"""Safe composition of local background tasks and disabled adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .config import Settings
from .database import Database
from .funpay import FunPayClient
from .funpay import FunPaySessionExpired
from .notifications import FunPayMessageNotifier
from .repositories import TaskStateRepository
from .session_health import FunPaySessionGuard
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
        session_guard: FunPaySessionGuard | None = None, session_expired_callback: Callable[[], None] | None = None,
        telegram_enabled: bool | None = None,
        session_validation: Callable[[], object] | None = None,
        read_model_refresh: Callable[[], object] | None = None,
        read_only_probe: Callable[[], object] | None = None,
    ) -> None:
        self.settings, self.database, self.logger = settings, database, logger
        self.funpay, self.telegram, self.notifications = funpay, telegram, notifications
        self.session_guard, self.session_expired_callback = session_guard, session_expired_callback
        self.telegram_enabled = settings.telegram_enabled if telegram_enabled is None else telegram_enabled
        self.session_validation, self.read_model_refresh = session_validation, read_model_refresh
        self.read_only_probe = read_only_probe
        self.runtime_states = TaskStateRepository(database)
        self._shutdown_requested = asyncio.Event()
        disabled = DisabledAdapter()
        recovery_actions = {
            "validate-external-sessions": self._validate_external_sessions,
            "catch-up-messages": self._poll_messages,
            "refresh-market": self._refresh_read_model,
        }
        recovery = RecoveryCoordinator(tuple(
            RecoveryStep(name, recovery_actions.get(name, disabled.run)) for name in RecoveryCoordinator.ORDER
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
                BackgroundTask("runtime-control", self._check_shutdown_request, 0.5),
                BackgroundTask("read-only-funpay-probe", self._run_read_only_probe, 0.5),
            ),
            logger=logger,
            backoff=ExponentialBackoff(settings.reconnect_initial_seconds, settings.reconnect_max_seconds),
            recovery=recovery,
        )

    async def run(self, *, once: bool) -> None:
        applied = apply_windows_process_priority(ProcessPriority.BELOW_NORMAL)
        self.logger.info("Background runtime started; windows_below_normal_priority=%s", applied)
        self.runtime_states.save("background_runtime", "running")
        self.runtime_states.save("background_control", "running")
        try:
            await self._validate_external_sessions()
            await self._refresh_read_model()
            if once:
                await self.run_cycle()
                return
            supervisor_task = asyncio.create_task(self.supervisor.run_forever())
            shutdown_wait = asyncio.create_task(self._shutdown_requested.wait())
            done, _ = await asyncio.wait((supervisor_task, shutdown_wait), return_when=asyncio.FIRST_COMPLETED)
            if shutdown_wait in done:
                await self.supervisor.shutdown()
            await supervisor_task
            shutdown_wait.cancel()
        finally:
            self.runtime_states.save("background_runtime", "stopped")

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
        if self.session_guard is not None and not self.session_guard.allows_polling():
            return TaskDisposition.DISABLED
        try:
            await asyncio.to_thread(self.notifications.sync)
        except FunPaySessionExpired:
            first_expiry = self.session_guard.mark_expired() if self.session_guard is not None else False
            if first_expiry and self.session_expired_callback is not None:
                try:
                    self.session_expired_callback()
                except Exception:
                    self.logger.warning("FunPay session expiry notification could not be prepared")
            self.logger.warning("FunPay session expired; outbound FunPay actions are paused")
            return TaskDisposition.DISABLED
        if self.session_guard is not None:
            self.session_guard.mark_authorized()
        return None

    async def _poll_telegram(self) -> TaskDisposition | None:
        if not self.telegram_enabled or self.telegram is None or self.telegram.is_stopped:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.telegram.poll_once)
        return None

    async def _validate_external_sessions(self) -> TaskDisposition | None:
        if self.session_validation is None:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.session_validation)
        return None

    async def _refresh_read_model(self) -> TaskDisposition | None:
        if self.read_model_refresh is None:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.read_model_refresh)
        return None

    async def _maintain_storage(self) -> None:
        await asyncio.to_thread(self.maintenance.integrity_check)
        await asyncio.to_thread(self.maintenance.backup)

    async def _run_read_only_probe(self) -> TaskDisposition | None:
        if self.read_only_probe is None:
            return TaskDisposition.DISABLED
        await asyncio.to_thread(self.read_only_probe)
        return None

    async def _check_shutdown_request(self) -> None:
        current = self.runtime_states.load("background_control")
        if current is not None and current[0] == "shutdown_requested":
            self._shutdown_requested.set()
