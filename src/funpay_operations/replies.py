"""Authorized Telegram-to-FunPay reply routing without logging buyer content."""

from __future__ import annotations

import time
from typing import Callable

from .funpay import FunPayError, FunPayReplyClient, RealOperationsDisabled
from .repositories import ReplyAttempt, ReplyRepository, TelegramMessageLinkRepository
from .telegram import CommandReply, TelegramUpdate


class FunPayReplyRouter:
    """Routes only linked Telegram notifications to their verified FunPay dialog."""

    def __init__(
        self, allowed_user_ids: tuple[int, ...], links: TelegramMessageLinkRepository,
        replies: ReplyRepository, funpay: FunPayReplyClient, *, clock: Callable[[], float] = time.time,
        outbound_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self._allowed = frozenset(allowed_user_ids)
        self._links = links
        self._replies = replies
        self._funpay = funpay
        self._clock = clock
        self._outbound_allowed = outbound_allowed or (lambda _: True)

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        if update.user_id not in self._allowed or update.chat_id != update.user_id:
            return None
        if update.callback_data:
            return self._handle_callback(update)
        if not update.text or update.text.startswith("/"):
            return None
        target = None
        if update.reply_to_message_id is not None:
            target = self._links.target_for_notification(update.chat_id, update.reply_to_message_id)
        else:
            target = self._replies.consume_mode(update.user_id, update.chat_id, int(self._clock()))
        if target is None:
            return None
        if not self._outbound_allowed("outbound_reply"):
            return CommandReply("🔒 Ответы в FunPay пока не разрешены.")
        attempt = self._replies.create_attempt(update.update_id, update.user_id, update.chat_id, target, update.text)
        if attempt.state == "sent":
            return CommandReply("Ответ уже был отправлен.")
        if attempt.state != "sending":
            return CommandReply("Ответ не может быть отправлен.", reply_markup=_retry_markup(attempt.attempt_id))
        return self._deliver(attempt)

    def _handle_callback(self, update: TelegramUpdate) -> CommandReply | None:
        action, separator, raw_id = update.callback_data.partition(":")
        if not separator or not raw_id.isdecimal():
            return None
        identifier = int(raw_id)
        if action == "funpay_reply":
            if not self._outbound_allowed("outbound_reply"):
                return CommandReply("🔒 Ответ будет включён после отдельного теста отправки.")
            target = self._links.target_for_dialog(update.chat_id, identifier)
            if target is None:
                return CommandReply("Диалог или покупатель не подтверждены.")
            self._replies.arm_mode(update.user_id, update.chat_id, target, int(self._clock()) + 300)
            return CommandReply("Напишите ответ в течение 5 минут.")
        if action == "funpay_retry":
            attempt = self._replies.claim_retry(identifier, update.user_id, update.chat_id)
            return self._deliver(attempt) if attempt is not None else CommandReply("Повтор недоступен.")
        if action == "funpay_cancel":
            attempt = self._replies.claim_retry(identifier, update.user_id, update.chat_id)
            if attempt is None:
                return CommandReply("Отмена недоступна.")
            self._replies.mark(attempt.attempt_id, "cancelled")
            return CommandReply("Отправка отменена.")
        return None

    def _deliver(self, attempt: ReplyAttempt) -> CommandReply:
        if not self._outbound_allowed("outbound_reply"):
            self._replies.mark(attempt.attempt_id, "failed")
            return CommandReply("Emergency stop blocks outbound replies.")
        try:
            self._funpay.send_reply(
                attempt.target.external_dialog_id, attempt.target.buyer_nickname, attempt.body, attempt.idempotency_key
            )
        except (FunPayError, RealOperationsDisabled, ValueError):
            self._replies.mark(attempt.attempt_id, "failed")
            return CommandReply("Ответ не отправлен.", reply_markup=_retry_markup(attempt.attempt_id))
        self._replies.mark(attempt.attempt_id, "sent")
        return CommandReply(f"✅ Отправлено {attempt.target.buyer_nickname}")


def _retry_markup(attempt_id: int) -> dict[str, object]:
    return {"inline_keyboard": [[
        {"text": "Повторить", "callback_data": f"funpay_retry:{attempt_id}"},
        {"text": "Отмена", "callback_data": f"funpay_cancel:{attempt_id}"},
    ]]}
