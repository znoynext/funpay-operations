from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayProtocolError
from funpay_operations.replies import FunPayReplyRouter
from funpay_operations.repositories import DialogRepository, ReplyRepository, TelegramMessageLinkRepository
from funpay_operations.telegram import TelegramUpdate


class MockFunPayReplyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail = False

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        self.calls.append((dialog_id, buyer_nickname, body, idempotency_key))
        if self.fail:
            raise FunPayProtocolError("simulated endpoint error")


class ReplyRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "operations.sqlite3")
        self.database.initialize()
        dialogs = DialogRepository(self.database)
        self.alice_id = dialogs.upsert_dialog("funpay-alice", "buyer-a", "Alice")
        alice_message = dialogs.store_message_with_id("incoming-a", self.alice_id, "incoming", "local", "now")
        self.bob_id = dialogs.upsert_dialog("funpay-bob", "buyer-b", "Bob")
        bob_message = dialogs.store_message_with_id("incoming-b", self.bob_id, "incoming", "local", "now")
        self.links = TelegramMessageLinkRepository(self.database)
        self.links.link(alice_message.local_id, self.alice_id, 1001, 71)
        self.links.link(bob_message.local_id, self.bob_id, 1001, 72)
        self.reply_client = MockFunPayReplyClient()
        self.now = 1000.0
        self.router = FunPayReplyRouter(
            (1001,), self.links, ReplyRepository(self.database), self.reply_client, clock=lambda: self.now
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def update(identifier: int, text: str | None = None, *, reply_to: int | None = None,
               callback: str | None = None, user_id: int = 1001, chat_id: int = 1001) -> TelegramUpdate:
        return TelegramUpdate(identifier, user_id, chat_id, text, reply_to, callback)

    def test_reply_to_notification_sends_only_to_linked_buyer_once(self) -> None:
        result = self.router.handle(self.update(1, "Ответ для Alice", reply_to=71))
        duplicate = self.router.handle(self.update(1, "Ответ для Alice", reply_to=71))

        self.assertEqual(result.text, "Ответ покупателю отправлен.")
        self.assertEqual(duplicate.text, "Ответ уже был отправлен.")
        self.assertEqual(self.reply_client.calls, [
            ("funpay-alice", "Alice", "Ответ для Alice", "telegram-update-1")
        ])
        self.assertNotIn("funpay-bob", [call[0] for call in self.reply_client.calls])

    def test_button_reply_uses_verified_dialog_and_expires_after_five_minutes(self) -> None:
        prompt = self.router.handle(self.update(2, callback=f"funpay_reply:{self.alice_id}"))
        result = self.router.handle(self.update(3, "Следующее сообщение"))
        self.assertEqual(prompt.text, "Напишите ответ в течение 5 минут.")
        self.assertEqual(result.text, "Ответ покупателю отправлен.")
        self.assertEqual(self.reply_client.calls[0][0:2], ("funpay-alice", "Alice"))

        self.router.handle(self.update(4, callback=f"funpay_reply:{self.bob_id}"))
        self.now = 1301.0
        self.assertIsNone(self.router.handle(self.update(5, "Поздний ответ")))
        self.assertEqual(len(self.reply_client.calls), 1)

    def test_cannot_route_to_unlinked_or_unauthorized_buyer(self) -> None:
        unlinked = DialogRepository(self.database).upsert_dialog("funpay-other", "buyer-x", "Mallory")
        result = self.router.handle(self.update(6, callback=f"funpay_reply:{unlinked}"))
        unauthorized = self.router.handle(self.update(7, "Попытка", reply_to=71, user_id=2002, chat_id=2002))

        self.assertEqual(result.text, "Диалог или покупатель не подтверждены.")
        self.assertIsNone(unauthorized)
        self.assertEqual(self.reply_client.calls, [])

    def test_failed_send_offers_retry_and_cancel_without_duplicate_key(self) -> None:
        self.reply_client.fail = True
        failed = self.router.handle(self.update(8, "Ответ", reply_to=71))
        buttons = failed.reply_markup["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in buttons], ["Повторить", "Отмена"])

        self.reply_client.fail = False
        retried = self.router.handle(self.update(9, callback="funpay_retry:1"))
        self.assertEqual(retried.text, "Ответ покупателю отправлен.")
        self.assertEqual(self.reply_client.calls[0][3], self.reply_client.calls[1][3])

        self.reply_client.fail = True
        self.router.handle(self.update(10, "Ещё ответ", reply_to=72))
        cancelled = self.router.handle(self.update(11, callback="funpay_cancel:2"))
        self.assertEqual(cancelled.text, "Отправка отменена.")
        self.assertEqual(len(self.reply_client.calls), 3)
