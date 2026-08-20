from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from funpay_operations.config import Settings
from funpay_operations.database import Database
from funpay_operations.tasks import BackgroundRunner


class BackgroundRunnerTests(unittest.TestCase):
    def test_once_mode_returns_without_external_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "operations.sqlite3")
            database.initialize()
            settings = Settings(
                "test", "INFO", root, database.path, root / "logs", root / "backups",
                "safe", False, 1, 1, 2, "funpay", "telegram", (), "RUB", None,
            )
            logger = logging.getLogger("funpay_operations.tests")
            asyncio.run(BackgroundRunner(settings, database, logger).run(once=True))

    def test_read_only_session_and_lot_refresh_run_at_startup_without_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "operations.sqlite3")
            database.initialize()
            settings = Settings(
                "test", "INFO", root, database.path, root / "logs", root / "backups",
                "safe", False, 1, 1, 2, "funpay", "telegram", (), "RUB", None,
            )
            calls: list[str] = []
            runner = BackgroundRunner(
                settings, database, logging.getLogger("funpay_operations.read-only-startup"),
                session_validation=lambda: calls.append("validate"),
                read_model_refresh=lambda: calls.append("refresh"),
            )
            asyncio.run(runner.run(once=True))
            self.assertEqual(calls, ["validate", "refresh"])

    def test_requested_read_only_probe_runs_inside_background_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = Database(root / "operations.sqlite3")
            database.initialize()
            settings = Settings(
                "test", "INFO", root, database.path, root / "logs", root / "backups",
                "safe", False, 1, 1, 2, "funpay", "telegram", (), "RUB", None,
            )
            calls: list[str] = []
            runner = BackgroundRunner(
                settings, database, logging.getLogger("funpay_operations.probe-background"),
                read_only_probe=lambda: calls.append("probe"),
            )
            asyncio.run(runner.run(once=True))
            self.assertEqual(calls, ["probe"])
