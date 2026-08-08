"""Explicit price guardrails without market scraping or statistics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PricePolicy:
    hard_floor: int | None

    def validate(self, price_minor: int) -> None:
        if price_minor < 0:
            raise ValueError("Price must not be negative")
        if self.hard_floor is not None and price_minor < self.hard_floor:
            raise ValueError("Price is below the configured hard floor")
