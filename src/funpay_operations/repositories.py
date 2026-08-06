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


@dataclass(frozen=True)
class StoredFunPayMessage:
    local_id: int
    is_new: bool


@dataclass(frozen=True)
class ReplyTarget:
    local_dialog_id: int
    external_dialog_id: str
    buyer_nickname: str


@dataclass(frozen=True)
class ReplyAttempt:
    attempt_id: int
    telegram_update_id: int
    telegram_chat_id: int
    telegram_user_id: int
    target: ReplyTarget
    body: str
    idempotency_key: str
    state: str


@dataclass(frozen=True)
class AutoReplyAttempt:
    attempt_id: int
    target: ReplyTarget
    idempotency_key: str


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
        return self.store_message_with_id(external_id, dialog_id, direction, body, sent_at).is_new

    def store_message_with_id(
        self, external_id: str, dialog_id: int, direction: str, body: str, sent_at: str
    ) -> StoredFunPayMessage:
        if direction not in {"incoming", "outgoing"} or not external_id.strip() or not sent_at.strip():
            raise ValueError("message id, direction, and timestamp are required")
        with self.database.session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO funpay_messages (external_id, dialog_id, direction, body, sent_at)
                VALUES (?, ?, ?, ?, ?)""",
                (external_id, dialog_id, direction, body, sent_at),
            )
            row = connection.execute(
                "SELECT id FROM funpay_messages WHERE external_id = ?", (external_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("stored FunPay message could not be retrieved")
            return StoredFunPayMessage(int(row["id"]), cursor.rowcount == 1)


class TelegramMessageLinkRepository:
    """Durable at-most-once delivery links; message bodies never leave SQLite here."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def is_linked(self, funpay_message_id: int) -> bool:
        with self.database.session() as connection:
            return connection.execute(
                "SELECT 1 FROM telegram_message_links WHERE funpay_message_id = ?", (funpay_message_id,)
            ).fetchone() is not None

    def link(self, funpay_message_id: int, funpay_dialog_id: int, telegram_chat_id: int,
             telegram_message_id: int) -> bool:
        with self.database.session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO telegram_message_links
                (funpay_message_id, funpay_dialog_id, telegram_chat_id, telegram_message_id)
                VALUES (?, ?, ?, ?)""",
                (funpay_message_id, funpay_dialog_id, telegram_chat_id, telegram_message_id),
            )
            return cursor.rowcount == 1

    def target_for_notification(self, telegram_chat_id: int, telegram_message_id: int) -> ReplyTarget | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT d.id, d.external_id, d.counterparty_name
                FROM telegram_message_links link JOIN funpay_dialogs d ON d.id = link.funpay_dialog_id
                WHERE link.telegram_chat_id = ? AND link.telegram_message_id = ?""",
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
        return _reply_target(row)

    def target_for_dialog(self, telegram_chat_id: int, local_dialog_id: int) -> ReplyTarget | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT d.id, d.external_id, d.counterparty_name
                FROM telegram_message_links link JOIN funpay_dialogs d ON d.id = link.funpay_dialog_id
                WHERE link.telegram_chat_id = ? AND d.id = ?
                ORDER BY link.id DESC LIMIT 1""",
                (telegram_chat_id, local_dialog_id),
            ).fetchone()
        return _reply_target(row)


class ReplyRepository:
    """Durable pending-reply and idempotent-send state, including reply bodies locally only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def arm_mode(self, user_id: int, chat_id: int, target: ReplyTarget, expires_at_epoch: int) -> None:
        with self.database.session() as connection:
            connection.execute(
                """INSERT INTO telegram_reply_modes
                (telegram_chat_id, telegram_user_id, funpay_dialog_id, buyer_nickname, expires_at_epoch)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET telegram_user_id = excluded.telegram_user_id,
                    funpay_dialog_id = excluded.funpay_dialog_id, buyer_nickname = excluded.buyer_nickname,
                    expires_at_epoch = excluded.expires_at_epoch""",
                (chat_id, user_id, target.local_dialog_id, target.buyer_nickname, expires_at_epoch),
            )

    def consume_mode(self, user_id: int, chat_id: int, now_epoch: int) -> ReplyTarget | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT mode.funpay_dialog_id, d.external_id, mode.buyer_nickname, mode.expires_at_epoch
                FROM telegram_reply_modes mode JOIN funpay_dialogs d ON d.id = mode.funpay_dialog_id
                WHERE mode.telegram_chat_id = ? AND mode.telegram_user_id = ?""",
                (chat_id, user_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM telegram_reply_modes WHERE telegram_chat_id = ?", (chat_id,))
        if int(row["expires_at_epoch"]) < now_epoch:
            return None
        return ReplyTarget(int(row["funpay_dialog_id"]), row["external_id"], row["buyer_nickname"])

    def create_attempt(self, telegram_update_id: int, user_id: int, chat_id: int,
                       target: ReplyTarget, body: str) -> ReplyAttempt:
        key = f"telegram-update-{telegram_update_id}"
        with self.database.session() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO funpay_reply_attempts
                (telegram_update_id, telegram_chat_id, telegram_user_id, funpay_dialog_id, buyer_nickname,
                 body, idempotency_key, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sending')""",
                (telegram_update_id, chat_id, user_id, target.local_dialog_id, target.buyer_nickname, body, key),
            )
            row = connection.execute(
                """SELECT attempt.*, dialog.external_id AS external_dialog_id
                FROM funpay_reply_attempts attempt JOIN funpay_dialogs dialog ON dialog.id = attempt.funpay_dialog_id
                WHERE attempt.telegram_update_id = ?""",
                (telegram_update_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("reply attempt could not be retrieved")
        return _reply_attempt(row)

    def claim_retry(self, attempt_id: int, user_id: int, chat_id: int) -> ReplyAttempt | None:
        with self.database.session() as connection:
            cursor = connection.execute(
                """UPDATE funpay_reply_attempts SET state = 'sending', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND telegram_user_id = ? AND telegram_chat_id = ? AND state = 'failed'""",
                (attempt_id, user_id, chat_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """SELECT attempt.*, dialog.external_id AS external_dialog_id
                FROM funpay_reply_attempts attempt JOIN funpay_dialogs dialog ON dialog.id = attempt.funpay_dialog_id
                WHERE attempt.id = ?""",
                (attempt_id,),
            ).fetchone()
        return _reply_attempt(row)

    def mark(self, attempt_id: int, state: str) -> None:
        if state not in {"sent", "failed", "cancelled"}:
            raise ValueError("invalid reply state")
        with self.database.session() as connection:
            connection.execute(
                "UPDATE funpay_reply_attempts SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, attempt_id),
            )


class AutoReplyRepository:
    """Local idempotency ledger for the exact automatic greeting text."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def previous_message_time(self, local_dialog_id: int, local_message_id: int) -> str | None:
        with self.database.session() as connection:
            row = connection.execute(
                """SELECT sent_at FROM funpay_messages
                WHERE dialog_id = ? AND id != ? ORDER BY sent_at DESC, id DESC LIMIT 1""",
                (local_dialog_id, local_message_id),
            ).fetchone()
        return row["sent_at"] if row else None

    def claim(self, local_message_id: int, target: ReplyTarget, trigger_sent_at: str) -> AutoReplyAttempt | None:
        with self.database.session() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO funpay_auto_replies
                (trigger_funpay_message_id, funpay_dialog_id, trigger_sent_at, idempotency_key, state)
                VALUES (?, ?, ?, ?, 'sending')""",
                (local_message_id, target.local_dialog_id, trigger_sent_at, f"auto-reply-{local_message_id}"),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT id, idempotency_key FROM funpay_auto_replies WHERE trigger_funpay_message_id = ?",
                (local_message_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("automatic reply attempt could not be retrieved")
        return AutoReplyAttempt(int(row["id"]), target, row["idempotency_key"])

    def mark(self, attempt_id: int, state: str) -> None:
        if state not in {"sent", "failed"}:
            raise ValueError("invalid automatic reply state")
        with self.database.session() as connection:
            connection.execute(
                "UPDATE funpay_auto_replies SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, attempt_id),
            )


def _reply_target(row: object) -> ReplyTarget | None:
    if row is None or not row["external_id"] or not row["counterparty_name"]:
        return None
    return ReplyTarget(int(row["id"]), row["external_id"], row["counterparty_name"])


def _reply_attempt(row: object) -> ReplyAttempt:
    if row is None:
        raise RuntimeError("reply attempt is missing")
    return ReplyAttempt(
        int(row["id"]), int(row["telegram_update_id"]), int(row["telegram_chat_id"]), int(row["telegram_user_id"]),
        ReplyTarget(int(row["funpay_dialog_id"]), row["external_dialog_id"], row["buyer_nickname"]),
        row["body"], row["idempotency_key"], row["state"],
    )


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

    def load(self, task_name: str) -> tuple[str, str | None] | None:
        if not task_name.strip():
            raise ValueError("task name is required")
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT state, cursor FROM task_state WHERE task_name = ?", (task_name,)
            ).fetchone()
        return (row["state"], row["cursor"]) if row else None


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
