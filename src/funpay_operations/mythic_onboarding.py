"""Local-only Mythic+ onboarding primitives for production read-only flows.

The module parses already-read lot snapshots, stores explicit confirmations,
and prepares pricing guardrails.  It has no FunPay client and no mutation
adapter.  External identifiers remain in local SQLite and view keys are opaque.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable, Mapping

from .database import Database
from .funpay import FunPayLotDetails
from .pricing import OwnLotPricingMode


class MappingConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OwnMappingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    RECHECK_REQUIRED = "recheck_required"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class MythicVariant:
    key_level: int
    region: str
    service_format: str
    package_size: int
    conditions: Mapping[str, str]

    def __post_init__(self) -> None:
        if isinstance(self.key_level, bool) or not 1 <= self.key_level <= 1000:
            raise ValueError("key level must be from 1 to 1000")
        if self.region not in {"eu", "us", "kr", "tw"}:
            raise ValueError("unsupported region")
        if self.service_format not in {"selfplay", "pilot"}:
            raise ValueError("unsupported service format")
        if isinstance(self.package_size, bool) or not 1 <= self.package_size <= 1000:
            raise ValueError("package size must be from 1 to 1000")
        if not all(_identifier(name) and _identifier(value) for name, value in self.conditions.items()):
            raise ValueError("conditions must contain stable identifiers")

    @property
    def service_code(self) -> str:
        base = f"mplus_k{self.key_level}_{self.region}_{self.service_format}_x{self.package_size}"
        suffix = "".join(f"_{name}_{value}" for name, value in sorted(self.conditions.items()))
        candidate = base + suffix
        if len(candidate) <= 64:
            return candidate
        digest = hashlib.sha256(suffix.encode()).hexdigest()[:10]
        return f"{base[:52]}_c{digest}"

    @property
    def label(self) -> str:
        mode = "Self-play" if self.service_format == "selfplay" else "Pilot"
        conditions = "".join(f" • {value.replace('_', ' ')}" for _, value in sorted(self.conditions.items()))
        return f"+{self.key_level} • {self.region.upper()} • {mode} • x{self.package_size}{conditions}"

    def as_dict(self) -> dict[str, object]:
        return {
            "key_level": self.key_level,
            "region": self.region,
            "service_format": self.service_format,
            "package_size": self.package_size,
            "conditions": dict(sorted(self.conditions.items())),
        }


@dataclass(frozen=True)
class ParsedMythicLot:
    external_lot_id: str
    opaque_key: str
    display_title: str
    variant: MythicVariant | None
    confidence: MappingConfidence
    evidence: tuple[str, ...]
    missing_fields: tuple[str, ...]
    ambiguity_reasons: tuple[str, ...]
    material_fingerprint: str
    source_fingerprint: str
    status: OwnMappingStatus = OwnMappingStatus.CANDIDATE
    source: str = "automatic"

    @property
    def bulk_confirmable(self) -> bool:
        return (
            self.status is OwnMappingStatus.CANDIDATE
            and self.confidence is MappingConfidence.HIGH
            and self.variant is not None
            and not self.missing_fields
            and not self.ambiguity_reasons
        )


@dataclass(frozen=True)
class OwnMappingSummary:
    total: int
    high: int
    attention: int
    excluded: int
    confirmed: int
    reviews: tuple[ParsedMythicLot, ...]


def parse_mythic_lot(details: FunPayLotDetails) -> ParsedMythicLot:
    """Parse explicit critical attributes without inventing missing values."""

    sources = _lot_sources(details)
    all_text = "\n".join(value for _, value in sources).casefold()
    marker = bool(re.search(r"(?:mythic\s*\+|мифик\s*\+|миф\s*\+|\bm\s*\+|ключ|key\s*level)", all_text))
    key_values, key_sources = _key_levels(sources)
    region_values, region_sources = _regions(sources)
    format_values, format_sources = _formats(sources)
    package_values, package_sources = _packages(sources)

    values: dict[str, set[object]] = {
        "key level": set(key_values), "region": set(region_values),
        "execution mode": set(format_values), "package": set(package_values),
    }
    missing = tuple(name for name, found in values.items() if not found)
    ambiguity = tuple(f"conflicting {name}" for name, found in values.items() if len(found) > 1)
    evidence = tuple(sorted({
        *(_evidence("key level", key_sources) if len(key_values) == 1 else ()),
        *(_evidence("region", region_sources) if len(region_values) == 1 else ()),
        *(_evidence("execution mode", format_sources) if len(format_values) == 1 else ()),
        *(_evidence("package", package_sources) if len(package_values) == 1 else ()),
        *(('Mythic+ marker: explicit text/field',) if marker else ()),
    }))
    complete = marker and not missing and not ambiguity
    variant = None
    if complete:
        variant = MythicVariant(
            int(next(iter(key_values))), str(next(iter(region_values))),
            str(next(iter(format_values))), int(next(iter(package_values))),
            _material_conditions(all_text),
        )
    known = sum(bool(found) for found in values.values())
    confidence = (
        MappingConfidence.HIGH if complete else
        MappingConfidence.MEDIUM if marker and known >= 2 and not ambiguity else
        MappingConfidence.LOW
    )
    source_fingerprint = _source_fingerprint(
        details, variant,
        {name: tuple(sorted(str(value) for value in found)) for name, found in values.items()},
    )
    material = _fingerprint({"source": source_fingerprint, "variant": variant.as_dict() if variant else None})
    return ParsedMythicLot(
        details.lot_id, opaque_lot_key(details.lot_id), details.title[:120], variant, confidence,
        evidence, missing, ambiguity, material, source_fingerprint,
        status=OwnMappingStatus.CANDIDATE if marker or known >= 3 else OwnMappingStatus.EXCLUDED,
    )


def parse_manual_variant(value: str) -> MythicVariant:
    """Parse a compact correction such as ``+10 EU self-play x1``."""

    if not isinstance(value, str) or not 4 <= len(value) <= 200 or "\n" in value or "\r" in value:
        raise ValueError("correction must be one short line")
    sources = (("manual", value.casefold()),)
    keys, _ = _key_levels(sources)
    regions, _ = _regions(sources)
    formats, _ = _formats(sources)
    packages, _ = _packages(sources)
    if any(len(items) != 1 for items in (keys, regions, formats, packages)):
        raise ValueError("correction must contain one key, region, format, and package")
    return MythicVariant(
        next(iter(keys)), next(iter(regions)), next(iter(formats)), next(iter(packages)),
        _material_conditions(value.casefold()),
    )


class OwnLotMappingRepository:
    """Versioned local confirmation ledger with material-change invalidation."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def analyze(self, lots: tuple[FunPayLotDetails, ...]) -> OwnMappingSummary:
        if len({item.lot_id for item in lots}) != len(lots):
            raise ValueError("duplicate own lot identity")
        parsed = [parse_mythic_lot(item) for item in lots]
        active_codes: dict[str, list[int]] = {}
        for index, item in enumerate(parsed):
            if item.variant is not None and lots[index].is_active is not False:
                active_codes.setdefault(item.variant.service_code, []).append(index)
        for indexes in active_codes.values():
            if len(indexes) > 1:
                for index in indexes:
                    parsed[index] = replace(
                        parsed[index], confidence=MappingConfidence.LOW,
                        ambiguity_reasons=parsed[index].ambiguity_reasons + ("duplicate canonical variant",),
                    )

        with self.database.session() as connection:
            existing = {
                row["external_lot_id"]: row
                for row in connection.execute("SELECT * FROM own_lot_mapping_reviews").fetchall()
            }
            current_ids: list[str] = []
            for item in parsed:
                current_ids.append(item.external_lot_id)
                previous = existing.get(item.external_lot_id)
                if previous is not None and previous["status"] == OwnMappingStatus.CONFIRMED.value:
                    if previous["source_fingerprint"] == item.source_fingerprint:
                        item = _review_from_row(previous, display_title=item.display_title)
                        if item.variant is not None:
                            connection.execute(
                                "UPDATE own_lot_registry SET classification='mythic_plus',mapping_state='mapped',"
                                "service_data_json=?,updated_at=CURRENT_TIMESTAMP WHERE external_id=?",
                                (_json(item.variant.as_dict() | {"confidence": "high", "source": item.source}),
                                 item.external_lot_id),
                            )
                    else:
                        item = replace(item, status=OwnMappingStatus.RECHECK_REQUIRED)
                        connection.execute(
                            "DELETE FROM lot_service_mappings WHERE external_lot_id = ?", (item.external_lot_id,)
                        )
                        connection.execute(
                            "UPDATE own_lot_registry SET classification='unknown', mapping_state='unmapped' "
                            "WHERE external_id = ?", (item.external_lot_id,)
                        )
                self._store_review(connection, item)
            if current_ids:
                placeholders = ",".join("?" for _ in current_ids)
                connection.execute(
                    f"DELETE FROM own_lot_mapping_reviews WHERE external_lot_id NOT IN ({placeholders})", current_ids
                )
            else:
                connection.execute("DELETE FROM own_lot_mapping_reviews")
        return self.summary()

    def summary(self) -> OwnMappingSummary:
        reviews = self.list()
        return OwnMappingSummary(
            total=len(reviews),
            high=sum(item.bulk_confirmable for item in reviews),
            attention=sum(item.status is not OwnMappingStatus.EXCLUDED and not item.bulk_confirmable
                          and item.status is not OwnMappingStatus.CONFIRMED for item in reviews),
            excluded=sum(item.status is OwnMappingStatus.EXCLUDED for item in reviews),
            confirmed=sum(item.status is OwnMappingStatus.CONFIRMED for item in reviews),
            reviews=reviews,
        )

    def list(self) -> tuple[ParsedMythicLot, ...]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT reviews.*, registry.title AS display_title FROM own_lot_mapping_reviews reviews "
                "JOIN own_lot_registry registry ON registry.external_id=reviews.external_lot_id "
                "ORDER BY reviews.external_lot_id"
            ).fetchall()
        return tuple(_review_from_row(row) for row in rows)

    def get_by_opaque_key(self, key: str) -> ParsedMythicLot:
        matches = tuple(item for item in self.list() if item.opaque_key == key)
        if len(matches) != 1:
            raise ValueError("mapping selection is stale")
        return matches[0]

    def confirm_high_batch(self) -> int:
        candidates = tuple(item for item in self.list() if item.bulk_confirmable)
        codes = [item.variant.service_code for item in candidates if item.variant]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate canonical variant")
        with self.database.session() as connection:
            for item in candidates:
                self._confirm(connection, item)
        return len(candidates)

    def confirm_manual(self, key: str, variant: MythicVariant) -> ParsedMythicLot:
        current = self.get_by_opaque_key(key)
        candidate = replace(
            current, variant=variant, confidence=MappingConfidence.HIGH,
            evidence=("critical fields: explicitly confirmed by owner",), missing_fields=(),
            ambiguity_reasons=(), status=OwnMappingStatus.CANDIDATE, source="manual",
            material_fingerprint=_fingerprint({"source": current.source_fingerprint, "variant": variant.as_dict()}),
        )
        with self.database.session() as connection:
            duplicate = connection.execute(
                "SELECT external_lot_id FROM own_lot_mapping_reviews WHERE canonical_code=? "
                "AND external_lot_id<>? AND status='confirmed'",
                (variant.service_code, current.external_lot_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("canonical variant is already confirmed")
            self._store_review(connection, candidate)
            self._confirm(connection, candidate)
        return self.get_by_opaque_key(key)

    def _confirm(self, connection: object, item: ParsedMythicLot) -> None:
        if item.variant is None or item.ambiguity_reasons or item.missing_fields:
            raise ValueError("only a complete unambiguous Mythic+ variant can be confirmed")
        variant = item.variant
        connection.execute(
            """INSERT INTO service_catalog
            (stable_code,family,variant_json,enabled,desired_state,template_reference,
             description_profile,price_policy_reference,price_conditions_json)
            VALUES (?,'mythic_plus',?,1,'enabled','confirmed_own','safe_neutral','manual_floor',?)
            ON CONFLICT(stable_code) DO UPDATE SET variant_json=excluded.variant_json,
            price_conditions_json=excluded.price_conditions_json, updated_at=CURRENT_TIMESTAMP""",
            (
                variant.service_code,
                _json({
                    "key_level": variant.key_level,
                    "region": variant.region,
                    "service_format": variant.service_format,
                    "package_size": variant.package_size,
                }),
                _json(variant.conditions),
            ),
        )
        connection.execute(
            """INSERT INTO lot_service_mappings (external_lot_id,service_code,confirmed_at)
            VALUES (?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(external_lot_id) DO UPDATE SET service_code=excluded.service_code,
            confirmed_at=CURRENT_TIMESTAMP""",
            (item.external_lot_id, variant.service_code),
        )
        connection.execute(
            """INSERT INTO lot_control_settings
            (external_lot_id,pricing_mode,fixed_price_minor,minimum_price_minor)
            VALUES (?,'check_only',NULL,NULL)
            ON CONFLICT(external_lot_id) DO UPDATE SET pricing_mode='check_only',
            fixed_price_minor=NULL,updated_at=CURRENT_TIMESTAMP""",
            (item.external_lot_id,),
        )
        service_data = variant.as_dict() | {"confidence": item.confidence.value, "source": item.source}
        connection.execute(
            "UPDATE own_lot_registry SET classification='mythic_plus',mapping_state='mapped',"
            "service_data_json=?,updated_at=CURRENT_TIMESTAMP WHERE external_id=?",
            (_json(service_data), item.external_lot_id),
        )
        connection.execute(
            """UPDATE own_lot_mapping_reviews SET status='confirmed',confirmed_at=CURRENT_TIMESTAMP,
            mapping_version=mapping_version+1,canonical_code=?,variant_json=?,confidence='high',
            missing_fields_json='[]',ambiguity_json='[]',material_fingerprint=?,source=?,updated_at=CURRENT_TIMESTAMP
            WHERE external_lot_id=?""",
            (variant.service_code, _json(variant.as_dict()), item.material_fingerprint, item.source,
             item.external_lot_id),
        )

    @staticmethod
    def _store_review(connection: object, item: ParsedMythicLot) -> None:
        connection.execute(
            """INSERT INTO own_lot_mapping_reviews
            (external_lot_id,opaque_key,canonical_code,variant_json,confidence,status,evidence_json,
             missing_fields_json,ambiguity_json,material_fingerprint,source_fingerprint,source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(external_lot_id) DO UPDATE SET opaque_key=excluded.opaque_key,
            canonical_code=excluded.canonical_code,variant_json=excluded.variant_json,
            confidence=excluded.confidence,status=excluded.status,evidence_json=excluded.evidence_json,
            missing_fields_json=excluded.missing_fields_json,ambiguity_json=excluded.ambiguity_json,
            material_fingerprint=excluded.material_fingerprint,source_fingerprint=excluded.source_fingerprint,
            source=excluded.source,updated_at=CURRENT_TIMESTAMP""",
            (
                item.external_lot_id, item.opaque_key, item.variant.service_code if item.variant else None,
                _json(item.variant.as_dict()) if item.variant else None, item.confidence.value, item.status.value,
                _json(item.evidence), _json(item.missing_fields), _json(item.ambiguity_reasons),
                item.material_fingerprint, item.source_fingerprint, item.source,
            ),
        )


class MinimumPriceRepository:
    """Explicit owner-provided floors with variant > key > global precedence."""

    GLOBAL_KEY = "mythic_plus"

    def __init__(self, database: Database) -> None:
        self.database = database

    def set_global(self, amount_minor: int) -> None:
        self._set("global", self.GLOBAL_KEY, amount_minor)

    def set_key(self, key_level: int, amount_minor: int) -> None:
        if isinstance(key_level, bool) or not 1 <= key_level <= 1000:
            raise ValueError("invalid key level")
        self._set("key", str(key_level), amount_minor)

    def set_variant(self, service_code: str, amount_minor: int) -> None:
        if not _identifier(service_code):
            raise ValueError("invalid service code")
        self._set("variant", service_code, amount_minor)

    def resolve(self, variant: MythicVariant) -> int | None:
        keys = (("variant", variant.service_code), ("key", str(variant.key_level)), ("global", self.GLOBAL_KEY))
        with self.database.session() as connection:
            for scope, key in keys:
                row = connection.execute(
                    "SELECT amount_minor FROM mythic_minimum_prices WHERE scope=? AND scope_key=?",
                    (scope, key),
                ).fetchone()
                if row is not None:
                    return int(row["amount_minor"])
        return None

    def counts(self, variants: Iterable[MythicVariant]) -> tuple[int, int, int, int]:
        with self.database.session() as connection:
            rows = connection.execute("SELECT scope,scope_key FROM mythic_minimum_prices").fetchall()
        global_count = sum(row["scope"] == "global" for row in rows)
        key_count = sum(row["scope"] == "key" for row in rows)
        variant_count = sum(row["scope"] == "variant" for row in rows)
        covered = sum(self.resolve(variant) is not None for variant in variants)
        return global_count, key_count, variant_count, covered

    def apply_key_batch(self, values: Mapping[int, int]) -> None:
        if not values:
            raise ValueError("minimum-price batch is empty")
        with self.database.session() as connection:
            for key, amount in sorted(values.items()):
                _validate_minor(amount)
                if isinstance(key, bool) or not 1 <= key <= 1000:
                    raise ValueError("invalid key level")
                connection.execute(
                    """INSERT INTO mythic_minimum_prices(scope,scope_key,amount_minor,currency)
                    VALUES('key',?,?,'RUB') ON CONFLICT(scope,scope_key) DO UPDATE SET
                    amount_minor=excluded.amount_minor,updated_at=CURRENT_TIMESTAMP""",
                    (str(key), amount),
                )

    def _set(self, scope: str, key: str, amount_minor: int) -> None:
        _validate_minor(amount_minor)
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO mythic_minimum_prices(scope,scope_key,amount_minor,currency)
                VALUES(?,?,?,'RUB') ON CONFLICT(scope,scope_key) DO UPDATE SET
                amount_minor=excluded.amount_minor,updated_at=CURRENT_TIMESTAMP""",
                (scope, key, amount_minor),
            )


def parse_minimum_price_batch(value: str) -> dict[int, int]:
    if not isinstance(value, str) or not 1 <= len(value) <= 4000:
        raise ValueError("minimum-price batch is invalid")
    result: dict[int, int] = {}
    for raw in value.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.fullmatch(r"\+?(\d{1,3})\s+([0-9][0-9 ]*(?:[.,][0-9]{1,2})?)\s*(?:₽|rub)?", line, re.I)
        if match is None:
            raise ValueError("each line must contain a key level and price")
        key = int(match.group(1))
        amount = _rubles_to_minor(match.group(2))
        if key in result:
            raise ValueError("duplicate key level")
        result[key] = amount
    if not result:
        raise ValueError("minimum-price batch is empty")
    return result


def parse_nickname_batch(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValueError("seller list is invalid")
    values: list[str] = []
    seen: set[str] = set()
    for raw in value.splitlines():
        nickname = raw.strip()
        if not nickname:
            continue
        if len(nickname) > 64 or any(char in nickname for char in "\r\n\t"):
            raise ValueError("each nickname must be a short single line")
        folded = nickname.casefold()
        if folded in seen:
            raise ValueError("duplicate seller nickname")
        seen.add(folded)
        values.append(nickname)
    if not values or len(values) > 20:
        raise ValueError("seller list must contain from 1 to 20 unique nicknames")
    return tuple(values)


class BudgetDecision(StrEnum):
    ALLOWED = "allowed"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"


class ReadOnlyRequestBudgetRepository:
    """Persistent semantic cooldowns above the adapter's wire-level pacing."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(self, action: str, *, cooldown_seconds: int, now: datetime | None = None) -> BudgetDecision:
        if not _identifier(action) or cooldown_seconds < 0:
            raise ValueError("invalid request budget")
        instant = (now or datetime.now(UTC)).replace(microsecond=0)
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM read_only_request_budgets WHERE action=?", (action,)
            ).fetchone()
            if row is not None:
                circuit = _datetime(row["circuit_until"])
                if circuit is not None and instant < circuit:
                    return BudgetDecision.CIRCUIT_OPEN
                last = _datetime(row["last_started_at"])
                if last is not None and instant < last + timedelta(seconds=cooldown_seconds):
                    return BudgetDecision.COOLDOWN
            connection.execute(
                """INSERT INTO read_only_request_budgets(action,last_started_at,failure_count)
                VALUES(?,?,0) ON CONFLICT(action) DO UPDATE SET last_started_at=excluded.last_started_at""",
                (action, instant.isoformat()),
            )
        return BudgetDecision.ALLOWED

    def succeed(self, action: str, *, now: datetime | None = None) -> None:
        instant = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "UPDATE read_only_request_budgets SET last_completed_at=?,failure_count=0,circuit_until=NULL "
                "WHERE action=?", (instant, action),
            )

    def fail(self, action: str, *, severe: bool = False, now: datetime | None = None) -> None:
        instant = (now or datetime.now(UTC)).replace(microsecond=0)
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT failure_count FROM read_only_request_budgets WHERE action=?", (action,)
            ).fetchone()
            failures = (int(row["failure_count"]) if row else 0) + 1
            circuit = instant + timedelta(seconds=60) if severe or failures >= 3 else None
            connection.execute(
                """INSERT INTO read_only_request_budgets(action,last_started_at,failure_count,circuit_until)
                VALUES(?,?,?,?) ON CONFLICT(action) DO UPDATE SET failure_count=excluded.failure_count,
                circuit_until=excluded.circuit_until""",
                (action, instant.isoformat(), failures, circuit.isoformat() if circuit else None),
            )

    def clear_cooldown(self, action: str) -> None:
        if not _identifier(action):
            raise ValueError("invalid request budget")
        with self.database.session() as connection:
            connection.execute(
                "UPDATE read_only_request_budgets SET last_started_at=NULL WHERE action=? AND circuit_until IS NULL",
                (action,),
            )


@dataclass(frozen=True)
class PreLiveEligibility:
    eligible_for_future_test: bool
    live_write_enabled: bool
    blockers: tuple[str, ...]


class PreLiveEligibilityGuard:
    """Central fail-closed guard.  Live capability is deliberately impossible."""

    def evaluate(
        self, *, family: str, own_mapping_confirmed: bool, own_fingerprint_current: bool,
        mode: OwnLotPricingMode, minimum_exists: bool, valid_reference_exists: bool,
        competitor_mappings_current: bool, suspicious: bool, session_authorized: bool,
        emergency_stop: bool, future_live_capability_enabled: bool = False,
    ) -> PreLiveEligibility:
        blockers: list[str] = []
        checks = (
            (family == "mythic_plus", "service is not Mythic+"),
            (own_mapping_confirmed, "own mapping is not confirmed"),
            (own_fingerprint_current, "own mapping requires recheck"),
            (mode is not OwnLotPricingMode.PAUSED, "lot is paused"),
            (minimum_exists, "minimum price is not configured"),
            (valid_reference_exists, "no valid trusted reference"),
            (competitor_mappings_current, "competitor mapping requires recheck"),
            (not suspicious, "pricing decision is suspicious"),
            (session_authorized, "FunPay session is not authorized"),
            (not emergency_stop, "emergency stop is active"),
        )
        blockers.extend(reason for passed, reason in checks if not passed)
        # This release ignores a truthy caller value. Enabling live writes
        # requires a separate code change and explicit future authorization.
        del future_live_capability_enabled
        return PreLiveEligibility(not blockers, False, tuple(blockers + ["live price capability is disabled"]))


def opaque_lot_key(external_lot_id: str) -> str:
    return hashlib.sha256(f"local-own-lot:{external_lot_id}".encode()).hexdigest()[:16]


def _lot_sources(details: FunPayLotDetails) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for name, value in (
        ("title", details.title), ("short description", details.short_description),
        ("description", details.description), ("location", details.location),
    ):
        if value:
            result.append((name, str(value)))
    for field, selected in sorted(details.editor_fields.items()):
        if _sensitive_field(field):
            continue
        result.append(("structured field", f"{field} {selected}"))
        labels = [label for label, option_value in details.editor_options.get(field, ()) if option_value == selected]
        result.extend(("structured option", label) for label in labels if label)
    return tuple(result)


def _key_levels(sources: Iterable[tuple[str, str]]) -> tuple[set[int], set[str]]:
    values: set[int] = set()
    evidence: set[str] = set()
    for source, text in sources:
        found = {int(value) for value in re.findall(r"(?:\+\s*|(?:key|ключ)\D{0,16})(\d{1,3})\b", text, re.I)}
        if source.startswith("structured") and re.search(r"(?:key|ключ|level|уров)", text, re.I):
            found.update(int(value) for value in re.findall(r"\b(\d{1,3})\b", text))
        if found:
            values.update(value for value in found if 1 <= value <= 1000)
            evidence.add(source)
    return values, evidence


def _regions(sources: Iterable[tuple[str, str]]) -> tuple[set[str], set[str]]:
    patterns = {
        "eu": r"(?:\beu\b|\beurope\b|европ\w*)", "us": r"(?:\bus\b|\bna\b|america|сша|америк\w*)",
        "kr": r"(?:\bkr\b|коре\w*)", "tw": r"(?:\btw\b|тайван\w*)",
    }
    return _dimension_matches(sources, patterns)


def _formats(sources: Iterable[tuple[str, str]]) -> tuple[set[str], set[str]]:
    patterns = {
        "selfplay": r"(?:self[- ]?play|самостоятель\w*|без\s+пилот\w*|сво(?:им|его)\s+персонаж)",
        "pilot": r"(?:\bpilot\b|пилот\w*|передач\w+\s+аккаунт|на\s+вашем\s+аккаунт)",
    }
    return _dimension_matches(sources, patterns)


def _packages(sources: Iterable[tuple[str, str]]) -> tuple[set[int], set[str]]:
    values: set[int] = set()
    evidence: set[str] = set()
    for source, text in sources:
        found = {
            int(first or second or third)
            for first, second, third in re.findall(
                r"(?:\bx\s*(\d{1,3})\b|\b(\d{1,3})\s*x\b|\b(\d{1,3})\s*(?:runs?|проход\w*|забег\w*))",
                text, re.I,
            )
        }
        if source.startswith("structured") and re.search(r"(?:runs?|package|quantity|колич|проход|забег)", text, re.I):
            found.update(int(value) for value in re.findall(r"\b(\d{1,3})\b", text))
        if found:
            values.update(value for value in found if 1 <= value <= 1000)
            evidence.add(source)
    return values, evidence


def _dimension_matches(
    sources: Iterable[tuple[str, str]], patterns: Mapping[str, str]
) -> tuple[set[str], set[str]]:
    values: set[str] = set()
    evidence: set[str] = set()
    for source, text in sources:
        found = {name for name, pattern in patterns.items() if re.search(pattern, text, re.I)}
        if found:
            values.update(found)
            evidence.add(source)
    return values, evidence


def _material_conditions(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if re.search(r"(?:any dungeon|любой данж|любое подземель)", text, re.I):
        result["dungeon"] = "any"
    elif re.search(r"(?:specific dungeon|конкретн\w+ (?:данж|подземель))", text, re.I):
        result["dungeon"] = "specific"
    if re.search(r"(?:in[- ]?time|в тайм|вовремя)", text, re.I):
        result["timing"] = "in_time"
    if re.search(r"(?:rating|рейтин|push|пуш)", text, re.I):
        result["goal"] = "rating_push"
    if re.search(r"(?:weekly|vault|недельн|хранилищ)", text, re.I):
        result["reward"] = "weekly_vault"
    return result


def _source_fingerprint(
    details: FunPayLotDetails, variant: MythicVariant | None, signals: Mapping[str, tuple[str, ...]]
) -> str:
    selected = {
        name: value for name, value in sorted(details.editor_fields.items())
        if not _sensitive_field(name) and not re.search(r"(?:summary|desc|опис|price|цена|image)", name, re.I)
    }
    return _fingerprint({
        "category": details.category_node_id,
        "selected_fields": selected,
        "variant": variant.as_dict() if variant else None,
        "critical_signals": dict(sorted(signals.items())),
    })


def _review_from_row(row: object, *, display_title: str | None = None) -> ParsedMythicLot:
    variant_raw = _json_value(row["variant_json"], None)
    variant = None
    if isinstance(variant_raw, dict):
        variant = MythicVariant(
            int(variant_raw["key_level"]), str(variant_raw["region"]), str(variant_raw["service_format"]),
            int(variant_raw["package_size"]),
            {str(k): str(v) for k, v in dict(variant_raw.get("conditions", {})).items()},
        )
    return ParsedMythicLot(
        row["external_lot_id"], row["opaque_key"], display_title or row["display_title"], variant,
        MappingConfidence(row["confidence"]), tuple(_json_value(row["evidence_json"], [])),
        tuple(_json_value(row["missing_fields_json"], [])), tuple(_json_value(row["ambiguity_json"], [])),
        row["material_fingerprint"], row["source_fingerprint"], OwnMappingStatus(row["status"]), row["source"],
    )


def _evidence(dimension: str, sources: set[str]) -> tuple[str, ...]:
    return tuple(f"{dimension}: {source}" for source in sorted(sources))


def _sensitive_field(name: str) -> bool:
    return bool(re.search(r"(?:secret|payment|csrf|token|cookie|golden)", name, re.I))


def _identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", value))


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: str | None, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _rubles_to_minor(value: str) -> int:
    normalized = value.replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as error:
        raise ValueError("price is invalid") from error
    if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
        raise ValueError("price is outside the supported range")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError("price must have at most two decimal places")
    result = int(minor)
    _validate_minor(result)
    return result


def _validate_minor(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000_000:
        raise ValueError("price must be a positive integer in minor units")


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=UTC)
