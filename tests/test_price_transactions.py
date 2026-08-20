from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.price_safety import PriceObservationRecord, SafetyValidatedPricingEngine
from funpay_operations.price_transactions import (
    FamilyBatchStatus,
    ManagedPriceLot,
    MockCompetitorObservationAdapter,
    MockOwnLotPriceAdapter,
    PriceSnapshotRepository,
    PriceUpdateCoordinator,
    TransactionMode,
    run_price_transaction_command,
)
from funpay_operations.pricing import OwnLotPriceState, OwnLotPricingMode, PricePolicy, TrustedPriceObservation
from funpay_operations.trusted_sellers import (
    CompetitorLotMapping,
    MappingState,
    SellerFamily,
    SellerLastCheckedState,
    SellerVerificationState,
    TrustedSeller,
)


class PriceTransactionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "transactions.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_execute_fetches_snapshots_writes_only_differences_and_verifies(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000})
        coordinator, source, snapshots = _coordinator(self.database, adapter)
        result = coordinator.run(TransactionMode.EXECUTE)
        (mythic,) = result.batches
        self.assertEqual(source.calls, 1)
        self.assertEqual(mythic.status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(adapter.write_calls, [("mplus-lot", 9_900)])
        self.assertEqual(adapter.prices["mplus-lot"], 9_900)
        self.assertIsNotNone(snapshots.latest_completed(SellerFamily.MYTHIC_PLUS))
        self.assertTrue(all(item.successful for item in mythic.lot_results))

    def test_verification_mismatch_retries_once_then_succeeds(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000}, stale_write_attempts={"mplus-lot": 1})
        coordinator, _, _ = _coordinator(self.database, adapter)
        batch = coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertEqual(batch.status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(adapter.write_calls, [("mplus-lot", 9_900), ("mplus-lot", 9_900)])
        self.assertTrue(batch.lot_results[0].successful)

    def test_verified_price_is_used_by_the_next_fresh_transaction_cycle(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000})
        coordinator, _, _ = _coordinator(self.database, adapter)
        self.assertEqual(coordinator.run(TransactionMode.EXECUTE).batches[0].status, FamilyBatchStatus.COMPLETED)
        repeated = coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertEqual(repeated.status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(adapter.write_calls, [("mplus-lot", 9_900)])

    def test_persistent_verification_mismatch_fails_and_marks_family_unsafe(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000}, stale_write_attempts={"mplus-lot": 2})
        coordinator, _, snapshots = _coordinator(self.database, adapter)
        batch = coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertEqual(batch.status, FamilyBatchStatus.FAILED)
        self.assertFalse(batch.lot_results[0].successful)
        self.assertIn("verification mismatch", snapshots.unsafe_reason(SellerFamily.MYTHIC_PLUS))
        self.assertIsNone(snapshots.latest_completed(SellerFamily.MYTHIC_PLUS))

    def test_rejected_write_fails_the_lot_and_marks_family_unsafe(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000}, write_failures={"mplus-lot"})
        coordinator, _, snapshots = _coordinator(self.database, adapter)
        batch = coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertEqual(batch.status, FamilyBatchStatus.FAILED)
        self.assertEqual(batch.lot_results[0].reason, "write rejected")
        self.assertFalse(batch.lot_results[0].successful)
        self.assertEqual(snapshots.unsafe_reason(SellerFamily.MYTHIC_PLUS), "write rejected")

    def test_unconfirmed_own_identity_blocks_price_write(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000})
        coordinator, _, _ = _coordinator(self.database, adapter)
        coordinator.lots = tuple(
            ManagedPriceLot(item.lot_id, item.family, item.price_state, False) for item in coordinator.lots
        )
        batch = coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertEqual(batch.status, FamilyBatchStatus.BLOCKED)
        self.assertEqual(adapter.write_calls, [])

    def test_dry_run_and_check_never_call_mock_writer(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000})
        coordinator, _, _ = _coordinator(self.database, adapter)
        self.assertEqual(coordinator.run(TransactionMode.CHECK).batches[0].status, FamilyBatchStatus.CHECKED)
        self.assertEqual(coordinator.run(TransactionMode.DRY_RUN).batches[0].status, FamilyBatchStatus.DRY_RUN)
        self.assertEqual(adapter.write_calls, [])

    def test_rollback_restores_last_completed_snapshot_and_verifies(self) -> None:
        adapter = MockOwnLotPriceAdapter({"mplus-lot": 11_000})
        coordinator, _, snapshots = _coordinator(self.database, adapter)
        self.assertEqual(coordinator.run(TransactionMode.EXECUTE).batches[0].status, FamilyBatchStatus.COMPLETED)
        preview = coordinator.rollback(SellerFamily.MYTHIC_PLUS, TransactionMode.ROLLBACK_PREVIEW)
        self.assertEqual(preview.status, FamilyBatchStatus.ROLLBACK_PREVIEW)
        self.assertEqual(adapter.prices["mplus-lot"], 9_900)
        restored = coordinator.rollback(SellerFamily.MYTHIC_PLUS, TransactionMode.ROLLBACK)
        self.assertEqual(restored.status, FamilyBatchStatus.ROLLED_BACK)
        self.assertEqual(adapter.prices["mplus-lot"], 11_000)
        self.assertIsNone(snapshots.latest_completed(SellerFamily.MYTHIC_PLUS))

    def test_cli_commands_are_empty_mock_previews_only(self) -> None:
        output = StringIO()
        self.assertEqual(run_price_transaction_command("check", database=self.database, output=output), 0)
        self.assertIn("observations=0", output.getvalue())
        output = StringIO()
        self.assertEqual(run_price_transaction_command("dry-run-update", database=self.database, output=output), 0)
        self.assertIn("writes=0", output.getvalue())
        output = StringIO()
        self.assertEqual(run_price_transaction_command("rollback-preview", database=self.database, output=output), 0)
        self.assertIn("writes=0", output.getvalue())

    def test_coordinator_rejects_non_mock_adapter_before_any_transaction(self) -> None:
        class NonMockObservationAdapter(MockCompetitorObservationAdapter):
            mock_only = False
        with self.assertRaisesRegex(ValueError, "mock adapters only"):
            PriceUpdateCoordinator(
                observation_adapter=NonMockObservationAdapter(), own_price_adapter=MockOwnLotPriceAdapter({}),
                safety_engine=SafetyValidatedPricingEngine(), snapshots=PriceSnapshotRepository(self.database), lots=(),
                sellers=(), mappings=(), history=(), policies={},
            )


def _coordinator(database: Database, adapter: MockOwnLotPriceAdapter):
    lots = [ManagedPriceLot("mplus-lot", SellerFamily.MYTHIC_PLUS, _price_state("mplus"), True)]
    records = list(_records("mplus", "a", "b"))
    sellers = [_seller("a", SellerFamily.MYTHIC_PLUS), _seller("b", SellerFamily.MYTHIC_PLUS)]
    mappings = [_mapping("a", "mplus"), _mapping("b", "mplus")]
    policies = {"mplus": PricePolicy(hard_floor=1_000, price_step_minor=100, currency="RUB")}
    source = MockCompetitorObservationAdapter(tuple(records))
    snapshots = PriceSnapshotRepository(database)
    return (
        PriceUpdateCoordinator(
            observation_adapter=source, own_price_adapter=adapter, safety_engine=SafetyValidatedPricingEngine(), snapshots=snapshots,
            lots=tuple(lots), sellers=tuple(sellers), mappings=tuple(mappings), history=(), policies=policies,
        ), source, snapshots,
    )


def _price_state(service_code: str) -> OwnLotPriceState:
    return OwnLotPriceState(service_code, 11_000, "RUB", OwnLotPricingMode.AUTOMATIC)


def _seller(seller_id: str, family: SellerFamily) -> TrustedSeller:
    return TrustedSeller(seller_id, f"mock-{seller_id}", family, True, SellerVerificationState.VERIFIED, SellerLastCheckedState.CURRENT)


def _mapping(seller_id: str, service_code: str) -> CompetitorLotMapping:
    return CompetitorLotMapping(seller_id, f"lot-{seller_id}", service_code, MappingState.CONFIRMED, f"hash-{seller_id}")


def _records(service_code: str, *seller_ids: str) -> tuple[PriceObservationRecord, ...]:
    return tuple(
        PriceObservationRecord(
            f"obs-{seller_id}-{service_code}",
            TrustedPriceObservation(seller_id, f"lot-{seller_id}", service_code, 10_000 + index * 100, "RUB"),
            f"hash-{seller_id}", "stable", 1,
        )
        for index, seller_id in enumerate(seller_ids)
    )
