from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayLotDetails, MockFunPayClient
from funpay_operations.lot_discovery import LotClassification, OwnLotRegistryRepository, RegisteredLot
from funpay_operations.lot_sync import (
    DescriptionConfirmation,
    DescriptionTarget,
    DryRunOutcome,
    LotSyncDecision,
    LotSyncDryRunExecutor,
    LotSyncPlanner,
    current_lots_from_funpay,
    desired_from_catalog,
    run_plan_sync,
)
from funpay_operations.lot_writes import CapabilityState, LotWriteCapability, MockLotWriteClient
from funpay_operations.service_catalog import CatalogFamily, CatalogService, DesiredState, ServiceCatalogRepository


class LotSyncPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _service()
        self.desired = _desired(self.service)

    def test_empty_account_requires_create(self) -> None:
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), ()).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.CREATE_REQUIRED)
        self.assertEqual(action.capability_requirements, (LotWriteCapability.CREATE_LOT,))

    def test_all_lots_exist_uses_confirmed_mapping_not_title_guessing(self) -> None:
        client = MockFunPayClient(own_lot_details=(_detail(),))
        current = current_lots_from_funpay(
            client, {"lot-1": self.service.stable_code}, {"lot-1": {"timed": "yes"}}
        )
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), current).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.ALREADY_CORRECT)
        self.assertEqual(client.calls, ["get_own_lot_details"])

    def test_one_missing_lot_requires_only_one_create(self) -> None:
        missing = _desired(_service("mplus_k11_eu_selfplay_x1"))
        actions = LotSyncPlanner(MockLotWriteClient()).plan((self.desired, missing), (_current(),)).actions
        by_code = {action.service_code: action.decision for action in actions}
        self.assertEqual(by_code[self.service.stable_code], LotSyncDecision.ALREADY_CORRECT)
        self.assertEqual(by_code[missing.service_code], LotSyncDecision.CREATE_REQUIRED)

    def test_unconfirmed_existing_lot_blocks_creation_to_prevent_duplicates(self) -> None:
        unconfirmed = _current()
        unconfirmed = type(unconfirmed)(
            lot_id=unconfirmed.lot_id, confirmed_service_code=None, managed_service_family=None,
            identity_confirmed=False, title=unconfirmed.title,
            description=unconfirmed.description, price_minor=unconfirmed.price_minor, is_active=unconfirmed.is_active,
            category_node_id=unconfirmed.category_node_id, form_fields=unconfirmed.form_fields,
            service_conditions=unconfirmed.service_conditions,
        )
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), (unconfirmed,)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.AMBIGUOUS)
        self.assertIn("duplicate", action.reason)

    def test_duplicate_candidates_are_ambiguous_instead_of_creating_or_updating(self) -> None:
        duplicate = _current(lot_id="lot-2")
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), (_current(), duplicate)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.AMBIGUOUS)
        self.assertEqual(action.capability_requirements, ())

    def test_non_mythic_lot_is_never_planned_for_mutation(self) -> None:
        other = _current()
        other = type(other)(
            lot_id=other.lot_id, confirmed_service_code=self.service.stable_code,
            managed_service_family=None, identity_confirmed=False, title="Other WoW service",
            description=other.description, price_minor=other.price_minor, is_active=other.is_active,
            category_node_id=other.category_node_id, form_fields=other.form_fields,
            service_conditions=other.service_conditions,
        )
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), (other,)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.AMBIGUOUS)
        self.assertEqual(action.capability_requirements, ())

    def test_incompatible_existing_lot_is_unsupported_when_form_updates_are_unavailable(self) -> None:
        incompatible = _current(category_node_id="other-node")
        writer = MockLotWriteClient({LotWriteCapability.UPDATE_FIELDS: CapabilityState.UNSUPPORTED})
        action = LotSyncPlanner(writer).plan((self.desired,), (incompatible,)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.UNSUPPORTED)
        self.assertIn("category_node", action.changed_fields)

    def test_changed_description_is_an_update(self) -> None:
        changed = _current(description="Old verified text")
        action = LotSyncPlanner(MockLotWriteClient()).plan((self.desired,), (changed,)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.UPDATE_REQUIRED)
        self.assertIn("description", action.changed_fields)
        self.assertIn(LotWriteCapability.UPDATE_DESCRIPTION, action.capability_requirements)

    def test_changed_parameters_are_unsupported_without_update_fields(self) -> None:
        changed = _current(form_fields={"amount": "2"})
        writer = MockLotWriteClient({LotWriteCapability.UPDATE_FIELDS: CapabilityState.UNSUPPORTED})
        action = LotSyncPlanner(writer).plan((self.desired,), (changed,)).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.UNSUPPORTED)
        self.assertIn("form_fields", action.changed_fields)

    def test_unsupported_create_is_reported_without_send(self) -> None:
        writer = MockLotWriteClient({LotWriteCapability.CREATE_LOT: CapabilityState.UNSUPPORTED})
        action = LotSyncPlanner(writer).plan((self.desired,), ()).actions[0]
        self.assertEqual(action.decision, LotSyncDecision.UNSUPPORTED)
        self.assertEqual(writer.calls, [])

    def test_repeated_plan_execution_is_idempotent_and_never_invokes_write_client(self) -> None:
        writer = MockLotWriteClient()
        plan = LotSyncPlanner(writer).plan((self.desired,), ())
        executor = LotSyncDryRunExecutor()
        first, second = executor.execute(plan), executor.execute(plan)
        self.assertEqual(first.actions[0].outcome, DryRunOutcome.REQUESTED)
        self.assertEqual(second.actions[0].outcome, DryRunOutcome.SKIPPED)
        self.assertEqual(writer.calls, [])

    def test_unconfirmed_seasonal_description_is_blocked_unless_safe_neutral_is_configured(self) -> None:
        unsafe = desired_from_catalog(
            self.service,
            title="M+ 10", category_node_id="node-1", form_fields={"amount": "1"},
            description=DescriptionTarget("seasonal claim", DescriptionConfirmation.UNCONFIRMED),
        )
        blocked = LotSyncPlanner(MockLotWriteClient()).plan((unsafe,), ()).actions[0]
        self.assertEqual(blocked.decision, LotSyncDecision.BLOCKED)

        neutral = desired_from_catalog(
            self.service,
            title="M+ 10", category_node_id="node-1", form_fields={"amount": "1"},
            description=DescriptionTarget(
                "seasonal claim", DescriptionConfirmation.UNCONFIRMED, safe_neutral_text="Contact for details"
            ),
        )
        allowed = LotSyncPlanner(MockLotWriteClient()).plan((neutral,), ()).actions[0]
        self.assertEqual(allowed.decision, LotSyncDecision.CREATE_REQUIRED)
        self.assertEqual(allowed.changed_fields["description"], "Contact for details")


class LotSyncLocalIntegrationTests(unittest.TestCase):
    def test_cli_plan_reads_local_catalog_and_registry_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_database = Database(Path(directory) / "catalog.sqlite3")
            registry_database = Database(Path(directory) / "registry.sqlite3")
            catalog_database.initialize()
            registry_database.initialize()
            service = _service()
            ServiceCatalogRepository(catalog_database).replace((service,))
            OwnLotRegistryRepository(registry_database).replace((RegisteredLot(_detail(), LotClassification("unknown", "unmapped")),))
            output = StringIO()
            status = run_plan_sync(
                catalog_database=catalog_database, registry_database=registry_database,
                write_client=MockLotWriteClient(), output=output,
            )
        self.assertEqual(status, 0)
        self.assertIn("services=1", output.getvalue())
        self.assertIn("blocked=1", output.getvalue())


def _service(code: str = "mplus_k10_eu_selfplay_x1") -> CatalogService:
    return CatalogService(
        stable_code=code, family=CatalogFamily.MYTHIC_PLUS,
        variant={"key_level": 10, "region": "eu", "service_format": "selfplay", "package_size": 1},
        enabled=True, desired_state=DesiredState.ENABLED, template_reference="mplus_template",
        description_profile="mplus_verified", price_policy_reference="mplus_policy", price_conditions={"timed": "yes"},
    )


def _desired(service: CatalogService):
    return desired_from_catalog(
        service, title="Mythic+ +10 EU self-play x1", category_node_id="node-1", form_fields={"amount": "1"},
        description=DescriptionTarget("Verified description", DescriptionConfirmation.CONFIRMED),
    )


def _current(*, lot_id: str = "lot-1", category_node_id: str = "node-1", title: str = "Mythic+ +10 EU self-play x1",
             description: str = "Verified description", form_fields: dict[str, str] | None = None):
    from funpay_operations.lot_sync import CurrentLotState
    return CurrentLotState(
        lot_id=lot_id, confirmed_service_code="mplus_k10_eu_selfplay_x1",
        managed_service_family=CatalogFamily.MYTHIC_PLUS, identity_confirmed=True,
        title=title, description=description,
        price_minor=100, is_active=True, category_node_id=category_node_id, form_fields=form_fields or {"amount": "1"},
        service_conditions={"timed": "yes"},
    )


def _detail() -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id="lot-1", title="Mythic+ +10 EU self-play x1", price_minor=100, currency="RUB", seller_id="seller-1",
        category_node_id="node-1", is_active=True, description="Verified description", short_description=None,
        location=None, is_deleted=False, editor_fields={"amount": "1"}, editor_options={}, omitted_field_names=(),
    )
