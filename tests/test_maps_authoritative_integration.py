"""Offline Phase 3C.1 Maps authoritative-integration regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from company_registry.discovery_adapter import RegistryUnavailableError
from company_registry.maps_authoritative import (
    AuthoritativeMapsSession,
    AuthoritativeResumeError,
    UnsafeAuthoritativePathError,
    maps_observation,
)
from company_registry.models import Classification
from company_registry.schema import MIGRATION_1, MIGRATION_2, MIGRATION_3
from company_registry.storage import initialize_registry, open_registry
from utils.google_maps_scraper import GoogleMaps
from utils.run_acceptance_budget import RunAcceptanceBudget
from utils.threading_controller import FastSearchAlgo


STAMP = "2026-10-08T12:00:00+00:00"


def row(index=1, **changes):
    values = {
        "title": f"Company {index}",
        "map_link": (
            "https://www.google.com/maps/place/Company/"
            f"data=!4m1!1sChIJ-Authoritative-{index}"
        ),
        "webpage": f"https://company-{index}.test/contact",
        "phone_number": f"+216 71 {index:06d}",
        "address": f"{index} Registry Street, Tunis",
    }
    values.update(changes)
    return values


class AuthoritativeMapsIntegrationTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "authoritative-exports"
        initialize_registry(self.database)

    def session(self, run_id="maps-test"):
        return AuthoritativeMapsSession(
            self.database, self.exports, run_id=run_id,
        )

    def observation(self, index=1, **changes):
        return maps_observation(
            row(index, **changes), query="software Tunis", observed_at=STAMP,
        )

    def scalar(self, sql, parameters=()):
        connection = open_registry(self.database)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def test_known_new_branch_and_review_semantics(self):
        session = self.session()
        budget = RunAcceptanceBudget(10)
        created = session.resolve(self.observation(1), budget)
        replay = session.resolve(self.observation(1), budget)
        branch = session.resolve(self.observation(
            2,
            title="Company 1",
            webpage="https://company-1.test/branch",
        ), budget)
        shared_domain = session.resolve(self.observation(
            3,
            title="Unrelated Legal Entity",
            webpage="https://company-1.test/about",
        ), budget)
        quarantined = session.resolve(self.observation(
            4, title="", map_link="", webpage="", phone_number="", address="",
        ), budget)

        self.assertEqual(created.outcome, "new")
        self.assertEqual(replay.outcome, "same_run")
        self.assertEqual(branch.outcome, "branch")
        self.assertEqual(shared_domain.outcome, "ambiguous")
        self.assertEqual(quarantined.outcome, "quarantined")
        self.assertEqual(branch.decision.resolution.company_id,
                         created.decision.resolution.company_id)
        self.assertNotEqual(branch.decision.resolution.branch_id,
                            created.decision.resolution.branch_id)
        self.assertIsNone(shared_domain.decision.resolution.company_id)
        self.assertEqual(budget.snapshot()["committed"], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM branches"), 2)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 4)

    def test_cross_run_replay_is_known_and_does_not_consume_budget(self):
        first = self.session("first")
        first_budget = RunAcceptanceBudget(1)
        created = first.resolve(self.observation(), first_budget)
        first.complete()
        second = self.session("second")
        second_budget = RunAcceptanceBudget(1)
        known = second.resolve(self.observation(), second_budget)
        self.assertEqual(created.outcome, "new")
        self.assertEqual(known.outcome, "known")
        self.assertEqual(known.decision.resolution.classification, Classification.KNOWN)
        self.assertEqual(first_budget.snapshot()["committed"], 1)
        self.assertEqual(second_budget.snapshot()["committed"], 0)

    def test_four_workers_same_company_commit_one_slot_and_one_entity(self):
        session = self.session()
        budget = RunAcceptanceBudget(5)
        observation = self.observation()
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(
                lambda _: session.resolve(observation, budget).outcome,
                range(16),
            ))
        self.assertEqual(outcomes.count("new"), 1)
        self.assertEqual(outcomes.count("same_run"), 15)
        self.assertEqual(budget.snapshot(), {
            "limit": 5, "reserved": 0, "committed": 1, "remaining": 4,
        })
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM branches"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 1)

    def test_global_five_limit_under_concurrency(self):
        session = self.session()
        budget = RunAcceptanceBudget(5)
        observations = [self.observation(index) for index in range(1, 41)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(
                lambda item: session.resolve(item, budget).outcome,
                observations,
            ))
        self.assertEqual(outcomes.count("new"), 5)
        self.assertEqual(outcomes.count("limit"), 35)
        self.assertEqual(budget.snapshot()["committed"], 5)
        self.assertEqual(budget.snapshot()["reserved"], 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 5)

    def test_coordinator_enforces_global_limit_and_publishes_sqlite_export(self):
        class OfflineWorker:
            def __init__(self, **kwargs):
                self.session = kwargs["authoritative_session"]
                self.budget = kwargs["acceptance_budget"]

            def start_scrapper(self, query):
                index = int(query.rsplit("-", 1)[1])
                observation = maps_observation(
                    row(index), query=query, observed_at=STAMP,
                )
                self.session.resolve(observation, self.budget)
                return "COMPLETED"

            def quit_driver(self):
                pass

            def resource_metrics(self):
                return {}

            def query_states(self):
                return []

        coordinator = FastSearchAlgo(
            workers=4,
            result_range=5,
            output_path=str(self.root / "legacy-output-must-not-be-written"),
            discovery_mode="authoritative-canary",
            registry_database=self.database,
            registry_run_id="coordinator-run",
            authoritative_export_directory=self.exports,
            verbose=False,
        )
        with patch("utils.threading_controller.GoogleMaps", OfflineWorker):
            result = coordinator.fast_search_algorithm(
                [f"query-{index}" for index in range(1, 21)]
            )
        self.assertEqual(
            result["run_observability"]["global_acceptance_budget"]["committed"],
            5,
        )
        self.assertEqual(result["authoritative_export"]["rows"], 5)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 5)
        self.assertFalse((self.root / "legacy-output-must-not-be-written").exists())

    def test_failed_commit_releases_reservation_and_never_becomes_review(self):
        session = self.session()
        budget = RunAcceptanceBudget(1)
        session.adapter.commit = Mock(side_effect=RegistryUnavailableError("locked"))
        with self.assertRaisesRegex(RegistryUnavailableError, "locked"):
            session.resolve(self.observation(), budget)
        self.assertEqual(budget.snapshot(), {
            "limit": 1, "reserved": 0, "committed": 0, "remaining": 1,
        })
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 0)

    def test_export_failure_is_rebuildable_from_committed_sqlite(self):
        session = self.session()
        session.resolve(self.observation(), RunAcceptanceBudget(1))
        with patch(
            "company_registry.maps_authoritative.export_new_companies",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                session.complete()
        self.assertEqual(self.scalar(
            "SELECT count(*) FROM discovery_run_decisions WHERE classification='NEW'"
        ), 1)
        from company_registry.exports import export_new_companies
        rebuilt = export_new_companies(self.database, session.run_id, self.exports)
        self.assertEqual(rebuilt["rows"], 1)

    def test_mocked_maps_writes_sqlite_and_never_calls_legacy_writer(self):
        session = self.session()
        budget = RunAcceptanceBudget(1)
        scraper = GoogleMaps(
            authoritative_session=session,
            acceptance_budget=budget,
            result_range=1,
            output_path=str(self.root / "worker-scratch"),
            verbose=False,
        )
        scraper._GoogleMaps__pprint_override = lambda *args, **kwargs: None
        scraper.validate_result_link = lambda result, driver: (
            "36.8", "10.1", row()["map_link"],
        )
        scraper.get_title = lambda driver: row()["title"]
        scraper.get_website_link = lambda driver: row()["webpage"]
        scraper.get_phone_number = lambda driver: row()["phone_number"]
        scraper.get_cover_image = lambda driver: ""
        scraper.get_rating_in_card = lambda driver: "5"
        scraper.get_privacy_price = lambda driver: ""
        scraper.get_category = lambda driver: "Software company"
        scraper.get_address = lambda driver: row()["address"]
        scraper.get_working_hours = lambda driver: ""
        scraper.get_menu_link = lambda driver: ""
        scraper.get_related_images_list = lambda driver: ""
        scraper.get_about_description = lambda driver: {}
        scraper._web_pattern_scraper.find_patterns = lambda *args: {}
        scraper.reset_driver_for_next_run = lambda result, driver: None
        scraper._file_creator.create = Mock()

        self.assertEqual(
            scraper._scrape_result_and_store(Mock(), "continue", "query", [1, 1]),
            "new",
        )
        scraper._file_creator.create.assert_not_called()
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(scraper.resource_metrics()["companies_persisted"], 1)

    def test_outdated_missing_production_paths_and_resume_are_rejected(self):
        missing = self.root / "missing.db"
        with self.assertRaises(RegistryUnavailableError):
            AuthoritativeMapsSession(missing, self.exports)
        old = self.root / "v3.db"
        connection = sqlite3.connect(old)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        with self.assertRaises(RegistryUnavailableError):
            AuthoritativeMapsSession(old, self.exports)

        session = self.session("durable")
        with self.assertRaises(AuthoritativeResumeError):
            self.session("durable")
        session.fail()

        project = Path(__file__).resolve().parents[1]
        with self.assertRaises(UnsafeAuthoritativePathError):
            AuthoritativeMapsSession(project / "data/company_registry.db", self.exports)
        with self.assertRaises(UnsafeAuthoritativePathError):
            AuthoritativeMapsSession(
                self.database, project / "CSV_FILES" / "authoritative",
            )

    def test_production_opt_in_is_explicit_authoritative_and_v6_only(self):
        with patch(
            "company_registry.maps_authoritative.DEFAULT_DATABASE", self.database,
        ):
            with self.assertRaisesRegex(
                UnsafeAuthoritativePathError, "explicit production activation",
            ):
                AuthoritativeMapsSession(self.database, self.exports)
            self.assertEqual(self.scalar("SELECT count(*) FROM discovery_runs"), 0)

            with self.assertRaisesRegex(
                UnsafeAuthoritativePathError, "requires discovery mode 'authoritative'",
            ):
                AuthoritativeMapsSession(
                    self.database, self.exports,
                    allow_production_registry=True,
                )
            self.assertEqual(self.scalar("SELECT count(*) FROM discovery_runs"), 0)

            session = AuthoritativeMapsSession(
                self.database, self.exports, mode="authoritative",
                run_id="disposable-production-opt-in",
                allow_production_registry=True,
            )
            self.assertEqual(self.scalar("SELECT count(*) FROM discovery_runs"), 1)
            session.fail()

        old = self.root / "production-v2.db"
        connection = sqlite3.connect(old)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        before = old.read_bytes()
        with patch("company_registry.maps_authoritative.DEFAULT_DATABASE", old):
            with self.assertRaisesRegex(
                UnsafeAuthoritativePathError, "schema-v6",
            ):
                AuthoritativeMapsSession(
                    old, self.exports, mode="authoritative",
                    allow_production_registry=True,
                )
        self.assertEqual(old.read_bytes(), before)

    def test_legacy_origin_and_null_first_seen_are_preserved(self):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO companies VALUES
               ('legacy-company', 'Legacy Co', 'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branches VALUES
               ('legacy-branch', 'legacy-company', 'Legacy Co',
                'place_id:chij-legacy', NULL, NULL, NULL,
                'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branch_identities VALUES
               ('legacy-place', 'legacy-branch', 'GOOGLE_MAPS_PLACE_ID',
                'place_id:chij-legacy', 'place_id:chij-legacy', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        session = self.session()
        outcome = session.resolve(self.observation(
            title="Legacy Co",
            map_link="https://google.com/maps/place/x/data=!4m1!1sChIJ-Legacy",
            webpage="", phone_number="", address="",
        ), RunAcceptanceBudget(1))
        self.assertIn(outcome.outcome, {"known", "updated"})
        self.assertNotEqual(outcome.decision.resolution.classification, Classification.NEW)
        connection = open_registry(self.database)
        company = connection.execute(
            "SELECT discovery_status, first_seen_at FROM companies WHERE company_id='legacy-company'"
        ).fetchone()
        branch = connection.execute(
            "SELECT discovery_status, first_seen_at FROM branches WHERE branch_id='legacy-branch'"
        ).fetchone()
        connection.close()
        self.assertEqual(tuple(company), ("LEGACY_UNKNOWN", None))
        self.assertEqual(tuple(branch), ("LEGACY_UNKNOWN", None))
