"""Presentation-only view models for the compact Telegram control panel.

The classes in this module contain no network, database, pricing, or lot-write
logic.  A service adapter supplies the models; the Telegram router only turns
them into human-readable text and opaque button intents.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusView:
    label: str
    value: str


@dataclass(frozen=True)
class DashboardView:
    statuses: tuple[StatusView, ...]
    mythic_lots: int
    attention_lots: int
    last_price_update: str
    last_raise: str
    next_raise: str
    last_funpay_read: str = "Пока не проверено"
    unknown_lots: int = 0
    ambiguous_lots: int = 0


@dataclass(frozen=True)
class FamilyView:
    family: str
    managed_lots: int
    automatic: int
    fixed: int
    paused: int
    blocked: int
    last_price_update: str
    last_raise: str


@dataclass(frozen=True)
class ReferencePriceView:
    seller_name: str
    price_minor: int
    currency: str = "RUB"


@dataclass(frozen=True)
class LotView:
    key: str
    family: str
    title: str
    attributes: tuple[str, ...]
    price_minor: int
    mode: str
    hard_floor_minor: int | None
    references: tuple[ReferencePriceView, ...]
    calculation: str
    warning: str | None = None
    technical_detail: str | None = None
    managed: bool = True


@dataclass(frozen=True)
class PriceOverviewView:
    automatic: int
    fixed: int
    paused: int
    blocked: int
    last_update: str


@dataclass(frozen=True)
class PriceChangeView:
    lot_title: str
    current_minor: int
    target_minor: int
    currency: str = "RUB"


@dataclass(frozen=True)
class PriceSkipView:
    lot_title: str
    reason: str


@dataclass(frozen=True)
class PricePreviewView:
    changes: tuple[PriceChangeView, ...]
    skipped: tuple[PriceSkipView, ...]


@dataclass(frozen=True)
class TrustedSellerView:
    nickname: str
    family: str
    enabled: bool
    verified: bool


@dataclass(frozen=True)
class SellerCandidateView:
    nickname: str
    verified: bool


@dataclass(frozen=True)
class MappingChoiceView:
    label: str
    details: str


def rubles(minor: int, currency: str = "RUB") -> str:
    """Format integer minor units without using floating point."""

    if currency == "RUB":
        return f"{minor // 100} ₽"
    return f"{minor // 100} {currency}"


def dashboard_text(view: DashboardView, *, emergency_active: bool) -> str:
    status_lines = "\n".join(f"• {item.label}: {item.value}" for item in view.statuses)
    emergency = "\n\n🔴 AUTOMATION STOPPED" if emergency_active else ""
    return (
        "🤖 FunPay Operations for World of Warcraft Mythic+\n\n"
        f"Статусы:\n{status_lines}{emergency}\n\n"
        "Мои лоты:\n"
        f"• Управляемые Mythic+ лоты: {view.mythic_lots}\n"
        f"• Не управляется ботом: {view.unknown_lots}\n"
        f"• Неоднозначно: {view.ambiguous_lots}\n"
        f"• Требуют внимания: {view.attention_lots}\n"
        f"• Последнее чтение FunPay: {view.last_funpay_read}\n"
        f"• Последнее обновление цен: {view.last_price_update}\n"
        f"• Последний raise: {view.last_raise}\n"
        f"• Следующий raise: {view.next_raise}"
    )


def family_text(view: FamilyView) -> str:
    return (
        f"{_family_icon(view.family)} {view.family}\n\n"
        f"Управляемые лоты: {view.managed_lots}\n"
        f"🟢 Automatic: {view.automatic}\n"
        f"🔵 Fixed price: {view.fixed}\n"
        f"⏸ Paused: {view.paused}\n"
        f"⚠️ Требуют внимания: {view.blocked}\n\n"
        f"Последнее обновление цен: {view.last_price_update}\n"
        f"Последний raise: {view.last_raise}"
    )


def lot_text(view: LotView) -> str:
    references = "\n".join(
        f"{item.seller_name} — {rubles(item.price_minor, item.currency)}" for item in view.references
    ) or "Нет подтверждённых ориентиров"
    warning = f"\n\n⚠️ {view.warning}" if view.warning else ""
    return (
        f"{view.title}\n"
        f"{' • '.join(view.attributes)}\n\n"
        f"Цена: {rubles(view.price_minor)}\n"
        f"Режим: {_mode_label(view.mode)}\n"
        f"Минимально допустимая цена: {_minimum_price(view.hard_floor_minor)}\n\n"
        f"Ориентир:\n{references}\n\n"
        f"Расчёт:\n{view.calculation}{warning}"
    )


def price_overview_text(view: PriceOverviewView) -> str:
    return (
        "💰 Цены\n\n"
        f"🟢 Automatic: {view.automatic}\n"
        f"🔵 Fixed price: {view.fixed}\n"
        f"⏸ Paused: {view.paused}\n"
        f"⚠️ Blocked: {view.blocked}\n\n"
        f"Последнее обновление: {view.last_update}"
    )


def price_preview_text(view: PricePreviewView) -> str:
    changes = "\n".join(
        f"{item.lot_title}\n{rubles(item.current_minor, item.currency)} → {rubles(item.target_minor, item.currency)}"
        for item in view.changes
    ) or "Нет изменений."
    skipped = "\n".join(f"{item.lot_title} — {item.reason}" for item in view.skipped) or "Нет"
    return f"💰 Предпросмотр обновления\n\nИзменения:\n{changes}\n\nПропущено:\n{skipped}"


def sellers_text(sellers: tuple[TrustedSellerView, ...]) -> str:
    lines = ["👥 Доверенные продавцы", "", "Mythic+"]
    lines.extend(
        f"• {seller.nickname} {'🟢' if seller.enabled and seller.verified else '⚪'}" for seller in sellers
    )
    if not sellers:
        lines.append("• Пока нет")
    return "\n".join(lines).rstrip()


def seller_candidate_text(view: SellerCandidateView) -> str:
    verification = "FunPay profile verified" if view.verified else "Профиль требует проверки"
    return f"Найден:\n{view.nickname}\n{verification}"


def mappings_text(choices: tuple[MappingChoiceView, ...]) -> str:
    listed = "\n".join(f"• {item.label}\n  {item.details}" for item in choices)
    return f"Соответствия лотов\n\nНужно выбрать точное соответствие:\n{listed}"


def settings_text(auto_reply_enabled: bool) -> str:
    enabled = "🔴 Выключен" if not auto_reply_enabled else "🔒 Включение заблокировано"
    return (
        "⚙️ Настройки\n\n"
        f"Автоответ: {enabled}\n"
        "Текст: Привет\n"
        "Отправляется максимум один раз в новом диалоге."
    )


def _minimum_price(value: int | None) -> str:
    return rubles(value) if value is not None else "⚠️ Не настроена"


def _mode_label(mode: str) -> str:
    return {
        "automatic": "🟢 Automatic",
        "fixed_price": "🔵 Fixed price",
        "paused": "⏸ Paused",
        "check_only": "🟡 Check only",
    }.get(mode, "⚪ Неизвестно")


def _family_icon(family: str) -> str:
    return "⚔️"
