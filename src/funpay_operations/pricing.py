"""Deterministic local pricing from confirmed trusted-seller observations.

Prices are always integer minor units.  There is no network access, market
scraping, float arithmetic, currency conversion, or lot write in this module.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .trusted_sellers import (
    CompetitorLotMapping,
    MappingState,
    SellerVerificationState,
    TrustedSeller,
)


class OwnLotPricingMode(StrEnum):
    AUTOMATIC = "automatic"
    FIXED_PRICE = "fixed_price"
    PAUSED = "paused"
    CHECK_ONLY = "check_only"


class PriceAction(StrEnum):
    UPDATE_PRICE = "update_price"
    KEEP_CURRENT_PRICE = "keep_current_price"
    FIXED_PRICE = "fixed_price"
    PAUSED = "paused"
    CHECK_ONLY = "check_only"


@dataclass(frozen=True)
class PricePolicy:
    """Currency-specific local guardrails in integer minor units."""

    hard_floor: int | None
    price_step_minor: int = 1
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if not isinstance(self.price_step_minor, int) or isinstance(self.price_step_minor, bool) or self.price_step_minor <= 0:
            raise ValueError("price_step_minor must be a positive integer")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("currency is required")
        if self.hard_floor is not None:
            _valid_price(self.hard_floor, "hard_floor")
            if self.hard_floor % self.price_step_minor:
                raise ValueError("hard_floor must align to price_step_minor")

    def validate(self, price_minor: int) -> None:
        _valid_price(price_minor, "price")
        if self.hard_floor is not None and price_minor < self.hard_floor:
            raise ValueError("Price is below the configured hard floor")
        if price_minor % self.price_step_minor:
            raise ValueError("Price does not align to the configured price step")


@dataclass(frozen=True)
class OwnLotPriceState:
    service_code: str
    current_price_minor: int
    currency: str
    mode: OwnLotPricingMode
    fixed_price_minor: int | None = None


@dataclass(frozen=True)
class TrustedPriceObservation:
    seller_id: str
    competitor_lot_id: str
    service_code: str
    price_minor: int | None
    currency: str | None
    is_valid: bool = True


@dataclass(frozen=True)
class ExcludedObservation:
    observation: TrustedPriceObservation
    reason: str


@dataclass(frozen=True)
class PriceDecision:
    service_code: str
    current_price_minor: int
    observations: tuple[TrustedPriceObservation, ...]
    excluded_observations: tuple[ExcludedObservation, ...]
    minimum_valid_price_minor: int | None
    percent_99_price_minor: int | None
    rounded_price_minor: int | None
    hard_floor_minor: int | None
    final_target_minor: int
    action: PriceAction
    reason: str


class PricingEngine:
    """Pure price-decision engine; callers retain ownership of any later write."""

    def decide(
        self, own_lot: OwnLotPriceState, *, sellers: tuple[TrustedSeller, ...],
        mappings: tuple[CompetitorLotMapping, ...], observations: tuple[TrustedPriceObservation, ...],
        policy: PricePolicy,
    ) -> PriceDecision:
        _validate_own_lot(own_lot, policy)
        if policy.hard_floor is None:
            raise ValueError("pricing engine requires a configured hard_floor")
        if own_lot.mode is OwnLotPricingMode.FIXED_PRICE:
            fixed_price = own_lot.fixed_price_minor
            if fixed_price is None:
                raise ValueError("fixed_price mode requires fixed_price_minor")
            policy.validate(fixed_price)
            return _decision(own_lot, (), (), None, None, None, policy.hard_floor, fixed_price, PriceAction.FIXED_PRICE,
                             "fixed price is set manually")
        if own_lot.mode is OwnLotPricingMode.PAUSED:
            return _decision(own_lot, (), (), None, None, None, policy.hard_floor, own_lot.current_price_minor, PriceAction.PAUSED,
                             "lot pricing is paused")

        included, excluded = self._valid_observations(own_lot.service_code, sellers, mappings, observations, policy)
        if not included:
            return _decision(
                own_lot, (), excluded, None, None, None, policy.hard_floor, own_lot.current_price_minor,
                PriceAction.KEEP_CURRENT_PRICE, "no valid confirmed trusted observation exists",
            )
        minimum = min(observation.price_minor for observation in included if observation.price_minor is not None)
        percent_99 = minimum * 99 // 100
        rounded = percent_99 // policy.price_step_minor * policy.price_step_minor
        final_target = max(rounded, policy.hard_floor)
        if own_lot.mode is OwnLotPricingMode.CHECK_ONLY:
            action, reason = PriceAction.CHECK_ONLY, "check-only mode calculated a target without write authorization"
        elif final_target == own_lot.current_price_minor:
            action, reason = PriceAction.KEEP_CURRENT_PRICE, "calculated target already equals current price"
        else:
            action, reason = PriceAction.UPDATE_PRICE, "target is 99 percent of the minimum valid trusted price"
        return _decision(
            own_lot, included, excluded, minimum, percent_99, rounded, policy.hard_floor, final_target, action, reason
        )

    def batch_preview(
        self, own_lots: tuple[OwnLotPriceState, ...], *, sellers: tuple[TrustedSeller, ...],
        mappings: tuple[CompetitorLotMapping, ...], observations: tuple[TrustedPriceObservation, ...],
        policies: Mapping[str, PricePolicy],
    ) -> tuple[PriceDecision, ...]:
        service_codes = [lot.service_code for lot in own_lots]
        if len(service_codes) != len(set(service_codes)):
            raise ValueError("batch preview requires unique own-lot service codes")
        grouped: dict[str, list[TrustedPriceObservation]] = defaultdict(list)
        for observation in observations:
            grouped[observation.service_code].append(observation)
        return tuple(
            self.decide(
                lot, sellers=sellers, mappings=mappings, observations=tuple(grouped[lot.service_code]),
                policy=policies[lot.service_code],
            )
            for lot in sorted(own_lots, key=lambda item: item.service_code)
        )

    def _valid_observations(
        self, service_code: str, sellers: tuple[TrustedSeller, ...], mappings: tuple[CompetitorLotMapping, ...],
        observations: tuple[TrustedPriceObservation, ...], policy: PricePolicy,
    ) -> tuple[tuple[TrustedPriceObservation, ...], tuple[ExcludedObservation, ...]]:
        if len({seller.seller_id for seller in sellers}) != len(sellers):
            raise ValueError("trusted seller observations contain duplicate seller ids")
        if len({(mapping.seller_id, mapping.competitor_lot_id) for mapping in mappings}) != len(mappings):
            raise ValueError("trusted seller observations contain duplicate competitor mappings")
        sellers_by_id = {seller.seller_id: seller for seller in sellers}
        mappings_by_lot = {(mapping.seller_id, mapping.competitor_lot_id): mapping for mapping in mappings}
        included: list[TrustedPriceObservation] = []
        excluded: list[ExcludedObservation] = []
        seen_lots: set[tuple[str, str]] = set()
        for observation in observations:
            lot_key = (observation.seller_id, observation.competitor_lot_id)
            if lot_key in seen_lots:
                excluded.append(ExcludedObservation(observation, "duplicate observation for competitor lot"))
                continue
            seen_lots.add(lot_key)
            reason = _exclusion_reason(observation, service_code, sellers_by_id, mappings_by_lot, policy)
            if reason:
                excluded.append(ExcludedObservation(observation, reason))
            else:
                included.append(observation)
        return tuple(included), tuple(excluded)


def _exclusion_reason(
    observation: TrustedPriceObservation, service_code: str, sellers: Mapping[str, TrustedSeller],
    mappings: Mapping[tuple[str, str], CompetitorLotMapping], policy: PricePolicy,
) -> str | None:
    if observation.service_code != service_code:
        return "observation service code differs from requested service"
    seller = sellers.get(observation.seller_id)
    if seller is None:
        return "seller is not trusted"
    if not seller.enabled:
        return "seller is disabled"
    if seller.verification_state is not SellerVerificationState.VERIFIED:
        return "seller is not verified"
    mapping = mappings.get((observation.seller_id, observation.competitor_lot_id))
    if mapping is None:
        return "competitor lot has no confirmed mapping"
    if mapping.state is not MappingState.CONFIRMED:
        return "competitor lot mapping requires revalidation"
    if mapping.service_code != service_code:
        return "confirmed mapping points to another service code"
    if not observation.is_valid:
        return "observation is invalid"
    if observation.currency != policy.currency:
        return "observation currency does not match policy currency"
    if not _is_valid_price(observation.price_minor):
        return "observation price is not a positive integer minor-unit value"
    return None


def _decision(
    own_lot: OwnLotPriceState, observations: tuple[TrustedPriceObservation, ...],
    excluded: tuple[ExcludedObservation, ...], minimum: int | None, percent_99: int | None, rounded: int | None,
    hard_floor: int | None, final_target: int, action: PriceAction, reason: str,
) -> PriceDecision:
    return PriceDecision(
        own_lot.service_code, own_lot.current_price_minor, observations, excluded, minimum, percent_99, rounded,
        hard_floor, final_target, action, reason,
    )


def _validate_own_lot(own_lot: OwnLotPriceState, policy: PricePolicy) -> None:
    if not isinstance(own_lot.service_code, str) or not own_lot.service_code.strip():
        raise ValueError("service_code is required")
    _valid_price(own_lot.current_price_minor, "current_price_minor")
    if own_lot.currency != policy.currency:
        raise ValueError("own lot currency does not match policy currency")
    if own_lot.mode is not OwnLotPricingMode.FIXED_PRICE and own_lot.fixed_price_minor is not None:
        raise ValueError("fixed_price_minor is only allowed in fixed_price mode")


def _is_valid_price(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_price(value: object, name: str) -> None:
    if not _is_valid_price(value):
        raise ValueError(f"{name} must be a positive integer minor-unit value")
