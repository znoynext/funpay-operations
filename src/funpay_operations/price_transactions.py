"""Mock-only, verified price-update transactions and local rollback snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Mapping, Protocol, TextIO
from uuid import uuid4

from .database import Database
from .price_safety import PriceObservationRecord, SafetyDecisionStatus, SafetyPriceDecision, SafetyValidatedPricingEngine
from .pricing import OwnLotPriceState, PriceAction, PricePolicy
from .trusted_sellers import CompetitorLotMapping, SellerFamily, TrustedSeller


class TransactionMode(StrEnum):
    CHECK = "check"
    DRY_RUN = "dry_run"
    EXECUTE = "execute"
    ROLLBACK_PREVIEW = "rollback_preview"
    ROLLBACK = "rollback"


class FamilyBatchStatus(StrEnum):
    CHECKED = "checked"
    DRY_RUN = "dry_run"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    ROLLBACK_PREVIEW = "rollback_preview"
    ROLLED_BACK = "rolled_back"
    NO_SNAPSHOT = "no_snapshot"


@dataclass(frozen=True)
class ManagedPriceLot:
    lot_id: str
    family: SellerFamily
    price_state: OwnLotPriceState


@dataclass(frozen=True)
class PriceSnapshotItem:
    lot_id: str
    service_code: str
    price_minor: int
    currency: str


@dataclass(frozen=True)
class PriceSnapshot:
    batch_id: str
    family: SellerFamily
    items: tuple[PriceSnapshotItem, ...]


@dataclass(frozen=True)
class LotTransactionResult:
    lot_id: str
    service_code: str
    old_price_minor: int
    target_price_minor: int
    successful: bool
    reason: str


@dataclass(frozen=True)
class FamilyPriceBatchResult:
    family: SellerFamily
    status: FamilyBatchStatus
    decisions: tuple[SafetyPriceDecision, ...]
    lot_results: tuple[LotTransactionResult, ...]
    reason: str


@dataclass(frozen=True)
class PriceTransactionResult:
    mode: TransactionMode
    fetched_observations: int
    batches: tuple[FamilyPriceBatchResult, ...]


class CompetitorObservationAdapter(Protocol):
    mock_only: bool
    def fetch_competitor_observations(self) -> tuple[PriceObservationRecord, ...]: ...


class OwnLotPriceAdapter(Protocol):
    mock_only: bool
    def read_own_prices(self, lot_ids: tuple[str, ...]) -> Mapping[str, int]: ...
    def update_price(self, lot_id: str, price_minor: int) -> bool: ...


@dataclass
class MockCompetitorObservationAdapter:
    mock_only: ClassVar[bool] = True
    observations: tuple[PriceObservationRecord, ...] = ()
    calls: int = 0

    def fetch_competitor_observations(self) -> tuple[PriceObservationRecord, ...]:
        self.calls += 1
        return self.observations


@dataclass
class MockOwnLotPriceAdapter:
    mock_only: ClassVar[bool] = True
    prices: dict[str, int]
    write_failures: set[str] = field(default_factory=set)
    stale_write_attempts: dict[str, int] = field(default_factory=dict)
    read_calls: int = 0
    write_calls: list[tuple[str, int]] = field(default_factory=list)

    def read_own_prices(self, lot_ids: tuple[str, ...]) -> Mapping[str, int]:
        self.read_calls += 1
        return {lot_id: self.prices[lot_id] for lot_id in lot_ids if lot_id in self.prices}

    def update_price(self, lot_id: str, price_minor: int) -> bool:
        self.write_calls.append((lot_id, price_minor))
        if lot_id in self.write_failures or lot_id not in self.prices:
            return False
        remaining = self.stale_write_attempts.get(lot_id, 0)
        if remaining:
            self.stale_write_attempts[lot_id] = remaining - 1
            return True
        self.prices[lot_id] = price_minor
        return True


class PriceSnapshotRepository:
    """Local durable snapshots; it stores no competitor text or credentials."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_pending(self, batch_id: str, family: SellerFamily, items: tuple[PriceSnapshotItem, ...]) -> None:
        if not batch_id or not items or len({item.lot_id for item in items}) != len(items):
            raise ValueError("snapshot requires a batch id and unique items")
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO price_transaction_batches (batch_id, family, status) VALUES (?, ?, 'pending')",
                (batch_id, family.value),
            )
            connection.executemany(
                """INSERT INTO price_transaction_snapshot_items
                (batch_id, lot_id, service_code, price_minor, currency) VALUES (?, ?, ?, ?, ?)""",
                [(batch_id, item.lot_id, item.service_code, item.price_minor, item.currency) for item in items],
            )

    def mark(self, batch_id: str, status: FamilyBatchStatus, reason: str | None = None) -> None:
        database_status = {FamilyBatchStatus.COMPLETED: "completed", FamilyBatchStatus.FAILED: "failed", FamilyBatchStatus.ROLLED_BACK: "rolled_back"}.get(status)
        if database_status is None:
            raise ValueError("unsupported stored batch status")
        with self.database.session() as connection:
            if connection.execute(
                "UPDATE price_transaction_batches SET status = ?, error_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                (database_status, reason, batch_id),
            ).rowcount != 1:
                raise KeyError("snapshot batch does not exist")

    def latest_completed(self, family: SellerFamily) -> PriceSnapshot | None:
        with self.database.session() as connection:
            batch = connection.execute(
                """SELECT batch_id FROM price_transaction_batches
                WHERE family = ? AND status = 'completed' ORDER BY rowid DESC LIMIT 1""", (family.value,)
            ).fetchone()
            if batch is None:
                return None
            rows = connection.execute(
                "SELECT lot_id, service_code, price_minor, currency FROM price_transaction_snapshot_items WHERE batch_id = ? ORDER BY lot_id",
                (batch["batch_id"],),
            ).fetchall()
        return PriceSnapshot(batch["batch_id"], family, tuple(
            PriceSnapshotItem(row["lot_id"], row["service_code"], int(row["price_minor"]), row["currency"]) for row in rows
        ))

    def mark_unsafe_for_raise(self, family: SellerFamily, reason: str) -> None:
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO unsafe_for_raise_families (family, error_reason) VALUES (?, ?)
                ON CONFLICT(family) DO UPDATE SET error_reason = excluded.error_reason, updated_at = CURRENT_TIMESTAMP""",
                (family.value, reason),
            )

    def unsafe_reason(self, family: SellerFamily) -> str | None:
        with self.database.session() as connection:
            row = connection.execute("SELECT error_reason FROM unsafe_for_raise_families WHERE family = ?", (family.value,)).fetchone()
        return row["error_reason"] if row else None


class PriceUpdateCoordinator:
    """Implements snapshot -> write -> reread -> verify with one retry, on mocks only."""

    def __init__(
        self, *, observation_adapter: CompetitorObservationAdapter, own_price_adapter: OwnLotPriceAdapter,
        safety_engine: SafetyValidatedPricingEngine, snapshots: PriceSnapshotRepository,
        lots: tuple[ManagedPriceLot, ...], sellers: tuple[TrustedSeller, ...], mappings: tuple[CompetitorLotMapping, ...],
        history: tuple[PriceObservationRecord, ...], policies: Mapping[str, PricePolicy],
    ) -> None:
        if not observation_adapter.mock_only or not own_price_adapter.mock_only:
            raise ValueError("PriceUpdateCoordinator accepts mock adapters only in this release")
        self.observation_adapter, self.own_price_adapter = observation_adapter, own_price_adapter
        self.safety_engine, self.snapshots = safety_engine, snapshots
        self.lots, self.sellers, self.mappings, self.history, self.policies = lots, sellers, mappings, history, policies

    def run(self, mode: TransactionMode) -> PriceTransactionResult:
        if mode not in {TransactionMode.CHECK, TransactionMode.DRY_RUN, TransactionMode.EXECUTE}:
            raise ValueError("run supports check, dry_run, or execute")
        observations = self.observation_adapter.fetch_competitor_observations()
        batches = tuple(self._run_family(family, observations, mode) for family in SellerFamily)
        return PriceTransactionResult(mode, len(observations), batches)

    def rollback(self, family: SellerFamily, mode: TransactionMode) -> FamilyPriceBatchResult:
        if mode not in {TransactionMode.ROLLBACK_PREVIEW, TransactionMode.ROLLBACK}:
            raise ValueError("rollback requires rollback_preview or rollback")
        snapshot = self.snapshots.latest_completed(family)
        if snapshot is None:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.NO_SNAPSHOT, (), (), "no completed snapshot")
        current = self.own_price_adapter.read_own_prices(tuple(item.lot_id for item in snapshot.items))
        if any(item.lot_id not in current for item in snapshot.items):
            reason = "own lot is missing during rollback"
            self.snapshots.mark_unsafe_for_raise(family, reason)
            return FamilyPriceBatchResult(family, FamilyBatchStatus.FAILED, (), (), reason)
        planned = tuple(item for item in snapshot.items if current.get(item.lot_id) != item.price_minor)
        preview = tuple(LotTransactionResult(item.lot_id, item.service_code, current.get(item.lot_id, 0), item.price_minor, False, "rollback preview") for item in planned)
        if mode is TransactionMode.ROLLBACK_PREVIEW:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.ROLLBACK_PREVIEW, (), preview, "no writes in rollback preview")
        outcomes = self._write_verify(tuple((item.lot_id, item.service_code, current[item.lot_id], item.price_minor) for item in planned))
        if any(not item.successful for item in outcomes):
            reason = next(item.reason for item in outcomes if not item.successful)
            self.snapshots.mark_unsafe_for_raise(family, reason)
            return FamilyPriceBatchResult(family, FamilyBatchStatus.FAILED, (), outcomes, reason)
        self.snapshots.mark(snapshot.batch_id, FamilyBatchStatus.ROLLED_BACK)
        return FamilyPriceBatchResult(family, FamilyBatchStatus.ROLLED_BACK, (), outcomes, "rollback verified")

    def _run_family(self, family: SellerFamily, observations: tuple[PriceObservationRecord, ...], mode: TransactionMode) -> FamilyPriceBatchResult:
        lots = tuple(item for item in self.lots if item.family is family)
        if not lots:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.CHECKED, (), (), "no managed lots")
        decisions, batch_safety = self.safety_engine.batch_preview(
            tuple(item.price_state for item in lots), sellers=self.sellers, mappings=self.mappings,
            records=observations, history=self.history, policies={item.price_state.service_code: self.policies[item.price_state.service_code] for item in lots},
        )
        if mode is TransactionMode.CHECK:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.CHECKED, decisions, (), "check completed without writes")
        if batch_safety.status is SafetyDecisionStatus.REJECTED:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.BLOCKED, decisions, (), batch_safety.reason)
        targets = tuple(
            (lot.lot_id, lot.price_state.service_code, lot.price_state.current_price_minor, decision.price_decision.final_target_minor)
            for lot, decision in zip(sorted(lots, key=lambda item: item.price_state.service_code), decisions)
            if decision.price_decision.action is PriceAction.UPDATE_PRICE
            and decision.price_decision.final_target_minor != lot.price_state.current_price_minor
        )
        if mode is TransactionMode.DRY_RUN:
            planned = tuple(LotTransactionResult(lot_id, service, current, target, False, "dry-run; no write") for lot_id, service, current, target in targets)
            return FamilyPriceBatchResult(family, FamilyBatchStatus.DRY_RUN, decisions, planned, "dry-run completed without writes")
        actual = self.own_price_adapter.read_own_prices(tuple(item[0] for item in targets))
        if any(actual.get(lot_id) != current for lot_id, _, current, _ in targets):
            reason = "own price changed since planning"
            self.snapshots.mark_unsafe_for_raise(family, reason)
            return FamilyPriceBatchResult(family, FamilyBatchStatus.FAILED, decisions, (), reason)
        if not targets:
            return FamilyPriceBatchResult(family, FamilyBatchStatus.COMPLETED, decisions, (), "no differing prices require writes")
        batch_id = uuid4().hex
        snapshot_items = tuple(PriceSnapshotItem(lot_id, service, current, self.policies[service].currency) for lot_id, service, current, _ in targets)
        self.snapshots.create_pending(batch_id, family, snapshot_items)
        outcomes = self._write_verify(targets)
        if any(not item.successful for item in outcomes):
            reason = next(item.reason for item in outcomes if not item.successful)
            self.snapshots.mark(batch_id, FamilyBatchStatus.FAILED, reason)
            self.snapshots.mark_unsafe_for_raise(family, reason)
            return FamilyPriceBatchResult(family, FamilyBatchStatus.FAILED, decisions, outcomes, reason)
        self.snapshots.mark(batch_id, FamilyBatchStatus.COMPLETED)
        return FamilyPriceBatchResult(family, FamilyBatchStatus.COMPLETED, decisions, outcomes, "writes reread and verified")

    def _write_verify(self, targets: tuple[tuple[str, str, int, int], ...]) -> tuple[LotTransactionResult, ...]:
        failed: dict[str, str] = {}
        for lot_id, _, _, target in targets:
            if not self.own_price_adapter.update_price(lot_id, target):
                failed[lot_id] = "write rejected"
        first_read = self.own_price_adapter.read_own_prices(tuple(item[0] for item in targets))
        for lot_id, _, _, target in targets:
            if lot_id not in failed and first_read.get(lot_id) != target:
                if not self.own_price_adapter.update_price(lot_id, target):
                    failed[lot_id] = "retry write rejected"
        final_read = self.own_price_adapter.read_own_prices(tuple(item[0] for item in targets))
        return tuple(
            LotTransactionResult(
                lot_id, service, old, target, lot_id not in failed and final_read.get(lot_id) == target,
                failed.get(lot_id, "verified" if final_read.get(lot_id) == target else "verification mismatch after retry"),
            )
            for lot_id, service, old, target in targets
        )


def run_price_transaction_command(action: str, *, database: Database, output: TextIO) -> int:
    """CLI-only empty mock composition. It never reads a session or sends a network request."""
    database.initialize()
    coordinator = PriceUpdateCoordinator(
        observation_adapter=MockCompetitorObservationAdapter(), own_price_adapter=MockOwnLotPriceAdapter({}),
        safety_engine=SafetyValidatedPricingEngine(), snapshots=PriceSnapshotRepository(database), lots=(),
        sellers=(), mappings=(), history=(), policies={},
    )
    if action == "check":
        result = coordinator.run(TransactionMode.CHECK)
        print(f"prices check: observations={result.fetched_observations} mythic_plus=checked delves=checked", file=output)
        return 0
    if action == "dry-run-update":
        result = coordinator.run(TransactionMode.DRY_RUN)
        print(f"prices dry-run-update: observations={result.fetched_observations} writes=0", file=output)
        return 0
    if action == "rollback-preview":
        results = tuple(coordinator.rollback(family, TransactionMode.ROLLBACK_PREVIEW) for family in SellerFamily)
        print(f"prices rollback-preview: mythic_plus={results[0].status.value} delves={results[1].status.value} writes=0", file=output)
        return 0
    raise ValueError("unsupported price transaction action")
