"""Local session-expiry safety state without retaining any credential value."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


SESSION_EXPIRED_TEXT = (
    "🔴 Требуется повторная авторизация FunPay\n\n"
    "Сессия больше не действует.\n"
    "Автоматические изменения остановлены."
)
SESSION_EXPIRED_MARKUP = {
    "inline_keyboard": [[{"text": "Как восстановить", "callback_data": "setup:funpay"}]],
}


class SessionStateStore(Protocol):
    def save(self, task_name: str, state: str, cursor: str | None = None, last_error: str | None = None) -> None: ...

    def load(self, task_name: str) -> tuple[str, str | None] | None: ...


@dataclass
class FunPaySessionGuard:
    """Fail closed after an expired session until the local DPAPI file changes.

    A file timestamp is only a non-secret change marker.  The stored session is
    never read, copied, logged, or exposed by this object.
    """

    states: SessionStateStore
    secret_store_path: Path
    _expired_marker: str | None = None

    def __post_init__(self) -> None:
        saved = self.states.load("funpay_session")
        if saved is not None and saved[0] == "expired":
            self._expired_marker = saved[1] or self._marker()

    def permits(self, _operation: str) -> bool:
        """Block every outbound FunPay action while the session is expired."""

        return not self.is_expired

    @property
    def is_expired(self) -> bool:
        return self._expired_marker is not None

    def allows_polling(self) -> bool:
        """Permit exactly one reconnect attempt only after local reconfiguration."""

        if not self.is_expired:
            return True
        if self._marker() != self._expired_marker:
            self.states.save("funpay_session", "reconnecting", self._marker())
            return True
        return False

    def mark_expired(self) -> bool:
        """Persist expiry and return true only for the first notification."""

        if self.is_expired:
            # A user may have replaced an invalid local session.  Record its
            # new marker so that it receives one reconnect attempt, not an
            # unbounded network retry loop.
            if self._marker() != self._expired_marker:
                self._expired_marker = self._marker()
                self.states.save("funpay_session", "expired", self._expired_marker, "session_expired")
            return False
        self._expired_marker = self._marker()
        self.states.save("funpay_session", "expired", self._expired_marker, "session_expired")
        return True

    def mark_authorized(self) -> None:
        if self.is_expired:
            self._expired_marker = None
            self.states.save("funpay_session", "authorized")

    def _marker(self) -> str:
        try:
            return str(self.secret_store_path.stat().st_mtime_ns)
        except OSError:
            return "missing"
