"""Serializable Mythic+ service models with stable internal codes.

These models deliberately describe an offer only. They do not create or change
FunPay lots and have no network or persistence dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping, TypeVar


class Region(str, Enum):
    EU = "eu"
    US = "us"
    KR = "kr"
    TW = "tw"


class ServiceFormat(str, Enum):
    SELF_PLAY = "selfplay"
    PILOT = "pilot"


_CONDITION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONDITION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_PACKAGE_RUNS = 20


class DuplicateServiceError(ValueError):
    """Raised when a catalog already contains the same regional offer."""


def _validated_conditions(value: Mapping[str, str] | tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    items = tuple(value.items()) if isinstance(value, Mapping) else tuple(value)
    normalized: list[tuple[str, str]] = []
    names: set[str] = set()
    for name, condition in items:
        if not isinstance(name, str) or not _CONDITION_NAME.fullmatch(name):
            raise ValueError("price condition names must use lowercase stable identifiers")
        if not isinstance(condition, str) or not _CONDITION_VALUE.fullmatch(condition):
            raise ValueError("price condition values contain unsupported characters")
        if name in names:
            raise ValueError("price condition names must be unique")
        names.add(name)
        normalized.append((name, condition))
    return tuple(sorted(normalized))


def _validated_package_runs(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PACKAGE_RUNS:
        raise ValueError(f"runs must be an integer from 1 to {_MAX_PACKAGE_RUNS}")
    return value


def _validated_enum(value: Any, enum_type: type[Enum], field: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field} must be a {enum_type.__name__}")


@dataclass(frozen=True)
class MythicPlusService:
    """A Mythic+ service, either one run or a bounded package of runs."""

    key_level: int
    region: Region
    service_format: ServiceFormat
    runs: int = 1
    price_conditions: Mapping[str, str] | tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.key_level, bool) or not isinstance(self.key_level, int) or not 2 <= self.key_level <= 30:
            raise ValueError("key_level must be an integer from 2 to 30")
        _validated_enum(self.region, Region, "region")
        _validated_enum(self.service_format, ServiceFormat, "service_format")
        object.__setattr__(self, "runs", _validated_package_runs(self.runs))
        object.__setattr__(self, "price_conditions", _validated_conditions(self.price_conditions))

    @property
    def code(self) -> str:
        return f"mplus_{self.key_level}_{self.service_format.value}_x{self.runs}"

    @property
    def is_package(self) -> bool:
        return self.runs > 1

    @property
    def deduplication_key(self) -> str:
        conditions = ",".join(f"{name}={value}" for name, value in self.price_conditions)
        return f"{self.code}|{self.region.value}|{conditions}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "mythic_plus",
            "key_level": self.key_level,
            "region": self.region.value,
            "service_format": self.service_format.value,
            "runs": self.runs,
            "price_conditions": dict(self.price_conditions),
            "code": self.code,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MythicPlusService":
        if payload.get("kind") != "mythic_plus":
            raise ValueError("payload is not a Mythic+ service")
        service = cls(
            key_level=payload.get("key_level"),
            region=Region(payload.get("region")),
            service_format=ServiceFormat(payload.get("service_format")),
            runs=payload.get("runs", 1),
            price_conditions=payload.get("price_conditions", {}),
        )
        return _verified_code(payload, service)

    @classmethod
    def from_json(cls, payload: str) -> "MythicPlusService":
        return cls.from_dict(_json_object(payload))


ServiceModel = MythicPlusService
TService = TypeVar("TService", bound=ServiceModel)


class ServiceCatalog:
    """In-memory guard against duplicate service variants before lot creation exists."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceModel] = {}

    def add(self, service: TService) -> TService:
        if service.deduplication_key in self._services:
            raise DuplicateServiceError(f"duplicate service: {service.deduplication_key}")
        self._services[service.deduplication_key] = service
        return service

    def get(self, deduplication_key: str) -> ServiceModel | None:
        return self._services.get(deduplication_key)

    def values(self) -> tuple[ServiceModel, ...]:
        return tuple(self._services.values())


def _json_object(payload: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("service payload must be valid JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("service payload must be a JSON object")
    return decoded


def _verified_code(payload: Mapping[str, Any], service: TService) -> TService:
    serialized_code = payload.get("code")
    if serialized_code is not None and serialized_code != service.code:
        raise ValueError("serialized service code does not match its fields")
    return service
