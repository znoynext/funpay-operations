"""Mock-only, capability-gated raise coordination.

No production raise adapter is composed here. The coordinator rejects an
adapter that is not explicitly marked mock-only, so it cannot use a FunPay
session or send a network request in this release.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Protocol

from .database import Database
from .lot_writes import CapabilityState, LotWriteOutcome
from .price_transactions import (
    FamilyPriceBatchResult,
    FamilyBatchStatus,
    PriceSnapshotRepository,
    PriceTransactionResult,
    PriceUpdateCoordinator,
    TransactionMode,
)
from .trusted_sellers import SellerFamily


class RaiseResultStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    COOLDOWN = "cooldown"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class RaiseAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    COOLDOWN = "cooldown"


@dataclass(frozen=True)
class RaiseCapabilityInfo:
    state: CapabilityState
    availability: RaiseAvailability
    reason: str
    next_allowed_at: datetime | None = None


@dataclass(frozen=True)
class RaiseCapabilityResult:
    outcome: LotWriteOutcome
    detail: str


class RaiseCapabilityClient(Protocol):
    """Narrow capability interface; no per-lot FunPay contract is assumed."""

    mock_only: bool

    def raise_capability(self, family: SellerFamily) -> RaiseCapabilityInfo: ...

    def raise_family(self, family: SellerFamily, *, operation_key: str) -> RaiseCapabilityResult: ...


@dataclass
class MockRaiseCapabilityClient:
    """In-memory capability double for CI; it has no network boundary."""

    mock_only: ClassVar[bool] = True
    capability_state: CapabilityState = CapabilityState.SUPPORTED
    availability: RaiseAvailability = RaiseAvailability.AVAILABLE
    next_allowed_at: datetime | None = None
    outcome: LotWriteOutcome = LotWriteOutcome.SUCCEEDED
    detail: str = "mock raise result"
    calls: list[tuple[SellerFamily, str]] = field(default_factory=list)

    def raise_capability(self, family: SellerFamily) -> RaiseCapabilityInfo:
        return RaiseCapabilityInfo(self.capability_state, self.availability, self.detail, self.next_allowed_at)

    def raise_family(self, family: SellerFamily, *, operation_key: str) -> RaiseCapabilityResult:
        self.calls.append((family, operation_key))
        return RaiseCapabilityResult(self.outcome, self.detail)


@dataclass(frozen=True)
class RaiseAttemptState:
    operation_key: str
    family: SellerFamily
    last_attempt: datetime
    last_result: RaiseResultStatus
    next_allowed_at: datetime | None
    failure_reason: str | None


class RaiseAttemptRepository:
    """Durable local idempotency and last-attempt ledger; it stores no account data."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def reserve(self, family: SellerFamily, operation_key: str, attempted_at: datetime) -> bool:
        try:
            with self.database.session() as connection:
                connection.execute(
                    """INSERT INTO raise_attempts (operation_key, family, attempted_at, result)
                    VALUES (?, ?, ?, 'scheduled')""",
                    (operation_key, family.value, _stored_time(attempted_at)),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def finish(
        self, operation_key: str, result: RaiseResultStatus, *, next_allowed_at: datetime | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if result is RaiseResultStatus.DUPLICATE:
            raise ValueError("duplicate attempts do not replace the original result")
        with self.database.session() as connection:
            if connection.execute(
                """UPDATE raise_attempts SET result = ?, next_allowed_at = ?, failure_reason = ?
                WHERE operation_key = ?""",
                (result.value, _stored_time(next_allowed_at) if next_allowed_at else None, failure_reason, operation_key),
            ).rowcount != 1:
                raise KeyError("raise attempt does not exist")

    def last(self, family: SellerFamily) -> RaiseAttemptState | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM raise_attempts WHERE family = ? ORDER BY rowid DESC LIMIT 1", (family.value,)
            ).fetchone()
        if row is None:
            return None
        return RaiseAttemptState(
            row["operation_key"], SellerFamily(row["family"]), _read_time(row["attempted_at"]),
            RaiseResultStatus(row["result"]), _read_time(row["next_allowed_at"]) if row["next_allowed_at"] else None,
            row["failure_reason"],
        )


@dataclass(frozen=True)
class CooldownDecision:
    allowed: bool
    next_allowed_at: datetime | None
    reason: str


class RaiseCooldownPolicy(Protocol):
    def check(self, last_attempt: RaiseAttemptState | None, now: datetime) -> CooldownDecision: ...

    def after_success(self, now: datetime) -> datetime | None: ...


@dataclass(frozen=True)
class FixedRaiseCooldown:
    """Local cooldown abstraction; it never claims FunPay supplied the limit."""

    duration: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.duration < timedelta(0):
            raise ValueError("cooldown duration must not be negative")

    def check(self, last_attempt: RaiseAttemptState | None, now: datetime) -> CooldownDecision:
        if last_attempt and last_attempt.next_allowed_at and now < last_attempt.next_allowed_at:
            return CooldownDecision(False, last_attempt.next_allowed_at, "local raise cooldown is active")
        return CooldownDecision(True, None, "local cooldown allows raise")

    def after_success(self, now: datetime) -> datetime | None:
        return now + self.duration if self.duration else None


@dataclass(frozen=True)
class FamilyRaiseResult:
    family: SellerFamily
    status: RaiseResultStatus
    operation_key: str
    last_attempt: datetime
    next_allowed_at: datetime | None
    failure_reason: str | None


@dataclass(frozen=True)
class RaiseRunResult:
    price_transaction: PriceTransactionResult
    families: tuple[FamilyRaiseResult, ...]


class RaiseCoordinator:
    """Runs fresh mock price verification before an independent family raise attempt."""

    mock_only: ClassVar[bool] = True

    def __init__(
        self, *, price_coordinator: PriceUpdateCoordinator, snapshots: PriceSnapshotRepository,
        raise_client: RaiseCapabilityClient, attempts: RaiseAttemptRepository,
        cooldown: RaiseCooldownPolicy | None = None,
    ) -> None:
        if not getattr(price_coordinator, "mock_only", False) or not raise_client.mock_only:
            raise ValueError("RaiseCoordinator accepts mock-only dependencies in this release")
        self.price_coordinator, self.snapshots = price_coordinator, snapshots
        self.raise_client, self.attempts = raise_client, attempts
        self.cooldown = cooldown or FixedRaiseCooldown()

    def run(self, schedule_key: str, *, now: datetime | None = None) -> RaiseRunResult:
        if not isinstance(schedule_key, str) or not schedule_key.strip():
            raise ValueError("schedule_key is required")
        current_time = _utc(now or datetime.now(UTC))
        # This call fetches fresh observations and performs mapping, anomaly, pricing,
        # price-write, reread, and verification before either family can be raised.
        price_transaction = self.price_coordinator.run(TransactionMode.EXECUTE)
        batches = {batch.family: batch for batch in price_transaction.batches}
        results = tuple(
            self._raise_family(family, batches[family], schedule_key.strip(), current_time)
            for family in SellerFamily
        )
        return RaiseRunResult(price_transaction, results)

    def _raise_family(
        self, family: SellerFamily, price_batch: FamilyPriceBatchResult, schedule_key: str, now: datetime,
    ) -> FamilyRaiseResult:
        operation_key = _operation_key(family, schedule_key)
        previous = self.attempts.last(family)
        if not self.attempts.reserve(family, operation_key, now):
            return FamilyRaiseResult(
                family, RaiseResultStatus.DUPLICATE, operation_key, now,
                previous.next_allowed_at if previous else None, "duplicate scheduled attempt",
            )
        if price_batch.status is not FamilyBatchStatus.COMPLETED:
            return self._finish(
                family, operation_key, now, RaiseResultStatus.BLOCKED,
                price_batch.reason,
            )
        unsafe_reason = self.snapshots.unsafe_reason(family)
        if unsafe_reason:
            return self._finish(family, operation_key, now, RaiseResultStatus.BLOCKED, unsafe_reason)
        cooldown = self.cooldown.check(previous, now)
        if not cooldown.allowed:
            return self._finish(
                family, operation_key, now, RaiseResultStatus.COOLDOWN, cooldown.reason,
                next_allowed_at=cooldown.next_allowed_at,
            )
        capability = self.raise_client.raise_capability(family)
        if capability.state is CapabilityState.UNSUPPORTED:
            return self._finish(family, operation_key, now, RaiseResultStatus.UNSUPPORTED, capability.reason)
        if capability.state is CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION:
            return self._finish(family, operation_key, now, RaiseResultStatus.UNAVAILABLE, capability.reason)
        if capability.availability is RaiseAvailability.UNAVAILABLE:
            return self._finish(family, operation_key, now, RaiseResultStatus.UNAVAILABLE, capability.reason)
        if capability.availability is RaiseAvailability.COOLDOWN:
            return self._finish(
                family, operation_key, now, RaiseResultStatus.COOLDOWN, capability.reason,
                next_allowed_at=capability.next_allowed_at,
            )
        result = self.raise_client.raise_family(family, operation_key=operation_key)
        if result.outcome is LotWriteOutcome.SUCCEEDED:
            return self._finish(
                family, operation_key, now, RaiseResultStatus.COMPLETED, None,
                next_allowed_at=self.cooldown.after_success(now),
            )
        return self._finish(family, operation_key, now, RaiseResultStatus.FAILED, result.detail)

    def _finish(
        self, family: SellerFamily, operation_key: str, now: datetime, status: RaiseResultStatus,
        reason: str | None, *, next_allowed_at: datetime | None = None,
    ) -> FamilyRaiseResult:
        self.attempts.finish(operation_key, status, next_allowed_at=next_allowed_at, failure_reason=reason)
        return FamilyRaiseResult(family, status, operation_key, now, next_allowed_at, reason)


def _operation_key(family: SellerFamily, schedule_key: str) -> str:
    return hashlib.sha256(f"raise:{family.value}:{schedule_key}".encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stored_time(value: datetime) -> str:
    return _utc(value).isoformat()


def _read_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))
