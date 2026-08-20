"""Compact, mock-ready Telegram control panel.

Business services provide presentation models and perform named actions.  This
module owns only navigation, safe opaque callbacks, confirmation barriers, and
human-friendly rendering.  It deliberately has no FunPay or Telegram network
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol

from .telegram import CommandReply, TelegramInteractionRouter, TelegramUpdate, TaskStateStore
from .telegram_views import (
    DashboardView,
    CompetitorMappingOverviewView,
    FamilyView,
    LotView,
    MappingChoiceView,
    MinimumPriceOverviewView,
    OwnMappingCandidateView,
    OwnMappingOverviewView,
    PriceOverviewView,
    PricePreviewView,
    ReferencePriceView,
    SellerCandidateView,
    SellerBatchPreviewView,
    TrustedSellerView,
    ReadinessView,
    competitor_mapping_overview_text,
    dashboard_text,
    family_text,
    lot_text,
    mappings_text,
    minimum_price_overview_text,
    own_mapping_overview_text,
    own_mapping_preview_text,
    price_overview_text,
    price_preview_text,
    sellers_text,
    seller_candidate_text,
    seller_batch_preview_text,
    settings_text,
    readiness_text,
)


CONTROL_MENU = {
    "keyboard": [
        ["⚔️ Mythic+"],
        ["💰 Цены", "💬 Сообщения"],
        ["👥 Продавцы", "📦 Лоты"],
        ["🔍 Проверить FunPay"],
        ["🔄 Обновить и поднять"],
        ["⚙️ Настройки"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


class ControlService(Protocol):
    """Service boundary consumed by the presentation router."""

    def execute(self, action: str, payload: str | None = None) -> str: ...
    def dashboard(self, *, emergency_active: bool) -> DashboardView: ...
    def family(self, family: str) -> FamilyView: ...
    def lots(self, family: str | None = None) -> tuple[LotView, ...]: ...
    def price_overview(self) -> PriceOverviewView: ...
    def price_preview(self) -> PricePreviewView: ...
    def sellers(self) -> tuple[TrustedSellerView, ...]: ...
    def find_seller(self, nickname: str) -> SellerCandidateView | None: ...
    def find_sellers(self, value: str) -> SellerBatchPreviewView: ...
    def mapping_choices(self) -> tuple[MappingChoiceView, ...]: ...
    def own_mapping_overview(self) -> OwnMappingOverviewView: ...
    def preview_own_mapping_correction(self, key: str, value: str) -> OwnMappingCandidateView: ...
    def competitor_mapping_overview(self) -> CompetitorMappingOverviewView: ...
    def minimum_price_overview(self) -> MinimumPriceOverviewView: ...
    def preview_minimum_price_batch(self, value: str) -> Mapping[int, int]: ...
    def readiness(self) -> ReadinessView: ...
    def probe_status(self) -> str: ...


@dataclass(frozen=True)
class _Intent:
    action: str
    payload: str | None = None


@dataclass(frozen=True)
class _CallbackEntry:
    user_id: int
    revision: int
    intent: _Intent


@dataclass
class MockControlService:
    """Presentation/service double used by all non-live control flows."""

    calls: list[tuple[str, str | None]] = field(default_factory=list)
    _lots: dict[str, LotView] = field(default_factory=dict)
    _sellers: list[TrustedSellerView] = field(default_factory=list)
    external_mutations_allowed: bool = True
    _probe_status: str = "🔍 Безопасная проверка FunPay\n\nПроверка ещё не запускалась."

    def __post_init__(self) -> None:
        if not self._lots:
            self._lots = {
                "m10": LotView(
                    "m10", "Mythic+", "Mythic+ +10", ("EU", "Self-play", "x1"), 149_000,
                    "automatic", 100_000,
                    (ReferencePriceView("Seller A", 151_000), ReferencePriceView("Seller B", 150_500)),
                    "1505 ₽ × 0.99 = 1489 ₽", technical_detail="FunPay lot ID: mock-m10\nService code: hidden from the normal UI",
                ),
                "m12": LotView(
                    "m12", "Mythic+", "Mythic+ +12", ("EU", "Self-play", "x1"), 210_000,
                    "fixed_price", 150_000,
                    (ReferencePriceView("Seller A", 210_000),), "Fixed price: 2100 ₽",
                    technical_detail="FunPay lot ID: mock-m12\nService code: hidden from the normal UI",
                ),
            }
        if not self._sellers:
            self._sellers = [
                TrustedSellerView("SellerOne", "Mythic+", True, True),
                TrustedSellerView("SellerTwo", "Mythic+", True, True),
            ]

    def execute(self, action: str, payload: str | None = None) -> str:
        self.calls.append((action, payload))
        if action == "run_probe":
            self._probe_status = "🔍 Проверяю FunPay…\n\nНичего на FunPay не изменяется."
            return "accepted"
        if action in {"lot_automatic", "lot_fixed_price", "lot_paused", "lot_check_only"} and payload in self._lots:
            self._lots[payload] = replace(self._lots[payload], mode=action.removeprefix("lot_"))
        if action == "lot_set_fixed" and payload:
            key, _, amount = payload.partition(":")
            if key in self._lots and amount.isdecimal() and int(amount) > 0:
                self._lots[key] = replace(self._lots[key], mode="fixed_price", price_minor=int(amount) * 100)
        if action == "lot_set_floor" and payload:
            key, _, amount = payload.partition(":")
            if key in self._lots and amount.isdecimal() and int(amount) > 0:
                self._lots[key] = replace(self._lots[key], hard_floor_minor=int(amount) * 100)
        if action == "seller_add" and payload:
            nickname = payload
            if not any(item.nickname.casefold() == nickname.casefold() for item in self._sellers):
                self._sellers.append(TrustedSellerView(nickname, "Mythic+", True, True))
        if action == "seller_remove" and payload:
            self._sellers = [item for item in self._sellers if item.nickname != payload]
        if action == "seller_disable" and payload:
            self._sellers = [
                replace(item, enabled=False) if item.nickname == payload else item for item in self._sellers
            ]
        return f"mock:{action}"

    def dashboard(self, *, emergency_active: bool) -> DashboardView:
        lots = tuple(self._lots.values())
        return DashboardView(
            (
                _status("Bot", "🟢 Готов"), _status("FunPay", "⚪ Не настроен"),
                _status("Telegram", "⚪ Mock mode"), _status("Automation", "🟡 Safe mode"),
                _status("Emergency stop", "🔴 Активен" if emergency_active else "🟢 Не активен"),
            ),
            sum(item.family == "Mythic+" for item in lots),
            sum(item.warning is not None for item in lots), "Пока не выполнялось", "Пока не выполнялся", "Неизвестно",
        )

    def family(self, family: str) -> FamilyView:
        lots = self.lots(family)
        return FamilyView(
            family, len(lots), sum(item.mode == "automatic" for item in lots),
            sum(item.mode == "fixed_price" for item in lots), sum(item.mode == "paused" for item in lots),
            sum(item.warning is not None for item in lots), "Пока не выполнялось", "Пока не выполнялся",
        )

    def lots(self, family: str | None = None) -> tuple[LotView, ...]:
        return tuple(item for item in self._lots.values() if family is None or item.family == family)

    def price_overview(self) -> PriceOverviewView:
        lots = self.lots()
        return PriceOverviewView(
            sum(item.mode == "automatic" for item in lots), sum(item.mode == "fixed_price" for item in lots),
            sum(item.mode == "paused" for item in lots), sum(item.warning is not None for item in lots), "Пока не выполнялось",
        )

    def price_preview(self) -> PricePreviewView:
        from .telegram_views import PriceChangeView, PriceSkipView

        return PricePreviewView(
            (PriceChangeView("Mythic+ +10", 149_000, 142_000), PriceChangeView("Mythic+ +12", 210_000, 205_000)),
            (PriceSkipView("Mythic+ +8", "fixed price"), PriceSkipView("Mythic+ +9", "подозрительный ориентир")),
        )

    def sellers(self) -> tuple[TrustedSellerView, ...]:
        return tuple(self._sellers)

    def find_seller(self, nickname: str) -> SellerCandidateView | None:
        clean = nickname.strip()[:64]
        return SellerCandidateView(clean, True) if clean else None

    def find_sellers(self, value: str) -> SellerBatchPreviewView:
        names = tuple(line.strip() for line in value.splitlines() if line.strip())
        return SellerBatchPreviewView(names, ())

    def mapping_choices(self) -> tuple[MappingChoiceView, ...]:
        return (
            MappingChoiceView("Mythic+ +10", "EU • Self-play • x1", "Mythic+ +10"),
            MappingChoiceView("Mythic+ +10 package", "EU • Self-play • x3", "Mythic+ +10 package"),
        )

    def own_mapping_overview(self) -> OwnMappingOverviewView:
        candidates = tuple(OwnMappingCandidateView(
            item.key, item.title, " • ".join(item.attributes), "high", "candidate",
            ("critical fields: mock structured data",), (), True,
        ) for item in self._lots.values())
        return OwnMappingOverviewView(len(candidates), len(candidates), 0, 0, 0, candidates)

    def preview_own_mapping_correction(self, key: str, value: str) -> OwnMappingCandidateView:
        del value
        lot = self._lots[key]
        return OwnMappingCandidateView(
            key, lot.title, "+10 • EU • Self-play • x1", "high", "candidate",
            ("critical fields: explicitly entered by owner",), (), False,
        )

    def competitor_mapping_overview(self) -> CompetitorMappingOverviewView:
        return CompetitorMappingOverviewView(2, 2, 0, 0, self.mapping_choices())

    def minimum_price_overview(self) -> MinimumPriceOverviewView:
        return MinimumPriceOverviewView(False, 0, 0, 0, len(self._lots))

    def preview_minimum_price_batch(self, value: str) -> Mapping[int, int]:
        result: dict[int, int] = {}
        for line in value.splitlines():
            level, amount = line.replace("+", "").split()
            result[int(level)] = int(amount) * 100
        return result

    def readiness(self) -> ReadinessView:
        return ReadinessView(len(self._lots), 0, len(self._sellers), 2, 0, 0, 0, len(self._lots), False)

    def probe_status(self) -> str:
        return self._probe_status


def _status(label: str, value: str):
    from .telegram_views import StatusView

    return StatusView(label, value)


class EmergencyStopGate:
    """Persistent outbound barrier; incoming notification reads remain outside it."""

    BLOCKED = frozenset({"lot_writes", "price_writes", "raise", "auto_reply", "automated_messages", "outbound_reply"})

    def __init__(self, states: TaskStateStore) -> None:
        self.states = states

    def engage(self) -> None:
        self.states.save("emergency_stop", "active")

    def release(self) -> None:
        self.states.save("emergency_stop", "inactive")

    def active(self) -> bool:
        state = self.states.load("emergency_stop")
        return bool(state and state[0] == "active")

    def permits(self, operation: str) -> bool:
        return not self.active() or operation not in self.BLOCKED


class CompositeTelegramRouter:
    def __init__(self, *routers: TelegramInteractionRouter) -> None:
        self.routers = routers

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        for router in self.routers:
            reply = router.handle(update)
            if reply is not None:
                return reply
        return None


class TelegramControlRouter:
    """Private, button-first dashboard with opaque, per-user callbacks."""

    _TEXT_ACTIONS = {
        "⚔️ mythic+": _Intent("family", "Mythic+"), "mythic+": _Intent("family", "Mythic+"),
        "💰 цены": _Intent("prices"), "💬 сообщения": _Intent("messages"),
        "👥 продавцы": _Intent("sellers"), "trusted sellers": _Intent("sellers"),
        "📦 лоты": _Intent("lots"), "🔄 обновить и поднять": _Intent("update_raise_preview"),
        "🔍 проверить funpay": _Intent("probe"), "проверить funpay": _Intent("probe"),
        "⚙️ настройки": _Intent("settings"), "статус": _Intent("home"),
        "/start": _Intent("home"), "/status": _Intent("home"),
        "обновить цены": _Intent("price_preview"), "проверить цены": _Intent("check_prices"),
        "минимальные цены": _Intent("minimum_prices"), "готовность": _Intent("readiness"),
        "rollback": _Intent("rollback_preview"), "emergency stop": _Intent("emergency_preview"),
        "pause": _Intent("pause"), "resume": _Intent("resume_preview"),
    }

    def __init__(self, allowed_user_ids: tuple[int, ...], states: TaskStateStore,
                 services: ControlService, gate: EmergencyStopGate) -> None:
        self.allowed = frozenset(allowed_user_ids)
        self.states, self.services, self.gate = states, services, gate
        self._callbacks: dict[str, _CallbackEntry] = {}
        self._tokens_by_user: dict[int, set[str]] = {}
        self._revisions: dict[int, int] = {}
        self._input_modes: dict[int, tuple[str, str | None]] = {}
        self._next_token = 0

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        if update.user_id not in self.allowed or update.chat_id != update.user_id:
            return None
        if update.callback_data is not None:
            if update.callback_data == "setup:funpay":
                return CommandReply(
                    "Как восстановить FunPay\n\n"
                    "Откройте локальный setup wizard и выберите FunPay. "
                    "Данные сессии вводятся только локально, без отображения в чате.",
                    edit_message=True,
                )
            if update.callback_data == "probe:status":
                return self._dispatch_safe(update.user_id, _Intent("probe_status"), edit=True)
            return self._callback(update.user_id, update.callback_data)
        text = (update.text or "").strip()
        if not text:
            return None
        if update.user_id in self._input_modes and not text.startswith("/"):
            return self._safe_input(update.user_id, text)
        intent = self._TEXT_ACTIONS.get(text.casefold())
        if intent is None:
            return None
        return self._dispatch_safe(update.user_id, intent, edit=False)

    def _callback(self, user_id: int, data: str) -> CommandReply | None:
        if not data.startswith("ux:"):
            return None
        token = data.removeprefix("ux:")
        if len(token) > 12 or not token.isascii() or not token.isalnum():
            return self._stale(user_id)
        entry = self._callbacks.get(token)
        if entry is None or entry.user_id != user_id or entry.revision != self._revisions.get(user_id):
            return self._stale(user_id)
        # A navigation callback always leaves text-entry mode.  Otherwise a
        # later ordinary message could be misinterpreted as stale form input.
        self._input_modes.pop(user_id, None)
        return self._dispatch_safe(user_id, entry.intent, edit=True)

    def _dispatch_safe(self, user_id: int, intent: _Intent, *, edit: bool) -> CommandReply:
        try:
            return self._dispatch(user_id, intent, edit=edit)
        except (RuntimeError, ValueError):
            return self._service_error(user_id, edit=edit)

    def _safe_input(self, user_id: int, text: str) -> CommandReply:
        try:
            return self._input(user_id, text)
        except (RuntimeError, ValueError):
            return self._service_error(user_id)

    def _input(self, user_id: int, text: str) -> CommandReply:
        mode, payload = self._input_modes.pop(user_id)
        if mode == "seller_batch":
            preview = self.services.find_sellers(text)
            rows = []
            if preview.exact:
                rows.append([("✅ Добавить точные", _Intent("confirm", "seller_add_batch"))])
            rows.append([("Исправить список", _Intent("seller_add_batch_input")), ("Отмена", _Intent("sellers"))])
            return self._render(user_id, seller_batch_preview_text(preview), rows)
        if mode == "seller_add":
            if len(text) > 64 or "\r" in text or "\n" in text:
                return self._render(user_id, "Ник должен быть одной строкой до 64 символов.", [
                    [("Попробовать снова", _Intent("seller_add_input")), ("Назад", _Intent("sellers"))],
                ])
            candidate = self.services.find_seller(text)
            if candidate is None:
                return self._render(user_id, "Не удалось найти профиль. Проверьте ник и попробуйте снова.", [
                    [("Попробовать снова", _Intent("seller_add_input")), ("Назад", _Intent("sellers"))],
                ])
            return self._render(user_id, seller_candidate_text(candidate), [
                [("Добавить для Mythic+", _Intent("confirm", f"seller_add:{candidate.nickname}"))],
                [("❌ Отмена", _Intent("sellers"))],
            ])
        if mode == "own_mapping_correction" and payload:
            preview = self.services.preview_own_mapping_correction(payload, text)
            body = (
                "⚠️ Исправление Mythic+ варианта\n\n"
                f"Предпросмотр: {preview.variant_label}\n"
                "Источник: явное подтверждение владельца.\n\n"
                "Это изменит только локальное сопоставление; FunPay не изменяется."
            )
            return self._render(user_id, body, [
                [("✅ Подтвердить", _Intent("confirm", f"own_manual:{payload}"))],
                [("Исправить", _Intent("own_mapping_correct_input", payload)),
                 ("Отмена", _Intent("own_mappings"))],
            ])
        if mode == "floor_global":
            clean = text.strip().replace(" ", "")
            if not clean or len(clean) > 16:
                raise ValueError("minimum price is invalid")
            amount = self.services.preview_minimum_price_batch(f"+1 {clean}")[1]
            return self._render(user_id, f"Минимально допустимая цена для всех Mythic+: {amount // 100} ₽", [
                [("✅ Сохранить", _Intent("confirm", f"floor_global:{clean}"))],
                [("Исправить", _Intent("floor_global_input")), ("Отмена", _Intent("minimum_prices"))],
            ])
        if mode == "floor_batch":
            values = self.services.preview_minimum_price_batch(text)
            preview = "\n".join(f"+{key} → {minor // 100} ₽" for key, minor in sorted(values.items()))
            return self._render(user_id, f"Минимальные цены\n\n{preview}", [
                [("✅ Сохранить", _Intent("confirm", "floor_key_batch"))],
                [("Исправить", _Intent("floor_batch_input")), ("Отмена", _Intent("minimum_prices"))],
            ])
        if mode == "floor_variant" and payload:
            clean = text.strip().replace(" ", "")
            if not clean or len(clean) > 16:
                raise ValueError("minimum price is invalid")
            amount = self.services.preview_minimum_price_batch(f"+1 {clean}")[1]
            return self._render(user_id, f"Минимально допустимая цена варианта: {amount // 100} ₽", [
                [("✅ Сохранить", _Intent("confirm", f"floor_variant:{payload}:{clean}"))],
                [("Исправить", _Intent("floor_variant_input", payload)), ("Отмена", _Intent("minimum_prices"))],
            ])
        if mode in {"fixed_price", "hard_floor"} and payload:
            if not 1 <= len(text) <= 8 or not text.isdecimal() or not 1 <= int(text) <= 10_000_000:
                return self._render(user_id, "Введите цену целым числом в рублях.", [
                    [("Повторить", _Intent("lot_input", f"{mode}:{payload}")), ("Назад", _Intent("lot", payload))],
                ])
            action = "lot_set_fixed" if mode == "fixed_price" else "lot_set_floor"
            self._run(action, f"{payload}:{text}")
            return self._lot_screen(user_id, payload)
        return self._stale(user_id)

    def _dispatch(self, user_id: int, intent: _Intent, *, edit: bool) -> CommandReply:
        action, payload = intent.action, intent.payload
        if action == "home":
            return self._home(user_id, edit=edit)
        if action == "family" and payload:
            return self._family_screen(user_id, payload, edit=edit)
        if action == "own_mappings":
            return self._own_mappings_screen(user_id, edit=edit)
        if action == "own_mapping_analyze":
            self._run("own_mapping_analyze")
            return self._own_mappings_screen(user_id, edit=edit)
        if action == "own_mapping_preview":
            return self._own_mapping_preview_screen(user_id, attention_only=False, edit=edit)
        if action == "own_mapping_attention":
            return self._own_mapping_preview_screen(user_id, attention_only=True, edit=edit)
        if action == "own_mapping_correct_input" and payload:
            self._input_modes[user_id] = ("own_mapping_correction", payload)
            return self._render(
                user_id,
                "Введите только исправленные параметры одной строкой.\n\nПример: +10 EU self-play x1",
                [[("Назад", _Intent("own_mapping_attention"))]], edit=edit,
            )
        if action == "lots":
            return self._lots_screen(user_id, None, edit=edit)
        if action == "family_lots" and payload:
            return self._lots_screen(user_id, payload, edit=edit)
        if action == "lot" and payload:
            return self._lot_screen(user_id, payload, edit=edit)
        if action == "prices":
            return self._prices_screen(user_id, edit=edit)
        if action == "price_preview":
            return self._price_preview_screen(user_id, edit=edit)
        if action == "check_prices":
            try:
                self._run("check_prices")
            except RuntimeError:
                return self._render(
                    user_id, "Не удалось выполнить проверку. Ничего на FunPay не изменялось.",
                    [[("Повторить", _Intent("check_prices")), ("Назад", _Intent("prices"))]], edit=edit,
                )
            return self._dry_run_screen(user_id, edit=edit)
        if action == "rollback_preview":
            return self._confirm_screen(user_id, "rollback", "↩️ Rollback", "Будут восстановлены последние подтверждённые цены.", edit=edit)
        if action == "update_raise_preview":
            return self._confirm_screen(user_id, "update_raise", "🔄 Обновить и поднять", "Сначала будут проверены цены, затем — доступность raise.", edit=edit)
        if action == "sellers":
            return self._sellers_screen(user_id, edit=edit)
        if action == "seller_add_input":
            self._input_modes[user_id] = ("seller_add", None)
            return self._render(user_id, "Введите ник продавца", [[("Назад", _Intent("sellers"))]], edit=edit)
        if action == "seller_add_batch_input":
            self._input_modes[user_id] = ("seller_batch", None)
            return self._render(
                user_id, "Отправьте nicknames одним сообщением — по одному в строке.",
                [[("Назад", _Intent("sellers"))]], edit=edit,
            )
        if action == "seller_remove_select":
            return self._seller_remove_screen(user_id, edit=edit)
        if action == "seller_disable_select":
            return self._seller_disable_screen(user_id, edit=edit)
        if action == "mappings":
            return self._competitor_mappings_screen(user_id, edit=edit)
        if action == "competitor_discover":
            self._run("competitor_discover")
            return self._competitor_mappings_screen(user_id, edit=edit)
        if action == "seller_recheck":
            return self._run_then_sellers(user_id, "seller_recheck", edit=edit)
        if action == "seller_remap" and payload:
            return self._run_then_sellers(user_id, "seller_remap", payload, edit=edit)
        if action == "seller_disable" and payload:
            return self._run_then_sellers(user_id, "seller_disable", payload, edit=edit)
        if action == "messages":
            return self._messages_screen(user_id, edit=edit)
        if action == "settings":
            return self._settings_screen(user_id, edit=edit)
        if action == "minimum_prices":
            return self._minimum_prices_screen(user_id, edit=edit)
        if action == "floor_global_input":
            self._input_modes[user_id] = ("floor_global", None)
            return self._render(user_id, "Введите общий минимум в рублях.", [[("Назад", _Intent("minimum_prices"))]], edit=edit)
        if action == "floor_batch_input":
            self._input_modes[user_id] = ("floor_batch", None)
            return self._render(
                user_id, "Введите минимумы по ключам — по одному в строке.\n\n+2 500\n+3 550\n+4 600",
                [[("Назад", _Intent("minimum_prices"))]], edit=edit,
            )
        if action == "floor_variant_select":
            return self._floor_variant_select_screen(user_id, edit=edit)
        if action == "floor_variant_input" and payload:
            self._input_modes[user_id] = ("floor_variant", payload)
            return self._render(user_id, "Введите минимум для выбранного варианта в рублях.", [[("Назад", _Intent("minimum_prices"))]], edit=edit)
        if action == "readiness":
            return self._readiness_screen(user_id, edit=edit)
        if action == "prepare_live_test":
            return self._simple_screen(
                user_id,
                "🧪 Первый live-тест\n\nСледующий этап потребует отдельного явного разрешения. Сейчас любые FunPay write-операции заблокированы.",
                _Intent("readiness"), edit=edit,
            )
        if action == "probe":
            return self._probe_screen(user_id, start=True, edit=edit)
        if action == "probe_status":
            return self._probe_screen(user_id, start=False, edit=edit)
        if action == "automation":
            return self._automation_screen(user_id, edit=edit)
        if action == "notifications":
            return self._simple_screen(user_id, "🔔 Уведомления\n\nВключаются только значимые события: новое сообщение, истёкшая сессия, критическая ошибка, ошибка проверки цены, blocked batch, rollback, raise failure и emergency stop.", _Intent("settings"), edit=edit)
        if action == "catalog":
            return self._simple_screen(user_id, "📚 Service catalog\n\nКаталог настраивается локально. Здесь будут показаны человекочитаемые услуги и шаблоны.", _Intent("settings"), edit=edit)
        if action == "diagnostics":
            return self._simple_screen(user_id, "🩺 Диагностика\n\nСостояние подключений показано на главном экране.", _Intent("settings"), edit=edit)
        if action == "about":
            return self._simple_screen(
                user_id,
                "ℹ️ FunPay Operations for World of Warcraft Mythic+\n\n"
                "Production read-only режим. Изменения FunPay не разрешены.",
                _Intent("settings"),
                edit=edit,
            )
        if action == "pause":
            self.states.save("operations", "paused")
            self._run("pause")
            return self._automation_screen(user_id, edit=edit)
        if action == "resume_preview":
            return self._confirm_screen(user_id, "resume", "▶️ Возобновить", "Автоматизация снова сможет планировать разрешённые действия.", edit=edit)
        if action == "auto_reply_toggle":
            if not self._writes_available():
                self.states.save("funpay_auto_reply", "disabled")
                return self._simple_screen(
                    user_id, "🔒 Автоответ пока заблокирован. Реальные сообщения в FunPay не отправляются.",
                    _Intent("settings"), edit=edit,
                )
            current = self.states.load("funpay_auto_reply")
            enabled = not bool(current and current[0] == "enabled")
            self.states.save("funpay_auto_reply", "enabled" if enabled else "disabled")
            self._run("auto_reply_toggle")
            return self._settings_screen(user_id, edit=edit)
        if action == "emergency_preview":
            return self._confirm_screen(
                user_id, "emergency_engage", "⚠️ Emergency Stop",
                "Будут остановлены:\n• изменение цен;\n• изменение лотов;\n• raise;\n• автоответ;\n• автоматические исходящие действия.\n\nВходящие уведомления продолжат работать.",
                confirm_label="🛑 Остановить", edit=edit,
            )
        if action == "lot_mode" and payload:
            return self._lot_mode_screen(user_id, payload, edit=edit)
        if action == "lot_input" and payload:
            mode, _, key = payload.partition(":")
            self._input_modes[user_id] = (mode, key)
            prompt = "Введите fixed price в рублях" if mode == "fixed_price" else "Введите hard floor в рублях"
            return self._render(user_id, prompt, [[("Назад", _Intent("lot", key))]], edit=edit)
        if action == "lot_check" and payload:
            return self._run_readonly(user_id, "lot_check", "Проверка лота запущена. Это только чтение.", payload, edit=edit)
        if action == "lot_decision" and payload:
            self._run("lot_decision", payload)
            return self._lot_screen(user_id, payload, edit=edit)
        if action == "lot_detail" and payload:
            lot = self._lot(payload)
            detail = lot.technical_detail or "Технические данные пока недоступны."
            return self._render(user_id, f"Подробнее\n\n{detail}", [[("Назад", _Intent("lot", payload))]], edit=edit)
        if action.startswith("lot_") and payload:
            self._run(action, payload)
            return self._lot_screen(user_id, payload, edit=edit)
        if action == "confirm" and payload:
            return self._execute_confirmed(user_id, payload, edit=edit)
        if action == "cancel":
            return self._home(user_id, edit=edit, notice="Операция отменена.")
        if action == "refresh":
            return self._run_readonly(user_id, "refresh", "Данные FunPay обновлены.", edit=edit)
        return self._stale(user_id)

    def _home(self, user_id: int, *, edit: bool, notice: str | None = None) -> CommandReply:
        text = dashboard_text(self.services.dashboard(emergency_active=self.gate.active()), emergency_active=self.gate.active())
        if notice:
            text = f"{notice}\n\n{text}"
        rows = [
            [("⚔️ Mythic+", _Intent("family", "Mythic+"))],
            [("💰 Цены", _Intent("prices")), ("💬 Сообщения", _Intent("messages"))],
            [("👥 Продавцы", _Intent("sellers")), ("📦 Лоты", _Intent("lots"))],
            [("🔍 Проверить FunPay", _Intent("probe"))],
            [("⚙️ Настройки", _Intent("settings"))],
        ]
        if self._writes_available():
            rows.insert(3, [("🔄 Обновить и поднять", _Intent("update_raise_preview"))])
        if self.gate.active():
            rows.append([("▶️ Возобновить", _Intent("resume_preview"))])
        return self._render(user_id, text, rows, edit=edit)

    def _family_screen(self, user_id: int, family: str, *, edit: bool) -> CommandReply:
        rows = [
            [("⚙️ Настроить Mythic+ лоты", _Intent("own_mappings"))],
            [("📦 Лоты", _Intent("family_lots", family)), ("💰 Проверить цены", _Intent("check_prices"))],
            [("👥 Trusted sellers", _Intent("sellers")), ("💰 Минимальные цены", _Intent("minimum_prices"))],
            [("🧪 Готовность", _Intent("readiness"))],
            _nav(),
        ]
        if self._writes_available():
            rows.insert(1, [("💸 Обновить цены", _Intent("price_preview")), ("🔄 Обновить и поднять", _Intent("update_raise_preview"))])
        return self._render(user_id, family_text(self.services.family(family)), rows, edit=edit)

    def _own_mappings_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        view = self.services.own_mapping_overview()
        rows = [[("👀 Проверить все", _Intent("own_mapping_preview"))]]
        if view.attention:
            rows.append([("⚠️ Разобрать проблемные", _Intent("own_mapping_attention"))])
        rows.extend([
            [("🔄 Перечитать и проанализировать", _Intent("own_mapping_analyze"))],
            _nav(_Intent("family", "Mythic+")),
        ])
        return self._render(user_id, own_mapping_overview_text(view), rows, edit=edit)

    def _own_mapping_preview_screen(self, user_id: int, *, attention_only: bool, edit: bool) -> CommandReply:
        view = self.services.own_mapping_overview()
        rows: list[list[tuple[str, _Intent]]] = []
        if not attention_only and view.high:
            rows.append([("✅ Подтвердить точные", _Intent("confirm", "own_high"))])
        for item in view.candidates:
            if item.status != "confirmed" and not item.bulk_confirmable:
                label = item.variant_label or item.title
                rows.append([(f"⚠️ {label[:48]}", _Intent("own_mapping_correct_input", item.key))])
        rows.append(_nav(_Intent("own_mappings")))
        return self._render(
            user_id, own_mapping_preview_text(view, attention_only=attention_only), rows, edit=edit
        )

    def _competitor_mappings_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        view = self.services.competitor_mapping_overview()
        rows: list[list[tuple[str, _Intent]]] = []
        if view.exact:
            rows.append([("✅ Подтвердить точные", _Intent("confirm", "competitor_high"))])
        rows.extend([[("🔄 Найти exact lots", _Intent("competitor_discover"))]])
        rows.extend([[(item.label[:56], _Intent("seller_remap", item.key))]
                     for item in view.choices if item.key])
        rows.append(_nav(_Intent("sellers")))
        return self._render(user_id, competitor_mapping_overview_text(view), rows, edit=edit)

    def _minimum_prices_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        return self._render(user_id, minimum_price_overview_text(self.services.minimum_price_overview()), [
            [("⚙️ Задать общий минимум", _Intent("floor_global_input"))],
            [("📋 Задать по ключам", _Intent("floor_batch_input"))],
            [("✏️ Изменить отдельный", _Intent("floor_variant_select"))],
            _nav(_Intent("family", "Mythic+")),
        ], edit=edit)

    def _floor_variant_select_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        view = self.services.own_mapping_overview()
        rows = [[(item.variant_label[:56], _Intent("floor_variant_input", item.key))]
                for item in view.candidates if item.status == "confirmed" and item.variant_label]
        rows.append(_nav(_Intent("minimum_prices")))
        return self._render(user_id, "Выберите подтверждённый Mythic+ вариант.", rows, edit=edit)

    def _dry_run_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        text = (
            "READ-ONLY: только чтение, цены не изменяются.\n\n"
            + price_preview_text(self.services.price_preview()) + "\n\n" + readiness_text(self.services.readiness())
        )
        return self._render(user_id, text, [
            [("📋 Готовность", _Intent("readiness"))],
            _nav(_Intent("prices")),
        ], edit=edit)

    def _readiness_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        return self._render(user_id, readiness_text(self.services.readiness()), [
            [("⚠️ Исправить mappings", _Intent("own_mappings"))],
            [("🧪 Подготовить первый live-тест", _Intent("prepare_live_test"))],
            _nav(_Intent("family", "Mythic+")),
        ], edit=edit)

    def _lots_screen(self, user_id: int, family: str | None, *, edit: bool) -> CommandReply:
        lots = self.services.lots(family)
        heading = f"📦 Лоты — {family}" if family else "📦 Лоты"
        rows = [[(item.title, _Intent("lot", item.key))] for item in lots]
        rows.append([("🔄 Обновить данные", _Intent("refresh"))])
        rows.append(_nav(_Intent("family", family) if family else _Intent("home")))
        return self._render(user_id, f"{heading}\n\nВыберите лот, чтобы посмотреть цену, режим и решение.", rows, edit=edit)

    def _lot_screen(self, user_id: int, key: str, *, edit: bool = True) -> CommandReply:
        lot = self._lot(key)
        if not lot.managed:
            return self._render(user_id, lot_text(lot), [
                [("Подробнее", _Intent("lot_detail", key))],
                _nav(_Intent("lots")),
            ], edit=edit)
        return self._render(user_id, lot_text(lot), [
            [("Режим (локально)", _Intent("lot_mode", key)), ("Fixed price (локально)", _Intent("lot_input", f"fixed_price:{key}"))],
            [("Минимальная цена", _Intent("lot_input", f"hard_floor:{key}")), ("Сбросить минимум", _Intent("lot_clear_floor", key))],
            [("Проверить сейчас", _Intent("lot_check", key))],
            [("Решение по цене", _Intent("lot_decision", key)), ("Подробнее", _Intent("lot_detail", key))],
            [("▶️ Resume" if lot.mode == "paused" else "⏸ Pause", _Intent("lot_automatic" if lot.mode == "paused" else "lot_paused", key))],
            _nav(_Intent("family_lots", lot.family)),
        ], edit=edit)

    def _lot_mode_screen(self, user_id: int, key: str, *, edit: bool) -> CommandReply:
        return self._render(user_id, "Выберите режим лота. Изменение будет применено только к этому лоту.", [
            [("🟢 Automatic (локально)", _Intent("lot_automatic", key)),
             ("🔵 Fixed price (локально)", _Intent("lot_input", f"fixed_price:{key}"))],
            [("⏸ Paused (локально)", _Intent("lot_paused", key)),
             ("🟡 Check only", _Intent("lot_check_only", key))],
            _nav(_Intent("lot", key)),
        ], edit=edit)

    def _prices_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        rows = [
            [("Проверить сейчас", _Intent("check_prices")), ("Решения по лотам", _Intent("lots"))],
            [("Минимальные цены", _Intent("minimum_prices")), ("Готовность", _Intent("readiness"))],
        ]
        if self._writes_available():
            rows.extend([
                [("Обновить цены", _Intent("price_preview")), ("Rollback", _Intent("rollback_preview"))],
            ])
        rows.append(_nav())
        return self._render(user_id, price_overview_text(self.services.price_overview()), rows, edit=edit)

    def _price_preview_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        rows = []
        if self._writes_available():
            rows.append([("✅ Подтвердить", _Intent("confirm", "mass_price_update")), ("❌ Отмена", _Intent("cancel"))])
        rows.append(_nav(_Intent("prices")))
        return self._render(user_id, price_preview_text(self.services.price_preview()), rows, edit=edit)

    def _sellers_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        return self._render(user_id, sellers_text(self.services.sellers()), [
            [("Добавить", _Intent("seller_add_input")), ("📋 Добавить списком", _Intent("seller_add_batch_input"))],
            [("Удалить", _Intent("seller_remove_select"))],
            [("Отключить", _Intent("seller_disable_select")), ("Проверить", _Intent("seller_recheck"))],
            [("Соответствия", _Intent("mappings"))],
            _nav(),
        ], edit=edit)

    def _seller_remove_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        rows = [[(item.nickname, _Intent("confirm", f"seller_remove:{item.nickname}"))] for item in self.services.sellers()]
        rows.append(_nav(_Intent("sellers")))
        return self._render(user_id, "Выберите продавца для удаления.", rows, edit=edit)

    def _seller_disable_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        rows = [[(item.nickname, _Intent("seller_disable", item.nickname))] for item in self.services.sellers() if item.enabled]
        rows.append(_nav(_Intent("sellers")))
        return self._render(user_id, "Выберите продавца для отключения.", rows, edit=edit)

    def _mappings_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        choices = self.services.mapping_choices()
        rows = [[(item.label, _Intent("seller_remap", item.label))] for item in choices]
        rows.append(_nav(_Intent("sellers")))
        return self._render(user_id, mappings_text(choices), rows, edit=edit)

    def _messages_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        reply_hint = ("Нажмите «Ответить» прямо под нужным уведомлением." if self._writes_available()
                      else "🔒 Ответы в FunPay пока заблокированы; входящие уведомления продолжают работать.")
        return self._render(user_id, f"💬 Сообщения\n\nНовые сообщения приходят отдельными компактными уведомлениями. {reply_hint}", [
            [("🏠 Главная", _Intent("home"))],
        ], edit=edit)

    def _settings_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        auto_reply = self.states.load("funpay_auto_reply")
        text = settings_text(bool(auto_reply and auto_reply[0] == "enabled"))
        return self._render(user_id, text, [
            [("Автоматизация" if self._writes_available() else "Автоматизация 🔒", _Intent("automation")),
             ("Автоответ" if self._writes_available() else "Автоответ 🔒", _Intent("auto_reply_toggle"))],
            [("Уведомления", _Intent("notifications")), ("Каталог услуг", _Intent("catalog"))],
            [("Минимальные цены", _Intent("minimum_prices")), ("Диагностика", _Intent("diagnostics"))],
            [("🔍 Проверить FunPay", _Intent("probe"))],
            [("Информация о боте", _Intent("about"))],
            [("⚠️ Emergency stop", _Intent("emergency_preview"))],
            _nav(),
        ], edit=edit)

    def _probe_screen(self, user_id: int, *, start: bool, edit: bool) -> CommandReply:
        notice = ""
        if start:
            outcome = self._run("run_probe")
            notice = {
                "accepted": "🔍 Проверяю FunPay…\n\n",
                "already_running": "🔍 Проверка уже выполняется.\n\n",
                "rate_limited": "⏳ Проверка запускалась недавно. Попробуйте позже.\n\n",
            }.get(outcome, "")
        return self._render(
            user_id,
            notice + self.services.probe_status(),
            [
                [("Обновить статус", _Intent("probe_status"))],
                _nav(_Intent("settings")),
            ],
            edit=edit,
        )

    def _automation_screen(self, user_id: int, *, edit: bool) -> CommandReply:
        if not self._writes_available():
            return self._render(
                user_id,
                "⚙️ Автоматизация\n\n🔒 Внешние изменения FunPay отключены. Работают только чтение и локальные настройки.",
                [_nav(_Intent("settings"))],
                edit=edit,
            )
        state = self.states.load("operations")
        paused = bool(state and state[0] == "paused")
        text = f"⚙️ Автоматизация\n\nСостояние: {'⏸ Приостановлена' if paused else '🟢 Готова'}"
        rows = [[("▶️ Resume", _Intent("resume_preview")) if paused else ("⏸ Pause", _Intent("pause"))], _nav(_Intent("settings"))]
        return self._render(user_id, text, rows, edit=edit)

    def _simple_screen(self, user_id: int, text: str, back: _Intent, *, edit: bool) -> CommandReply:
        return self._render(user_id, text, [_nav(back)], edit=edit)

    def _service_error(self, user_id: int, *, edit: bool = False) -> CommandReply:
        return self._render(
            user_id, "Не удалось выполнить действие. Ничего не подтверждено как изменённое.",
            [[("Обновить", _Intent("refresh"))]], edit=edit,
        )

    def _run_then_sellers(self, user_id: int, action: str, payload: str | None = None, *, edit: bool) -> CommandReply:
        try:
            self._run(action, payload)
        except RuntimeError:
            return self._render(user_id, "Не удалось выполнить действие. Попробуйте обновить экран позже.", [[("Обновить", _Intent("sellers"))]], edit=edit)
        return self._sellers_screen(user_id, edit=edit)

    def _confirm_screen(self, user_id: int, action: str, title: str, body: str, *,
                        confirm_label: str = "✅ Подтвердить", edit: bool) -> CommandReply:
        return self._render(user_id, f"{title}\n\n{body}\n\nПосле подтверждения будет выполнена операция.", [
            [(confirm_label, _Intent("confirm", action)), ("Отмена", _Intent("cancel"))],
            _nav(),
        ], edit=edit)

    def _execute_confirmed(self, user_id: int, action: str, *, edit: bool) -> CommandReply:
        if action == "emergency_engage":
            self.gate.engage()
            return self._home(user_id, edit=edit, notice="🛑 Emergency stop активирован. Исходящие автоматические действия заблокированы.")
        if action == "resume":
            self.gate.release()
            self.states.save("operations", "active")
            self._run("resume")
            return self._home(user_id, edit=edit, notice="▶️ Automation resumed.")
        if action.startswith("seller_add:"):
            nickname = action.partition(":")[2]
            self._run("seller_add", nickname)
            return self._sellers_screen(user_id, edit=edit)
        if action == "seller_add_batch":
            self._run("seller_add_batch")
            return self._competitor_mappings_screen(user_id, edit=edit)
        if action.startswith("seller_remove:"):
            nickname = action.partition(":")[2]
            self._run("seller_remove", nickname)
            return self._sellers_screen(user_id, edit=edit)
        if action == "own_high":
            self._run("confirm_own_high")
            return self._own_mappings_screen(user_id, edit=edit)
        if action.startswith("own_manual:"):
            key = action.partition(":")[2]
            self._run("confirm_own_manual", key)
            return self._own_mappings_screen(user_id, edit=edit)
        if action == "competitor_high":
            self._run("confirm_competitor_high")
            return self._competitor_mappings_screen(user_id, edit=edit)
        if action.startswith("floor_global:"):
            self._run("floor_set_global", action.partition(":")[2])
            return self._minimum_prices_screen(user_id, edit=edit)
        if action == "floor_key_batch":
            self._run("floor_set_key_batch")
            return self._minimum_prices_screen(user_id, edit=edit)
        if action.startswith("floor_variant:"):
            _, _, remainder = action.partition(":")
            key, separator, amount = remainder.partition(":")
            if not separator:
                raise RuntimeError("minimum-price confirmation is stale")
            self._run("floor_set_variant", f"{key}:{amount}")
            return self._minimum_prices_screen(user_id, edit=edit)
        operation = _gate_operation(action)
        if action in {"mass_price_update", "update_raise", "rollback", "mass_lot_sync", "disable_lots"} and not self._writes_available():
            return self._render(
                user_id, "🔒 Реальные изменения FunPay пока не разрешены.\n\n"
                "Сначала будет отдельная проверка и подтверждение первого live-действия.",
                [[("🏠 Главная", _Intent("home"))]], edit=edit,
            )
        if not self.gate.permits(operation):
            return self._render(user_id, "🛑 Emergency stop блокирует это исходящее действие.", [[("🏠 Главная", _Intent("home"))]], edit=edit)
        labels = {
            "mass_price_update": "✅ Обновление цен передано на выполнение.",
            "update_raise": "✅ Обновление и raise переданы на выполнение.",
            "rollback": "✅ Rollback передан на выполнение.",
            "mass_lot_sync": "✅ Синхронизация лотов передана на выполнение.",
            "disable_lots": "✅ Отключение лотов передано на выполнение.",
        }
        self._run(action)
        return self._home(user_id, edit=edit, notice=labels.get(action, "Операция выполнена."))

    def _run_readonly(self, user_id: int, action: str, notice: str, payload: str | None = None, *, edit: bool) -> CommandReply:
        try:
            self._run(action, payload)
        except RuntimeError:
            return self._render(user_id, "Не удалось выполнить проверку. Попробуйте обновить экран позже.", [[("Обновить", _Intent("refresh"))]], edit=edit)
        return self._home(user_id, edit=edit, notice=f"✅ {notice}")

    def _run(self, action: str, payload: str | None = None) -> str:
        try:
            return self.services.execute(action, payload)
        except Exception as error:
            raise RuntimeError("control service failed") from error

    def _render(self, user_id: int, text: str, rows: list[list[tuple[str, _Intent]]], *,
                edit: bool = False, show_menu: bool = False) -> CommandReply:
        revision = self._revisions.get(user_id, 0) + 1
        self._revisions[user_id] = revision
        for token in self._tokens_by_user.pop(user_id, set()):
            self._callbacks.pop(token, None)
        keyboard: list[list[dict[str, str]]] = []
        tokens: set[str] = set()
        for row in rows:
            rendered_row: list[dict[str, str]] = []
            for label, intent in row:
                token = self._new_token()
                tokens.add(token)
                self._callbacks[token] = _CallbackEntry(user_id, revision, intent)
                rendered_row.append({"text": label, "callback_data": f"ux:{token}"})
            keyboard.append(rendered_row)
        self._tokens_by_user[user_id] = tokens
        markup = CONTROL_MENU if show_menu else {"inline_keyboard": keyboard}
        return CommandReply(text, show_menu=show_menu, reply_markup=markup, edit_message=edit)

    def _stale(self, user_id: int) -> CommandReply:
        return self._render(
            user_id, "Этот экран устарел. Нажмите «Обновить», чтобы получить актуальное состояние.",
            [[("Обновить", _Intent("refresh"))]], edit=True,
        )

    def _lot(self, key: str) -> LotView:
        for item in self.services.lots():
            if item.key == key:
                return item
        raise RuntimeError("lot is unavailable")

    def _new_token(self) -> str:
        self._next_token += 1
        return format(self._next_token, "x")

    def _writes_available(self) -> bool:
        return bool(getattr(self.services, "external_mutations_allowed", True))


def _nav(back: _Intent | None = None) -> list[tuple[str, _Intent]]:
    return [("Назад", back or _Intent("home")), ("🏠 Главная", _Intent("home"))]


def _gate_operation(action: str) -> str:
    return {
        "mass_lot_sync": "lot_writes", "mass_price_update": "price_writes", "update_raise": "raise",
        "rollback": "price_writes", "disable_lots": "lot_writes",
    }.get(action, "automated_messages")
