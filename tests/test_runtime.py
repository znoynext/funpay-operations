from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.runtime import (
    BackgroundSupervisor,
    BackgroundTask,
    DisabledAdapter,
    DuplicateProcessError,
    ExponentialBackoff,
    RecoveryCoordinator,
    RecoveryStep,
    SingletonProcessLock,
    SleepResumeHandler,
    SQLiteMaintenance,
    TaskDisposition,
    WindowsNetworkRecoveryHandler,
)


class RuntimeFaultInjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("funpay_operations.runtime-tests")

    async def test_disabled_adapter_is_healthy_not_a_crash(self) -> None:
        supervisor = _supervisor(BackgroundTask("disabled", DisabledAdapter().run, 1), logger=self.logger)
        result = (await supervisor.run_once())[0]
        self.assertEqual(result.disposition, TaskDisposition.DISABLED)

    async def test_network_offline_and_repeated_timeouts_back_off_and_reconnect(self) -> None:
        reconnects: list[str] = []

        def timeout() -> None:
            raise TimeoutError("network offline")

        async def reconnect() -> None:
            reconnects.append("reconnect")

        supervisor = _supervisor(
            BackgroundTask("funpay-message-poller", timeout, 1, reconnect), logger=self.logger,
        )
        first = (await supervisor.run_once())[0]
        second = (await supervisor.run_once())[0]
        self.assertEqual((first.disposition, first.retry_delay_seconds), (TaskDisposition.FAILED, 1))
        self.assertEqual((second.disposition, second.retry_delay_seconds), (TaskDisposition.FAILED, 2))
        self.assertEqual(reconnects, ["reconnect", "reconnect"])

    async def test_service_exception_and_database_error_are_isolated(self) -> None:
        def service_failure() -> None:
            raise RuntimeError("service crash")

        def database_failure() -> None:
            raise sqlite3.OperationalError("database is locked")

        supervisor = _supervisor(
            BackgroundTask("telegram-polling-service", service_failure, 1),
            BackgroundTask("sqlite-maintenance", database_failure, 1),
            BackgroundTask("price-scheduler", lambda: None, 1), logger=self.logger,
        )
        results = {item.name: item.disposition for item in await supervisor.run_once()}
        self.assertEqual(results["telegram-polling-service"], TaskDisposition.FAILED)
        self.assertEqual(results["sqlite-maintenance"], TaskDisposition.FAILED)
        self.assertEqual(results["price-scheduler"], TaskDisposition.SUCCEEDED)

    async def test_sleep_resume_and_windows_network_recovery_use_fixed_order(self) -> None:
        calls: list[str] = []

        async def step(name: str) -> TaskDisposition:
            calls.append(name)
            return TaskDisposition.DISABLED

        recovery = RecoveryCoordinator(tuple(
            RecoveryStep(name, lambda name=name: step(name)) for name in RecoveryCoordinator.ORDER
        ), self.logger)
        sleep_resume = SleepResumeHandler(recovery)
        network = WindowsNetworkRecoveryHandler(recovery)
        sleep_resume.on_sleep()
        self.assertTrue(sleep_resume.sleeping)
        result = await sleep_resume.on_resume()
        self.assertFalse(sleep_resume.sleeping)
        self.assertEqual(tuple(item.disposition for item in result.steps), (TaskDisposition.DISABLED,) * 6)
        self.assertEqual(calls, list(RecoveryCoordinator.ORDER))
        self.assertIsNone(await network.on_network_change(False))
        await network.on_network_change(True)
        self.assertEqual(calls, list(RecoveryCoordinator.ORDER) * 2)

    async def test_task_crash_does_not_stop_other_tasks_and_shutdown_is_graceful(self) -> None:
        ran = asyncio.Event()

        def crash() -> None:
            raise RuntimeError("crash")

        async def healthy() -> None:
            ran.set()

        supervisor = _supervisor(
            BackgroundTask("raise-scheduler", crash, 0.01),
            BackgroundTask("price-scheduler", healthy, 0.01), logger=self.logger,
        )
        run_forever = asyncio.create_task(supervisor.run_forever())
        await asyncio.wait_for(ran.wait(), timeout=1)
        await supervisor.shutdown()
        await asyncio.wait_for(run_forever, timeout=1)


class RuntimeStorageTests(unittest.TestCase):
    def test_singleton_process_lock_rejects_duplicate_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "app.lock"
            first, second = SingletonProcessLock(path), SingletonProcessLock(path)
            first.acquire()
            with self.assertRaises(DuplicateProcessError):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_integrity_check_and_backup_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "operations.sqlite3")
            database.initialize()
            maintenance = SQLiteMaintenance(database, root / "backups", retention_count=2)
            maintenance.integrity_check()
            first = maintenance.backup(datetime(2026, 8, 9, 10, tzinfo=UTC))
            maintenance.backup(datetime(2026, 8, 9, 10, 1, tzinfo=UTC))
            maintenance.backup(datetime(2026, 8, 9, 10, 2, tzinfo=UTC))
            self.assertFalse(first.exists())
            backups = tuple((root / "backups").glob("database-*.sqlite3"))
            self.assertEqual(len(backups), 2)
            connection = sqlite3.connect(backups[0])
            try:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                connection.close()


def _supervisor(*tasks: BackgroundTask, logger: logging.Logger) -> BackgroundSupervisor:
    return BackgroundSupervisor(
        tuple(tasks), logger=logger, backoff=ExponentialBackoff(1, 4),
    )
