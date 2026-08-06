"""Lot domain objects and persistence operations."""

from __future__ import annotations

from dataclasses import dataclass

from .database import Database


@dataclass(frozen=True)
class Lot:
    external_id: str
    title: str
    price_minor: int
    currency: str


class LotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, lot: Lot) -> None:
        if lot.price_minor < 0:
            raise ValueError("price_minor must not be negative")
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO lots (external_id, title, price_minor, currency)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(external_id) DO UPDATE SET
                    title = excluded.title,
                    price_minor = excluded.price_minor,
                    currency = excluded.currency,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (lot.external_id, lot.title, lot.price_minor, lot.currency),
            )
