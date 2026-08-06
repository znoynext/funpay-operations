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

    def test_loads_reconnect_and_storage_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.yaml"
            config.write_text(
                """storage:
  database_file: app.sqlite3
  logs_directory: application-logs
  backups_directory: backup-files
operations:
  enabled: false
  mode: dry_run
  reconnect_initial_seconds: 3
  reconnect_max_seconds: 10
""",
                encoding="utf-8",
            )
            settings = load_settings(config_path=config, env_path=root / ".env")

        self.assertEqual(settings.operation_mode, "dry_run")
        self.assertEqual(settings.logs_directory, Path("data") / "application-logs")
        self.assertEqual(settings.backups_directory, Path("data") / "backup-files")
        self.assertEqual(settings.reconnect_max_seconds, 10)

    def test_rejects_reconnect_range_and_unsafe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.yaml"
            config.write_text(
                "operations:\n  enabled: false\n  mode: live\n  reconnect_initial_seconds: 10\n  reconnect_max_seconds: 5\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                load_settings(config_path=config, env_path=root / ".env")
