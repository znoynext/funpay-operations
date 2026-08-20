from __future__ import annotations

import unittest

from funpay_operations.pricing import (
    OwnLotPriceState,
    OwnLotPricingMode,
    PriceAction,
    PricePolicy,
    PricingEngine,
    TrustedPriceObservation,
)
from funpay_operations.trusted_sellers import (
    CompetitorLotMapping,
    MappingState,
    SellerFamily,
    SellerLastCheckedState,
    SellerVerificationState,
    TrustedSeller,
)


class PricingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PricingEngine()
        self.policy = PricePolicy(hard_floor=5_000, price_step_minor=50, currency="RUB")
        self.own = _own()

    def test_one_seller_uses_exact_integer_99_percent_target(self) -> None:
        decision = self.engine.decide(
            self.own, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),), policy=self.policy,
        )
        self.assertEqual(decision.minimum_valid_price_minor, 10_000)
        self.assertEqual(decision.percent_99_price_minor, 9_900)
        self.assertEqual(decision.rounded_price_minor, 9_900)
        self.assertEqual(decision.final_target_minor, 9_900)
        self.assertEqual(decision.action, PriceAction.UPDATE_PRICE)

    def test_many_sellers_uses_only_minimum_valid_trusted_price(self) -> None:
        sellers = (_seller("a"), _seller("b"), _seller("c"))
        mappings = (_mapping("a"), _mapping("b"), _mapping("c"))
        observations = (_observation(11_000, "a"), _observation(9_000, "b"), _observation(10_000, "c"))
        decision = self.engine.decide(self.own, sellers=sellers, mappings=mappings, observations=observations, policy=self.policy)
        self.assertEqual(decision.minimum_valid_price_minor, 9_000)
        self.assertEqual(decision.final_target_minor, 8_900)
        self.assertEqual(len(decision.observations), 3)

    def test_no_valid_sellers_keeps_current_price(self) -> None:
        decision = self.engine.decide(
            self.own, sellers=(), mappings=(), observations=(_observation(10_000, "untrusted"),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.KEEP_CURRENT_PRICE)
        self.assertEqual(decision.final_target_minor, self.own.current_price_minor)
        self.assertEqual(len(decision.excluded_observations), 1)

    def test_equal_prices_are_all_valid_and_produce_same_target(self) -> None:
        decision = self.engine.decide(
            self.own, sellers=(_seller("a"), _seller("b")), mappings=(_mapping("a"), _mapping("b")),
            observations=(_observation(10_000, "a"), _observation(10_000, "b")), policy=self.policy,
        )
        self.assertEqual(decision.minimum_valid_price_minor, 10_000)
        self.assertEqual(len(decision.observations), 2)
        self.assertEqual(decision.final_target_minor, 9_900)

    def test_hard_floor_applies_after_rounding(self) -> None:
        policy = PricePolicy(hard_floor=9_950, price_step_minor=50, currency="RUB")
        decision = self.engine.decide(
            self.own, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),), policy=policy,
        )
        self.assertEqual(decision.rounded_price_minor, 9_900)
        self.assertEqual(decision.hard_floor_minor, 9_950)
        self.assertEqual(decision.final_target_minor, 9_950)

    def test_rounding_is_down_to_configured_step(self) -> None:
        decision = self.engine.decide(
            self.own, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_099),), policy=self.policy,
        )
        self.assertEqual(decision.percent_99_price_minor, 9_998)
        self.assertEqual(decision.rounded_price_minor, 9_950)
        self.assertEqual(decision.final_target_minor, 9_950)

    def test_fixed_price_uses_manual_value_without_observation_processing(self) -> None:
        fixed = _own(mode=OwnLotPricingMode.FIXED_PRICE, fixed_price_minor=12_000)
        decision = self.engine.decide(
            fixed, sellers=(), mappings=(), observations=(_observation(10_000),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.FIXED_PRICE)
        self.assertEqual(decision.final_target_minor, 12_000)
        self.assertEqual(decision.observations, ())

    def test_paused_never_calculates_an_automatic_target(self) -> None:
        paused = _own(mode=OwnLotPricingMode.PAUSED)
        decision = self.engine.decide(
            paused, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.PAUSED)
        self.assertIsNone(decision.minimum_valid_price_minor)
        self.assertEqual(decision.final_target_minor, paused.current_price_minor)

    def test_check_only_calculates_but_never_requests_a_write(self) -> None:
        check_only = _own(mode=OwnLotPricingMode.CHECK_ONLY)
        decision = self.engine.decide(
            check_only, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.CHECK_ONLY)
        self.assertEqual(decision.final_target_minor, 9_900)

    def test_automatic_lot_does_not_request_a_noop_price_update(self) -> None:
        current_target = _own(current_price_minor=9_900)
        decision = self.engine.decide(
            current_target, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.KEEP_CURRENT_PRICE)
        self.assertEqual(decision.final_target_minor, current_target.current_price_minor)

    def test_invalid_currencies_are_excluded_and_own_currency_cannot_be_converted(self) -> None:
        decision = self.engine.decide(
            self.own, sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000, currency="USD"),), policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.KEEP_CURRENT_PRICE)
        self.assertEqual(decision.excluded_observations[0].reason, "observation currency does not match policy currency")
        with self.assertRaisesRegex(ValueError, "currency"):
            self.engine.decide(_own(currency="USD"), sellers=(), mappings=(), observations=(), policy=self.policy)

    def test_invalid_prices_and_unconfirmed_or_disabled_sources_are_excluded(self) -> None:
        disabled = _seller("disabled", enabled=False)
        revalidation = _mapping("revalidation", state=MappingState.REVALIDATION_REQUIRED)
        observations = (
            _observation(0, "a"), _observation(-1, "b"), _observation(100.0, "c"),  # type: ignore[arg-type]
            _observation(10_000, "disabled"), _observation(10_000, "revalidation"),
        )
        decision = self.engine.decide(
            self.own, sellers=(_seller("a"), _seller("b"), _seller("c"), disabled, _seller("revalidation")),
            mappings=(_mapping("a"), _mapping("b"), _mapping("c"), _mapping("disabled"), revalidation),
            observations=observations, policy=self.policy,
        )
        self.assertEqual(decision.action, PriceAction.KEEP_CURRENT_PRICE)
        self.assertEqual(len(decision.excluded_observations), 5)

    def test_batch_preview_is_sorted_and_keeps_services_isolated(self) -> None:
        another = _own(service_code="mplus_k12_eu_selfplay_x1")
        decisions = self.engine.batch_preview(
            (another, self.own), sellers=(_seller(),), mappings=(_mapping(),), observations=(_observation(10_000),),
            policies={self.own.service_code: self.policy, another.service_code: self.policy},
        )
        self.assertEqual(
            [item.service_code for item in decisions],
            sorted((another.service_code, self.own.service_code)),
        )
        by_code = {item.service_code: item for item in decisions}
        self.assertEqual(by_code[another.service_code].action, PriceAction.KEEP_CURRENT_PRICE)
        self.assertEqual(by_code[self.own.service_code].action, PriceAction.UPDATE_PRICE)

    def test_duplicate_mapping_or_observation_is_not_silently_selected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate competitor mappings"):
            self.engine.decide(
                self.own, sellers=(_seller(),), mappings=(_mapping(), _mapping()), observations=(), policy=self.policy,
            )
        decision = self.engine.decide(
            self.own, sellers=(_seller(),), mappings=(_mapping(),),
            observations=(_observation(10_000), _observation(9_000)), policy=self.policy,
        )
        self.assertEqual(decision.minimum_valid_price_minor, 10_000)
        self.assertEqual(decision.excluded_observations[0].reason, "duplicate observation for competitor lot")


def _own(*, service_code: str = "mplus_k10_eu_selfplay_x1", current_price_minor: int = 11_000,
         currency: str = "RUB", mode: OwnLotPricingMode = OwnLotPricingMode.AUTOMATIC,
         fixed_price_minor: int | None = None) -> OwnLotPriceState:
    return OwnLotPriceState(service_code, current_price_minor, currency, mode, fixed_price_minor)


def _seller(seller_id: str = "a", *, enabled: bool = True) -> TrustedSeller:
    return TrustedSeller(
        seller_id, f"mock-{seller_id}", SellerFamily.MYTHIC_PLUS, enabled,
        SellerVerificationState.VERIFIED, SellerLastCheckedState.CURRENT,
    )


def _mapping(seller_id: str = "a", *, state: MappingState = MappingState.CONFIRMED) -> CompetitorLotMapping:
    return CompetitorLotMapping(seller_id, f"lot-{seller_id}", "mplus_k10_eu_selfplay_x1", state, "mock-hash")


def _observation(price_minor: int | None, seller_id: str = "a", *, currency: str | None = "RUB") -> TrustedPriceObservation:
    return TrustedPriceObservation(seller_id, f"lot-{seller_id}", "mplus_k10_eu_selfplay_x1", price_minor, currency)
