"""Offline Phase 3C.2 Search and cross-source authoritative regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from csv import DictReader
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from company_registry.discovery_adapter import RegistryUnavailableError
from company_registry.exports import export_new_companies
from company_registry.maps_authoritative import (
    AuthoritativeMapsSession,
    AuthoritativeResumeError,
    maps_observation,
)
from company_registry.schema import MIGRATION_1, MIGRATION_2, MIGRATION_3
from company_registry.search_authoritative import (
    AuthoritativeSearchSession,
    search_observation,
)
from company_registry.storage import initialize_registry, open_registry
from utils.google_search_discovery import run_search_discovery
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-10-09T12:00:00+00:00"


class Driver:
    def quit(self):
        pass


def search_row(name="Acme", domain="acme.test", **changes):
    row = {
        "company_name": name,
        "source_url": f"https://{domain}/about",
        "source_query": "software Tunis",
    }
    row.update(changes)
    return row


def maps_row(name="Acme", domain="acme.test", place="ChIJ-Acme-Tunis", **changes):
    row = {
        "title": name,
        "map_link": f"https://google.com/maps/place/x/data=!4m1!1s{place}",
        "webpage": f"https://{domain}/contact",
        "phone_number": "+216 71 123 456",
        "address": "1 Registry Street, Tunis",
    }
    row.update(changes)
    return row


class SearchAuthoritativeIntegrationTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "authoritative-search"
        self.query = self.root / "queries.txt"
        self.query.write_text("software Tunis\nsoftware Sousse\n", encoding="utf-8")
        self.legacy_output = self.root / "protected-legacy.csv"
        initialize_registry(self.database)

    def scalar(self, sql, parameters=()):
        connection = open_registry(self.database)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def search_session(self, run_id="search-run"):
        return AuthoritativeSearchSession(
            self.database, self.exports, run_id=run_id,
        )

    def maps_session(self, run_id="maps-run"):
        return AuthoritativeMapsSession(
            self.database, self.root / "authoritative-maps", run_id=run_id,
        )

    def search_observation(self, **changes):
        return search_observation(
            search_row(**changes), query="software Tunis", observed_at=STAMP,
        )

    def maps_observation(self, **changes):
        return maps_observation(
            maps_row(**changes), query="software Tunis", observed_at=STAMP,
        )

    def test_mocked_search_creates_company_and_sqlite_derived_export_only(self):
        with patch(
            "utils.google_search_discovery.search_query",
            return_value=([search_row()], ""),
        ), patch(
            "utils.google_search_discovery.discovery_timestamp", return_value=STAMP,
        ):
            result = run_search_discovery(
                discovery_mode="authoritative-canary",
                registry_database=self.database,
                registry_run_id="mocked-search",
                authoritative_export_directory=self.exports,
                query_file=self.query,
                output_path=self.legacy_output,
                limit=1,
                delay=0,
                driver_factory=lambda windowed=False: Driver(),
            )

        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(result["authoritative_export"]["rows"], 1)
        self.assertFalse(self.legacy_output.exists())
        with result["authoritative_export"]["csv"].open(
            newline="", encoding="utf-8-sig",
        ) as handle:
            exported = next(DictReader(handle))
        self.assertEqual(exported["source_system"], "GOOGLE_SEARCH")
        self.assertTrue(exported["source_record_key"].startswith("domain:"))
        self.assertEqual(exported["classification"], "NEW")

    def test_maps_then_search_and_search_then_maps_share_company_registry(self):
        maps = self.maps_session("maps-first")
        mapped = maps.resolve(self.maps_observation(), RunAcceptanceBudget(1))
        search = self.search_session("search-second")
        found = search.resolve(self.search_observation(), RunAcceptanceBudget(1))
        self.assertEqual(mapped.outcome, "new")
        self.assertEqual(found.outcome, "known")
        self.assertEqual(
            mapped.decision.resolution.company_id,
            found.decision.resolution.company_id,
        )

        other_search = self.search_session("search-first-other")
        discovered = other_search.resolve(
            self.search_observation(name="Beta", domain="beta.test"),
            RunAcceptanceBudget(1),
        )
        other_maps = self.maps_session("maps-second-other")
        branched = other_maps.resolve(
            self.maps_observation(
                name="Beta", domain="beta.test", place="ChIJ-Beta-Tunis",
            ),
            RunAcceptanceBudget(1),
        )
        self.assertEqual(discovered.outcome, "new")
        self.assertEqual(branched.outcome, "branch")
        self.assertEqual(
            discovered.decision.resolution.company_id,
            branched.decision.resolution.company_id,
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 2)

    def test_concurrent_maps_and_search_create_one_company_and_one_budget_slot(self):
        maps = self.maps_session("concurrent-maps")
        search = self.search_session("concurrent-search")
        maps_budget = RunAcceptanceBudget(1)
        search_budget = RunAcceptanceBudget(1)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(maps.resolve, self.maps_observation(), maps_budget),
                workers.submit(search.resolve, self.search_observation(), search_budget),
            ]
            outcomes = [future.result().outcome for future in futures]
        self.assertIn("new", outcomes)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(
            maps_budget.snapshot()["committed"]
            + search_budget.snapshot()["committed"],
            1,
        )

    def test_search_branch_shared_domain_and_invalid_identity_are_safe(self):
        session = self.search_session()
        budget = RunAcceptanceBudget(2)
        created = session.resolve(self.search_observation(), budget)
        branch = session.resolve(self.search_observation(
            place_id="ChIJ-Acme-Sousse",
            phone="+216 73 999 999",
            address="2 Registry Street, Sousse",
        ), budget)
        shared = session.resolve(self.search_observation(
            name="Unrelated Entity", domain="acme.test",
        ), budget)
        invalid = session.resolve(search_observation(
            {"company_name": "", "source_url": "", "source_query": "bad"},
            query="bad", observed_at=STAMP,
        ), budget)
        self.assertEqual(created.outcome, "new")
        self.assertEqual(branch.outcome, "branch")
        self.assertEqual(shared.outcome, "ambiguous")
        self.assertEqual(invalid.outcome, "quarantined")
        self.assertEqual(budget.snapshot()["committed"], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM branches"), 1)
        session.complete()
        with session.export_result["csv"].open(newline="", encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(DictReader(handle))), 1)

    def test_same_run_cross_run_and_failed_commit_budget_semantics(self):
        observation = self.search_observation()
        first = self.search_session("first")
        first_budget = RunAcceptanceBudget(1)
        self.assertEqual(first.resolve(observation, first_budget).outcome, "new")
        self.assertEqual(first.resolve(observation, first_budget).outcome, "same_run")
        second = self.search_session("second")
        second_budget = RunAcceptanceBudget(1)
        self.assertEqual(second.resolve(observation, second_budget).outcome, "known")
        self.assertEqual(first_budget.snapshot()["committed"], 1)
        self.assertEqual(second_budget.snapshot()["committed"], 0)

        failing = self.search_session("failing")
        failing.adapter.commit = Mock(side_effect=RegistryUnavailableError("locked"))
        failed_budget = RunAcceptanceBudget(1)
        with self.assertRaisesRegex(RegistryUnavailableError, "locked"):
            failing.resolve(
                self.search_observation(name="Beta", domain="beta.test"),
                failed_budget,
            )
        self.assertEqual(failed_budget.snapshot()["reserved"], 0)
        self.assertEqual(failed_budget.snapshot()["committed"], 0)

    def test_global_limit_across_queries_and_no_legacy_checkpoint(self):
        calls = []

        def fake_search(driver, query, limit, timeout, verbose=False, start=0):
            calls.append((query, start))
            index = len(calls)
            return ([search_row(f"Company {index}", f"company-{index}.test")], "")

        with patch(
            "utils.google_search_discovery.search_query", side_effect=fake_search,
        ), patch(
            "utils.google_search_discovery.discovery_timestamp", return_value=STAMP,
        ):
            result = run_search_discovery(
                discovery_mode="authoritative",
                registry_database=self.database,
                registry_run_id="global-limit",
                authoritative_export_directory=self.exports,
                query_file=self.query,
                output_path=self.legacy_output,
                limit=1,
                delay=0,
                driver_factory=lambda windowed=False: Driver(),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["authoritative_export"]["rows"], 1)
        self.assertFalse(self.legacy_output.exists())

    def test_invalid_registry_and_existing_cross_source_run_rejected_pre_browser(self):
        driver_factory = Mock(side_effect=AssertionError("browser must not start"))
        with self.assertRaises(RegistryUnavailableError):
            run_search_discovery(
                discovery_mode="authoritative",
                registry_database=self.root / "missing.db",
                query_file=self.query,
                limit=1,
                driver_factory=driver_factory,
            )
        driver_factory.assert_not_called()

        old = self.root / "v3.db"
        connection = sqlite3.connect(old)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        with self.assertRaises(RegistryUnavailableError):
            AuthoritativeSearchSession(old, self.exports)

        maps = self.maps_session("shared-run-id")
        with self.assertRaises(AuthoritativeResumeError):
            AuthoritativeSearchSession(
                self.database, self.exports, run_id=maps.run_id,
            )
        maps.fail()

    def test_interruption_marks_failed_and_export_failure_is_recoverable(self):
        with patch(
            "utils.google_search_discovery.search_query",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_search_discovery(
                    discovery_mode="authoritative",
                    registry_database=self.database,
                    registry_run_id="interrupted",
                    authoritative_export_directory=self.exports,
                    query_file=self.query,
                    limit=1,
                    delay=0,
                    driver_factory=lambda windowed=False: Driver(),
                )
        self.assertEqual(self.scalar(
            "SELECT status FROM discovery_runs WHERE run_id='interrupted'"
        ), "FAILED")

        session = self.search_session("export-failure")
        session.resolve(self.search_observation(), RunAcceptanceBudget(1))
        with patch(
            "company_registry.maps_authoritative.export_new_companies",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                session.complete()
        self.assertEqual(self.scalar(
            "SELECT count(*) FROM discovery_run_decisions "
            "WHERE run_id='export-failure' AND classification='NEW'"
        ), 1)
        rebuilt = export_new_companies(
            self.database, "export-failure", self.exports,
        )
        self.assertEqual(rebuilt["rows"], 1)

