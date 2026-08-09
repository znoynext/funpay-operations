"""Private Telegram control bot using Bot API long polling.

Tokens are retrieved only from the local DPAPI store at request time. Incoming
message contents are intentionally never logged.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    """Base class for a Telegram Bot API error without sensitive details."""


class TelegramTokenUnavailable(TelegramError):
    """The local DPAPI store has no bot token."""


class TelegramNetworkUnavailable(TelegramError):
    """Telegram could not be reached."""


class TelegramProtocolError(TelegramError):
    """Telegram returned an unexpected or unsuccessful response."""


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    user_id: int
    chat_id: int
    text: str | None
    reply_to_message_id: int | None = None
    callback_data: str | None = None


@dataclass(frozen=True)
class CommandReply:
    text: str
    show_menu: bool = False
    stop_polling: bool = False
    reply_markup: Mapping[str, Any] | None = None
    edit_message: bool = False


class TelegramBotApi(Protocol):
    def get_updates(self, offset: int | None, timeout_seconds: int) -> tuple[TelegramUpdate, ...]: ...

    def send_message(self, chat_id: int, text: str, *, reply_markup: Mapping[str, Any] | None = None) -> int: ...

    def edit_message(self, chat_id: int, message_id: int, text: str, *,
                     reply_markup: Mapping[str, Any] | None = None) -> None: ...


class TaskStateStore(Protocol):
    def save(self, task_name: str, state: str, cursor: str | None = None, last_error: str | None = None) -> None: ...

    def load(self, task_name: str) -> tuple[str, str | None] | None: ...


class TelegramInteractionRouter(Protocol):
    def handle(self, update: TelegramUpdate) -> CommandReply | None: ...


MAIN_MENU: Mapping[str, Any] = {
    "keyboard": [
        ["/status", "/check"],
        ["/pause", "/resume"],
        ["/lots", "/sellers"],
        ["/messages", "/rollback"],
        ["/update_prices", "/raise"],
        ["/auto_reply_on", "/auto_reply_off"],
        ["/stop"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

_UNAVAILABLE_COMMANDS = {
    "/check", "/update_prices", "/raise", "/lots", "/sellers", "/messages", "/rollback",
}


class TelegramHttpApi:
    """Minimal Bot API transport; the token is never retained or logged."""

    def __init__(self, token_provider: Callable[[], str | None], *, timeout_seconds: int = 35) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._token_provider = token_provider
        self._timeout_seconds = timeout_seconds

    def get_updates(self, offset: int | None, timeout_seconds: int) -> tuple[TelegramUpdate, ...]:
        if not 1 <= timeout_seconds <= 50:
            raise ValueError("long polling timeout must be between 1 and 50 seconds")
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout_seconds, "allowed_updates": ["message", "callback_query"]},
            timeout_seconds=timeout_seconds + 10,
        )
        if not isinstance(result, list):
            raise TelegramProtocolError("Telegram getUpdates result is not an array")
        return tuple(parsed for item in result if (parsed := _parse_update(item)) is not None)

    def send_message(self, chat_id: int, text: str, *, reply_markup: Mapping[str, Any] | None = None) -> int:
        if not text or len(text) > 4096:
            raise ValueError("Telegram reply text must be between 1 and 4096 characters")
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self._call("sendMessage", payload, timeout_seconds=self._timeout_seconds)
        if not isinstance(result, dict) or not _is_int(result.get("message_id")):
            raise TelegramProtocolError("Telegram sendMessage result has no message identifier")
        return int(result["message_id"])

    def edit_message(self, chat_id: int, message_id: int, text: str, *,
                     reply_markup: Mapping[str, Any] | None = None) -> None:
        if message_id <= 0 or not text or len(text) > 4096:
            raise ValueError("Telegram edited message must have a valid identifier and text")
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        self._call("editMessageText", payload, timeout_seconds=self._timeout_seconds)

    def _call(self, method: str, payload: Mapping[str, Any], *, timeout_seconds: int) -> Any:
        token = self._token_provider()
        if not token or "\r" in token or "\n" in token:
            raise TelegramTokenUnavailable("Telegram bot token is unavailable locally")
        request = Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS API host
                raw_response = response.read()
        except (HTTPError, URLError, OSError, socket.timeout) as error:
            raise TelegramNetworkUnavailable("Telegram API request could not be completed") from error
        try:
            document = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramProtocolError("Telegram API response is invalid") from error
        if not isinstance(document, dict) or document.get("ok") is not True:
            raise TelegramProtocolError("Telegram API rejected the request")
        return document.get("result")


class TelegramCommandHandler:
    """Authorizes private commands and keeps commands outside its scope inert."""

    def __init__(self, allowed_user_ids: tuple[int, ...], states: TaskStateStore, logger: logging.Logger,
                 *, auto_reply_available: bool = False) -> None:
        self._allowed_user_ids = frozenset(allowed_user_ids)
        self._states = states
        self._logger = logger
        self._auto_reply_available = auto_reply_available

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        return self._allowed_user_ids

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        if update.user_id not in self._allowed_user_ids or update.chat_id != update.user_id:
            self._security_rejection("unauthorized sender", update)
            return None
        command = _command_from_text(update.text)
        if command is None or command not in _UNAVAILABLE_COMMANDS | {
            "/start", "/status", "/pause", "/resume", "/stop",
            "/auto_reply_on", "/auto_reply_off",
        }:
            self._security_rejection("unknown command", update)
            return CommandReply("Команда недоступна.", show_menu=True)
        if command == "/start":
            return CommandReply("Панель управления готова.", show_menu=True)
        if command == "/status":
            state = self._states.load("operations")
            return CommandReply(f"Состояние: {state[0] if state else 'active'}.", show_menu=True)
        if command == "/pause":
            self._states.save("operations", "paused")
            return CommandReply("Операции приостановлены.", show_menu=True)
        if command == "/resume":
            self._states.save("operations", "active")
            return CommandReply("Операции возобновлены.", show_menu=True)
        if command == "/stop":
            self._states.save("telegram_bot", "stopped")
            return CommandReply("Long polling остановлен.", stop_polling=True)
        if command in {"/auto_reply_on", "/auto_reply_off"}:
            if not self._auto_reply_available:
                return CommandReply("Автоответ недоступен: локальный live-режим выключен.", show_menu=True)
            self._states.save("funpay_auto_reply", "enabled" if command.endswith("_on") else "disabled")
            return CommandReply("Автоответ включен." if command.endswith("_on") else "Автоответ выключен.", show_menu=True)
        return CommandReply("Функция пока недоступна.", show_menu=True)

    def _security_rejection(self, reason: str, update: TelegramUpdate) -> None:
        # Do not log text, token, chat titles, or Telegram payloads.
        self._logger.warning(
            "SECURITY telegram command rejected: reason=%s user_id=%d chat_id=%d update_id=%d",
            reason, update.user_id, update.chat_id, update.update_id,
        )


class TelegramLongPollingBot:
    """Single-consumer long-polling loop with durable update offset."""

    def __init__(
        self, api: TelegramBotApi, handler: TelegramCommandHandler, states: TaskStateStore,
        logger: logging.Logger, *, timeout_seconds: int,
    ) -> None:
        self._api = api
        self._handler = handler
        self._states = states
        self._logger = logger
        self._timeout_seconds = timeout_seconds
        self._offset: int | None = None
        self._stopped = False
        self._state_restored = False
        self._interaction_router: TelegramInteractionRouter | None = None

    def set_interaction_router(self, router: TelegramInteractionRouter) -> None:
        self._interaction_router = router

    def poll_once(self) -> None:
        self._restore_state()
        if self._stopped:
            return
        try:
            updates = self._api.get_updates(self._offset, self._timeout_seconds)
        except TelegramTokenUnavailable:
            self._logger.warning("Telegram long polling skipped: local token is unavailable")
            return
        except TelegramError:
            self._logger.warning("Telegram long polling failed without processing an update")
            return
        for update in sorted(updates, key=lambda item: item.update_id):
            if self._offset is not None and update.update_id < self._offset:
                continue
            reply = self._interaction_router.handle(update) if self._interaction_router is not None else None
            if reply is None:
                reply = self._handler.handle(update)
            if reply is not None:
                try:
                    markup = reply.reply_markup if reply.reply_markup is not None else (MAIN_MENU if reply.show_menu else None)
                    if reply.edit_message and update.callback_data and update.reply_to_message_id is not None:
                        self._api.edit_message(update.chat_id, update.reply_to_message_id, reply.text, reply_markup=markup)
                    else:
                        self._api.send_message(update.chat_id, reply.text, reply_markup=markup)
                except TelegramError:
                    self._logger.warning("Telegram reply could not be sent; update will be retried")
                    return
            self._offset = update.update_id + 1
            self._states.save("telegram_polling", "running", str(update.update_id))
            if reply is not None and reply.stop_polling:
                self._stopped = True
                self._states.save("telegram_polling", "stopped", str(update.update_id))
                return

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def send_private_notification(self, recipient_id: int, text: str, reply_markup: Mapping[str, Any]) -> int:
        if recipient_id not in self._handler.allowed_user_ids:
            raise PermissionError("Telegram notification recipient is not allowlisted")
        return self._api.send_message(recipient_id, text, reply_markup=reply_markup)

    def _restore_state(self) -> None:
        if self._state_restored:
            return
        persisted = self._states.load("telegram_polling")
        self._offset = _offset_from_cursor(persisted[1]) if persisted else None
        self._stopped = bool(persisted and persisted[0] == "stopped")
        self._state_restored = True


@dataclass
class MockTelegramApi:
    """Deterministic Telegram API double for unit tests."""

    update_batches: list[tuple[TelegramUpdate, ...]] = field(default_factory=list)
    sent_messages: list[tuple[int, str, Mapping[str, Any] | None]] = field(default_factory=list)
    edited_messages: list[tuple[int, int, str, Mapping[str, Any] | None]] = field(default_factory=list)
    requested_offsets: list[int | None] = field(default_factory=list)
    next_message_id: int = 1

    def get_updates(self, offset: int | None, timeout_seconds: int) -> tuple[TelegramUpdate, ...]:
        self.requested_offsets.append(offset)
        return self.update_batches.pop(0) if self.update_batches else ()

    def send_message(self, chat_id: int, text: str, *, reply_markup: Mapping[str, Any] | None = None) -> int:
        self.sent_messages.append((chat_id, text, reply_markup))
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    def edit_message(self, chat_id: int, message_id: int, text: str, *,
                     reply_markup: Mapping[str, Any] | None = None) -> None:
        self.edited_messages.append((chat_id, message_id, text, reply_markup))


def build_telegram_bot(settings: Any, local_secret_store: Any, states: TaskStateStore,
                       logger: logging.Logger) -> TelegramLongPollingBot:
    """Compose the bot without reading the DPAPI token until polling starts."""

    def token_provider() -> str | None:
        return local_secret_store.get(settings.telegram_token_key)

    api = TelegramHttpApi(token_provider, timeout_seconds=settings.telegram_long_poll_timeout_seconds + 10)
    handler = TelegramCommandHandler(
        settings.allowed_telegram_user_ids, states, logger,
        auto_reply_available=settings.operations_enabled,
    )
    return TelegramLongPollingBot(api, handler, states, logger, timeout_seconds=settings.telegram_long_poll_timeout_seconds)


def _parse_update(value: Any) -> TelegramUpdate | None:
    if not isinstance(value, dict) or not _is_int(value.get("update_id")):
        raise TelegramProtocolError("Telegram update has an invalid identifier")
    message = value.get("message")
    if isinstance(message, dict):
        parsed = _parse_message(value["update_id"], message)
        if parsed is None:
            return None
        user_id, chat_id, text, reply_to_message_id = parsed
        return TelegramUpdate(value["update_id"], user_id, chat_id, text, reply_to_message_id)
    callback = value.get("callback_query")
    if not isinstance(callback, dict) or not isinstance(callback.get("message"), dict):
        return None
    parsed = _parse_message(value["update_id"], callback["message"], sender=callback.get("from"))
    if parsed is None or not isinstance(callback.get("data"), str):
        return None
    user_id, chat_id, _, message_id = parsed
    return TelegramUpdate(value["update_id"], user_id, chat_id, None, message_id, callback["data"])


def _parse_message(update_id: int, message: Mapping[str, Any], *, sender: Any | None = None) -> tuple[int, int, str | None, int | None] | None:
    del update_id
    sender = sender if sender is not None else message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    user_id, chat_id, message_id = sender.get("id"), chat.get("id"), message.get("message_id")
    if not _is_int(user_id) or not _is_int(chat_id) or not _is_int(message_id) or message_id <= 0:
        return None
    reply_to = message.get("reply_to_message")
    reply_to_message_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
    if reply_to_message_id is not None and not _is_int(reply_to_message_id):
        return None
    text = message.get("text")
    return user_id, chat_id, text if isinstance(text, str) else None, reply_to_message_id


def _command_from_text(text: str | None) -> str | None:
    if not text:
        return None
    command = text.strip().split(maxsplit=1)[0].lower()
    return command.split("@", 1)[0] if command.startswith("/") else None


def _offset_from_cursor(cursor: str | None) -> int | None:
    try:
        update_id = int(cursor) if cursor is not None else None
    except ValueError:
        return None
    return update_id + 1 if update_id is not None and update_id >= 0 else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
