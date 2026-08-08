from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path

from funpay_operations.funpay import (
    FunPayNetworkUnavailable,
    FunPayProfile,
    FunPayProtocolError,
    FunPaySessionExpired,
    HttpResponse,
    MockFunPayClient,
    ReadEndpoints,
    ReadOnlyFunPayHttpClient,
    RequestRateLimiter,
    RetryPolicy,
    build_read_client,
)
from funpay_operations.config import Settings
from funpay_operations.setup_wizard import SecretStore


class FakeTransport:
    def __init__(self, responses: list[HttpResponse | BaseException]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(self, url: str, *, headers: Mapping[str, str], timeout_seconds: int) -> HttpResponse:
        self.calls.append((url, headers))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode("utf-8"))


class FunPayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoints = ReadEndpoints(
            profile="/read/profile",
            own_lots="/read/own-lots",
            seller_lots="/read/sellers/{seller_id}/lots",
            dialogs="/read/dialogs",
            new_messages="/read/messages?after={after_message_id}",
            bump_availability="/read/lots/{lot_id}/bump-availability",
        )

    def client(self, transport: FakeTransport, **kwargs: object) -> ReadOnlyFunPayHttpClient:
        options: dict[str, object] = {
            "rate_limiter": RequestRateLimiter(0),
            "sleeper": lambda _: None,
        }
        options.update(kwargs)
        return ReadOnlyFunPayHttpClient(
            session_provider=lambda: "session-kept-local",
            endpoints=self.endpoints,
            transport=transport,
            **options,
        )

    def test_reads_normalized_models_without_exposing_session(self) -> None:
        transport = FakeTransport([
            response({"profile": {"account_id": "7", "username": "owner", "authorized": True}}),
            response({"lots": [{"lot_id": "a", "title": "Own", "price_minor": 100, "currency": "RUB", "seller_id": "7"}]}),
            response({"lots": [{"lot_id": "b", "title": "Other", "price_minor": 20, "currency": "RUB", "seller_id": "42"}]}),
            response({"dialogs": [{"dialog_id": "d", "counterparty_id": "42", "counterparty_name": "Seller", "last_message_at": None}]}),
            response({"messages": [{"message_id": "m", "dialog_id": "d", "direction": "incoming", "body": "Hello", "sent_at": "2026-08-06T10:00:00Z"}]}),
            response({"available": True}),
        ])
        client = self.client(transport)

        self.assertTrue(client.check_authorization())
        self.assertEqual(client.get_own_lots()[0].price_minor, 100)
        self.assertEqual(client.get_seller_lots("42")[0].seller_id, "42")
        self.assertEqual(client.get_dialogs()[0].counterparty_name, "Seller")
        self.assertEqual(client.get_new_messages("previous")[0].direction, "incoming")
        self.assertTrue(client.check_bump_availability("a"))
        self.assertIn("/read/sellers/42/lots", transport.calls[2][0])
        self.assertIn("after=previous", transport.calls[4][0])
        self.assertEqual(transport.calls[0][1]["Cookie"], "golden_key=session-kept-local")

    def test_missing_or_rejected_session_is_reported_without_retry(self) -> None:
        missing = ReadOnlyFunPayHttpClient(
            session_provider=lambda: None, endpoints=self.endpoints, transport=FakeTransport([]),
        )
        with self.assertRaises(FunPaySessionExpired):
            missing.get_profile()

        rejected = self.client(FakeTransport([response({}, status=401)]))
        with self.assertRaises(FunPaySessionExpired):
            rejected.get_profile()

    def test_network_errors_are_retried_with_a_bound(self) -> None:
        transport = FakeTransport([
            OSError("offline"),
            response({"account_id": "7", "username": "owner", "authorized": True}),
        ])
        delays: list[float] = []
        client = self.client(
            transport,
            retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.5, maximum_delay_seconds=1),
            sleeper=delays.append,
        )
        self.assertEqual(client.get_profile().account_id, "7")
        self.assertEqual(delays, [0.5])
        self.assertEqual(len(transport.calls), 2)

        unavailable = self.client(
            FakeTransport([OSError("offline"), OSError("offline")]),
            retry_policy=RetryPolicy(max_attempts=2),
        )
        with self.assertRaises(FunPayNetworkUnavailable):
            unavailable.get_profile()

    def test_rate_limit_waits_between_requests(self) -> None:
        clock = iter([0.0, 0.1, 1.0])
        waits: list[float] = []
        limiter = RequestRateLimiter(1.0, clock=lambda: next(clock), sleeper=waits.append)
        client = self.client(
            FakeTransport([
                response({"account_id": "7", "username": "owner", "authorized": True}),
                response({"account_id": "7", "username": "owner", "authorized": True}),
            ]),
            rate_limiter=limiter,
        )
        client.get_profile()
        client.get_profile()
        self.assertEqual(waits, [0.9])

    def test_missing_endpoint_and_invalid_response_fail_closed(self) -> None:
        client = ReadOnlyFunPayHttpClient(
            session_provider=lambda: "local", endpoints=ReadEndpoints(), transport=FakeTransport([]),
        )
        with self.assertRaises(FunPayProtocolError):
            client.get_profile()
        invalid = self.client(FakeTransport([response({"profile": {"account_id": "7"}})]))
        with self.assertRaises(FunPayProtocolError):
            invalid.get_profile()

    def test_mock_client_is_a_complete_read_only_double(self) -> None:
        mock = MockFunPayClient(profile=FunPayProfile("mock", "tester", True), bump_available={"1": True})
        self.assertTrue(mock.check_authorization())
        self.assertEqual(mock.get_profile().username, "tester")
        self.assertEqual(mock.get_own_lots(), ())
        self.assertEqual(mock.get_seller_lots("other"), ())
        self.assertEqual(mock.get_dialogs(), ())
        self.assertEqual(mock.get_new_messages(), ())
        self.assertTrue(mock.check_bump_availability("1"))

    def test_composition_defers_dpapi_session_access_until_a_read(self) -> None:
        settings = Settings(
            "test", "INFO", Path("data"), Path("data/app.sqlite3"), Path("data/logs"), Path("data/backups"),
            "safe", False, 1, 1, 2, "funpay_session", "telegram", (), "RUB", None,
            funpay_read_endpoints=(("profile", "/read/profile"),),
        )
        client = build_read_client(settings, SecretStore(Path("data") / "not-created.dpapi"))
        with self.assertRaises(FunPaySessionExpired):
            client.get_profile()
