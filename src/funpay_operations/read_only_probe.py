"""Local, explicitly requested FunPay self-test with a sanitized result boundary.

The background process owns the DPAPI-backed client.  Setup Center and
Telegram can only enqueue a command in SQLite and read the aggregate result;
neither surface receives the client or a decrypted session.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from .database import MIGRATIONS, Database
from .funpay import (
    FunPayAccessDenied,
    FunPayDialog,
    FunPayLotDetails,
    FunPayNetworkUnavailable,
    FunPayProfile,
    FunPayProtocolError,
    FunPayRateLimited,
    FunPaySessionExpired,
)
from .lot_discovery import OwnLotRegistryRepository, RegisteredLot, classify_wow_lot


class ProbeState(StrEnum):
    IDLE = "idle"
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProbeRequestResult(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_RUNNING = "already_running"
    RATE_LIMITED = "rate_limited"


class ProbeErrorCode(StrEnum):
    AUTHORIZATION_REQUIRED = "authorization_required"
    NETWORK_UNAVAILABLE = "network_unavailable"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    PROTOCOL_CHANGED = "protocol_changed"
    MUTATION_BLOCKED = "mutation_blocked"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class SanitizedProbeResult:
    """Only values that may safely cross the local process/UI boundary."""

    requested_at: str | None
    started_at: str | None
    finished_at: str | None
    state: ProbeState
    authorization_ok: bool | None = None
    profile_ok: bool | None = None
    own_lots_ok: bool | None = None
    own_lots_total: int | None = None
    mythic_plus_count: int | None = None
    unmanaged_count: int | None = None
    ambiguous_count: int | None = None
    dialogs_ok: bool | None = None
    dialogs_count: int | None = None
    error_code: ProbeErrorCode | None = None
    build_sha: str = "unknown"
    schema_version: int = MIGRATIONS[-1][0]
    mutation_attempts: int = 0
    secrets_exposed: int = 0


class ReadOnlyFunPayProbeClient(Protocol):
    """Narrow capability: mutation operations are intentionally absent."""

    def get_profile(self) -> FunPayProfile: ...

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]: ...

    def get_dialogs(self) -> tuple[FunPayDialog, ...]: ...


class MutationAttemptBlocked(RuntimeError):
    """Raised if probe code attempts to resolve any known mutation method."""


class ProbeMutationTrap:
    """Fail-closed counter used by the read facade and asserted after every run."""

    _DENIED = frozenset({
        "send_message", "send_reply", "update_price", "update_title", "update_description",
        "update_fields", "create_lot", "enable_lot", "disable_lot", "bump", "raise_lot",
    })

    def __init__(self) -> None:
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def block(self, name: str) -> None:
        self._attempts += 1
        raise MutationAttemptBlocked(f"read-only probe blocked mutation capability: {name}")

    def denies(self, name: str) -> bool:
        return name in self._DENIED


class ProbeReadBoundary:
    """Expose exactly three read operations from the production FunPay client."""

    def __init__(self, client: object, trap: ProbeMutationTrap | None = None) -> None:
        self.__client = client
        self.trap = trap or ProbeMutationTrap()

    def get_profile(self) -> FunPayProfile:
        return self.__client.get_profile()  # type: ignore[attr-defined,no-any-return]

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
        return self.__client.get_own_lot_details()  # type: ignore[attr-defined,no-any-return]

    def get_dialogs(self) -> tuple[FunPayDialog, ...]:
        return self.__client.get_dialogs()  # type: ignore[attr-defined,no-any-return]

    def __getattr__(self, name: str) -> object:
        if self.trap.denies(name):
            self.trap.block(name)
        raise AttributeError(name)


class ReadOnlyProbeRepository:
    """Atomic command queue and sanitized single-row result storage."""

    def __init__(
        self,
        database: Database,
        *,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("probe cooldown must not be negative")
        self.database, self.cooldown_seconds, self.clock = database, cooldown_seconds, clock

    def request(self) -> ProbeRequestResult:
        now_epoch = self.clock()
        now = _timestamp(now_epoch)
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, requested_at FROM read_only_probe_state WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("read-only probe schema is unavailable")
            if row["state"] in {ProbeState.REQUESTED.value, ProbeState.RUNNING.value}:
                return ProbeRequestResult.ALREADY_RUNNING
            requested_epoch = _timestamp_epoch(row["requested_at"])
            if requested_epoch is not None and now_epoch - requested_epoch < self.cooldown_seconds:
                return ProbeRequestResult.RATE_LIMITED
            connection.execute(
                """UPDATE read_only_probe_state SET requested_at = ?, started_at = NULL,
                finished_at = NULL, state = 'requested', authorization_ok = NULL, profile_ok = NULL,
                own_lots_ok = NULL, own_lots_total = NULL, mythic_plus_count = NULL,
                unmanaged_count = NULL, ambiguous_count = NULL, dialogs_ok = NULL,
                dialogs_count = NULL, error_code = NULL, mutation_attempts = 0,
                secrets_exposed = 0, updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1""",
                (now,),
            )
        return ProbeRequestResult.ACCEPTED

    def claim(self) -> SanitizedProbeResult | None:
        started = _timestamp(self.clock())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE read_only_probe_state SET state = 'running', started_at = ?,
                updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1 AND state = 'requested'""",
                (started,),
            ).rowcount
            if updated != 1:
                return None
            row = connection.execute(
                "SELECT * FROM read_only_probe_state WHERE singleton_id = 1"
            ).fetchone()
        return _result_from_row(row)

    def save(self, result: SanitizedProbeResult) -> None:
        if result.mutation_attempts < 0 or result.secrets_exposed != 0:
            raise ValueError("unsafe probe result cannot be persisted")
        with self.database.session() as connection:
            connection.execute(
                """UPDATE read_only_probe_state SET requested_at = ?, started_at = ?, finished_at = ?,
                state = ?, authorization_ok = ?, profile_ok = ?, own_lots_ok = ?, own_lots_total = ?,
                mythic_plus_count = ?, unmanaged_count = ?, ambiguous_count = ?, dialogs_ok = ?,
                dialogs_count = ?, error_code = ?, build_sha = ?, schema_version = ?,
                mutation_attempts = ?, secrets_exposed = 0, updated_at = CURRENT_TIMESTAMP
                WHERE singleton_id = 1""",
                (
                    result.requested_at, result.started_at, result.finished_at, result.state.value,
                    _db_bool(result.authorization_ok), _db_bool(result.profile_ok), _db_bool(result.own_lots_ok),
                    result.own_lots_total, result.mythic_plus_count, result.unmanaged_count,
                    result.ambiguous_count, _db_bool(result.dialogs_ok), result.dialogs_count,
                    result.error_code.value if result.error_code else None, result.build_sha,
                    result.schema_version, result.mutation_attempts,
                ),
            )

    def load(self) -> SanitizedProbeResult:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM read_only_probe_state WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("read-only probe schema is unavailable")
        return _result_from_row(row)

    def recover_interrupted(self) -> bool:
        """Fail a lease left running by a crashed previous singleton process."""

        finished = _timestamp(self.clock())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE read_only_probe_state SET state = 'failed', requested_at = NULL,
                finished_at = ?, error_code = 'internal_error', mutation_attempts = 0,
                secrets_exposed = 0, updated_at = CURRENT_TIMESTAMP
                WHERE singleton_id = 1 AND state = 'running'""",
                (finished,),
            ).rowcount
        return updated == 1


class ReadOnlyFunPayProbe:
    """Execute the bounded profile/lots/dialogs sequence inside background."""

    def __init__(
        self,
        client: ReadOnlyFunPayProbeClient,
        registry: OwnLotRegistryRepository,
        repository: ReadOnlyProbeRepository,
        *,
        trap: ProbeMutationTrap,
        build_sha: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client, self.registry, self.repository = client, registry, repository
        self.trap, self.clock = trap, clock
        self.build_sha = build_sha or local_build_sha()

    def run_pending(self) -> SanitizedProbeResult | None:
        claimed = self.repository.claim()
        if claimed is None:
            return None
        result = self._execute(claimed)
        self.repository.save(result)
        return result

    def _execute(self, claimed: SanitizedProbeResult) -> SanitizedProbeResult:
        authorization_ok: bool | None = None
        profile_ok: bool | None = None
        own_lots_ok: bool | None = None
        dialogs_ok: bool | None = None
        total = mythic = unmanaged = ambiguous = dialogs_count = None
        error_code: ProbeErrorCode | None = None
        try:
            profile = self.client.get_profile()
            if not isinstance(profile, FunPayProfile):
                raise FunPayProtocolError("profile result has an unexpected shape")
            profile_ok = True
            authorization_ok = profile.authorized
            if not profile.authorized:
                raise FunPaySessionExpired("FunPay profile is not authorized")

            details = self.client.get_own_lot_details()
            if not isinstance(details, tuple) or not all(isinstance(item, FunPayLotDetails) for item in details):
                raise FunPayProtocolError("own-lot result has an unexpected shape")
            registered = tuple(RegisteredLot(item, classify_wow_lot(item)) for item in details)
            self.registry.replace(registered)
            own_lots_ok = True
            total = len(registered)
            mythic = sum(item.classification.kind == "mythic_plus" for item in registered)
            ambiguous = sum(item.classification.ambiguous for item in registered)
            unmanaged = total - mythic - ambiguous

            dialogs = self.client.get_dialogs()
            if not isinstance(dialogs, tuple) or not all(isinstance(item, FunPayDialog) for item in dialogs):
                raise FunPayProtocolError("dialog result has an unexpected shape")
            dialogs_ok = True
            dialogs_count = len(dialogs)
            state = ProbeState.SUCCEEDED
        except FunPaySessionExpired:
            authorization_ok = False
            error_code, state = ProbeErrorCode.AUTHORIZATION_REQUIRED, ProbeState.FAILED
        except FunPayRateLimited:
            error_code, state = ProbeErrorCode.RATE_LIMITED, ProbeState.FAILED
        except FunPayAccessDenied:
            error_code, state = ProbeErrorCode.ACCESS_DENIED, ProbeState.FAILED
        except FunPayNetworkUnavailable:
            error_code, state = ProbeErrorCode.NETWORK_UNAVAILABLE, ProbeState.FAILED
        except (FunPayProtocolError, TypeError, ValueError):
            error_code, state = ProbeErrorCode.PROTOCOL_CHANGED, ProbeState.FAILED
        except MutationAttemptBlocked:
            error_code, state = ProbeErrorCode.MUTATION_BLOCKED, ProbeState.FAILED
        except Exception:
            error_code, state = ProbeErrorCode.INTERNAL_ERROR, ProbeState.FAILED

        if self.trap.attempts:
            error_code, state = ProbeErrorCode.MUTATION_BLOCKED, ProbeState.FAILED
        return SanitizedProbeResult(
            requested_at=claimed.requested_at,
            started_at=claimed.started_at,
            finished_at=_timestamp(self.clock()),
            state=state,
            authorization_ok=authorization_ok,
            profile_ok=profile_ok,
            own_lots_ok=own_lots_ok,
            own_lots_total=total,
            mythic_plus_count=mythic,
            unmanaged_count=unmanaged,
            ambiguous_count=ambiguous,
            dialogs_ok=dialogs_ok,
            dialogs_count=dialogs_count,
            error_code=error_code,
            build_sha=self.build_sha,
            schema_version=MIGRATIONS[-1][0],
            mutation_attempts=self.trap.attempts,
            secrets_exposed=0,
        )


def render_safe_probe_result(result: SanitizedProbeResult) -> str:
    """Copy-safe text containing no account identifiers or private content."""

    if result.state in {ProbeState.REQUESTED, ProbeState.RUNNING}:
        return "🔍 Проверяю FunPay…\n\nНичего на FunPay не изменяется."
    if result.state is ProbeState.IDLE:
        return "🔍 Безопасная проверка FunPay\n\nПроверка ещё не запускалась."
    if result.state is ProbeState.FAILED:
        return _failure_text(result.error_code)
    return (
        "READ-ONLY FUNPAY CHECK\n\n"
        f"Authorization: {_ok(result.authorization_ok)}\n"
        f"Profile: {_ok(result.profile_ok)}\n"
        f"Own lots: {_ok(result.own_lots_ok)}\n"
        f"Own lots total: {result.own_lots_total or 0}\n"
        f"Mythic+ managed: {result.mythic_plus_count or 0}\n"
        f"Unmanaged: {result.unmanaged_count or 0}\n"
        f"Ambiguous: {result.ambiguous_count or 0}\n"
        f"Dialogs: {_ok(result.dialogs_ok)}\n"
        f"Dialogs count: {result.dialogs_count or 0}\n\n"
        "Mutation attempts: 0\nSecrets exposed: 0\n\n"
        "Price writes: DISABLED\nLot writes: DISABLED\nRaise: DISABLED\n"
        "Auto-reply: DISABLED\nTelegram replies: DISABLED\nAutomation: DISABLED"
    )


def local_build_sha() -> str:
    """Return a sanitized build fingerprint without reading configuration."""

    declared = os.environ.get("FUNPAY_OPERATIONS_BUILD_SHA", "")
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", declared):
        return declared.lower()
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return "unknown"
    return digest


def _result_from_row(row: object) -> SanitizedProbeResult:
    return SanitizedProbeResult(
        requested_at=row["requested_at"], started_at=row["started_at"], finished_at=row["finished_at"],
        state=ProbeState(row["state"]), authorization_ok=_bool(row["authorization_ok"]),
        profile_ok=_bool(row["profile_ok"]), own_lots_ok=_bool(row["own_lots_ok"]),
        own_lots_total=row["own_lots_total"], mythic_plus_count=row["mythic_plus_count"],
        unmanaged_count=row["unmanaged_count"], ambiguous_count=row["ambiguous_count"],
        dialogs_ok=_bool(row["dialogs_ok"]), dialogs_count=row["dialogs_count"],
        error_code=ProbeErrorCode(row["error_code"]) if row["error_code"] else None,
        build_sha=row["build_sha"], schema_version=row["schema_version"],
        mutation_attempts=row["mutation_attempts"], secrets_exposed=row["secrets_exposed"],
    )


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _timestamp_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _db_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _ok(value: bool | None) -> str:
    return "OK" if value else "FAIL"


def _failure_text(error: ProbeErrorCode | None) -> str:
    if error is ProbeErrorCode.AUTHORIZATION_REQUIRED:
        return "🔴 FunPay требуется авторизация\n\nОткройте локальный Setup Center и войдите снова."
    if error in {ProbeErrorCode.NETWORK_UNAVAILABLE, ProbeErrorCode.RATE_LIMITED, ProbeErrorCode.ACCESS_DENIED}:
        return "🟡 FunPay временно недоступен\n\nНичего не изменено. Попробуйте позже."
    return (
        "⚠️ Не удалось безопасно прочитать данные FunPay\n\n"
        "Изменения FunPay не выполнялись. Подробности сохранены без приватных данных."
    )
