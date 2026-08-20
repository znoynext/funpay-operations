"""Versioned, public seasonal data and safe description previews.

No data is fetched from source URLs. They are evidence metadata supplied by an
owner, and descriptions use only records explicitly marked as confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from .services import MythicPlusService, Region


_MAX_YAML_BYTES = 64 * 1024
_SEASON_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUPPORTED_SCHEMA_VERSION = 1
_MAX_CONFIRMED_DATA_AGE_DAYS = 30


class SeasonalDataError(ValueError):
    """Raised when public seasonal metadata is malformed or incompatible."""


class UnconfirmedSeasonalDataError(SeasonalDataError):
    """Raised when an unverified seasonal record would be used in text."""


class ConfirmationStatus(str, Enum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class SeasonalData:
    schema_version: int
    data_version: int
    service: str
    season: str
    region: Region
    start_date: date | None
    reward_item_levels: Mapping[str, int]
    crests: Mapping[str, str]
    checked_at: date | None
    sources: tuple[str, ...]
    confirmation_status: ConfirmationStatus

    @property
    def is_confirmed(self) -> bool:
        return self.confirmation_status is ConfirmationStatus.CONFIRMED

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SeasonalData":
        if not isinstance(payload, Mapping):
            raise SeasonalDataError("seasonal YAML must contain an object")
        schema_version = _positive_int(payload.get("schema_version"), "schema_version")
        if schema_version != _SUPPORTED_SCHEMA_VERSION:
            raise SeasonalDataError(f"unsupported schema_version: {schema_version}")
        data_version = _positive_int(payload.get("data_version"), "data_version")
        service = payload.get("service")
        if service != "mythic_plus":
            raise SeasonalDataError("service must be mythic_plus")
        season = payload.get("season")
        if not isinstance(season, str) or not _SEASON_ID.fullmatch(season):
            raise SeasonalDataError("season must be a stable lowercase identifier")
        try:
            region = Region(payload.get("region"))
            status = ConfirmationStatus(payload.get("confirmation_status"))
        except ValueError as error:
            raise SeasonalDataError("region or confirmation_status is invalid") from error
        start_date = _optional_date(payload.get("start_date"), "start_date")
        checked_at = _optional_date(payload.get("checked_at"), "checked_at")
        item_levels = _item_levels(payload.get("reward_item_levels"))
        crests = _crests(payload.get("crests"))
        sources = _sources(payload.get("sources"))
        result = cls(
            schema_version, data_version, service, season, region, start_date,
            item_levels, crests, checked_at, sources, status,
        )
        result.require_complete_if_confirmed()
        return result

    def require_complete_if_confirmed(self) -> None:
        if not self.is_confirmed:
            return
        if self.start_date is None or self.checked_at is None:
            raise SeasonalDataError("confirmed data requires start_date and checked_at")
        if not self.reward_item_levels or not self.crests or not self.sources:
            raise SeasonalDataError("confirmed data requires rewards, crests, and sources")

    def require_confirmed_for(self, service: str, region: Region, *, today: date | None = None) -> None:
        if self.service != service or self.region is not region:
            raise SeasonalDataError("seasonal data does not match this service or region")
        if not self.is_confirmed:
            raise UnconfirmedSeasonalDataError("unconfirmed seasonal data cannot be used")
        checked_today = today or date.today()
        if self.checked_at is None or self.checked_at > checked_today:
            raise UnconfirmedSeasonalDataError("seasonal data has no usable verification date")
        if (checked_today - self.checked_at).days > _MAX_CONFIRMED_DATA_AGE_DAYS:
            raise UnconfirmedSeasonalDataError("seasonal data is stale and cannot be used")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "data_version": self.data_version,
            "service": self.service,
            "season": self.season,
            "region": self.region.value,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "reward_item_levels": dict(self.reward_item_levels),
            "crests": dict(self.crests),
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "sources": list(self.sources),
            "confirmation_status": self.confirmation_status.value,
        }


@dataclass(frozen=True)
class DescriptionPreview:
    code: str
    season: str
    data_version: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "season": self.season,
            "data_version": self.data_version,
            "text": self.text,
        }


def load_seasonal_data(path: Path) -> SeasonalData:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise SeasonalDataError("seasonal data must be a YAML file")
    try:
        if path.stat().st_size > _MAX_YAML_BYTES:
            raise SeasonalDataError("seasonal YAML is too large")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SeasonalDataError("seasonal YAML cannot be read") from error
    except yaml.YAMLError as error:
        raise SeasonalDataError("seasonal YAML is invalid") from error
    return SeasonalData.from_mapping(payload)


class DescriptionGenerator:
    """Render text previews only from owner-confirmed public season records."""

    def mythic_plus(self, service: MythicPlusService, data: SeasonalData) -> DescriptionPreview:
        data.require_confirmed_for("mythic_plus", service.region)
        if service.key_level <= 12:
            rewards = _specific_value(data.reward_item_levels, "key", service.key_level, "reward item level")
            crests = _specific_value(data.crests, "key", service.key_level, "crests")
            text = (
                f"Mythic+ +{service.key_level}, формат: {service.service_format.value}. "
                f"Актуальная награда для этого ключа: ilvl {rewards}. "
                f"Гребни для этого ключа: {crests}. "
                "Случайный предмет не гарантируется."
            )
        else:
            text = (
                f"Mythic+ +{service.key_level}, формат: {service.service_format.value}. "
                "Акцент услуги — рейтинг Mythic+ и прохождение высоких ключей. "
                "Случайный предмет не гарантируется."
            )
        return DescriptionPreview(service.code, data.season, data.data_version, text)

def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SeasonalDataError(f"{field} must be a positive integer")
    return value


def _optional_date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise SeasonalDataError(f"{field} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SeasonalDataError(f"{field} must be an ISO date or null") from error


def _item_levels(value: Any) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise SeasonalDataError("reward_item_levels must be an object")
    result: dict[str, int] = {}
    for name, level in value.items():
        if not isinstance(name, str) or not _SEASON_ID.fullmatch(name):
            raise SeasonalDataError("reward item level names must be stable identifiers")
        if isinstance(level, bool) or not isinstance(level, int) or level < 1 or level > 1000:
            raise SeasonalDataError("reward item levels must be plausible positive integers")
        result[name] = level
    return dict(sorted(result.items()))


def _crests(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise SeasonalDataError("crests must be an object")
    result: dict[str, str] = {}
    for name, detail in value.items():
        if not isinstance(name, str) or not _SEASON_ID.fullmatch(name):
            raise SeasonalDataError("crest names must be stable identifiers")
        if not isinstance(detail, str) or not detail.strip() or len(detail) > 160:
            raise SeasonalDataError("crest details must be short non-empty text")
        result[name] = detail.strip()
    return dict(sorted(result.items()))


def _sources(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SeasonalDataError("sources must be a list of HTTPS URLs")
    if len(value) > 10:
        raise SeasonalDataError("sources may contain at most ten URLs")
    if len(set(value)) != len(value):
        raise SeasonalDataError("sources must not contain duplicates")
    for source in value:
        parsed = urlparse(source)
        if parsed.scheme != "https" or not parsed.netloc or len(source) > 2048:
            raise SeasonalDataError("sources must be HTTPS URLs")
    return tuple(value)


def _specific_value(values: Mapping[str, Any], prefix: str, level: int, field: str) -> Any:
    value = values.get(f"{prefix}_{level}")
    if value is None:
        raise SeasonalDataError(f"confirmed seasonal data has no {field} for {prefix}_{level}")
    return value
