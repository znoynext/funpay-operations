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


LotKind = Literal["mythic_plus", "unknown"]
MappingState = Literal["mapped", "unmapped"]
TemplateKind = Literal["mythic_plus"]


@dataclass(frozen=True)
class LotClassification:
    kind: LotKind
    mapping_state: MappingState
    key_level: int | None = None
    region: str | None = None
    service_format: str | None = None
    package_size: int | None = None
    conditions_source: str | None = None
    ambiguous: bool = False

    def as_dict(self) -> dict[str, object | None]:
        return {
            "key_level": self.key_level,
            "region": self.region,
            "service_format": self.service_format,
            "package_size": self.package_size,
            "conditions_source": self.conditions_source,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True)
class RegisteredLot:
    details: FunPayLotDetails
    classification: LotClassification


@dataclass(frozen=True)
class DiscoverySummary:
    total: int
    mythic_plus: int
    unknown: int
    ambiguous: int
    mythic_template_selected: bool


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
        if kind != "mythic_plus":
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
                "SELECT template_kind FROM own_lot_templates "
                "WHERE template_kind = 'mythic_plus' ORDER BY template_kind"
            ).fetchall()
        return tuple(row["template_kind"] for row in rows)

    def list(self) -> tuple[RegisteredLot, ...]:
        """Return the current local snapshot without exposing it outside the process."""

        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM own_lot_registry ORDER BY classification, external_id"
            ).fetchall()
        result: list[RegisteredLot] = []
        for row in rows:
            service_data = _json_object(row["service_data_json"])
            stored_fields = _json_object(row["editor_fields_json"])
            if not all(isinstance(value, str) for value in stored_fields.values()):
                raise ValueError("stored editor fields are malformed")
            editor_fields = {name: value for name, value in stored_fields.items() if isinstance(value, str)}
            editor_options = _editor_options(row["editor_options_json"])
            omitted_names = _json_array(row["omitted_field_names_json"])
            if not all(isinstance(value, str) for value in omitted_names):
                raise ValueError("stored omitted field names are malformed")
            details = FunPayLotDetails(
                lot_id=row["external_id"], title=row["title"], price_minor=row["price_minor"],
                currency=row["currency"], seller_id="local-owner", category_node_id=row["category_node_id"],
                is_active=_from_database_boolean(row["is_active"]), description=row["description"],
                short_description=row["short_description"], location=row["location"],
                is_deleted=_from_database_boolean(row["is_deleted"]), editor_fields=editor_fields,
                editor_options=editor_options,
                omitted_field_names=tuple(value for value in omitted_names if isinstance(value, str)),
            )
            stored_kind = row["classification"]
            stored_mapping = row["mapping_state"]
            kind: LotKind = "mythic_plus" if stored_kind == "mythic_plus" and stored_mapping == "mapped" else "unknown"
            mapping_state: MappingState = "mapped" if kind == "mythic_plus" else "unmapped"
            result.append(RegisteredLot(details, LotClassification(
                kind=kind, mapping_state=mapping_state,
                key_level=_optional_int(service_data.get("key_level")),
                region=_optional_string(service_data.get("region")),
                service_format=_optional_string(service_data.get("service_format")),
                package_size=_optional_int(service_data.get("package_size")),
                conditions_source=_optional_string(service_data.get("conditions_source")),
                ambiguous=service_data.get("ambiguous") is True,
            )))
        return tuple(result)


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
            unknown=sum(item.classification.kind == "unknown" for item in registered),
            ambiguous=sum(item.classification.ambiguous for item in registered),
            mythic_template_selected="mythic_plus" in selected,
        )


def run_discovery(
    client: FunPayClient,
    registry: OwnLotRegistryRepository,
    *,
    output: TextIO,
    mythic_template_id: str | None = None,
) -> int:
    """Run local discovery and print aggregate states only."""

    try:
        summary = LotDiscovery(client, registry).run()
        if mythic_template_id is not None:
            registry.select_template("mythic_plus", mythic_template_id)
        selected = set(registry.selected_template_kinds())
    except Exception as error:
        # The CLI deliberately does not echo an external lot id or fpx error
        # text, because either can contain account-specific context.
        print(f"discover-lots: failed={error.__class__.__name__}", file=output)
        return 1
    print(
        "discover-lots: "
        f"own_lots={summary.total} managed_mythic_plus={summary.mythic_plus} "
        f"unknown={summary.unknown} ambiguous={summary.ambiguous} "
        f"mythic_template={'selected' if 'mythic_plus' in selected else 'not_selected'}",
        file=output,
    )
    return 0


def classify_wow_lot(details: FunPayLotDetails) -> LotClassification:
    """Classify only explicit, unique markers from the lot's own public text."""

    text = "\n".join(part for part in (details.title, details.short_description, details.description) if part).lower()
    has_mythic = bool(re.search(r"(?:mythic\s*\+|мифик\s*\+|миф\+|\bm\+)", text, re.IGNORECASE))
    if not has_mythic:
        return LotClassification("unknown", "unmapped")

    region = _single_region(text)
    service_format = _single_format(text)
    package_size = _single_package_size(text)
    conditions_source = "description" if details.description else "short_description" if details.short_description else None
    key_level = _single_number(r"\+\s*(\d{1,2})\b", text)
    ambiguous = any((
        _multiple_numbers(r"\+\s*(\d{1,2})\b", text),
        _multiple_regions(text),
        _multiple_formats(text),
        _multiple_package_sizes(text),
    ))
    complete = all(value is not None for value in (key_level, region, service_format, package_size))
    return LotClassification(
        "mythic_plus" if complete and not ambiguous else "unknown",
        "mapped" if complete and not ambiguous else "unmapped",
        key_level=key_level,
        region=region, service_format=service_format, package_size=package_size,
        conditions_source=conditions_source,
        ambiguous=ambiguous,
    )


def _single_region(text: str) -> str | None:
    matches = _regions(text)
    return next(iter(matches)) if len(matches) == 1 else None


def _regions(text: str) -> set[str]:
    return {
        code for code, pattern in {
            "eu": r"(?:\beu\b|\beurope\b|европ\w*)",
            "us": r"(?:\bus\b|\bamerica\b|сша|америк\w*)",
            "kr": r"(?:\bkr\b|коре\w*)",
            "tw": r"(?:\btw\b|тайван\w*)",
        }.items() if re.search(pattern, text, re.IGNORECASE)
    }


def _multiple_regions(text: str) -> bool:
    return len(_regions(text)) > 1


def _single_format(text: str) -> str | None:
    self_play = bool(re.search(r"(?:self[- ]?play|самостоятельн\w*|без\s+пилот\w*)", text, re.IGNORECASE))
    pilot = bool(re.search(r"(?:\bpilot\b|пилот\w*)", text, re.IGNORECASE))
    return "selfplay" if self_play and not pilot else "pilot" if pilot and not self_play else None


def _multiple_formats(text: str) -> bool:
    self_play = bool(re.search(r"(?:self[- ]?play|самостоятельн\w*|без\s+пилот\w*)", text, re.IGNORECASE))
    pilot = bool(re.search(r"(?:\bpilot\b|пилот\w*)", text, re.IGNORECASE))
    return self_play and pilot


def _single_package_size(text: str) -> int | None:
    matches = {int(first or second) for first, second in re.findall(r"(?:\bx\s*(\d+)\b|\b(\d+)\s*x\b)", text, re.IGNORECASE)}
    return next(iter(matches)) if len(matches) == 1 and next(iter(matches)) > 0 else None


def _multiple_package_sizes(text: str) -> bool:
    matches = {
        int(first or second)
        for first, second in re.findall(r"(?:\bx\s*(\d+)\b|\b(\d+)\s*x\b)", text, re.IGNORECASE)
    }
    return len(matches) > 1


def _single_number(pattern: str, text: str) -> int | None:
    values = {int(value) for value in re.findall(pattern, text, re.IGNORECASE)}
    return next(iter(values)) if len(values) == 1 else None


def _multiple_numbers(pattern: str, text: str) -> bool:
    return len({int(value) for value in re.findall(pattern, text, re.IGNORECASE)}) > 1


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("stored lot metadata is malformed") from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError("stored lot metadata must be an object")
    return decoded


def _json_array(value: str) -> list[object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("stored lot metadata is malformed") from error
    if not isinstance(decoded, list):
        raise ValueError("stored lot metadata must be an array")
    return decoded


def _editor_options(value: str) -> dict[str, tuple[tuple[str, str], ...]]:
    raw = _json_object(value)
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for name, options in raw.items():
        if not isinstance(options, list):
            raise ValueError("stored editor options are malformed")
        normalized: list[tuple[str, str]] = []
        for option in options:
            if (not isinstance(option, list) or len(option) != 2
                    or not all(isinstance(item, str) for item in option)):
                raise ValueError("stored editor options are malformed")
            normalized.append((option[0], option[1]))
        result[name] = tuple(normalized)
    return result


def _from_database_boolean(value: object) -> bool | None:
    return None if value is None else bool(value)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_database_boolean(value: bool | None) -> int | None:
    return None if value is None else int(value)
