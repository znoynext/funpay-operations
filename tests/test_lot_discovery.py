from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayLotDetails, MockFunPayClient
from funpay_operations.lot_discovery import (
    LotDiscovery,
    OwnLotRegistryRepository,
    classify_wow_lot,
    run_discovery,
)


def lot(lot_id: str, title: str, description: str | None = None) -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id=lot_id, title=title, price_minor=12345, currency="RUB", seller_id="owner",
        category_node_id="42", is_active=True, description=description, short_description=None,
        location=None, is_deleted=False, editor_fields={"price": "123.45", "amount": "1"},
        editor_options={"fields[type]": (("Run", "run"),)},
        omitted_field_names=("secrets",),
    )


class LotDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "operations.sqlite3")
        self.database.initialize()
        self.registry = OwnLotRegistryRepository(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_normalizes_complete_mythic_plus_and_delves_without_guessing(self) -> None:
        mythic = classify_wow_lot(lot("m1", "Mythic+ +10 EU self-play x1", "Own key service"))
        self.assertEqual((mythic.kind, mythic.mapping_state), ("mythic_plus", "mapped"))
        self.assertEqual((mythic.key_level, mythic.region, mythic.service_format, mythic.package_size), (10, "eu", "selfplay", 1))

        delve = classify_wow_lot(lot("d1", "Delves Tier 8 Bountiful EU Pilot x1", "Terms"))
        self.assertEqual((delve.kind, delve.mapping_state), ("delves", "mapped"))
        self.assertEqual((delve.delve_tier, delve.bountiful, delve.region, delve.service_format, delve.package_size), (8, True, "eu", "pilot", 1))

        russian_mythic = classify_wow_lot(lot("m2", "Мифик+ +12 Европа самостоятельно x1", "Условия"))
        self.assertEqual((russian_mythic.kind, russian_mythic.mapping_state), ("mythic_plus", "mapped"))
        self.assertEqual((russian_mythic.key_level, russian_mythic.region, russian_mythic.service_format), (12, "eu", "selfplay"))

    def test_unknown_or_ambiguous_lots_are_unmapped(self) -> None:
        unknown = classify_wow_lot(lot("u1", "WoW help", "No explicit service markers"))
        both = classify_wow_lot(lot("u2", "Mythic+ and Delves Tier 8 EU self-play x1", ""))
        conflicting_region = classify_wow_lot(lot("u3", "Mythic+ +10 EU US self-play x1", ""))
        self.assertEqual((unknown.kind, unknown.mapping_state), ("unknown", "unmapped"))
        self.assertEqual((both.kind, both.mapping_state), ("unknown", "unmapped"))
        self.assertEqual((conflicting_region.kind, conflicting_region.mapping_state), ("mythic_plus", "unmapped"))
        self.assertIsNone(conflicting_region.region)

    def test_registry_is_local_and_only_accepts_mapped_templates(self) -> None:
        client = MockFunPayClient(own_lot_details=(
            lot("m1", "Mythic+ +10 EU self-play x1", "Terms"),
            lot("d1", "Delves Tier 8 Normal EU Pilot x1", "Terms"),
            lot("u1", "Unclassified", None),
        ))
        summary = LotDiscovery(client, self.registry).run()
        self.assertEqual((summary.total, summary.mythic_plus, summary.delves, summary.unmapped), (3, 1, 1, 1))
        self.registry.select_template("mythic_plus", "m1")
        self.registry.select_template("delves", "d1")
        self.assertEqual(self.registry.selected_template_kinds(), ("delves", "mythic_plus"))
        with self.assertRaises(ValueError):
            self.registry.select_template("mythic_plus", "u1")
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT description, editor_fields_json, editor_options_json,
                omitted_field_names_json, category_node_id FROM own_lot_registry WHERE external_id = ?""",
                ("m1",),
            ).fetchone()
        self.assertEqual((row["description"], row["category_node_id"]), ("Terms", "42"))
        self.assertIn('"price":"123.45"', row["editor_fields_json"])
        self.assertIn('"fields[type]":[["Run","run"]]', row["editor_options_json"])
        self.assertEqual(row["omitted_field_names_json"], '["secrets"]')

    def test_safe_summary_does_not_print_lot_id_or_title(self) -> None:
        output = io.StringIO()
        code = run_discovery(
            MockFunPayClient(own_lot_details=(lot("sensitive-id", "Private title", "Terms"),)),
            self.registry, output=output,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue().strip(),
            "discover-lots: own_lots=1 mythic_plus=0 delves=0 unmapped=1 mythic_template=not_selected delves_template=not_selected",
        )
        self.assertNotIn("sensitive-id", output.getvalue())
        self.assertNotIn("Private title", output.getvalue())
