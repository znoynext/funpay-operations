"""Secret-safe services used by the local Setup Center presentation layer."""

from __future__ import annotations

import json
import sqlite3
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .database import Database
from .funpay import FunPayError, NativeFunPayClient
from .repositories import TaskStateRepository
from .setup_wizard import SecretStore, SecretStoreError
from .telegram import TelegramBotProfile, TelegramError, TelegramHttpApi, TelegramUpdate
from .webview_auth import WebView2AuthLauncher, WebView2RuntimeUnavailable, WebViewAuthCancelled, WebViewAuthError
from .windows_infra import (
    WindowsPaths,
    delete_setup_value,
    save_setup_value,
    setup_value,
)


@dataclass(frozen=True)
class SetupOutcome:
    ok: bool
    message: str
    detail: str | None = None
    username: str | None = None


@dataclass(frozen=True)
class TelegramOwnerCandidate:
    user_id: int
    username: str | None

    @property
    def masked_id(self) -> str:
        value = str(self.user_id)
        return "•" * max(0, len(value) - 4) + value[-4:]


class FunPayAuthorizationClient(Protocol):
    def check_authorization(self) -> bool: ...

    def get_profile(self) -> object: ...

    def close(self) -> None: ...


class TelegramSetupApi(Protocol):
    def get_me(self) -> TelegramBotProfile: ...

    def get_updates(self, offset: int | None, timeout_seconds: int) -> tuple[TelegramUpdate, ...]: ...

    def send_message(self, chat_id: int, text: str, *, reply_markup: object | None = None) -> int: ...


def redact_setup_text(value: str, sensitive_values: tuple[str, ...] = ()) -> str:
    """Remove the exact GUI inputs before a diagnostic is written locally."""

    result = value
    for secret in sensitive_values:
        if secret:
            representations = (secret, json.dumps(secret), json.dumps(secret)[1:-1])
            for representation in representations:
                result = result.replace(representation, "<redacted>")
    return result


def write_redacted_setup_failure(paths: WindowsPaths, error: BaseException, sensitive_values: tuple[str, ...]) -> None:
    """Store technical detail locally without exposing GUI credential input."""

    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        detail = redact_setup_text(traceback.format_exc(), sensitive_values)
        (paths.logs / "setup-center-diagnostics.log").write_text(
            f"{type(error).__name__}\n{detail}", encoding="utf-8"
        )
    except OSError:
        pass


class FunPaySetupService:
    """Verify a proposed session read-only before committing it to DPAPI."""

    def __init__(
        self,
        paths: WindowsPaths,
        *,
        store_factory: Callable[[Path], SecretStore] = SecretStore,
        client_factory: Callable[[str], FunPayAuthorizationClient] | None = None,
    ) -> None:
        self.paths = paths
        self._store_factory = store_factory
        self._client_factory = client_factory or self._production_client

    @staticmethod
    def _production_client(session_value: str) -> NativeFunPayClient:
        return NativeFunPayClient(lambda: session_value, currency="RUB", allow_replies=False)

    def verify_and_save(self, golden_key: str, golden_seal: str) -> SetupOutcome:
        if not golden_key or not golden_seal or any("\r" in value or "\n" in value for value in (golden_key, golden_seal)):
            return SetupOutcome(False, "Не удалось проверить авторизацию FunPay.")
        proposed = json.dumps({"golden_key": golden_key, "golden_seal": golden_seal})
        sensitive = (golden_key, golden_seal, proposed)
        try:
            candidate = self._client_factory(proposed)
            try:
                if not candidate.check_authorization():
                    return SetupOutcome(False, "Не удалось подтвердить авторизацию FunPay.")
                profile = candidate.get_profile()
            finally:
                candidate.close()
            store = self._store_factory(self.paths.secrets)
            store.set("funpay_session", proposed)
            saved = store.get("funpay_session")
            if saved != proposed:
                store.delete("funpay_session")
                return SetupOutcome(False, "Не удалось безопасно сохранить сессию FunPay.")
            reread = self._client_factory(saved)
            try:
                if not reread.check_authorization():
                    store.delete("funpay_session")
                    return SetupOutcome(False, "Сессия FunPay не прошла повторную проверку.")
            finally:
                reread.close()
            Database(self.paths.database).initialize()
            TaskStateRepository(Database(self.paths.database)).save("funpay_session", "authorized")
            username = getattr(profile, "username", None)
            return SetupOutcome(True, "FunPay подключён.", username=username if isinstance(username, str) else None)
        except (FunPayError, SecretStoreError, OSError, ValueError, sqlite3.Error) as error:
            write_redacted_setup_failure(self.paths, error, sensitive)
            return SetupOutcome(False, "Не удалось проверить авторизацию FunPay.")

    def authorize_with_webview(self, launcher: WebView2AuthLauncher) -> SetupOutcome:
        """Use the local helper, then retain only a read-only verified session."""

        try:
            candidate = launcher.acquire()
        except WebView2RuntimeUnavailable:
            return SetupOutcome(False, "Для входа в FunPay требуется Microsoft Edge WebView2 Runtime.")
        except WebViewAuthCancelled:
            return SetupOutcome(False, "Вход в FunPay отменён.")
        except WebViewAuthError as error:
            write_redacted_setup_failure(self.paths, error, ())
            return SetupOutcome(False, "Не удалось открыть окно авторизации FunPay.")
        try:
            return self.verify_and_save(candidate.golden_key, candidate.golden_seal)
        finally:
            launcher.cleanup(candidate.profile)

    def verify_existing(self) -> SetupOutcome:
        """Read a DPAPI session once without exposing it to the UI."""

        session: str | None = None
        try:
            session = self._store_factory(self.paths.secrets).get("funpay_session")
            if not session:
                return SetupOutcome(False, "FunPay пока не настроен.")
            client = self._client_factory(session)
            try:
                if not client.check_authorization():
                    self._mark_expired()
                    return SetupOutcome(False, "Сессия FunPay больше не действует. Войдите снова.")
                profile = client.get_profile()
            finally:
                client.close()
            Database(self.paths.database).initialize()
            TaskStateRepository(Database(self.paths.database)).save("funpay_session", "authorized")
            username = getattr(profile, "username", None)
            return SetupOutcome(True, "FunPay подключён.", username=username if isinstance(username, str) else None)
        except (FunPayError, SecretStoreError, OSError, ValueError, sqlite3.Error) as error:
            self._mark_expired()
            write_redacted_setup_failure(self.paths, error, (session or "",))
            return SetupOutcome(False, "Сессия FunPay требует повторного входа.")

    def _mark_expired(self) -> None:
        try:
            Database(self.paths.database).initialize()
            TaskStateRepository(Database(self.paths.database)).save("funpay_session", "expired", last_error="session_expired")
        except sqlite3.Error:
            pass


class TelegramSetupService:
    """Validate a token and explicitly bind exactly one confirmed Telegram owner."""

    def __init__(
        self,
        paths: WindowsPaths,
        *,
        store_factory: Callable[[Path], SecretStore] = SecretStore,
        api_factory: Callable[[Callable[[], str | None]], TelegramSetupApi] | None = None,
    ) -> None:
        self.paths = paths
        self._store_factory = store_factory
        self._api_factory = api_factory or (lambda provider: TelegramHttpApi(provider))

    def verify_and_save(self, token: str) -> SetupOutcome:
        if not token or "\r" in token or "\n" in token:
            return SetupOutcome(False, "Не удалось проверить Telegram Bot Token.")
        try:
            profile = self._api_factory(lambda: token).get_me()
            store = self._store_factory(self.paths.secrets)
            store.set("telegram_bot_token", token)
            if store.get("telegram_bot_token") != token:
                store.delete("telegram_bot_token")
                return SetupOutcome(False, "Не удалось безопасно сохранить Telegram Bot Token.")
            save_setup_value(self.paths, "telegram_bot_username", profile.username)
            return SetupOutcome(True, "Бот найден.", username=profile.username)
        except (TelegramError, SecretStoreError, OSError, ValueError, sqlite3.Error) as error:
            write_redacted_setup_failure(self.paths, error, (token,))
            return SetupOutcome(False, "Не удалось проверить Telegram Bot Token.")

    def wait_for_owner_start(self, *, timeout_seconds: int = 25) -> TelegramOwnerCandidate | None:
        """Capture a /start candidate only; explicit local confirmation is required."""

        try:
            store = self._store_factory(self.paths.secrets)
            api = self._api_factory(lambda: store.get("telegram_bot_token"))
            updates = api.get_updates(None, timeout_seconds)
            for update in sorted(updates, key=lambda item: item.update_id):
                command = (update.text or "").strip().split(maxsplit=1)[0].casefold()
                if update.chat_id == update.user_id and command.split("@", 1)[0] == "/start":
                    candidate = TelegramOwnerCandidate(update.user_id, update.username)
                    save_setup_value(self.paths, "telegram_owner_candidate", {
                        "user_id": candidate.user_id, "username": candidate.username,
                    })
                    return candidate
        except (TelegramError, SecretStoreError, OSError, ValueError, sqlite3.Error) as error:
            write_redacted_setup_failure(self.paths, error, ())
            return None
        return None

    def pending_owner(self) -> TelegramOwnerCandidate | None:
        try:
            value = setup_value(self.paths, "telegram_owner_candidate")
        except (sqlite3.Error, ValueError, json.JSONDecodeError):
            return None
        user_id = value.get("user_id") if isinstance(value, dict) else None
        username = value.get("username") if isinstance(value, dict) else None
        if not isinstance(user_id, int) or user_id <= 0:
            return None
        return TelegramOwnerCandidate(user_id, username if isinstance(username, str) else None)

    def confirm_owner(self, user_id: int) -> SetupOutcome:
        candidate = self.pending_owner()
        if candidate is None or candidate.user_id != user_id:
            return SetupOutcome(False, "Подтверждение владельца больше недействительно.")
        try:
            save_setup_value(self.paths, "telegram_owner", {"user_id": candidate.user_id, "username": candidate.username})
            delete_setup_value(self.paths, "telegram_owner_candidate")
        except (sqlite3.Error, OSError, ValueError) as error:
            write_redacted_setup_failure(self.paths, error, ())
            return SetupOutcome(False, "Не удалось сохранить подтверждение владельца.")
        return SetupOutcome(True, "Этот Telegram-аккаунт получил доступ к управлению.", username=candidate.username)

    def reject_owner(self) -> None:
        try:
            delete_setup_value(self.paths, "telegram_owner_candidate")
        except (sqlite3.Error, OSError, ValueError):
            return

    def notify_funpay_authorized(self) -> None:
        """Best-effort success notice for the explicitly confirmed local owner."""

        try:
            owner = setup_value(self.paths, "telegram_owner")
            user_id = owner.get("user_id") if isinstance(owner, dict) else None
            if not isinstance(user_id, int) or user_id <= 0:
                return
            store = self._store_factory(self.paths.secrets)
            api = self._api_factory(lambda: store.get("telegram_bot_token"))
            api.send_message(
                user_id,
                "✅ FunPay снова подключён\n\nСессия проверена.\nАвтоматические изменения всё ещё выключены до отдельного разрешения.",
            )
        except (TelegramError, SecretStoreError, OSError, ValueError, sqlite3.Error):
            return
