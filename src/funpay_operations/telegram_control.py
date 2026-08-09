"""Mock-ready Telegram control router with explicit confirmation barriers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from .telegram import CommandReply, TelegramInteractionRouter, TelegramUpdate, TaskStateStore


CONTROL_MENU = {
    "keyboard": [
        ["Статус"], ["Mythic+", "Delves"], ["Сообщения"], ["Проверить цены", "Обновить цены"],
        ["Обновить и поднять"], ["Trusted sellers", "Лоты"], ["Rollback"], ["Pause", "Resume"],
        ["Emergency stop"],
    ], "resize_keyboard": True, "is_persistent": True,
}

_ACTIONS = {
    "Статус": "status", "Mythic+": "mythic_plus", "Delves": "delves", "Сообщения": "messages",
    "Проверить цены": "check_prices", "Обновить цены": "update_prices", "Обновить и поднять": "update_raise",
    "Trusted sellers": "trusted_sellers", "Лоты": "lots", "Rollback": "rollback", "Pause": "pause",
    "Resume": "resume", "Emergency stop": "emergency_stop",
}
_CONFIRMATION_ACTIONS = {"mass_lot_sync", "mass_price_update", "rollback", "update_raise", "disable_lots"}


class ControlService(Protocol):
    def execute(self, action: str, payload: str | None = None) -> str: ...


@dataclass
class MockControlService:
    """In-memory service-layer double; it never invokes external adapters."""

    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def execute(self, action: str, payload: str | None = None) -> str:
        self.calls.append((action, payload))
        return f"mock:{action}"


class EmergencyStopGate:
    """Persistent outbound barrier; incoming notification reads remain outside it."""

    BLOCKED = frozenset({"lot_writes", "price_writes", "raise", "auto_reply", "automated_messages", "outbound_reply"})

    def __init__(self, states: TaskStateStore) -> None:
        self.states = states

    def engage(self) -> None:
        self.states.save("emergency_stop", "active")

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
    """Private menu control; writes require a callback confirmation and gate check."""

    def __init__(self, allowed_user_ids: tuple[int, ...], states: TaskStateStore,
                 services: ControlService, gate: EmergencyStopGate) -> None:
        self.allowed = frozenset(allowed_user_ids)
        self.states, self.services, self.gate = states, services, gate
        self.pending: dict[str, str] = {}

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        if update.user_id not in self.allowed or update.chat_id != update.user_id:
            return None
        if update.callback_data:
            return self._callback(update.callback_data)
        action = _ACTIONS.get((update.text or "").strip())
        if action is None:
            return None
        if action == "emergency_stop":
            self.gate.engage()
            return CommandReply("Emergency stop active: outbound automation is blocked.", show_menu=True, reply_markup=CONTROL_MENU)
        if action == "pause":
            self.states.save("operations", "paused")
            return CommandReply("Operations paused.", show_menu=True, reply_markup=CONTROL_MENU)
        if action == "resume":
            self.states.save("operations", "active")
            return CommandReply("Operations resumed; emergency stop remains independent.", show_menu=True, reply_markup=CONTROL_MENU)
        if action == "status":
            return CommandReply(self.services.execute("status"), show_menu=True, reply_markup=CONTROL_MENU)
        if action == "update_prices":
            return self._confirm("mass_price_update")
        if action == "update_raise":
            return self._confirm("update_raise")
        if action == "rollback":
            return self._confirm("rollback")
        if action == "lots":
            return self._confirm("mass_lot_sync")
        return CommandReply(self.services.execute(action), show_menu=True, reply_markup=CONTROL_MENU)

    def _confirm(self, action: str) -> CommandReply:
        token = uuid4().hex
        self.pending[token] = action
        return CommandReply(
            f"Confirm {action}?", reply_markup={"inline_keyboard": [[
                {"text": "Confirm", "callback_data": f"control_confirm:{token}"},
                {"text": "Cancel", "callback_data": f"control_cancel:{token}"},
            ]]},
        )

    def _callback(self, data: str) -> CommandReply | None:
        action, separator, token = data.partition(":")
        if action == "control_action" and separator:
            if token == "disable_lots":
                return self._confirm("disable_lots")
            if token in {
                "seller_add", "seller_remove", "seller_disable", "seller_recheck", "seller_remap",
                "lot_automatic", "lot_fixed_price", "lot_paused", "lot_check_only", "lot_hard_floor", "lot_decision",
            }:
                return CommandReply(self.services.execute(token), show_menu=True, reply_markup=CONTROL_MENU)
            return CommandReply("Control is unavailable.")
        if not separator or not token or action not in {"control_confirm", "control_cancel"}:
            return None
        pending = self.pending.pop(token, None)
        if pending is None:
            return CommandReply("Confirmation is unavailable.")
        if action == "control_cancel":
            return CommandReply("Operation cancelled.", show_menu=True, reply_markup=CONTROL_MENU)
        operation = _gate_operation(pending)
        if not self.gate.permits(operation):
            return CommandReply("Emergency stop blocks this outbound operation.", show_menu=True, reply_markup=CONTROL_MENU)
        return CommandReply(self.services.execute(pending), show_menu=True, reply_markup=CONTROL_MENU)


def _gate_operation(action: str) -> str:
    return {
        "mass_lot_sync": "lot_writes", "mass_price_update": "price_writes", "update_raise": "raise",
        "rollback": "price_writes", "disable_lots": "lot_writes",
    }[action]
