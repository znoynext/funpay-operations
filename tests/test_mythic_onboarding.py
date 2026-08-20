from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayLotDetails, MockFunPayClient
from funpay_operations.lot_discovery import OwnLotRegistryRepository, RegisteredLot, classify_wow_lot
from funpay_operations.mythic_onboarding import (
    BudgetDecision,
    MappingConfidence,
    MinimumPriceRepository,
    MythicVariant,
    OwnLotMappingRepository,
    OwnMappingStatus,
    PreLiveEligibilityGuard,
    ReadOnlyRequestBudgetRepository,
    parse_manual_variant,
    parse_minimum_price_batch,
    parse_mythic_lot,
    parse_nickname_batch,
)
from funpay_operations.pricing import OwnLotPricingMode
from funpay_operations.read_only_control import (
    OnboardingMutationBlocked,
    OnboardingMutationTrap,
    OnboardingReadBoundary,
)


class MythicParserTests(unittest.TestCase):
    def test_high_confidence_requires_all_explicit_critical_fields(self) -> None:
        parsed = parse_mythic_lot(lot("one", "Mythic+ +10 EU self-play x1"))
        self.assertEqual(parsed.confidence, MappingConfidence.HIGH)
        self.assertEqual(parsed.variant, MythicVariant(10, "eu", "selfplay", 1, {}))
        self.assertTrue(parsed.bulk_confirmable)
        self.assertEqual(parsed.missing_fields, ())

    def test_structured_fields_and_selected_option_labels_are_evidence(self) -> None:
        parsed = parse_mythic_lot(lot(
            "structured", "Mythic+ service",
            editor_fields={"fields[key_level]": "10", "fields[region]": "eu", "fields[mode]": "sp", "fields[runs]": "1"},
            editor_options={
                "fields[key_level]": (("+10 key level", "10"),),
                "fields[region]": (("Europe EU", "eu"),),
                "fields[mode]": (("Self-play", "sp"),),
                "fields[runs]": (("x1 run", "1"),),
            },
        ))
        self.assertEqual(parsed.confidence, MappingConfidence.HIGH)
        self.assertTrue(any("structured" in item for item in parsed.evidence))

    def test_unknown_conflict_and_missing_fields_are_never_high(self) -> None:
        missing = parse_mythic_lot(lot("missing", "Mythic+ +10 EU"))
        conflict = parse_mythic_lot(lot("conflict", "Mythic+ +10 +11 EU self-play x1"))
        unrelated = parse_mythic_lot(lot("other", "Raid assistance"))
        self.assertIn("execution mode", missing.missing_fields)
        self.assertIn("conflicting key level", conflict.ambiguity_reasons)
        self.assertEqual(unrelated.status, OwnMappingStatus.EXCLUDED)
        self.assertFalse(any(item.bulk_confirmable for item in (missing, conflict, unrelated)))

    def test_manual_correction_is_strict_and_uses_no_float_money(self) -> None:
        self.assertEqual(parse_manual_variant("+12 EU pilot x3").service_code, "mplus_k12_eu_pilot_x3")
        with self.assertRaises(ValueError):
            parse_manual_variant("+12 EU x3")

    def test_cosmetic_text_keeps_source_fingerprint_but_material_change_does_not(self) -> None:
        first = parse_mythic_lot(lot("same", "Mythic+ +10 EU self-play x1 — fast"))
        cosmetic = parse_mythic_lot(lot("same", "Mythic+ +10 EU self-play x1 — friendly"))
        material = parse_mythic_lot(lot("same", "Mythic+ +11 EU self-play x1 — friendly"))
        self.assertEqual(first.source_fingerprint, cosmetic.source_fingerprint)
        self.assertNotEqual(first.source_fingerprint, material.source_fingerprint)


class OwnMappingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "state.sqlite3")
        self.database.initialize()
        self.registry = OwnLotRegistryRepository(self.database)
        self.repository = OwnLotMappingRepository(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store(self, *details: FunPayLotDetails) -> None:
        self.registry.replace(tuple(RegisteredLot(item, classify_wow_lot(item)) for item in details))

    def test_high_batch_persists_confirmed_mapping_and_check_only_default(self) -> None:
        self.store(lot("one", "Mythic+ +10 EU self-play x1"), lot("two", "Mythic+ +11 EU self-play x1"))
        summary = self.repository.analyze(tuple(item.details for item in self.registry.list()))
        self.assertEqual((summary.high, summary.attention), (2, 0))
        self.assertEqual(self.repository.confirm_high_batch(), 2)
        confirmed = self.repository.summary()
        self.assertEqual(confirmed.confirmed, 2)
        with self.database.session() as connection:
            modes = connection.execute("SELECT pricing_mode FROM lot_control_settings ORDER BY external_lot_id").fetchall()
            mappings = connection.execute("SELECT COUNT(*) FROM lot_service_mappings").fetchone()[0]
        self.assertEqual([row["pricing_mode"] for row in modes], ["check_only", "check_only"])
        self.assertEqual(mappings, 2)

    def test_duplicate_canonical_variants_are_excluded_from_bulk_confirmation(self) -> None:
        self.store(lot("one", "Mythic+ +10 EU self-play x1"), lot("two", "Mythic+ +10 EU self-play x1"))
        summary = self.repository.analyze(tuple(item.details for item in self.registry.list()))
        self.assertEqual(summary.high, 0)
        self.assertTrue(all("duplicate canonical variant" in item.ambiguity_reasons for item in summary.reviews))
        self.assertEqual(self.repository.confirm_high_batch(), 0)

    def test_manual_correction_confirms_only_selected_problem(self) -> None:
        self.store(lot("one", "Mythic+ +10 EU self-play"))
        summary = self.repository.analyze(tuple(item.details for item in self.registry.list()))
        self.assertEqual(summary.high, 0)
        corrected = self.repository.confirm_manual(summary.reviews[0].opaque_key, parse_manual_variant("+10 EU self-play x1"))
        self.assertEqual(corrected.status, OwnMappingStatus.CONFIRMED)
        self.assertEqual(corrected.source, "manual")

    def test_material_change_invalidates_but_cosmetic_change_preserves_confirmation(self) -> None:
        original = lot("one", "Mythic+ +10 EU self-play x1 — fast")
        self.store(original)
        self.repository.analyze((original,))
        self.repository.confirm_high_batch()

        cosmetic = lot("one", "Mythic+ +10 EU self-play x1 — friendly")
        self.store(cosmetic)
        self.assertEqual(self.repository.analyze((cosmetic,)).confirmed, 1)

        changed = lot("one", "Mythic+ +11 EU self-play x1 — friendly")
        self.store(changed)
        result = self.repository.analyze((changed,))
        self.assertEqual(result.reviews[0].status, OwnMappingStatus.RECHECK_REQUIRED)
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lot_service_mappings").fetchone()[0], 0)


class LocalSafetyPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "state.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_floor_precedence_and_decimal_batch_parser(self) -> None:
        prices = MinimumPriceRepository(self.database)
        variant = MythicVariant(10, "eu", "selfplay", 1, {})
        prices.set_global(50_000)
        self.assertEqual(prices.resolve(variant), 50_000)
        prices.set_key(10, 60_000)
        self.assertEqual(prices.resolve(variant), 60_000)
        prices.set_variant(variant.service_code, 70_000)
        self.assertEqual(prices.resolve(variant), 70_000)
        self.assertEqual(parse_minimum_price_batch("+2 500\n+3 550,50"), {2: 50_000, 3: 55_050})
        for invalid in ("+2 0", "+2 -1", "+2 500\n+2 600", "+2 nan"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_minimum_price_batch(invalid)

    def test_seller_batch_parser_is_exact_bounded_and_deduplicated(self) -> None:
        self.assertEqual(parse_nickname_batch("SellerOne\nSellerTwo"), ("SellerOne", "SellerTwo"))
        with self.assertRaises(ValueError):
            parse_nickname_batch("SellerOne\nsellerone")
        with self.assertRaises(ValueError):
            parse_nickname_batch("\n")

    def test_request_budget_cooldown_and_circuit_breaker(self) -> None:
        budget = ReadOnlyRequestBudgetRepository(self.database)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(budget.claim("seller_lookup", cooldown_seconds=30, now=now), BudgetDecision.ALLOWED)
        self.assertEqual(
            budget.claim("seller_lookup", cooldown_seconds=30, now=now + timedelta(seconds=1)),
            BudgetDecision.COOLDOWN,
        )
        budget.fail("seller_lookup", severe=True, now=now)
        self.assertEqual(
            budget.claim("seller_lookup", cooldown_seconds=0, now=now + timedelta(seconds=1)),
            BudgetDecision.CIRCUIT_OPEN,
        )

    def test_pre_live_guard_can_report_readiness_but_never_enable_writes(self) -> None:
        result = PreLiveEligibilityGuard().evaluate(
            family="mythic_plus", own_mapping_confirmed=True, own_fingerprint_current=True,
            mode=OwnLotPricingMode.CHECK_ONLY, minimum_exists=True, valid_reference_exists=True,
            competitor_mappings_current=True, suspicious=False, session_authorized=True,
            emergency_stop=False, future_live_capability_enabled=True,
        )
        self.assertTrue(result.eligible_for_future_test)
        self.assertFalse(result.live_write_enabled)
        self.assertIn("live price capability is disabled", result.blockers)

    def test_onboarding_boundary_has_no_mutation_interface_and_traps_resolution(self) -> None:
        trap = OnboardingMutationTrap()
        boundary = OnboardingReadBoundary(MockFunPayClient(), trap)
        self.assertTrue(boundary.get_profile().authorized)
        with self.assertRaises(OnboardingMutationBlocked):
            getattr(boundary, "update_price")
        self.assertEqual(trap.attempts, 1)


def lot(
    lot_id: str,
    title: str,
    *,
    editor_fields: dict[str, str] | None = None,
    editor_options: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id, title, 100_000, "RUB", "owner", "wow-node", True,
        "Safe public description", title, None, False,
        editor_fields or {}, editor_options or {}, (),
    )
