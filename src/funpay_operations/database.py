"""Transactional, versioned SQLite storage for local operational data."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


Migration = tuple[int, str, tuple[str, ...]]


MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "initial operational schema",
        (
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            # Kept for backward compatibility with the first project scaffold.
            """CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY,
                external_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                currency TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS trusted_sellers (
                id INTEGER PRIMARY KEY,
                seller_id TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS own_lots (
                id INTEGER PRIMARY KEY,
                external_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                currency TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS competitor_lot_mappings (
                id INTEGER PRIMARY KEY,
                own_lot_id INTEGER NOT NULL REFERENCES own_lots(id) ON DELETE CASCADE,
                seller_id INTEGER REFERENCES trusted_sellers(id) ON DELETE SET NULL,
                competitor_lot_id TEXT NOT NULL,
                competitor_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(own_lot_id, competitor_lot_id)
            )""",
            """CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY,
                own_lot_id INTEGER NOT NULL REFERENCES own_lots(id) ON DELETE CASCADE,
                previous_price_minor INTEGER CHECK (previous_price_minor IS NULL OR previous_price_minor >= 0),
                new_price_minor INTEGER NOT NULL CHECK (new_price_minor >= 0),
                reason TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS description_versions (
                id INTEGER PRIMARY KEY,
                own_lot_id INTEGER NOT NULL REFERENCES own_lots(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(own_lot_id, content_hash)
            )""",
            """CREATE TABLE IF NOT EXISTS funpay_dialogs (
                id INTEGER PRIMARY KEY,
                external_id TEXT NOT NULL UNIQUE,
                counterparty_id TEXT,
                counterparty_name TEXT,
                last_message_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS funpay_messages (
                id INTEGER PRIMARY KEY,
                external_id TEXT NOT NULL UNIQUE,
                dialog_id INTEGER NOT NULL REFERENCES funpay_dialogs(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS processed_events (
                id INTEGER PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS task_state (
                task_name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                cursor TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY,
                snapshot_key TEXT NOT NULL UNIQUE,
                own_lot_id INTEGER NOT NULL REFERENCES own_lots(id) ON DELETE CASCADE,
                price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                currency TEXT NOT NULL,
                description_version_id INTEGER REFERENCES description_versions(id) ON DELETE SET NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_price_history_lot ON price_history(own_lot_id, changed_at)",
            "CREATE INDEX IF NOT EXISTS idx_messages_dialog ON funpay_messages(dialog_id, sent_at)",
        ),
    ),
    (
        2,
        "telegram notification links",
        (
            """CREATE TABLE IF NOT EXISTS telegram_message_links (
                id INTEGER PRIMARY KEY,
                funpay_message_id INTEGER NOT NULL UNIQUE REFERENCES funpay_messages(id) ON DELETE CASCADE,
                funpay_dialog_id INTEGER NOT NULL REFERENCES funpay_dialogs(id) ON DELETE CASCADE,
                telegram_chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_chat_id, telegram_message_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_telegram_links_dialog ON telegram_message_links(funpay_dialog_id)",
        ),
    ),
    (
        3,
        "telegram FunPay reply state",
        (
            """CREATE TABLE IF NOT EXISTS telegram_reply_modes (
                telegram_chat_id INTEGER PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                funpay_dialog_id INTEGER NOT NULL REFERENCES funpay_dialogs(id) ON DELETE CASCADE,
                buyer_nickname TEXT NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS funpay_reply_attempts (
                id INTEGER PRIMARY KEY,
                telegram_update_id INTEGER NOT NULL UNIQUE,
                telegram_chat_id INTEGER NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                funpay_dialog_id INTEGER NOT NULL REFERENCES funpay_dialogs(id) ON DELETE CASCADE,
                buyer_nickname TEXT NOT NULL,
                body TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('sending', 'sent', 'failed', 'cancelled')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_reply_attempts_chat ON funpay_reply_attempts(telegram_chat_id, state)",
        ),
    ),
    (
        4,
        "FunPay automatic greeting state",
        (
            """CREATE TABLE IF NOT EXISTS funpay_auto_replies (
                id INTEGER PRIMARY KEY,
                trigger_funpay_message_id INTEGER NOT NULL UNIQUE REFERENCES funpay_messages(id) ON DELETE CASCADE,
                funpay_dialog_id INTEGER NOT NULL UNIQUE REFERENCES funpay_dialogs(id) ON DELETE CASCADE,
                trigger_sent_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('sending', 'sent', 'failed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_auto_replies_dialog ON funpay_auto_replies(funpay_dialog_id, trigger_sent_at)",
        ),
    ),
    (
        5,
        "read-only own lot registry",
        (
            """CREATE TABLE IF NOT EXISTS own_lot_registry (
                external_id TEXT PRIMARY KEY,
                category_node_id TEXT,
                title TEXT NOT NULL,
                price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
                currency TEXT NOT NULL,
                is_active INTEGER CHECK (is_active IN (0, 1) OR is_active IS NULL),
                region TEXT,
                short_description TEXT,
                description TEXT,
                location TEXT,
                is_deleted INTEGER CHECK (is_deleted IN (0, 1) OR is_deleted IS NULL),
                editor_fields_json TEXT NOT NULL,
                editor_options_json TEXT NOT NULL,
                omitted_field_names_json TEXT NOT NULL,
                available_field_names_json TEXT NOT NULL,
                classification TEXT NOT NULL CHECK (classification IN ('mythic_plus', 'delves', 'unknown')),
                mapping_state TEXT NOT NULL CHECK (mapping_state IN ('mapped', 'unmapped')),
                service_data_json TEXT NOT NULL,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS own_lot_templates (
                template_kind TEXT PRIMARY KEY CHECK (template_kind IN ('mythic_plus', 'delves')),
                external_lot_id TEXT NOT NULL REFERENCES own_lot_registry(external_id) ON DELETE CASCADE,
                selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_own_lot_registry_classification ON own_lot_registry(classification, mapping_state)",
        ),
    ),
    (
        6,
        "local service catalog",
        (
            """CREATE TABLE IF NOT EXISTS service_catalog (
                stable_code TEXT PRIMARY KEY,
                family TEXT NOT NULL CHECK (family IN ('mythic_plus', 'delves')),
                variant_json TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                desired_state TEXT NOT NULL CHECK (desired_state IN ('enabled', 'disabled')),
                template_reference TEXT NOT NULL,
                description_profile TEXT NOT NULL,
                price_policy_reference TEXT NOT NULL,
                price_conditions_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_service_catalog_family ON service_catalog(family, enabled)",
        ),
    ),
    (
        7,
        "confirmed lot service mappings",
        (
            """CREATE TABLE IF NOT EXISTS lot_service_mappings (
                external_lot_id TEXT PRIMARY KEY REFERENCES own_lot_registry(external_id) ON DELETE CASCADE,
                service_code TEXT NOT NULL UNIQUE,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
        ),
    ),
    (
        8,
        "trusted seller matching engine",
        (
            """CREATE TABLE IF NOT EXISTS trusted_seller_profiles (
                seller_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                family TEXT NOT NULL CHECK (family IN ('mythic_plus', 'delves')),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                verification_state TEXT NOT NULL CHECK (verification_state IN ('pending', 'verified', 'rejected')),
                last_checked_state TEXT NOT NULL CHECK (last_checked_state IN ('never', 'current', 'changed', 'error')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS competitor_service_mappings (
                seller_id TEXT NOT NULL REFERENCES trusted_seller_profiles(seller_id) ON DELETE CASCADE,
                competitor_lot_id TEXT NOT NULL,
                service_code TEXT NOT NULL,
                mapping_state TEXT NOT NULL CHECK (mapping_state IN ('confirmed', 'revalidation_required')),
                material_snapshot_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (seller_id, competitor_lot_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_competitor_service_mappings_code ON competitor_service_mappings(service_code, mapping_state)",
        ),
    ),
)


class MigrationError(RuntimeError):
    """Raised when migration metadata is inconsistent with the program schema."""


class Database:
    """Owns SQLite connections, transactions, and idempotent migrations."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction and always close its SQLite file handle."""

        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.apply_migrations()

    def apply_migrations(self) -> None:
        """Apply each migration once; a failure rolls back the incomplete version."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            applied = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}
            known = {version for version, _, _ in MIGRATIONS}
            if not applied.issubset(known):
                raise MigrationError("database contains a migration version unknown to this program")
            for version, name, statements in MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, name))

    def applied_migrations(self) -> tuple[int, ...]:
        with self.session() as connection:
            return tuple(row["version"] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))
