"""Allow the confirmed Telegram owner to request one local auth window."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .telegram import CommandReply, TelegramUpdate
from .windows_infra import WindowsPaths, WindowsSetupError, installed_binaries


class TaskStateStore(Protocol):
    def save(self, task_name: str, state: str, cursor: str | None = None, last_error: str | None = None) -> None: ...

    def load(self, task_name: str) -> tuple[str, str | None] | None: ...


@dataclass(frozen=True)
class AuthLaunchResult:
    started: bool
    message: str


class LocalFunPayAuthRequest:
    """Starts only the installed Setup Center with a fixed local argument."""

    def __init__(
        self, paths: WindowsPaths, states: TaskStateStore, *,
        launcher: Callable[..., object] = subprocess.Popen,
        interactive_available: Callable[[], bool] | None = None,
        now: Callable[[], float] = time.time,
        cooldown_seconds: int = 20,
    ) -> None:
        self.paths, self.states, self._launcher = paths, states, launcher
        self._interactive_available = interactive_available or _interactive_desktop_available
        self._now, self._cooldown_seconds = now, cooldown_seconds

    def request(self) -> AuthLaunchResult:
        if not self._interactive_available():
            return AuthLaunchResult(False, "Окно авторизации будет доступно на компьютере после входа в Windows.")
        previous = self.states.load("funpay_auth_window")
        if previous is not None and previous[1] is not None:
            try:
                if self._now() - float(previous[1]) < self._cooldown_seconds:
                    return AuthLaunchResult(False, "Окно авторизации уже открывается. Проверьте компьютер.")
            except ValueError:
                pass
        try:
            _, _, setup = installed_binaries(self.paths)
            self._launcher(
                [str(setup), "--funpay-auth"], cwd=str(setup.parent), shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, WindowsSetupError):
            return AuthLaunchResult(False, "Не удалось открыть локальное окно авторизации.")
        self.states.save("funpay_auth_window", "requested", f"{self._now():.6f}")
        return AuthLaunchResult(True, "Окно авторизации открыто на этом компьютере. Войдите в FunPay обычным способом.")


class TelegramFunPayAuthRouter:
    """Private callback handler with no remote credential transport."""

    def __init__(self, allowed_user_ids: tuple[int, ...], request: LocalFunPayAuthRequest) -> None:
        self._allowed = frozenset(allowed_user_ids)
        self._request = request

    def handle(self, update: TelegramUpdate) -> CommandReply | None:
        if update.user_id not in self._allowed or update.chat_id != update.user_id:
            return None
        if update.callback_data == "auth:funpay":
            result = self._request.request()
            return CommandReply(result.message, edit_message=True)
        if update.callback_data == "auth:status":
            return CommandReply("FunPay требует авторизации. Автоматические изменения остановлены.", edit_message=True)
        return None


def _interactive_desktop_available() -> bool:
    if os.name != "nt":
        return False
    session = os.environ.get("SESSIONNAME", "")
    return session.casefold() not in {"", "services"}
