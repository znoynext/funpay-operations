from __future__ import annotations

import unittest

from funpay_operations.telegram import TelegramUpdate
from funpay_operations.telegram_control import CONTROL_MENU, EmergencyStopGate, MockControlService, TelegramControlRouter
from tests.test_telegram import InMemoryStates


class TelegramControlRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.states = InMemoryStates()
        self.services = MockControlService()
        self.gate = EmergencyStopGate(self.states)
        self.router = TelegramControlRouter((1,), self.states, self.services, self.gate)

    def update(self, text: str | None = None, callback: str | None = None, *, user: int = 1, chat: int = 1) -> TelegramUpdate:
        return TelegramUpdate(1, user, chat, text, callback_data=callback)

    def test_main_menu_tree_and_read_actions_use_mock_service(self) -> None:
        self.assertEqual(CONTROL_MENU["keyboard"][0], ["Статус"])
        reply = self.router.handle(self.update("Статус"))
        self.assertEqual(reply.text, "mock:status")
        self.assertEqual(reply.reply_markup, CONTROL_MENU)
        self.router.handle(self.update("Mythic+"))
        self.router.handle(self.update("Delves"))
        self.router.handle(self.update("Trusted sellers"))
        self.assertEqual([item[0] for item in self.services.calls], ["status", "mythic_plus", "delves", "trusted_sellers"])

    def test_mass_actions_require_confirmation_and_cancel_has_no_service_call(self) -> None:
        proposal = self.router.handle(self.update("Обновить цены"))
        callback = proposal.reply_markup["inline_keyboard"][0][1]["callback_data"]
        cancelled = self.router.handle(self.update(callback=callback))
        self.assertIn("cancelled", cancelled.text)
        self.assertEqual(self.services.calls, [])

    def test_confirmed_actions_route_to_service_layer(self) -> None:
        proposal = self.router.handle(self.update("Rollback"))
        callback = proposal.reply_markup["inline_keyboard"][0][0]["callback_data"]
        reply = self.router.handle(self.update(callback=callback))
        self.assertEqual(reply.text, "mock:rollback")
        self.assertEqual(self.services.calls, [("rollback", None)])

    def test_own_lot_and_trusted_seller_controls_are_available(self) -> None:
        self.router.handle(self.update(callback="control_action:lot_automatic"))
        self.router.handle(self.update(callback="control_action:lot_fixed_price"))
        self.router.handle(self.update(callback="control_action:lot_paused"))
        self.router.handle(self.update(callback="control_action:lot_check_only"))
        self.router.handle(self.update(callback="control_action:lot_hard_floor"))
        self.router.handle(self.update(callback="control_action:lot_decision"))
        self.router.handle(self.update(callback="control_action:seller_add"))
        self.router.handle(self.update(callback="control_action:seller_remove"))
        self.router.handle(self.update(callback="control_action:seller_disable"))
        self.router.handle(self.update(callback="control_action:seller_recheck"))
        self.router.handle(self.update(callback="control_action:seller_remap"))
        self.assertEqual(len(self.services.calls), 11)

    def test_emergency_stop_blocks_confirmed_writes_but_not_read_menu(self) -> None:
        self.router.handle(self.update("Emergency stop"))
        self.assertTrue(self.gate.active())
        proposal = self.router.handle(self.update("Обновить и поднять"))
        confirm = proposal.reply_markup["inline_keyboard"][0][0]["callback_data"]
        blocked = self.router.handle(self.update(callback=confirm))
        self.assertIn("blocks", blocked.text)
        self.assertEqual(self.services.calls, [])
        self.router.handle(self.update("Статус"))
        self.assertEqual(self.services.calls, [("status", None)])

    def test_allowlist_is_preserved(self) -> None:
        self.assertIsNone(self.router.handle(self.update("Статус", user=2, chat=2)))
        self.assertIsNone(self.router.handle(self.update("Статус", user=1, chat=-1)))
        self.assertEqual(self.services.calls, [])
