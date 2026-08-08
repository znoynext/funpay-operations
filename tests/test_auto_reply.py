from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from funpay_operations.auto_reply import AUTO_REPLY_TEXT, AutoReplyService
from funpay_operations.database import Database
from funpay_operations.funpay import FunPayMessage, FunPayProtocolError
from funpay_operations.notifications import FunPayMessageNotifier
from funpay_operations.repositories import AutoReplyRepository, DialogRepository, TaskStateRepository, TelegramMessageLinkRepository


class FakeInbox:
    def __init__(self) -> None:
        self.messages: tuple[FunPayMessage, ...] = ()

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        return self.messages


class RecordingTelegram:
    def __init__(self) -> None:
        self.notifications: list[str] = []

    def send_private_notification(self, recipient_id: int, text: str, reply_markup: Mapping[str, object]) -> int:
        self.notifications.append(text)
        return len(self.notifications)


class RecordingReplyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail = False

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        self.calls.append((dialog_id, buyer_nickname, body, idempotency_key))
        if self.fail:
            raise FunPayProtocolError("offline")


class AutoReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "operations.sqlite3")
        self.database.initialize()
        self.inbox = FakeInbox()
        self.telegram = RecordingTelegram()
        self.replies = RecordingReplyClient()
        self.states = TaskStateRepository(self.database)
        self.auto = AutoReplyService(
            self.replies, AutoReplyRepository(self.database), self.states,
            logging.getLogger("funpay_operations.auto_reply.tests"), default_enabled=True,
        )
        self.notifier = FunPayMessageNotifier(
            self.inbox, self.telegram, DialogRepository(self.database), TelegramMessageLinkRepository(self.database),
            self.states, 1001, logging.getLogger("funpay_operations.auto_reply.tests"), self.auto,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def message(identifier: str, dialog: str, direction: str, sent_at: str) -> FunPayMessage:
        return FunPayMessage(identifier, dialog, direction, "private body", sent_at, "Buyer")

    def test_historical_messages_are_not_greeted_but_notifications_are_sent(self) -> None:
        self.inbox.messages = (self.message("old", "old-dialog", "incoming", "2026-08-01T10:00:00Z"),)
        self.notifier.sync()

        self.assertEqual(len(self.telegram.notifications), 1)
        self.assertEqual(self.replies.calls, [])
        self.assertTrue(self.auto.is_initialized())

    def test_first_new_message_gets_exactly_one_exact_greeting(self) -> None:
        self.notifier.sync()  # marks the historical bootstrap complete
        self.inbox.messages = (self.message("new", "new-dialog", "incoming", "2026-08-06T10:00:00Z"),)
        self.notifier.sync()
        self.notifier.sync()

        self.assertEqual(len(self.telegram.notifications), 1)
        self.assertEqual(self.replies.calls, [("new-dialog", "Buyer", "Привет", "auto-reply-1")])
        self.assertEqual(self.replies.calls[0][2], AUTO_REPLY_TEXT)

    def test_multiple_incoming_messages_in_one_dialog_never_repeat_greeting(self) -> None:
        self.notifier.sync()
        self.inbox.messages = (self.message("m1", "dialog", "incoming", "2026-08-06T10:00:00Z"),)
        self.notifier.sync()
        self.inbox.messages = (self.message("m2", "dialog", "incoming", "2026-08-08T10:00:00Z"),)
        self.notifier.sync()
        self.inbox.messages = (self.message("m3", "dialog", "incoming", "2026-08-12T10:00:00Z"),)
        self.notifier.sync()

        self.assertEqual(len(self.replies.calls), 1)
        self.assertEqual(self.replies.calls[0][0], "dialog")

    def test_restart_does_not_reset_dialog_greeting_state(self) -> None:
        self.notifier.sync()
        self.inbox.messages = (self.message("m1", "dialog", "incoming", "2026-08-06T10:00:00Z"),)
        self.notifier.sync()

        restarted_auto = AutoReplyService(
            self.replies, AutoReplyRepository(self.database), self.states,
            logging.getLogger("funpay_operations.auto_reply.tests"), default_enabled=True,
        )
        restarted_notifier = FunPayMessageNotifier(
            self.inbox, self.telegram, DialogRepository(self.database), TelegramMessageLinkRepository(self.database),
            self.states, 1001, logging.getLogger("funpay_operations.auto_reply.tests"), restarted_auto,
        )
        self.inbox.messages = (self.message("m2", "dialog", "incoming", "2026-08-10T10:00:00Z"),)
        restarted_notifier.sync()

        self.assertEqual(len(self.replies.calls), 1)

    def test_owner_message_never_triggers_greeting(self) -> None:
        self.notifier.sync()
        self.inbox.messages = (self.message("owner-1", "dialog", "outgoing", "2026-08-06T10:00:00Z"),)
        self.notifier.sync()

        self.assertEqual(self.telegram.notifications, [])
        self.assertEqual(self.replies.calls, [])

    def test_telegram_notification_does_not_depend_on_auto_reply_success(self) -> None:
        self.notifier.sync()
        self.replies.fail = True
        self.inbox.messages = (self.message("m1", "dialog", "incoming", "2026-08-06T10:00:00Z"),)
        with self.assertLogs("funpay_operations.auto_reply.tests", level="WARNING") as logged:
            self.notifier.sync()

        self.assertEqual(len(self.telegram.notifications), 1)
        self.assertEqual(self.replies.calls[0][2], "Привет")
        self.assertEqual(logged.output, ["WARNING:funpay_operations.auto_reply.tests:Automatic greeting was not sent"])

    def test_disabled_state_prevents_auto_reply(self) -> None:
        self.notifier.sync()
        self.states.save("funpay_auto_reply", "disabled")
        self.inbox.messages = (self.message("m1", "dialog", "incoming", "2026-08-06T10:00:00Z"),)
        self.notifier.sync()

        self.assertEqual(self.replies.calls, [])
