from __future__ import annotations

import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from funpay_operations.funpay import FunPayError
from funpay_operations.setup_center import SetupCenterController, main as setup_center_main
from funpay_operations.setup_services import FunPaySetupService, TelegramSetupService, redact_setup_text
from funpay_operations.telegram import TelegramBotProfile, TelegramError, TelegramUpdate
from funpay_operations.database import Database
from funpay_operations.repositories import TaskStateRepository
from funpay_operations.windows_infra import resolve_windows_paths


class MemorySecretStore:
    def __init__(self, _path: Path) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeFunPayClient:
    def __init__(self, authorized: bool, username: str = "seller") -> None:
        self.authorized, self.username, self.closed = authorized, username, False

    def check_authorization(self) -> bool:
        return self.authorized

    def get_profile(self) -> object:
        return type("Profile", (), {"username": self.username})()

    def close(self) -> None:
        self.closed = True


class FakeTelegramApi:
    def __init__(self, *, username: str = "local_test_bot", updates: tuple[TelegramUpdate, ...] = ()) -> None:
        self.username, self.updates = username, updates

    def get_me(self) -> TelegramBotProfile:
        return TelegramBotProfile(self.username)

    def get_updates(self, offset: int | None, timeout_seconds: int) -> tuple[TelegramUpdate, ...]:
        del offset, timeout_seconds
        return self.updates


class SetupCenterServiceTests(unittest.TestCase):
    def test_funpay_valid_mock_session_is_saved_only_after_two_read_only_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            calls: list[str] = []

            def factory(value: str) -> FakeFunPayClient:
                calls.append(value)
                return FakeFunPayClient(True)

            service = FunPaySetupService(paths, store_factory=lambda _path: store, client_factory=factory)
            result = service.verify_and_save("key-value", "seal-value")

            self.assertTrue(result.ok)
            self.assertEqual(len(calls), 2)
            self.assertIn("funpay_session", store.values)
            self.assertNotIn("key-value", result.message)

    def test_invalid_funpay_session_is_not_saved_and_secret_is_redacted_from_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            secret = "this-must-never-be-logged"

            def factory(_value: str) -> FakeFunPayClient:
                raise FunPayError(f"invalid {secret}")

            result = FunPaySetupService(paths, store_factory=lambda _path: store, client_factory=factory).verify_and_save(secret, "seal")

            self.assertFalse(result.ok)
            self.assertNotIn("funpay_session", store.values)
            detail = (paths.logs / "setup-center-diagnostics.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, detail)

    def test_session_replacement_overwrites_only_the_dpapi_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            store.set("funpay_session", "old")
            service = FunPaySetupService(paths, store_factory=lambda _path: store, client_factory=lambda _value: FakeFunPayClient(True))

            self.assertTrue(service.verify_and_save("new-key", "new-seal").ok)
            self.assertNotEqual(store.get("funpay_session"), "old")

    def test_telegram_valid_token_then_explicit_owner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            update = TelegramUpdate(1, 12345678, 12345678, "/start", username="owner")
            api = FakeTelegramApi(updates=(update,))
            service = TelegramSetupService(paths, store_factory=lambda _path: store, api_factory=lambda _provider: api)

            connected = service.verify_and_save("bot-token")
            candidate = service.wait_for_owner_start()
            rejected = service.confirm_owner(999)
            accepted = service.confirm_owner(candidate.user_id if candidate else 0)

            self.assertTrue(connected.ok)
            self.assertEqual(connected.username, "local_test_bot")
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate.masked_id, "••••5678")
            self.assertFalse(rejected.ok)
            self.assertTrue(accepted.ok)

    def test_invalid_telegram_token_is_not_saved_or_written_to_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            token = "not-a-real-token"

            class BrokenApi(FakeTelegramApi):
                def get_me(self) -> TelegramBotProfile:
                    raise TelegramError(f"rejected {token}")

            service = TelegramSetupService(
                paths, store_factory=lambda _path: store, api_factory=lambda _provider: BrokenApi(),
            )
            result = service.verify_and_save(token)

            self.assertFalse(result.ok)
            self.assertNotIn("telegram_bot_token", store.values)
            self.assertNotIn(token, (paths.logs / "setup-center-diagnostics.log").read_text(encoding="utf-8"))

    def test_redaction_covers_json_escaped_gui_values(self) -> None:
        secret = 'value-with-"-quote'
        self.assertNotIn(secret, redact_setup_text(f"failed {json.dumps(secret)}", (secret,)))

    def test_setup_center_model_and_smoke_start_without_tk_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"LOCALAPPDATA": directory}, clear=False):
                paths = resolve_windows_paths(Path(directory))
                statuses = SetupCenterController(paths).statuses()
                self.assertIn("⚪ Не настроен", dict((item.label, item.value) for item in statuses).values())
                self.assertEqual(setup_center_main(["--smoke"]), 0)

    def test_gui_runtime_smoke_initializes_tcl_without_opening_a_window(self) -> None:
        calls: list[tuple[str, str]] = []
        interpreter = types.SimpleNamespace(call=lambda *args: calls.append(args) or "8.6")
        tkinter = types.ModuleType("tkinter")
        tkinter.Tcl = lambda: interpreter

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("os.environ", {"LOCALAPPDATA": directory}, clear=False),
                patch.dict("sys.modules", {"tkinter": tkinter}),
            ):
                self.assertEqual(setup_center_main(["--gui-runtime-smoke"]), 0)

        self.assertEqual(calls, [("info", "patchlevel")])

    def test_session_expired_status_is_clear_and_has_no_cookie_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            Database(paths.database).initialize()
            TaskStateRepository(Database(paths.database)).save("funpay_session", "expired", "marker")
            status = {item.label: item.value for item in SetupCenterController(paths).statuses()}
            self.assertEqual(status["FunPay"], "🔴 Требуется повторная авторизация")
            self.assertNotIn("marker", status["FunPay"])

    def test_gui_source_uses_masked_standard_entry_fields_and_no_credential_cli(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "funpay_operations" / "setup_center.py").read_text(encoding="utf-8")
        self.assertIn('show="*"', source)
        self.assertNotIn("golden_key=", source)
        self.assertNotIn("bot-token=", source)

    def test_controller_catalog_minimum_price_and_restart_stay_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            restarted: list[Path] = []
            controller = SetupCenterController(paths, restart=lambda _paths: restarted.append(Path("background.exe")) or Path("background.exe"))
            definition = {
                "version": 1,
                "mythic_plus": {
                    "min_key_level": 10, "max_key_level": 10, "regions": ["eu"], "service_formats": ["selfplay"],
                    "package_sizes": [1], "price_conditions": {}, "enabled": False, "desired_state": "disabled",
                    "template_reference": "not_selected", "description_profile": "safe_neutral", "price_policy_reference": "not_selected",
                },
            }
            self.assertTrue(controller.save_catalog(definition).ok)
            self.assertTrue(controller.save_minimum_price("Mythic+ +10", "1000").ok)
            self.assertTrue(controller.restart_background().ok)
            self.assertEqual(restarted, [Path("background.exe")])
