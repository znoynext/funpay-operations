"""Command-line entry point for the background application."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .app import Application


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FunPay operations background scaffold.")
    parser.add_argument("--config", type=Path, help="YAML configuration path (takes priority over .env)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv configuration path")
    parser.add_argument("--once", action="store_true", help="Run one safe background cycle and exit")
    args = parser.parse_args()

    load_dotenv(dotenv_path=args.env_file, override=False)
    config_path = args.config or Path(os.getenv("FUNPAY_MANAGER_CONFIG", "config.yaml"))
    application = Application.from_files(config_path, args.env_file)
    asyncio.run(application.run(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
