"""FunPay integration boundary; real operations are deliberately disabled."""

from __future__ import annotations

from dataclasses import dataclass


class RealOperationsDisabled(RuntimeError):
    """Raised before any real FunPay or Telegram action can be attempted."""


@dataclass(frozen=True)
class FunPayClient:
    credential_key: str
    operations_enabled: bool = False

    def require_explicit_operation(self, operation: str) -> None:
        if not self.operations_enabled:
            raise RealOperationsDisabled(f"FunPay operation '{operation}' is disabled by configuration")
        raise NotImplementedError("Real FunPay operations require explicit owner-approved implementation")
