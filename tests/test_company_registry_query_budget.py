"""Offline Phase 6D durable per-query NEW-company budget regressions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from company_registry.maps_authoritative import (
    AuthoritativeMapsSession,
    AuthoritativeResumeError,
    maps_observation,
)
from company_registry.service import RegistryService
from company_registry.storage import initialize_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget
from utils.threading_controller import FastSearchAlgo


STAMP = "2026-11-01T12:00:00+00:00"
QUERIES = ["software Tunis", "software Sfax"]


def row(index: int, **changes) -> dict:
    value = {
        "title": f"Company {index}",
        "map_link": (
            "https://www.google.com/maps/place/Company/"
            f"data=!4m1!1sChIJ-Phase6D-{index}"
        ),
        "webpage": f"https://company-{index}.test/contact",
        "phone_number": f"+216 71 {index:06d}",
        "address": f"{index} Durable Street, Tunis",
    }
    value.update(changes)
    return value


class DurablePerQueryBudgetTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "exports"
        initialize_registry(self.database)

    def session(
        self,
        run_id: str = "phase6d",
        *,
        global_limit: int = 20,
        per_query: int | None = 2,
        resume: bool = False,
    ) -> AuthoritativeMapsSession:
        return AuthoritativeMapsSession(
            self.database,
            self.exports,
            run_id=None if resume else run_id,
            resume_run_id=run_id if resume else None,
            new_company_limit=global_limit,
            max_new_per_query=per_query,
        )

    @staticmethod
    def observation(index: int, query: str = QUERIES[0], **changes):
        return maps_observation(
            row(index, **changes), query=query, observed_at=STAMP,
        )

    def scalar(self, sql: str, parameters=()):
        connection = open_registry(self.database)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def test_omitted_limit_preserves_existing_behavior_and_hash_contract(self):
        session = self.session(per_query=None)
        session.configure_queries(QUERIES)
        outcomes = [
            session.resolve(
                self.observation(index), RunAcceptanceBudget(20), query_index=0,
            ).outcome
            for index in range(1, 4)
        ]
        self.assertEqual(outcomes, ["new", "new", "new"])
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM discovery_run_queries WHERE run_id=?",
                (session.run_id,),
            ),
            2,
        )

    def test_per_query_limit_and_global_limit_are_both_enforced(self):
        session = self.session(global_limit=3, per_query=2)
        session.configure_queries(QUERIES)
        budget = RunAcceptanceBudget(3)
        outcomes = [
            session.resolve(self.observation(1), budget, query_index=0).outcome,
            session.resolve(self.observation(2), budget, query_index=0).outcome,
            session.resolve(self.observation(3), budget, query_index=0).outcome,
            session.resolve(
                self.observation(4, QUERIES[1]), budget, query_index=1,
            ).outcome,
            session.resolve(
                self.observation(5, QUERIES[1]), budget, query_index=1,
            ).outcome,
        ]
        self.assertEqual(outcomes, ["new", "new", "query_limit", "new", "limit"])
        self.assertEqual(session.query_budget_snapshot(0).committed_new, 2)
        self.assertEqual(session.query_budget_snapshot(1).committed_new, 1)
        self.assertEqual(session.budget_snapshot().committed_new, 3)

    def test_concurrent_workers_cannot_overspend_one_query(self):
        session = self.session(per_query=3)
        session.configure_queries(QUERIES)
        budget = RunAcceptanceBudget(20)
        observations = [self.observation(index) for index in range(1, 21)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            outcomes = list(workers.map(
                lambda item: session.resolve(
                    item, budget, query_index=0,
                ).outcome,
                observations,
            ))
        self.assertEqual(outcomes.count("new"), 3)
        self.assertEqual(outcomes.count("query_limit"), 17)
        self.assertEqual(session.query_budget_snapshot(0).committed_new, 3)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 3)

    def test_duplicates_known_review_and_rejected_do_not_consume_query_budget(self):
        session = self.session(per_query=3)
        session.configure_queries(QUERIES)
        budget = RunAcceptanceBudget(20)
        created = session.resolve(self.observation(1), budget, query_index=0)
        duplicate = session.resolve(self.observation(1), budget, query_index=0)
        ambiguous = session.resolve(
            self.observation(
                2,
                title="Unrelated Entity",
                webpage="https://company-1.test/about",
            ),
            budget,
            query_index=0,
        )
        rejected = session.resolve(
            self.observation(
                3, title="", map_link="", webpage="", phone_number="", address="",
            ),
            budget,
            query_index=0,
        )
        self.assertEqual(
            [created.outcome, duplicate.outcome, ambiguous.outcome, rejected.outcome],
            ["new", "same_run", "ambiguous", "quarantined"],
        )
        self.assertEqual(session.query_budget_snapshot(0).committed_new, 1)

    def test_interruption_resume_preserves_count_and_rejects_limit_change(self):
        first = self.session(per_query=2)
        first.configure_queries(QUERIES)
        first.resolve(
            self.observation(1), RunAcceptanceBudget(20), query_index=0,
        )
        first.fail("INTERRUPTED")

        changed = self.session(per_query=3, resume=True)
        with self.assertRaisesRegex(RuntimeError, "configuration|limit"):
            changed.configure_queries(QUERIES)

        resumed = self.session(per_query=2, resume=True)
        resumed.configure_queries(QUERIES)
        self.assertEqual(resumed.query_budget_snapshot(0).committed_new, 1)
        budget = RunAcceptanceBudget(20, committed=1)
        self.assertEqual(
            resumed.resolve(
                self.observation(2), budget, query_index=0,
            ).outcome,
            "new",
        )
        self.assertEqual(
            resumed.resolve(
                self.observation(3), budget, query_index=0,
            ).outcome,
            "query_limit",
        )

    def test_crash_rolls_back_entity_decision_and_query_counter(self):
        session = self.session(per_query=2)
        session.configure_queries(QUERIES)

        def fail(point):
            if point == "before_commit":
                raise RuntimeError("simulated crash")

        service = RegistryService(self.database, failure_injector=fail)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            service.resolve_with_metadata(
                session.run_id,
                self.observation(1),
                lease_owner=session.lease_owner,
                query_index=0,
            )
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 0)
        self.assertEqual(session.query_budget_snapshot(0).committed_new, 0)

    def test_query_ledger_constraints_and_configuration_are_durable(self):
        session = self.session(per_query=2)
        session.configure_queries(QUERIES)
        connection = open_registry(self.database)
        try:
            rows = connection.execute(
                """SELECT query_index, query_text, committed_new_count
                     FROM discovery_run_queries
                    WHERE run_id=? ORDER BY query_index""",
                (session.run_id,),
            ).fetchall()
            self.assertEqual(
                [tuple(item) for item in rows],
                [(0, QUERIES[0], 0), (1, QUERIES[1], 0)],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE discovery_run_queries SET committed_new_count=-1
                         WHERE run_id=? AND query_index=0""",
                    (session.run_id,),
                )
        finally:
            connection.close()

    def test_exhausted_query_does_not_stop_subsequent_queries(self):
        calls = []

        class OfflineWorker:
            def __init__(worker, **kwargs):
                worker.session = kwargs["authoritative_session"]
                worker.budget = kwargs["acceptance_budget"]
                worker._current_query_index = None

            def start_scrapper(worker, query):
                query_index = worker._current_query_index
                calls.append(query_index)
                base = query_index * 10
                first = worker.session.resolve(
                    DurablePerQueryBudgetTests.observation(base + 1, query),
                    worker.budget,
                    query_index=query_index,
                )
                second = worker.session.resolve(
                    DurablePerQueryBudgetTests.observation(base + 2, query),
                    worker.budget,
                    query_index=query_index,
                )
                self.assertEqual(first.outcome, "new")
                self.assertEqual(second.outcome, "query_limit")
                return "COMPLETED"

            def quit_driver(worker):
                pass

            def resource_metrics(worker):
                return {}

            def query_states(worker):
                return []

        coordinator = FastSearchAlgo(
            workers=1,
            result_range=10,
            max_new_per_query=1,
            discovery_mode="authoritative-canary",
            registry_database=self.database,
            registry_run_id="coordinator",
            authoritative_export_directory=self.exports,
            verbose=False,
        )
        with patch("utils.threading_controller.GoogleMaps", OfflineWorker):
            result = coordinator.fast_search_algorithm(QUERIES)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(result["authoritative_export"]["rows"], 2)
