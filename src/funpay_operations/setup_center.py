"""Local tkinter Setup Center; presentation only, with services kept outside callbacks."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import webbrowser

from .setup_services import FunPaySetupService, SetupOutcome, TelegramOwnerCandidate, TelegramSetupService
from .windows_infra import (
    WindowsPaths,
    WindowsSetupError,
    background_runtime_status,
    configure_minimum_price,
    configure_service_catalog,
    diagnostics,
    diagnostics_summary,
    installed_binaries,
    restart_background,
    setup_value,
    verify_autostart_target,
)


@dataclass(frozen=True)
class SetupStatus:
    label: str
    value: str


class SetupCenterController:
    """Controller used by both tkinter and smoke tests; it owns no widget state."""

    def __init__(
        self,
        paths: WindowsPaths,
        *,
        funpay: FunPaySetupService | None = None,
        telegram: TelegramSetupService | None = None,
        restart: Callable[[WindowsPaths], Path] = restart_background,
    ) -> None:
        self.paths = paths
        self.funpay = funpay or FunPaySetupService(paths)
        self.telegram = telegram or TelegramSetupService(paths)
        self._restart = restart

    def statuses(self) -> tuple[SetupStatus, ...]:
        report = diagnostics(self.paths)
        try:
            background, _, setup = installed_binaries(self.paths)
            autostart = "🟢 Готов" if verify_autostart_target(background) else "🔴 Требует проверки"
            setup_state = "🟢 Готово" if setup.is_file() else "🔴 Не найдено"
        except WindowsSetupError:
            autostart = "🔴 Не настроен"
            setup_state = "🔴 Не установлено"
        funpay = {
            "configured": "🟢 Подключён",
            "expired": "🔴 Требуется повторная авторизация",
            "not_configured": "⚪ Не настроен",
        }.get(report.get("funpay"), "🔴 Требует внимания")
        telegram = "🟢 Подключён" if report.get("telegram") == "configured" else "⚪ Не настроен"
        owner = "🟢 Подтверждён" if report.get("owner") == "configured" else "⚪ Не подтверждён"
        database = "🟢 Готова" if report.get("database") == "ok" else "🔴 Ошибка"
        application = "🟢 Готово" if report.get("directories") == "ok" else "🔴 Ошибка"
        automation = "⏸ Выключена (безопасный режим)"
        return (
            SetupStatus("Application", application),
            SetupStatus("FunPay", funpay),
            SetupStatus("Telegram", telegram),
            SetupStatus("Владелец", owner),
            SetupStatus("Автозапуск", autostart),
            SetupStatus("База данных", database),
            SetupStatus("Automation", automation),
            SetupStatus("Background", "🟢 Работает" if background_runtime_status(self.paths) == "running" else "⚪ Остановлен"),
            SetupStatus("Setup Center", setup_state),
        )

    def diagnostics_text(self) -> str:
        return "\n".join(diagnostics_summary(diagnostics(self.paths)))

    def connect_funpay(self, golden_key: str, golden_seal: str) -> SetupOutcome:
        return self.funpay.verify_and_save(golden_key, golden_seal)

    def connect_telegram(self, token: str) -> SetupOutcome:
        return self.telegram.verify_and_save(token)

    def wait_for_owner(self) -> TelegramOwnerCandidate | None:
        return self.telegram.wait_for_owner_start()

    def confirm_owner(self, user_id: int) -> SetupOutcome:
        return self.telegram.confirm_owner(user_id)

    def reject_owner(self) -> None:
        self.telegram.reject_owner()

    def save_catalog(self, definition: dict[str, object]) -> SetupOutcome:
        try:
            count = configure_service_catalog(self.paths, definition)
        except (ValueError, TypeError, OSError, sqlite3.Error) as error:
            return SetupOutcome(False, "Проверьте параметры услуг.", detail=type(error).__name__)
        return SetupOutcome(True, f"Каталог сохранён локально: услуг — {count}.")

    def save_minimum_price(self, label: str, amount: str) -> SetupOutcome:
        try:
            integer = int(amount)
            configure_minimum_price(self.paths, label, integer)
        except (TypeError, ValueError, OSError, sqlite3.Error):
            return SetupOutcome(False, "Введите положительную цену в рублях.")
        return SetupOutcome(True, "Минимальная цена сохранена локально.")

    def restart_background(self) -> SetupOutcome:
        try:
            self._restart(self.paths)
        except (WindowsSetupError, OSError, sqlite3.Error):
            return SetupOutcome(False, "Не удалось перезапустить фоновое приложение.")
        return SetupOutcome(True, "Фоновое приложение запущено в безопасном режиме.")

    def telegram_username(self) -> str | None:
        try:
            value = setup_value(self.paths, "telegram_bot_username")
        except (OSError, sqlite3.Error, ValueError):
            return None
        return value if isinstance(value, str) and value.strip() else None


class SetupCenterWindow:
    """Thin tkinter view delegating all decisions to ``SetupCenterController``."""

    def __init__(self, root: object, controller: SetupCenterController) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root, self.controller, self.tk, self.ttk = root, controller, tk, ttk
        root.title("FunPay Operations")
        root.minsize(540, 470)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self._status = tk.StringVar()
        frame = ttk.Frame(root, padding=18)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        ttk.Label(frame, text="FunPay Operations", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, textvariable=self._status, justify="left").grid(row=1, column=0, sticky="w", pady=(14, 14))
        buttons = (
            ("🔐 Подключить FunPay", self._open_funpay),
            ("🤖 Подключить Telegram", self._open_telegram),
            ("📦 Услуги", self._open_catalog),
            ("💰 Минимальные цены", self._open_minimum_price),
            ("🩺 Диагностика", self._show_diagnostics),
            ("🔄 Перезапустить бота", self._restart_background),
            ("Открыть Telegram", self._open_telegram_chat),
            ("Подробнее", self._show_diagnostics),
        )
        for row, (label, command) in enumerate(buttons, start=2):
            ttk.Button(frame, text=label, command=command).grid(row=row, column=0, sticky="ew", pady=3)
        ttk.Button(frame, text="Обновить статус", command=self.refresh).grid(row=len(buttons) + 2, column=0, sticky="ew", pady=(12, 0))
        frame.columnconfigure(0, weight=1)
        self.refresh()

    def refresh(self) -> None:
        self._status.set("\n".join(f"{item.label:<14} {item.value}" for item in self.controller.statuses()))

    def _message(self, title: str, text: str, *, error: bool = False) -> None:
        from tkinter import messagebox

        (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=self.root)

    def _open_funpay(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Подключение FunPay")
        frame = self.ttk.Frame(window, padding=16)
        frame.grid(sticky="nsew")
        self.ttk.Label(frame, text="🔐 Подключение FunPay", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(frame, text=("Бот использует существующую сессию FunPay.\n"
                                    "Браузер можно закрыть после подключения.\n\n"
                                    "Введите два значения cookie с funpay.com.")).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 10))
        key, seal = self.tk.StringVar(), self.tk.StringVar()
        self._secret_row(frame, 2, "golden_key:", key)
        self._secret_row(frame, 3, "golden_seal:", seal)

        def submit() -> None:
            result = self.controller.connect_funpay(key.get(), seal.get())
            key.set("")
            seal.set("")
            self._message("FunPay", result.message, error=not result.ok)
            if result.ok:
                window.destroy()
                self.refresh()

        self.ttk.Button(frame, text="Проверить и сохранить", command=submit).grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.ttk.Button(frame, text="Как найти ключи?", command=lambda: self._message(
            "Как найти ключи?", "1. Войдите в FunPay в обычном браузере.\n2. Нажмите F12 → Application → Cookies → https://funpay.com.\n3. Скопируйте Value golden_key и golden_seal."
        )).grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        self.ttk.Button(frame, text="Отмена", command=window.destroy).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _secret_row(self, frame: object, row: int, label: str, variable: object) -> None:
        self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        self.ttk.Entry(frame, textvariable=variable, show="*", width=42).grid(row=row, column=1, sticky="ew", pady=3)

    def _open_telegram(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Подключение Telegram")
        frame = self.ttk.Frame(window, padding=16)
        frame.grid(sticky="nsew")
        self.ttk.Label(frame, text="🤖 Подключение Telegram", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(frame, text="Создайте личного Telegram-бота через @BotFather и вставьте Bot Token.").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 10))
        token = self.tk.StringVar()
        self._secret_row(frame, 2, "Bot Token:", token)

        def submit() -> None:
            result = self.controller.connect_telegram(token.get())
            token.set("")
            self._message("Telegram", (result.message + (f"\n@{result.username}" if result.ok and result.username else "")), error=not result.ok)
            if result.ok:
                self.ttk.Button(frame, text="Жду /start...", command=self._wait_for_owner).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
                self.refresh()

        self.ttk.Button(frame, text="Проверить", command=submit).grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.ttk.Button(frame, text="Как создать бота?", command=lambda: self._message(
            "Как создать бота?", "1. Откройте @BotFather.\n2. Отправьте /newbot.\n3. Выберите имя и username.\n4. Скопируйте выданный token и вставьте его сюда."
        )).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))

    def _wait_for_owner(self) -> None:
        candidate = self.controller.wait_for_owner()
        if candidate is None:
            self._message("Telegram", "Пока не найдено личное сообщение /start. Откройте бота и нажмите Start, затем повторите.", error=True)
            return
        self._confirm_owner(candidate)

    def _confirm_owner(self, candidate: TelegramOwnerCandidate) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Подтверждение владельца")
        frame = self.ttk.Frame(window, padding=16)
        frame.grid(sticky="nsew")
        username = f"@{candidate.username}" if candidate.username else "без username"
        self.ttk.Label(frame, text=f"Найден Telegram аккаунт:\n{username}\nID: {candidate.masked_id}\n\nРазрешить управление FunPay Operations этому аккаунту?").grid(row=0, column=0, columnspan=2, sticky="w")

        def accept() -> None:
            result = self.controller.confirm_owner(candidate.user_id)
            self._message("Telegram", result.message, error=not result.ok)
            if result.ok:
                window.destroy()
                self.refresh()

        self.ttk.Button(frame, text="✅ Это я", command=accept).grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.ttk.Button(frame, text="❌ Нет", command=lambda: (self.controller.reject_owner(), window.destroy())).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))

    def _open_catalog(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Услуги")
        frame = self.ttk.Frame(window, padding=16)
        frame.grid(sticky="nsew")
        mythic, delves = self.tk.BooleanVar(value=True), self.tk.BooleanVar(value=True)
        min_key, max_key, min_tier, max_tier, packages = (self.tk.StringVar(value=value) for value in ("10", "10", "1", "1", "1"))
        self.ttk.Label(frame, text="Что будем продавать?", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Checkbutton(frame, text="Mythic+", variable=mythic).grid(row=1, column=0, sticky="w")
        self.ttk.Checkbutton(frame, text="Delves", variable=delves).grid(row=1, column=1, sticky="w")
        for row, label, variable in ((2, "Mythic+: минимальный ключ", min_key), (3, "Mythic+: максимальный ключ", max_key), (4, "Delves: минимальный tier", min_tier), (5, "Delves: максимальный tier", max_tier), (6, "Пакеты (например 1,3)", packages)):
            self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            self.ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)

        def save() -> None:
            try:
                package_sizes = [int(value.strip()) for value in packages.get().split(",") if value.strip()]
                definition: dict[str, object] = {"version": 1}
                common = {"regions": ["eu"], "service_formats": ["selfplay"], "package_sizes": package_sizes, "price_conditions": {}, "enabled": False, "desired_state": "disabled", "template_reference": "not_selected", "description_profile": "safe_neutral", "price_policy_reference": "not_selected"}
                if mythic.get():
                    definition["mythic_plus"] = {**common, "min_key_level": int(min_key.get()), "max_key_level": int(max_key.get())}
                if delves.get():
                    definition["delves"] = {**common, "min_tier": int(min_tier.get()), "max_tier": int(max_tier.get()), "modes": ["normal", "bountiful"]}
            except ValueError:
                self._message("Услуги", "Проверьте числовые параметры и пакеты.", error=True)
                return
            result = self.controller.save_catalog(definition)
            self._message("Услуги", result.message, error=not result.ok)
            if result.ok:
                window.destroy()
                self.refresh()

        self.ttk.Button(frame, text="Предпросмотр и сохранить", command=save).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _open_minimum_price(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("Минимальные цены")
        frame = self.ttk.Frame(window, padding=16)
        frame.grid(sticky="nsew")
        label, amount = self.tk.StringVar(value="Mythic+ +10"), self.tk.StringVar(value="1000")
        self.ttk.Label(frame, text="Минимально допустимая цена", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.ttk.Label(frame, text="Бот никогда не установит цену ниже этого значения.").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 10))
        self.ttk.Label(frame, text="Услуга").grid(row=2, column=0, sticky="w")
        self.ttk.Entry(frame, textvariable=label).grid(row=2, column=1, sticky="ew")
        self.ttk.Label(frame, text="Цена, ₽").grid(row=3, column=0, sticky="w")
        self.ttk.Entry(frame, textvariable=amount).grid(row=3, column=1, sticky="ew")

        def save() -> None:
            result = self.controller.save_minimum_price(label.get(), amount.get())
            self._message("Минимальные цены", result.message, error=not result.ok)
            if result.ok:
                window.destroy()
                self.refresh()

        self.ttk.Button(frame, text="Сохранить", command=save).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _show_diagnostics(self) -> None:
        self._message("Диагностика", self.controller.diagnostics_text())

    def _restart_background(self) -> None:
        result = self.controller.restart_background()
        self._message("Фоновое приложение", result.message, error=not result.ok)
        self.refresh()

    def _open_telegram_chat(self) -> None:
        username = self.controller.telegram_username()
        if username is None:
            self._message("Telegram", "Сначала подключите Telegram-бота.", error=True)
            return
        webbrowser.open_new_tab(f"https://t.me/{username}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local FunPay Operations Setup Center.")
    parser.add_argument("--smoke", action="store_true", help="load the safe GUI model without showing a window")
    args = parser.parse_args(argv)
    from .windows_infra import resolve_windows_paths

    controller = SetupCenterController(resolve_windows_paths())
    if args.smoke:
        controller.statuses()
        return 0
    import tkinter as tk

    root = tk.Tk()
    SetupCenterWindow(root, controller)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
