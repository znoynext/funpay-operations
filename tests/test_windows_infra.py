from __future__ import annotations

import os
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from funpay_operations.__main__ import main
from funpay_operations.windows_infra import (
    TASK_NAME,
    diagnostics,
    first_run,
    install_autostart,
    remove_autostart,
    resolve_background_executable,
    resolve_windows_paths,
    safe_config_path,
    task_scheduler_command,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> None:
        self.calls.append((command, kwargs))


class WindowsInfraTests(unittest.TestCase):
    def test_clean_install_existing_install_and_update_preserve_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = resolve_windows_paths(Path(temporary_directory))
            first = first_run(paths)
            self.assertEqual(first["config"], "created")
            self.assertTrue(paths.database.is_file())
            config = safe_config_path(paths)
            owner_config = config.read_text(encoding="utf-8") + "# owner-local-setting\n"
            config.write_text(owner_config, encoding="utf-8")
            paths.secrets.write_bytes(b"synthetic-local-dpapi-placeholder")

            second = first_run(paths)
            self.assertEqual(second["config"], "existing")
            self.assertEqual(config.read_text(encoding="utf-8"), owner_config)
            self.assertEqual(paths.secrets.read_bytes(), b"synthetic-local-dpapi-placeholder")
            self.assertTrue(paths.database.is_file())

    def test_diagnostics_missing_secrets_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = diagnostics(resolve_windows_paths(Path(temporary_directory)))
        self.assertEqual(result["telegram"], "not_configured")
        self.assertEqual(result["database"], "ok")
        self.assertEqual(result["singleton"], "ok")

    def test_scheduler_install_remove_and_uninstall_preserve_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "funpay-operations.exe"
            executable.touch()
            data = root / "data" / "owner.sqlite3"
            data.parent.mkdir()
            data.write_bytes(b"owner-local-data")
            runner = RecordingRunner()

            install_autostart(executable, runner=runner)
            remove_autostart(runner=runner)

            install_command, remove_command = (call[0] for call in runner.calls)
            self.assertIn(TASK_NAME, install_command)
            self.assertIn("0000:30", install_command)
            self.assertIn(f'"{executable}" --background', install_command)
            self.assertEqual(remove_command[1], "/Delete")
            self.assertEqual(data.read_bytes(), b"owner-local-data")

    def test_background_executable_resolution_requires_noconsole_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cli = root / "funpay-operations-cli.exe"
            background = root / "funpay-operations.exe"
            cli.touch()
            background.touch()
            self.assertEqual(resolve_background_executable(cli), background.resolve())
            background.unlink()
            with self.assertRaises(FileNotFoundError):
                resolve_background_executable(cli)
            with self.assertRaises(RuntimeError):
                resolve_background_executable(root / "python.exe")

    def test_safe_background_start_needs_no_external_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = dict(os.environ)
            environment["LOCALAPPDATA"] = temporary_directory
            with patch.dict(os.environ, environment, clear=True), patch(
                "sys.argv", ["funpay-operations-cli.exe", "--background", "--once"]
            ):
                self.assertEqual(main(), 0)
            logging.shutdown()
            paths = resolve_windows_paths(Path(temporary_directory))
            self.assertTrue(paths.database.is_file())
            self.assertTrue(safe_config_path(paths).is_file())
            self.assertTrue(any(paths.backups.glob("database-*.sqlite3")))

    def test_scheduler_command_rejects_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            task_scheduler_command(Path("funpay-operations.exe"), action="unknown")
        with self.assertRaises(ValueError):
            task_scheduler_command(Path('unsafe"name.exe'), action="install")
