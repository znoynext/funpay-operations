from __future__ import annotations

import os
import logging
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from funpay_operations.__main__ import main
from funpay_operations.windows_infra import (
    TASK_NAME,
    diagnostics,
    first_run,
    configure_minimum_price,
    configure_service_catalog,
    diagnostics_summary,
    install_application,
    install_autostart,
    remove_autostart,
    resolve_background_executable,
    resolve_windows_paths,
    run_setup_wizard,
    safe_config_path,
    task_scheduler_command,
    task_scheduler_xml,
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
            self.assertIn("/XML", install_command)
            self.assertEqual(remove_command[1], "/Delete")
            self.assertEqual(data.read_bytes(), b"owner-local-data")

    def test_scheduler_xml_preserves_paths_with_spaces_and_uses_limited_current_user_settings(self) -> None:
        executable = Path(r"C:\Users\Owner\App Data\FunPay Operations\app\funpay-operations.exe")
        document = task_scheduler_xml(executable)
        self.assertIn("<Command>C:\\Users\\Owner\\App Data\\FunPay Operations\\app\\funpay-operations.exe</Command>", document)
        self.assertIn("<Arguments>--background</Arguments>", document)
        self.assertIn("<Delay>PT30S</Delay>", document)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", document)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", document)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", document)

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
            task_scheduler_xml(Path('unsafe"name.exe'))

    def test_installer_copies_only_generic_binaries_and_preserves_owner_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            background = source / "funpay-operations.exe"
            cli = source / "funpay-operations-cli.exe"
            background.write_bytes(b"background-v1")
            cli.write_bytes(b"cli-v1")
            paths = resolve_windows_paths(root / "local")
            first_run(paths)
            paths.secrets.write_bytes(b"owner-secret-placeholder")
            installed_background, installed_cli = install_application(
                paths, source_background=background, source_cli=cli
            )
            self.assertEqual(installed_background.read_bytes(), b"background-v1")
            self.assertEqual(installed_cli.read_bytes(), b"cli-v1")
            background.write_bytes(b"background-v2")
            install_application(paths, source_background=background, source_cli=cli)
            self.assertEqual(installed_background.read_bytes(), b"background-v2")
            self.assertEqual(paths.secrets.read_bytes(), b"owner-secret-placeholder")

    def test_seven_step_wizard_and_human_diagnostics_hide_technical_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = resolve_windows_paths(Path(temporary_directory))
            output = io.StringIO()
            self.assertEqual(run_setup_wizard(paths, output), 0)
            text = output.getvalue()
            self.assertIn("Шаг 1/7 — Система", text)
            self.assertIn("Шаг 2/7 — FunPay", text)
            self.assertIn("[ Настроить позже ]", text)
            self.assertIn("Шаг 7/7 — Готово", text)
            self.assertNotIn("Traceback", text)
            self.assertIn("⚪ FunPay — не настроен", "\n".join(diagnostics_summary(diagnostics(paths))))

    def test_wizard_catalog_preview_and_minimum_price_are_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = resolve_windows_paths(Path(temporary_directory))
            answers = iter(("m", "10", "10", "selfplay", "1,3", "y", "Mythic+ +10", "1000", "y"))
            output = io.StringIO()
            self.assertEqual(run_setup_wizard(paths, output, input_fn=lambda _prompt: next(answers)), 0)
            self.assertIn("Предпросмотр: будет сохранено услуг: 2", output.getvalue())
            self.assertEqual(diagnostics(paths)["catalog"], "ok")
            self.assertEqual(diagnostics(paths)["hard_floors"], "ok")
            self.assertTrue(paths.database.is_file())

    def test_wizard_failure_hides_traceback_and_records_local_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = resolve_windows_paths(Path(temporary_directory))
            output = io.StringIO()
            with patch("funpay_operations.windows_infra.first_run", side_effect=OSError("synthetic failure")):
                with self.assertRaisesRegex(Exception, "setup wizard failed"):
                    run_setup_wizard(paths, output)
            self.assertNotIn("synthetic failure", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            self.assertIn("synthetic failure", (paths.logs / "setup-diagnostics.log").read_text(encoding="utf-8"))

    def test_catalog_and_minimum_price_can_be_saved_without_yaml_or_external_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            paths = resolve_windows_paths(Path(temporary_directory))
            first_run(paths)
            definition = {
                "version": 1,
                "mythic_plus": {
                    "min_key_level": 10, "max_key_level": 10, "regions": ["eu"],
                    "service_formats": ["selfplay"], "package_sizes": [1], "price_conditions": {},
                    "enabled": False, "desired_state": "disabled", "template_reference": "not_selected",
                    "description_profile": "safe_neutral", "price_policy_reference": "not_selected",
                },
            }
            self.assertEqual(configure_service_catalog(paths, definition), 1)
            configure_minimum_price(paths, "Mythic+ +10", 1000)
            report = diagnostics(paths)
            self.assertEqual((report["catalog"], report["hard_floors"]), ("ok", "ok"))
