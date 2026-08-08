from __future__ import annotations

import unittest

from funpay_operations.services import (
    DelveService,
    DuplicateServiceError,
    MythicPlusService,
    Region,
    ServiceCatalog,
    ServiceFormat,
)


class ServiceModelTests(unittest.TestCase):
    def test_mythic_plus_code_conditions_and_round_trip(self) -> None:
        service = MythicPlusService(
            key_level=10,
            region=Region.EU,
            service_format=ServiceFormat.SELF_PLAY,
            price_conditions={"timed": "yes", "affix": "fortified"},
        )

        self.assertEqual(service.code, "mplus_10_selfplay_x1")
        self.assertFalse(service.is_package)
        self.assertEqual(service.price_conditions, (("affix", "fortified"), ("timed", "yes")))
        self.assertEqual(MythicPlusService.from_json(service.to_json()), service)

    def test_mythic_plus_package_and_duplicate_protection(self) -> None:
        catalog = ServiceCatalog()
        service = MythicPlusService(10, Region.EU, ServiceFormat.SELF_PLAY, runs=3)
        catalog.add(service)
        catalog.add(MythicPlusService(10, Region.US, ServiceFormat.SELF_PLAY, runs=3))

        with self.assertRaises(DuplicateServiceError):
            catalog.add(service)
        self.assertTrue(service.is_package)
        self.assertEqual(len(catalog.values()), 2)

    def test_delve_codes_and_round_trip(self) -> None:
        bountiful = DelveService(
            8, True, Region.EU, ServiceFormat.SELF_PLAY, price_conditions={"keys": "included"}
        )
        ordinary = DelveService(8, False, Region.EU, ServiceFormat.PILOT, runs=5)

        self.assertEqual(bountiful.code, "delve_t8_bountiful_selfplay_x1")
        self.assertEqual(ordinary.code, "delve_t8_pilot_x5")
        self.assertTrue(ordinary.is_package)
        self.assertEqual(bountiful.price_conditions, (("keys", "included"),))
        self.assertEqual(DelveService.from_json(bountiful.to_json()), bountiful)

    def test_delve_price_conditions_participate_in_stable_deduplication(self) -> None:
        catalog = ServiceCatalog()
        included = DelveService(8, True, Region.EU, ServiceFormat.SELF_PLAY, price_conditions={"keys": "included"})
        buyer_keys = DelveService(8, True, Region.EU, ServiceFormat.SELF_PLAY, price_conditions={"keys": "buyer"})

        catalog.add(included)
        catalog.add(buyer_keys)
        with self.assertRaises(DuplicateServiceError):
            catalog.add(DelveService(8, True, Region.EU, ServiceFormat.SELF_PLAY, price_conditions={"keys": "included"}))

    def test_invalid_parameters_and_unsafe_serialization_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MythicPlusService(1, Region.EU, ServiceFormat.SELF_PLAY)
        with self.assertRaises(ValueError):
            MythicPlusService(10, Region.EU, ServiceFormat.SELF_PLAY, runs=0)
        with self.assertRaises(ValueError):
            MythicPlusService(10, Region.EU, ServiceFormat.SELF_PLAY, price_conditions={"Bad name": "x"})
        with self.assertRaises(ValueError):
            DelveService(12, True, Region.EU, ServiceFormat.SELF_PLAY)
        with self.assertRaises(ValueError):
            DelveService(8, "true", Region.EU, ServiceFormat.SELF_PLAY)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            DelveService.from_json("[]")
        with self.assertRaises(ValueError):
            MythicPlusService.from_dict({
                "kind": "mythic_plus", "key_level": 10, "region": "eu",
                "service_format": "selfplay", "code": "mplus_20_selfplay_x1",
            })
