from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.lot_writes import CapabilityState, LotWriteOutcome
from funpay_operations.price_safety import PriceObservationRecord, SafetyValidatedPricingEngine
from funpay_operations.price_transactions import (
    ManagedPriceLot,
    MockCompetitorObservationAdapter,
    MockOwnLotPriceAdapter,
    PriceSnapshotRepository,
    PriceUpdateCoordinator,
)
from funpay_operations.pricing import OwnLotPriceState, OwnLotPricingMode, PricePolicy, TrustedPriceObservation
from funpay_operations.raise_transactions import (
    FixedRaiseCooldown,
    MockRaiseCapabilityClient,
    RaiseAttemptRepository,
    RaiseAvailability,
    RaiseCoordinator,
    RaiseResultStatus,
)
from funpay_operations.trusted_sellers import (
    CompetitorLotMapping,
    MappingState,
    SellerFamily,
    SellerLastCheckedState,
    SellerVerificationState,
    TrustedSeller,
)


NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


class RaiseCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "raise.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_successful_path_runs_fresh_pricing_verification_then_raise(self) -> None:
        coordinator, source, _, client, attempts = _coordinator(self.database)
        result = coordinator.run("scheduled-1", now=NOW)
        self.assertEqual(source.calls, 1)
        self.assertEqual([item.status for item in result.families], [RaiseResultStatus.COMPLETED])
        self.assertEqual([item[0] for item in client.calls], list(SellerFamily))
        self.assertEqual(attempts.last(SellerFamily.MYTHIC_PLUS).last_result, RaiseResultStatus.COMPLETED)

    def test_pricing_verification_failure_blocks_raise(self) -> None:
        coordinator, _, _, client, attempts = _coordinator(self.database, stale_mplus=2)
        result = coordinator.run("scheduled-2", now=NOW)
        self.assertEqual(result.families[0].status, RaiseResultStatus.BLOCKED)
        self.assertEqual(client.calls, [])
        self.assertIn("verification mismatch", attempts.last(SellerFamily.MYTHIC_PLUS).failure_reason)

    def test_unsupported_raise_is_not_reported_as_success(self) -> None:
        coordinator, _, _, client, _ = _coordinator(self.database, capability_state=CapabilityState.UNSUPPORTED)
        result = coordinator.run("scheduled-3", now=NOW)
        self.assertEqual([item.status for item in result.families], [RaiseResultStatus.UNSUPPORTED])
        self.assertEqual(client.calls, [])

    def test_unavailable_raise_is_not_called(self) -> None:
        coordinator, _, _, client, _ = _coordinator(
            self.database, availability=RaiseAvailability.UNAVAILABLE
        )
        result = coordinator.run("scheduled-unavailable", now=NOW)
        self.assertEqual([item.status for item in result.families], [RaiseResultStatus.UNAVAILABLE])
        self.assertEqual(client.calls, [])

    def test_non_mock_raise_adapter_is_rejected_before_any_run(self) -> None:
        class NonMockRaiseClient(MockRaiseCapabilityClient):
            mock_only = False

        coordinator, _, _, _, _ = _coordinator(self.database)
        with self.assertRaisesRegex(ValueError, "mock-only dependencies"):
            RaiseCoordinator(
                price_coordinator=coordinator.price_coordinator, snapshots=coordinator.snapshots,
                raise_client=NonMockRaiseClient(), attempts=coordinator.attempts,
            )

    def test_capability_cooldown_is_recorded_without_raise_call(self) -> None:
        next_allowed = NOW + timedelta(hours=2)
        coordinator, _, _, client, attempts = _coordinator(
            self.database, availability=RaiseAvailability.COOLDOWN, next_allowed_at=next_allowed
        )
        result = coordinator.run("scheduled-4", now=NOW)
        self.assertEqual(result.families[0].status, RaiseResultStatus.COOLDOWN)
        self.assertEqual(result.families[0].next_allowed_at, next_allowed)
        self.assertEqual(attempts.last(SellerFamily.MYTHIC_PLUS).next_allowed_at, next_allowed)
        self.assertEqual(client.calls, [])

    def test_failed_raise_is_persisted(self) -> None:
        coordinator, _, _, client, attempts = _coordinator(self.database, outcome=LotWriteOutcome.FAILED)
        result = coordinator.run("scheduled-5", now=NOW)
        self.assertEqual(result.families[0].status, RaiseResultStatus.FAILED)
        self.assertEqual(attempts.last(SellerFamily.MYTHIC_PLUS).last_result, RaiseResultStatus.FAILED)
        self.assertEqual(len(client.calls), 1)

    def test_local_cooldown_and_duplicate_schedule_prevent_second_raise(self) -> None:
        coordinator, _, _, client, _ = _coordinator(self.database, cooldown=FixedRaiseCooldown(timedelta(hours=1)))
        first = coordinator.run("scheduled-6", now=NOW)
        second = coordinator.run("scheduled-6", now=NOW + timedelta(minutes=1))
        third = coordinator.run("scheduled-7", now=NOW + timedelta(minutes=2))
        self.assertEqual([item.status for item in first.families], [RaiseResultStatus.COMPLETED])
        self.assertEqual([item.status for item in second.families], [RaiseResultStatus.DUPLICATE])
        self.assertEqual([item.status for item in third.families], [RaiseResultStatus.COOLDOWN])
        self.assertEqual(len(client.calls), 1)


def _coordinator(
    database: Database, *, stale_mplus: int = 0, capability_state: CapabilityState = CapabilityState.SUPPORTED,
    availability: RaiseAvailability = RaiseAvailability.AVAILABLE, next_allowed_at: datetime | None = None,
    outcome: LotWriteOutcome = LotWriteOutcome.SUCCEEDED, cooldown: FixedRaiseCooldown | None = None,
):
    lots = (
        ManagedPriceLot("mplus-lot", SellerFamily.MYTHIC_PLUS, _state("mplus"), True),
    )
    source = MockCompetitorObservationAdapter(_records("mplus", "a", "b"))
    own_prices = MockOwnLotPriceAdapter(
        {"mplus-lot": 11_000},
        stale_write_attempts={"mplus-lot": stale_mplus} if stale_mplus else {},
    )
    sellers = (
        _seller("a", SellerFamily.MYTHIC_PLUS), _seller("b", SellerFamily.MYTHIC_PLUS),
    )
    mappings = tuple(_mapping(seller_id, "mplus") for seller_id in ("a", "b"))
    policies = {"mplus": PricePolicy(hard_floor=1_000, price_step_minor=100, currency="RUB")}
    snapshots = PriceSnapshotRepository(database)
    prices = PriceUpdateCoordinator(
        observation_adapter=source, own_price_adapter=own_prices, safety_engine=SafetyValidatedPricingEngine(),
        snapshots=snapshots, lots=lots, sellers=sellers, mappings=mappings, history=(), policies=policies,
    )
    client = MockRaiseCapabilityClient(capability_state, availability, next_allowed_at, outcome)
    attempts = RaiseAttemptRepository(database)
    return (
        RaiseCoordinator(price_coordinator=prices, snapshots=snapshots, raise_client=client, attempts=attempts, cooldown=cooldown),
        source, own_prices, client, attempts,
    )


def _state(service_code: str) -> OwnLotPriceState:
    return OwnLotPriceState(service_code, 11_000, "RUB", OwnLotPricingMode.AUTOMATIC)


def _seller(seller_id: str, family: SellerFamily) -> TrustedSeller:
    return TrustedSeller(seller_id, seller_id, family, True, SellerVerificationState.VERIFIED, SellerLastCheckedState.CURRENT)


def _mapping(seller_id: str, service_code: str) -> CompetitorLotMapping:
    return CompetitorLotMapping(seller_id, f"lot-{seller_id}", service_code, MappingState.CONFIRMED, f"hash-{seller_id}")


def _records(service_code: str, first: str, second: str) -> tuple[PriceObservationRecord, ...]:
    return tuple(
        PriceObservationRecord(
            f"obs-{seller_id}-{service_code}",
            TrustedPriceObservation(seller_id, f"lot-{seller_id}", service_code, 10_000 + index * 100, "RUB"),
            f"hash-{seller_id}", "stable", 1,
        )
        for index, seller_id in enumerate((first, second))
    )
