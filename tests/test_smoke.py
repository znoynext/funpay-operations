from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayError, FunPayLotDetails
from funpay_operations.smoke import run_smoke_test


class SmokeClient:
    def __init__(self, *, local_session: bool = True, authorization: bool = True, fail: bool = False) -> None:
        self.local_session = local_session
        self.authorization = authorization
        self.fail = fail
        self.closed = False

    def has_local_session(self) -> bool:
        return self.local_session

    def check_authorization(self) -> bool:
        return self.authorization

    def get_profile(self) -> object:
        if self.fail:
            raise FunPayError("private detail")
        return object()

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
        return (FunPayLotDetails(
            "lot", "Mythic+ +10 EU self-play x1", 100, "RUB", "owner", "node",
            True, None, None, None, False, {}, {}, (),
        ),)

    def get_dialogs(self) -> tuple[object, ...]:
        return (object(), object())

    def close(self) -> None:
        self.closed = True


class SmokeTests(unittest.TestCase):
    def test_success_output_has_counts_but_no_private_data(self) -> None:
        client = SmokeClient()
        output = io.StringIO()
        self.assertEqual(run_smoke_test(client, output=output), 0)  # type: ignore[arg-type]
        self.assertEqual(
            output.getvalue().strip(),
            "smoke-test: local_session=present authorization=ok profile=ok own_lots_total=1 "
            "managed_mythic_plus=0 unknown_non_managed=1 ambiguous=0 dialogs=2 closed=ok",
        )
        self.assertTrue(client.closed)

    def test_missing_or_failed_session_is_sanitized(self) -> None:
        output = io.StringIO()
        self.assertEqual(run_smoke_test(SmokeClient(local_session=False), output=output), 1)  # type: ignore[arg-type]
        self.assertEqual(output.getvalue().strip(), "smoke-test: local_session=missing_or_invalid")

        output = io.StringIO()
        self.assertEqual(run_smoke_test(SmokeClient(fail=True), output=output), 1)  # type: ignore[arg-type]
        self.assertNotIn("private detail", output.getvalue())

    def test_managed_count_requires_local_mythic_catalog_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            database.initialize()
            with database.session() as connection:
                connection.execute(
                    """INSERT INTO service_catalog
                    (stable_code, family, variant_json, enabled, desired_state, template_reference,
                     description_profile, price_policy_reference, price_conditions_json)
                    VALUES ('mplus-k10', 'mythic_plus', '{}', 1, 'enabled', 'template', 'profile', 'policy', '{}')"""
                )
                connection.execute(
                    """INSERT INTO own_lot_registry
                    (external_id, category_node_id, title, price_minor, currency, is_active, region,
                     short_description, description, location, is_deleted, editor_fields_json,
                     editor_options_json, omitted_field_names_json, available_field_names_json,
                     classification, mapping_state, service_data_json)
                    VALUES ('lot', 'node', 'Mythic+ +10 EU self-play x1', 100, 'RUB', 1, 'eu',
                    NULL, NULL, NULL, 0, '{}', '{}', '[]', '[]', 'mythic_plus', 'mapped', '{}')"""
                )
                connection.execute(
                    "INSERT INTO lot_service_mappings(external_lot_id, service_code) VALUES ('lot', 'mplus-k10')"
                )
            output = io.StringIO()
            self.assertEqual(run_smoke_test(SmokeClient(), output=output, database=database), 0)  # type: ignore[arg-type]
        self.assertIn("managed_mythic_plus=1 unknown_non_managed=0 ambiguous=0", output.getvalue())
