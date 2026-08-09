"""Local, configurable service-catalog generation for future lot management.

The catalog knows neither FunPay nor Telegram.  It expands a local JSON
definition into deterministic service variants and can persist only those
derived local records in SQLite.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, TextIO

from .database import Database


class CatalogFamily(StrEnum):
    MYTHIC_PLUS = "mythic_plus"
    DELVES = "delves"


class DesiredState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RESERVED_CONDITIONS = {"region", "format", "package", "key_level", "tier", "mode"}


@dataclass(frozen=True)
class CatalogService:
    stable_code: str
    family: CatalogFamily
    variant: Mapping[str, object]
    enabled: bool
    desired_state: DesiredState
    template_reference: str
    description_profile: str
    price_policy_reference: str
    price_conditions: Mapping[str, str]


class ServiceCatalogValidationError(ValueError):
    """Raised when a local catalog definition cannot form safe variants."""


class DuplicateStableCodeError(ServiceCatalogValidationError):
    """Raised before duplicate variants can enter the local registry."""


class ServiceCatalogRepository:
    """Transactional local persistence; it has no external side effects."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def replace(self, services: tuple[CatalogService, ...]) -> None:
        _validate_services(services)
        codes = [service.stable_code for service in services]
        with self.database.session() as connection:
            for service in services:
                connection.execute(
                    """INSERT INTO service_catalog
                    (stable_code, family, variant_json, enabled, desired_state, template_reference,
                     description_profile, price_policy_reference, price_conditions_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stable_code) DO UPDATE SET
                        family = excluded.family, variant_json = excluded.variant_json,
                        enabled = excluded.enabled, desired_state = excluded.desired_state,
                        template_reference = excluded.template_reference,
                        description_profile = excluded.description_profile,
                        price_policy_reference = excluded.price_policy_reference,
                        price_conditions_json = excluded.price_conditions_json,
                        updated_at = CURRENT_TIMESTAMP""",
                    (
                        service.stable_code, service.family.value, _json(service.variant), int(service.enabled),
                        service.desired_state.value, service.template_reference, service.description_profile,
                        service.price_policy_reference, _json(service.price_conditions),
                    ),
                )
            if codes:
                placeholders = ", ".join("?" for _ in codes)
                connection.execute(f"DELETE FROM service_catalog WHERE stable_code NOT IN ({placeholders})", codes)
            else:
                connection.execute("DELETE FROM service_catalog")

    def list(self) -> tuple[CatalogService, ...]:
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM service_catalog ORDER BY stable_code").fetchall()
        return tuple(
            CatalogService(
                stable_code=row["stable_code"], family=CatalogFamily(row["family"]),
                variant=_json_object(row["variant_json"]), enabled=bool(row["enabled"]),
                desired_state=DesiredState(row["desired_state"]), template_reference=row["template_reference"],
                description_profile=row["description_profile"], price_policy_reference=row["price_policy_reference"],
                price_conditions=_json_object(row["price_conditions_json"]),
            )
            for row in rows
        )


def load_catalog_definition(path: Path) -> Mapping[str, object]:
    try:
        return _json_object(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ServiceCatalogValidationError("local catalog definition is unavailable") from error


def generate_catalog(definition: Mapping[str, object]) -> tuple[CatalogService, ...]:
    if definition.get("version") != 1:
        raise ServiceCatalogValidationError("catalog version must be 1")
    mythic_raw = definition.get("mythic_plus")
    delves_raw = definition.get("delves")
    if mythic_raw is None and delves_raw is None:
        raise ServiceCatalogValidationError("choose at least one service family")
    services = (
        *(_generate_mythic_plus(_mapping(mythic_raw, "mythic_plus")) if mythic_raw is not None else ()),
        *(_generate_delves(_mapping(delves_raw, "delves")) if delves_raw is not None else ()),
    )
    _validate_services(services)
    return tuple(sorted(services, key=lambda service: service.stable_code))


def init_example(*, example_path: Path, catalog_path: Path, database_path: Path) -> tuple[CatalogService, ...]:
    """Copy only the safe fixture to ignored local data and seed local SQLite."""

    if catalog_path.exists():
        raise FileExistsError("local catalog already exists; refusing to overwrite it")
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, catalog_path)
    services = generate_catalog(load_catalog_definition(catalog_path))
    database = Database(database_path)
    database.initialize()
    ServiceCatalogRepository(database).replace(services)
    return services


def run_catalog_command(
    action: str, *, catalog_path: Path, database_path: Path, example_path: Path, output: TextIO
) -> int:
    if action == "init-example":
        try:
            services = init_example(example_path=example_path, catalog_path=catalog_path, database_path=database_path)
        except (FileExistsError, ServiceCatalogValidationError) as error:
            print(f"catalog init-example: failed={error.__class__.__name__}", file=output)
            return 1
        print(f"catalog init-example: local_file=created services={len(services)}", file=output)
        return 0
    try:
        services = generate_catalog(load_catalog_definition(catalog_path))
    except ServiceCatalogValidationError as error:
        print(f"catalog {action}: failed={error.__class__.__name__}", file=output)
        return 1
    if action == "validate":
        print(f"catalog validate: valid services={len(services)}", file=output)
        return 0
    if action == "preview":
        families = {
            family.value: sum(service.family is family for service in services)
            for family in CatalogFamily
        }
        print(
            f"catalog preview: services={len(services)} mythic_plus={families['mythic_plus']} delves={families['delves']}",
            file=output,
        )
        for service in services:
            print(service.stable_code, file=output)
        return 0
    raise ValueError("unsupported catalog action")


def default_example_path() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "service_catalog.example.json"


def _generate_mythic_plus(config: Mapping[str, object]) -> tuple[CatalogService, ...]:
    common = _common(config, CatalogFamily.MYTHIC_PLUS)
    minimum, maximum = _range(config, "min_key_level", "max_key_level")
    formats = _formats(config, required=True)
    return tuple(
        _service(
            common, {"key_level": level, "region": region, "service_format": service_format, "package_size": package},
            f"mplus_k{level}_{region}_{service_format}_x{package}", conditions,
        )
        for level, region, service_format, package, conditions in itertools.product(
            range(minimum, maximum + 1), common["regions"], formats, common["package_sizes"], _condition_combinations(common["conditions"]),
        )
    )


def _generate_delves(config: Mapping[str, object]) -> tuple[CatalogService, ...]:
    common = _common(config, CatalogFamily.DELVES)
    minimum, maximum = _range(config, "min_tier", "max_tier")
    modes = _choices(config.get("modes"), "modes", {"normal", "bountiful"}, required=True)
    formats = _formats(config, required=False) or ("not_applicable",)
    return tuple(
        _service(
            common, {"tier": tier, "mode": mode, "region": region, "service_format": service_format, "package_size": package},
            f"delve_t{tier}_{mode}_{region}_{service_format}_x{package}", conditions,
        )
        for tier, mode, region, service_format, package, conditions in itertools.product(
            range(minimum, maximum + 1), modes, common["regions"], formats, common["package_sizes"], _condition_combinations(common["conditions"]),
        )
    )


def _common(config: Mapping[str, object], family: CatalogFamily) -> dict[str, object]:
    regions = _identifiers(config.get("regions"), "regions", required=True)
    package_sizes = _package_sizes(config.get("package_sizes"))
    return {
        "family": family,
        "regions": regions,
        "package_sizes": package_sizes,
        "conditions": _conditions(config.get("price_conditions", {})),
        "enabled": _bool(config.get("enabled", True), "enabled"),
        "desired_state": DesiredState(_required_identifier(config.get("desired_state"), "desired_state")),
        "template_reference": _required_identifier(config.get("template_reference"), "template_reference"),
        "description_profile": _required_identifier(config.get("description_profile"), "description_profile"),
        "price_policy_reference": _required_identifier(config.get("price_policy_reference"), "price_policy_reference"),
    }


def _service(common: Mapping[str, object], variant: Mapping[str, object], base_code: str, conditions: Mapping[str, str]) -> CatalogService:
    suffix = "".join(f"_{name}_{value}" for name, value in sorted(conditions.items()))
    return CatalogService(
        stable_code=base_code + suffix, family=common["family"], variant=dict(variant), enabled=common["enabled"],
        desired_state=common["desired_state"], template_reference=common["template_reference"],
        description_profile=common["description_profile"], price_policy_reference=common["price_policy_reference"],
        price_conditions=dict(conditions),
    )


def _range(config: Mapping[str, object], minimum_name: str, maximum_name: str) -> tuple[int, int]:
    minimum, maximum = config.get(minimum_name), config.get(maximum_name)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1000 for value in (minimum, maximum)):
        raise ServiceCatalogValidationError(f"{minimum_name} and {maximum_name} must be integers from 1 to 1000")
    if minimum > maximum:
        raise ServiceCatalogValidationError(f"{minimum_name} must not exceed {maximum_name}")
    return minimum, maximum


def _formats(config: Mapping[str, object], *, required: bool) -> tuple[str, ...]:
    return _choices(config.get("service_formats", []), "service_formats", {"selfplay", "pilot"}, required=required)


def _choices(value: object, field: str, allowed: set[str], *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise ServiceCatalogValidationError(f"{field} must be {'a non-empty ' if required else 'a '}list")
    if not all(isinstance(item, str) and item in allowed for item in value):
        raise ServiceCatalogValidationError(f"{field} contains an unsupported value")
    if len(value) != len(set(value)):
        raise ServiceCatalogValidationError(f"{field} must not contain duplicates")
    return tuple(sorted(value))


def _identifiers(value: object, field: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise ServiceCatalogValidationError(f"{field} must be a non-empty list")
    if not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in value):
        raise ServiceCatalogValidationError(f"{field} must contain stable identifiers")
    if len(value) != len(set(value)):
        raise ServiceCatalogValidationError(f"{field} must not contain duplicates")
    return tuple(sorted(value))


def _package_sizes(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ServiceCatalogValidationError("package_sizes must be a non-empty list")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 1000 for item in value):
        raise ServiceCatalogValidationError("package_sizes must contain integers from 1 to 1000")
    if len(value) != len(set(value)):
        raise ServiceCatalogValidationError("package_sizes must not contain duplicates")
    if 1 not in value:
        raise ServiceCatalogValidationError("package_sizes must include x1")
    return tuple(sorted(value))


def _conditions(value: object) -> Mapping[str, tuple[str, ...]]:
    config = _mapping(value, "price_conditions")
    result: dict[str, tuple[str, ...]] = {}
    for name, values in config.items():
        normalized_name = _required_identifier(name, "price condition name")
        if normalized_name in _RESERVED_CONDITIONS:
            raise ServiceCatalogValidationError("price condition name conflicts with a service variant")
        result[normalized_name] = _identifiers(values, f"price condition {normalized_name}", required=True)
    return result


def _condition_combinations(conditions: Mapping[str, tuple[str, ...]]) -> tuple[Mapping[str, str], ...]:
    if not conditions:
        return ({},)
    names = tuple(sorted(conditions))
    return tuple(dict(zip(names, values)) for values in itertools.product(*(conditions[name] for name in names)))


def _validate_services(services: tuple[CatalogService, ...]) -> None:
    codes = [service.stable_code for service in services]
    if len(codes) != len(set(codes)):
        raise DuplicateStableCodeError("catalog generated duplicate stable codes")
    if not services:
        raise ServiceCatalogValidationError("catalog must generate at least one service")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ServiceCatalogValidationError(f"{field} must be an object")
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ServiceCatalogValidationError(f"{field} must be a boolean")
    return value


def _required_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ServiceCatalogValidationError(f"{field} must be a stable identifier")
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ServiceCatalogValidationError("catalog JSON must be valid") from error
    return _mapping(parsed, "catalog JSON")
