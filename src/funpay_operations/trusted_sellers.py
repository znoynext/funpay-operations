"""Local-only trusted seller matching on normalized competitor-lot snapshots.

This module has no FunPay client and does not scrape, request, or modify any
external resource.  It keeps only material snapshot hashes in SQLite so that a
later read integration can ask a human to revalidate a changed competitor lot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .database import Database
from .service_catalog import CatalogService


class SellerFamily(StrEnum):
    MYTHIC_PLUS = "mythic_plus"


class SellerVerificationState(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class SellerLastCheckedState(StrEnum):
    NEVER = "never"
    CURRENT = "current"
    CHANGED = "changed"
    ERROR = "error"


class MatchResult(StrEnum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    INCOMPATIBLE = "incompatible"
    INSUFFICIENT_DATA = "insufficient_data"


class MappingState(StrEnum):
    CONFIRMED = "confirmed"
    REVALIDATION_REQUIRED = "revalidation_required"


@dataclass(frozen=True)
class TrustedSeller:
    seller_id: str
    nickname: str
    family: SellerFamily
    enabled: bool
    verification_state: SellerVerificationState
    last_checked_state: SellerLastCheckedState


@dataclass(frozen=True)
class CompetitorLotSnapshot:
    """A caller-normalized public offer snapshot; never sent outside the process."""

    seller_id: str
    lot_id: str
    title: str
    family: SellerFamily | None
    category: str | None
    region: str | None
    key_level: int | None
    service_format: str | None
    package_size: int | None
    substantial_conditions: Mapping[str, str] | None
    form_fields: Mapping[str, str]
    form_options: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ServiceMatchSpec:
    """Complete exact-match signature for one internal service code."""

    service_code: str
    family: SellerFamily
    category: str
    region: str
    key_level: int
    service_format: str
    package_size: int
    substantial_conditions: Mapping[str, str]

    @classmethod
    def from_catalog(cls, service: CatalogService, *, category: str) -> "ServiceMatchSpec":
        if not category.strip():
            raise ValueError("category is required")
        variant = service.variant
        family = SellerFamily(service.family.value)
        region = _required_string(variant.get("region"), "catalog region")
        service_format = _required_string(variant.get("service_format"), "catalog service_format")
        package_size = _required_int(variant.get("package_size"), "catalog package_size")
        return cls(
            service.stable_code, family, category.strip(), region,
            _required_int(variant.get("key_level"), "catalog key_level"),
            service_format, package_size, _conditions(service.price_conditions),
        )


@dataclass(frozen=True)
class MatchAssessment:
    result: MatchResult
    service_code: str | None
    reason: str


@dataclass(frozen=True)
class CompetitorLotMapping:
    seller_id: str
    competitor_lot_id: str
    service_code: str
    state: MappingState
    material_snapshot_hash: str


class TrustedSellerRepository:
    """Local profile store. It deliberately contains no live account integration."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add_seller(
        self, seller_id: str, nickname: str,
        *, verification_state: SellerVerificationState = SellerVerificationState.PENDING,
    ) -> TrustedSeller:
        seller_id, nickname = _required_string(seller_id, "seller_id"), _required_string(nickname, "nickname")
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO trusted_seller_profiles
                (seller_id, nickname, family, enabled, verification_state, last_checked_state)
                VALUES (?, ?, ?, 1, ?, 'never')
                ON CONFLICT(seller_id) DO UPDATE SET nickname = excluded.nickname, family = excluded.family,
                    enabled = 1, verification_state = excluded.verification_state,
                    updated_at = CURRENT_TIMESTAMP""",
                (seller_id, nickname, SellerFamily.MYTHIC_PLUS.value, verification_state.value),
            )
        seller = self.get(seller_id)
        if seller is None:  # pragma: no cover - SQLite failure guard
            raise RuntimeError("trusted seller could not be retrieved")
        return seller

    def add_mock_seller(
        self, seller_id: str, nickname: str,
        *, verification_state: SellerVerificationState = SellerVerificationState.PENDING,
    ) -> TrustedSeller:
        """Backward-compatible test helper; production uses :meth:`add_seller`."""

        return self.add_seller(seller_id, nickname, verification_state=verification_state)

    def get(self, seller_id: str) -> TrustedSeller | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM trusted_seller_profiles WHERE seller_id = ? AND family = 'mythic_plus'",
                (seller_id,),
            ).fetchone()
        return _seller_from_row(row)

    def list(self) -> tuple[TrustedSeller, ...]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM trusted_seller_profiles WHERE family = 'mythic_plus' ORDER BY seller_id"
            ).fetchall()
        return tuple(_seller_from_row(row) for row in rows if row is not None)

    def disable_seller(self, seller_id: str) -> bool:
        return self._update_enabled(seller_id, False)

    def remove_seller(self, seller_id: str) -> bool:
        with self.database.session() as connection:
            return connection.execute(
                "DELETE FROM trusted_seller_profiles WHERE seller_id = ? AND family = 'mythic_plus'",
                (seller_id,),
            ).rowcount == 1

    def set_last_checked_state(self, seller_id: str, state: SellerLastCheckedState) -> None:
        with self.database.session() as connection:
            if connection.execute(
                "UPDATE trusted_seller_profiles SET last_checked_state = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE seller_id = ? AND family = 'mythic_plus'",
                (state.value, seller_id),
            ).rowcount != 1:
                raise KeyError("trusted seller does not exist")

    def _update_enabled(self, seller_id: str, enabled: bool) -> bool:
        with self.database.session() as connection:
            return connection.execute(
                "UPDATE trusted_seller_profiles SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE seller_id = ? AND family = 'mythic_plus'",
                (int(enabled), seller_id),
            ).rowcount == 1


class CompetitorLotMappingRepository:
    """Confirmation ledger keyed by seller and external lot ID."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, seller_id: str, competitor_lot_id: str) -> CompetitorLotMapping | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT mappings.* FROM competitor_service_mappings mappings
                JOIN service_catalog catalog ON catalog.stable_code = mappings.service_code
                WHERE mappings.seller_id = ? AND mappings.competitor_lot_id = ?
                  AND catalog.family = 'mythic_plus'""",
                (seller_id, competitor_lot_id),
            ).fetchone()
        return _mapping_from_row(row)

    def list_for_seller(self, seller_id: str) -> tuple[CompetitorLotMapping, ...]:
        with self.database.session() as connection:
            rows = connection.execute(
                """SELECT mappings.* FROM competitor_service_mappings mappings
                JOIN service_catalog catalog ON catalog.stable_code = mappings.service_code
                WHERE mappings.seller_id = ? AND catalog.family = 'mythic_plus'
                ORDER BY mappings.competitor_lot_id""",
                (seller_id,),
            ).fetchall()
        return tuple(_mapping_from_row(row) for row in rows if row is not None)

    def confirm_exact(self, snapshot: CompetitorLotSnapshot, service_code: str) -> CompetitorLotMapping:
        self._store(snapshot, service_code, MappingState.CONFIRMED)
        result = self.get(snapshot.seller_id, snapshot.lot_id)
        if result is None:  # pragma: no cover - SQLite failure guard
            raise RuntimeError("competitor mapping could not be retrieved")
        return result

    def remap_exact(self, snapshot: CompetitorLotSnapshot, service_code: str) -> CompetitorLotMapping:
        return self.confirm_exact(snapshot, service_code)

    def invalidate_if_materially_changed(self, snapshot: CompetitorLotSnapshot) -> bool:
        mapping = self.get(snapshot.seller_id, snapshot.lot_id)
        if mapping is None or mapping.material_snapshot_hash == _material_hash(snapshot):
            return False
        with self.database.session() as connection:
            connection.execute(
                """UPDATE competitor_service_mappings SET mapping_state = 'revalidation_required',
                updated_at = CURRENT_TIMESTAMP WHERE seller_id = ? AND competitor_lot_id = ?""",
                (snapshot.seller_id, snapshot.lot_id),
            )
        return True

    def _store(self, snapshot: CompetitorLotSnapshot, service_code: str, state: MappingState) -> None:
        seller_id, lot_id, service_code = (
            _required_string(snapshot.seller_id, "seller_id"), _required_string(snapshot.lot_id, "lot_id"),
            _required_string(service_code, "service_code"),
        )
        if snapshot.family is not SellerFamily.MYTHIC_PLUS:
            raise ValueError("only an exact Mythic+ competitor lot can be mapped")
        with self.database.session() as connection:
            service = connection.execute(
                "SELECT 1 FROM service_catalog WHERE stable_code = ? AND family = 'mythic_plus'",
                (service_code,),
            ).fetchone()
            if service is None:
                raise ValueError("mapping target must be an active Mythic+ service code")
            connection.execute(
                """INSERT INTO competitor_service_mappings
                (seller_id, competitor_lot_id, service_code, mapping_state, material_snapshot_hash)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(seller_id, competitor_lot_id) DO UPDATE SET service_code = excluded.service_code,
                    mapping_state = excluded.mapping_state, material_snapshot_hash = excluded.material_snapshot_hash,
                    updated_at = CURRENT_TIMESTAMP""",
                (seller_id, lot_id, service_code, state.value, _material_hash(snapshot)),
            )


class SellerMatchingEngine:
    """Strict deterministic matching: the caller decides whether to confirm an exact result."""

    def match(self, snapshot: CompetitorLotSnapshot, services: tuple[ServiceMatchSpec, ...]) -> MatchAssessment:
        if not services:
            raise ValueError("at least one service match specification is required")
        if len({service.service_code for service in services}) != len(services):
            raise ValueError("service match specifications must have unique service codes")
        missing = _missing_match_data(snapshot)
        if missing:
            return MatchAssessment(MatchResult.INSUFFICIENT_DATA, None, f"missing required fields: {', '.join(missing)}")
        candidates = tuple(service for service in services if _exactly_matches(snapshot, service))
        if not candidates:
            return MatchAssessment(MatchResult.INCOMPATIBLE, None, "no service has the same complete structured signature")
        if len(candidates) > 1:
            return MatchAssessment(MatchResult.AMBIGUOUS, None, "multiple services have the same complete structured signature")
        return MatchAssessment(MatchResult.EXACT, candidates[0].service_code, "one complete structured signature matches")


class ManualSellerConfirmationAPI:
    """Local confirmation boundary intended for a future Telegram adapter."""

    def __init__(self, sellers: TrustedSellerRepository, mappings: CompetitorLotMappingRepository,
                 matcher: SellerMatchingEngine | None = None) -> None:
        self.sellers, self.mappings, self.matcher = sellers, mappings, matcher or SellerMatchingEngine()

    def add_mock_seller(
        self, seller_id: str, nickname: str,
        *, verification_state: SellerVerificationState = SellerVerificationState.PENDING,
    ) -> TrustedSeller:
        return self.sellers.add_seller(seller_id, nickname, verification_state=verification_state)

    def disable_seller(self, seller_id: str) -> bool:
        return self.sellers.disable_seller(seller_id)

    def remove_seller(self, seller_id: str) -> bool:
        return self.sellers.remove_seller(seller_id)

    def confirm_match(self, snapshot: CompetitorLotSnapshot, services: tuple[ServiceMatchSpec, ...]) -> CompetitorLotMapping:
        self._eligible_seller(snapshot)
        assessment = self.matcher.match(snapshot, services)
        if assessment.result is not MatchResult.EXACT or assessment.service_code is None:
            raise ValueError(f"manual confirmation requires exact match, got {assessment.result.value}")
        if snapshot.family is not SellerFamily.MYTHIC_PLUS:
            raise ValueError("snapshot family does not match trusted seller family")
        mapping = self.mappings.confirm_exact(snapshot, assessment.service_code)
        self.sellers.set_last_checked_state(snapshot.seller_id, SellerLastCheckedState.CURRENT)
        return mapping

    def remap_lot(self, snapshot: CompetitorLotSnapshot, services: tuple[ServiceMatchSpec, ...]) -> CompetitorLotMapping:
        return self.confirm_match(snapshot, services)

    def observe_lot(self, snapshot: CompetitorLotSnapshot) -> bool:
        self._eligible_seller(snapshot)
        changed = self.mappings.invalidate_if_materially_changed(snapshot)
        self.sellers.set_last_checked_state(
            snapshot.seller_id, SellerLastCheckedState.CHANGED if changed else SellerLastCheckedState.CURRENT
        )
        return changed

    def _eligible_seller(self, snapshot: CompetitorLotSnapshot) -> TrustedSeller:
        seller = self.sellers.get(snapshot.seller_id)
        if seller is None:
            raise KeyError("trusted seller does not exist")
        if not seller.enabled or seller.verification_state is not SellerVerificationState.VERIFIED:
            raise ValueError("seller must be enabled and verified")
        return seller


def _missing_match_data(snapshot: CompetitorLotSnapshot) -> tuple[str, ...]:
    fields = [
        ("family", snapshot.family), ("category", snapshot.category), ("region", snapshot.region),
        ("service_format", snapshot.service_format), ("package_size", snapshot.package_size),
        ("substantial_conditions", snapshot.substantial_conditions),
    ]
    fields.append(("key_level", snapshot.key_level))
    return tuple(name for name, value in fields if value is None or value == "")


def _exactly_matches(snapshot: CompetitorLotSnapshot, service: ServiceMatchSpec) -> bool:
    return (
        snapshot.family is service.family and snapshot.category == service.category and snapshot.region == service.region
        and snapshot.service_format == service.service_format and snapshot.package_size == service.package_size
        and _conditions(snapshot.substantial_conditions) == _conditions(service.substantial_conditions)
        and snapshot.key_level == service.key_level
    )


def _material_hash(snapshot: CompetitorLotSnapshot) -> str:
    material = {
        "title": snapshot.title,
        "form_fields": _conditions(snapshot.form_fields),
        "form_options": {name: tuple(sorted(values)) for name, values in sorted(snapshot.form_options.items())},
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _conditions(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not all(isinstance(name, str) and name and isinstance(item, str) for name, item in value.items()):
        raise ValueError("conditions must have non-empty string names and string values")
    return dict(sorted(value.items()))


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _seller_from_row(row: object) -> TrustedSeller | None:
    if row is None:
        return None
    return TrustedSeller(
        row["seller_id"], row["nickname"], SellerFamily(row["family"]), bool(row["enabled"]),
        SellerVerificationState(row["verification_state"]), SellerLastCheckedState(row["last_checked_state"]),
    )


def _mapping_from_row(row: object) -> CompetitorLotMapping | None:
    if row is None:
        return None
    return CompetitorLotMapping(
        row["seller_id"], row["competitor_lot_id"], row["service_code"], MappingState(row["mapping_state"]),
        row["material_snapshot_hash"],
    )
