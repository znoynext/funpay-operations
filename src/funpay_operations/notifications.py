"""Fast, local-only routing of read-only FunPay messages to Telegram."""

from __future__ import annotations

import json
import logging
from typing import Mapping, Protocol
from urllib.parse import urlparse

from .auto_reply import AutoReplyService
from .funpay import FunPayClient, FunPayError, FunPayMessage
from .repositories import DialogRepository, TaskStateRepository, TelegramMessageLinkRepository
from .telegram import TelegramError


class TelegramNotificationSender(Protocol):
    def send_private_notification(self, recipient_id: int, text: str, reply_markup: Mapping[str, object]) -> int: ...


class FunPayMessageNotifier:
    """Poll and deliver incoming messages once per local FunPay message ID.

    The persistent cursor is advanced only after a message has been stored and,
    for incoming messages, successfully linked to its Telegram notification.
    """

    def __init__(
        self, funpay: FunPayClient, sender: TelegramNotificationSender, dialogs: DialogRepository,
        links: TelegramMessageLinkRepository, states: TaskStateRepository, recipient_id: int,
        logger: logging.Logger, auto_replies: AutoReplyService | None = None,
    ) -> None:
        self._funpay = funpay
        self._sender = sender
        self._dialogs = dialogs
        self._links = links
        self._states = states
        self._recipient_id = recipient_id
        self._logger = logger
        self._auto_replies = auto_replies

    def sync(self) -> None:
        state = self._states.load("funpay_message_notifications")
        cursor = state[1] if state else None
        try:
            messages = self._funpay.get_new_messages(cursor)
        except FunPayError:
            self._logger.warning("FunPay message sync unavailable; cursor was retained")
            return
        initial_auto_reply_sync = self._auto_replies is not None and not self._auto_replies.is_initialized()
        for message in messages:
            if not self._process(message, allow_auto_reply=not initial_auto_reply_sync):
                return
            self._states.save(
                "funpay_message_notifications", "running", _advance_cursor(cursor, message)
            )
            cursor = _advance_cursor(cursor, message)
        if initial_auto_reply_sync:
            self._auto_replies.mark_initialized()

    def _process(self, message: FunPayMessage, *, allow_auto_reply: bool) -> bool:
        dialog_id = self._dialogs.upsert_dialog(message.dialog_id, None, message.buyer_nickname)
        stored = self._dialogs.store_message_with_id(
            message.message_id, dialog_id, message.direction, message.body, message.sent_at or "unknown"
        )
        if message.direction != "incoming" or self._links.is_linked(stored.local_id):
            return True
        try:
            telegram_message_id = self._sender.send_private_notification(
                self._recipient_id, _notification_text(message), _notification_markup(dialog_id, message.dialog_url)
            )
        except (TelegramError, PermissionError):
            # The body and buyer information are intentionally not part of logs.
            self._logger.warning("Telegram notification delivery failed; message cursor was retained")
            return False
        self._links.link(stored.local_id, dialog_id, self._recipient_id, telegram_message_id)
        if allow_auto_reply and self._auto_replies is not None:
            self._auto_replies.maybe_reply(message, stored, dialog_id)
        return True


def _notification_text(message: FunPayMessage) -> str:
    buyer = (message.buyer_nickname or "Неизвестный покупатель")[:256]
    related = message.related_item[:512] if message.related_item else "FunPay сообщение"
    # Bot API has a 4096-character message limit. The original remains in local SQLite.
    body = message.body[:3000]
    return f"💬 {buyer}\n{related}\n\n{body}"[:4096]


def _notification_markup(dialog_id: int, dialog_url: str | None) -> Mapping[str, object]:
    open_button: dict[str, str] = {"text": "Открыть FunPay"}
    if _is_safe_funpay_url(dialog_url):
        open_button["url"] = dialog_url or ""
    else:
        # A safe fallback leaves a local callback identifier only; it never
        # creates an unverified external URL.
        open_button["callback_data"] = f"funpay_open:{dialog_id}"
    return {
        "inline_keyboard": [
            [{"text": "Ответить", "callback_data": f"funpay_reply:{dialog_id}"}],
            [open_button],
        ]
    }


def _is_safe_funpay_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == "funpay.com"


def _advance_cursor(previous: str | None, message: FunPayMessage) -> str:
    """Persist each dialog's last external message identifier locally."""

    try:
        parsed = json.loads(previous) if previous else {}
    except json.JSONDecodeError:
        parsed = {}
    cursors = parsed if isinstance(parsed, dict) else {}
    cursors[message.dialog_id] = message.message_id
    return json.dumps(cursors, sort_keys=True, separators=(",", ":"))
