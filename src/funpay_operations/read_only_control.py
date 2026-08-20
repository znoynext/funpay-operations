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
from typing import Callable

from .config import Settings
from .database import Database
from .funpay import (
    FunPayClient,
    FunPayError,
    FunPayLotDetails,
    FunPayNetworkUnavailable,
    FunPayProtocolError,
    FunPaySessionExpired,
    RealOperationsDisabled,
)
from .lot_discovery import LotDiscovery, OwnLotRegistryRepository, RegisteredLot, classify_wow_lot
from .price_safety import PriceObservationRecord, SafetyDecisionStatus, SafetyValidatedPricingEngine
from .pricing import OwnLotPriceState, OwnLotPricingMode, PricePolicy, TrustedPriceObservation
from .repositories import TaskStateRepository
from .service_catalog import ServiceCatalogRepository
from .session_health import FunPaySessionGuard
from .telegram_views import (
    DashboardView,
    FamilyView,
    LotView,
    MappingChoiceView,
    PriceChangeView,
    PriceOverviewView,
    PricePreviewView,
    PriceSkipView,
    SellerCandidateView,
    StatusView,
    TrustedSellerView,
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

    READ_ACTIONS = frozenset({"refresh", "check_prices", "lot_check", "lot_decision", "seller_recheck"})
    LOCAL_ACTIONS = frozenset({
        "pause", "resume", "lot_automatic", "lot_paused", "lot_check_only", "lot_set_fixed",
        "lot_set_floor", "lot_clear_floor", "seller_add", "seller_remove", "seller_disable", "seller_remap",
    })
    FUNPAY_MUTATIONS = frozenset({
        "mass_lot_sync", "mass_price_update", "update_raise", "rollback", "disable_lots",
        "auto_reply_toggle", "outbound_reply", "price_writes", "lot_writes", "raise",
    })
    external_mutations_allowed = False

    def __init__(
        self, database: Database, funpay: FunPayClient, settings: Settings, states: TaskStateRepository,
        session_guard: FunPaySessionGuard, *, telegram_configured: bool, logger: logging.Logger,
        health_ttl_seconds: float = 45.0, clock: Callable[[], float] = time.monotonic,
        session_expired_callback: Callable[[], None] | None = None,
    ) -> None:
        if health_ttl_seconds <= 0:
            raise ValueError("health TTL must be positive")
        self.database, self.funpay, self.settings, self.states = database, funpay, settings, states
        self.session_guard, self.telegram_configured, self.logger = session_guard, telegram_configured, logger
        self.session_expired_callback = session_expired_callback
        self.registry = OwnLotRegistryRepository(database)
        self.controls = LotControlRepository(database)
        self.seller_repository = TrustedSellerRepository(database)
        self.mapping_repository = CompetitorLotMappingRepository(database)
        self.catalog = ServiceCatalogRepository(database)
        self.confirmations = ManualSellerConfirmationAPI(self.seller_repository, self.mapping_repository)
        self.observation_history = ReadOnlyPriceObservationRepository(database)
        self.pricing = SafetyValidatedPricingEngine()
        self.health_ttl_seconds, self.clock = health_ttl_seconds, clock
        self._health = FunPayHealth(FunPayReadStatus.NOT_CHECKED, 0)
        self._health_checking = False
        self._discovery_attempted = False
        self._seller_candidates: dict[str, tuple[str, str]] = {}
        self._mapping_candidates: dict[str, tuple[CompetitorLotSnapshot, tuple[ServiceMatchSpec, ...]]] = {}
        self._last_preview = PricePreviewView((), ())
        self._last_calculations: dict[str, str] = {}

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
        if action == "seller_recheck":
            self._recheck_sellers()
            return "checked"
        if action in {"pause", "resume"}:
            return "local-state"
        if action.startswith("lot_"):
            self._update_lot_setting(action, payload)
            return "saved-locally"
        if action.startswith("seller_"):
            self._update_seller(action, payload)
            return "saved-locally"
        raise RealOperationsDisabled("Реальные изменения FunPay пока не разрешены.")

    def dashboard(self, *, emergency_active: bool) -> DashboardView:
        health = self.health()
        if health.status is FunPayReadStatus.CONNECTED and not self.registry.list() and not self._discovery_attempted:
            self.refresh_lots()
            health = self.health()
        lots = self.registry.list()
        return DashboardView(
            (
                StatusView("Bot", "🟢 Работает"),
                StatusView("FunPay", _health_label(health.status)),
                StatusView("Telegram", "🟢 Подключён" if self.telegram_configured else "⚪ Не настроен"),
                StatusView("Automation", "⏸ Выключена"),
                StatusView("Emergency stop", "🔴 Активен" if emergency_active else "🟢 Не активен"),
            ),
            sum(_is_managed_mythic_plus(item, self._confirmed_service_code(item.details.lot_id)) for item in lots),
            sum(not _is_managed_mythic_plus(item, self._confirmed_service_code(item.details.lot_id)) for item in lots),
            "Не выполнялось: запись отключена", "Не выполнялся: raise отключён", "Не планируется",
            last_funpay_read=health.successful_read_at or "Пока не проверено",
            unknown_lots=sum(not _is_managed_mythic_plus(
                item, self._confirmed_service_code(item.details.lot_id)
            ) for item in lots),
            ambiguous_lots=sum(item.classification.ambiguous for item in lots),
        )

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
        clean = nickname.strip()
        if not clean or len(clean) > 64 or "\r" in clean or "\n" in clean:
            return None
        self._require_connected()
        matches = {
            dialog.counterparty_id for dialog in self.funpay.get_dialogs()
            if dialog.counterparty_id and dialog.counterparty_name.casefold() == clean.casefold()
        }
        if len(matches) != 1:
            return None
        stable_id = next(iter(matches))
        try:
            # fpx-engine has no supported nickname-search endpoint.  The exact
            # dialog identity supplies the stable ID; this public profile read
            # verifies that the ID is still resolvable before local confirmation.
            self.funpay.get_seller_lot_details(stable_id)
        except FunPayError:
            return None
        self._seller_candidates[clean.casefold()] = (stable_id, clean)
        return SellerCandidateView(clean, True)

    def mapping_choices(self) -> tuple[MappingChoiceView, ...]:
        return tuple(
            MappingChoiceView(label, "Точное совпадение; подтвердите вручную")
            for label in sorted(self._mapping_candidates)
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
        if self.health(force=force_health).status is not FunPayReadStatus.CONNECTED:
            return False
        try:
            summary = LotDiscovery(self.funpay, self.registry).run()
        except FunPaySessionExpired:
            self._mark_session_expired()
            self._health = FunPayHealth(FunPayReadStatus.AUTHORIZATION_REQUIRED, self.clock())
            return False
        except FunPayError:
            self._health = FunPayHealth(FunPayReadStatus.UNAVAILABLE, self.clock(), self._health.successful_read_at)
            return False
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
            return
        if action in {"seller_remove", "seller_disable"} and payload:
            matches = [item for item in self.seller_repository.list() if item.nickname == payload]
            if len(matches) != 1:
                raise ValueError("seller selection is stale")
            if action == "seller_remove":
                self.seller_repository.remove_seller(matches[0].seller_id)
            else:
                self.seller_repository.disable_seller(matches[0].seller_id)
            return
        if action == "seller_remap" and payload:
            candidate = self._mapping_candidates.pop(payload, None)
            if candidate is None:
                raise ValueError("mapping selection is stale")
            snapshot, specs = candidate
            self.confirmations.confirm_match(snapshot, specs)
            return
        raise ValueError("seller action is incomplete")

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
        self._require_connected()
        specs = self._service_specs()
        matcher = SellerMatchingEngine()
        candidates: dict[str, tuple[CompetitorLotSnapshot, tuple[ServiceMatchSpec, ...]]] = {}
        for seller in self.seller_repository.list():
            if not seller.enabled or seller.verification_state is not SellerVerificationState.VERIFIED:
                continue
            try:
                details = self.funpay.get_seller_lot_details(seller.seller_id)
            except FunPayError:
                self.seller_repository.set_last_checked_state(seller.seller_id, SellerLastCheckedState.ERROR)
                continue
            changed = False
            for index, detail in enumerate(details, start=1):
                snapshot = _competitor_snapshot(detail)
                changed = self.mapping_repository.invalidate_if_materially_changed(snapshot) or changed
                if specs:
                    assessment = matcher.match(snapshot, specs)
                    if assessment.result is MatchResult.EXACT and assessment.service_code:
                        label = f"{seller.nickname} • вариант {index} → {_service_label(assessment.service_code)}"
                        candidates[label] = (snapshot, specs)
            self.seller_repository.set_last_checked_state(
                seller.seller_id, SellerLastCheckedState.CHANGED if changed else SellerLastCheckedState.CURRENT
            )
        self._mapping_candidates = candidates

    def _run_price_check(self, only_lot_key: str | None) -> None:
        self._require_connected()
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
            return
        sellers = self.seller_repository.list()
        mappings = tuple(
            item for seller in sellers for item in self.mapping_repository.list_for_seller(seller.seller_id)
        )
        details_by_seller: dict[str, dict[str, FunPayLotDetails]] = {}
        for seller in sellers:
            if seller.enabled and seller.verification_state is SellerVerificationState.VERIFIED:
                try:
                    details_by_seller[seller.seller_id] = {
                        item.lot_id: item for item in self.funpay.get_seller_lot_details(seller.seller_id)
                    }
                except FunPayError:
                    details_by_seller[seller.seller_id] = {}
        current_records: list[PriceObservationRecord] = []
        for mapping in mappings:
            detail = details_by_seller.get(mapping.seller_id, {}).get(mapping.competitor_lot_id)
            if detail is None:
                continue
            snapshot = _competitor_snapshot(detail)
            self.mapping_repository.invalidate_if_materially_changed(snapshot)
            sequence = self.observation_history.next_sequence(mapping.seller_id, mapping.competitor_lot_id)
            observation_id = "obs-" + hashlib.sha256(
                f"{mapping.seller_id}|{mapping.competitor_lot_id}|{sequence}".encode("utf-8")
            ).hexdigest()[:24]
            current_records.append(PriceObservationRecord(
                observation_id,
                TrustedPriceObservation(
                    mapping.seller_id, mapping.competitor_lot_id, mapping.service_code,
                    detail.price_minor, detail.currency,
                ),
                mapping.material_snapshot_hash, mapping.service_code, sequence,
            ))
        # Invalidation may have changed mapping states; load them again before validation.
        mappings = tuple(
            item for seller in sellers for item in self.mapping_repository.list_for_seller(seller.seller_id)
        )
        history = self.observation_history.list()
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
        if batch.status is SafetyDecisionStatus.REJECTED:
            skipped.append(PriceSkipView("Пакет", "массовое подозрительное изменение заблокировано"))
        self._last_calculations = calculations
        self._last_preview = PricePreviewView(tuple(changes), tuple(skipped))
        self.observation_history.save(tuple(current_records))
        self.states.save("read_only_price_check", "completed", _now_label())


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
    classification = classify_wow_lot(details)
    family = SellerFamily.MYTHIC_PLUS if classification.kind == "mythic_plus" else None
    public_fields = {
        name: value for name, value in {
            "short_description": details.short_description,
            "description": details.description,
        }.items() if value
    }
    return CompetitorLotSnapshot(
        details.seller_id, details.lot_id, details.title, family, details.category_node_id,
        classification.region, classification.key_level, classification.service_format,
        classification.package_size, {}, public_fields, {},
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
