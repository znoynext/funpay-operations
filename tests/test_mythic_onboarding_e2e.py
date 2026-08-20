from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from funpay_operations.config import Settings
from funpay_operations.database import Database
from funpay_operations.funpay import FunPayDialog, FunPayLotDetails, FunPayProfile, MockFunPayClient
from funpay_operations.read_only_control import (
    OnboardingMutationTrap,
    OnboardingReadBoundary,
    ProductionReadOnlyControlService,
)
from funpay_operations.repositories import TaskStateRepository
from funpay_operations.session_health import FunPaySessionGuard
from funpay_operations.trusted_sellers import SellerVerificationState, TrustedSellerRepository


class ProductionReadOnlyOnboardingE2ETests(unittest.TestCase):
    def test_mapping_sellers_competitors_floors_and_real_data_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = Database(root / "state.sqlite3")
            database.initialize()
            states = TaskStateRepository(database)
            settings = Settings(
                "test", "INFO", root, database.path, root / "logs", root / "backups",
                "safe", False, 30, 1, 4, "funpay_session", "telegram_bot_token", (1,), "RUB", None,
            )
            own = details("own-private-id", "Mythic+ +10 EU self-play x1", "owner-private", 160_000)
            competitor = details(
                "competitor-private-id", "Mythic+ +10 EU self-play x1", "stable-seller-private", 150_000
            )
            client = MockFunPayClient(
                profile=FunPayProfile("owner-private", "Owner", True),
                own_lot_details=(own,),
                dialogs=(FunPayDialog("dialog-private", "stable-seller-private", "SellerOne", None),),
                seller_lot_details={"stable-seller-private": (competitor,)},
            )
            trap = OnboardingMutationTrap()
            service = ProductionReadOnlyControlService(
                database, OnboardingReadBoundary(client, trap), settings, states,
                FunPaySessionGuard(states, root / "secrets.dpapi"),
                telegram_configured=True, logger=logging.getLogger("onboarding-e2e"),
                health_ttl_seconds=45,
            )

            self.assertTrue(service.refresh_lots())
            own_preview = service.own_mapping_overview()
            self.assertEqual((own_preview.total, own_preview.high, own_preview.confirmed), (1, 1, 0))
            self.assertEqual(service.execute("confirm_own_high"), "1")
            self.assertEqual(service.own_mapping_overview().confirmed, 1)
            self.assertEqual(service.lots("Mythic+")[0].mode, "check_only")

            sellers = service.find_sellers("SellerOne")
            self.assertEqual(sellers.exact, ("SellerOne",))
            service.execute("seller_add_batch")
            competitor_preview = service.competitor_mapping_overview()
            self.assertEqual((competitor_preview.exact, competitor_preview.attention), (1, 0))
            self.assertEqual(service.execute("confirm_competitor_high"), "1")

            service.execute("floor_set_global", "1000")
            # A second enabled seller without an exact mapping must not suppress
            # the consecutive reads required by this variant's sole reference.
            TrustedSellerRepository(database).add_seller(
                "unmapped-seller-private", "SellerTwo",
                verification_state=SellerVerificationState.VERIFIED,
            )
            service.execute("check_prices")
            preview = service.price_preview()
            self.assertEqual(len(preview.changes), 1)
            self.assertEqual(preview.changes[0].target_minor, 148_500)
            readiness = service.readiness()
            self.assertEqual((readiness.dry_run_ready, readiness.dry_run_blocked), (1, 0))
            self.assertFalse(readiness.live_enabled)
            self.assertEqual(trap.attempts, 0)
            self.assertFalse(any("send" in call or "update" in call or "raise" in call for call in client.calls))

            with database.session() as connection:
                safe = connection.execute(
                    "SELECT mutation_attempts,secrets_exposed,dry_run_success FROM read_only_readiness_state "
                    "WHERE singleton_id=1"
                ).fetchone()
            self.assertEqual(tuple(safe), (0, 0, 1))


def details(lot_id: str, title: str, seller_id: str, price_minor: int) -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id, title, price_minor, "RUB", seller_id, "wow-node", True,
        "Safe public terms", title, None, False, {}, {}, (),
    )
