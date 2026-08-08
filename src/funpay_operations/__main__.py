"""Command-line entry point for the background application."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .app import Application
from .seasonal import DescriptionGenerator, SeasonalDataError, load_seasonal_data
from .services import DelveService, MythicPlusService, Region, ServiceFormat


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FunPay operations background scaffold.")
    parser.add_argument("--config", type=Path, help="YAML configuration path (takes priority over .env)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv configuration path")
    parser.add_argument("--once", action="store_true", help="Run one safe background cycle and exit")
    parser.add_argument("--preview-seasonal-data", type=Path, help="Render a local seasonal description preview from YAML")
    parser.add_argument("--preview-key-level", type=int, help="Mythic+ key level for --preview-seasonal-data")
    parser.add_argument("--preview-delve-tier", type=int, help="Delve tier for --preview-seasonal-data")
    parser.add_argument("--preview-region", choices=[region.value for region in Region], default="eu")
    parser.add_argument("--preview-format", choices=[format_.value for format_ in ServiceFormat], default="selfplay")
    parser.add_argument("--preview-bountiful", action="store_true", help="Use Bountiful mode for a Delve preview")
    args = parser.parse_args()

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
    application = Application.from_files(config_path, args.env_file)
    asyncio.run(application.run(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
