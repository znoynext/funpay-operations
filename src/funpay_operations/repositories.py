"""Repositories for transactional local operational data; no network or logging side effects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .database import Database


@dataclass(frozen=True)
class OwnLot:
    external_id: str
    title: str
    price_minor: int
    currency: str
    description: str = ""


class TrustedSellerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, seller_id: str, nickname: str, notes: str | None = None) -> int:
        if not seller_id.strip() or not nickname.strip():
            raise ValueError("seller_id and nickname must not be empty")
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO trusted_sellers (seller_id, nickname, notes)
                VALUES (?, ?, ?)
                ON CONFLICT(seller_id) DO UPDATE SET
                    nickname = excluded.nickname, notes = excluded.notes, updated_at = CURRENT_TIMESTAMP""",
                (seller_id, nickname, notes),
            )
            return int(connection.execute("SELECT id FROM trusted_sellers WHERE seller_id = ?", (seller_id,)).fetchone()["id"])


class OwnLotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, lot: OwnLot) -> int:
        if lot.price_minor < 0 or not lot.external_id.strip() or not lot.title.strip():
            raise ValueError("lot id, title, and non-negative price are required")
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO own_lots (external_id, title, price_minor, currency, description)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    title = excluded.title, price_minor = excluded.price_minor,
                    currency = excluded.currency, description = excluded.description,
                    updated_at = CURRENT_TIMESTAMP""",
                (lot.external_id, lot.title, lot.price_minor, lot.currency, lot.description),
            )
            return int(connection.execute("SELECT id FROM own_lots WHERE external_id = ?", (lot.external_id,)).fetchone()["id"])

    def map_competitor(self, own_lot_id: int, competitor_lot_id: str, seller_id: int | None = None, url: str | None = None) -> None:
        if not competitor_lot_id.strip():
            raise ValueError("competitor_lot_id must not be empty")
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO competitor_lot_mappings (own_lot_id, seller_id, competitor_lot_id, competitor_url)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(own_lot_id, competitor_lot_id) DO UPDATE SET
                    seller_id = excluded.seller_id, competitor_url = excluded.competitor_url,
                    updated_at = CURRENT_TIMESTAMP""",
                (own_lot_id, seller_id, competitor_lot_id, url),
            )

    def record_price_change(self, own_lot_id: int, new_price_minor: int, reason: str) -> bool:
        if new_price_minor < 0 or not reason.strip():
            raise ValueError("non-negative price and reason are required")
        with self.database.session() as connection:
            current = connection.execute("SELECT price_minor FROM own_lots WHERE id = ?", (own_lot_id,)).fetchone()
            if current is None:
                raise KeyError("own lot does not exist")
            previous = int(current["price_minor"])
            if previous == new_price_minor:
                return False
            connection.execute("UPDATE own_lots SET price_minor = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_price_minor, own_lot_id))
            connection.execute(
                "INSERT INTO price_history (own_lot_id, previous_price_minor, new_price_minor, reason) VALUES (?, ?, ?, ?)",
                (own_lot_id, previous, new_price_minor, reason),
            )
            return True


class DescriptionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_version(self, own_lot_id: int, content: str) -> int | None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.database.session() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO description_versions (own_lot_id, content, content_hash) VALUES (?, ?, ?)",
                (own_lot_id, content, digest),
            )
            row = connection.execute(
                "SELECT id FROM description_versions WHERE own_lot_id = ? AND content_hash = ?", (own_lot_id, digest)
            ).fetchone()
            return int(row["id"]) if row is not None else None


class DialogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_dialog(self, external_id: str, counterparty_id: str | None, counterparty_name: str | None) -> int:
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO funpay_dialogs (external_id, counterparty_id, counterparty_name)
                VALUES (?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    counterparty_id = excluded.counterparty_id, counterparty_name = excluded.counterparty_name,
                    updated_at = CURRENT_TIMESTAMP""",
                (external_id, counterparty_id, counterparty_name),
            )
            return int(connection.execute("SELECT id FROM funpay_dialogs WHERE external_id = ?", (external_id,)).fetchone()["id"])

    def store_message(self, external_id: str, dialog_id: int, direction: str, body: str, sent_at: str) -> bool:
        if direction not in {"incoming", "outgoing"} or not external_id.strip() or not sent_at.strip():
            raise ValueError("message id, direction, and timestamp are required")
        with self.database.session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO funpay_messages (external_id, dialog_id, direction, body, sent_at)
                VALUES (?, ?, ?, ?, ?)""",
                (external_id, dialog_id, direction, body, sent_at),
            )
            return cursor.rowcount == 1


class EventRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def mark_processed(self, event_key: str, event_type: str, payload_json: str | None = None) -> bool:
        if not event_key.strip() or not event_type.strip():
            raise ValueError("event key and type are required")
        with self.database.session() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO processed_events (event_key, event_type, payload_json) VALUES (?, ?, ?)",
                (event_key, event_type, payload_json),
            )
            return cursor.rowcount == 1


class TaskStateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, task_name: str, state: str, cursor: str | None = None, last_error: str | None = None) -> None:
        if not task_name.strip() or not state.strip():
            raise ValueError("task name and state are required")
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO task_state (task_name, state, cursor, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_name) DO UPDATE SET state = excluded.state, cursor = excluded.cursor,
                    last_error = excluded.last_error, updated_at = CURRENT_TIMESTAMP""",
                (task_name, state, cursor, last_error),
            )


class PriceSnapshotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, snapshot_key: str, own_lot_id: int, price_minor: int, currency: str, reason: str, description_version_id: int | None = None) -> bool:
        if not snapshot_key.strip() or price_minor < 0 or not reason.strip():
            raise ValueError("snapshot key, non-negative price, and reason are required")
        with self.database.session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO price_snapshots
                (snapshot_key, own_lot_id, price_minor, currency, description_version_id, reason)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (snapshot_key, own_lot_id, price_minor, currency, description_version_id, reason),
            )
            return cursor.rowcount == 1
