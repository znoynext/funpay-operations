from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.funpay import FunPayClient, RealOperationsDisabled
from funpay_operations.lots import Lot, LotRepository
from funpay_operations.pricing import PricePolicy
from funpay_operations.telegram import TelegramClient


class DomainTests(unittest.TestCase):
    def test_lot_repository_uses_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Database(Path(temporary_directory) / "operations.sqlite3")
            database.initialize()
            LotRepository(database).upsert(Lot("lot-1", "Example", 100, "RUB"))
            with database.session() as connection:
                row = connection.execute(
                    "SELECT title, price_minor FROM lots WHERE external_id = ?", ("lot-1",)
                ).fetchone()
                result = dict(row)

        self.assertEqual(result, {"title": "Example", "price_minor": 100})

    def test_external_actions_are_disabled_by_default(self) -> None:
        with self.assertRaises(RealOperationsDisabled):
            FunPayClient("funpay_session").require_explicit_operation("update_lot")
        with self.assertRaises(RealOperationsDisabled):
            TelegramClient("telegram_token", (1,)).require_explicit_send(1)

    def test_price_policy_respects_floor(self) -> None:
        with self.assertRaises(ValueError):
            PricePolicy(hard_floor=100).validate(99)
