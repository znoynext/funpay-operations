"""Read-only discovery and local registry of authenticated FunPay lots.

The registry deliberately does not contain a CSRF token, auto-delivery values,
or payment-message values.  It is a local SQLite cache of the public lot data
and non-sensitive editor form fields needed to inspect a later edit proposal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, TextIO

from .database import Database
from .funpay import FunPayClient, FunPayLotDetails


LotKind = Literal["mythic_plus", "delves", "unknown"]
MappingState = Literal["mapped", "unmapped"]
TemplateKind = Literal["mythic_plus", "delves"]


@dataclass(frozen=True)
class LotClassification:
    kind: LotKind
    mapping_state: MappingState
    key_level: int | None = None
    delve_tier: int | None = None
    bountiful: bool | None = None
    region: str | None = None
    service_format: str | None = None
    package_size: int | None = None
    conditions_source: str | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "key_level": self.key_level,
            "delve_tier": self.delve_tier,
            "bountiful": self.bountiful,
            "region": self.region,
            "service_format": self.service_format,
            "package_size": self.package_size,
            "conditions_source": self.conditions_source,
        }


@dataclass(frozen=True)
class RegisteredLot:
    details: FunPayLotDetails
    classification: LotClassification


@dataclass(frozen=True)
class DiscoverySummary:
    total: int
    mythic_plus: int
    delves: int
    unmapped: int
    mythic_template_selected: bool
    delves_template_selected: bool


class OwnLotRegistryRepository:
    """Local-only SQLite persistence for the discovery result and exemplars."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def replace(self, lots: tuple[RegisteredLot, ...]) -> None:
        """Atomically replace the current account snapshot without logging it."""

        external_ids = [lot.details.lot_id for lot in lots]
        if len(external_ids) != len(set(external_ids)):
            raise ValueError("FunPay discovery returned duplicate lot ids")
        with self.database.session() as connection:
            for registered in lots:
                details = registered.details
                classification = registered.classification
                connection.execute(
                    """INSERT INTO own_lot_registry
                    (external_id, category_node_id, title, price_minor, currency, is_active, region,
                     short_description, description, location, is_deleted, editor_fields_json,
                     editor_options_json, omitted_field_names_json, available_field_names_json, classification,
                     mapping_state, service_data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        category_node_id = excluded.category_node_id, title = excluded.title,
                        price_minor = excluded.price_minor, currency = excluded.currency,
                        is_active = excluded.is_active, region = excluded.region,
                        short_description = excluded.short_description, description = excluded.description,
                        location = excluded.location, is_deleted = excluded.is_deleted,
                        editor_fields_json = excluded.editor_fields_json,
                        editor_options_json = excluded.editor_options_json,
                        omitted_field_names_json = excluded.omitted_field_names_json,
                        available_field_names_json = excluded.available_field_names_json,
                        classification = excluded.classification, mapping_state = excluded.mapping_state,
                        service_data_json = excluded.service_data_json, updated_at = CURRENT_TIMESTAMP""",
                    (
                        details.lot_id, details.category_node_id, details.title, details.price_minor,
                        details.currency, _to_database_boolean(details.is_active), classification.region,
                        details.short_description, details.description, details.location,
                        _to_database_boolean(details.is_deleted), _json(details.editor_fields),
                        _json(details.editor_options),
                        _json(details.omitted_field_names),
                        _json(tuple(sorted(set(details.editor_fields) | set(details.editor_options)))),
                        classification.kind, classification.mapping_state, _json(classification.as_dict()),
                    ),
                )
            if external_ids:
                placeholders = ", ".join("?" for _ in external_ids)
                connection.execute(f"DELETE FROM own_lot_registry WHERE external_id NOT IN ({placeholders})", external_ids)
            else:
                connection.execute("DELETE FROM own_lot_registry")

    def select_template(self, kind: TemplateKind, lot_id: str) -> None:
        if kind not in {"mythic_plus", "delves"}:
            raise ValueError("unsupported template kind")
        if not lot_id.strip():
            raise ValueError("lot id is required")
        with self.database.session() as connection:
            matching = connection.execute(
                """SELECT external_id FROM own_lot_registry
                WHERE external_id = ? AND classification = ? AND mapping_state = 'mapped'""",
                (lot_id.strip(), kind),
            ).fetchone()
            if matching is None:
                raise ValueError("the selected lot is not a mapped lot of the requested kind")
            connection.execute(
                """INSERT INTO own_lot_templates (template_kind, external_lot_id)
                VALUES (?, ?)
                ON CONFLICT(template_kind) DO UPDATE SET
                    external_lot_id = excluded.external_lot_id, updated_at = CURRENT_TIMESTAMP""",
                (kind, lot_id.strip()),
            )

    def selected_template_kinds(self) -> tuple[TemplateKind, ...]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT template_kind FROM own_lot_templates ORDER BY template_kind"
            ).fetchall()
        return tuple(row["template_kind"] for row in rows)


class LotDiscovery:
    """Normalizes a complete own-lot snapshot; this class performs no writes to FunPay."""

    def __init__(self, client: FunPayClient, registry: OwnLotRegistryRepository) -> None:
        self.client = client
        self.registry = registry

    def run(self) -> DiscoverySummary:
        registered = tuple(
            RegisteredLot(details, classify_wow_lot(details)) for details in self.client.get_own_lot_details()
        )
        self.registry.replace(registered)
        selected = set(self.registry.selected_template_kinds())
        return DiscoverySummary(
            total=len(registered),
            mythic_plus=sum(item.classification.kind == "mythic_plus" for item in registered),
            delves=sum(item.classification.kind == "delves" for item in registered),
            unmapped=sum(item.classification.mapping_state == "unmapped" for item in registered),
            mythic_template_selected="mythic_plus" in selected,
            delves_template_selected="delves" in selected,
        )


def run_discovery(
    client: FunPayClient,
    registry: OwnLotRegistryRepository,
    *,
    output: TextIO,
    mythic_template_id: str | None = None,
    delves_template_id: str | None = None,
) -> int:
    """Run local discovery and print aggregate states only."""

    try:
        summary = LotDiscovery(client, registry).run()
        if mythic_template_id is not None:
            registry.select_template("mythic_plus", mythic_template_id)
        if delves_template_id is not None:
            registry.select_template("delves", delves_template_id)
        selected = set(registry.selected_template_kinds())
    except Exception as error:
        # The CLI deliberately does not echo an external lot id or fpx error
        # text, because either can contain account-specific context.
        print(f"discover-lots: failed={error.__class__.__name__}", file=output)
        return 1
    print(
        "discover-lots: "
        f"own_lots={summary.total} mythic_plus={summary.mythic_plus} delves={summary.delves} "
        f"unmapped={summary.unmapped} mythic_template={'selected' if 'mythic_plus' in selected else 'not_selected'} "
        f"delves_template={'selected' if 'delves' in selected else 'not_selected'}",
        file=output,
    )
    return 0


def classify_wow_lot(details: FunPayLotDetails) -> LotClassification:
    """Classify only explicit, unique markers from the lot's own public text."""

    text = "\n".join(part for part in (details.title, details.short_description, details.description) if part).lower()
    has_mythic = bool(re.search(r"(?:mythic\s*\+|мифик\s*\+|миф\+|\bm\+)", text, re.IGNORECASE))
    has_delves = bool(re.search(r"(?:\bdelves?\b|вылазк\w*)", text, re.IGNORECASE))
    if has_mythic == has_delves:
        return LotClassification("unknown", "unmapped")

    region = _single_region(text)
    service_format = _single_format(text)
    package_size = _single_package_size(text)
    conditions_source = "description" if details.description else "short_description" if details.short_description else None
    if has_mythic:
        key_level = _single_number(r"\+\s*(\d{1,2})\b", text)
        complete = all(value is not None for value in (key_level, region, service_format, package_size))
        return LotClassification(
            "mythic_plus", "mapped" if complete else "unmapped", key_level=key_level,
            region=region, service_format=service_format, package_size=package_size,
            conditions_source=conditions_source,
        )

    tier = _single_number(r"\b(?:tier|t|тир)\s*([1-9]|1[0-9])\b", text)
    bountiful = _single_bountiful(text)
    complete = all(value is not None for value in (tier, bountiful, region, service_format, package_size))
    return LotClassification(
        "delves", "mapped" if complete else "unmapped", delve_tier=tier, bountiful=bountiful,
        region=region, service_format=service_format, package_size=package_size,
        conditions_source=conditions_source,
    )


def _single_region(text: str) -> str | None:
    matches = {
        code for code, pattern in {
            "eu": r"(?:\beu\b|\beurope\b|европ\w*)",
            "us": r"(?:\bus\b|\bamerica\b|сша|америк\w*)",
            "kr": r"(?:\bkr\b|коре\w*)",
            "tw": r"(?:\btw\b|тайван\w*)",
        }.items() if re.search(pattern, text, re.IGNORECASE)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _single_format(text: str) -> str | None:
    self_play = bool(re.search(r"(?:self[- ]?play|самостоятельн\w*|без\s+пилот\w*)", text, re.IGNORECASE))
    pilot = bool(re.search(r"(?:\bpilot\b|пилот\w*)", text, re.IGNORECASE))
    return "selfplay" if self_play and not pilot else "pilot" if pilot and not self_play else None


def _single_package_size(text: str) -> int | None:
    matches = {int(first or second) for first, second in re.findall(r"(?:\bx\s*(\d+)\b|\b(\d+)\s*x\b)", text, re.IGNORECASE)}
    return next(iter(matches)) if len(matches) == 1 and next(iter(matches)) > 0 else None


def _single_number(pattern: str, text: str) -> int | None:
    values = {int(value) for value in re.findall(pattern, text, re.IGNORECASE)}
    return next(iter(values)) if len(values) == 1 else None


def _single_bountiful(text: str) -> bool | None:
    bountiful = bool(re.search(r"(?:\bbountiful\b|обильн\w*)", text, re.IGNORECASE))
    normal = bool(re.search(r"(?:\bnormal\b|обычн\w*)", text, re.IGNORECASE))
    return True if bountiful and not normal else False if normal and not bountiful else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _to_database_boolean(value: bool | None) -> int | None:
    return None if value is None else int(value)
