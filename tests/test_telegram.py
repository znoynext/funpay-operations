from __future__ import annotations

import logging
import unittest

from funpay_operations.telegram import (
    MAIN_MENU,
    MockTelegramApi,
    TelegramCommandHandler,
    TelegramLongPollingBot,
    TelegramUpdate,
)


class InMemoryStates:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, str | None]] = {}

    def save(self, task_name: str, state: str, cursor: str | None = None, last_error: str | None = None) -> None:
        self.values[task_name] = (state, cursor)

    def load(self, task_name: str) -> tuple[str, str | None] | None:
        return self.values.get(task_name)


class TelegramBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("funpay_operations.telegram.tests")
        self.states = InMemoryStates()
        self.api = MockTelegramApi()
        self.handler = TelegramCommandHandler((1001,), self.states, self.logger)
        self.bot = TelegramLongPollingBot(self.api, self.handler, self.states, self.logger, timeout_seconds=25)

    def update(self, identifier: int, text: str | None, *, user_id: int = 1001, chat_id: int = 1001) -> TelegramUpdate:
        return TelegramUpdate(identifier, user_id, chat_id, text)

    def test_allowlisted_private_user_gets_menu_and_state_commands(self) -> None:
        self.api.update_batches.append((
            self.update(1, "/start"), self.update(2, "/pause"), self.update(3, "/status"),
            self.update(4, "/resume"), self.update(5, "/check"),
        ))
        self.bot.poll_once()

        self.assertEqual([message[1] for message in self.api.sent_messages], [
            "Панель управления готова.", "Операции приостановлены.", "Состояние: paused.",
            "Операции возобновлены.", "Функция пока недоступна.",
        ])
        self.assertEqual(self.api.sent_messages[0][2], MAIN_MENU)
        self.assertEqual(self.states.load("operations"), ("active", None))
        self.assertEqual(self.states.load("telegram_polling"), ("running", "5"))

    def test_unapproved_user_or_non_private_chat_is_rejected_and_logged(self) -> None:
        self.api.update_batches.append((
            self.update(1, "/status", user_id=2002, chat_id=2002),
            self.update(2, "/status", user_id=1001, chat_id=-100),
        ))
        with self.assertLogs(self.logger, level="WARNING") as captured:
            self.bot.poll_once()

        self.assertEqual(self.api.sent_messages, [])
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all("SECURITY telegram command rejected" in record.getMessage() for record in captured.records))
        self.assertTrue(all("/status" not in record.getMessage() for record in captured.records))

    def test_unknown_command_is_rejected_without_recording_its_text(self) -> None:
        self.api.update_batches.append((self.update(1, "/unexpected private text"),))
        with self.assertLogs(self.logger, level="WARNING") as captured:
            self.bot.poll_once()

        self.assertEqual(self.api.sent_messages[0][1], "Команда недоступна.")
        self.assertNotIn("private text", captured.output[0])

    def test_stop_persists_offset_and_prevents_future_polling(self) -> None:
        self.api.update_batches.append((self.update(7, "/stop"),))
        self.bot.poll_once()
        self.bot.poll_once()

        self.assertEqual(self.api.sent_messages[0][1], "Long polling остановлен.")
        self.assertEqual(self.states.load("telegram_polling"), ("stopped", "7"))
        self.assertEqual(self.api.requested_offsets, [None])

    def test_offset_is_restored_on_restart(self) -> None:
        self.states.save("telegram_polling", "running", "11")
        self.api.update_batches.append((self.update(12, "/lots"),))
        restarted = TelegramLongPollingBot(self.api, self.handler, self.states, self.logger, timeout_seconds=25)
        restarted.poll_once()

        self.assertEqual(self.api.requested_offsets, [12])
        self.assertEqual(self.states.load("telegram_polling"), ("running", "12"))
