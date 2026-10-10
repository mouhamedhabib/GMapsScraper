"""Phase 4A.2E offline reliability acceptance scenarios for Google Maps."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.exports import verify_run_exports
from company_registry.maps_authoritative import (
    AuthoritativeMapsSession,
    maps_observation,
    recover_finalizing_run,
)
from company_registry.run_lifecycle import RunStateTransitionError, transition_run
from company_registry.service import RegistryService, get_run_budget_snapshot
from company_registry.storage import initialize_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


QUERIES = ["software Tunis", "technology Sousse"]
STAMP = "2026-10-10T18:00:00+00:00"


class SimulatedProcessLoss(RuntimeError):
    pass


class ReliabilityAcceptanceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "schema-v6.db"
        self.exports = self.root / "exports"
        initialize_registry(self.database)

    def session(self, run_id, *, resume=False):
        arguments = {"resume_run_id" if resume else "run_id": run_id}
        return AuthoritativeMapsSession(
            self.database,
            self.exports,
            new_company_limit=3,
            **arguments,
        )

    @staticmethod
    def observation(index):
        return maps_observation(
            {
                "title": f"Acceptance Company {index}",
                "map_link": (
                    "https://google.test/maps/place/x/"
                    f"data=!4m1!1sChIJ-Acceptance-{index}"
                ),
                "webpage": f"https://acceptance-{index}.test",
                "phone_number": f"+216 71 {index:06d}",
                "address": f"{index} Acceptance Street, Tunis",
                "category": "Software company",
                "description": "Technology services and employment",
            },
            query=QUERIES[0],
            observed_at=STAMP,
        )

    def run_status(self, run_id):
        connection = open_registry(self.database)
        try:
            return connection.execute(
                "SELECT status FROM discovery_runs WHERE run_id=?", (run_id,),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_interruption_resume_budget_replay_and_export_recovery_end_to_end(self):
        seed = self.session("seed")
        seed.configure_queries(QUERIES)
        seed.resolve(self.observation(0), RunAcceptanceBudget(None))
        seed.fail("FAILED")

        initial = self.session("acceptance")
        initial.configure_queries(QUERIES)
        first_new = initial.resolve(self.observation(1), RunAcceptanceBudget(None))
        known = initial.resolve(self.observation(0), RunAcceptanceBudget(None))
        self.assertEqual((first_new.outcome, known.outcome), ("new", "known"))
        initial.fail("INTERRUPTED")

        reopened = get_run_budget_snapshot(self.database, "acceptance")
        self.assertEqual((reopened.committed_new, reopened.remaining_capacity), (1, 2))

        resumed = self.session("acceptance", resume=True)
        resumed.configure_queries(QUERIES)
        self.assertEqual(resumed.recovery_checkpoint.decision_count, 2)
        self.assertEqual(
            resumed.resolve(self.observation(1), RunAcceptanceBudget(None)).outcome,
            "same_run",
        )
        self.assertEqual(
            resumed.resolve(self.observation(0), RunAcceptanceBudget(None)).outcome,
            "known",
        )
        self.assertEqual(
            resumed.resolve(self.observation(2), RunAcceptanceBudget(None)).outcome,
            "new",
        )
        self.assertEqual(
            resumed.resolve(self.observation(3), RunAcceptanceBudget(None)).outcome,
            "new",
        )
        self.assertTrue(resumed.budget_snapshot().exhausted)

        def interrupt_export(point):
            if point == "employment:before_manifest_publish":
                raise SimulatedProcessLoss(point)

        with self.assertRaises(SimulatedProcessLoss):
            resumed.complete(failure_injector=interrupt_export)
        self.assertEqual(self.run_status("acceptance"), "FINALIZING")

        recovered = recover_finalizing_run(
            self.database,
            self.exports,
            "acceptance",
            QUERIES,
            new_company_limit=3,
        )
        self.assertEqual(recovered["status"], "SUCCESS")
        verified = verify_run_exports(self.database, "acceptance", self.exports)
        self.assertEqual(verified["discovery"]["rows"], 3)
        self.assertEqual(
            len(verified["discovery"]["company_ids"]),
            len(set(verified["discovery"]["company_ids"])),
        )

        connection = open_registry(self.database)
        try:
            counts = connection.execute(
                """SELECT count(*) AS decisions,
                          count(DISTINCT observation_id) AS observations,
                          sum(classification='NEW') AS new_decisions,
                          sum(classification='KNOWN') AS known_decisions
                     FROM discovery_run_decisions WHERE run_id='acceptance'"""
            ).fetchone()
            self.assertEqual(tuple(counts), (4, 4, 3, 1))
            self.assertEqual(
                connection.execute("SELECT count(*) FROM companies").fetchone()[0],
                4,
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeError, "does not accept decisions"):
            RegistryService(self.database).resolve(
                "acceptance", self.observation(4),
            )

    def test_lifecycle_state_matrix_and_invalid_terminal_transition(self):
        running = self.session("running")
        self.assertEqual(self.run_status("running"), "RUNNING")
        running.fail("INTERRUPTED")
        self.assertEqual(self.run_status("running"), "INTERRUPTED")

        failed = self.session("failed")
        failed.fail("FAILED")
        self.assertEqual(self.run_status("failed"), "FAILED")

        partial = self.session("partial")
        partial.configure_queries(QUERIES)
        partial.complete(status="PARTIAL")
        self.assertEqual(self.run_status("partial"), "PARTIAL")
        with self.assertRaises(RunStateTransitionError):
            transition_run(self.database, "partial", "SUCCESS")

