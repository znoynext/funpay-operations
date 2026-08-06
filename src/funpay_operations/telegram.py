"""Telegram integration boundary; transport is intentionally not implemented."""

from __future__ import annotations

from dataclasses import dataclass

from .funpay import RealOperationsDisabled


@dataclass(frozen=True)
class TelegramClient:
    token_key: str
    allowed_user_ids: tuple[int, ...]
    operations_enabled: bool = False

    def require_explicit_send(self, recipient_id: int) -> None:
        if recipient_id not in self.allowed_user_ids:
            raise PermissionError("Telegram recipient is not allowlisted")
        if not self.operations_enabled:
            raise RealOperationsDisabled("Telegram sending is disabled by configuration")
        raise NotImplementedError("Telegram transport requires explicit owner-approved implementation")
