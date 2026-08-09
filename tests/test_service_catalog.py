from __future__ import annotations

import copy
import io
import tempfile
import unittest
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.service_catalog import (
    CatalogFamily,
    DesiredState,
    DuplicateStableCodeError,
    ServiceCatalogRepository,
    ServiceCatalogValidationError,
    generate_catalog,
    load_catalog_definition,
    run_catalog_command,
)


def definition() -> dict[str, object]:
    return {
        "version": 1,
        "mythic_plus": {
            "min_key_level": 2, "max_key_level": 3, "regions": ["eu", "us"],
            "service_formats": ["selfplay"], "package_sizes": [1, 3],
            "price_conditions": {"timed": ["yes", "no"]}, "enabled": True,
            "desired_state": "enabled", "template_reference": "mplus_template",
            "description_profile": "mplus_profile", "price_policy_reference": "mplus_policy",
        },
        "delves": {
            "min_tier": 1, "max_tier": 1, "modes": ["normal", "bountiful"], "regions": ["eu"],
            "service_formats": [], "package_sizes": [1, 2], "price_conditions": {"key_source": ["included"]},
            "enabled": False, "desired_state": "disabled", "template_reference": "delve_template",
            "description_profile": "delve_profile", "price_policy_reference": "delve_policy",
        },
    }


class ServiceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_generates_configurable_mythic_and_delves_variants_with_stable_codes(self) -> None:
        services = generate_catalog(definition())
        self.assertEqual(len(services), 20)
        self.assertEqual(sum(service.family is CatalogFamily.MYTHIC_PLUS for service in services), 16)
        self.assertEqual(sum(service.family is CatalogFamily.DELVES for service in services), 4)
        self.assertIn("mplus_k2_eu_selfplay_x1_timed_yes", [service.stable_code for service in services])
        delve = next(service for service in services if service.family is CatalogFamily.DELVES)
        self.assertEqual(delve.variant["service_format"], "not_applicable")
        self.assertEqual(delve.desired_state, DesiredState.DISABLED)
        self.assertFalse(delve.enabled)

    def test_validation_rejects_ranges_packages_conditions_and_incompatible_combinations(self) -> None:
        invalid_range = copy.deepcopy(definition())
        invalid_range["mythic_plus"]["min_key_level"] = 4  # type: ignore[index]
        with self.assertRaises(ServiceCatalogValidationError):
            generate_catalog(invalid_range)

        duplicate_packages = copy.deepcopy(definition())
        duplicate_packages["delves"]["package_sizes"] = [1, 1]  # type: ignore[index]
        with self.assertRaises(ServiceCatalogValidationError):
            generate_catalog(duplicate_packages)

        no_single = copy.deepcopy(definition())
        no_single["mythic_plus"]["package_sizes"] = [2]  # type: ignore[index]
        with self.assertRaises(ServiceCatalogValidationError):
            generate_catalog(no_single)

        reserved_condition = copy.deepcopy(definition())
        reserved_condition["delves"]["price_conditions"] = {"mode": ["anything"]}  # type: ignore[index]
        with self.assertRaises(ServiceCatalogValidationError):
            generate_catalog(reserved_condition)

    def test_repository_replaces_catalog_and_rejects_duplicate_codes(self) -> None:
        services = generate_catalog(definition())
        database = Database(self.root / "catalog.sqlite3")
        database.initialize()
        repository = ServiceCatalogRepository(database)
        repository.replace(services)
        stored = repository.list()
        self.assertEqual([service.stable_code for service in stored], sorted(service.stable_code for service in services))
        self.assertEqual(stored[0].template_reference, services[0].template_reference)
        with self.assertRaises(DuplicateStableCodeError):
            repository.replace((services[0], services[0]))

    def test_example_init_and_cli_preview_validate_are_local_only(self) -> None:
        example = Path(__file__).resolve().parents[1] / "examples" / "service_catalog.example.json"
        catalog_path = self.root / "data" / "service_catalog.json"
        database_path = self.root / "data" / "service_catalog.sqlite3"
        output = io.StringIO()
        self.assertEqual(
            run_catalog_command("init-example", catalog_path=catalog_path, database_path=database_path, example_path=example, output=output),
            0,
        )
        self.assertTrue(catalog_path.is_file())
        self.assertEqual(len(ServiceCatalogRepository(Database(database_path)).list()), 48)
        output = io.StringIO()
        self.assertEqual(
            run_catalog_command("validate", catalog_path=catalog_path, database_path=database_path, example_path=example, output=output),
            0,
        )
        self.assertEqual(output.getvalue().strip(), "catalog validate: valid services=48")
        output = io.StringIO()
        self.assertEqual(
            run_catalog_command("preview", catalog_path=catalog_path, database_path=database_path, example_path=example, output=output),
            0,
        )
        self.assertTrue(output.getvalue().startswith("catalog preview: services=48 mythic_plus=16 delves=32"))

    def test_loader_rejects_non_object_json(self) -> None:
        path = self.root / "invalid.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ServiceCatalogValidationError):
            load_catalog_definition(path)
