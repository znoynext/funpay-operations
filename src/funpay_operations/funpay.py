"""FunPay boundary backed by the pinned, unofficial ``fpx-engine`` library.

FunPay has no documented public seller API.  This module intentionally calls no
hand-written FunPay endpoints: all wire details and HTML parsing live in the
pinned library behind the small application-facing contract below.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar


class RealOperationsDisabled(RuntimeError):
    """Raised before a state-changing FunPay action can be attempted."""


class FunPayError(RuntimeError):
    """Base class for a FunPay integration error without sensitive details."""


class FunPaySessionExpired(FunPayError):
    """The local session is absent, invalid, or has expired."""


class FunPayNetworkUnavailable(FunPayError):
    """FunPay could not be reached after the library's bounded retry policy."""


class FunPayProtocolError(FunPayError):
    """The library could not normalize a FunPay response safely."""


@dataclass(frozen=True)
class FunPaySession:
    """Cookies required by fpx; values are read only from local Windows DPAPI."""

    golden_key: str
    golden_seal: str


@dataclass(frozen=True)
class FunPayProfile:
    account_id: str
    username: str
    authorized: bool


@dataclass(frozen=True)
class FunPayLot:
    lot_id: str
    title: str
    price_minor: int
    currency: str
    seller_id: str


@dataclass(frozen=True)
class FunPayLotDetails:
    """Read-only editor snapshot for one of the authenticated seller's lots.

    ``fpx-engine`` exposes a dynamic form, not a stable typed lot schema.  The
    adapter preserves its non-sensitive field names and values so a later,
    separately approved edit feature can discover the exact shape again.
    CSRF, auto-delivery secrets, and payment messages are deliberately omitted.
    """

    lot_id: str
    title: str
    price_minor: int
    currency: str
    seller_id: str
    category_node_id: str | None
    is_active: bool | None
    description: str | None
    short_description: str | None
    location: str | None
    is_deleted: bool | None
    editor_fields: Mapping[str, str]
    editor_options: Mapping[str, tuple[tuple[str, str], ...]]
    omitted_field_names: tuple[str, ...]


@dataclass(frozen=True)
class FunPayBumpMetadata:
    """Read-only category data needed by a future, separately approved bump step."""

    lot_id: str
    category_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class FunPayDialog:
    dialog_id: str
    counterparty_id: str | None
    counterparty_name: str
    last_message_at: str | None


@dataclass(frozen=True)
class FunPayMessage:
    message_id: str
    dialog_id: str
    direction: str
    body: str
    sent_at: str | None
    buyer_nickname: str | None = None
    related_item: str | None = None
    dialog_url: str | None = None


class FunPayClient(Protocol):
    """The application-facing FunPay contract, independent of fpx."""

    def check_authorization(self) -> bool: ...

    def get_profile(self) -> FunPayProfile: ...

    def get_own_lots(self) -> tuple[FunPayLot, ...]: ...

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]: ...

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]: ...

    def get_dialogs(self) -> tuple[FunPayDialog, ...]: ...

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]: ...

    def get_bump_metadata(self, lot_id: str) -> FunPayBumpMetadata | None: ...

    def check_bump_availability(self, lot_id: str) -> bool: ...


class FunPayReplyClient(Protocol):
    """Explicitly enabled state-changing boundary for buyer replies only."""

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None: ...


class DisabledFunPayReplyClient:
    """Safe default: no FunPay write request can leave the process."""

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        raise RealOperationsDisabled("FunPay replies require local live-mode configuration")


T = TypeVar("T")
FunPayToolsFactory = Callable[[str, str], Any]


class NativeFunPayClient:
    """Synchronous adapter for the asynchronous, pinned ``fpx-engine`` client.

    Every public operation creates and closes one fpx client in its own event
    loop.  The DPAPI value therefore is never persisted by this process and no
    browser automation or hand-written endpoint contract is involved.
    """

    def __init__(
        self,
        session_provider: Callable[[], str | None],
        *,
        currency: str,
        allow_replies: bool,
        min_request_interval_seconds: float = 0,
        tools_factory: FunPayToolsFactory | None = None,
    ) -> None:
        self._session_provider = session_provider
        self._currency = _required_text(currency, "currency")
        self._allow_replies = allow_replies
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must not be negative")
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_operation_at: float | None = None
        self._tools_factory = tools_factory or _fpx_tools_factory
        self._call_lock = threading.Lock()

    def has_local_session(self) -> bool:
        """Check only the local DPAPI value format; never expose its contents."""

        try:
            _session_from_value(self._session_provider())
        except FunPaySessionExpired:
            return False
        return True

    def close(self) -> None:
        """Compatibility close hook; individual fpx clients close per operation."""

    def check_authorization(self) -> bool:
        try:
            return self.get_profile().authorized
        except FunPaySessionExpired:
            return False

    def get_profile(self) -> FunPayProfile:
        async def action(tools: Any) -> FunPayProfile:
            user_data = await tools.account.profile.get_user_data()
            return FunPayProfile(
                account_id=_required_text(getattr(user_data, "user_id", None), "profile.account_id"),
                username=_required_text(getattr(tools.account.data, "username", None), "profile.username"),
                authorized=True,
            )

        return self._run(action)

    def get_own_lots(self) -> tuple[FunPayLot, ...]:
        async def action(tools: Any) -> tuple[FunPayLot, ...]:
            user_data = await tools.account.profile.get_user_data()
            seller_id = _required_text(getattr(user_data, "user_id", None), "profile.account_id")
            profile = await tools.account.profile.profile(seller_id)
            return await self._normalize_lots(tools, profile, seller_id)

        return self._run(action)

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
        """Read each owned lot's public page and its fpx editor snapshot.

        The fpx method is intentionally the library's read-only parser.  This
        adapter never calls any of fpx's editor mutation methods.
        """

        async def action(tools: Any) -> tuple[FunPayLotDetails, ...]:
            user_data = await tools.account.profile.get_user_data()
            seller_id = _required_text(getattr(user_data, "user_id", None), "profile.account_id")
            profile = await tools.account.profile.profile(seller_id)
            result: list[FunPayLotDetails] = []
            node_options: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
            for lot in getattr(profile, "lots", ()):
                lot_id = _required_text(getattr(lot, "id", None), "lot.id")
                lot_info = await tools.account.lot.get_lot_info(lot_id)
                editor = await tools.account.lot._get_lot_editor_details(lot_id)
                fields, omitted = _safe_editor_fields(getattr(editor, "fields", None))
                category_node_id = _optional_text(getattr(editor, "node_id", None))
                if category_node_id is not None and category_node_id not in node_options:
                    node_editor = await tools.account.lot.get_node_editor_data(category_node_id)
                    node_options[category_node_id] = _safe_node_options(getattr(node_editor, "fields", None))
                result.append(FunPayLotDetails(
                    lot_id=lot_id,
                    title=_required_text(getattr(lot, "name", None), "lot.title"),
                    price_minor=_price_minor(getattr(lot_info, "price", None)),
                    currency=self._currency,
                    seller_id=seller_id,
                    category_node_id=category_node_id,
                    is_active=_active_from_editor(editor, fields),
                    description=_optional_text(getattr(lot_info, "description", None)),
                    short_description=_optional_text(getattr(lot_info, "short_desc", None)),
                    location=_optional_text(getattr(editor, "location", None)),
                    is_deleted=_optional_boolean(getattr(editor, "deleted", None)),
                    editor_fields=fields,
                    editor_options=node_options.get(category_node_id, {}),
                    omitted_field_names=omitted,
                ))
            return tuple(result)

        return self._run(action)

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]:
        normalized_seller_id = _required_text(seller_id, "seller_id")

        async def action(tools: Any) -> tuple[FunPayLot, ...]:
            profile = await tools.account.profile.profile(normalized_seller_id)
            return await self._normalize_lots(tools, profile, normalized_seller_id)

        return self._run(action)

    def get_dialogs(self) -> tuple[FunPayDialog, ...]:
        async def action(tools: Any) -> tuple[FunPayDialog, ...]:
            chats = await _chats_or_empty(tools)
            own_id = _optional_text(getattr(tools.account.data, "user_id", None))
            result: list[FunPayDialog] = []
            for chat in chats:
                dialog_id = _required_text(getattr(chat, "id", None), "chat.id")
                chat_data = await tools.account.chat.get_chat_data(dialog_id)
                result.append(FunPayDialog(
                    dialog_id=dialog_id,
                    counterparty_id=_counterparty_id(getattr(chat_data, "node_name", None), own_id),
                    counterparty_name=_required_text(getattr(chat, "username", None), "chat.username"),
                    last_message_at=_optional_text(getattr(chat, "date", None)),
                ))
            return tuple(result)

        return self._run(action)

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        cursors, legacy_cursor = _dialog_cursors(after_message_id)

        async def action(tools: Any) -> tuple[FunPayMessage, ...]:
            user_data = await tools.account.profile.get_user_data()
            own_id = _required_text(getattr(user_data, "user_id", None), "profile.account_id")
            own_name = _required_text(getattr(tools.account.data, "username", None), "profile.username")
            chats = await _chats_or_empty(tools)
            events: list[tuple[int, FunPayMessage]] = []
            seen: set[str] = set()
            for chat in chats:
                dialog_id = _required_text(getattr(chat, "id", None), "chat.id")
                dialog_cursor = cursors.get(dialog_id, legacy_cursor)
                chat_data = await tools.account.chat.get_chat_data(dialog_id, dialog_cursor)
                counterparty_id = _counterparty_id(getattr(chat_data, "node_name", None), own_id)
                for message in getattr(chat_data, "last_messages", ()):
                    event = _normalize_message(
                        message, dialog_id=dialog_id, chat=chat, own_name=own_name,
                        counterparty_id=counterparty_id,
                    )
                    if event is None:
                        continue
                    node_id = _node_cursor(event.message_id)
                    if node_id is None or (dialog_cursor is not None and node_id <= dialog_cursor) or event.message_id in seen:
                        continue
                    seen.add(event.message_id)
                    events.append((node_id, event))
            return tuple(event for _, event in sorted(events, key=lambda item: item[0]))

        return self._run(action)

    def get_bump_metadata(self, lot_id: str) -> FunPayBumpMetadata | None:
        normalized_lot_id = _required_text(lot_id, "lot_id")

        async def action(tools: Any) -> FunPayBumpMetadata | None:
            user_data = await tools.account.profile.get_user_data()
            profile = await tools.account.profile.profile(user_data.user_id)
            lot_ids = {_required_text(getattr(lot, "id", None), "lot.id") for lot in getattr(profile, "lots", ())}
            if normalized_lot_id not in lot_ids:
                return None
            categories = tuple(
                _required_text(category_id, "profile.category_id")
                for category_id in getattr(profile, "category_ids", ())
            )
            return FunPayBumpMetadata(normalized_lot_id, categories)

        return self._run(action)

    def check_bump_availability(self, lot_id: str) -> bool:
        """Compatibility predicate: confirms only that read-only bump metadata exists."""

        return self.get_bump_metadata(lot_id) is not None

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        if not self._allow_replies:
            raise RealOperationsDisabled("FunPay replies require local live-mode configuration")
        normalized_dialog_id = _required_text(dialog_id, "dialog_id")
        _required_text(buyer_nickname, "buyer_nickname")
        normalized_body = _required_text(body, "reply body")
        _required_text(idempotency_key, "idempotency_key")

        async def action(tools: Any) -> None:
            await tools.account.chat.send_message(normalized_dialog_id, normalized_body)

        self._run(action)

    async def _normalize_lots(self, tools: Any, profile: Any, seller_id: str) -> tuple[FunPayLot, ...]:
        result: list[FunPayLot] = []
        for lot in getattr(profile, "lots", ()):
            lot_id = _required_text(getattr(lot, "id", None), "lot.id")
            lot_info = await tools.account.lot.get_lot_info(lot_id)
            result.append(FunPayLot(
                lot_id=lot_id,
                title=_required_text(getattr(lot, "name", None), "lot.title"),
                price_minor=_price_minor(getattr(lot_info, "price", None)),
                currency=self._currency,
                seller_id=seller_id,
            ))
        return tuple(result)

    def _run(self, action: Callable[[Any], Awaitable[T]]) -> T:
        session = _session_from_value(self._session_provider())

        async def invoke() -> T:
            tools = self._tools_factory(session.golden_key, session.golden_seal)
            async with tools:
                return await action(tools)

        try:
            # All application callers execute this synchronous adapter in a
            # worker thread.  Refusing a nested event loop prevents unsafe
            # cross-loop reuse of the library HTTP client.
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("NativeFunPayClient must be called from a worker thread")
        try:
            with self._call_lock:
                if self._last_operation_at is not None:
                    delay = self._min_request_interval_seconds - (time.monotonic() - self._last_operation_at)
                    if delay > 0:
                        time.sleep(delay)
                self._last_operation_at = time.monotonic()
                return asyncio.run(invoke())
        except FunPayError:
            raise
        except BaseException as error:
            raise _map_library_error(error) from error


@dataclass
class MockFunPayClient:
    """In-memory implementation for deterministic application tests and CI."""

    authorized: bool = True
    profile: FunPayProfile = field(default_factory=lambda: FunPayProfile("mock", "mock", True))
    own_lots: tuple[FunPayLot, ...] = ()
    own_lot_details: tuple[FunPayLotDetails, ...] = ()
    seller_lots: Mapping[str, tuple[FunPayLot, ...]] = field(default_factory=dict)
    dialogs: tuple[FunPayDialog, ...] = ()
    messages: tuple[FunPayMessage, ...] = ()
    bump_metadata: Mapping[str, FunPayBumpMetadata] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def check_authorization(self) -> bool:
        self.calls.append("check_authorization")
        return self.authorized

    def get_profile(self) -> FunPayProfile:
        self.calls.append("get_profile")
        return self.profile

    def get_own_lots(self) -> tuple[FunPayLot, ...]:
        self.calls.append("get_own_lots")
        return self.own_lots

    def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
        self.calls.append("get_own_lot_details")
        return self.own_lot_details

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]:
        self.calls.append("get_seller_lots")
        return self.seller_lots.get(seller_id, ())

    def get_dialogs(self) -> tuple[FunPayDialog, ...]:
        self.calls.append("get_dialogs")
        return self.dialogs

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        self.calls.append("get_new_messages")
        return self.messages

    def get_bump_metadata(self, lot_id: str) -> FunPayBumpMetadata | None:
        self.calls.append("get_bump_metadata")
        return self.bump_metadata.get(lot_id)

    def check_bump_availability(self, lot_id: str) -> bool:
        self.calls.append("check_bump_availability")
        return lot_id in self.bump_metadata


def session_from_local_store(store: Any, credential_key: str) -> Callable[[], str | None]:
    """Adapt DPAPI ``SecretStore`` without printing, logging, or persisting a value."""

    if not credential_key.replace("_", "").isalnum():
        raise ValueError("credential_key must be a simple local secret key")
    return lambda: store.get(credential_key)


def build_read_client(settings: Any, local_secret_store: Any) -> NativeFunPayClient:
    """Compose the native adapter; construction does not read or transmit a session."""

    return NativeFunPayClient(
        session_from_local_store(local_secret_store, settings.funpay_credential_key),
        currency=settings.default_currency,
        allow_replies=settings.operations_enabled and settings.operation_mode == "live",
        min_request_interval_seconds=settings.funpay_min_request_interval_seconds,
        tools_factory=lambda key, seal: _fpx_tools_factory(
            key, seal, timeout_seconds=settings.funpay_request_timeout_seconds,
        ),
    )


def build_reply_client(settings: Any, read_client: NativeFunPayClient) -> FunPayReplyClient:
    """Expose the same adapter for replies only after local live-mode opt-in."""

    if not settings.operations_enabled or settings.operation_mode != "live":
        return DisabledFunPayReplyClient()
    return read_client


def _fpx_tools_factory(golden_key: str, golden_seal: str, *, timeout_seconds: int = 15) -> Any:
    try:
        from fpx import FunPayTools
        import httpx
    except ImportError as error:  # pragma: no cover - packaging failure is environment-specific
        raise FunPayProtocolError("fpx-engine is not installed") from error
    http_client = httpx.AsyncClient(
        http2=True, base_url="https://funpay.com", follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds), limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    )
    return FunPayTools(golden_key, golden_seal, http_client=http_client)


def _session_from_value(value: str | None) -> FunPaySession:
    if not value:
        raise FunPaySessionExpired("FunPay session is unavailable locally")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise FunPaySessionExpired("FunPay session has an invalid local format") from error
    if not isinstance(decoded, dict):
        raise FunPaySessionExpired("FunPay session has an invalid local format")
    try:
        golden_key = _required_text(decoded.get("golden_key"), "session.golden_key")
        golden_seal = _required_text(decoded.get("golden_seal"), "session.golden_seal")
    except FunPayProtocolError as error:
        raise FunPaySessionExpired("FunPay session has an invalid local format") from error
    if any("\r" in item or "\n" in item for item in (golden_key, golden_seal)):
        raise FunPaySessionExpired("FunPay session has an invalid local format")
    return FunPaySession(golden_key, golden_seal)


async def _chats_or_empty(tools: Any) -> tuple[Any, ...]:
    try:
        chats = await tools.account.chat.get_chats()
    except BaseException as error:
        if error.__class__.__name__ == "FpxNullDataError":
            return ()
        raise
    if not isinstance(chats, (tuple, list)):
        raise FunPayProtocolError("FunPay chats response is malformed")
    return tuple(chats)


def _normalize_message(
    message: Any, *, dialog_id: str, chat: Any, own_name: str, counterparty_id: str | None,
) -> FunPayMessage | None:
    if bool(getattr(message, "is_system", False)):
        return None
    node_id = _node_cursor(getattr(message, "node_msg_id", None))
    sender = _optional_text(getattr(message, "sender", None))
    body = _optional_text(getattr(message, "text", None))
    if node_id is None or sender is None or body is None:
        return None
    direction = "outgoing" if sender == own_name else "incoming"
    return FunPayMessage(
        message_id=f"{dialog_id}:{node_id}",
        dialog_id=dialog_id,
        direction=direction,
        body=body,
        sent_at=_optional_text(getattr(chat, "date", None)),
        buyer_nickname=sender if direction == "incoming" else None,
        dialog_url=_dialog_url(dialog_id),
    )


def _counterparty_id(node_name: Any, own_id: str | None) -> str | None:
    if not isinstance(node_name, str) or not own_id:
        return None
    parts = node_name.split("-")
    if len(parts) != 3 or parts[0] != "users" or not all(part.isdecimal() for part in parts[1:]):
        return None
    if parts[1] == own_id:
        return parts[2]
    if parts[2] == own_id:
        return parts[1]
    return None


def _dialog_url(dialog_id: str) -> str | None:
    return f"https://funpay.com/chat/?node={dialog_id}" if dialog_id.isdecimal() else None


def _node_cursor(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    candidate = value.rsplit(":", 1)[-1]
    return int(candidate) if candidate.isdecimal() else None


def _dialog_cursors(value: str | None) -> tuple[dict[str, int], int | None]:
    """Read the local per-dialog cursor map, with a safe legacy fallback."""

    if not value:
        return {}, None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}, _node_cursor(value)
    if not isinstance(decoded, dict):
        return {}, None
    cursors = {
        dialog_id: node_id for raw_dialog_id, raw_node_id in decoded.items()
        if isinstance(raw_dialog_id, str) and (dialog_id := raw_dialog_id.strip())
        and (node_id := _node_cursor(raw_node_id)) is not None
    }
    return cursors, None


def _price_minor(value: Any) -> int:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FunPayProtocolError("lot.price must be a decimal number") from error
    if not price.is_finite() or price < 0:
        raise FunPayProtocolError("lot.price must be a non-negative decimal number")
    return int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FunPayProtocolError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _optional_boolean(value: Any) -> bool | None:
    """Normalize only explicit common HTML form booleans; otherwise unknown."""

    if value is True or value == 1 or value in ("1", "on", "true", "True"):
        return True
    if value is False or value == 0 or value in ("0", "off", "false", "False"):
        return False
    return None


def _active_from_editor(editor: Any, fields: Mapping[str, str]) -> bool | None:
    """Expose activity only when the parsed form supplies an explicit value."""

    for name in ("active", "is_active", "fields[active]"):
        if name in fields:
            return _optional_boolean(fields[name])
    # ``deleted`` is a distinct editor state, not evidence that a lot is merely
    # disabled, so it intentionally does not become a guessed activity value.
    return None


def _safe_editor_fields(value: Any) -> tuple[dict[str, str], tuple[str, ...]]:
    """Keep editable-form data locally while excluding secret-bearing fields."""

    if not isinstance(value, Mapping):
        raise FunPayProtocolError("lot editor fields must be a mapping")
    fields: dict[str, str] = {}
    omitted: list[str] = []
    for raw_name, raw_field_value in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise FunPayProtocolError("lot editor field name must be text")
        name = raw_name.strip()
        lowered = name.lower()
        if _is_sensitive_field_name(lowered):
            omitted.append(name)
            continue
        if isinstance(raw_field_value, (str, int, float)) and not isinstance(raw_field_value, bool):
            fields[name] = str(raw_field_value)
            continue
        if isinstance(raw_field_value, bool):
            fields[name] = "1" if raw_field_value else "0"
            continue
        if raw_field_value is None:
            fields[name] = ""
            continue
        raise FunPayProtocolError("lot editor field value must be scalar")
    return fields, tuple(sorted(omitted))


def _safe_node_options(value: Any) -> dict[str, tuple[tuple[str, str], ...]]:
    """Normalize only non-sensitive declared options from an fpx node editor."""

    if not isinstance(value, (list, tuple)):
        raise FunPayProtocolError("lot node editor fields must be a sequence")
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for field in value:
        name = _required_text(getattr(field, "key", None), "lot node editor field name")
        if _is_sensitive_field_name(name.lower()):
            continue
        raw_options = getattr(field, "options", None)
        if raw_options is None:
            result[name] = ()
            continue
        if not isinstance(raw_options, (list, tuple)):
            raise FunPayProtocolError("lot node editor options must be a sequence")
        options: list[tuple[str, str]] = []
        for option in raw_options:
            label = _required_text(getattr(option, "key", None), "lot node editor option label")
            option_value = _required_text(getattr(option, "value", None), "lot node editor option value")
            options.append((label, option_value))
        result[name] = tuple(options)
    return result


def _is_sensitive_field_name(name: str) -> bool:
    return "secret" in name or "payment_msg" in name or "csrf" in name


def _map_library_error(error: BaseException) -> FunPayError:
    name = error.__class__.__name__.lower()
    message = str(error).lower()
    if "auth" in name or any(marker in message for marker in ("сесси", "cookie", "авториз", "invalid cookie")):
        return FunPaySessionExpired("FunPay session was rejected or expired")
    if any(marker in name or marker in message for marker in ("timeout", "connect", "network", "requesterror", "429")):
        return FunPayNetworkUnavailable("FunPay is unavailable after controlled retries")
    return FunPayProtocolError("FunPay response could not be normalized")
