"""Regression coverage for passive live company-registry shadowing."""

from concurrent.futures import ThreadPoolExecutor
from csv import DictReader, DictWriter
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from company_registry.schema import MIGRATION_1, MIGRATION_2, MIGRATION_3, SCHEMA_VERSION
from company_registry.shadow import (
    ShadowObserver,
    create_consistent_shadow_snapshot,
    inspect_shadow_database,
)
from utils.google_maps_scraper import GoogleMaps
from utils.google_search_discovery import DISCOVERY_FIELDS, discover_companies
from utils.known_companies import KnownCompanies


STAMP = "2026-10-08T12:00:00+00:00"
PLACE = "https://www.google.com/maps/place/Acme/data=!4m1!1sChIJAcme"


def create_v2_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATION_1)
    connection.executescript(MIGRATION_2)
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()


class PassiveShadowTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production.db"
        self.shadow = self.root / "shadow.db"
        create_v2_database(self.production)

    def initialize_shadow(self):
        return create_consistent_shadow_snapshot(self.production, self.shadow)

    def test_snapshot_migrates_only_shadow_and_refuses_implicit_overwrite(self):
        production_before = self.production.read_bytes()
        result = self.initialize_shadow()
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(result["foreign_key_violations"], 0)
        self.assertEqual(self.production.read_bytes(), production_before)
        raw = sqlite3.connect(self.production)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 2)
        raw.close()
        with self.assertRaises(FileExistsError):
            self.initialize_shadow()

    def test_explicit_reset_archives_existing_shadow(self):
        self.initialize_shadow()
        result = create_consistent_shadow_snapshot(
            self.production, self.shadow, reset=True,
        )
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertEqual(
            inspect_shadow_database(self.shadow)["schema_version"], SCHEMA_VERSION,
        )

    def test_restart_and_concurrent_submissions_are_idempotent(self):
        self.initialize_shadow()
        observer = ShadowObserver(
            self.shadow, source_system="GOOGLE_MAPS",
            report_directory=self.root / "reports", run_id="run-one",
        )
        row = {
            "title": "Acme", "map_link": PLACE,
            "webpage": "https://acme.tn", "phone_number": "71 123 456",
        }
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(
                lambda _: observer.observe(row, query="software Tunis"), range(8),
            ))
        first_summary = observer.close()
        self.assertEqual(first_summary["total_observations"], 1)

        restarted = ShadowObserver(
            self.shadow, source_system="GOOGLE_MAPS",
            report_directory=self.root / "reports", run_id="run-two",
        )
        restarted.observe(row, query="software Tunis")
        second_summary = restarted.close()
        self.assertEqual(second_summary["total_observations"], 1)
        connection = sqlite3.connect(self.shadow)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM discovery_observations"
        ).fetchone()[0], 1)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM shadow_comparisons"
        ).fetchone()[0], 1)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM shadow_run_observations"
        ).fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT count(*) FROM companies").fetchone()[0], 1)
        connection.close()

    def test_v3_shadow_is_rejected_without_mutation_or_report_side_effects(self):
        connection = sqlite3.connect(self.shadow)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        before = self.shadow.read_bytes()
        reports = self.root / "v3-reports"

        with self.assertRaisesRegex(
            RuntimeError, "schema version 3 is incompatible.*never migrated automatically",
        ):
            ShadowObserver(
                self.shadow, source_system="GOOGLE_MAPS",
                report_directory=reports, run_id="v3-rejected",
            )

        self.assertEqual(self.shadow.read_bytes(), before)
        self.assertFalse(reports.exists())

    def configure_maps_scraper(self, shadow_observer=None):
        scraper = GoogleMaps(
            incremental=True, known_companies=KnownCompanies(), verbose=False,
            shadow_observer=shadow_observer,
        )
        driver = Mock(current_window_handle="main")
        scraper.validate_result_link = lambda result, driver: ("1", "2", PLACE)
        scraper.get_title = lambda driver: "Acme"
        scraper.get_website_link = lambda driver: "https://acme.tn"
        scraper.get_phone_number = lambda driver: "71 123 456"
        scraper.get_cover_image = lambda driver: ""
        scraper.get_rating_in_card = lambda driver: "5"
        scraper.get_privacy_price = lambda driver: ""
        scraper.get_category = lambda driver: "Software company"
        scraper.get_address = lambda driver: "Tunis"
        scraper.get_working_hours = lambda driver: ""
        scraper.get_menu_link = lambda driver: ""
        scraper.get_related_images_list = lambda driver: ""
        scraper.get_about_description = lambda driver: {}
        scraper._web_pattern_scraper.find_patterns = lambda *args: {}
        scraper.reset_driver_for_next_run = lambda result, driver: None
        scraper._file_creator.create = Mock()
        return scraper, driver

    def test_maps_shadow_exception_does_not_change_output_or_counters(self):
        failing = Mock()
        failing.observe.side_effect = RuntimeError("shadow unavailable")
        control, control_driver = self.configure_maps_scraper()
        shadowed, shadow_driver = self.configure_maps_scraper(failing)
        with patch("utils.google_maps_scraper.discovery_timestamp", return_value=STAMP):
            control_outcome = control._scrape_result_and_store(
                control_driver, "continue", "software Tunis", [1, 1],
            )
            shadow_outcome = shadowed._scrape_result_and_store(
                shadow_driver, "continue", "software Tunis", [1, 1],
            )
        self.assertEqual(control_outcome, shadow_outcome)
        self.assertEqual(
            control._file_creator.create.call_args.kwargs,
            shadowed._file_creator.create.call_args.kwargs,
        )
        self.assertEqual(control.resource_metrics(), shadowed.resource_metrics())

    def test_search_shadow_exception_preserves_byte_identical_export(self):
        class Driver:
            def quit(self):
                pass

        row = {
            "company_name": "Acme", "source_url": "https://acme.tn/about",
            "source_query": "software Tunis",
        }
        failing = Mock()
        failing.observe.side_effect = RuntimeError("shadow unavailable")
        outputs = []
        for observer in (None, failing):
            directory = self.root / ("control" if observer is None else "shadowed")
            directory.mkdir()
            query = directory / "queries.txt"
            query.write_text("software Tunis\n", encoding="utf-8")
            output = directory / "google_search_companies.csv"
            with patch(
                "utils.google_search_discovery.search_query",
                return_value=([row], ""),
            ), patch(
                "utils.google_search_discovery.discovery_timestamp",
                return_value=STAMP,
            ):
                discover_companies(
                    query_file=query, output_path=output, limit=1, delay=0,
                    incremental=True, driver_factory=lambda windowed=False: Driver(),
                    shadow_observer=observer,
                )
            outputs.append(output.read_bytes())
        self.assertEqual(outputs[0], outputs[1])
