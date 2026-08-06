from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from funpay_operations.config import ConfigurationError, load_settings


class ConfigurationTests(unittest.TestCase):
    def test_loads_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.yaml"
            config.write_text("operations:\n  enabled: false\n  poll_interval_seconds: 5\n", encoding="utf-8")
            settings = load_settings(config_path=config, env_path=root / ".env")

        self.assertFalse(settings.operations_enabled)
        self.assertEqual(settings.poll_interval_seconds, 5)
        self.assertEqual(settings.database_path, Path("data") / "funpay.sqlite3")

    def test_rejects_database_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.yaml"
            config.write_text("storage:\n  database_file: ../outside.sqlite3\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_settings(config_path=config, env_path=root / ".env")

    def test_rejects_string_operation_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.yaml"
            config.write_text("operations:\n  enabled: 'false'\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_settings(config_path=config, env_path=root / ".env")
