"""Pure local planning for synchronization between catalog services and lots.

The planner never guesses lot identity from title, price, or description.  A
lot participates only if a separately confirmed local mapping assigns its
stable service code.  This module does not invoke a FunPay write client.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, TextIO

from .database import Database
from .funpay import FunPayClient
from .lot_discovery import classify_wow_lot
from .lot_writes import (
    CapabilityState,
    LotWriteCapability,
    LotWriteClient,
    LotWriteOutcome,
    LotWriteResult,
    MockLotWriteClient,
)
from .service_catalog import CatalogFamily, CatalogService, DesiredState, ServiceCatalogRepository


class LotSyncDecision(StrEnum):
    ALREADY_CORRECT = "already_correct"
    CREATE_REQUIRED = "create_required"
    UPDATE_REQUIRED = "update_required"
    DISABLE_REQUIRED = "disable_required"
    ENABLE_REQUIRED = "enable_required"
    AMBIGUOUS = "ambiguous"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"


class LotSyncSafety(StrEnum):
    SAFE = "safe"
    DRY_RUN_ONLY = "dry_run_only"
    AMBIGUOUS = "ambiguous"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"


class DescriptionConfirmation(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    MISSING = "missing"


class DryRunOutcome(StrEnum):
    REQUESTED = "requested"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DescriptionTarget:
    text: str | None
    confirmation: DescriptionConfirmation
    safe_neutral_text: str | None = None

    def resolved(self) -> tuple[str | None, str | None]:
        if self.confirmation is DescriptionConfirmation.CONFIRMED and self.text:
            return self.text, None
        if self.safe_neutral_text:
            return self.safe_neutral_text, None
        return None, "description profile is not confirmed"


@dataclass(frozen=True)
class DesiredLotState:
    service: CatalogService
    title: str | None
    description: DescriptionTarget
    category_node_id: str | None
    form_fields: Mapping[str, str]
    price_placeholder: str | None

    @property
    def service_code(self) -> str:
        return self.service.stable_code


@dataclass(frozen=True)
class CurrentLotState:
    lot_id: str
    confirmed_service_code: str | None
    managed_service_family: CatalogFamily | None
    identity_confirmed: bool
    title: str
    description: str | None
    price_minor: int
    is_active: bool | None
    category_node_id: str | None
    form_fields: Mapping[str, str]
    service_conditions: Mapping[str, str] | None


@dataclass(frozen=True)
class LotSyncAction:
    service_code: str
    decision: LotSyncDecision
    current_state: Mapping[str, object]
    desired_state: Mapping[str, object]
    changed_fields: Mapping[str, object]
    capability_requirements: tuple[LotWriteCapability, ...]
    safety_status: LotSyncSafety
    reason: str


@dataclass(frozen=True)
class LotSyncPlan:
    plan_id: str
    actions: tuple[LotSyncAction, ...]

    @property
    def counts(self) -> Mapping[LotSyncDecision, int]:
        return Counter(action.decision for action in self.actions)


@dataclass(frozen=True)
class DryRunActionResult:
    service_code: str
    outcome: DryRunOutcome
    decision: LotSyncDecision


@dataclass(frozen=True)
class DryRunExecution:
    plan_id: str
    actions: tuple[DryRunActionResult, ...]


class LotSyncPlanner:
    """Determines safe technical actions without performing them."""

    def __init__(self, write_client: LotWriteClient) -> None:
        self.write_client = write_client

    def plan(self, desired: tuple[DesiredLotState, ...], current: tuple[CurrentLotState, ...]) -> LotSyncPlan:
        if any(item.service.family is not CatalogFamily.MYTHIC_PLUS for item in desired):
            raise ValueError("only Mythic+ catalog services can enter lot synchronization")
        codes = [item.service_code for item in desired]
        if len(codes) != len(set(codes)):
            raise ValueError("desired catalog contains duplicate stable codes")
        mapped: dict[str, list[CurrentLotState]] = defaultdict(list)
        for item in current:
            if (
                item.confirmed_service_code
                and item.managed_service_family is CatalogFamily.MYTHIC_PLUS
                and item.identity_confirmed
            ):
                mapped[item.confirmed_service_code].append(item)
        has_unconfirmed_lot = any(
            not item.identity_confirmed or item.managed_service_family is not CatalogFamily.MYTHIC_PLUS
            for item in current
        )
        actions = tuple(sorted(
            (self._action(item, mapped.get(item.service_code, ()), has_unconfirmed_lot) for item in desired),
            key=lambda action: action.service_code,
        ))
        return LotSyncPlan(_plan_id(actions), actions)

    def _action(
        self, desired: DesiredLotState, candidates: tuple[CurrentLotState, ...] | list[CurrentLotState],
        has_unconfirmed_lot: bool,
    ) -> LotSyncAction:
        desired_view = _desired_view(desired)
        if len(candidates) > 1:
            return _action(desired, LotSyncDecision.AMBIGUOUS, None, desired_view, {}, (), LotSyncSafety.AMBIGUOUS,
                           "multiple confirmed mappings point to the same stable service code")
        description, description_blocker = desired.description.resolved()
        if description_blocker:
            return _action(desired, LotSyncDecision.BLOCKED, candidates[0] if candidates else None, desired_view, {}, (), LotSyncSafety.BLOCKED,
                           description_blocker)
        if not candidates:
            if has_unconfirmed_lot:
                return _action(
                    desired, LotSyncDecision.AMBIGUOUS, None, desired_view, {}, (), LotSyncSafety.AMBIGUOUS,
                    "an existing lot lacks a confirmed service mapping; create could duplicate it",
                )
            requirements = (LotWriteCapability.CREATE_LOT,)
            return self._capability_action(desired, LotSyncDecision.CREATE_REQUIRED, None, desired_view, {
                "title": desired.title, "description": description, "price_placeholder": desired.price_placeholder,
                "category_node": desired.category_node_id, "form_fields": dict(desired.form_fields),
                "service_conditions": dict(desired.service.price_conditions),
            }, requirements, "no confirmed lot mapping exists")

        current = candidates[0]
        changed: dict[str, object] = {}
        requirements: list[LotWriteCapability] = []
        if desired.category_node_id is not None and current.category_node_id != desired.category_node_id:
            changed["category_node"] = {"current": current.category_node_id, "desired": desired.category_node_id}
            requirements.append(LotWriteCapability.UPDATE_FIELDS)
        if desired.title is not None and current.title != desired.title:
            changed["title"] = {"current": current.title, "desired": desired.title}
            requirements.append(LotWriteCapability.UPDATE_TITLE)
        if current.description != description:
            changed["description"] = {"current": current.description, "desired": description}
            requirements.append(LotWriteCapability.UPDATE_DESCRIPTION)
        fields_changed = {
            name: {"current": current.form_fields.get(name), "desired": value}
            for name, value in desired.form_fields.items() if current.form_fields.get(name) != value
        }
        if fields_changed:
            changed["form_fields"] = fields_changed
            requirements.append(LotWriteCapability.UPDATE_FIELDS)
        if current.service_conditions is None and desired.service.price_conditions:
            return _action(
                desired, LotSyncDecision.BLOCKED, current, desired_view, changed,
                tuple(dict.fromkeys(requirements)), LotSyncSafety.BLOCKED,
                "current service conditions are not confirmed",
            )
        if current.service_conditions is not None and dict(current.service_conditions) != dict(desired.service.price_conditions):
            changed["service_conditions"] = {
                "current": dict(current.service_conditions), "desired": dict(desired.service.price_conditions),
            }
            requirements.append(LotWriteCapability.UPDATE_FIELDS)
        if desired.price_placeholder:
            changed["price_placeholder"] = desired.price_placeholder

        expected_enabled = desired.service.desired_state is DesiredState.ENABLED
        if current.is_active is None:
            return _action(desired, LotSyncDecision.BLOCKED, current, desired_view, changed, tuple(requirements), LotSyncSafety.BLOCKED,
                           "current lot activity is unknown")
        if current.is_active is not None and current.is_active != expected_enabled:
            capability = LotWriteCapability.ENABLE_LOT if expected_enabled else LotWriteCapability.DISABLE_LOT
            if changed:
                requirements.append(capability)
                return self._capability_action(desired, LotSyncDecision.UPDATE_REQUIRED, current, desired_view, changed, tuple(requirements),
                                               "content and activity both differ")
            decision = LotSyncDecision.ENABLE_REQUIRED if expected_enabled else LotSyncDecision.DISABLE_REQUIRED
            return self._capability_action(desired, decision, current, desired_view, {}, (capability,), "lot activity differs")
        if not changed or set(changed) == {"price_placeholder"}:
            return _action(desired, LotSyncDecision.ALREADY_CORRECT, current, desired_view, changed, (), LotSyncSafety.SAFE,
                           "all concrete desired values match; price remains a placeholder")
        return self._capability_action(desired, LotSyncDecision.UPDATE_REQUIRED, current, desired_view, changed,
                                       tuple(dict.fromkeys(requirements)), "confirmed mapping has changed fields")

    def _capability_action(
        self, desired: DesiredLotState, decision: LotSyncDecision, current: CurrentLotState | None,
        desired_view: Mapping[str, object], changed: Mapping[str, object], requirements: tuple[LotWriteCapability, ...], reason: str,
    ) -> LotSyncAction:
        capabilities = self.write_client.capabilities()
        unsupported = [capability for capability in requirements if capabilities[capability].state is CapabilityState.UNSUPPORTED]
        unavailable = [capability for capability in requirements if capabilities[capability].state is CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION]
        if unsupported:
            return _action(desired, LotSyncDecision.UNSUPPORTED, current, desired_view, changed, requirements, LotSyncSafety.UNSUPPORTED,
                           f"unsupported capability: {unsupported[0].value}")
        if unavailable:
            return _action(desired, LotSyncDecision.BLOCKED, current, desired_view, changed, requirements, LotSyncSafety.BLOCKED,
                           f"capability unavailable without live session: {unavailable[0].value}")
        return _action(desired, decision, current, desired_view, changed, requirements, LotSyncSafety.DRY_RUN_ONLY, reason)


class LotSyncDryRunExecutor:
    """Records planned actions without calling either native or mock write methods."""

    def __init__(self) -> None:
        self._executed_plan_ids: set[str] = set()

    def execute(self, plan: LotSyncPlan) -> DryRunExecution:
        repeated = plan.plan_id in self._executed_plan_ids
        self._executed_plan_ids.add(plan.plan_id)
        return DryRunExecution(
            plan.plan_id,
            tuple(DryRunActionResult(action.service_code, DryRunOutcome.SKIPPED if repeated else DryRunOutcome.REQUESTED, action.decision) for action in plan.actions),
        )


class MockLotSyncExecutor(LotSyncDryRunExecutor):
    """Test double with no additional behavior or external dependency."""


@dataclass(frozen=True)
class MockLotSyncResult:
    """Read-back evidence from a mock-only synchronization cycle."""

    initial_plan: LotSyncPlan
    write_results: tuple[LotWriteResult, ...]
    reread_lots: tuple[CurrentLotState, ...]
    verification_plan: LotSyncPlan
    verified: bool


class MockLotSyncCoordinator:
    """Apply a plan to in-memory lots, reread them, and verify by replanning.

    This coordinator accepts only ``MockLotWriteClient`` and cannot cross a
    network boundary. Production lot writes remain hard-disabled.
    """

    def __init__(self, write_client: MockLotWriteClient) -> None:
        if not isinstance(write_client, MockLotWriteClient):
            raise ValueError("MockLotSyncCoordinator accepts MockLotWriteClient only")
        self.write_client = write_client
        self.planner = LotSyncPlanner(write_client)

    def execute(
        self, desired: tuple[DesiredLotState, ...], current: tuple[CurrentLotState, ...]
    ) -> MockLotSyncResult:
        initial = self.planner.plan(desired, current)
        desired_by_code = {item.service_code: item for item in desired}
        reread = list(current)
        results: list[LotWriteResult] = []
        for action in initial.actions:
            if action.safety_status is not LotSyncSafety.DRY_RUN_ONLY:
                continue
            target = desired_by_code[action.service_code]
            action_results = self._dispatch(initial.plan_id, action, target)
            results.extend(action_results)
            if action_results and all(item.outcome is LotWriteOutcome.SUCCEEDED for item in action_results):
                reread = self._apply(reread, action, target)
        reread_lots = tuple(sorted(reread, key=lambda item: item.lot_id))
        verification = self.planner.plan(desired, reread_lots)
        verified = all(action.decision is LotSyncDecision.ALREADY_CORRECT for action in verification.actions)
        return MockLotSyncResult(initial, tuple(results), reread_lots, verification, verified)

    def _dispatch(
        self, plan_id: str, action: LotSyncAction, desired: DesiredLotState
    ) -> tuple[LotWriteResult, ...]:
        lot_id = str(action.current_state.get("lot_id", ""))
        key_prefix = f"lot-sync:{plan_id}:{action.service_code}"
        if action.decision is LotSyncDecision.CREATE_REQUIRED:
            if desired.category_node_id is None or not desired.form_fields:
                return ()
            return (self.write_client.create_lot(
                desired.category_node_id, desired.form_fields,
                operation_key=f"{key_prefix}:create_lot",
            ),)

        calls: list[LotWriteResult] = []
        for capability in action.capability_requirements:
            operation_key = f"{key_prefix}:{capability.value}"
            if capability is LotWriteCapability.UPDATE_TITLE and desired.title is not None:
                calls.append(self.write_client.update_title(
                    lot_id, desired.title, desired.title, operation_key=operation_key
                ))
            elif capability is LotWriteCapability.UPDATE_DESCRIPTION:
                description, blocker = desired.description.resolved()
                if blocker is None and description is not None:
                    calls.append(self.write_client.update_description(
                        lot_id, description, description, operation_key=operation_key
                    ))
            elif capability is LotWriteCapability.UPDATE_FIELDS:
                fields = dict(desired.form_fields) or dict(desired.service.price_conditions)
                if fields:
                    calls.append(self.write_client.update_fields(lot_id, fields, operation_key=operation_key))
            elif capability is LotWriteCapability.ENABLE_LOT:
                calls.append(self.write_client.enable_lot(lot_id, operation_key=operation_key))
            elif capability is LotWriteCapability.DISABLE_LOT:
                calls.append(self.write_client.disable_lot(lot_id, operation_key=operation_key))
        return tuple(calls)

    @staticmethod
    def _apply(
        current: list[CurrentLotState], action: LotSyncAction, desired: DesiredLotState
    ) -> list[CurrentLotState]:
        lot_id = str(action.current_state.get("lot_id") or f"mock-{desired.service_code}")
        previous = next((item for item in current if item.lot_id == lot_id), None)
        description, _ = desired.description.resolved()
        replacement = CurrentLotState(
            lot_id=lot_id,
            confirmed_service_code=desired.service_code,
            managed_service_family=CatalogFamily.MYTHIC_PLUS,
            identity_confirmed=True,
            title=desired.title or (previous.title if previous else desired.service_code),
            description=description,
            price_minor=previous.price_minor if previous else 0,
            is_active=desired.service.desired_state is DesiredState.ENABLED,
            category_node_id=desired.category_node_id,
            form_fields=dict(desired.form_fields),
            service_conditions=dict(desired.service.price_conditions),
        )
        return [item for item in current if item.lot_id != lot_id] + [replacement]


class ConfirmedLotMappingRepository:
    """Local confirmation ledger; no mapping is inferred from mutable lot text."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def confirm(self, lot_id: str, service_code: str) -> None:
        if not lot_id.strip() or not service_code.strip():
            raise ValueError("lot id and service code are required")
        with self.database.session() as connection:
            eligible = connection.execute(
                """SELECT 1 FROM own_lot_registry lot, service_catalog catalog
                WHERE lot.external_id = ? AND lot.classification = 'mythic_plus'
                  AND lot.mapping_state = 'mapped' AND catalog.stable_code = ?
                  AND catalog.family = 'mythic_plus'""",
                (lot_id, service_code),
            ).fetchone()
            if eligible is None:
                raise ValueError("only an exact confirmed Mythic+ identity can be mapped")
            connection.execute("DELETE FROM lot_service_mappings WHERE external_lot_id = ? OR service_code = ?", (lot_id, service_code))
            connection.execute(
                "INSERT INTO lot_service_mappings (external_lot_id, service_code) VALUES (?, ?)", (lot_id, service_code)
            )


def current_lots_from_funpay(
    client: FunPayClient, confirmed_mappings: Mapping[str, str],
    confirmed_conditions: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[CurrentLotState, ...]:
    """Adapt a mock/read client snapshot without doing any write operation."""

    result: list[CurrentLotState] = []
    for detail in client.get_own_lot_details():
        classification = classify_wow_lot(detail)
        service_code = confirmed_mappings.get(detail.lot_id)
        confirmed = (
            service_code is not None
            and classification.kind == "mythic_plus"
            and classification.mapping_state == "mapped"
            and not classification.ambiguous
        )
        result.append(CurrentLotState(
            lot_id=detail.lot_id, confirmed_service_code=service_code if confirmed else None,
            managed_service_family=CatalogFamily.MYTHIC_PLUS if confirmed else None,
            identity_confirmed=confirmed, title=detail.title,
            description=detail.description, price_minor=detail.price_minor, is_active=detail.is_active,
            category_node_id=detail.category_node_id, form_fields=detail.editor_fields,
            service_conditions=(confirmed_conditions or {}).get(detail.lot_id),
        ))
    return tuple(result)


def desired_from_catalog(service: CatalogService, *, title: str | None = None, description: DescriptionTarget | None = None,
                         category_node_id: str | None = None, form_fields: Mapping[str, str] = (), price_placeholder: str | None = None) -> DesiredLotState:
    """Build a safe desired state; missing description data remains blocked."""

    return DesiredLotState(
        service=service, title=title,
        description=description or DescriptionTarget(None, DescriptionConfirmation.MISSING),
        category_node_id=category_node_id, form_fields=dict(form_fields),
        price_placeholder=price_placeholder or service.price_policy_reference,
    )


def run_plan_sync(*, catalog_database: Database, registry_database: Database, write_client: LotWriteClient, output: TextIO) -> int:
    """CLI implementation: only local SQLite reads and a no-send dry-run plan."""

    catalog_database.initialize()
    registry_database.initialize()
    services = ServiceCatalogRepository(catalog_database).list()
    desired = tuple(desired_from_catalog(service) for service in services)
    current = _current_lots_from_registry(registry_database)
    plan = LotSyncPlanner(write_client).plan(desired, current)
    execution = LotSyncDryRunExecutor().execute(plan)
    counts = plan.counts
    print(
        "lots plan-sync: "
        f"services={len(plan.actions)} already_correct={counts.get(LotSyncDecision.ALREADY_CORRECT, 0)} "
        f"create_required={counts.get(LotSyncDecision.CREATE_REQUIRED, 0)} update_required={counts.get(LotSyncDecision.UPDATE_REQUIRED, 0)} "
        f"blocked={counts.get(LotSyncDecision.BLOCKED, 0)} ambiguous={counts.get(LotSyncDecision.AMBIGUOUS, 0)} "
        f"unsupported={counts.get(LotSyncDecision.UNSUPPORTED, 0)} dry_run_actions={len(execution.actions)}",
        file=output,
    )
    return 0


def _current_lots_from_registry(database: Database) -> tuple[CurrentLotState, ...]:
    with database.session() as connection:
        rows = connection.execute(
            """SELECT lot.external_id, lot.title, lot.description, lot.price_minor, lot.is_active,
            lot.category_node_id, lot.editor_fields_json, lot.classification, lot.mapping_state,
            mapping.service_code, catalog.family AS service_family
            FROM own_lot_registry lot
            LEFT JOIN lot_service_mappings mapping ON mapping.external_lot_id = lot.external_id
            LEFT JOIN service_catalog catalog ON catalog.stable_code = mapping.service_code"""
        ).fetchall()
    return tuple(
        CurrentLotState(
            lot_id=row["external_id"],
            confirmed_service_code=(
                row["service_code"]
                if row["classification"] == "mythic_plus" and row["mapping_state"] == "mapped"
                and row["service_family"] == "mythic_plus"
                else None
            ),
            managed_service_family=(
                CatalogFamily.MYTHIC_PLUS
                if row["classification"] == "mythic_plus" and row["mapping_state"] == "mapped"
                and row["service_code"] is not None and row["service_family"] == "mythic_plus" else None
            ),
            identity_confirmed=(
                row["classification"] == "mythic_plus" and row["mapping_state"] == "mapped"
                and row["service_code"] is not None and row["service_family"] == "mythic_plus"
            ),
            title=row["title"],
            description=row["description"], price_minor=int(row["price_minor"]),
            is_active=None if row["is_active"] is None else bool(row["is_active"]), category_node_id=row["category_node_id"],
            form_fields=_json_object(row["editor_fields_json"]), service_conditions=None,
        )
        for row in rows
    )


def _action(desired: DesiredLotState, decision: LotSyncDecision, current: CurrentLotState | None,
            desired_state: Mapping[str, object], changed: Mapping[str, object], requirements: tuple[LotWriteCapability, ...],
            safety: LotSyncSafety, reason: str) -> LotSyncAction:
    return LotSyncAction(desired.service_code, decision, _current_view(current) if current else {}, desired_state,
                         dict(changed), requirements, safety, reason)


def _desired_view(desired: DesiredLotState) -> Mapping[str, object]:
    return {
        "service_code": desired.service_code, "family": desired.service.family.value,
        "desired_state": desired.service.desired_state.value, "enabled": desired.service.enabled,
        "template_reference": desired.service.template_reference, "description_profile": desired.service.description_profile,
        "price_policy_reference": desired.service.price_policy_reference,
    }


def _current_view(current: CurrentLotState) -> Mapping[str, object]:
    return {
        "lot_id": current.lot_id, "mapped_service_code": current.confirmed_service_code,
        "managed_service_family": current.managed_service_family.value if current.managed_service_family else None,
        "identity_confirmed": current.identity_confirmed,
        "is_active": current.is_active, "category_node_id": current.category_node_id,
    }


def _plan_id(actions: tuple[LotSyncAction, ...]) -> str:
    material = [
        {"service_code": action.service_code, "decision": action.decision.value, "current": action.current_state,
         "desired": action.desired_state, "changed": action.changed_fields, "requirements": [item.value for item in action.capability_requirements]}
        for action in actions
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _json_object(value: str) -> Mapping[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("stored lot fields are invalid") from error
    if not isinstance(parsed, dict) or not all(isinstance(name, str) and isinstance(item, str) for name, item in parsed.items()):
        raise ValueError("stored lot fields are invalid")
    return parsed
