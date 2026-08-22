from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.repositories import (
    DescriptionRepository,
    DialogRepository,
    EventRepository,
    OwnLot,
    OwnLotRepository,
    PriceSnapshotRepository,
    TaskStateRepository,
    TrustedSellerRepository,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "operations.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migrations_are_idempotent_and_preserve_existing_data(self) -> None:
        with self.database.session() as connection:
            connection.execute("INSERT INTO lots (external_id, title, price_minor, currency) VALUES (?, ?, ?, ?)", ("legacy", "Legacy", 1, "RUB"))
        self.database.apply_migrations()
        self.assertEqual(self.database.applied_migrations(), (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lots").fetchone()[0], 1)

    def test_transaction_rolls_back_after_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.database.session() as connection:
                connection.execute("INSERT INTO trusted_sellers (seller_id, nickname) VALUES (?, ?)", ("seller", "nick"))
                raise RuntimeError("simulated failure")
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM trusted_sellers").fetchone()[0], 0)

    def test_existing_database_is_integrity_checked_and_backed_up_before_migration(self) -> None:
        root = Path(self.temporary_directory.name) / "migration-copy"
        database = Database(root / "data" / "state.sqlite3")
        database.initialize()
        with database.session() as connection:
            connection.execute(
                "INSERT INTO lots (external_id, title, price_minor, currency) VALUES ('kept', 'Kept', 1, 'RUB')"
            )
            connection.execute("DROP TABLE read_only_probe_state")
            connection.execute("DELETE FROM schema_migrations WHERE version = 13")

        database.initialize()

        backups = tuple((root / "backups").glob("state.pre-migration.*.sqlite3"))
        self.assertEqual(len(backups), 1)
        copy = sqlite3.connect(backups[0])
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM lots WHERE external_id = 'kept'").fetchone()[0], 1)
        finally:
            copy.close()
        self.assertEqual(database.applied_migrations()[-1], 14)
        database.check_integrity()

    def test_mythic_onboarding_migration_preserves_legacy_registry_and_creates_backup(self) -> None:
        root = Path(self.temporary_directory.name) / "onboarding-migration"
        database = Database(root / "data" / "state.sqlite3")
        database.initialize()
        with database.session() as connection:
            connection.execute(
                """INSERT INTO own_lot_registry
                (external_id,title,price_minor,currency,editor_fields_json,editor_options_json,
                 omitted_field_names_json,available_field_names_json,classification,mapping_state,service_data_json)
                VALUES('legacy-local-only','Legacy',1,'RUB','{}','{}','[]','[]','unknown','unmapped','{}')"""
            )
            for table in (
                "own_lot_mapping_reviews", "mythic_minimum_prices",
                "read_only_request_budgets", "read_only_readiness_state",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version=14")

        database.initialize()

        self.assertEqual(len(tuple((root / "backups").glob("state.pre-migration.*.sqlite3"))), 1)
        with database.session() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM own_lot_registry WHERE external_id='legacy-local-only'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM read_only_readiness_state").fetchone()[0], 1)
        database.check_integrity()

    def test_repositories_prevent_duplicate_events_messages_and_snapshots(self) -> None:
        seller_id = TrustedSellerRepository(self.database).upsert("seller-1", "trusted")
        lots = OwnLotRepository(self.database)
        lot_id = lots.upsert(OwnLot("own-1", "Own lot", 100, "RUB", "Description"))
        lots.map_competitor(lot_id, "competitor-1", seller_id, "https://example.invalid/lot")
        self.assertTrue(lots.record_price_change(lot_id, 110, "manual"))
        self.assertFalse(lots.record_price_change(lot_id, 110, "manual"))

        version_id = DescriptionRepository(self.database).save_version(lot_id, "Description")
        self.assertEqual(version_id, DescriptionRepository(self.database).save_version(lot_id, "Description"))
        snapshots = PriceSnapshotRepository(self.database)
        self.assertTrue(snapshots.create("snapshot-1", lot_id, 110, "RUB", "before update", version_id))
        self.assertFalse(snapshots.create("snapshot-1", lot_id, 110, "RUB", "before update", version_id))

        dialogs = DialogRepository(self.database)
        dialog_id = dialogs.upsert_dialog("dialog-1", "buyer-1", "Buyer")
        self.assertTrue(dialogs.store_message("message-1", dialog_id, "incoming", "local only", "2026-01-01T00:00:00Z"))
        self.assertFalse(dialogs.store_message("message-1", dialog_id, "incoming", "local only", "2026-01-01T00:00:00Z"))
        events = EventRepository(self.database)
        self.assertTrue(events.mark_processed("event-1", "message"))
        self.assertFalse(events.mark_processed("event-1", "message"))
        TaskStateRepository(self.database).save("inbox", "idle", cursor="message-1")

        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT state FROM task_state WHERE task_name = ?", ("inbox",)).fetchone()[0], "idle")

    def test_foreign_keys_reject_orphan_message(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            DialogRepository(self.database).store_message("message-orphan", 999, "incoming", "body", "2026-01-01T00:00:00Z")
