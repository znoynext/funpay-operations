"""Production Telegram control service backed only by read-only FunPay calls.

This module is the production-safe composition boundary.  It owns no FunPay
mutation client and rejects every external write action before dispatch.
Account identifiers stay in local SQLite or memory and never enter Telegram
view models.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable, Mapping, Protocol

from .config import Settings
from .database import Database
from .funpay import (
    FunPayClient,
    FunPayAccessDenied,
    FunPayError,
    FunPayLotDetails,
    FunPayNetworkUnavailable,
    FunPayProtocolError,
    FunPayRateLimited,
    FunPaySessionExpired,
    RealOperationsDisabled,
)
from .lot_discovery import LotDiscovery, OwnLotRegistryRepository, RegisteredLot
from .mythic_onboarding import (
    BudgetDecision,
    MappingConfidence,
    MythicVariant,
    OwnLotMappingRepository,
    OwnMappingStatus,
    PreLiveEligibilityGuard,
    ReadOnlyRequestBudgetRepository,
    MinimumPriceRepository,
    opaque_lot_key,
    parse_manual_variant,
    parse_minimum_price_batch,
    parse_mythic_lot,
    parse_nickname_batch,
)
from .price_safety import PriceObservationRecord, SafetyDecisionStatus, SafetyValidatedPricingEngine
from .pricing import OwnLotPriceState, OwnLotPricingMode, PricePolicy, TrustedPriceObservation
from .repositories import TaskStateRepository
from .read_only_probe import (
    ProbeErrorCode,
    ProbeState,
    ReadOnlyProbeRepository,
    render_safe_probe_result,
)
from .service_catalog import ServiceCatalogRepository
from .session_health import FunPaySessionGuard
from .telegram_views import (
    DashboardView,
    FamilyView,
    LotView,
    MappingChoiceView,
    CompetitorMappingOverviewView,
    MinimumPriceOverviewView,
    OwnMappingCandidateView,
    OwnMappingOverviewView,
    PriceChangeView,
    PriceOverviewView,
    PricePreviewView,
    PriceSkipView,
    SellerCandidateView,
    SellerBatchPreviewView,
    StatusView,
    TrustedSellerView,
    ReadinessView,
)
from .trusted_sellers import (
    CompetitorLotMappingRepository,
    CompetitorLotSnapshot,
    ManualSellerConfirmationAPI,
    MatchResult,
    SellerFamily,
    SellerLastCheckedState,
    SellerMatchingEngine,
    SellerVerificationState,
    ServiceMatchSpec,
    TrustedSellerRepository,
)


class ReadOnlyOnboardingClient(Protocol):
    """Narrow production capability used by onboarding and pricing preview."""

    def has_local_session(self) -> bool: ...
    def get_profile(self): ...
    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]: ...
    def get_seller_lot_details(self, seller_id: str) -> tuple[FunPayLotDetails, ...]: ...
    def get_dialogs(self): ...


class OnboardingMutationBlocked(RuntimeError):
    """Raised if onboarding code resolves a known state-changing capability."""


class OnboardingMutationTrap:
    MUTATIONS = frozenset({
        "send_reply", "update_price", "change_lot_price", "edit_price", "update_title",
        "update_description", "create_lot", "enable_lot", "disable_lot", "activate",
        "deactivate", "raise_lots", "bump", "rollback",
    })

    def __init__(self) -> None:
        self.attempts = 0

    def block(self, name: str) -> None:
        self.attempts += 1
        raise OnboardingMutationBlocked(f"read-only onboarding blocked mutation capability: {name}")


class OnboardingReadBoundary:
    """Expose only authenticated GET/read operations from the production adapter."""

    def __init__(self, client: FunPayClient, trap: OnboardingMutationTrap) -> None:
        self.__client, self.__trap = client, trap

    def has_local_session(self) -> bool:
        return bool(getattr(self.__client, "has_local_session", lambda: True)())

    def get_profile(self):
        return self.__client.get_profile()

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
        return self.__client.get_own_lot_details()

    def get_seller_lot_details(self, seller_id: str) -> tuple[FunPayLotDetails, ...]:
        return self.__client.get_seller_lot_details(seller_id)

    def get_dialogs(self):
        return self.__client.get_dialogs()

    def __getattr__(self, name: str):
        if name in self.__trap.MUTATIONS:
            self.__trap.block(name)
        raise AttributeError(name)


class FunPayReadStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    CONNECTED = "connected"
    CHECKING = "checking"
    AUTHORIZATION_REQUIRED = "authorization_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FunPayHealth:
    status: FunPayReadStatus
    checked_at: float
    successful_read_at: str | None = None


@dataclass(frozen=True)
class LotControlSettings:
    mode: OwnLotPricingMode
    fixed_price_minor: int | None
    minimum_price_minor: int | None


class LotControlRepository:
    """Safe local pricing preferences; no external adapter is reachable here."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, external_lot_id: str) -> LotControlSettings:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM lot_control_settings WHERE external_lot_id = ?", (external_lot_id,)
            ).fetchone()
        if row is None:
            return LotControlSettings(OwnLotPricingMode.CHECK_ONLY, None, None)
        return LotControlSettings(
            OwnLotPricingMode(row["pricing_mode"]), row["fixed_price_minor"], row["minimum_price_minor"]
        )

    def set_mode(self, external_lot_id: str, mode: OwnLotPricingMode) -> None:
        if mode is OwnLotPricingMode.FIXED_PRICE:
            raise ValueError("fixed price must be entered explicitly")
        self._upsert(external_lot_id, mode=mode, fixed_price_minor=None)

    def set_fixed_price(self, external_lot_id: str, price_minor: int) -> None:
        _positive_minor(price_minor)
        self._upsert(external_lot_id, mode=OwnLotPricingMode.FIXED_PRICE, fixed_price_minor=price_minor)

    def set_minimum_price(self, external_lot_id: str, price_minor: int | None) -> None:
        if price_minor is not None:
            _positive_minor(price_minor)
        current = self.get(external_lot_id)
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO lot_control_settings
                (external_lot_id, pricing_mode, fixed_price_minor, minimum_price_minor)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(external_lot_id) DO UPDATE SET minimum_price_minor = excluded.minimum_price_minor,
                updated_at = CURRENT_TIMESTAMP""",
                (external_lot_id, current.mode.value, current.fixed_price_minor, price_minor),
            )

    def _upsert(self, external_lot_id: str, *, mode: OwnLotPricingMode, fixed_price_minor: int | None) -> None:
        current = self.get(external_lot_id)
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO lot_control_settings
                (external_lot_id, pricing_mode, fixed_price_minor, minimum_price_minor)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(external_lot_id) DO UPDATE SET pricing_mode = excluded.pricing_mode,
                fixed_price_minor = excluded.fixed_price_minor, updated_at = CURRENT_TIMESTAMP""",
                (external_lot_id, mode.value, fixed_price_minor, current.minimum_price_minor),
            )


class ReadOnlyPriceObservationRepository:
    """Bounded local history for single-seller confirmation and consensus."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def next_sequence(self, seller_id: str, competitor_lot_id: str) -> int:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT COALESCE(MAX(sequence), 0) AS value FROM read_only_price_observations
                WHERE seller_id = ? AND competitor_lot_id = ?""",
                (seller_id, competitor_lot_id),
            ).fetchone()
        return int(row["value"]) + 1

    def list(self) -> tuple[PriceObservationRecord, ...]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM read_only_price_observations ORDER BY observed_at, observation_id"
            ).fetchall()
        return tuple(_observation_from_row(row) for row in rows)

    def save(self, records: tuple[PriceObservationRecord, ...]) -> None:
        with self.database.session() as connection:
            for record in records:
                item = record.observation
                connection.execute(
                    """INSERT OR IGNORE INTO read_only_price_observations
                    (observation_id, seller_id, competitor_lot_id, service_code, price_minor, currency,
                     lot_identity_hash, structural_signature, sequence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.observation_id, item.seller_id, item.competitor_lot_id, item.service_code,
                        item.price_minor, item.currency, record.lot_identity_hash,
                        record.structural_signature, record.sequence,
                    ),
                )
            # Keep enough history for consensus while bounding local growth.
            connection.execute(
                """DELETE FROM read_only_price_observations WHERE observation_id IN (
                    SELECT observation_id FROM read_only_price_observations current
                    WHERE 20 < (
                        SELECT COUNT(*) FROM read_only_price_observations newer
                        WHERE newer.seller_id = current.seller_id
                          AND newer.competitor_lot_id = current.competitor_lot_id
                          AND newer.sequence >= current.sequence
                    )
                )"""
            )


class ProductionReadOnlyControlService:
    """Truthful production view service with an unconditional mutation barrier."""

    READ_ACTIONS = frozenset({
        "refresh", "check_prices", "lot_check", "lot_decision", "seller_recheck",
        "own_mapping_analyze", "competitor_discover",
    })
    LOCAL_ACTIONS = frozenset({
        "pause", "resume", "lot_automatic", "lot_paused", "lot_check_only", "lot_set_fixed",
        "lot_set_floor", "lot_clear_floor", "seller_add", "seller_remove", "seller_disable", "seller_remap",
        "seller_add_batch", "confirm_own_high", "confirm_own_manual", "confirm_competitor_high",
        "floor_set_global", "floor_set_key_batch", "floor_set_variant", "run_probe",
    })
    FUNPAY_MUTATIONS = frozenset({
        "mass_lot_sync", "mass_price_update", "update_raise", "rollback", "disable_lots",
        "auto_reply_toggle", "outbound_reply", "price_writes", "lot_writes", "raise",
    })
    external_mutations_allowed = False

    def __init__(
        self, database: Database, funpay: ReadOnlyOnboardingClient, settings: Settings, states: TaskStateRepository,
        session_guard: FunPaySessionGuard, *, telegram_configured: bool, logger: logging.Logger,
        health_ttl_seconds: float = 45.0, clock: Callable[[], float] = time.monotonic,
        session_expired_callback: Callable[[], None] | None = None,
        probe_repository: ReadOnlyProbeRepository | None = None,
        mutation_trap: OnboardingMutationTrap | None = None,
    ) -> None:
        if health_ttl_seconds <= 0:
            raise ValueError("health TTL must be positive")
        self.database, self.funpay, self.settings, self.states = database, funpay, settings, states
        self.session_guard, self.telegram_configured, self.logger = session_guard, telegram_configured, logger
        self.session_expired_callback = session_expired_callback
        self.probe_repository = probe_repository
        self.mutation_trap = mutation_trap
        self.registry = OwnLotRegistryRepository(database)
        self.controls = LotControlRepository(database)
        self.seller_repository = TrustedSellerRepository(database)
        self.mapping_repository = CompetitorLotMappingRepository(database)
        self.catalog = ServiceCatalogRepository(database)
        self.confirmations = ManualSellerConfirmationAPI(self.seller_repository, self.mapping_repository)
        self.observation_history = ReadOnlyPriceObservationRepository(database)
        self.own_mapping_repository = OwnLotMappingRepository(database)
        self.minimum_prices = MinimumPriceRepository(database)
        self.request_budgets = ReadOnlyRequestBudgetRepository(database)
        self.eligibility = PreLiveEligibilityGuard()
        self.pricing = SafetyValidatedPricingEngine()
        self.health_ttl_seconds, self.clock = health_ttl_seconds, clock
        self._health = FunPayHealth(FunPayReadStatus.NOT_CHECKED, 0)
        self._health_checking = False
        self._discovery_attempted = False
        self._seller_candidates: dict[str, tuple[str, str]] = {}
        self._seller_batch_candidates: dict[str, tuple[str, str]] = {}
        self._seller_identity_cache: dict[str, set[str]] | None = None
        self._verified_seller_ids: set[str] = set()
        self._mapping_candidates: dict[str, tuple[CompetitorLotSnapshot, tuple[ServiceMatchSpec, ...]]] = {}
        self._mapping_candidate_labels: dict[str, str] = {}
        self._mapping_counts = (0, 0, 0, 0)
        self._pending_corrections: dict[str, MythicVariant] = {}
        self._pending_floor_batch: dict[int, int] = {}
        self._last_preview = PricePreviewView((), ())
        self._last_calculations: dict[str, str] = {}
        self._last_readiness = ReadinessView(0, 0, 0, 0, 0, 0, 0, 0, False)

    def execute(self, action: str, payload: str | None = None) -> str:
        if action in self.FUNPAY_MUTATIONS or action not in self.READ_ACTIONS | self.LOCAL_ACTIONS:
            raise RealOperationsDisabled("Реальные изменения FunPay пока не разрешены.")
        if action == "refresh":
            if not self.refresh_lots(force_health=True):
                raise RuntimeError("FunPay read refresh is unavailable")
            return "refreshed"
        if action in {"check_prices", "lot_check", "lot_decision"}:
            self._run_price_check(payload)
            return "checked"
        if action == "own_mapping_analyze":
            self.refresh_lots(force_health=True)
            self._analyze_cached_own_lots()
            return "analyzed"
        if action == "competitor_discover":
            self._recheck_sellers()
            return "checked"
        if action == "seller_recheck":
            self._recheck_sellers()
            return "checked"
        if action == "run_probe":
            if self.probe_repository is None:
                raise RuntimeError("read-only probe is unavailable")
            return self.probe_repository.request().value
        if action in {"pause", "resume"}:
            return "local-state"
        if action == "confirm_own_high":
            result = str(self.own_mapping_repository.confirm_high_batch())
            self.request_budgets.clear_cooldown("price_observation")
            return result
        if action == "confirm_own_manual" and payload:
            variant = self._pending_corrections.pop(payload, None)
            if variant is None:
                raise ValueError("mapping correction preview is stale")
            self.own_mapping_repository.confirm_manual(payload, variant)
            self.request_budgets.clear_cooldown("price_observation")
            return "saved-locally"
        if action == "confirm_competitor_high":
            confirmed = 0
            for key, (snapshot, specs) in tuple(self._mapping_candidates.items()):
                self.confirmations.confirm_match(snapshot, specs)
                self._mapping_candidates.pop(key, None)
                confirmed += 1
            checked, _, attention, no_match = self._mapping_counts
            self._mapping_counts = checked, 0, attention, no_match
            self.request_budgets.clear_cooldown("price_observation")
            return str(confirmed)
        if action.startswith("floor_"):
            self._update_minimum_price(action, payload)
            return "saved-locally"
        if action.startswith("lot_"):
            self._update_lot_setting(action, payload)
            return "saved-locally"
        if action.startswith("seller_"):
            self._update_seller(action, payload)
            return "saved-locally"
        raise RealOperationsDisabled("Реальные изменения FunPay пока не разрешены.")

    def dashboard(self, *, emergency_active: bool) -> DashboardView:
        if self.probe_repository is not None:
            mapping_summary = self._analyze_cached_own_lots()
            probe = self.probe_repository.load()
            probe_status = {
                ProbeState.IDLE: FunPayReadStatus.NOT_CHECKED,
                ProbeState.REQUESTED: FunPayReadStatus.CHECKING,
                ProbeState.RUNNING: FunPayReadStatus.CHECKING,
                ProbeState.SUCCEEDED: FunPayReadStatus.CONNECTED,
                ProbeState.FAILED: (
                    FunPayReadStatus.AUTHORIZATION_REQUIRED
                    if probe.error_code is ProbeErrorCode.AUTHORIZATION_REQUIRED
                    else FunPayReadStatus.UNAVAILABLE
                ),
            }[probe.state]
            managed = mapping_summary.confirmed
            unmanaged = mapping_summary.total - mapping_summary.confirmed
            ambiguous = mapping_summary.attention
            return self._dashboard_view(
                emergency_active, probe_status, managed, unmanaged, ambiguous,
                "только что" if probe.state is ProbeState.SUCCEEDED else probe.finished_at or "Пока не проверено",
            )
        health = self.health()
        if health.status is FunPayReadStatus.CONNECTED and not self.registry.list() and not self._discovery_attempted:
            self.refresh_lots()
            health = self.health()
        mapping_summary = self._analyze_cached_own_lots()
        return self._dashboard_view(
            emergency_active, health.status,
            mapping_summary.confirmed,
            mapping_summary.total - mapping_summary.confirmed,
            mapping_summary.attention,
            health.successful_read_at or "Пока не проверено",
        )

    def _dashboard_view(
        self,
        emergency_active: bool,
        health_status: FunPayReadStatus,
        managed: int,
        unmanaged: int,
        ambiguous: int,
        last_read: str,
    ) -> DashboardView:
        return DashboardView(
            (
                StatusView("Bot", "🟢 Работает"),
                StatusView("FunPay", _health_label(health_status)),
                StatusView("Telegram", "🟢 Подключён" if self.telegram_configured else "⚪ Не настроен"),
                StatusView("Automation", "⏸ Выключена"),
                StatusView("Emergency stop", "🔴 Активен" if emergency_active else "🟢 Не активен"),
            ),
            managed,
            unmanaged + ambiguous,
            "Не выполнялось: запись отключена", "Не выполнялся: raise отключён", "Не планируется",
            last_funpay_read=last_read,
            unknown_lots=unmanaged,
            ambiguous_lots=ambiguous,
        )

    def probe_status(self) -> str:
        if self.probe_repository is None:
            return "🔍 Безопасная проверка FunPay\n\nПроверка недоступна."
        return render_safe_probe_result(self.probe_repository.load())

    def own_mapping_overview(self) -> OwnMappingOverviewView:
        summary = self._analyze_cached_own_lots()
        candidates = tuple(OwnMappingCandidateView(
            item.opaque_key, item.display_title,
            item.variant.label if item.variant else "",
            item.confidence.value, item.status.value, item.evidence,
            item.missing_fields + item.ambiguity_reasons,
            item.bulk_confirmable,
        ) for item in summary.reviews)
        return OwnMappingOverviewView(
            summary.total, summary.high, summary.attention, summary.excluded, summary.confirmed, candidates
        )

    def preview_own_mapping_correction(self, key: str, value: str) -> OwnMappingCandidateView:
        current = self.own_mapping_repository.get_by_opaque_key(key)
        variant = parse_manual_variant(value)
        self._pending_corrections[key] = variant
        return OwnMappingCandidateView(
            key, current.display_title, variant.label, MappingConfidence.HIGH.value,
            OwnMappingStatus.CANDIDATE.value, ("critical fields: explicitly entered by owner",), (), False,
        )

    def find_sellers(self, value: str) -> SellerBatchPreviewView:
        nicknames = parse_nickname_batch(value)
        decision = self.request_budgets.claim("seller_lookup", cooldown_seconds=5)
        using_cache = decision is not BudgetDecision.ALLOWED
        if using_cache and self._seller_identity_cache is None:
            raise RuntimeError("seller lookup is rate limited")
        exact: list[str] = []
        attention: list[str] = []
        self._seller_batch_candidates = {}
        try:
            self._require_connected()
            if not using_cache:
                dialogs = self.funpay.get_dialogs()
                identities: dict[str, set[str]] = {}
                for dialog in dialogs:
                    if dialog.counterparty_id:
                        identities.setdefault(dialog.counterparty_name.casefold(), set()).add(dialog.counterparty_id)
                self._seller_identity_cache = identities
            else:
                identities = self._seller_identity_cache or {}
            for nickname in nicknames:
                matches = identities.get(nickname.casefold(), set())
                if len(matches) != 1:
                    attention.append(f"{nickname} — {'не найден точный профиль' if not matches else 'неоднозначно'}")
                    continue
                seller_id = next(iter(matches))
                if seller_id not in self._verified_seller_ids:
                    if using_cache:
                        attention.append(f"{nickname} — повторная проверка временно ограничена")
                        continue
                    self.funpay.get_seller_lot_details(seller_id)
                    self._verified_seller_ids.add(seller_id)
                self._seller_batch_candidates[nickname.casefold()] = (seller_id, nickname)
                exact.append(nickname)
        except (FunPayRateLimited, FunPayAccessDenied):
            self.request_budgets.fail("seller_lookup", severe=True)
            raise RuntimeError("seller lookup stopped by FunPay safety response")
        except FunPayError:
            self.request_budgets.fail("seller_lookup")
            raise RuntimeError("seller lookup is unavailable")
        if not using_cache:
            self.request_budgets.succeed("seller_lookup")
        return SellerBatchPreviewView(tuple(exact), tuple(attention))

    def competitor_mapping_overview(self) -> CompetitorMappingOverviewView:
        checked, exact, attention, no_match = self._mapping_counts
        return CompetitorMappingOverviewView(
            checked, exact, attention, no_match,
            tuple(MappingChoiceView(label, "Точное совпадение; подтвердите вручную", key)
                  for key, label in self._mapping_labels()),
        )

    def minimum_price_overview(self) -> MinimumPriceOverviewView:
        variants = tuple(
            item.variant for item in self.own_mapping_repository.list()
            if item.status is OwnMappingStatus.CONFIRMED and item.variant is not None
        )
        global_count, key_count, variant_count, covered = self.minimum_prices.counts(variants)
        return MinimumPriceOverviewView(bool(global_count), key_count, variant_count, covered, len(variants))

    def preview_minimum_price_batch(self, value: str) -> Mapping[int, int]:
        parsed = parse_minimum_price_batch(value)
        self._pending_floor_batch = dict(parsed)
        return parsed

    def readiness(self) -> ReadinessView:
        self._update_readiness(
            self._last_readiness.dry_run_ready, self._last_readiness.dry_run_blocked,
            dry_run_success=None,
        )
        return self._last_readiness

    def family(self, family: str) -> FamilyView:
        if family != "Mythic+":
            raise ValueError("only Mythic+ is a managed service family")
        lots = self.lots(family)
        return FamilyView(
            family, len(lots), sum(item.mode == "automatic" for item in lots),
            sum(item.mode == "fixed_price" for item in lots), sum(item.mode == "paused" for item in lots),
            sum(item.warning is not None for item in lots), "Не выполнялось: запись отключена", "Raise отключён",
        )

    def lots(self, family: str | None = None) -> tuple[LotView, ...]:
        views = tuple(self._lot_view(item) for item in self.registry.list())
        return tuple(item for item in views if family is None or item.family == family)

    def price_overview(self) -> PriceOverviewView:
        lots = self.lots("Mythic+")
        return PriceOverviewView(
            sum(item.mode == "automatic" for item in lots), sum(item.mode == "fixed_price" for item in lots),
            sum(item.mode == "paused" for item in lots), sum(item.warning is not None for item in lots),
            "Только read-only проверка; цены не изменяются",
        )

    def price_preview(self) -> PricePreviewView:
        return self._last_preview

    def sellers(self) -> tuple[TrustedSellerView, ...]:
        return tuple(TrustedSellerView(
            item.nickname, _seller_family_label(item.family), item.enabled,
            item.verification_state is SellerVerificationState.VERIFIED,
        ) for item in self.seller_repository.list())

    def find_seller(self, nickname: str) -> SellerCandidateView | None:
        preview = self.find_sellers(nickname)
        if len(preview.exact) != 1:
            return None
        clean = preview.exact[0]
        candidate = self._seller_batch_candidates.get(clean.casefold())
        if candidate is None:
            return None
        self._seller_candidates[clean.casefold()] = candidate
        return SellerCandidateView(clean, True)

    def mapping_choices(self) -> tuple[MappingChoiceView, ...]:
        return tuple(
            MappingChoiceView(label, "Точное совпадение; подтвердите вручную", key)
            for key, label in self._mapping_labels()
        )

    def health(self, *, force: bool = False) -> FunPayHealth:
        now = self.clock()
        if (not force and self._health.status is not FunPayReadStatus.NOT_CHECKED
                and now - self._health.checked_at < self.health_ttl_seconds):
            return self._health
        if self.session_guard.is_expired and not self.session_guard.allows_polling():
            self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, now)
            return self._health
        has_session = getattr(self.funpay, "has_local_session", lambda: True)()
        if not has_session:
            self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, now)
            return self._health
        if self._health_checking:
            return FunPayHealth(FunPayReadStatus.CHECKING, now, self._health.successful_read_at)
        self._health_checking = True
        try:
            profile = self.funpay.get_profile()
            if not profile.authorized:
                self._mark_session_expired()
                self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, now)
            else:
                self.session_guard.mark_authorized()
                checked = _now_label()
                self.states.save("funpay_read_health", "connected", checked)
                self._health = FunPayHealth(FunPayReadStatus.CONNECTED, now, "только что")
        except FunPaySessionExpired:
            self._mark_session_expired()
            self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, now)
        except (FunPayNetworkUnavailable, FunPayProtocolError, FunPayError):
            self.states.save("funpay_read_health", "unavailable", last_error="read_unavailable")
            self._health = FunPayHealth(FunPayReadStatus.UNAVAILABLE, now, self._health.successful_read_at)
        finally:
            self._health_checking = False
        return self._health

    def refresh_lots(self, *, force_health: bool = False) -> bool:
        self._discovery_attempted = True
        budget = self.request_budgets.claim("own_lot_read", cooldown_seconds=30)
        if budget is not BudgetDecision.ALLOWED:
            return bool(self.registry.list())
        if self.health(force=force_health).status is not FunPayReadStatus.CONNECTED:
            self.request_budgets.fail("own_lot_read")
            return False
        try:
            summary = LotDiscovery(self.funpay, self.registry).run()
            self._analyze_cached_own_lots()
        except FunPaySessionExpired:
            self.request_budgets.fail("own_lot_read", severe=True)
            self._mark_session_expired()
            self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, self.clock())
            return False
        except (FunPayRateLimited, FunPayAccessDenied):
            self.request_budgets.fail("own_lot_read", severe=True)
            self._health = FunPayHealth(FunPayReadStatus.UNAVAILABLE, self.clock(), self._health.successful_read_at)
            return False
        except FunPayError:
            self.request_budgets.fail("own_lot_read")
            self._health = FunPayHealth(FunPayReadStatus.UNAVAILABLE, self.clock(), self._health.successful_read_at)
            return False
        self.request_budgets.succeed("own_lot_read")
        self.session_guard.mark_authorized()
        self.states.save("funpay_own_lots", "ready", _now_label())
        self._health = FunPayHealth(FunPayReadStatus.CONNECTED, self.clock(), "только что")
        self.logger.info(
            "FunPay own-lot read completed; total=%d managed_mythic_plus=%d unknown=%d ambiguous=%d",
            summary.total, summary.mythic_plus, summary.unknown, summary.ambiguous,
        )
        return True

    def _mark_session_expired(self) -> None:
        first_expiry = self.session_guard.mark_expired()
        if not first_expiry or self.session_expired_callback is None:
            return
        try:
            self.session_expired_callback()
        except Exception:
            self.logger.warning("FunPay session expiry notification could not be prepared")

    def _require_connected(self) -> None:
        if self.health().status is not FunPayReadStatus.CONNECTED:
            raise RuntimeError("FunPay read connection is unavailable")

    def _lot_view(self, registered: RegisteredLot) -> LotView:
        details, classification = registered.details, registered.classification
        key = _opaque_lot_key(details.lot_id)
        control = self.controls.get(details.lot_id)
        minimum = control.minimum_price_minor or self._default_minimum(registered)
        family = _classification_family(classification.kind)
        attributes = _lot_attributes(registered)
        service_code = self._confirmed_service_code(details.lot_id)
        managed = _is_managed_mythic_plus(registered, service_code)
        warning = _lot_warning(registered, minimum, service_code)
        calculation = self._last_calculations.get(key, "Пока невозможно рассчитать: нужны подтверждённые соответствия и ориентиры.")
        technical = (
            f"Категория: {'определена' if details.category_node_id else 'не определена'}\n"
            f"Доступные параметры: {len(details.editor_fields) + len(details.editor_options)}\n"
            f"Подтверждённое соответствие: {'да' if self._confirmed_service_code(details.lot_id) else 'нет'}\n"
            "FunPay ID скрыт и хранится только локально."
        )
        return LotView(
            key, family, details.title[:120], attributes, details.price_minor, control.mode.value, minimum,
            (), calculation, warning, technical, managed,
        )

    def _default_minimum(self, registered: RegisteredLot) -> int | None:
        try:
            review = self.own_mapping_repository.get_by_opaque_key(opaque_lot_key(registered.details.lot_id))
        except ValueError:
            review = None
        if review is not None and review.variant is not None:
            configured = self.minimum_prices.resolve(review.variant)
            if configured is not None:
                return configured
        service_code = self._confirmed_service_code(registered.details.lot_id)
        candidates = (
            service_code, _classification_family(registered.classification.kind),
            registered.details.title, registered.classification.kind,
        )
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT value_json FROM local_setup_preferences WHERE name = 'minimum_prices'"
            ).fetchone()
        try:
            values = json.loads(row["value_json"]) if row is not None else {}
        except json.JSONDecodeError:
            values = {}
        if isinstance(values, dict):
            for candidate in candidates:
                value = values.get(candidate) if candidate else None
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return self.settings.hard_floor

    def _confirmed_service_code(self, external_lot_id: str) -> str | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT mappings.service_code FROM lot_service_mappings mappings
                JOIN service_catalog catalog ON catalog.stable_code = mappings.service_code
                WHERE mappings.external_lot_id = ? AND catalog.family = 'mythic_plus'""",
                (external_lot_id,),
            ).fetchone()
        return row["service_code"] if row is not None else None

    def _resolve_lot(self, key: str) -> RegisteredLot:
        matches = tuple(item for item in self.registry.list() if _opaque_lot_key(item.details.lot_id) == key)
        if len(matches) != 1:
            raise ValueError("lot selection is stale")
        return matches[0]

    def _update_lot_setting(self, action: str, payload: str | None) -> None:
        if not payload:
            raise ValueError("lot selection is required")
        key, separator, amount = payload.partition(":")
        lot = self._resolve_lot(key)
        if not _is_managed_mythic_plus(lot, self._confirmed_service_code(lot.details.lot_id)):
            raise RealOperationsDisabled("Этот лот не управляется ботом.")
        if action == "lot_set_fixed":
            if not separator or not amount.isdecimal():
                raise ValueError("fixed price is invalid")
            self.controls.set_fixed_price(lot.details.lot_id, int(amount) * 100)
        elif action == "lot_set_floor":
            if not separator or not amount.isdecimal():
                raise ValueError("minimum price is invalid")
            self.controls.set_minimum_price(lot.details.lot_id, int(amount) * 100)
        elif action == "lot_clear_floor":
            self.controls.set_minimum_price(lot.details.lot_id, None)
        elif action in {"lot_automatic", "lot_paused", "lot_check_only"}:
            self.controls.set_mode(lot.details.lot_id, OwnLotPricingMode(action.removeprefix("lot_")))
        else:
            raise RealOperationsDisabled("Реальные изменения FunPay пока не разрешены.")
        self.request_budgets.clear_cooldown("price_observation")

    def _update_seller(self, action: str, payload: str | None) -> None:
        if action == "seller_add" and payload:
            nickname = payload
            candidate = self._seller_candidates.pop(nickname.casefold(), None)
            if candidate is None:
                raise ValueError("seller confirmation is stale")
            seller_id, confirmed_nickname = candidate
            self.seller_repository.add_seller(
                seller_id, confirmed_nickname,
                verification_state=SellerVerificationState.VERIFIED,
            )
            self._recheck_sellers()
            self.request_budgets.clear_cooldown("price_observation")
            return
        if action == "seller_add_batch":
            if not self._seller_batch_candidates:
                raise ValueError("seller batch confirmation is stale")
            for seller_id, confirmed_nickname in self._seller_batch_candidates.values():
                self.seller_repository.add_seller(
                    seller_id, confirmed_nickname,
                    verification_state=SellerVerificationState.VERIFIED,
                )
            self._seller_batch_candidates = {}
            self._recheck_sellers()
            self.request_budgets.clear_cooldown("price_observation")
            return
        if action in {"seller_remove", "seller_disable"} and payload:
            matches = [item for item in self.seller_repository.list() if item.nickname == payload]
            if len(matches) != 1:
                raise ValueError("seller selection is stale")
            if action == "seller_remove":
                self.seller_repository.remove_seller(matches[0].seller_id)
            else:
                self.seller_repository.disable_seller(matches[0].seller_id)
            self.request_budgets.clear_cooldown("price_observation")
            return
        if action == "seller_remap" and payload:
            candidate = self._mapping_candidates.pop(payload, None)
            if candidate is None:
                raise ValueError("mapping selection is stale")
            snapshot, specs = candidate
            self.confirmations.confirm_match(snapshot, specs)
            self.request_budgets.clear_cooldown("price_observation")
            return
        raise ValueError("seller action is incomplete")

    def _update_minimum_price(self, action: str, payload: str | None) -> None:
        if action == "floor_set_global" and payload:
            self.minimum_prices.set_global(parse_minimum_price_batch(f"+1 {payload}")[1])
            self.request_budgets.clear_cooldown("price_observation")
            return
        if action == "floor_set_key_batch":
            if not self._pending_floor_batch:
                raise ValueError("minimum-price preview is stale")
            self.minimum_prices.apply_key_batch(self._pending_floor_batch)
            self._pending_floor_batch = {}
            self.request_budgets.clear_cooldown("price_observation")
            return
        if action == "floor_set_variant" and payload:
            key, separator, amount = payload.partition(":")
            if not separator:
                raise ValueError("variant minimum price is incomplete")
            review = self.own_mapping_repository.get_by_opaque_key(key)
            if review.status is not OwnMappingStatus.CONFIRMED or review.variant is None:
                raise ValueError("variant is not confirmed")
            self.minimum_prices.set_variant(
                review.variant.service_code, parse_minimum_price_batch(f"+1 {amount}")[1]
            )
            self.request_budgets.clear_cooldown("price_observation")
            return
        raise ValueError("minimum-price action is incomplete")

    def _service_specs(self) -> tuple[ServiceMatchSpec, ...]:
        lots = {item.details.lot_id: item for item in self.registry.list()}
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM lot_service_mappings").fetchall()
        by_code = {row["service_code"]: row["external_lot_id"] for row in rows}
        result: list[ServiceMatchSpec] = []
        for service in self.catalog.list():
            lot = lots.get(by_code.get(service.stable_code, ""))
            category = lot.details.category_node_id if lot is not None else None
            if not category:
                continue
            try:
                result.append(ServiceMatchSpec.from_catalog(service, category=category))
            except ValueError:
                continue
        return tuple(result)

    def _recheck_sellers(self) -> None:
        budget = self.request_budgets.claim("competitor_discovery", cooldown_seconds=30)
        if budget is not BudgetDecision.ALLOWED:
            return
        self._require_connected()
        specs = self._service_specs()
        matcher = SellerMatchingEngine()
        candidates: dict[str, tuple[CompetitorLotSnapshot, tuple[ServiceMatchSpec, ...]]] = {}
        labels: dict[str, str] = {}
        checked = exact = attention = no_match = 0
        try:
            for seller in self.seller_repository.list():
                if not seller.enabled or seller.verification_state is not SellerVerificationState.VERIFIED:
                    continue
                details = self.funpay.get_seller_lot_details(seller.seller_id)
                changed = False
                matches_by_code: dict[str, list[CompetitorLotSnapshot]] = {spec.service_code: [] for spec in specs}
                partial_mythic = False
                for detail in details:
                    snapshot = _competitor_snapshot(detail)
                    partial_mythic = partial_mythic or snapshot.key_level is not None
                    changed = self.mapping_repository.invalidate_if_materially_changed(snapshot) or changed
                    if specs:
                        assessment = matcher.match(snapshot, specs)
                        if assessment.result is MatchResult.EXACT and assessment.service_code:
                            matches_by_code[assessment.service_code].append(snapshot)
                for spec in specs:
                    checked += 1
                    matches = matches_by_code[spec.service_code]
                    if len(matches) == 1:
                        snapshot = matches[0]
                        existing = self.mapping_repository.get(snapshot.seller_id, snapshot.lot_id)
                        if existing is not None and existing.state.value == "confirmed" and existing.service_code == spec.service_code:
                            continue
                        key = "cmp-" + hashlib.sha256(
                            f"{snapshot.seller_id}|{snapshot.lot_id}|{spec.service_code}".encode()
                        ).hexdigest()[:16]
                        candidates[key] = (snapshot, specs)
                        labels[key] = f"{seller.nickname} • {_service_label(spec.service_code)}"
                        exact += 1
                    elif len(matches) > 1:
                        attention += 1
                    elif partial_mythic:
                        attention += 1
                    else:
                        no_match += 1
                self.seller_repository.set_last_checked_state(
                    seller.seller_id, SellerLastCheckedState.CHANGED if changed else SellerLastCheckedState.CURRENT
                )
        except (FunPayRateLimited, FunPayAccessDenied):
            self.request_budgets.fail("competitor_discovery", severe=True)
            raise RuntimeError("competitor discovery stopped by FunPay safety response")
        except FunPayError:
            self.request_budgets.fail("competitor_discovery")
            raise RuntimeError("competitor discovery is unavailable")
        self.request_budgets.succeed("competitor_discovery")
        self._mapping_candidates = candidates
        self._mapping_candidate_labels = labels
        self._mapping_counts = checked, exact, attention, no_match

    def _run_price_check(self, only_lot_key: str | None) -> None:
        budget = self.request_budgets.claim("price_observation", cooldown_seconds=30)
        if budget is not BudgetDecision.ALLOWED:
            if self._last_preview.changes or self._last_preview.skipped:
                return
            raise RuntimeError("price check is rate limited")
        self._require_connected()
        self._analyze_cached_own_lots()
        actual = {item.details.lot_id: item for item in self.registry.list()}
        with self.database.session() as connection:
            own_rows = connection.execute("SELECT * FROM lot_service_mappings ORDER BY service_code").fetchall()
        selected: list[tuple[RegisteredLot, str]] = []
        for row in own_rows:
            lot = actual.get(row["external_lot_id"])
            if lot is None or (only_lot_key and _opaque_lot_key(lot.details.lot_id) != only_lot_key):
                continue
            if not _is_managed_mythic_plus(lot, self._confirmed_service_code(lot.details.lot_id)):
                continue
            selected.append((lot, row["service_code"]))
        if not selected:
            self._last_preview = PricePreviewView((), (
                PriceSkipView("Управляемые лоты", "нет подтверждённых соответствий собственных лотов"),
            ))
            self._last_calculations = {}
            self.states.save("read_only_price_check", "completed", _now_label())
            self.request_budgets.succeed("price_observation")
            self._update_readiness(0, 0, dry_run_success=False)
            return
        sellers = self.seller_repository.list()
        mappings = tuple(
            item for seller in sellers for item in self.mapping_repository.list_for_seller(seller.seller_id)
        )
        enabled_sellers = tuple(
            item for item in sellers
            if item.enabled and item.verification_state is SellerVerificationState.VERIFIED
        )
        enabled_seller_ids = {item.seller_id for item in enabled_sellers}
        confirmed_mappings = tuple(
            mapping for mapping in mappings
            if mapping.state.value == "confirmed" and mapping.seller_id in enabled_seller_ids
        )
        sellers_by_service: dict[str, set[str]] = {}
        for mapping in confirmed_mappings:
            sellers_by_service.setdefault(mapping.service_code, set()).add(mapping.seller_id)
        single_seller_codes = {
            service_code for service_code, seller_ids in sellers_by_service.items() if len(seller_ids) == 1
        }
        mapped_seller_ids = {mapping.seller_id for mapping in confirmed_mappings}
        repeated_seller_ids = {
            mapping.seller_id for mapping in confirmed_mappings
            if mapping.service_code in single_seller_codes
        }
        repetitions = 3 if repeated_seller_ids else 1
        records_by_mapping: dict[tuple[str, str], list[PriceObservationRecord]] = {}
        all_new_records: list[PriceObservationRecord] = []
        next_sequences = {
            (mapping.seller_id, mapping.competitor_lot_id):
                self.observation_history.next_sequence(mapping.seller_id, mapping.competitor_lot_id)
            for mapping in confirmed_mappings
        }
        try:
            for repetition in range(repetitions):
                seller_ids_to_read = mapped_seller_ids if repetition == 0 else repeated_seller_ids
                details_by_seller: dict[str, dict[str, FunPayLotDetails]] = {}
                for seller in enabled_sellers:
                    if seller.seller_id not in seller_ids_to_read:
                        continue
                    details_by_seller[seller.seller_id] = {
                        item.lot_id: item for item in self.funpay.get_seller_lot_details(seller.seller_id)
                    }
                for mapping in confirmed_mappings:
                    if repetition > 0 and mapping.service_code not in single_seller_codes:
                        continue
                    detail = details_by_seller.get(mapping.seller_id, {}).get(mapping.competitor_lot_id)
                    if detail is None:
                        continue
                    snapshot = _competitor_snapshot(detail)
                    self.mapping_repository.invalidate_if_materially_changed(snapshot)
                    sequence_key = (mapping.seller_id, mapping.competitor_lot_id)
                    sequence = next_sequences[sequence_key]
                    next_sequences[sequence_key] += 1
                    observation_id = "obs-" + hashlib.sha256(
                        f"{mapping.seller_id}|{mapping.competitor_lot_id}|{sequence}".encode("utf-8")
                    ).hexdigest()[:24]
                    record = PriceObservationRecord(
                        observation_id,
                        TrustedPriceObservation(
                            mapping.seller_id, mapping.competitor_lot_id, mapping.service_code,
                            detail.price_minor, detail.currency,
                        ),
                        mapping.material_snapshot_hash, mapping.service_code, sequence,
                    )
                    records_by_mapping.setdefault(
                        (mapping.seller_id, mapping.competitor_lot_id), []
                    ).append(record)
                    all_new_records.append(record)
        except (FunPayRateLimited, FunPayAccessDenied, FunPaySessionExpired):
            self.request_budgets.fail("price_observation", severe=True)
            raise RuntimeError("price observation stopped by FunPay safety response")
        except FunPayError:
            self.request_budgets.fail("price_observation")
            raise RuntimeError("price observation is unavailable")
        current_records = [records[-1] for records in records_by_mapping.values() if records]
        prior_records = [record for records in records_by_mapping.values() for record in records[:-1]]
        # Invalidation may have changed mapping states; load them again before validation.
        mappings = tuple(
            item for seller in sellers for item in self.mapping_repository.list_for_seller(seller.seller_id)
        )
        history = self.observation_history.list() + tuple(prior_records)
        own_states: list[OwnLotPriceState] = []
        policies: dict[str, PricePolicy] = {}
        skipped: list[PriceSkipView] = []
        selected_by_code: dict[str, RegisteredLot] = {}
        for lot, service_code in selected:
            control = self.controls.get(lot.details.lot_id)
            minimum = control.minimum_price_minor or self._default_minimum(lot)
            if minimum is None:
                skipped.append(PriceSkipView(lot.details.title[:120], "сначала укажите минимально допустимую цену"))
                continue
            own_states.append(OwnLotPriceState(
                service_code, lot.details.price_minor, lot.details.currency, control.mode, control.fixed_price_minor
            ))
            policies[service_code] = PricePolicy(minimum, price_step_minor=100, currency=lot.details.currency)
            selected_by_code[service_code] = lot
        decisions, batch = self.pricing.batch_preview(
            tuple(own_states), sellers=sellers, mappings=mappings, records=tuple(current_records),
            history=history, policies=policies,
        )
        changes: list[PriceChangeView] = []
        calculations: dict[str, str] = {}
        ready_count = 0
        emergency = self.states.load("emergency_stop")
        emergency_active = bool(emergency and emergency[0] == "active")
        for item in decisions:
            decision, consensus = item.price_decision, item.consensus
            lot = selected_by_code[decision.service_code]
            key = _opaque_lot_key(lot.details.lot_id)
            if decision.minimum_valid_price_minor is not None:
                calculations[key] = (
                    f"{decision.minimum_valid_price_minor // 100} ₽ × 0.99 → "
                    f"{decision.final_target_minor // 100} ₽. Только расчёт; запись заблокирована."
                )
                changes.append(PriceChangeView(
                    lot.details.title[:120], decision.current_price_minor, decision.final_target_minor,
                    decision.observations[0].currency or lot.details.currency if decision.observations else lot.details.currency,
                ))
            else:
                reason = consensus.reason if consensus.status is not SafetyDecisionStatus.VALID else decision.reason
                calculations[key] = f"Цена сохранена без изменений: {reason}."
                skipped.append(PriceSkipView(lot.details.title[:120], _safe_reason(reason)))
            control = self.controls.get(lot.details.lot_id)
            review = self.own_mapping_repository.get_by_opaque_key(opaque_lot_key(lot.details.lot_id))
            competitor_current = any(
                mapping.service_code == decision.service_code and mapping.state.value == "confirmed"
                for mapping in mappings
            )
            eligibility = self.eligibility.evaluate(
                family="mythic_plus",
                own_mapping_confirmed=review.status is OwnMappingStatus.CONFIRMED,
                own_fingerprint_current=review.status is OwnMappingStatus.CONFIRMED,
                mode=control.mode,
                minimum_exists=policies[decision.service_code].hard_floor is not None,
                valid_reference_exists=decision.minimum_valid_price_minor is not None,
                competitor_mappings_current=competitor_current,
                suspicious=consensus.status is not SafetyDecisionStatus.VALID,
                session_authorized=self.health().status is FunPayReadStatus.CONNECTED,
                emergency_stop=emergency_active,
                future_live_capability_enabled=False,
            )
            ready_count += eligibility.eligible_for_future_test
        if batch.status is SafetyDecisionStatus.REJECTED:
            skipped.append(PriceSkipView("Пакет", "массовое подозрительное изменение заблокировано"))
        self._last_calculations = calculations
        self._last_preview = PricePreviewView(tuple(changes), tuple(skipped))
        self.observation_history.save(tuple(all_new_records))
        self.states.save("read_only_price_check", "completed", _now_label())
        self.request_budgets.succeed("price_observation")
        self._update_readiness(ready_count, len(selected) - ready_count, dry_run_success=True)

    def _analyze_cached_own_lots(self):
        return self.own_mapping_repository.analyze(tuple(item.details for item in self.registry.list()))

    def _mapping_labels(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._mapping_candidate_labels.items(), key=lambda item: item[1].casefold()))

    def _update_readiness(self, ready: int, blocked: int, *, dry_run_success: bool | None) -> None:
        summary = self.own_mapping_repository.summary()
        sellers = tuple(
            item for item in self.seller_repository.list()
            if item.enabled and item.verification_state is SellerVerificationState.VERIFIED
        )
        mappings = tuple(
            item for seller in sellers for item in self.mapping_repository.list_for_seller(seller.seller_id)
        )
        confirmed_mappings = sum(item.state.value == "confirmed" for item in mappings)
        recheck_mappings = sum(item.state.value != "confirmed" for item in mappings)
        variants = tuple(
            item.variant for item in summary.reviews
            if item.status is OwnMappingStatus.CONFIRMED and item.variant is not None
        )
        _, _, _, covered = self.minimum_prices.counts(variants)
        mapping_attention = summary.total - summary.confirmed - summary.excluded
        competitor_attention = recheck_mappings + self._mapping_counts[2] + self._mapping_counts[3]
        self._last_readiness = ReadinessView(
            summary.confirmed, mapping_attention, len(sellers), confirmed_mappings,
            competitor_attention, covered, ready, blocked, False,
        )
        self._persist_readiness(self._last_readiness, dry_run_success=dry_run_success)

    def _persist_readiness(self, view: ReadinessView, *, dry_run_success: bool | None = None) -> None:
        mutation_attempts = self.mutation_trap.attempts if self.mutation_trap is not None else 0
        if mutation_attempts:
            raise OnboardingMutationBlocked("read-only onboarding mutation trap was triggered")
        with self.database.session() as connection:
            existing = connection.execute(
                "SELECT dry_run_success FROM read_only_readiness_state WHERE singleton_id=1"
            ).fetchone()
            success = int(dry_run_success) if dry_run_success is not None else int(existing["dry_run_success"] if existing else 0)
            connection.execute(
                """UPDATE read_only_readiness_state SET own_lots_available=?,confirmed_own_mappings=?,
                own_mappings_attention=?,trusted_sellers=?,confirmed_competitor_mappings=?,
                competitor_mappings_attention=?,lots_with_minimum_prices=?,dry_run_success=?,
                dry_run_ready=?,dry_run_blocked=?,mutation_attempts=0,secrets_exposed=0,
                updated_at=CURRENT_TIMESTAMP WHERE singleton_id=1""",
                (
                    int(bool(self.registry.list())), view.confirmed_lots, view.mapping_attention,
                    view.trusted_sellers, view.confirmed_competitor_mappings, view.competitor_attention,
                    view.minimum_prices_covered, success, view.dry_run_ready, view.dry_run_blocked,
                ),
            )


def _positive_minor(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("price must be a positive integer in minor units")


def _observation_from_row(row: object) -> PriceObservationRecord:
    return PriceObservationRecord(
        row["observation_id"],
        TrustedPriceObservation(
            row["seller_id"], row["competitor_lot_id"], row["service_code"],
            row["price_minor"], row["currency"],
        ),
        row["lot_identity_hash"], row["structural_signature"], row["sequence"],
    )


def _health_label(status: FunPayReadStatus) -> str:
    return {
        FunPayReadStatus.CONNECTED: "🟢 Подключён",
        FunPayReadStatus.CHECKING: "🟡 Проверяем",
        FunPayReadStatus.AUTHORIZATION_REQUIRED: "🔴 Требуется авторизация",
        FunPayReadStatus.UNAVAILABLE: "⚪ Недоступен",
        FunPayReadStatus.NOT_CHECKED: "⚪ Пока не проверен",
    }[status]


def _opaque_lot_key(external_lot_id: str) -> str:
    return hashlib.sha256(f"local-own-lot:{external_lot_id}".encode("utf-8")).hexdigest()[:16]


def _classification_family(kind: str) -> str:
    return "Mythic+" if kind == "mythic_plus" else "Другие лоты"


def _seller_family_label(family: SellerFamily) -> str:
    return "Mythic+"


def _lot_attributes(lot: RegisteredLot) -> tuple[str, ...]:
    classification = lot.classification
    values: list[str] = []
    if classification.key_level is not None:
        values.append(f"+{classification.key_level}")
    if classification.region:
        values.append(classification.region.upper())
    if classification.service_format:
        values.append("Self-play" if classification.service_format == "selfplay" else "Pilot")
    if classification.package_size:
        values.append(f"x{classification.package_size}")
    return tuple(values) or ("Параметры не подтверждены",)


def _lot_warning(lot: RegisteredLot, minimum: int | None, service_code: str | None) -> str | None:
    if lot.classification.ambiguous:
        return "Не управляется ботом: параметры Mythic+ неоднозначны."
    if lot.classification.kind != "mythic_plus":
        return "Не управляется ботом."
    if lot.classification.mapping_state == "unmapped" or service_code is None:
        return "Требуется подтверждение соответствия услуги."
    if minimum is None:
        return "Минимально допустимая цена не настроена."
    return None


def _competitor_snapshot(details: FunPayLotDetails) -> CompetitorLotSnapshot:
    parsed = parse_mythic_lot(details)
    variant = parsed.variant
    family = SellerFamily.MYTHIC_PLUS if variant is not None else None
    public_fields = {
        name: value for name, value in {
            "short_description": details.short_description,
            "description": details.description,
        }.items() if value
    }
    return CompetitorLotSnapshot(
        details.seller_id, details.lot_id, details.title, family, details.category_node_id,
        variant.region if variant else None, variant.key_level if variant else None,
        variant.service_format if variant else None, variant.package_size if variant else None,
        variant.conditions if variant else None, public_fields, {},
    )


def _is_managed_mythic_plus(lot: RegisteredLot, service_code: str | None) -> bool:
    classification = lot.classification
    return (
        classification.kind == "mythic_plus"
        and classification.mapping_state == "mapped"
        and not classification.ambiguous
        and service_code is not None
    )


def _service_label(service_code: str) -> str:
    parts = service_code.replace("_", " ").replace("-", " ").split()
    return " ".join(parts[:6])[:64] or "услуга"


def _safe_reason(reason: str) -> str:
    translations = {
        "single seller needs stable consecutive observations": "нужно несколько стабильных наблюдений одного продавца",
        "all valid observations are suspicious": "ориентиры выглядят подозрительно",
        "no valid confirmed trusted observation exists": "нет валидного подтверждённого ориентира",
    }
    return translations.get(reason, "расчёт безопасно заблокирован")


def _now_label() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
