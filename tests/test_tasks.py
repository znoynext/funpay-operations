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
