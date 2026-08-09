from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from funpay_operations.config import Settings
from funpay_operations.funpay import (
    DisabledFunPayReplyClient,
    FunPayBumpMetadata,
    FunPayProfile,
    FunPayProtocolError,
    FunPaySessionExpired,
    MockFunPayClient,
    NativeFunPayClient,
    build_read_client,
    build_reply_client,
)
from funpay_operations.setup_wizard import SecretStore


@dataclass
class FakeLot:
    id: str
    name: str


@dataclass
class FakeChat:
    id: str
    username: str
    date: str = "2026-08-09 12:00"


@dataclass
class FakeMessage:
    node_msg_id: int | str | None
    sender: str | None
    text: str | None
    is_system: bool = False


class FakeProfileManager:
    def __init__(self, account: object) -> None:
        self.account = account
        self.profiles = {
            "7": SimpleNamespace(category_ids=["10", "11"], lots=[FakeLot("a", "Own")]),
            "42": SimpleNamespace(category_ids=["20"], lots=[FakeLot("b", "Other")]),
            "404": SimpleNamespace(category_ids=[], lots=[]),
        }

    async def get_user_data(self) -> object:
        return SimpleNamespace(user_id="7")

    async def profile(self, seller_id: str) -> object:
        return self.profiles[seller_id]


class FakeLotManager:
    async def get_lot_info(self, lot_id: str) -> object:
        return SimpleNamespace(price={"a": "12.345", "b": 20}[lot_id])


class FakeChatManager:
    def __init__(self) -> None:
        self.chats: list[FakeChat] = [FakeChat("100", "Buyer")]
        self.messages: list[FakeMessage] = [
            FakeMessage(5, "Buyer", "first"),
            FakeMessage(6, "Buyer", "second"),
        ]
        self.sent: list[tuple[str, str]] = []

    async def get_chats(self) -> list[FakeChat]:
        return self.chats

    async def get_chat_data(self, chat_id: str, after: int | None = None) -> object:
        messages = self.messages if after is None else [item for item in self.messages if isinstance(item.node_msg_id, int) and item.node_msg_id > after]
        return SimpleNamespace(node_name="users-7-88", last_messages=messages)

    async def send_message(self, chat_id: str, body: str) -> object:
        self.sent.append((chat_id, body))
        return {"response": {"error": None}}


class FakeTools:
    def __init__(self) -> None:
        account = SimpleNamespace(data=SimpleNamespace(username="Owner", user_id="7"))
        account.profile = FakeProfileManager(account)
        account.lot = FakeLotManager()
        account.chat = FakeChatManager()
        self.account = account
        self.closed = False

    async def __aenter__(self) -> "FakeTools":
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.closed = True


class FpxAuthError(Exception):
    pass


class FailingTools(FakeTools):
    def __init__(self) -> None:
        super().__init__()
        self.account.profile.get_user_data = self.fail  # type: ignore[method-assign]

    async def fail(self) -> object:
        raise FpxAuthError("invalid cookies")


class FunPayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools: list[FakeTools] = []

    def client(self, *, allow_replies: bool = False, factory: object | None = None) -> NativeFunPayClient:
        def tools_factory(key: str, seal: str) -> FakeTools:
            self.assertEqual((key, seal), ("key", "seal"))
            tools = FakeTools() if factory is None else factory()  # type: ignore[operator]
            self.tools.append(tools)
            return tools

        return NativeFunPayClient(
            lambda: json.dumps({"golden_key": "key", "golden_seal": "seal"}),
            currency="RUB", allow_replies=allow_replies, tools_factory=tools_factory,
        )

    def test_normalizes_profile_lots_dialogs_and_messages(self) -> None:
        client = self.client()

        self.assertTrue(client.check_authorization())
        self.assertEqual(client.get_profile(), FunPayProfile("7", "Owner", True))
        self.assertEqual(client.get_own_lots()[0].price_minor, 1235)
        self.assertEqual(client.get_seller_lots("42")[0].seller_id, "42")
        dialog = client.get_dialogs()[0]
        self.assertEqual((dialog.dialog_id, dialog.counterparty_id, dialog.counterparty_name), ("100", "88", "Buyer"))
        messages = client.get_new_messages()
        self.assertEqual([message.message_id for message in messages], ["100:5", "100:6"])
        self.assertEqual(messages[0].direction, "incoming")
        self.assertTrue(all(tool.closed for tool in self.tools))

    def test_expired_and_invalid_session_fail_without_exposure(self) -> None:
        missing = NativeFunPayClient(lambda: None, currency="RUB", allow_replies=False, tools_factory=lambda *_: FakeTools())
        self.assertFalse(missing.has_local_session())
        self.assertFalse(missing.check_authorization())
        with self.assertRaises(FunPaySessionExpired):
            missing.get_profile()

        invalid = NativeFunPayClient(lambda: "not-json", currency="RUB", allow_replies=False, tools_factory=lambda *_: FakeTools())
        with self.assertRaises(FunPaySessionExpired):
            invalid.get_profile()

        rejected = self.client(factory=FailingTools)
        self.assertFalse(rejected.check_authorization())

    def test_reconnect_cursor_and_duplicate_event_protection(self) -> None:
        client = self.client()
        tools = FakeTools()
        tools.account.chat.messages.append(FakeMessage(6, "Buyer", "duplicate"))
        client._tools_factory = lambda *_: tools  # type: ignore[method-assign]

        recovered = client.get_new_messages('{"100":"100:5"}')
        self.assertEqual([message.message_id for message in recovered], ["100:6"])

    def test_malformed_events_empty_dialogs_and_missing_lot_are_safe(self) -> None:
        client = self.client()
        tools = FakeTools()
        tools.account.chat.messages = [FakeMessage(None, "Buyer", "bad"), FakeMessage(7, None, "bad"), FakeMessage(8, "Buyer", None)]
        client._tools_factory = lambda *_: tools  # type: ignore[method-assign]
        self.assertEqual(client.get_new_messages(), ())

        tools.account.chat.chats = []
        self.assertEqual(client.get_dialogs(), ())
        self.assertIsNone(client.get_bump_metadata("missing"))
        self.assertFalse(client.check_bump_availability("missing"))
        self.assertEqual(client.get_bump_metadata("a"), FunPayBumpMetadata("a", ("10", "11")))

    def test_replies_are_live_only_and_do_not_claim_server_idempotency(self) -> None:
        disabled = self.client()
        with self.assertRaisesRegex(Exception, "live-mode"):
            disabled.send_reply("100", "Buyer", "hello", "local-key")

        enabled = self.client(allow_replies=True)
        enabled.send_reply("100", "Buyer", "hello", "local-key")
        self.assertEqual(self.tools[-1].account.chat.sent, [("100", "hello")])

    def test_mock_client_is_a_complete_double(self) -> None:
        mock = MockFunPayClient(profile=FunPayProfile("mock", "tester", True), bump_metadata={"1": FunPayBumpMetadata("1", ("2",))})
        self.assertTrue(mock.check_authorization())
        self.assertEqual(mock.get_seller_lots("other"), ())
        self.assertTrue(mock.check_bump_availability("1"))

    def test_composition_defers_dpapi_session_access_and_has_no_endpoints(self) -> None:
        settings = Settings(
            "test", "INFO", Path("data"), Path("data/app.sqlite3"), Path("data/logs"), Path("data/backups"),
            "safe", False, 1, 1, 2, "funpay_session", "telegram", (), "RUB", None,
        )
        client = build_read_client(settings, SecretStore(Path("data") / "not-created.dpapi"))
        self.assertFalse(client.has_local_session())
        self.assertIsInstance(build_reply_client(settings, client), DisabledFunPayReplyClient)
