from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayMessage, FunPayNetworkUnavailable
from funpay_operations.notifications import FunPayMessageNotifier
from funpay_operations.repositories import DialogRepository, TaskStateRepository, TelegramMessageLinkRepository


class FakeFunPayClient:
    def __init__(self, messages: tuple[FunPayMessage, ...] = ()) -> None:
        self.messages = messages
        self.raise_unavailable = False
        self.cursors: list[str | None] = []

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        self.cursors.append(after_message_id)
        if self.raise_unavailable:
            raise FunPayNetworkUnavailable("offline")
        return self.messages


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Mapping[str, object]]] = []

    def send_private_notification(self, recipient_id: int, text: str, reply_markup: Mapping[str, object]) -> int:
        self.sent.append((recipient_id, text, reply_markup))
        return len(self.sent)


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "operations.sqlite3")
        self.database.initialize()
        self.funpay = FakeFunPayClient()
        self.sender = RecordingSender()
        self.logger = logging.getLogger("funpay_operations.notifications.tests")
        self.notifier = FunPayMessageNotifier(
            self.funpay, self.sender, DialogRepository(self.database), TelegramMessageLinkRepository(self.database),
            TaskStateRepository(self.database), 1001, self.logger,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def message(message_id: str, dialog_id: str, direction: str = "incoming", *,
                buyer: str | None = "buyer", related: str | None = None, url: str | None = None) -> FunPayMessage:
        return FunPayMessage(message_id, dialog_id, direction, "private message body", "2026-08-06T00:00:00Z", buyer, related, url)

    def test_deduplicates_repeated_incoming_message(self) -> None:
        self.funpay.messages = (self.message("m-1", "dialog-a", related="Lot A"),)
        self.notifier.sync()
        self.notifier.sync()

        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.sender.sent[0][0], 1001)
        self.assertIn("Покупатель: buyer", self.sender.sent[0][1])
        self.assertIn("Лот/заказ: Lot A", self.sender.sent[0][1])
        self.assertEqual(self.funpay.cursors, [None, '{"dialog-a":"m-1"}'])
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM telegram_message_links").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT telegram_message_id FROM telegram_message_links").fetchone()[0], 1)

    def test_routes_each_dialog_separately_and_ignores_outgoing(self) -> None:
        self.funpay.messages = (
            self.message("m-1", "dialog-a", buyer="Alice", related="Order 1"),
            self.message("m-2", "dialog-b", buyer="Bob", url="https://funpay.com/chat/example"),
            self.message("m-3", "dialog-a", direction="outgoing", buyer="Alice"),
        )
        self.notifier.sync()

        self.assertEqual(len(self.sender.sent), 2)
        self.assertIn("Alice", self.sender.sent[0][1])
        self.assertIn("Bob", self.sender.sent[1][1])
        first_markup = self.sender.sent[0][2]["inline_keyboard"]
        second_markup = self.sender.sent[1][2]["inline_keyboard"]
        self.assertNotEqual(first_markup[0][0]["callback_data"], second_markup[0][0]["callback_data"])
        self.assertEqual(second_markup[1][0]["url"], "https://funpay.com/chat/example")
        self.assertEqual(
            TaskStateRepository(self.database).load("funpay_message_notifications"),
            ("running", '{"dialog-a":"m-3","dialog-b":"m-2"}'),
        )
        with self.database.session() as connection:
            rows = connection.execute(
                """SELECT d.external_id, COUNT(link.id) AS links
                FROM funpay_dialogs d LEFT JOIN telegram_message_links link ON link.funpay_dialog_id = d.id
                GROUP BY d.external_id ORDER BY d.external_id"""
            ).fetchall()
        self.assertEqual([(row["external_id"], row["links"]) for row in rows], [("dialog-a", 1), ("dialog-b", 1)])

    def test_recovers_after_funpay_network_failure_without_logging_body(self) -> None:
        self.funpay.raise_unavailable = True
        with self.assertLogs(self.logger, level="WARNING") as captured:
            self.notifier.sync()
        self.assertIsNone(TaskStateRepository(self.database).load("funpay_message_notifications"))
        self.assertNotIn("private message body", captured.output[0])

        self.funpay.raise_unavailable = False
        self.funpay.messages = (self.message("m-4", "dialog-c"),)
        self.notifier.sync()
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(
            TaskStateRepository(self.database).load("funpay_message_notifications"),
            ("running", '{"dialog-c":"m-4"}'),
        )
