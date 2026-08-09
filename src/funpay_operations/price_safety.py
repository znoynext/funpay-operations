"""Explainable local safety validation for trusted-seller pricing.

The module receives only normalized local observations. It makes no network
request and never invokes a lot write operation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Mapping

from .pricing import (
    OwnLotPriceState,
    PriceAction,
    PriceDecision,
    PricePolicy,
    PricingEngine,
    TrustedPriceObservation,
)
from .trusted_sellers import CompetitorLotMapping, MappingState, SellerVerificationState, TrustedSeller


class SafetyDecisionStatus(StrEnum):
    VALID = "valid"
    SUSPICIOUS = "suspicious"
    REJECTED = "rejected"
    AWAITING_CONFIRMATION = "awaiting_confirmation"


_STABLE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class PriceObservationRecord:
    observation_id: str
    observation: TrustedPriceObservation
    lot_identity_hash: str
    structural_signature: str
    sequence: int


@dataclass(frozen=True)
class ValidatedPriceObservation:
    record: PriceObservationRecord
    status: SafetyDecisionStatus
    reason: str


@dataclass(frozen=True)
class SafetyPolicy:
    """Detection thresholds; none is a maximum permitted price drop."""

    outlier_low_ratio_bps: int = 8_000
    single_seller_confirmation_count: int = 3
    single_seller_tolerance_bps: int = 250
    high_volatility_min_sellers: int = 2
    mass_suspicious_service_count: int = 3
    mass_extreme_signal_bps: int = 7_000

    def __post_init__(self) -> None:
        for name in ("outlier_low_ratio_bps", "single_seller_tolerance_bps", "mass_extreme_signal_bps"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 10_000:
                raise ValueError(f"{name} must be an integer from 1 to 10000")
        for name in ("single_seller_confirmation_count", "high_volatility_min_sellers", "mass_suspicious_service_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 2:
                raise ValueError(f"{name} must be an integer of at least 2")


@dataclass(frozen=True)
class MarketConsensusDecision:
    service_code: str
    observations: tuple[ValidatedPriceObservation, ...]
    accepted_observations: tuple[TrustedPriceObservation, ...]
    status: SafetyDecisionStatus
    high_volatility_consensus: bool
    reason: str


@dataclass(frozen=True)
class SafetyPriceDecision:
    price_decision: PriceDecision
    consensus: MarketConsensusDecision


@dataclass(frozen=True)
class BatchSafetyDecision:
    status: SafetyDecisionStatus
    blocked_service_codes: tuple[str, ...]
    reason: str


class PriceObservationValidator:
    """Rejects observations that cannot safely represent a stable mapped lot."""

    def validate_batch(
        self, records: tuple[PriceObservationRecord, ...], *, sellers: tuple[TrustedSeller, ...],
        mappings: tuple[CompetitorLotMapping, ...], policy: PricePolicy,
        history: tuple[PriceObservationRecord, ...] = (),
    ) -> tuple[ValidatedPriceObservation, ...]:
        sellers_by_id = {seller.seller_id: seller for seller in sellers}
        mappings_by_lot = {(item.seller_id, item.competitor_lot_id): item for item in mappings}
        if len(sellers_by_id) != len(sellers) or len(mappings_by_lot) != len(mappings):
            raise ValueError("seller and competitor mapping identifiers must be unique")
        history_by_lot: dict[tuple[str, str, str], list[PriceObservationRecord]] = defaultdict(list)
        for item in history:
            history_by_lot[_history_key(item)].append(item)
        seen: set[str] = set()
        results: list[ValidatedPriceObservation] = []
        for record in records:
            if record.observation_id in seen:
                results.append(ValidatedPriceObservation(record, SafetyDecisionStatus.REJECTED, "duplicate observation id"))
                continue
            seen.add(record.observation_id)
            reason = _validation_failure(record, sellers_by_id, mappings_by_lot, history_by_lot, policy)
            results.append(ValidatedPriceObservation(
                record, SafetyDecisionStatus.REJECTED if reason else SafetyDecisionStatus.VALID,
                reason or "seller, mapping, identity, currency, and price are valid",
            ))
        return tuple(results)


class MarketConsensusEngine:
    """Excludes isolated lows while accepting explainable multi-seller volatility."""

    def __init__(self, safety_policy: SafetyPolicy = SafetyPolicy()) -> None:
        self.policy = safety_policy

    def evaluate(
        self, service_code: str, observations: tuple[ValidatedPriceObservation, ...], *,
        history: tuple[PriceObservationRecord, ...] = (),
    ) -> MarketConsensusDecision:
        relevant = tuple(item for item in observations if item.record.observation.service_code == service_code)
        candidates = [item for item in relevant if item.status is SafetyDecisionStatus.VALID]
        if not candidates:
            return _consensus(service_code, relevant, (), SafetyDecisionStatus.REJECTED, False, "no structurally valid observation")
        initial_valid_count = len(candidates)

        downward_sellers = _downward_sellers(candidates, history)
        high_volatility = len(downward_sellers) >= self.policy.high_volatility_min_sellers
        if high_volatility:
            candidates = _exclude_non_consensus_low_outlier(candidates, downward_sellers, self.policy)
        else:
            candidates = _exclude_single_low_outlier(candidates, self.policy)

        accepted = [item for item in candidates if item.status is SafetyDecisionStatus.VALID]
        if len(accepted) == 1 and initial_valid_count == 1:
            single_checked = _confirm_single_seller(accepted[0], history, self.policy)[0]
            candidates = [
                single_checked if item.record.observation_id == single_checked.record.observation_id else item
                for item in candidates
            ]
            accepted = [single_checked]
        accepted = [item for item in accepted if item.status is SafetyDecisionStatus.VALID]
        updated_observations = _merged_observations(relevant, candidates)
        if accepted:
            reason = (
                f"high-volatility consensus: {len(downward_sellers)} independent sellers moved down"
                if high_volatility else "valid trusted observations remain after safety validation"
            )
            return _consensus(
                service_code, updated_observations,
                tuple(item.record.observation for item in accepted), SafetyDecisionStatus.VALID, high_volatility, reason,
            )
        statuses = [item.status for item in candidates]
        status = SafetyDecisionStatus.AWAITING_CONFIRMATION if SafetyDecisionStatus.AWAITING_CONFIRMATION in statuses else SafetyDecisionStatus.SUSPICIOUS
        reason = "single seller needs stable consecutive observations" if status is SafetyDecisionStatus.AWAITING_CONFIRMATION else "all valid observations are suspicious"
        return _consensus(service_code, updated_observations, (), status, high_volatility, reason)

    def protect_batch(self, decisions: tuple[SafetyPriceDecision, ...]) -> BatchSafetyDecision:
        service_codes = [item.price_decision.service_code for item in decisions]
        if len(service_codes) != len(set(service_codes)):
            raise ValueError("batch safety decisions must have unique service codes")
        flagged = tuple(sorted(
            item.price_decision.service_code for item in decisions
            if item.price_decision.final_target_minor < item.price_decision.current_price_minor
            and item.price_decision.final_target_minor * 10_000
            <= item.price_decision.current_price_minor * self.policy.mass_extreme_signal_bps
            and not item.consensus.high_volatility_consensus
        ))
        if len(flagged) >= self.policy.mass_suspicious_service_count:
            return BatchSafetyDecision(
                SafetyDecisionStatus.REJECTED, flagged,
                "mass safety block: multiple extreme targets lack independent market consensus",
            )
        return BatchSafetyDecision(SafetyDecisionStatus.VALID, (), "batch has no mass suspicious change signal")


class SafetyValidatedPricingEngine:
    """Composes validation, consensus, and the existing pure pricing calculation."""

    def __init__(self, *, validator: PriceObservationValidator | None = None,
                 consensus_engine: MarketConsensusEngine | None = None, pricing_engine: PricingEngine | None = None) -> None:
        self.validator = validator or PriceObservationValidator()
        self.consensus_engine = consensus_engine or MarketConsensusEngine()
        self.pricing_engine = pricing_engine or PricingEngine()

    def decide(
        self, own_lot: OwnLotPriceState, *, sellers: tuple[TrustedSeller, ...],
        mappings: tuple[CompetitorLotMapping, ...], records: tuple[PriceObservationRecord, ...],
        history: tuple[PriceObservationRecord, ...], policy: PricePolicy,
    ) -> SafetyPriceDecision:
        validated = self.validator.validate_batch(records, sellers=sellers, mappings=mappings, policy=policy, history=history)
        consensus = self.consensus_engine.evaluate(own_lot.service_code, validated, history=history)
        return SafetyPriceDecision(
            self.pricing_engine.decide(
                own_lot, sellers=sellers, mappings=mappings, observations=consensus.accepted_observations, policy=policy,
            ), consensus,
        )

    def batch_preview(
        self, own_lots: tuple[OwnLotPriceState, ...], *, sellers: tuple[TrustedSeller, ...],
        mappings: tuple[CompetitorLotMapping, ...], records: tuple[PriceObservationRecord, ...],
        history: tuple[PriceObservationRecord, ...], policies: Mapping[str, PricePolicy],
    ) -> tuple[tuple[SafetyPriceDecision, ...], BatchSafetyDecision]:
        service_codes = [lot.service_code for lot in own_lots]
        if len(service_codes) != len(set(service_codes)):
            raise ValueError("batch preview requires unique service codes")
        grouped: dict[str, list[PriceObservationRecord]] = defaultdict(list)
        for record in records:
            grouped[record.observation.service_code].append(record)
        decisions = tuple(
            self.decide(
                lot, sellers=sellers, mappings=mappings, records=tuple(grouped[lot.service_code]),
                history=history, policy=policies[lot.service_code],
            )
            for lot in sorted(own_lots, key=lambda item: item.service_code)
        )
        batch = self.consensus_engine.protect_batch(decisions)
        if batch.status is not SafetyDecisionStatus.REJECTED:
            return decisions, batch
        blocked = set(batch.blocked_service_codes)
        return tuple(
            SafetyPriceDecision(
                replace(
                    item.price_decision, final_target_minor=item.price_decision.current_price_minor,
                    action=PriceAction.KEEP_CURRENT_PRICE, reason="batch safety block prevents automatic price change",
                ) if item.price_decision.service_code in blocked and item.price_decision.action is PriceAction.UPDATE_PRICE else item.price_decision,
                item.consensus,
            )
            for item in decisions
        ), batch


def _validation_failure(
    record: PriceObservationRecord, sellers: Mapping[str, TrustedSeller],
    mappings: Mapping[tuple[str, str], CompetitorLotMapping],
    history_by_lot: Mapping[tuple[str, str, str], list[PriceObservationRecord]], policy: PricePolicy,
) -> str | None:
    observation = record.observation
    if not _STABLE_ID.fullmatch(observation.seller_id):
        return "seller id is not stable"
    if not _STABLE_ID.fullmatch(observation.competitor_lot_id):
        return "competitor lot id is not stable"
    if not _STABLE_ID.fullmatch(record.observation_id) or record.sequence < 1:
        return "observation identity is invalid"
    seller = sellers.get(observation.seller_id)
    if seller is None or not seller.enabled:
        return "seller is not enabled and trusted"
    if seller.verification_state is not SellerVerificationState.VERIFIED:
        return "seller is not verified"
    mapping = mappings.get((observation.seller_id, observation.competitor_lot_id))
    if mapping is None:
        return "competitor lot has no mapping"
    if mapping.state is not MappingState.CONFIRMED:
        return "competitor lot mapping requires revalidation"
    if mapping.service_code != observation.service_code:
        return "mapping does not exactly match service code"
    if not observation.is_valid:
        return "observation is marked invalid"
    if observation.currency != policy.currency:
        return "currency does not match policy"
    if not isinstance(observation.price_minor, int) or isinstance(observation.price_minor, bool) or observation.price_minor <= 0:
        return "price is not a positive integer minor-unit value"
    if not record.lot_identity_hash or not record.structural_signature:
        return "lot identity or structural signature is missing"
    if record.lot_identity_hash != mapping.material_snapshot_hash:
        return "material lot identity changed"
    structural_history = history_by_lot.get(_history_key(record), ())
    if any(item.structural_signature != record.structural_signature for item in structural_history):
        return "historical structural signature changed"
    return None


def _history_key(record: PriceObservationRecord) -> tuple[str, str, str]:
    observation = record.observation
    return observation.seller_id, observation.competitor_lot_id, observation.service_code


def _downward_sellers(candidates: list[ValidatedPriceObservation], history: tuple[PriceObservationRecord, ...]) -> set[str]:
    result: set[str] = set()
    for candidate in candidates:
        record = candidate.record
        previous = [
            item for item in history if _history_key(item) == _history_key(record)
            and item.sequence < record.sequence and item.structural_signature == record.structural_signature
            and item.lot_identity_hash == record.lot_identity_hash and isinstance(item.observation.price_minor, int)
            and not isinstance(item.observation.price_minor, bool) and item.observation.price_minor > 0
        ]
        if previous:
            last = max(previous, key=lambda item: item.sequence)
            if record.observation.price_minor < last.observation.price_minor:  # type: ignore[operator]
                result.add(record.observation.seller_id)
    return result


def _exclude_single_low_outlier(
    candidates: list[ValidatedPriceObservation], policy: SafetyPolicy,
) -> list[ValidatedPriceObservation]:
    if len(candidates) < 2:
        return candidates
    ordered = sorted(candidates, key=lambda item: item.record.observation.price_minor or 0)
    lowest, next_lowest = ordered[0], ordered[1]
    low_price, next_price = lowest.record.observation.price_minor, next_lowest.record.observation.price_minor
    if low_price is not None and next_price is not None and low_price * 10_000 < next_price * policy.outlier_low_ratio_bps:
        return [replace(lowest, status=SafetyDecisionStatus.SUSPICIOUS, reason="isolated low price is a suspicious outlier")] + ordered[1:]
    return candidates


def _exclude_non_consensus_low_outlier(
    candidates: list[ValidatedPriceObservation], downward_sellers: set[str], policy: SafetyPolicy,
) -> list[ValidatedPriceObservation]:
    consensus_prices = [
        item.record.observation.price_minor for item in candidates if item.record.observation.seller_id in downward_sellers
    ]
    if not consensus_prices:
        return candidates
    consensus_minimum = min(price for price in consensus_prices if price is not None)
    result: list[ValidatedPriceObservation] = []
    for item in candidates:
        price = item.record.observation.price_minor
        if price is not None and item.record.observation.seller_id not in downward_sellers and price * 10_000 < consensus_minimum * policy.outlier_low_ratio_bps:
            result.append(replace(item, status=SafetyDecisionStatus.SUSPICIOUS, reason="low price lacks downward consensus"))
        else:
            result.append(item)
    return result


def _confirm_single_seller(
    candidate: ValidatedPriceObservation, history: tuple[PriceObservationRecord, ...], policy: SafetyPolicy,
) -> list[ValidatedPriceObservation]:
    record = candidate.record
    series = [
        item for item in history if _history_key(item) == _history_key(record) and item.sequence < record.sequence
        and item.structural_signature == record.structural_signature and item.lot_identity_hash == record.lot_identity_hash
        and isinstance(item.observation.price_minor, int) and not isinstance(item.observation.price_minor, bool)
        and item.observation.price_minor > 0
    ] + [record]
    series.sort(key=lambda item: item.sequence)
    if len(series) < policy.single_seller_confirmation_count:
        return [replace(candidate, status=SafetyDecisionStatus.AWAITING_CONFIRMATION, reason="single seller lacks consecutive confirmations")]
    prices = [item.observation.price_minor for item in series[-policy.single_seller_confirmation_count:]]
    minimum, maximum = min(prices), max(prices)
    if minimum * 10_000 < maximum * (10_000 - policy.single_seller_tolerance_bps):
        return [replace(candidate, status=SafetyDecisionStatus.AWAITING_CONFIRMATION, reason="single seller observations are not close enough")]
    return [candidate]


def _consensus(
    service_code: str, observations: tuple[ValidatedPriceObservation, ...], accepted: tuple[TrustedPriceObservation, ...],
    status: SafetyDecisionStatus, high_volatility: bool, reason: str,
) -> MarketConsensusDecision:
    return MarketConsensusDecision(service_code, observations, accepted, status, high_volatility, reason)


def _merged_observations(
    original: tuple[ValidatedPriceObservation, ...], updates: list[ValidatedPriceObservation],
) -> tuple[ValidatedPriceObservation, ...]:
    by_id = {item.record.observation_id: item for item in updates}
    return tuple(by_id.get(item.record.observation_id, item) for item in original)
