"""Command-line entry point for the background application."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from .app import Application
from .config import load_settings
from .service_catalog import default_example_path, run_catalog_command
from .funpay import build_read_client
from .database import Database
from .lot_discovery import OwnLotRegistryRepository, run_discovery
from .lot_sync import run_plan_sync
from .lot_writes import MockLotWriteClient
from .price_transactions import run_price_transaction_command
from .seasonal import DescriptionGenerator, SeasonalDataError, load_seasonal_data
from .services import DelveService, MythicPlusService, Region, ServiceFormat
from .setup_wizard import SecretStore
from .smoke import run_smoke_test
from .windows_infra import (
    autostart_status, diagnostics, diagnostics_summary, first_run, install_autostart, install_current_build,
    remove_autostart, resolve_background_executable, resolve_windows_paths, run_setup_wizard, safe_config_path,
    WindowsSetupError,
)


def main() -> int:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run the FunPay operations background scaffold.")
    parser.add_argument("command", nargs="?", choices=["smoke-test", "discover-lots", "catalog", "lots", "prices", "diagnostics", "first-run", "setup", "install", "uninstall", "install-autostart", "remove-autostart", "show-autostart-status", "repair-autostart"], help="run a local command")
    parser.add_argument("catalog_action", nargs="?", choices=["preview", "validate", "init-example", "plan-sync", "check", "dry-run-update", "rollback-preview"])
    parser.add_argument("--config", type=Path, help="YAML configuration path (takes priority over .env)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv configuration path")
    parser.add_argument("--once", action="store_true", help="Run one safe background cycle and exit")
    parser.add_argument("--non-interactive", action="store_true", help="Skip optional local setup choices")
    parser.add_argument(
        "--background", action="store_true",
        help="Use the per-user Windows install paths (intended for the noconsole executable)",
    )
    parser.add_argument("--preview-seasonal-data", type=Path, help="Render a local seasonal description preview from YAML")
    parser.add_argument("--preview-key-level", type=int, help="Mythic+ key level for --preview-seasonal-data")
    parser.add_argument("--preview-delve-tier", type=int, help="Delve tier for --preview-seasonal-data")
    parser.add_argument("--preview-region", choices=[region.value for region in Region], default="eu")
    parser.add_argument("--preview-format", choices=[format_.value for format_ in ServiceFormat], default="selfplay")
    parser.add_argument("--preview-bountiful", action="store_true", help="Use Bountiful mode for a Delve preview")
    parser.add_argument(
        "--select-mythic-template", action="store_true",
        help="prompt locally to select an already mapped Mythic+ lot as an exemplar",
    )
    parser.add_argument(
        "--select-delves-template", action="store_true",
        help="prompt locally to select an already mapped Delves lot as an exemplar",
    )
    parser.add_argument(
        "--catalog-file", type=Path, default=Path("data") / "service_catalog.json",
        help="local ignored catalog definition path",
    )
    parser.add_argument(
        "--catalog-database", type=Path, default=Path("data") / "service_catalog.sqlite3",
        help="local ignored SQLite path for catalog entries",
    )
    parser.add_argument(
        "--registry-database", type=Path, default=Path("data") / "funpay.sqlite3",
        help="local ignored SQLite path for discovered own lots and confirmed mappings",
    )
    parser.add_argument(
        "--price-database", type=Path, default=Path("data") / "price_transactions.sqlite3",
        help="local ignored SQLite path for mock transaction snapshots",
    )
    args = parser.parse_args()

    if args.command in {"diagnostics", "first-run", "setup", "install", "uninstall", "install-autostart", "remove-autostart", "show-autostart-status", "repair-autostart"}:
        paths = resolve_windows_paths()
        if args.command == "diagnostics":
            for line in diagnostics_summary(diagnostics(paths)):
                print(line)
            return 0
        if args.command == "first-run":
            for name, value in first_run(paths).items():
                print(f"{name}: {value}")
            return 0
        if args.command in {"setup", "install"}:
            try:
                background, _ = install_current_build(paths)
            except WindowsSetupError:
                # Development and CI may run this Python entry point directly.
                # The wizard remains useful but intentionally never invents an
                # autostart target when no standalone build exists.
                background = None
            try:
                return run_setup_wizard(
                    paths, sys.stdout, installed_background=background,
                    input_fn=None if args.non_interactive else input,
                )
            except WindowsSetupError:
                return 1
        if args.command == "uninstall":
            remove_autostart()
            print("Автозапуск удалён. Локальные данные и зашифрованные секреты сохранены.")
            return 0
        if args.command == "show-autostart-status":
            print(f"autostart: {autostart_status()}")
            return 0
        if args.command in {"install-autostart", "repair-autostart"}:
            install_autostart(resolve_background_executable())
            return 0
        remove_autostart()
        return 0

    if args.background:
        paths = resolve_windows_paths()
        first_run(paths)
        args.config = safe_config_path(paths)
        args.env_file = paths.config / ".env"

    if args.preview_seasonal_data:
        try:
            data = load_seasonal_data(args.preview_seasonal_data)
            region = Region(args.preview_region)
            service_format = ServiceFormat(args.preview_format)
            generator = DescriptionGenerator()
            if args.preview_key_level is not None and args.preview_delve_tier is None:
                print(generator.mythic_plus(MythicPlusService(args.preview_key_level, region, service_format), data).text)
                return 0
            if args.preview_delve_tier is not None and args.preview_key_level is None:
                print(generator.delves(DelveService(args.preview_delve_tier, args.preview_bountiful, region, service_format), data).text)
                return 0
            parser.error("choose exactly one of --preview-key-level or --preview-delve-tier with --preview-seasonal-data")
        except SeasonalDataError as error:
            parser.error(f"seasonal preview unavailable: {error}")

    load_dotenv(dotenv_path=args.env_file, override=False)
    config_path = args.config or Path(os.getenv("FUNPAY_MANAGER_CONFIG", "config.yaml"))
    if args.command == "catalog":
        if args.catalog_action not in {"preview", "validate", "init-example"}:
            parser.error("catalog requires preview, validate, or init-example")
        return run_catalog_command(
            args.catalog_action, catalog_path=args.catalog_file, database_path=args.catalog_database,
            example_path=default_example_path(), output=sys.stdout,
        )
    if args.command == "lots":
        if args.catalog_action != "plan-sync":
            parser.error("lots requires plan-sync")
        return run_plan_sync(
            catalog_database=Database(args.catalog_database),
            registry_database=Database(args.registry_database),
            write_client=MockLotWriteClient(),
            output=sys.stdout,
        )
    if args.command == "prices":
        if args.catalog_action not in {"check", "dry-run-update", "rollback-preview"}:
            parser.error("prices requires check, dry-run-update, or rollback-preview")
        return run_price_transaction_command(args.catalog_action, database=Database(args.price_database), output=sys.stdout)
    if args.command == "smoke-test":
        settings = load_settings(config_path=config_path, env_path=args.env_file)
        client = build_read_client(settings, SecretStore(settings.data_directory / "secrets.dpapi"))
        return run_smoke_test(client, output=sys.stdout)
    if args.command == "discover-lots":
        settings = load_settings(config_path=config_path, env_path=args.env_file)
        database = Database(settings.database_path)
        database.initialize()
        client = build_read_client(settings, SecretStore(settings.data_directory / "secrets.dpapi"))
        mythic_template_id = None
        delves_template_id = None
        try:
            if args.select_mythic_template:
                mythic_template_id = getpass.getpass("FunPay Mythic+ lot ID for local template (input hidden): ")
            if args.select_delves_template:
                delves_template_id = getpass.getpass("FunPay Delves lot ID for local template (input hidden): ")
            return run_discovery(
                client, OwnLotRegistryRepository(database), output=sys.stdout,
                mythic_template_id=mythic_template_id, delves_template_id=delves_template_id,
            )
        finally:
            client.close()
    application = Application.from_files(config_path, args.env_file)
    asyncio.run(application.run(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
