from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import tempfile
import unittest

from funpay_operations.config import Settings
from funpay_operations.database import Database
from funpay_operations.funpay import (
    FunPayDialog,
    FunPayLotDetails,
    FunPayNetworkUnavailable,
    FunPayProfile,
    FunPaySessionExpired,
    MockFunPayClient,
    RealOperationsDisabled,
)
from funpay_operations.read_only_control import (
    FunPayReadStatus,
    ProductionReadOnlyControlService,
    _competitor_snapshot,
)
from funpay_operations.repositories import TaskStateRepository
from funpay_operations.read_only_probe import (
    ProbeMutationTrap,
    ProbeReadBoundary,
    ReadOnlyFunPayProbe,
    ReadOnlyProbeRepository,
)
from funpay_operations.lot_discovery import OwnLotRegistryRepository
from funpay_operations.session_health import FunPaySessionGuard
from funpay_operations.telegram import TelegramUpdate
from funpay_operations.telegram_control import EmergencyStopGate, TelegramControlRouter
from funpay_operations.trusted_sellers import (
    CompetitorLotMappingRepository,
    SellerVerificationState,
    TrustedSellerRepository,
)


class ExpiredFunPay(MockFunPayClient):
    def get_profile(self) -> FunPayProfile:
        raise FunPaySessionExpired("synthetic expiry")


class OfflineFunPay(MockFunPayClient):
    def get_profile(self) -> FunPayProfile:
        raise FunPayNetworkUnavailable("synthetic offline")


class ProductionReadOnlyControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = Database(root / "state.sqlite3")
        self.database.initialize()
        self.states = TaskStateRepository(self.database)
        self.settings = Settings(
            "test", "INFO", root, self.database.path, root / "logs", root / "backups",
            "safe", False, 30, 1, 4, "funpay_session", "telegram_bot_token", (10,), "RUB", None,
        )
        self.guard = FunPaySessionGuard(self.states, root / "secrets.dpapi")
        self.clock_value = 100.0

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def service(self, client: MockFunPayClient) -> ProductionReadOnlyControlService:
        return ProductionReadOnlyControlService(
            self.database, client, self.settings, self.states, self.guard,
            telegram_configured=True, logger=logging.getLogger("read-only-control-test"),
            health_ttl_seconds=45, clock=lambda: self.clock_value,
        )

    def test_dashboard_uses_real_registry_and_health_cache_without_synthetic_lots(self) -> None:
        client = MockFunPayClient(
            profile=FunPayProfile("owner", "owner", True),
            own_lot_details=(
                _lot("lot-one", "Mythic+ +10 EU self-play x1"),
                _lot("lot-two", "Raid service EU self-play x1"),
                _lot("lot-three", "Unrelated service"),
            ),
        )
        service = self.service(client)
        first = service.dashboard(emergency_active=False)
        second = service.dashboard(emergency_active=False)

        self.assertEqual((first.mythic_lots, first.unknown_lots), (0, 3))
        self.assertFalse(hasattr(first, "delve_lots"))
        self.assertEqual(len(service.lots()), 3)
        self.assertNotIn("Seller A", repr(service.lots()))
        self.assertNotIn("mock-m10", repr(service.lots()))
        self.assertEqual(client.calls.count("get_profile"), 1)
        self.assertEqual(client.calls.count("get_own_lot_details"), 1)
        self.assertEqual(first.statuses[1].value, "🟢 Подключён")
        self.assertEqual(first.statuses[2].value, "🟢 Подключён")
        self.assertEqual(second.last_funpay_read, "только что")

    def test_production_dashboard_uses_sanitized_probe_counts_without_another_network_read(self) -> None:
        client = MockFunPayClient(
            profile=FunPayProfile("private-owner", "private-name", True),
            own_lot_details=(
                _lot("private-one", "Mythic+ +10 EU self-play x1"),
                _lot("private-two", "Unmanaged service"),
            ),
            dialogs=(FunPayDialog("private-dialog", "buyer-id", "Buyer Name", None),),
        )
        repository = ReadOnlyProbeRepository(self.database, cooldown_seconds=0)
        repository.request()
        trap = ProbeMutationTrap()
        ReadOnlyFunPayProbe(
            ProbeReadBoundary(client, trap), OwnLotRegistryRepository(self.database), repository,
            trap=trap, build_sha="b" * 40,
        ).run_pending()
        calls_after_probe = tuple(client.calls)
        service = ProductionReadOnlyControlService(
            self.database, client, self.settings, self.states, self.guard,
            telegram_configured=True, logger=logging.getLogger("read-only-control-probe"),
            probe_repository=repository,
        )

        dashboard = service.dashboard(emergency_active=False)

        self.assertEqual((dashboard.mythic_lots, dashboard.unknown_lots, dashboard.ambiguous_lots), (1, 1, 0))
        self.assertEqual(client.calls, list(calls_after_probe))
        self.assertNotIn("private", repr(dashboard))

    def test_auth_expired_and_network_unavailable_are_distinct_and_cached(self) -> None:
        notifications: list[str] = []
        expired = ProductionReadOnlyControlService(
            self.database, ExpiredFunPay(), self.settings, self.states, self.guard,
            telegram_configured=True, logger=logging.getLogger("read-only-control-expired"),
            health_ttl_seconds=45, clock=lambda: self.clock_value,
            session_expired_callback=lambda: notifications.append("expired"),
        )
        self.assertEqual(expired.health().status, FunPayReadStatus.AUTHORIZATION_REQUIRED)
        self.assertEqual(expired.health().status, FunPayReadStatus.AUTHORIZATION_REQUIRED)
        self.assertTrue(self.guard.is_expired)
        self.assertEqual(notifications, ["expired"])

        other_root = Path(self.temporary_directory.name) / "other"
        other_database = Database(other_root / "state.sqlite3")
        other_database.initialize()
        states = TaskStateRepository(other_database)
        guard = FunPaySessionGuard(states, other_root / "secrets.dpapi")
        offline_client = OfflineFunPay()
        offline = ProductionReadOnlyControlService(
            other_database, offline_client, self.settings, states, guard, telegram_configured=True,
            logger=logging.getLogger("read-only-control-offline"), clock=lambda: self.clock_value,
        )
        self.assertEqual(offline.health().status, FunPayReadStatus.UNAVAILABLE)
        self.assertEqual(offline.health().status, FunPayReadStatus.UNAVAILABLE)
        self.assertFalse(guard.is_expired)

    def test_all_external_mutations_and_auto_reply_fail_closed_at_backend(self) -> None:
        service = self.service(MockFunPayClient())
        for action in (
            "mass_price_update", "update_raise", "rollback", "mass_lot_sync", "disable_lots",
            "auto_reply_toggle", "outbound_reply", "unexpected_action",
        ):
            with self.subTest(action=action), self.assertRaises(RealOperationsDisabled):
                service.execute(action)

    def test_production_router_hides_mutations_and_stale_write_intent_is_blocked(self) -> None:
        service = self.service(MockFunPayClient())
        router = TelegramControlRouter((10,), self.states, service, EmergencyStopGate(self.states))
        home = router.handle(TelegramUpdate(1, 10, 10, "/start"))
        buttons = [button["text"] for row in home.reply_markup["inline_keyboard"] for button in row]
        self.assertNotIn("🔄 Обновить и поднять", buttons)
        locked = router._execute_confirmed(10, "mass_price_update", edit=True)
        self.assertIn("Реальные изменения FunPay пока не разрешены", locked.text)
        automation = router._automation_screen(10, edit=True)
        automation_buttons = [
            button["text"] for row in automation.reply_markup["inline_keyboard"] for button in row
        ]
        self.assertIn("Внешние изменения FunPay отключены", automation.text)
        self.assertNotIn("⏸ Pause", automation_buttons)
        self.assertEqual(self.states.load("funpay_auto_reply"), None)

    def test_seller_lookup_accepts_only_one_exact_dialog_identity(self) -> None:
        client = MockFunPayClient(dialogs=(
            FunPayDialog("dialog-one", "stable-one", "ExactSeller", None),
            FunPayDialog("dialog-two", "stable-two", "OtherSeller", None),
        ))
        service = self.service(client)
        candidate = service.find_seller("ExactSeller")
        self.assertIsNotNone(candidate)
        self.assertIsNone(service.find_seller("Exact"))
        service.execute("seller_add", "ExactSeller")
        sellers = service.sellers()
        self.assertEqual([(item.nickname, item.family, item.verified) for item in sellers], [
            ("ExactSeller", "Mythic+", True),
        ])
        self.assertEqual(client.calls.count("get_seller_lot_details"), 1)

    def test_seller_lookup_rejects_ambiguous_stable_identity(self) -> None:
        service = self.service(MockFunPayClient(dialogs=(
            FunPayDialog("dialog-one", "stable-one", "SameSeller", None),
            FunPayDialog("dialog-two", "stable-two", "SameSeller", None),
        )))
        self.assertIsNone(service.find_seller("SameSeller"))

    def test_actual_and_desired_catalog_state_are_not_conflated(self) -> None:
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO service_catalog
                (stable_code, family, variant_json, enabled, desired_state, template_reference,
                 description_profile, price_policy_reference, price_conditions_json)
                VALUES ('desired-only', 'mythic_plus', '{}', 1, 'enabled', 'none', 'none', 'none', '{}')"""
            )
        service = self.service(MockFunPayClient())
        dashboard = service.dashboard(emergency_active=False)
        self.assertEqual(dashboard.mythic_lots, 0)

    def test_real_read_only_price_path_needs_floor_and_single_seller_history(self) -> None:
        own = _lot("own-lot", "Mythic+ +10 EU self-play x1")
        competitor = FunPayLotDetails(
            "competitor-lot", "Mythic+ +10 EU self-play x1", 150_000, "RUB", "seller-one",
            "node-one", None, "Synthetic description", "Mythic+ +10 EU self-play x1",
            None, None, {}, {}, (),
        )
        client = MockFunPayClient(
            own_lot_details=(own,), seller_lot_details={"seller-one": (competitor,)},
        )
        service = self.service(client)
        service.refresh_lots()
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO service_catalog
                (stable_code, family, variant_json, enabled, desired_state, template_reference,
                 description_profile, price_policy_reference, price_conditions_json)
                VALUES (?, 'mythic_plus', ?, 1, 'enabled', 'none', 'none', 'none', '{}')""",
                ("mplus-k10-eu-selfplay-x1", json.dumps({
                    "key_level": 10, "region": "eu", "service_format": "selfplay", "package_size": 1,
                })),
            )
            connection.execute(
                "INSERT INTO lot_service_mappings(external_lot_id, service_code) VALUES (?, ?)",
                ("own-lot", "mplus-k10-eu-selfplay-x1"),
            )
        TrustedSellerRepository(self.database).add_seller(
            "seller-one", "SellerOne",
            verification_state=SellerVerificationState.VERIFIED,
        )
        CompetitorLotMappingRepository(self.database).confirm_exact(
            _competitor_snapshot(competitor), "mplus-k10-eu-selfplay-x1"
        )

        service.execute("check_prices")
        self.assertIn("минимально допустимую цену", service.price_preview().skipped[0].reason)

        lot_key = service.lots()[0].key
        service.execute("lot_set_floor", f"{lot_key}:1000")
        service.execute("check_prices")
        awaiting = service.price_preview()
        self.assertEqual(awaiting.changes, ())
        service.execute("check_prices")
        accepted = service.price_preview()
        self.assertEqual(len(accepted.changes), 1)
        self.assertEqual(accepted.changes[0].target_minor, 148_500)
        self.assertIn("Только расчёт", service.lots()[0].calculation)

    def test_production_composition_has_no_mock_control_service_reference(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "funpay_operations" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("MockControlService", names)


def _lot(lot_id: str, title: str) -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id, title, 100_000, "RUB", "owner", "node-one", True,
        "Synthetic description", title, None, False, {}, {}, (),
    )


if __name__ == "__main__":
    unittest.main()
