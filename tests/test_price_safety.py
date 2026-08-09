from __future__ import annotations

import unittest

from funpay_operations.price_safety import (
    BatchSafetyDecision,
    MarketConsensusDecision,
    MarketConsensusEngine,
    PriceObservationRecord,
    PriceObservationValidator,
    SafetyDecisionStatus,
    SafetyPriceDecision,
    SafetyValidatedPricingEngine,
)
from funpay_operations.pricing import OwnLotPriceState, OwnLotPricingMode, PriceAction, PriceDecision, PricePolicy, TrustedPriceObservation
from funpay_operations.trusted_sellers import (
    CompetitorLotMapping,
    MappingState,
    SellerFamily,
    SellerLastCheckedState,
    SellerVerificationState,
    TrustedSeller,
)


class PriceObservationValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PriceObservationValidator()
        self.policy = PricePolicy(hard_floor=1_000, price_step_minor=100, currency="RUB")

    def test_accepts_only_enabled_stable_exact_mapped_identity(self) -> None:
        result = self.validator.validate_batch(
            (_record("a", 10_000),), sellers=(_seller("a"),), mappings=(_mapping("a"),), policy=self.policy,
        )
        self.assertEqual(result[0].status, SafetyDecisionStatus.VALID)

    def test_rejects_each_structural_safety_failure(self) -> None:
        disabled = _seller("a", enabled=False)
        cases = (
            (_record("a", 10_000), (disabled,), (_mapping("a"),), (), "not enabled"),
            (_record("bad id", 10_000), (_seller("a"),), (_mapping("a"),), (), "not stable"),
            (_record("a", 10_000), (_seller("a"),), (_mapping("a", state=MappingState.REVALIDATION_REQUIRED),), (), "revalidation"),
            (_record("a", 10_000, service_code="other"), (_seller("a"),), (_mapping("a"),), (), "exactly match"),
            (_record("a", 10_000, currency="USD"), (_seller("a"),), (_mapping("a"),), (), "currency"),
            (_record("a", 0), (_seller("a"),), (_mapping("a"),), (), "positive"),
            (_record("a", 10_000, identity_hash="changed"), (_seller("a"),), (_mapping("a"),), (), "identity changed"),
            (_record("a", 10_000, signature="new"), (_seller("a"),), (_mapping("a"),), (_record("a", 10_000, sequence=1, signature="old"),), "structural"),
        )
        for record, sellers, mappings, history, expected in cases:
            with self.subTest(expected=expected):
                result = self.validator.validate_batch((record,), sellers=sellers, mappings=mappings, policy=self.policy, history=history)
                self.assertEqual(result[0].status, SafetyDecisionStatus.REJECTED)
                self.assertIn(expected, result[0].reason)


class MarketConsensusEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PriceObservationValidator()
        self.engine = MarketConsensusEngine()
        self.policy = PricePolicy(hard_floor=1_000, price_step_minor=100, currency="RUB")

    def test_isolated_low_outlier_is_suspicious_and_next_valid_minimum_remains(self) -> None:
        records = (_record("a", 5_000), _record("b", 10_000), _record("c", 10_100))
        validated = self._validated(records)
        decision = self.engine.evaluate("mplus", validated)
        self.assertEqual(decision.status, SafetyDecisionStatus.VALID)
        self.assertEqual([item.price_minor for item in decision.accepted_observations], [10_000, 10_100])
        suspicious = next(item for item in decision.observations if item.record.observation.seller_id == "a")
        self.assertEqual(suspicious.status, SafetyDecisionStatus.SUSPICIOUS)

    def test_next_valid_minimum_is_used_even_if_outlier_leaves_one_peer(self) -> None:
        records = (_record("a", 5_000), _record("b", 10_000))
        decision = self.engine.evaluate("mplus", self._validated(records))
        self.assertEqual(decision.status, SafetyDecisionStatus.VALID)
        self.assertEqual([item.price_minor for item in decision.accepted_observations], [10_000])

    def test_multi_seller_downward_consensus_accepts_large_real_drop_without_cap(self) -> None:
        records = (_record("a", 4_000, sequence=3), _record("b", 4_100, sequence=3), _record("c", 10_000, sequence=3))
        history = (_record("a", 10_000, sequence=1), _record("b", 10_000, sequence=1), _record("c", 10_000, sequence=1))
        decision = self.engine.evaluate("mplus", self._validated(records, history), history=history)
        self.assertEqual(decision.status, SafetyDecisionStatus.VALID)
        self.assertTrue(decision.high_volatility_consensus)
        self.assertIn(4_000, [item.price_minor for item in decision.accepted_observations])

    def test_single_seller_requires_stable_consecutive_identity_preserving_observations(self) -> None:
        current = _record("a", 4_000, sequence=3)
        history = (_record("a", 4_000, sequence=1), _record("a", 4_050, sequence=2))
        accepted = self.engine.evaluate("mplus", self._validated((current,), history), history=history)
        self.assertEqual(accepted.status, SafetyDecisionStatus.VALID)
        self.assertEqual(accepted.accepted_observations[0].price_minor, 4_000)

        awaiting = self.engine.evaluate("mplus", self._validated((current,)))
        self.assertEqual(awaiting.status, SafetyDecisionStatus.AWAITING_CONFIRMATION)
        self.assertEqual(awaiting.accepted_observations, ())

    def _validated(self, records: tuple[PriceObservationRecord, ...], history: tuple[PriceObservationRecord, ...] = ()):
        seller_ids = tuple(record.observation.seller_id for record in records)
        return self.validator.validate_batch(
            records, sellers=tuple(_seller(item) for item in seller_ids), mappings=tuple(_mapping(item) for item in seller_ids),
            policy=self.policy, history=history,
        )


class SafetyValidatedPricingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SafetyValidatedPricingEngine()
        self.policy = PricePolicy(hard_floor=1_000, price_step_minor=100, currency="RUB")

    def test_safe_wrapper_passes_only_next_valid_minimum_to_pricing(self) -> None:
        records = (_record("a", 5_000), _record("b", 10_000), _record("c", 10_100))
        result = self.engine.decide(
            _own(), sellers=(_seller("a"), _seller("b"), _seller("c")),
            mappings=(_mapping("a"), _mapping("b"), _mapping("c")), records=records, history=(), policy=self.policy,
        )
        self.assertEqual(result.consensus.status, SafetyDecisionStatus.VALID)
        self.assertEqual(result.price_decision.minimum_valid_price_minor, 10_000)
        self.assertEqual(result.price_decision.final_target_minor, 9_900)

    def test_mass_extreme_non_consensus_batch_is_blocked_but_consensus_batch_is_not(self) -> None:
        guard = MarketConsensusEngine()
        blocked = tuple(_safety_decision(f"service-{index}", high_volatility=False) for index in range(3))
        decision = guard.protect_batch(blocked)
        self.assertEqual(decision.status, SafetyDecisionStatus.REJECTED)
        self.assertEqual(len(decision.blocked_service_codes), 3)

        accepted = tuple(_safety_decision(f"service-{index}", high_volatility=True) for index in range(3))
        self.assertEqual(guard.protect_batch(accepted).status, SafetyDecisionStatus.VALID)

    def test_batch_preview_converts_blocked_automatic_actions_to_keep_current(self) -> None:
        services = ("service-a", "service-b", "service-c")
        sellers = tuple(_seller(item[-1]) for item in services)
        mappings = tuple(_mapping(item[-1], service_code=item) for item in services)
        records = tuple(_record(item[-1], 4_000, service_code=item, sequence=3) for item in services)
        history = tuple(
            record for item in services for record in (
                _record(item[-1], 4_000, service_code=item, sequence=1),
                _record(item[-1], 4_000, service_code=item, sequence=2),
            )
        )
        decisions, batch = self.engine.batch_preview(
            tuple(_own(item) for item in services), sellers=sellers, mappings=mappings, records=records,
            history=history, policies={item: self.policy for item in services},
        )
        self.assertEqual(batch.status, SafetyDecisionStatus.REJECTED)
        self.assertTrue(all(item.price_decision.action is PriceAction.KEEP_CURRENT_PRICE for item in decisions))


def _seller(seller_id: str, *, enabled: bool = True) -> TrustedSeller:
    return TrustedSeller(
        seller_id, f"mock-{seller_id}", SellerFamily.MYTHIC_PLUS, enabled,
        SellerVerificationState.VERIFIED, SellerLastCheckedState.CURRENT,
    )


def _mapping(seller_id: str, *, service_code: str = "mplus", state: MappingState = MappingState.CONFIRMED) -> CompetitorLotMapping:
    return CompetitorLotMapping(seller_id, f"lot-{seller_id}", service_code, state, f"hash-{seller_id}")


def _record(seller_id: str, price_minor: int, *, service_code: str = "mplus", currency: str = "RUB",
            identity_hash: str | None = None, signature: str = "same", sequence: int = 2) -> PriceObservationRecord:
    observation = TrustedPriceObservation(seller_id, f"lot-{seller_id}", service_code, price_minor, currency)
    return PriceObservationRecord(f"obs-{seller_id}-{sequence}-{service_code}", observation, identity_hash or f"hash-{seller_id}", signature, sequence)


def _own(service_code: str = "mplus") -> OwnLotPriceState:
    return OwnLotPriceState(service_code, 10_000, "RUB", OwnLotPricingMode.AUTOMATIC)


def _safety_decision(service_code: str, *, high_volatility: bool) -> SafetyPriceDecision:
    price = PriceDecision(
        service_code, 10_000, (), (), 4_000, 3_960, 3_900, 1_000, 3_900,
        PriceAction.UPDATE_PRICE, "mock extreme target",
    )
    consensus = MarketConsensusDecision(service_code, (), (), SafetyDecisionStatus.VALID, high_volatility, "mock")
    return SafetyPriceDecision(price, consensus)
