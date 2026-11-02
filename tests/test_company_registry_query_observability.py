"""Offline Phase 6E per-query observability regressions."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.query_observability import build_query_observability_report
from company_registry.schema import MIGRATIONS
from company_registry.storage import initialize_registry, migrate_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-11-01T15:00:00+00:00"
QUERIES = [
    "software company Tunis Tunisia",
    "software agency Lyon France",
]


def row(index: int, **changes) -> dict:
    value = {
        "title": f"Observed Company {index}",
        "map_link": f"https://www.google.com/maps/place/x/data=!4m1!1sChIJ-6E-{index}",
        "webpage": f"https://observed-{index}.test/contact",
        "phone_number": f"+216 71 {index:06d}",
        "address": f"{index} Report Street, Tunis, Tunisia",
    }
    value.update(changes)
    return value


def create_historical_v6(database: Path, run_id: str = "historical") -> None:
    """Create a minimal historical run with a global decision but no query ledger."""
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for version, script in MIGRATIONS:
            if version > 6:
                break
            if version == 6:
                connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(script)
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, finished_at, created_at,
                new_company_limit, updated_at)
               VALUES (?, 'SCRAPE', 'SUCCESS', ?, ?, ?, 5, ?)""",
            (run_id, STAMP, STAMP, STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO companies
               (company_id, canonical_name, discovery_status, first_seen_at,
                last_seen_at, created_at, updated_at)
               VALUES ('historical-company', 'Historical Company', 'OBSERVED',
                       ?, ?, ?, ?)""",
            (STAMP, STAMP, STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO discovery_observations
               (observation_id, run_id, source_system, source_record_key,
                payload_hash, raw_payload_json, normalized_evidence_json,
                classification, resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES ('historical-observation', ?, 'GOOGLE_MAPS', 'row-1', ?,
                       ?, '{}', 'NEW', 'CREATE_COMPANY', 'historical-company',
                       NULL, '[]', '[]', '[]', '[]', 0, 'historical decision',
                       'historical', ?, ?)""",
            (
                run_id,
                "a" * 64,
                '{"source_query":"software company Tunis Tunisia"}',
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO discovery_run_decisions
               (decision_id, run_id, observation_id, classification,
                resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES ('historical-decision', ?, 'historical-observation',
                       'NEW', 'CREATE_COMPANY', 'historical-company', NULL,
                       '[]', '[]', '[]', '[]', 0, 'historical decision',
                       'historical', ?, ?)""",
            (run_id, STAMP, STAMP),
        )
        connection.commit()
    finally:
        connection.close()


class QueryObservabilityTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "exports"
        self.reports = self.root / "reports"
        initialize_registry(self.database)

    def session(
        self,
        run_id="observability",
        *,
        global_limit=10,
        per_query=2,
        resume=False,
    ):
        session = AuthoritativeMapsSession(
            self.database,
            self.exports,
            run_id=None if resume else run_id,
            resume_run_id=run_id if resume else None,
            new_company_limit=global_limit,
            max_new_per_query=per_query,
        )
        session.configure_queries(QUERIES)
        return session

    @staticmethod
    def observation(index: int, query_index=0, **changes):
        return maps_observation(
            row(index, **changes), query=QUERIES[query_index], observed_at=STAMP,
        )

    def test_multi_query_counts_duplicates_review_rejection_and_locations(self):
        seed = AuthoritativeMapsSession(
            self.database, self.exports, run_id="seed-known", new_company_limit=1,
        )
        seed.configure_queries(["seed query"])
        seed.resolve(
            self.observation(99), RunAcceptanceBudget(1), query_index=0,
        )
        seed.fail("INTERRUPTED")

        session = self.session(per_query=2)
        budget = RunAcceptanceBudget(10)

        session.start_query(0)
        session.record_discovered_results(
            0, ["place-1", "place-1", "place-2", "known", "noise"],
        )
        first = session.resolve(self.observation(1), budget, query_index=0)
        session.record_result_outcome(0, "place-1", inspected=True, outcome=first.outcome)
        duplicate = session.resolve(self.observation(1), budget, query_index=0)
        session.record_result_outcome(0, "place-1", inspected=True, outcome=duplicate.outcome)
        review = session.resolve(
            self.observation(
                2,
                title="Unrelated Entity",
                webpage="https://observed-1.test/about",
            ),
            budget,
            query_index=0,
        )
        session.record_result_outcome(0, "place-2", inspected=True, outcome=review.outcome)
        known = session.resolve(self.observation(99), budget, query_index=0)
        session.record_result_outcome(0, "known", inspected=True, outcome=known.outcome)
        session.record_result_outcome(
            0, "noise", outcome="REJECTED", rejection_reason="non_place_result_url",
        )
        session.finish_query(0, "COMPLETED")

        session.start_query(1)
        session.record_discovered_results(1, ["place-3"])
        second = session.resolve(self.observation(3, query_index=1), budget, query_index=1)
        session.record_result_outcome(1, "place-3", inspected=True, outcome=second.outcome)
        session.finish_query(1, "COMPLETED")

        report = build_query_observability_report(
            self.database, session.run_id, self.reports,
        )
        tunis, lyon = report["queries"]
        self.assertEqual(
            (tunis["query_index"], tunis["target_location"]),
            (0, "Tunis, Tunisia"),
        )
        self.assertEqual(
            (
                tunis["results_discovered"], tunis["detail_pages_inspected"],
                tunis["new_companies_committed"], tunis["known_records"],
                tunis["duplicate_records"],
                tunis["review_only_records"], tunis["rejected_records"],
            ),
            (4, 3, 1, 1, 1, 1, 1),
        )
        self.assertEqual(lyon["target_location"], "Lyon, France")
        self.assertEqual(lyon["new_companies_committed"], 1)
        self.assertEqual(report["totals"]["new_companies_committed"], 2)
        connection = open_registry(self.database)
        try:
            durable = connection.execute(
                "SELECT count(*) FROM discovery_run_query_decisions WHERE run_id=?",
                (session.run_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(
            durable,
            report["totals"]["new_companies_committed"]
            + report["totals"]["known_records"]
            + report["totals"]["review_only_records"],
        )
        self.assertIn("durable_unique_results", tunis["durability"]["result_counters"])

    def test_interruption_resume_is_idempotent_and_updates_latest_status(self):
        first = self.session(per_query=1)
        budget = RunAcceptanceBudget(10)
        first.start_query(0)
        first.record_discovered_results(0, ["same-result"])
        outcome = first.resolve(self.observation(1), budget, query_index=0)
        first.record_result_outcome(0, "same-result", inspected=True, outcome=outcome.outcome)
        first.fail("INTERRUPTED")

        interrupted = build_query_observability_report(
            self.database, first.run_id, self.reports,
        )
        self.assertEqual(interrupted["queries"][0]["status"], "INTERRUPTED")

        resumed = self.session(per_query=1, resume=True)
        resumed.start_query(0)
        resumed.record_discovered_results(0, ["same-result", "same-result"])
        replay = resumed.resolve(
            self.observation(1), RunAcceptanceBudget(10, committed=1), query_index=0,
        )
        resumed.record_result_outcome(0, "same-result", inspected=True, outcome=replay.outcome)
        resumed.finish_query(0, "COMPLETED")
        report = build_query_observability_report(
            self.database, resumed.run_id, self.reports,
        )
        query = report["queries"][0]
        self.assertEqual(query["attempt_count"], 2)
        self.assertEqual(query["results_discovered"], 1)
        self.assertEqual(query["detail_pages_inspected"], 1)
        self.assertEqual(query["new_companies_committed"], 1)
        self.assertEqual(query["duplicate_records"], 1)
        self.assertEqual(query["status"], "COMPLETED")
        self.assertEqual(
            query["counter_availability"]["results_discovered"], "available",
        )
        self.assertEqual(
            report["counter_availability"]["per_query_result_counters"], "mixed",
        )

    def test_query_and_global_budget_exhaustion_are_reported_separately(self):
        per_query = self.session("query-cap", global_limit=4, per_query=1)
        budget = RunAcceptanceBudget(4)
        per_query.start_query(0)
        accepted = per_query.resolve(self.observation(1), budget, query_index=0)
        limited = per_query.resolve(self.observation(2), budget, query_index=0)
        per_query.record_result_outcome(0, "accepted", inspected=True, outcome=accepted.outcome)
        per_query.record_result_outcome(0, "limited", inspected=True, outcome=limited.outcome)
        per_query.finish_query(0, "COMPLETED")
        report = build_query_observability_report(
            self.database, per_query.run_id, self.reports,
        )
        self.assertEqual(report["queries"][0]["per_query_budget_remaining"], 0)
        self.assertTrue(report["queries"][0]["budget_exhausted"])
        self.assertFalse(report["global_budget"]["exhausted"])

        global_cap = self.session("global-cap", global_limit=1, per_query=2)
        global_budget = RunAcceptanceBudget(1)
        global_cap.start_query(0)
        global_cap.resolve(self.observation(10), global_budget, query_index=0)
        global_cap.finish_query(0, "COMPLETED")
        global_cap.start_query(1)
        limited = global_cap.resolve(
            self.observation(11, query_index=1), global_budget, query_index=1,
        )
        self.assertEqual(limited.outcome, "limit")
        global_cap.record_result_outcome(1, "global-limited", inspected=True, outcome="limit")
        global_cap.finish_query(1, "COMPLETED")
        global_report = build_query_observability_report(
            self.database, global_cap.run_id, self.reports,
        )
        self.assertTrue(global_report["global_budget"]["exhausted"])
        self.assertEqual(global_report["queries"][1]["new_companies_committed"], 0)

    def test_report_is_read_only_deterministic_and_human_readable(self):
        session = self.session(per_query=None)
        session.start_query(0)
        session.finish_query(0, "NO_RESULTS")
        self.exports.mkdir(parents=True, exist_ok=True)
        export = self.exports / "existing.csv"
        manifest = self.exports / "existing.manifest.json"
        export.write_bytes(b"company_id\nprotected\n")
        manifest.write_bytes(b'{"protected":true}\n')
        protected = {export: export.read_bytes(), manifest: manifest.read_bytes()}
        before = self.database.read_bytes()
        first = build_query_observability_report(
            self.database, session.run_id, self.reports,
        )
        second = build_query_observability_report(
            self.database, session.run_id, self.reports,
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["json_sha256"], second["json_sha256"])
        self.assertEqual(first["text_sha256"], second["text_sha256"])
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(
            protected, {path: path.read_bytes() for path in protected},
        )
        self.assertIn("IDX | STATUS | LOCATION", first["text"].read_text(encoding="utf-8"))
        self.assertIn("query-observability-v1", str(first["json"]))

    def test_v8_measured_zero_is_not_reported_as_historical_unknown(self):
        session = self.session("measured-zero", per_query=None)
        session.start_query(0)
        session.finish_query(0, "NO_RESULTS")

        report = build_query_observability_report(
            self.database, session.run_id, self.reports,
        )
        measured, not_attempted = report["queries"]
        for field in (
            "results_discovered", "detail_pages_inspected", "duplicate_records",
            "rejected_records", "new_companies_committed", "known_records",
            "review_only_records", "updated_records",
        ):
            self.assertEqual(measured[field], 0, field)
        self.assertEqual(measured["attempt_count"], 1)
        self.assertEqual(measured["status"], "NO_RESULTS")
        self.assertIsNone(not_attempted["results_discovered"])
        self.assertEqual(not_attempted["new_companies_committed"], 0)
        self.assertEqual(
            measured["counter_availability"]["results_discovered"], "available",
        )
        self.assertEqual(
            not_attempted["counter_availability"]["results_discovered"],
            "not_measured",
        )

    def test_historical_v6_does_not_fabricate_unpersisted_counters(self):
        historical = self.root / "historical-v6.db"
        create_historical_v6(historical)

        report = build_query_observability_report(
            historical, "historical", self.reports, queries=QUERIES,
        )
        self.assertEqual(report["query_identity_source"], "caller_supplied_not_persisted")
        query = report["queries"][0]
        self.assertIsNone(query["results_discovered"])
        self.assertIsNone(query["detail_pages_inspected"])
        self.assertIsNone(query["duplicate_records"])
        self.assertIsNone(query["rejected_records"])
        self.assertIsNone(query["status"])
        self.assertIsNone(query["new_companies_committed"])
        self.assertIsNone(query["known_records"])
        self.assertIsNone(query["review_only_records"])
        self.assertEqual(report["totals"]["results_discovered"], None)
        self.assertEqual(
            report["counter_availability"]["per_query_decision_classifications"],
            "unavailable_historical",
        )

    def test_migrated_v6_preserves_global_totals_but_per_query_is_unknown(self):
        historical = self.root / "migrated-historical.db"
        create_historical_v6(historical)
        migrate_registry(historical)
        migrate_registry(historical)
        before = historical.read_bytes()

        first = build_query_observability_report(
            historical, "historical", self.reports, queries=QUERIES,
        )
        second = build_query_observability_report(
            historical, "historical", self.reports, queries=QUERIES,
        )

        self.assertEqual(first["registry_schema_version"], 8)
        self.assertEqual(first["query_identity_source"], "caller_supplied_not_persisted")
        for query in first["queries"]:
            for field in (
                "results_discovered", "detail_pages_inspected",
                "new_companies_committed", "known_records", "duplicate_records",
                "review_only_records", "rejected_records", "updated_records",
            ):
                self.assertIsNone(query[field], field)
        self.assertEqual(first["global_budget"]["committed_new"], 1)
        self.assertEqual(
            first["global_authoritative_totals"]["new_companies_committed"], 1,
        )
        self.assertIsNone(first["totals"]["new_companies_committed"])
        self.assertTrue(first["historical_data_limitations"])
        self.assertIn(
            "N/A (historical data unavailable)",
            first["text"].read_text(encoding="utf-8"),
        )
        self.assertTrue(second["reused"])
        self.assertEqual(first["json_sha256"], second["json_sha256"])
        self.assertEqual(first["text_sha256"], second["text_sha256"])
        self.assertEqual(before, historical.read_bytes())

    def test_mixed_historical_and_new_runs_use_independent_availability(self):
        mixed = self.root / "mixed.db"
        create_historical_v6(mixed)
        migrate_registry(mixed)

        fresh = AuthoritativeMapsSession(
            mixed, self.exports, run_id="fresh-v8", new_company_limit=5,
        )
        fresh.configure_queries(QUERIES)
        fresh.start_query(0)
        fresh.finish_query(0, "NO_RESULTS")

        historical = build_query_observability_report(
            mixed, "historical", self.reports, queries=QUERIES,
        )
        current = build_query_observability_report(
            mixed, "fresh-v8", self.reports,
        )
        self.assertIsNone(historical["queries"][0]["new_companies_committed"])
        self.assertIsNone(historical["queries"][0]["results_discovered"])
        self.assertEqual(current["queries"][0]["new_companies_committed"], 0)
        self.assertEqual(current["queries"][0]["results_discovered"], 0)
        self.assertEqual(
            current["queries"][0]["counter_availability"]["known_records"],
            "available",
        )
