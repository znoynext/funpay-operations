"""Application composition root."""

from __future__ import annotations

from pathlib import Path

from .config import Settings, load_settings
from .database import Database
from .funpay import build_read_client
from .logging_setup import configure_logging
from .setup_wizard import SecretStore
from .tasks import BackgroundRunner


class Application:
    """Composes local services without initiating external operations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = configure_logging(settings.logs_directory, settings.log_level)
        self.database = Database(settings.database_path)
        self.funpay = build_read_client(
            settings, SecretStore(settings.data_directory / "secrets.dpapi")
        )
        self.runner = BackgroundRunner(settings, self.database, self.logger, funpay=self.funpay)

    @classmethod
    def from_files(cls, config_path: Path, env_path: Path) -> "Application":
        return cls(load_settings(config_path=config_path, env_path=env_path))

    async def run(self, *, once: bool) -> None:
        self.settings.backups_directory.mkdir(parents=True, exist_ok=True)
        self.database.initialize()
        self.logger.info("Application started in %s environment", self.settings.environment)
        await self.runner.run(once=once)
