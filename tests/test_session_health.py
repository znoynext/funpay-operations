from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import tempfile
import unittest

from funpay_operations.config import Settings
from funpay_operations.database import Database
from funpay_operations.funpay import FunPaySessionExpired
from funpay_operations.repositories import TaskStateRepository
from funpay_operations.runtime import TaskDisposition
from funpay_operations.session_health import FunPaySessionGuard
from funpay_operations.tasks import BackgroundRunner


class _ExpiredNotifications:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self) -> None:
        self.calls += 1
        raise FunPaySessionExpired("synthetic expired session")


class _HealthyNotifications:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self) -> None:
        self.calls += 1


class FunPaySessionHealthTests(unittest.TestCase):
    def test_expiry_blocks_outbound_actions_until_dpapi_store_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "state.sqlite3")
            database.initialize()
            store = root / "secrets.dpapi"
            store.write_bytes(b"first")
            guard = FunPaySessionGuard(TaskStateRepository(database), store)
            self.assertTrue(guard.allows_polling())
            self.assertTrue(guard.mark_expired())
            self.assertFalse(guard.permits("outbound_reply"))
            self.assertFalse(guard.allows_polling())
            store.write_bytes(b"replacement")
            self.assertTrue(guard.allows_polling())
            self.assertFalse(guard.mark_expired())
            self.assertFalse(guard.allows_polling())
            guard.mark_authorized()
            self.assertTrue(guard.permits("price_writes"))

    def test_expired_poller_notifies_once_then_stays_offline_until_local_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "operations.sqlite3")
            database.initialize()
            store = root / "secrets.dpapi"
            store.write_bytes(b"first")
            guard = FunPaySessionGuard(TaskStateRepository(database), store)
            settings = Settings("test", "INFO", root, database.path, root / "logs", root / "backups", "safe", False, 1, 1, 2, "funpay", "telegram", (), "RUB", None)
            expired = _ExpiredNotifications()
            notifications: list[str] = []
            logger = logging.getLogger("funpay_operations.session-health")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            runner = BackgroundRunner(
                settings, database, logger,
                notifications=expired, session_guard=guard, session_expired_callback=lambda: notifications.append("sent"),
            )
            self.assertEqual(asyncio.run(runner._poll_messages()), TaskDisposition.DISABLED)
            self.assertEqual(asyncio.run(runner._poll_messages()), TaskDisposition.DISABLED)
            self.assertEqual((expired.calls, notifications), (1, ["sent"]))
            store.write_bytes(b"replacement")
            healthy = _HealthyNotifications()
            runner.notifications = healthy
            self.assertIsNone(asyncio.run(runner._poll_messages()))
            self.assertEqual(healthy.calls, 1)
            self.assertFalse(guard.is_expired)
