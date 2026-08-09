"""Disabled-by-default technical boundary for fpx lot write capabilities.

This module contains no handwritten FunPay HTTP contract.  It only plans or,
in a future separately enabled release, delegates to public methods exposed by
the pinned ``fpx-engine`` package.  Network mutation is disabled in production
for this version regardless of the configured operation mode.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol

from .funpay import FunPayError, NativeFunPayClient


class LotWriteCapability(StrEnum):
    UPDATE_PRICE = "update_price"
    UPDATE_TITLE = "update_title"
    UPDATE_DESCRIPTION = "update_description"
    UPDATE_FIELDS = "update_fields"
    ENABLE_LOT = "enable_lot"
    DISABLE_LOT = "disable_lot"
    CREATE_LOT = "create_lot"
    BUMP_RAISE = "bump_raise"


class CapabilityState(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE_WITHOUT_LIVE_SESSION = "unavailable_without_live_session"


class LotWriteOutcome(StrEnum):
    REQUESTED = "requested"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VERIFICATION_REQUIRED = "verification_required"


class LotWriteValidationError(ValueError):
    """Raised when a planned lot operation is structurally unsafe or incomplete."""


class LotWriteMalformedResponse(RuntimeError):
    """Raised when fpx does not return its documented boolean success value."""


@dataclass(frozen=True)
class Capability:
    name: LotWriteCapability
    state: CapabilityState
    detail: str


@dataclass(frozen=True)
class LotWritePlan:
    """In-memory operation plan; callers must not log or serialize field values."""

    capability: LotWriteCapability
    operation_key: str
    arguments: Mapping[str, object]
    fpx_method: str | None
    verification_required: bool


@dataclass(frozen=True)
class LotWriteResult:
    capability: LotWriteCapability
    outcome: LotWriteOutcome
    operation_key: str
    detail: str
    plan: LotWritePlan | None = None


class LotWriteClient(Protocol):
    """Application-facing contract for all lot-changing operations."""

    def capabilities(self) -> Mapping[LotWriteCapability, Capability]: ...

    def update_price(self, lot_id: str, price: str, *, operation_key: str | None = None) -> LotWriteResult: ...

    def update_title(self, lot_id: str, title_ru: str, title_en: str, *, operation_key: str | None = None) -> LotWriteResult: ...

    def update_description(self, lot_id: str, description_ru: str, description_en: str, *, operation_key: str | None = None) -> LotWriteResult: ...

    def update_fields(self, lot_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult: ...

    def enable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult: ...

    def disable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult: ...

    def create_lot(self, node_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult: ...

    def bump_raise(self, *, operation_key: str | None = None) -> LotWriteResult: ...


_FPX_METHODS: Mapping[LotWriteCapability, str | None] = {
    LotWriteCapability.UPDATE_PRICE: "account.editor.change_lot_price",
    LotWriteCapability.UPDATE_TITLE: "account.editor.change_lot_short_desc",
    LotWriteCapability.UPDATE_DESCRIPTION: "account.editor.change_lot_desc",
    # fpx only exposes individual editor helpers, not a public generic update.
    LotWriteCapability.UPDATE_FIELDS: None,
    LotWriteCapability.ENABLE_LOT: "account.editor.toggle_on_lot",
    LotWriteCapability.DISABLE_LOT: "account.editor.toggle_off_lot",
    LotWriteCapability.CREATE_LOT: "account.lot.get_node_editor_data + account.lot.create_lot",
    # fpx raises all owned categories together; it has no per-lot raise method.
    LotWriteCapability.BUMP_RAISE: "account.lot.raise_lots",
}


class NativeLotWriteClient:
    """Adapter that plans fpx writes while hard-blocking production mutation.

    ``live_execution_enabled`` is intentionally false in the production factory.
    It exists only to unit-test the adapter against an in-memory fpx double; it
    must never be enabled by configuration in this release.
    """

    def __init__(
        self,
        read_client: NativeFunPayClient,
        *,
        operation_mode: str,
        live_session_available: Callable[[], bool] | None = None,
        live_execution_enabled: bool = False,
    ) -> None:
        if operation_mode not in {"safe", "dry_run", "live"}:
            raise ValueError("operation_mode must be safe, dry_run, or live")
        self._read_client = read_client
        self._operation_mode = operation_mode
        self._live_session_available = live_session_available or (lambda: False)
        self._live_execution_enabled = live_execution_enabled
        self._seen_operation_keys: set[str] = set()

    def capabilities(self) -> Mapping[LotWriteCapability, Capability]:
        session_available = self._live_session_available()
        result: dict[LotWriteCapability, Capability] = {}
        for capability, method in _FPX_METHODS.items():
            if method is None:
                result[capability] = Capability(capability, CapabilityState.UNSUPPORTED, "fpx has no public generic operation")
            elif not session_available:
                result[capability] = Capability(
                    capability, CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION,
                    "a live local FunPay session is required for execution",
                )
            elif capability is LotWriteCapability.BUMP_RAISE:
                result[capability] = Capability(
                    capability, CapabilityState.SUPPORTED,
                    "fpx raises all owned lot categories; targeted per-lot raise is unavailable",
                )
            else:
                result[capability] = Capability(capability, CapabilityState.SUPPORTED, f"fpx public method: {method}")
        return result

    def update_price(self, lot_id: str, price: str, *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        normalized_price = _price(price)
        return self._dispatch(
            LotWriteCapability.UPDATE_PRICE, {"lot_id": normalized_lot_id, "price": normalized_price}, operation_key,
            verify=False,
            invoke=lambda tools: tools.account.editor.change_lot_price(normalized_lot_id, normalized_price),
        )

    def update_title(self, lot_id: str, title_ru: str, title_en: str, *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        ru, en = _required_text(title_ru, "title_ru"), _required_text(title_en, "title_en")
        return self._dispatch(
            LotWriteCapability.UPDATE_TITLE, {"lot_id": normalized_lot_id, "title_ru": ru, "title_en": en}, operation_key,
            verify=False,
            invoke=lambda tools: tools.account.editor.change_lot_short_desc(normalized_lot_id, ru, en),
        )

    def update_description(self, lot_id: str, description_ru: str, description_en: str, *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        ru, en = _required_text(description_ru, "description_ru"), _required_text(description_en, "description_en")
        return self._dispatch(
            LotWriteCapability.UPDATE_DESCRIPTION,
            {"lot_id": normalized_lot_id, "description_ru": ru, "description_en": en}, operation_key,
            verify=False,
            invoke=lambda tools: tools.account.editor.change_lot_desc(normalized_lot_id, ru, en),
        )

    def update_fields(self, lot_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        normalized_fields = _fields(fields)
        return self._dispatch(
            LotWriteCapability.UPDATE_FIELDS, {"lot_id": normalized_lot_id, "fields": normalized_fields}, operation_key,
            verify=True, invoke=None,
        )

    def enable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        return self._dispatch(
            LotWriteCapability.ENABLE_LOT, {"lot_id": normalized_lot_id}, operation_key, verify=True,
            invoke=lambda tools: tools.account.editor.toggle_on_lot(normalized_lot_id),
        )

    def disable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult:
        normalized_lot_id = _required_text(lot_id, "lot_id")
        return self._dispatch(
            LotWriteCapability.DISABLE_LOT, {"lot_id": normalized_lot_id}, operation_key, verify=True,
            invoke=lambda tools: tools.account.editor.toggle_off_lot(normalized_lot_id),
        )

    def create_lot(self, node_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult:
        normalized_node_id = _required_text(node_id, "node_id")
        normalized_fields = _fields(fields)
        required_create_fields = {"price", "amount", "fields[summary][ru]", "fields[summary][en]"}
        if not required_create_fields.issubset(normalized_fields):
            raise LotWriteValidationError("create_lot requires price, amount, and both title fields")

        async def invoke(tools: Any) -> Any:
            editor = await tools.account.lot.get_node_editor_data(normalized_node_id)
            available = {field.key for field in editor.fields}
            if not set(normalized_fields).issubset(available):
                raise LotWriteValidationError("create_lot includes fields unavailable for this node")
            for field in editor.fields:
                if field.key in normalized_fields:
                    field.value = normalized_fields[field.key]
            return await tools.account.lot.create_lot(editor)

        return self._dispatch(
            LotWriteCapability.CREATE_LOT, {"node_id": normalized_node_id, "fields": normalized_fields}, operation_key,
            verify=True, invoke=invoke,
        )

    def bump_raise(self, *, operation_key: str | None = None) -> LotWriteResult:
        return self._dispatch(
            LotWriteCapability.BUMP_RAISE, {}, operation_key, verify=True,
            invoke=lambda tools: tools.account.lot.raise_lots(),
        )

    def _dispatch(
        self,
        capability: LotWriteCapability,
        arguments: Mapping[str, object],
        operation_key: str | None,
        *,
        verify: bool,
        invoke: Callable[[Any], Any] | None,
    ) -> LotWriteResult:
        key = operation_key or _operation_key(capability, arguments)
        if not key.strip():
            raise LotWriteValidationError("operation_key must not be empty")
        plan = LotWritePlan(capability, key, dict(arguments), _FPX_METHODS[capability], verify)
        if self._operation_mode == "safe":
            return LotWriteResult(capability, LotWriteOutcome.SKIPPED, key, "safe mode blocks all lot writes", plan)
        capability_state = self.capabilities()[capability].state
        if capability_state is CapabilityState.UNSUPPORTED:
            return LotWriteResult(capability, LotWriteOutcome.UNSUPPORTED, key, "no supported fpx operation", plan)
        if key in self._seen_operation_keys:
            return LotWriteResult(capability, LotWriteOutcome.SKIPPED, key, "duplicate operation key", plan)
        self._seen_operation_keys.add(key)
        if self._operation_mode == "dry_run":
            return LotWriteResult(capability, LotWriteOutcome.REQUESTED, key, "operation planned; network send blocked", plan)
        if capability_state is CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION:
            return LotWriteResult(capability, LotWriteOutcome.SKIPPED, key, "live session is unavailable", plan)
        if not self._live_execution_enabled:
            return LotWriteResult(
                capability, LotWriteOutcome.VERIFICATION_REQUIRED, key,
                "production network writes are disabled in this release", plan,
            )
        if invoke is None:
            return LotWriteResult(capability, LotWriteOutcome.UNSUPPORTED, key, "no fpx invocation exists", plan)
        try:
            response = self._read_client._run(invoke)  # type: ignore[arg-type]
            if not _is_fpx_success(capability, response):
                raise LotWriteMalformedResponse("fpx write did not return True")
        except TimeoutError:
            return LotWriteResult(capability, LotWriteOutcome.FAILED, key, "operation timed out", plan)
        except (FunPayError, LotWriteValidationError, LotWriteMalformedResponse):
            return LotWriteResult(capability, LotWriteOutcome.FAILED, key, "fpx operation failed", plan)
        return LotWriteResult(
            capability,
            LotWriteOutcome.VERIFICATION_REQUIRED if verify else LotWriteOutcome.SUCCEEDED,
            key,
            "fpx accepted the operation; read-back verification is required" if verify else "fpx verified the requested field",
            plan,
        )


@dataclass
class MockLotWriteClient:
    """In-memory capability and outcome double for CI; no network boundary exists."""

    capability_states: Mapping[LotWriteCapability, CapabilityState] = field(default_factory=dict)
    outcomes: Mapping[LotWriteCapability, LotWriteOutcome] = field(default_factory=dict)
    calls: list[LotWritePlan] = field(default_factory=list)

    def capabilities(self) -> Mapping[LotWriteCapability, Capability]:
        return {
            capability: Capability(capability, self.capability_states.get(capability, CapabilityState.SUPPORTED), "mock")
            for capability in LotWriteCapability
        }

    def update_price(self, lot_id: str, price: str, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.UPDATE_PRICE, {"lot_id": _required_text(lot_id, "lot_id"), "price": _price(price)}, operation_key)

    def update_title(self, lot_id: str, title_ru: str, title_en: str, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.UPDATE_TITLE, {"lot_id": _required_text(lot_id, "lot_id"), "title_ru": _required_text(title_ru, "title_ru"), "title_en": _required_text(title_en, "title_en")}, operation_key)

    def update_description(self, lot_id: str, description_ru: str, description_en: str, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.UPDATE_DESCRIPTION, {"lot_id": _required_text(lot_id, "lot_id"), "description_ru": _required_text(description_ru, "description_ru"), "description_en": _required_text(description_en, "description_en")}, operation_key)

    def update_fields(self, lot_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.UPDATE_FIELDS, {"lot_id": _required_text(lot_id, "lot_id"), "fields": _fields(fields)}, operation_key)

    def enable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.ENABLE_LOT, {"lot_id": _required_text(lot_id, "lot_id")}, operation_key)

    def disable_lot(self, lot_id: str, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.DISABLE_LOT, {"lot_id": _required_text(lot_id, "lot_id")}, operation_key)

    def create_lot(self, node_id: str, fields: Mapping[str, str], *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.CREATE_LOT, {"node_id": _required_text(node_id, "node_id"), "fields": _fields(fields)}, operation_key)

    def bump_raise(self, *, operation_key: str | None = None) -> LotWriteResult:
        return self._record(LotWriteCapability.BUMP_RAISE, {}, operation_key)

    def _record(self, capability: LotWriteCapability, arguments: Mapping[str, object], operation_key: str | None) -> LotWriteResult:
        key = operation_key or _operation_key(capability, arguments)
        plan = LotWritePlan(capability, key, dict(arguments), _FPX_METHODS[capability], False)
        self.calls.append(plan)
        state = self.capabilities()[capability].state
        if state is CapabilityState.UNSUPPORTED:
            outcome = LotWriteOutcome.UNSUPPORTED
        elif state is CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION:
            outcome = LotWriteOutcome.SKIPPED
        else:
            outcome = self.outcomes.get(capability, LotWriteOutcome.SUCCEEDED)
        return LotWriteResult(capability, outcome, key, "mock", plan)


def build_lot_write_client(settings: Any, read_client: NativeFunPayClient) -> NativeLotWriteClient:
    """Production composition. It intentionally never enables network mutation."""

    return NativeLotWriteClient(
        read_client,
        operation_mode=settings.operation_mode,
        live_session_available=read_client.has_local_session,
        live_execution_enabled=False,
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LotWriteValidationError(f"{field} must be non-empty text")
    return value.strip()


def _price(value: str) -> str:
    normalized = _required_text(value, "price")
    try:
        parsed = Decimal(normalized.replace(",", "."))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise LotWriteValidationError("price must be a non-negative number") from error
    return normalized


def _fields(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise LotWriteValidationError("fields must be a non-empty mapping")
    result: dict[str, str] = {}
    for name, field_value in value.items():
        normalized_name = _required_text(name, "field name")
        if any(marker in normalized_name.lower() for marker in ("csrf", "secret", "payment_msg")):
            raise LotWriteValidationError("sensitive field updates are not supported")
        result[normalized_name] = _required_text(field_value, f"field {normalized_name}")
    return result


def _operation_key(capability: LotWriteCapability, arguments: Mapping[str, object]) -> str:
    # The digest supports local duplicate protection without retaining or
    # printing title/description/field values as an operation identifier.
    material = repr((capability.value, tuple(sorted(arguments.items())))).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _is_fpx_success(capability: LotWriteCapability, response: object) -> bool:
    if capability is LotWriteCapability.BUMP_RAISE:
        # fpx returns one opaque result per raised category, not a boolean.
        return isinstance(response, list)
    return response is True
