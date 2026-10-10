"""Focused SQLite-enforced NEW-company budget tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.service import RegistryService, get_run_budget_snapshot
from company_registry.storage import initialize_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-10-10T12:00:00+00:00"


class DurableNewBudgetTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        initialize_registry(self.database)

    def session(self, run_id="run", limit=1):
        return AuthoritativeMapsSession(
            self.database,
            self.root / f"exports-{run_id}",
            run_id=run_id,
            new_company_limit=limit,
        )

    @staticmethod
    def observation(index=1, **changes):
        row = {
            "title": f"Budget Company {index}",
            "map_link": (
                "https://google.com/maps/place/x/"
                f"data=!4m1!1sChIJ-Budget-{index}"
            ),
            "webpage": f"https://budget-{index}.test",
            "phone_number": f"+216 71 {index:06d}",
            "address": f"{index} Budget Street, Tunis",
        }
        row.update(changes)
        return maps_observation(row, query="software Tunis", observed_at=STAMP)

    def scalar(self, sql, parameters=()):
        connection = open_registry(self.database)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def test_workers_racing_final_slot_cannot_overshoot_sqlite_limit(self):
        session = self.session(limit=1)
        observations = [self.observation(index) for index in range(1, 13)]
        # Independent unlimited throttles prove SQLite, not shared memory, owns
        # the final capacity decision.
        with ThreadPoolExecutor(max_workers=6) as workers:
            outcomes = list(workers.map(
                lambda item: session.resolve(item, RunAcceptanceBudget(None)).outcome,
                observations,
            ))
        self.assertEqual(outcomes.count("new"), 1)
        self.assertEqual(outcomes.count("limit"), 11)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM discovery_run_decisions"), 1,
        )
        self.assertEqual(session.budget_snapshot().remaining_capacity, 0)

    def test_replay_and_non_new_decisions_do_not_consume_capacity(self):
        session = self.session(limit=2)
        budget = RunAcceptanceBudget(None)
        created = session.resolve(self.observation(1), budget)
        replay = session.resolve(self.observation(1), budget)
        branch = session.resolve(self.observation(
            2,
            title="Budget Company 1",
            webpage="https://budget-1.test/branch",
        ), budget)
        review = session.resolve(self.observation(
            3,
            title="Different Entity",
            webpage="https://budget-1.test/shared",
        ), budget)
        second = session.resolve(self.observation(4), budget)

        self.assertEqual(created.outcome, "new")
        self.assertEqual(replay.outcome, "same_run")
        self.assertEqual(branch.outcome, "branch")
        self.assertEqual(review.outcome, "ambiguous")
        self.assertEqual(second.outcome, "new")
        snapshot = get_run_budget_snapshot(self.database, session.run_id)
        self.assertEqual(snapshot.committed_new, 2)
        self.assertEqual(snapshot.remaining_capacity, 0)
        self.assertTrue(snapshot.exhausted)

    def test_cross_run_known_company_does_not_consume_new_run_budget(self):
        first = self.session("first", limit=1)
        first.resolve(self.observation(), RunAcceptanceBudget(None))
        second = self.session("second", limit=1)
        outcome = second.resolve(self.observation(), RunAcceptanceBudget(None))
        self.assertEqual(outcome.outcome, "known")
        snapshot = second.budget_snapshot()
        self.assertEqual(snapshot.committed_new, 0)
        self.assertEqual(snapshot.remaining_capacity, 1)
        self.assertFalse(snapshot.exhausted)

    def test_rollback_restores_capacity_and_entity_writes(self):
        session = self.session(limit=1)

        def fail(checkpoint):
            if checkpoint == "after_entity_changes":
                raise RuntimeError("injected rollback")

        with self.assertRaisesRegex(RuntimeError, "injected rollback"):
            RegistryService(self.database, failure_injector=fail).resolve(
                session.run_id,
                self.observation(),
                lease_owner=session.lease_owner,
            )
        snapshot = get_run_budget_snapshot(self.database, session.run_id)
        self.assertEqual(snapshot.committed_new, 0)
        self.assertEqual(snapshot.remaining_capacity, 1)
        self.assertFalse(snapshot.exhausted)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 0)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM discovery_run_decisions"), 0,
        )

    def test_snapshot_is_restart_safe_at_zero_and_exhaustion(self):
        session = self.session(limit=1)
        before = get_run_budget_snapshot(Path(str(self.database)), session.run_id)
        self.assertEqual(before.configured_limit, 1)
        self.assertEqual(before.committed_new, 0)
        self.assertEqual(before.remaining_capacity, 1)
        self.assertFalse(before.exhausted)

        session.resolve(self.observation(), RunAcceptanceBudget(None))
        # A new service call opens a fresh SQLite connection and derives state
        # only from the persisted run and decisions.
        after = get_run_budget_snapshot(Path(str(self.database)), session.run_id)
        self.assertEqual(after.configured_limit, 1)
        self.assertEqual(after.committed_new, 1)
        self.assertEqual(after.remaining_capacity, 0)
        self.assertTrue(after.exhausted)

    def test_terminal_run_rejects_write_without_changing_snapshot(self):
        session = self.session(limit=1)
        session.fail()
        with self.assertRaisesRegex(RuntimeError, "does not accept decisions"):
            RegistryService(self.database).resolve(
                session.run_id, self.observation(),
            )
        snapshot = get_run_budget_snapshot(self.database, session.run_id)
        self.assertEqual(snapshot.committed_new, 0)
        self.assertEqual(snapshot.remaining_capacity, 1)
