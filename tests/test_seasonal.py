from __future__ import annotations

from datetime import date
import tempfile
import unittest
from pathlib import Path

from funpay_operations.seasonal import (
    ConfirmationStatus,
    DescriptionGenerator,
    SeasonalData,
    SeasonalDataError,
    UnconfirmedSeasonalDataError,
    load_seasonal_data,
)
from funpay_operations.services import MythicPlusService, Region, ServiceFormat


def confirmed(service: str, *, region: Region = Region.EU) -> SeasonalData:
    return SeasonalData.from_mapping({
        "schema_version": 1,
        "data_version": 3,
        "service": service,
        "season": "test_season",
        "region": region.value,
        "start_date": "2026-01-01",
        "reward_item_levels": {"key_12": 700},
        "crests": {"key_12": "gilded"},
        "checked_at": date.today().isoformat(),
        "sources": ["https://example.test/season"],
        "confirmation_status": "confirmed",
    })


class SeasonalDataTests(unittest.TestCase):
    def test_unconfirmed_public_templates_load_but_cannot_be_applied(self) -> None:
        root = Path(__file__).parents[1]
        data = load_seasonal_data(root / "seasonal_data" / "v1" / "mythic_plus.yaml")
        service = MythicPlusService(10, Region.EU, ServiceFormat.SELF_PLAY)

        self.assertEqual(data.confirmation_status, ConfirmationStatus.UNCONFIRMED)
        with self.assertRaises(UnconfirmedSeasonalDataError):
            DescriptionGenerator().mythic_plus(service, data)

    def test_confirmed_data_requires_complete_evidence(self) -> None:
        with self.assertRaises(SeasonalDataError):
            SeasonalData.from_mapping({
                "schema_version": 1, "data_version": 1, "service": "mythic_plus", "season": "test",
                "region": "eu", "start_date": None, "reward_item_levels": {}, "crests": {},
                "checked_at": None, "sources": [], "confirmation_status": "confirmed",
            })

    def test_mythic_plus_templates_change_at_level_thirteen(self) -> None:
        generator = DescriptionGenerator()
        data = confirmed("mythic_plus")
        lower = generator.mythic_plus(MythicPlusService(12, Region.EU, ServiceFormat.SELF_PLAY), data)
        higher = generator.mythic_plus(MythicPlusService(13, Region.EU, ServiceFormat.PILOT), data)

        self.assertIn("ilvl 700", lower.text)
        self.assertIn("Гребни для этого ключа", lower.text)
        self.assertIn("рейтинг Mythic+", higher.text)
        self.assertNotIn("Актуальные награды", higher.text)
        self.assertIn("Случайный предмет не гарантируется.", lower.text)
        self.assertEqual(lower.to_dict()["data_version"], 3)

    def test_service_region_validation_is_mythic_plus_only(self) -> None:
        generator = DescriptionGenerator()
        with self.assertRaises(SeasonalDataError):
            generator.mythic_plus(
                MythicPlusService(12, Region.US, ServiceFormat.SELF_PLAY), confirmed("mythic_plus")
            )
        with self.assertRaises(SeasonalDataError):
            confirmed("delves")

    def test_generator_requires_exact_level_and_fresh_confirmation(self) -> None:
        generator = DescriptionGenerator()
        with self.assertRaises(SeasonalDataError):
            generator.mythic_plus(MythicPlusService(10, Region.EU, ServiceFormat.SELF_PLAY), confirmed("mythic_plus"))
        stale = SeasonalData.from_mapping({**confirmed("mythic_plus").to_dict(), "checked_at": "2026-01-01"})
        with self.assertRaises(UnconfirmedSeasonalDataError):
            generator.mythic_plus(MythicPlusService(12, Region.EU, ServiceFormat.SELF_PLAY), stale)

    def test_yaml_validation_uses_safe_loader_and_rejects_unsafe_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "unsafe.yaml"
            path.write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")
            with self.assertRaises(SeasonalDataError):
                load_seasonal_data(path)

    def test_seasonal_data_serializes_public_fields_only(self) -> None:
        data = confirmed("mythic_plus")
        serialized = data.to_dict()
        self.assertEqual(serialized["start_date"], date(2026, 1, 1).isoformat())
        self.assertEqual(serialized["sources"], ["https://example.test/season"])
        self.assertNotIn("account", serialized)
