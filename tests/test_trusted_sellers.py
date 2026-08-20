from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.service_catalog import CatalogFamily, CatalogService, DesiredState
from funpay_operations.trusted_sellers import (
    CompetitorLotMappingRepository,
    CompetitorLotSnapshot,
    ManualSellerConfirmationAPI,
    MappingState,
    MatchResult,
    SellerFamily,
    SellerLastCheckedState,
    SellerMatchingEngine,
    SellerVerificationState,
    ServiceMatchSpec,
    TrustedSellerRepository,
)


class SellerMatchingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SellerMatchingEngine()

    def test_exact_mythic_plus_requires_complete_structured_signature(self) -> None:
        mythic = _mplus_spec()
        assessment = self.engine.match(_mplus_snapshot(), (mythic,))
        self.assertEqual(assessment.result, MatchResult.EXACT)
        self.assertEqual(assessment.service_code, "mplus_k10_eu_selfplay_x1")

    def test_ambiguous_never_selects_a_service_code(self) -> None:
        duplicate_signature = ServiceMatchSpec(**(_mplus_spec().__dict__ | {"service_code": "another_code"}))
        assessment = self.engine.match(_mplus_snapshot(), (_mplus_spec(), duplicate_signature))
        self.assertEqual(assessment.result, MatchResult.AMBIGUOUS)
        self.assertIsNone(assessment.service_code)

    def test_incompatible_and_insufficient_data_are_distinct(self) -> None:
        wrong_region = _mplus_snapshot(region="us")
        self.assertEqual(self.engine.match(wrong_region, (_mplus_spec(),)).result, MatchResult.INCOMPATIBLE)
        missing_level = _mplus_snapshot(key_level=None)
        self.assertEqual(self.engine.match(missing_level, (_mplus_spec(),)).result, MatchResult.INSUFFICIENT_DATA)

    def test_substantial_conditions_must_match_exactly(self) -> None:
        changed_conditions = _mplus_snapshot(conditions={"timed": "no"})
        self.assertEqual(self.engine.match(changed_conditions, (_mplus_spec(),)).result, MatchResult.INCOMPATIBLE)

    def test_catalog_specs_keep_category_outside_mutable_text(self) -> None:
        catalog = CatalogService(
            stable_code="mplus_k10_eu_selfplay_x1", family=CatalogFamily.MYTHIC_PLUS,
            variant={"key_level": 10, "region": "eu", "service_format": "selfplay", "package_size": 1},
            enabled=True, desired_state=DesiredState.ENABLED, template_reference="template",
            description_profile="profile", price_policy_reference="price", price_conditions={"timed": "yes"},
        )
        spec = ServiceMatchSpec.from_catalog(catalog, category="wow-mplus")
        self.assertEqual(self.engine.match(_mplus_snapshot(), (spec,)).result, MatchResult.EXACT)


class TrustedSellerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "trusted.sqlite3")
        self.database.initialize()
        self.sellers = TrustedSellerRepository(self.database)
        self.mappings = CompetitorLotMappingRepository(self.database)
        self.api = ManualSellerConfirmationAPI(self.sellers, self.mappings)
        self._store_service("mplus_k10_eu_selfplay_x1")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_add_mock_disable_and_remove_seller_are_local_and_cascade_mappings(self) -> None:
        seller = self.api.add_mock_seller(
            "mock-seller", "Mock Seller",
            verification_state=SellerVerificationState.VERIFIED,
        )
        self.assertTrue(seller.enabled)
        self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(),))
        self.assertTrue(self.api.disable_seller("mock-seller"))
        self.assertFalse(self.sellers.get("mock-seller").enabled)  # type: ignore[union-attr]
        self.assertTrue(self.api.remove_seller("mock-seller"))
        self.assertIsNone(self.mappings.get("mock-seller", "mock-lot"))

    def test_manual_confirmation_accepts_only_exact_enabled_verified_seller(self) -> None:
        self.api.add_mock_seller("mock-seller", "Mock Seller")
        with self.assertRaisesRegex(ValueError, "enabled and verified"):
            self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(),))

        self.sellers.add_mock_seller(
            "mock-seller", "Mock Seller",
            verification_state=SellerVerificationState.VERIFIED,
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(), _mplus_spec("duplicate")))
        mapping = self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(),))
        self.assertEqual(mapping.service_code, "mplus_k10_eu_selfplay_x1")
        self.assertEqual(mapping.state, MappingState.CONFIRMED)
        self.assertEqual(self.sellers.get("mock-seller").last_checked_state, SellerLastCheckedState.CURRENT)  # type: ignore[union-attr]

    def test_remap_requires_a_new_exact_match(self) -> None:
        self._verified_seller()
        first = self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(),))
        replacement = _mplus_spec("mplus_k10_eu_selfplay_x1_new")
        self._store_service(replacement.service_code)
        remapped = self.api.remap_lot(_mplus_snapshot(), (replacement,))
        self.assertNotEqual(first.service_code, remapped.service_code)
        self.assertEqual(remapped.service_code, replacement.service_code)

    def test_title_form_or_options_change_requires_revalidation(self) -> None:
        self._verified_seller()
        self.api.confirm_match(_mplus_snapshot(), (_mplus_spec(),))
        self.assertFalse(self.api.observe_lot(_mplus_snapshot()))
        self.assertTrue(self.api.observe_lot(_mplus_snapshot(title="Changed mock title")))
        mapping = self.mappings.get("mock-seller", "mock-lot")
        self.assertEqual(mapping.state, MappingState.REVALIDATION_REQUIRED)  # type: ignore[union-attr]
        self.assertEqual(self.sellers.get("mock-seller").last_checked_state, SellerLastCheckedState.CHANGED)  # type: ignore[union-attr]

        self.api.remap_lot(_mplus_snapshot(), (_mplus_spec(),))
        self.assertTrue(self.api.observe_lot(_mplus_snapshot(form_fields={"amount": "2"})))
        self.assertEqual(self.mappings.get("mock-seller", "mock-lot").state, MappingState.REVALIDATION_REQUIRED)  # type: ignore[union-attr]

        self.api.remap_lot(_mplus_snapshot(form_fields={"amount": "2"}), (_mplus_spec(),))
        self.assertTrue(self.api.observe_lot(_mplus_snapshot(form_options={"mode": ("another",)})))
        self.assertEqual(self.mappings.get("mock-seller", "mock-lot").state, MappingState.REVALIDATION_REQUIRED)  # type: ignore[union-attr]

    def test_matcher_does_not_persist_or_auto_accept_assessment(self) -> None:
        self._verified_seller()
        assessment = SellerMatchingEngine().match(_mplus_snapshot(), (_mplus_spec(),))
        self.assertEqual(assessment.result, MatchResult.EXACT)
        self.assertIsNone(self.mappings.get("mock-seller", "mock-lot"))

    def test_legacy_non_mythic_seller_is_hidden_and_preserved(self) -> None:
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO trusted_seller_profiles
                (seller_id, nickname, family, enabled, verification_state, last_checked_state)
                VALUES ('legacy-seller', 'Legacy Seller', 'delves', 1, 'verified', 'current')"""
            )
        self.assertIsNone(self.sellers.get("legacy-seller"))
        self.assertNotIn("legacy-seller", {item.seller_id for item in self.sellers.list()})
        self.assertFalse(self.sellers.remove_seller("legacy-seller"))
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT 1 FROM trusted_seller_profiles WHERE seller_id = 'legacy-seller'"
            ).fetchone()
        self.assertIsNotNone(row)

    def _verified_seller(self) -> None:
        self.api.add_mock_seller(
            "mock-seller", "Mock Seller",
            verification_state=SellerVerificationState.VERIFIED,
        )

    def _store_service(self, code: str) -> None:
        with self.database.session() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO service_catalog
                (stable_code, family, variant_json, enabled, desired_state, template_reference,
                 description_profile, price_policy_reference, price_conditions_json)
                VALUES (?, 'mythic_plus', '{}', 1, 'enabled', 'template', 'profile', 'policy', '{}')""",
                (code,),
            )


def _mplus_spec(code: str = "mplus_k10_eu_selfplay_x1") -> ServiceMatchSpec:
    return ServiceMatchSpec(
        service_code=code, family=SellerFamily.MYTHIC_PLUS, category="wow-mplus", region="eu",
        key_level=10, service_format="selfplay", package_size=1,
        substantial_conditions={"timed": "yes"},
    )

def _mplus_snapshot(*, region: str | None = "eu", key_level: int | None = 10, title: str = "Mock M+ 10",
                    conditions: dict[str, str] | None = None, form_fields: dict[str, str] | None = None,
                    form_options: dict[str, tuple[str, ...]] | None = None) -> CompetitorLotSnapshot:
    return CompetitorLotSnapshot(
        seller_id="mock-seller", lot_id="mock-lot", title=title, family=SellerFamily.MYTHIC_PLUS, category="wow-mplus",
        region=region, key_level=key_level, service_format="selfplay", package_size=1,
        substantial_conditions=conditions if conditions is not None else {"timed": "yes"},
        form_fields=form_fields if form_fields is not None else {"amount": "1"},
        form_options=form_options if form_options is not None else {"mode": ("selfplay",)},
    )
