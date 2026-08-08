"""Read-only FunPay integration boundary.

The public FunPay site does not publish a stable seller API contract.  This
module therefore accepts only owner-configured relative read endpoints and
keeps the rest of the application behind a small, testable interface.  It
never issues a state-changing HTTP request.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class RealOperationsDisabled(RuntimeError):
    """Raised before a state-changing FunPay action can be attempted."""


class FunPayError(RuntimeError):
    """Base class for a read-only FunPay integration error."""


class FunPaySessionExpired(FunPayError):
    """The local session is absent, invalid, or has expired."""


class FunPayNetworkUnavailable(FunPayError):
    """The endpoint could not be reached after controlled retries."""


class FunPayProtocolError(FunPayError):
    """The configured endpoint or its response does not match the adapter."""


class FunPayReplyClient(Protocol):
    """Explicitly enabled state-changing boundary for buyer replies."""

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None: ...


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
class FunPayDialog:
    dialog_id: str
    counterparty_id: str
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
    """The application-facing, read-only FunPay contract."""

    def check_authorization(self) -> bool: ...

    def get_profile(self) -> FunPayProfile: ...

    def get_own_lots(self) -> tuple[FunPayLot, ...]: ...

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]: ...

    def get_dialogs(self) -> tuple[FunPayDialog, ...]: ...

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]: ...

    def check_bump_availability(self, lot_id: str) -> bool: ...


class DisabledFunPayReplyClient:
    """Safe default: no FunPay write request can leave the process."""

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        raise RealOperationsDisabled("FunPay replies require local live-mode configuration")


class ConfiguredFunPayReplyClient:
    """Owner-configured POST adapter. The endpoint must honour idempotency_key."""

    def __init__(self, session_provider: Callable[[], str | None], reply_endpoint: str, *, timeout_seconds: int) -> None:
        if not reply_endpoint.startswith("/") or "//" in reply_endpoint or ":" in reply_endpoint:
            raise ValueError("reply_endpoint must be a relative path")
        self._session_provider = session_provider
        self._reply_endpoint = reply_endpoint
        self._timeout_seconds = timeout_seconds

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        if not all((dialog_id.strip(), buyer_nickname.strip(), body.strip(), idempotency_key.strip())):
            raise ValueError("dialog, buyer, reply body, and idempotency key are required")
        session = self._session_provider()
        if not session or "\r" in session or "\n" in session:
            raise FunPaySessionExpired("FunPay session is unavailable locally")
        request = Request(
            "https://funpay.com" + self._reply_endpoint,
            data=json.dumps({
                "dialog_id": dialog_id, "buyer_nickname": buyer_nickname, "body": body,
                "idempotency_key": idempotency_key,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json", "Cookie": f"golden_key={session}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310 - fixed HTTPS FunPay host
                status_code = response.status
        except HTTPError as error:
            if error.code in {401, 403}:
                raise FunPaySessionExpired("FunPay session was rejected or expired") from error
            raise FunPayProtocolError("FunPay reply endpoint rejected the request") from error
        except (OSError, socket.timeout, URLError) as error:
            raise FunPayNetworkUnavailable("FunPay reply endpoint is unavailable") from error
        if not 200 <= status_code < 300:
            raise FunPayProtocolError("FunPay reply endpoint rejected the request")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout_seconds: int) -> HttpResponse: ...


class UrlLibTransport:
    """Small standard-library transport.  Only GET is exposed by design."""

    def get(self, url: str, *, headers: Mapping[str, str], timeout_seconds: int) -> HttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - fixed HTTPS base URL
                return HttpResponse(response.status, dict(response.headers.items()), response.read())
        except HTTPError as error:
            return HttpResponse(error.code, dict(error.headers.items()) if error.headers else {}, error.read())


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds <= 0 or self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError("retry delays must be positive and increasing")


class RequestRateLimiter:
    """A deterministic per-client request gap, injectable for tests."""

    def __init__(
        self, minimum_interval_seconds: float, *, clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must not be negative")
        self._minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    def wait(self) -> None:
        if self._last_request_at is not None:
            remaining = self._minimum_interval_seconds - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_at = self._clock()


@dataclass(frozen=True)
class ReadEndpoints:
    """Relative endpoint templates supplied by the account owner.

    Values may include ``{seller_id}``, ``{after_message_id}``, and ``{lot_id}``.
    Empty values deliberately leave that read capability disabled.
    """

    profile: str = ""
    own_lots: str = ""
    seller_lots: str = ""
    dialogs: str = ""
    new_messages: str = ""
    bump_availability: str = ""


class ReadOnlyFunPayHttpClient:
    """HTTP implementation which only performs authenticated GET requests."""

    def __init__(
        self,
        *,
        session_provider: Callable[[], str | None],
        endpoints: ReadEndpoints,
        transport: HttpTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RequestRateLimiter | None = None,
        base_url: str = "https://funpay.com",
        timeout_seconds: int = 15,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if base_url.rstrip("/") != "https://funpay.com":
            raise ValueError("base_url must remain an HTTPS FunPay URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session_provider = session_provider
        self._endpoints = endpoints
        self._transport = transport or UrlLibTransport()
        self._retry_policy = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter or RequestRateLimiter(1.0)
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._sleeper = sleeper

    def check_authorization(self) -> bool:
        return self.get_profile().authorized

    def get_profile(self) -> FunPayProfile:
        data = self._read_json("profile")
        profile = _object(data["profile"], "profile") if "profile" in data else data
        return FunPayProfile(
            account_id=_text(profile, "account_id"),
            username=_text(profile, "username"),
            authorized=_boolean(profile, "authorized"),
        )

    def get_own_lots(self) -> tuple[FunPayLot, ...]:
        return self._lots(self._read_json("own_lots"))

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]:
        return self._lots(self._read_json("seller_lots", seller_id=seller_id))

    def get_dialogs(self) -> tuple[FunPayDialog, ...]:
        return tuple(
            FunPayDialog(
                dialog_id=_text(item, "dialog_id"),
                counterparty_id=_text(item, "counterparty_id"),
                counterparty_name=_text(item, "counterparty_name"),
                last_message_at=_optional_text(item, "last_message_at"),
            )
            for item in _items(self._read_json("dialogs"), "dialogs")
        )

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        return tuple(
            FunPayMessage(
                message_id=_text(item, "message_id"),
                dialog_id=_text(item, "dialog_id"),
                direction=_one_of(item, "direction", {"incoming", "outgoing"}),
                body=_text(item, "body"),
                sent_at=_optional_text(item, "sent_at"),
                buyer_nickname=_optional_text(item, "buyer_nickname"),
                related_item=_optional_text(item, "related_item"),
                dialog_url=_optional_text(item, "dialog_url"),
            )
            for item in _items(
                self._read_json("new_messages", after_message_id=after_message_id or ""), "messages"
            )
        )

    def check_bump_availability(self, lot_id: str) -> bool:
        return _boolean(self._read_json("bump_availability", lot_id=lot_id), "available")

    def _lots(self, document: Mapping[str, Any]) -> tuple[FunPayLot, ...]:
        return tuple(
            FunPayLot(
                lot_id=_text(item, "lot_id"),
                title=_text(item, "title"),
                price_minor=_positive_or_zero_int(item, "price_minor"),
                currency=_text(item, "currency"),
                seller_id=_text(item, "seller_id"),
            )
            for item in _items(document, "lots")
        )

    def _read_json(self, endpoint_name: str, **parameters: str) -> Mapping[str, Any]:
        session = self._session_provider()
        if not session:
            raise FunPaySessionExpired("FunPay session is not configured locally")
        if "\r" in session or "\n" in session:
            raise FunPaySessionExpired("FunPay session has an unsafe local format")
        path = getattr(self._endpoints, endpoint_name)
        if not path:
            raise FunPayProtocolError(f"read endpoint '{endpoint_name}' is not configured")
        if not path.startswith("/") or "//" in path or ":" in path:
            raise FunPayProtocolError(f"read endpoint '{endpoint_name}' must be a relative path")
        for name, value in parameters.items():
            path = path.replace("{" + name + "}", quote(value, safe=""))
        if "{" in path or "}" in path:
            raise FunPayProtocolError(f"read endpoint '{endpoint_name}' has an unresolved parameter")
        response = self._request(path, session)
        if response.status_code in {401, 403}:
            raise FunPaySessionExpired("FunPay session was rejected or expired")
        if not 200 <= response.status_code < 300:
            raise FunPayProtocolError(f"FunPay read endpoint returned HTTP {response.status_code}")
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FunPayProtocolError("FunPay response is not valid UTF-8 JSON") from error
        return _object(decoded, "response")

    def _request(self, path: str, session: str) -> HttpResponse:
        retryable_statuses = {429, 500, 502, 503, 504}
        last_network_error: BaseException | None = None
        for attempt in range(self._retry_policy.max_attempts):
            self._rate_limiter.wait()
            try:
                response = self._transport.get(
                    self._base_url + path,
                    headers={"Accept": "application/json", "Cookie": f"golden_key={session}"},
                    timeout_seconds=self._timeout_seconds,
                )
            except (OSError, socket.timeout, URLError) as error:
                last_network_error = error
                response = None
            if response is not None and response.status_code not in retryable_statuses:
                return response
            if attempt + 1 < self._retry_policy.max_attempts:
                self._sleeper(min(
                    self._retry_policy.initial_delay_seconds * (2**attempt),
                    self._retry_policy.maximum_delay_seconds,
                ))
            elif response is not None:
                return response
        raise FunPayNetworkUnavailable("FunPay is unavailable after controlled retries") from last_network_error


@dataclass
class MockFunPayClient:
    """In-memory implementation for deterministic application tests."""

    authorized: bool = True
    profile: FunPayProfile = field(default_factory=lambda: FunPayProfile("mock", "mock", True))
    own_lots: tuple[FunPayLot, ...] = ()
    seller_lots: Mapping[str, tuple[FunPayLot, ...]] = field(default_factory=dict)
    dialogs: tuple[FunPayDialog, ...] = ()
    messages: tuple[FunPayMessage, ...] = ()
    bump_available: Mapping[str, bool] = field(default_factory=dict)
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

    def get_seller_lots(self, seller_id: str) -> tuple[FunPayLot, ...]:
        self.calls.append("get_seller_lots")
        return self.seller_lots.get(seller_id, ())

    def get_dialogs(self) -> tuple[FunPayDialog, ...]:
        self.calls.append("get_dialogs")
        return self.dialogs

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        self.calls.append("get_new_messages")
        return self.messages

    def check_bump_availability(self, lot_id: str) -> bool:
        self.calls.append("check_bump_availability")
        return self.bump_available.get(lot_id, False)


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FunPayProtocolError(f"{field_name} must be an object")
    return value


def _items(document: Mapping[str, Any], field_name: str) -> tuple[Mapping[str, Any], ...]:
    value = document.get(field_name)
    if not isinstance(value, list):
        raise FunPayProtocolError(f"{field_name} must be an array")
    return tuple(_object(item, field_name) for item in value)


def _text(document: Mapping[str, Any], field_name: str) -> str:
    value = document.get(field_name)
    if not isinstance(value, str) or not value:
        raise FunPayProtocolError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(document: Mapping[str, Any], field_name: str) -> str | None:
    value = document.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FunPayProtocolError(f"{field_name} must be a string or null")
    return value


def _boolean(document: Mapping[str, Any], field_name: str) -> bool:
    value = document.get(field_name)
    if not isinstance(value, bool):
        raise FunPayProtocolError(f"{field_name} must be a boolean")
    return value


def _positive_or_zero_int(document: Mapping[str, Any], field_name: str) -> int:
    value = document.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FunPayProtocolError(f"{field_name} must be a non-negative integer")
    return value


def _one_of(document: Mapping[str, Any], field_name: str, choices: set[str]) -> str:
    value = _text(document, field_name)
    if value not in choices:
        raise FunPayProtocolError(f"{field_name} must be one of the supported values")
    return value


def session_from_local_store(store: Any, credential_key: str) -> Callable[[], str | None]:
    """Adapt the DPAPI ``SecretStore`` without ever exposing its value to logs."""

    if not credential_key.replace("_", "").isalnum():
        raise ValueError("credential_key must be a simple local secret key")
    return lambda: store.get(credential_key)


def build_read_client(settings: Any, local_secret_store: Any) -> ReadOnlyFunPayHttpClient:
    """Compose the client from validated non-secret settings and a DPAPI store.

    Constructing the client neither reads nor transmits the session.  The store
    is consulted only immediately before an explicit read method is called.
    """

    return ReadOnlyFunPayHttpClient(
        session_provider=session_from_local_store(local_secret_store, settings.funpay_credential_key),
        endpoints=ReadEndpoints(**dict(settings.funpay_read_endpoints)),
        retry_policy=RetryPolicy(max_attempts=settings.funpay_retry_attempts),
        rate_limiter=RequestRateLimiter(settings.funpay_min_request_interval_seconds),
        timeout_seconds=settings.funpay_request_timeout_seconds,
    )


def build_reply_client(settings: Any, local_secret_store: Any) -> FunPayReplyClient:
    """Return a write client only after explicit local live-mode opt-in."""

    if not settings.operations_enabled or settings.operation_mode != "live" or not settings.funpay_reply_endpoint:
        return DisabledFunPayReplyClient()
    return ConfiguredFunPayReplyClient(
        session_from_local_store(local_secret_store, settings.funpay_credential_key),
        settings.funpay_reply_endpoint,
        timeout_seconds=settings.funpay_request_timeout_seconds,
    )
