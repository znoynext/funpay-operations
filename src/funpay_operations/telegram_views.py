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
    key: str | None = None


@dataclass(frozen=True)
class OwnMappingCandidateView:
    key: str
    title: str
    variant_label: str
    confidence: str
    status: str
    evidence: tuple[str, ...]
    issues: tuple[str, ...]
    bulk_confirmable: bool


@dataclass(frozen=True)
class OwnMappingOverviewView:
    total: int
    high: int
    attention: int
    excluded: int
    confirmed: int
    candidates: tuple[OwnMappingCandidateView, ...]


@dataclass(frozen=True)
class SellerBatchPreviewView:
    exact: tuple[str, ...]
    attention: tuple[str, ...]


@dataclass(frozen=True)
class CompetitorMappingOverviewView:
    checked_variants: int
    exact: int
    attention: int
    no_match: int
    choices: tuple[MappingChoiceView, ...]


@dataclass(frozen=True)
class MinimumPriceOverviewView:
    global_configured: bool
    per_key_count: int
    variant_count: int
    covered_lots: int
    confirmed_lots: int


@dataclass(frozen=True)
class ReadinessView:
    confirmed_lots: int
    mapping_attention: int
    trusted_sellers: int
    confirmed_competitor_mappings: int
    competitor_attention: int
    minimum_prices_covered: int
    dry_run_ready: int
    dry_run_blocked: int
    live_enabled: bool = False


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


def own_mapping_overview_text(view: OwnMappingOverviewView) -> str:
    lines = [
        "⚔️ Настройка Mythic+ лотов", "", f"Найдено: {view.total}",
        f"✅ Уверенно распознано: {view.high}", f"⚠️ Требуют проверки: {view.attention}",
        f"❌ Не Mythic+/не управлять: {view.excluded}", f"🔒 Уже подтверждено: {view.confirmed}", "",
        "Перед подтверждением проверьте массовый preview.",
    ]
    return "\n".join(lines)


def own_mapping_preview_text(view: OwnMappingOverviewView, *, attention_only: bool = False) -> str:
    selected = tuple(
        item for item in view.candidates
        if (not attention_only or (not item.bulk_confirmable and item.status != "confirmed"))
    )
    lines = ["📦 Mythic+ — предварительное сопоставление", ""]
    for index, item in enumerate(selected, start=1):
        icon = "✅" if item.bulk_confirmable else "🔒" if item.status == "confirmed" else "⚠️"
        detail = item.variant_label if item.variant_label else "Критические параметры не определены"
        lines.append(f"{index}. {detail} {icon}")
        if item.issues:
            lines.append(f"   Причина: {', '.join(item.issues)}")
    if not selected:
        lines.append("Нет лотов в этой группе.")
    lines.extend(("", "Raw FunPay IDs скрыты. Все подтверждения сохраняются только локально."))
    return "\n".join(lines)


def seller_batch_preview_text(view: SellerBatchPreviewView) -> str:
    lines = ["👥 Найдено продавцов", ""]
    lines.extend(f"✅ {nickname}" for nickname in view.exact)
    lines.extend(f"⚠️ {reason}" for reason in view.attention)
    if not view.exact:
        lines.append("Нет однозначно найденных профилей.")
    return "\n".join(lines)


def competitor_mapping_overview_text(view: CompetitorMappingOverviewView) -> str:
    return (
        "🔗 Соответствия конкурентов\n\n"
        f"Проверено вариантов: {view.checked_variants}\n"
        f"✅ Точных соответствий: {view.exact}\n"
        f"⚠️ Неоднозначных/несовместимых: {view.attention}\n"
        f"❌ Не найдено: {view.no_match}\n\n"
        "Нужно выбрать точное соответствие; неоднозначные варианты не угадываются.\n"
        "До подтверждения ни одно соответствие не используется для pricing."
    )


def minimum_price_overview_text(view: MinimumPriceOverviewView) -> str:
    return (
        "💰 Минимальные цены\n\n"
        f"Общий минимум: {'✅ задан' if view.global_configured else '⚪ не задан'}\n"
        f"По ключам: {view.per_key_count}\n"
        f"Отдельные варианты: {view.variant_count}\n"
        f"Покрыто подтверждённых лотов: {view.covered_lots}/{view.confirmed_lots}\n\n"
        "Бот никогда не предложит цену ниже указанного значения. Значения хранятся только локально."
    )


def readiness_text(view: ReadinessView) -> str:
    return (
        "🧪 Готовность Mythic+\n\n"
        f"Лоты:\n✅ Confirmed: {view.confirmed_lots}\n⚠️ Mapping issues: {view.mapping_attention}\n\n"
        f"Trusted sellers:\n✅ {view.trusted_sellers}\n\n"
        f"Competitor mappings:\n✅ {view.confirmed_competitor_mappings}\n"
        f"⚠️ {view.competitor_attention} требуют внимания\n\n"
        f"Minimum prices:\n✅ {view.minimum_prices_covered}/{view.confirmed_lots}\n\n"
        f"Pricing dry-run:\n✅ Готово: {view.dry_run_ready}\n"
        f"⚠️ Требует внимания: {view.dry_run_blocked}\n\n"
        f"LIVE:\n{'🟢 Включен' if view.live_enabled else '🔒 Выключен'}"
    )


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
