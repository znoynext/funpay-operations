"""Command-line entry point for the background application."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .app import Application


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FunPay operations background scaffold.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="YAML configuration path")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv configuration path")
    parser.add_argument("--once", action="store_true", help="Run one safe background cycle and exit")
    args = parser.parse_args()

    application = Application.from_files(args.config, args.env_file)
    asyncio.run(application.run(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
