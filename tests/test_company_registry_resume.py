"""Focused explicit Maps resume and schema-v6 writer-lease tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.run_lease import ActiveRunLeaseError, RunResumeError
from company_registry.storage import initialize_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-10-10T14:00:00+00:00"
QUERIES = ["software Tunis", "technology Sousse"]


class MapsResumeLeaseTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "exports"
        initialize_registry(self.database)

    def fresh(self, run_id="run", *, limit=2, **kwargs):
        return AuthoritativeMapsSession(
            self.database,
            self.exports,
            run_id=run_id,
            new_company_limit=limit,
            **kwargs,
        )

    def resume(self, run_id="run", *, limit=2, **kwargs):
        return AuthoritativeMapsSession(
            self.database,
            self.exports,
            resume_run_id=run_id,
            new_company_limit=limit,
            **kwargs,
        )

    @staticmethod
    def observation(index=1):
        return maps_observation(
            {
                "title": f"Resume Company {index}",
                "map_link": (
                    "https://google.com/maps/place/x/"
                    f"data=!4m1!1sChIJ-Resume-{index}"
                ),
                "webpage": f"https://resume-{index}.test",
                "phone_number": f"+216 71 {index:06d}",
                "address": f"{index} Resume Street, Tunis",
            },
            query=QUERIES[0],
            observed_at=STAMP,
        )

    def run_row(self, run_id="run"):
        connection = open_registry(self.database)
        try:
            return dict(connection.execute(
                "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,),
            ).fetchone())
        finally:
            connection.close()

    def expire_lease(self, run_id="run"):
        connection = open_registry(self.database)
        connection.execute(
            """UPDATE discovery_runs
                  SET lease_expires_at=? WHERE run_id=?""",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), run_id),
        )
        connection.commit()
        connection.close()

    def test_crash_resume_replays_checkpoint_and_uses_remaining_budget(self):
        crashed = self.fresh(limit=2)
        crashed.configure_queries(QUERIES)
        crashed.resolve(self.observation(1), RunAcceptanceBudget(None))
        old_owner = crashed.lease_owner
        self.expire_lease()

        resumed = self.resume(limit=2)
        resumed.configure_queries(QUERIES)
        checkpoint = resumed.recovery_checkpoint
        self.assertEqual(checkpoint.decision_count, 1)
        self.assertIsNotNone(checkpoint.last_decision_id)
        self.assertEqual(checkpoint.budget.committed_new, 1)
        self.assertEqual(checkpoint.budget.remaining_capacity, 1)
        self.assertEqual(
            resumed.resolve(self.observation(1), RunAcceptanceBudget(None)).outcome,
            "same_run",
        )
        self.assertEqual(
            resumed.resolve(self.observation(2), RunAcceptanceBudget(None)).outcome,
            "new",
        )
        self.assertTrue(resumed.budget_snapshot().exhausted)
        with self.assertRaisesRegex(RuntimeError, "owned elsewhere"):
            crashed.adapter.commit(
                crashed.run_id,
                self.observation(3),
                lease_owner=old_owner,
            )
        resumed.fail("INTERRUPTED")

    def test_active_lease_and_concurrent_resume_attempts_allow_one_writer(self):
        active = self.fresh()
        active.configure_queries(QUERIES)
        contender = self.resume()
        with self.assertRaises(ActiveRunLeaseError):
            contender.configure_queries(QUERIES)

        self.expire_lease()
        attempts = [self.resume(lease_owner=f"resume-{index}") for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(item.configure_queries, QUERIES) for item in attempts]
        successes = sum(future.exception() is None for future in futures)
        errors = [future.exception() for future in futures if future.exception()]
        self.assertEqual(successes, 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ActiveRunLeaseError)
        winner = attempts[0] if futures[0].exception() is None else attempts[1]
        winner.fail("INTERRUPTED")

    def test_explicit_resume_rejects_configuration_mismatches_without_mutation(self):
        session = self.fresh()
        session.configure_queries(QUERIES)
        session.fail("INTERRUPTED")
        before = self.run_row()

        cases = (
            self.resume(limit=1),
            self.resume(mode="authoritative"),
            AuthoritativeMapsSession(
                self.database,
                self.root / "different-exports",
                resume_run_id="run",
                new_company_limit=2,
            ),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(RunResumeError):
                    candidate.configure_queries(QUERIES)
        query_mismatch = self.resume()
        with self.assertRaises(RunResumeError):
            query_mismatch.configure_queries(["different query"])
        after = self.run_row()
        self.assertEqual(after["status"], before["status"])
        self.assertIsNone(after["lease_owner"])

    def test_terminal_and_missing_configuration_runs_fail_closed(self):
        failed = self.fresh("failed")
        failed.configure_queries(QUERIES)
        failed.fail("FAILED")
        with self.assertRaises(RunResumeError):
            self.resume("failed").configure_queries(QUERIES)

        successful = self.fresh("successful")
        successful.configure_queries(QUERIES)
        successful.complete()
        with self.assertRaises(RunResumeError):
            self.resume("successful").configure_queries(QUERIES)

        missing = self.fresh("missing")
        missing.fail("INTERRUPTED")
        connection = open_registry(self.database)
        connection.execute(
            "UPDATE discovery_runs SET run_config_hash=NULL WHERE run_id='missing'"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RunResumeError, "no durable configuration"):
            self.resume("missing").configure_queries(QUERIES)

    def test_heartbeat_and_interrupted_shutdown_release_lease(self):
        session = self.fresh(lease_seconds=60, heartbeat_interval_seconds=10)
        session.configure_queries(QUERIES)
        before = self.run_row()
        renewed_expiration = session.heartbeat()
        after_heartbeat = self.run_row()
        self.assertEqual(after_heartbeat["lease_owner"], session.lease_owner)
        self.assertEqual(after_heartbeat["lease_expires_at"], renewed_expiration)
        self.assertGreaterEqual(
            after_heartbeat["heartbeat_at"], before["heartbeat_at"],
        )

        session.start_heartbeat()
        session.assert_heartbeat_healthy()
        session.fail("INTERRUPTED")
        released = self.run_row()
        self.assertEqual(released["status"], "INTERRUPTED")
        self.assertIsNone(released["lease_owner"])
        self.assertIsNone(released["lease_expires_at"])
        self.assertIsNone(released["heartbeat_at"])

        resumed = self.resume(lease_owner="interrupted-resumer")
        resumed.configure_queries(QUERIES)
        self.assertEqual(self.run_row()["status"], "RUNNING")
        resumed.fail("INTERRUPTED")

        connection = open_registry(self.database)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()
