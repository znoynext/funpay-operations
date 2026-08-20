from __future__ import annotations

import logging
import unittest

from funpay_operations.telegram import MockTelegramApi, TelegramCommandHandler, TelegramLongPollingBot, TelegramUpdate
from funpay_operations.telegram_control import CONTROL_MENU, EmergencyStopGate, MockControlService, TelegramControlRouter
from funpay_operations.telegram_views import LotView
from tests.test_telegram import InMemoryStates


class FailingControlService(MockControlService):
    def execute(self, action: str, payload: str | None = None) -> str:
        if action == "check_prices":
            raise RuntimeError("synthetic backend failure")
        return super().execute(action, payload)


class TelegramControlRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.states = InMemoryStates()
        self.services = MockControlService()
        self.gate = EmergencyStopGate(self.states)
        self.router = TelegramControlRouter((1,), self.states, self.services, self.gate)

    @staticmethod
    def update(text: str | None = None, callback: str | None = None, *, user: int = 1, chat: int = 1,
               reply_to: int | None = None) -> TelegramUpdate:
        return TelegramUpdate(1, user, chat, text, reply_to_message_id=reply_to, callback_data=callback)

    @staticmethod
    def callback(reply, label: str) -> str:
        markup = reply.reply_markup if hasattr(reply, "reply_markup") else reply
        for row in markup["inline_keyboard"]:
            for button in row:
                if button["text"] == label:
                    return button["callback_data"]
        raise AssertionError(f"button not found: {label}")

    def press(self, reply, label: str):
        return self.router.handle(self.update(callback=self.callback(reply, label)))

    def test_dashboard_has_compact_human_menu_and_no_emergency_button(self) -> None:
        reply = self.router.handle(self.update("Статус"))
        self.assertIn("🤖 FunPay Operations for World of Warcraft Mythic+", reply.text)
        self.assertIn("Emergency stop", reply.text)
        self.assertEqual(CONTROL_MENU["keyboard"][0], ["⚔️ Mythic+"])
        self.assertNotIn("Delves", str(CONTROL_MENU))
        self.assertNotIn("Emergency stop", str(CONTROL_MENU))
        self.assertIn("inline_keyboard", reply.reply_markup)

    def test_home_and_back_navigation_edit_the_same_panel(self) -> None:
        dashboard = self.router.handle(self.update("⚔️ Mythic+"))
        family = self.press(dashboard, "📦 Лоты")
        lots = self.press(family, "Назад")
        self.assertIn("⚔️ Mythic+", lots.text)
        self.assertTrue(lots.edit_message)
        home = self.press(lots, "🏠 Главная")
        self.assertIn("🤖 FunPay Operations for World of Warcraft Mythic+", home.text)

    def test_only_mythic_family_summary_is_available(self) -> None:
        mythic = self.router.handle(self.update("⚔️ Mythic+"))
        self.assertIn("Управляемые лоты", mythic.text)
        self.assertIn("Automatic", mythic.text)
        self.assertIsNone(self.router.handle(self.update("🕳 Delves")))

    def test_lot_details_hide_internal_reference_until_details_button(self) -> None:
        family = self.router.handle(self.update("⚔️ Mythic+"))
        lots = self.press(family, "📦 Лоты")
        lot = self.press(lots, "Mythic+ +10")
        self.assertIn("EU • Self-play • x1", lot.text)
        self.assertIn("1505 ₽ × 0.99 = 1489 ₽", lot.text)
        self.assertNotIn("mock-m10", lot.text)
        detail = self.press(lot, "Подробнее")
        self.assertIn("mock-m10", detail.text)

    def test_lot_mode_fixed_price_and_hard_floor_use_clear_input_state(self) -> None:
        family = self.router.handle(self.update("⚔️ Mythic+"))
        lot = self.press(self.press(family, "📦 Лоты"), "Mythic+ +10")
        fixed = self.press(lot, "Fixed price (локально)")
        self.assertIn("Введите fixed price", fixed.text)
        updated = self.router.handle(self.update("1555"))
        self.assertIn("Цена: 1555 ₽", updated.text)
        self.press(updated, "Минимальная цена")
        updated = self.router.handle(self.update("1200"))
        self.assertIn("Минимально допустимая цена: 1200 ₽", updated.text)
        self.assertIn(("lot_set_fixed", "m10:1555"), self.services.calls)
        self.assertIn(("lot_set_floor", "m10:1200"), self.services.calls)

    def test_non_managed_lot_has_no_pricing_or_mutation_controls(self) -> None:
        services = MockControlService(_lots={
            "other": LotView(
                "other", "Другие лоты", "Other WoW service", ("Параметры не подтверждены",),
                100_000, "check_only", None, (), "Расчёт недоступен",
                warning="Не управляется ботом.", managed=False,
            ),
        })
        router = TelegramControlRouter((1,), self.states, services, self.gate)
        lots = router.handle(self.update("📦 Лоты"))
        lot = router.handle(self.update(callback=self.callback(lots, "Other WoW service")))
        buttons = [button["text"] for row in lot.reply_markup["inline_keyboard"] for button in row]
        self.assertEqual(buttons, ["Подробнее", "Назад", "🏠 Главная"])
        self.assertNotIn("Режим (локально)", buttons)

    def test_price_preview_has_only_changes_then_confirmation(self) -> None:
        prices = self.router.handle(self.update("💰 Цены"))
        preview = self.press(prices, "Обновить цены")
        self.assertIn("1490 ₽ → 1420 ₽", preview.text)
        self.assertIn("fixed price", preview.text)
        self.assertNotIn(("mass_price_update", None), self.services.calls)
        confirmed = self.press(preview, "✅ Подтвердить")
        self.assertIn("Обновление цен", confirmed.text)
        self.assertIn(("mass_price_update", None), self.services.calls)

    def test_price_preview_cancellation_has_no_write(self) -> None:
        preview = self.press(self.router.handle(self.update("💰 Цены")), "Обновить цены")
        cancelled = self.press(preview, "❌ Отмена")
        self.assertIn("Операция отменена", cancelled.text)
        self.assertNotIn(("mass_price_update", None), self.services.calls)

    def test_check_prices_is_read_only_without_confirmation(self) -> None:
        prices = self.router.handle(self.update("💰 Цены"))
        result = self.press(prices, "Проверить сейчас")
        self.assertIn("только чтение", result.text)
        self.assertIn(("check_prices", None), self.services.calls)

    def test_trusted_seller_add_remove_and_ambiguous_mapping_are_button_first(self) -> None:
        sellers = self.router.handle(self.update("👥 Продавцы"))
        self.assertIn("SellerOne 🟢", sellers.text)
        prompt = self.press(sellers, "Добавить")
        self.assertEqual(prompt.text, "Введите ник продавца")
        found = self.router.handle(self.update("SellerName"))
        self.assertIn("FunPay profile verified", found.text)
        added = self.press(found, "Добавить для Mythic+")
        self.assertIn("SellerName 🟢", added.text)
        disable = self.press(added, "Отключить")
        disabled = self.press(disable, "SellerTwo")
        self.assertIn("SellerTwo ⚪", disabled.text)
        self.assertIn(("seller_disable", "SellerTwo"), self.services.calls)
        mappings = self.press(disabled, "Соответствия")
        self.assertIn("Нужно выбрать точное соответствие", mappings.text)
        remapped = self.press(mappings, "Mythic+ +10 package")
        self.assertIn(("seller_remap", "Mythic+ +10 package"), self.services.calls)
        remove = self.press(remapped, "Удалить")
        deleted = self.press(remove, "SellerName")
        self.assertNotIn("SellerName 🟢", deleted.text)
        self.assertIn(("seller_add", "SellerName"), self.services.calls)

    def test_batch_own_mapping_seller_and_floor_onboarding_is_confirmation_first(self) -> None:
        family = self.router.handle(self.update("⚔️ Mythic+"))
        mapping = self.press(family, "⚙️ Настроить Mythic+ лоты")
        self.assertIn("Уверенно распознано: 2", mapping.text)
        overview_buttons = [
            button["text"] for row in mapping.reply_markup["inline_keyboard"] for button in row
        ]
        self.assertNotIn("✅ Подтвердить точные", overview_buttons)
        preview = self.press(mapping, "👀 Проверить все")
        self.assertNotIn(("confirm_own_high", None), self.services.calls)
        confirmed = self.press(preview, "✅ Подтвердить точные")
        self.assertIn(("confirm_own_high", None), self.services.calls)
        self.assertIn("Настройка Mythic+ лотов", confirmed.text)

        sellers = self.router.handle(self.update("👥 Продавцы"))
        prompt = self.press(sellers, "📋 Добавить списком")
        self.assertIn("по одному в строке", prompt.text)
        seller_preview = self.router.handle(self.update("SellerThree\nSellerFour"))
        self.assertIn("✅ SellerThree", seller_preview.text)
        self.press(seller_preview, "✅ Добавить точные")
        self.assertIn(("seller_add_batch", None), self.services.calls)

        floors = self.router.handle(self.update("Минимальные цены"))
        floor_prompt = self.press(floors, "📋 Задать по ключам")
        self.assertIn("+2 500", floor_prompt.text)
        floor_preview = self.router.handle(self.update("+10 1000\n+12 1500"))
        self.assertIn("+10 → 1000 ₽", floor_preview.text)
        self.assertNotIn(("floor_set_key_batch", None), self.services.calls)
        self.press(floor_preview, "✅ Сохранить")
        self.assertIn(("floor_set_key_batch", None), self.services.calls)

    def test_readiness_and_prepare_live_test_never_expose_write_confirmation(self) -> None:
        screen = self.router.handle(self.update("Готовность"))
        self.assertIn("LIVE:\n🔒 Выключен", screen.text)
        explanation = self.press(screen, "🧪 Подготовить первый live-тест")
        self.assertIn("отдельного явного разрешения", explanation.text)
        self.assertNotIn(("mass_price_update", None), self.services.calls)

    def test_messages_screen_explains_reply_flow_without_dialog_ids(self) -> None:
        reply = self.router.handle(self.update("💬 Сообщения"))
        self.assertIn("Нажмите «Ответить»", reply.text)
        self.assertNotIn("dialog_id", reply.text)

    def test_emergency_stop_requires_confirmation_blocks_writes_and_resume_requires_confirmation(self) -> None:
        settings = self.router.handle(self.update("⚙️ Настройки"))
        warning = self.press(settings, "⚠️ Emergency stop")
        self.assertFalse(self.gate.active())
        stopped = self.press(warning, "🛑 Остановить")
        self.assertTrue(self.gate.active())
        self.assertIn("AUTOMATION STOPPED", stopped.text)
        preview = self.router.handle(self.update("💰 Цены"))
        preview = self.press(preview, "Обновить цены")
        blocked = self.press(preview, "✅ Подтвердить")
        self.assertIn("блокирует", blocked.text)
        dashboard = self.press(blocked, "🏠 Главная")
        resume = self.press(dashboard, "▶️ Возобновить")
        self.assertTrue(self.gate.active())
        resumed = self.press(resume, "✅ Подтвердить")
        self.assertFalse(self.gate.active())
        self.assertIn("Automation resumed", resumed.text)

    def test_stale_and_malformed_callbacks_are_safe_and_offer_refresh(self) -> None:
        dashboard = self.router.handle(self.update("Статус"))
        stale_callback = self.callback(dashboard, "💰 Цены")
        self.press(dashboard, "⚔️ Mythic+")
        stale = self.router.handle(self.update(callback=stale_callback))
        malformed = self.router.handle(self.update(callback="ux:bad!"))
        self.assertIn("устарел", stale.text)
        self.assertIn("устарел", malformed.text)
        self.assertTrue(stale.edit_message)

    def test_unauthorized_user_cannot_get_or_trigger_control_callback(self) -> None:
        dashboard = self.router.handle(self.update("Статус"))
        callback = self.callback(dashboard, "💰 Цены")
        self.assertIsNone(self.router.handle(self.update("💰 Цены", user=2, chat=2)))
        self.assertIsNone(self.router.handle(self.update(callback=callback, user=2, chat=2)))
        self.assertEqual(self.services.calls, [])

    def test_owner_can_queue_probe_from_home_and_unauthorized_user_cannot(self) -> None:
        home = self.router.handle(self.update("Статус"))
        started = self.press(home, "🔍 Проверить FunPay")
        self.assertIn("Проверяю FunPay", started.text)
        self.assertEqual(self.services.calls, [("run_probe", None)])

        other = TelegramControlRouter((1,), self.states, MockControlService(), self.gate)
        self.assertIsNone(other.handle(self.update("🔍 Проверить FunPay", user=2, chat=2)))
        self.assertEqual(other.services.calls, [])

    def test_probe_button_is_also_present_in_settings(self) -> None:
        settings = self.router.handle(self.update("⚙️ Настройки"))
        probe = self.press(settings, "🔍 Проверить FunPay")
        self.assertIn("Ничего на FunPay не изменяется", probe.text)

    def test_sanitized_probe_completion_callback_is_owner_only(self) -> None:
        status = self.router.handle(self.update(callback="probe:status"))
        self.assertIn("Безопасная проверка FunPay", status.text)
        self.assertIsNone(self.router.handle(self.update(callback="probe:status", user=2, chat=2)))

    def test_funpay_reauthorization_help_callback_is_private_and_has_no_session_data(self) -> None:
        reply = self.router.handle(self.update(callback="setup:funpay"))
        self.assertIn("Как восстановить FunPay", reply.text)
        self.assertNotIn("golden_key", reply.text)
        self.assertNotIn("golden_seal", reply.text)
        self.assertTrue(reply.edit_message)
        self.assertIsNone(self.router.handle(self.update(callback="setup:funpay", user=2, chat=2)))

    def test_service_failure_has_human_error_message(self) -> None:
        router = TelegramControlRouter((1,), self.states, FailingControlService(), self.gate)
        prices = router.handle(self.update("💰 Цены"))
        failed = router.handle(self.update(callback=self.callback(prices, "Проверить сейчас")))
        self.assertIn("Не удалось выполнить проверку", failed.text)
        self.assertNotIn("synthetic backend failure", failed.text)

    def test_back_cancels_pending_text_input_before_next_navigation(self) -> None:
        sellers = self.router.handle(self.update("👥 Продавцы"))
        prompt = self.press(sellers, "Добавить")
        self.press(prompt, "Назад")
        prices = self.router.handle(self.update("💰 Цены"))
        self.assertIn("💰 Цены", prices.text)
        self.assertNotIn(("seller_add", "💰 Цены"), self.services.calls)

    def test_seller_input_and_callback_tokens_are_constrained(self) -> None:
        sellers = self.router.handle(self.update("👥 Продавцы"))
        self.press(sellers, "Добавить")
        invalid = self.router.handle(self.update("seller\nother"))
        self.assertIn("одной строкой", invalid.text)
        unicode_callback = self.router.handle(self.update(callback="ux:абв"))
        self.assertIn("устарел", unicode_callback.text)

    def test_callback_navigation_edits_existing_telegram_message(self) -> None:
        api = MockTelegramApi(update_batches=[(TelegramUpdate(1, 1, 1, "Статус"),)])
        handler = TelegramCommandHandler((1,), self.states, logging.getLogger("telegram-control-edit"))
        bot = TelegramLongPollingBot(api, handler, self.states, logging.getLogger("telegram-control-edit"), timeout_seconds=1)
        bot.set_interaction_router(self.router)
        bot.poll_once()
        callback = self.callback(api.sent_messages[0][2], "⚔️ Mythic+")
        api.update_batches.append((TelegramUpdate(2, 1, 1, None, reply_to_message_id=1, callback_data=callback),))
        bot.poll_once()
        self.assertEqual(len(api.sent_messages), 1)
        self.assertEqual(api.edited_messages[0][1], 1)
        self.assertIn("Mythic+", api.edited_messages[0][2])

    def test_callbacks_are_short_opaque_and_bound_to_the_screen_owner(self) -> None:
        dashboard = self.router.handle(self.update("Статус"))
        callbacks = [button["callback_data"] for row in dashboard.reply_markup["inline_keyboard"] for button in row]
        self.assertTrue(all(item.startswith("ux:") and len(item) <= 64 for item in callbacks))
        self.assertTrue(all("m10" not in item and "secret" not in item for item in callbacks))
