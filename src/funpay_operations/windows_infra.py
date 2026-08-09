"""Windows-only install, diagnostics, wizard, and Task Scheduler helpers."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO
from xml.sax.saxutils import escape as xml_escape

import yaml

from .database import Database
from .runtime import DuplicateProcessError, SingletonProcessLock

TASK_NAME = "FunPay Operations Background"
SAFE_CONFIG_NAME = "config.yaml"
INSTALLER_FILES = ("funpay-operations.exe", "funpay-operations-cli.exe")

@dataclass(frozen=True)
class WindowsPaths:
    application: Path
    config: Path
    data: Path
    secrets: Path
    database: Path
    logs: Path
    backups: Path


class WindowsSetupError(RuntimeError):
    """A recoverable local-installation problem safe to show without internals."""


@dataclass(frozen=True)
class WizardStep:
    number: int
    title: str
    rows: tuple[str, ...]
    action: str | None = None

def resolve_windows_paths(local_app_data: Path | None = None) -> WindowsPaths:
    root = local_app_data or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    base = root / "FunPay Operations"
    data = base / "data"
    return WindowsPaths(base / "app", base / "config", data, data / "secrets.dpapi", data / "funpay.sqlite3", data / "logs", data / "backups")

def initialise_windows_install(paths: WindowsPaths) -> None:
    for directory in (paths.application, paths.config, paths.data, paths.logs, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)
    Database(paths.database).initialize()

def safe_config_path(paths: WindowsPaths) -> Path:
    return paths.config / SAFE_CONFIG_NAME

def ensure_safe_config(paths: WindowsPaths) -> tuple[Path, bool]:
    """Create a secret-free safe-mode config once and preserve owner edits."""

    path = safe_config_path(paths)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "app": {
            "environment": "production",
            "log_level": "INFO",
            "data_directory": str(paths.data),
        },
        "storage": {
            "database_file": paths.database.name,
            "logs_directory": paths.logs.name,
            "backups_directory": paths.backups.name,
            "backup_retention_count": 7,
            "backup_interval_seconds": 3600,
        },
        "operations": {
            "mode": "safe",
            "enabled": False,
            "poll_interval_seconds": 30,
            "reconnect_initial_seconds": 5,
            "reconnect_max_seconds": 60,
        },
        "funpay": {
            "credential_key": "funpay_session",
            "request_timeout_seconds": 15,
            "min_request_interval_seconds": 1.0,
            "retry_attempts": 3,
            "message_notifications_enabled": False,
            "message_poll_interval_seconds": 5,
            "auto_reply_enabled": False,
        },
        "telegram": {
            "enabled": False,
            "token_key": "telegram_bot_token",
            "allowed_user_ids": [],
            "long_poll_timeout_seconds": 25,
            "notification_user_id": None,
        },
        "lots": {"default_currency": "RUB", "hard_floor": None},
    }
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path, True


def setup_value(paths: WindowsPaths, name: str) -> object | None:
    """Read a non-secret wizard preference from the local SQLite database."""

    with Database(paths.database).session() as connection:
        row = connection.execute(
            "SELECT value_json FROM local_setup_preferences WHERE name = ?", (name,)
        ).fetchone()
    return json.loads(row["value_json"]) if row is not None else None


def save_setup_value(paths: WindowsPaths, name: str, value: object) -> None:
    """Persist only non-secret setup choices; credentials remain in DPAPI."""

    if not name.replace("_", "").isalnum():
        raise ValueError("setup preference name is invalid")
    Database(paths.database).initialize()
    with Database(paths.database).session() as connection:
        connection.execute(
            """INSERT INTO local_setup_preferences(name, value_json)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value_json = excluded.value_json,
            updated_at = CURRENT_TIMESTAMP""",
            (name, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )


def configure_service_catalog(paths: WindowsPaths, definition: dict[str, object]) -> int:
    """Validate and store a user-selected catalog without asking for file paths."""

    from .service_catalog import ServiceCatalogRepository, generate_catalog

    services = generate_catalog(definition)
    ServiceCatalogRepository(Database(paths.database)).replace(services)
    save_setup_value(paths, "service_catalog_definition", definition)
    return len(services)


def configure_minimum_price(paths: WindowsPaths, service_label: str, amount_rub: int) -> None:
    """Store a local price floor choice in minor units for future mapped lots."""

    if not service_label.strip() or amount_rub <= 0:
        raise ValueError("minimum price must be positive")
    values = setup_value(paths, "minimum_prices")
    prices = dict(values) if isinstance(values, dict) else {}
    prices[service_label] = amount_rub * 100
    save_setup_value(paths, "minimum_prices", prices)

def diagnostics(paths: WindowsPaths) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        initialise_windows_install(paths)
        result["directories"] = "ok"
        result["write_permissions"] = "ok"
        database = Database(paths.database)
        with database.session() as connection:
            result["database"] = "ok" if connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else "error"
        result["migrations"] = "ok"
    except (OSError, sqlite3.Error):
        result.update({
            "directories": "error", "write_permissions": "error",
            "database": "error", "migrations": "error",
        })
    try:
        lock = SingletonProcessLock(paths.data / "funpay-operations.lock")
        lock.acquire()
        lock.release()
        result["singleton"] = "ok"
    except DuplicateProcessError:
        result["singleton"] = "in_use"
    except OSError:
        result["singleton"] = "error"
    result["dpapi"] = "available" if os.name == "nt" else "unavailable"
    try:
        from .setup_wizard import SecretStore, SecretStoreError

        result["funpay"] = "configured" if SecretStore(paths.secrets).get("funpay_session") else "not_configured"
    except (OSError, SecretStoreError):
        result["funpay"] = "error"
    try:
        with Database(paths.database).session() as connection:
            catalog_count = connection.execute("SELECT COUNT(*) FROM service_catalog").fetchone()[0]
        result["catalog"] = "ok" if catalog_count else "not_configured"
        minimum_prices = setup_value(paths, "minimum_prices")
        result["hard_floors"] = "ok" if isinstance(minimum_prices, dict) and minimum_prices else "not_configured"
    except (sqlite3.Error, ValueError, json.JSONDecodeError):
        result["catalog"] = "error"
        result["hard_floors"] = "error"
    result["funpay_adapter"] = "available"
    result["telegram"] = "not_configured"
    result["autostart"] = autostart_status()
    return result

def first_run(paths: WindowsPaths, *, configure_autostart: bool = False, executable: Path | None = None) -> dict[str, str]:
    initialise_windows_install(paths)
    _, config_created = ensure_safe_config(paths)
    result = diagnostics(paths)
    result.update({
        "config": "created" if config_created else "existing",
        "service_catalog": "not_configured", "hard_floor": "not_configured",
        "funpay": "skipped", "telegram": "skipped", "trusted_sellers": "skipped",
    })
    if configure_autostart and executable is not None:
        install_autostart(executable)
        result["autostart"] = "installed"
    return result


def installer_source_files(current_executable: Path | None = None) -> tuple[Path, Path]:
    """Find the two generic PyInstaller files next to the current CLI executable."""

    current = (current_executable or Path(sys.executable)).resolve()
    cli = current if current.name.casefold() == "funpay-operations-cli.exe" else current.with_name("funpay-operations-cli.exe")
    background = cli.with_name("funpay-operations.exe")
    if not cli.is_file() or not background.is_file():
        raise WindowsSetupError("standalone application files are unavailable")
    return background, cli


def install_application(paths: WindowsPaths, *, source_background: Path, source_cli: Path) -> tuple[Path, Path]:
    """Copy a generic build to the app directory without touching per-user data."""

    expected = (source_background.name.casefold(), source_cli.name.casefold())
    if expected != INSTALLER_FILES:
        raise WindowsSetupError("installer files have unexpected names")
    if not source_background.is_file() or not source_cli.is_file():
        raise WindowsSetupError("installer files are missing")
    paths.application.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for source in (source_background, source_cli):
        destination = paths.application / source.name
        temporary = destination.with_suffix(destination.suffix + ".new")
        try:
            temporary.write_bytes(source.read_bytes())
            temporary.replace(destination)
        except OSError as error:
            raise WindowsSetupError("application files could not be installed") from error
        installed.append(destination)
    return installed[0], installed[1]


def install_current_build(paths: WindowsPaths, current_executable: Path | None = None) -> tuple[Path, Path]:
    """Install the currently launched generic build into its per-user app folder."""

    background, cli = installer_source_files(current_executable)
    return install_application(paths, source_background=background, source_cli=cli)

def task_scheduler_xml(executable: Path) -> str:
    """Return a Scheduler XML task with command and arguments kept separate."""

    if any(character in str(executable) for character in ('"', "\r", "\n")):
        raise ValueError("autostart executable path contains unsafe characters")
    command, working_directory = xml_escape(str(executable)), xml_escape(str(executable.parent))
    return f"""<?xml version=\"1.0\" encoding=\"UTF-16\"?>
<Task version=\"1.4\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">
  <RegistrationInfo><Description>FunPay Operations background app</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
  <Actions Context=\"Author\"><Exec><Command>{command}</Command><Arguments>--background</Arguments><WorkingDirectory>{working_directory}</WorkingDirectory></Exec></Actions>
</Task>"""


def task_scheduler_command(executable: Path, *, action: str, task_xml: Path | None = None) -> list[str]:
    if action == "install":
        if task_xml is None:
            raise ValueError("autostart install requires a generated task XML file")
        return [
            "schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(task_xml), "/F",
        ]
    if action == "remove":
        return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if action == "status":
        return ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"]
    raise ValueError("unsupported autostart action")

def resolve_background_executable(current_executable: Path | None = None) -> Path:
    """Select the noconsole sibling from a standalone build."""

    current = (current_executable or Path(sys.executable)).resolve()
    if current.name.casefold() == "funpay-operations.exe":
        return current
    if current.name.casefold() == "funpay-operations-cli.exe":
        background = current.with_name("funpay-operations.exe")
        if background.is_file():
            return background
        raise FileNotFoundError("background executable is missing next to the CLI executable")
    raise RuntimeError("autostart installation requires the standalone Windows executable")

def install_autostart(executable: Path, runner: Callable[..., object] = subprocess.run) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-16", suffix=".xml", delete=False) as temporary:
        temporary.write(task_scheduler_xml(executable))
        temporary_path = Path(temporary.name)
    try:
        runner(
            task_scheduler_command(executable, action="install", task_xml=temporary_path), check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        temporary_path.unlink(missing_ok=True)

def remove_autostart(runner: Callable[..., object] = subprocess.run) -> None:
    runner(
        task_scheduler_command(Path("funpay-operations.exe"), action="remove"), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def autostart_status(runner: Callable[..., object] = subprocess.run) -> str:
    try:
        runner(
            task_scheduler_command(Path("funpay-operations.exe"), action="status"),
            check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "installed"
    except (OSError, subprocess.CalledProcessError):
        return "not_configured"


def diagnostics_summary(result: dict[str, str]) -> tuple[str, ...]:
    """Translate technical diagnostics to the compact local setup vocabulary."""

    def marker(value: str) -> str:
        return "🟢" if value in {"ok", "available", "installed"} else "⚪" if value in {"not_configured", "skipped"} else "❌"

    application = "готово" if result.get("directories") == "ok" and result.get("singleton") == "ok" else "требует внимания"
    database = "готова" if result.get("database") == "ok" and result.get("migrations") == "ok" else "не удалось проверить"
    funpay = "не настроен" if result.get("funpay") in {None, "skipped", "not_configured"} else "настроен"
    telegram = "не настроен" if result.get("telegram") == "not_configured" else result.get("telegram", "требует внимания")
    catalog = "готов" if result.get("catalog") == "ok" else "не настроен"
    return (
        f"{marker(result.get('directories', 'error'))} Application — {application}",
        f"{marker(result.get('database', 'error'))} Database — {database}",
        f"{marker(result.get('autostart', 'not_configured'))} Autostart — {'включён' if result.get('autostart') == 'installed' else 'не настроен'}",
        f"⚪ FunPay — {funpay}",
        f"{marker(result.get('telegram', 'not_configured'))} Telegram — {telegram}",
        f"{marker(result.get('catalog', 'not_configured'))} Service catalog — {catalog}",
    )


def wizard_steps(paths: WindowsPaths, *, autostart: str | None = None) -> tuple[WizardStep, ...]:
    """Build safe, credential-free screens for the seven-step local wizard."""

    report = diagnostics(paths)
    autostart = autostart or report["autostart"]
    return (
        WizardStep(1, "Система", (
            "✅ Windows поддерживается" if os.name == "nt" else "❌ Windows не поддерживается",
            "✅ Папки готовы" if report["directories"] == "ok" else "❌ Не удалось подготовить папки",
            "✅ База данных готова" if report["database"] == "ok" else "❌ Не удалось проверить базу данных",
        )),
        WizardStep(2, "FunPay", ("○ Пока не настроен", "Браузер не нужно держать открытым.", "Сессия будет храниться локально и зашифрована Windows."), "Настроить позже"),
        WizardStep(3, "Telegram", ("○ Пока не настроен", "Токен будет храниться локально и зашифрован Windows."), "Настроить позже"),
        WizardStep(4, "Каталог услуг", ("○ Пока не настроен" if report["catalog"] != "ok" else "✅ Каталог сохранён", "Выберите услуги и их параметры без редактирования файлов."), "Настроить позже"),
        WizardStep(5, "Минимальные цены", ("○ Пока не настроены" if report["hard_floors"] != "ok" else "✅ Минимальные цены сохранены", "Бот никогда не установит цену ниже указанной."), "Настроить позже"),
        WizardStep(6, "Автозапуск", ("✅ Запускается после входа в Windows" if autostart == "installed" else "○ Не настроен", "Запуск через 30 секунд, только для текущего пользователя."), "Установить автозапуск"),
        WizardStep(7, "Готово", diagnostics_summary(report)),
    )


def render_wizard_step(step: WizardStep) -> str:
    action = f"\n\n[ {step.action} ]" if step.action else ""
    return f"Шаг {step.number}/7 — {step.title}\n" + "\n".join(step.rows) + action


def _choice(input_fn: Callable[[str], str], prompt: str) -> str:
    return input_fn(prompt).strip().casefold()


def _positive_input(input_fn: Callable[[str], str], prompt: str) -> int:
    value = _choice(input_fn, prompt)
    if not value.isdecimal() or int(value) <= 0:
        raise WindowsSetupError("a positive number is required")
    return int(value)


def _choices_input(input_fn: Callable[[str], str], prompt: str, allowed: set[str]) -> list[str]:
    values = [part.strip().casefold() for part in input_fn(prompt).split(",") if part.strip()]
    if not values or any(value not in allowed for value in values) or len(values) != len(set(values)):
        raise WindowsSetupError("unsupported choice")
    return values


def _packages_input(input_fn: Callable[[str], str], prompt: str) -> list[int]:
    values = [part.strip() for part in input_fn(prompt).split(",") if part.strip()]
    if not values or any(not value.isdecimal() or int(value) <= 0 for value in values):
        raise WindowsSetupError("invalid package size")
    packages = [int(value) for value in values]
    if 1 not in packages or len(packages) != len(set(packages)):
        raise WindowsSetupError("packages must include one run exactly once")
    return packages


def _catalog_definition_from_wizard(input_fn: Callable[[str], str]) -> dict[str, object] | None:
    selected = _choice(input_fn, "Что будем продавать? [m] Mythic+  [d] Delves  [b] оба  [Enter] позже: ")
    if not selected:
        return None
    if selected not in {"m", "d", "b"}:
        raise WindowsSetupError("unknown catalog selection")

    definition: dict[str, object] = {"version": 1}
    if selected in {"m", "b"}:
        minimum = _positive_input(input_fn, "Mythic+: минимальный ключ: ")
        maximum = _positive_input(input_fn, "Mythic+: максимальный ключ: ")
        definition["mythic_plus"] = {
            "min_key_level": minimum, "max_key_level": maximum, "regions": ["eu"],
            "service_formats": _choices_input(input_fn, "Mythic+: формат (selfplay,pilot): ", {"selfplay", "pilot"}),
            "package_sizes": _packages_input(input_fn, "Mythic+: пакеты (например 1,3): "),
            "price_conditions": {}, "enabled": False, "desired_state": "disabled",
            "template_reference": "not_selected", "description_profile": "safe_neutral",
            "price_policy_reference": "not_selected",
        }
    if selected in {"d", "b"}:
        minimum = _positive_input(input_fn, "Delves: минимальный tier: ")
        maximum = _positive_input(input_fn, "Delves: максимальный tier: ")
        definition["delves"] = {
            "min_tier": minimum, "max_tier": maximum,
            "modes": _choices_input(input_fn, "Delves: режимы (normal,bountiful): ", {"normal", "bountiful"}),
            "regions": ["eu"],
            "service_formats": _choices_input(input_fn, "Delves: формат (selfplay,pilot): ", {"selfplay", "pilot"}),
            "package_sizes": _packages_input(input_fn, "Delves: пакеты (например 1,2): "),
            "price_conditions": {}, "enabled": False, "desired_state": "disabled",
            "template_reference": "not_selected", "description_profile": "safe_neutral",
            "price_policy_reference": "not_selected",
        }
    return definition


def run_setup_wizard(
    paths: WindowsPaths, output: TextIO, *, installed_background: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> int:
    """Safe seven-step setup flow; remote accounts and live writes stay disabled.

    Passing no input function produces the install-safe "configure later" path.
    When explicitly interactive, the catalog is previewed before its local
    SQLite save. Credentials are never requested by this general wizard.
    """

    try:
        first_run(paths)
        if installed_background is not None:
            install_autostart(installed_background)
        report = diagnostics(paths)
    except (OSError, sqlite3.Error, subprocess.CalledProcessError, WindowsSetupError) as error:
        try:
            paths.logs.mkdir(parents=True, exist_ok=True)
            (paths.logs / "setup-diagnostics.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        print("❌ Не удалось завершить первоначальную настройку", file=output)
        print("[ Повторить ]  [ Подробнее ]", file=output)
        raise WindowsSetupError("setup wizard failed; inspect local diagnostics and logs") from error
    for step in wizard_steps(paths, autostart=report["autostart"]):
        print(render_wizard_step(step), file=output)
        print(file=output)
        if input_fn is not None and step.number == 4:
            definition = _catalog_definition_from_wizard(input_fn)
            if definition is not None:
                from .service_catalog import generate_catalog

                services = generate_catalog(definition)
                print(f"Предпросмотр: будет сохранено услуг: {len(services)}. Изменений на FunPay не будет.", file=output)
                if _choice(input_fn, "Сохранить каталог? [y/N]: ") == "y":
                    configure_service_catalog(paths, definition)
                    print("✅ Каталог сохранён локально.", file=output)
        if input_fn is not None and step.number == 5:
            label = input_fn("Для какой услуги указать минимальную цену? [Enter] позже: ").strip()
            if label:
                amount = _positive_input(input_fn, f"{label}: минимально допустимая цена, ₽: ")
                print("Бот никогда не установит цену ниже этого значения.", file=output)
                if _choice(input_fn, "Сохранить минимальную цену? [y/N]: ") == "y":
                    configure_minimum_price(paths, label, amount)
                    print("✅ Минимальная цена сохранена локально.", file=output)
    print("Настройка завершена. FunPay и Telegram можно подключить позже.", file=output)
    return 0
