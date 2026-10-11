"""Focused explicit Maps resume and schema-v6 writer-lease tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import patch

from company_registry.discovery_adapter import RegistryUnavailableError
from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.run_lease import (
    ActiveRunLeaseError,
    RunLeaseError,
    RunResumeError,
)
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

    def diagnostic_events(self, session):
        if not session.diagnostic_log_path.exists():
            return []
        return [
            json.loads(line)
            for line in session.diagnostic_log_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

    def wait_for_diagnostic(self, session, event, timeout=2):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            matches = [
                item for item in self.diagnostic_events(session)
                if item["event"] == event
            ]
            if matches:
                return matches[-1]
            sleep(0.01)
        self.fail(f"diagnostic event was not recorded: {event}")

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

    def test_background_heartbeat_renews_during_processing_longer_than_lease(self):
        session = self.fresh(
            lease_seconds=1,
            heartbeat_interval_seconds=0.1,
        )
        session.configure_queries(QUERIES)
        before = self.run_row()
        session.start_heartbeat()

        deadline = monotonic() + 2
        while monotonic() < deadline:
            renewed = self.run_row()
            if renewed["heartbeat_at"] > before["heartbeat_at"]:
                break
            sleep(0.02)
        else:
            self.fail("background heartbeat did not renew the lease")

        # Model slow browser or website work that lasts beyond the original
        # one-second lease without touching the registry from the worker.
        sleep(1.1)
        outcome = session.resolve(self.observation(), RunAcceptanceBudget(None))
        self.assertEqual(outcome.outcome, "new")
        session.fail("INTERRUPTED")

    def test_expired_owner_cannot_commit(self):
        session = self.fresh()
        session.configure_queries(QUERIES)
        self.expire_lease()

        with self.assertRaisesRegex(RegistryUnavailableError, "lease has expired"):
            session.resolve(self.observation(), RunAcceptanceBudget(None))

        self.assertEqual(self.run_row()["status"], "RUNNING")
        session.release_lease()

    def test_heartbeat_startup_and_shutdown_diagnostics_are_retained(self):
        session = self.fresh(heartbeat_interval_seconds=0.05)
        session.configure_queries(QUERIES)
        session.start_heartbeat()
        started = self.wait_for_diagnostic(session, "heartbeat_thread_started")
        self.assertEqual(started["run_id"], session.run_id)
        self.assertTrue(started["thread_name"].startswith("registry-lease-"))

        session.fail("INTERRUPTED")
        events = self.diagnostic_events(session)
        names = [item["event"] for item in events]
        self.assertIn("heartbeat_startup_completed", names)
        self.assertIn("heartbeat_shutdown_requested", names)
        self.assertIn("heartbeat_thread_stopped", names)
        self.assertIn("lifecycle_transition_succeeded", names)
        self.assertIn("lease_released", names)
        self.assertEqual(
            session.diagnostic_log_path,
            self.database.parent / "logs/writer_lease.jsonl",
        )

    def test_heartbeat_records_schedule_entry_sqlite_stages_and_completion(self):
        session = self.fresh(heartbeat_interval_seconds=0.05)
        session.configure_queries(QUERIES)
        session.start_heartbeat()
        completed = self.wait_for_diagnostic(
            session, "heartbeat_renewal_completed"
        )
        session.fail("INTERRUPTED")

        events = self.diagnostic_events(session)
        scheduled = next(
            item for item in events
            if item["event"] == "heartbeat_renewal_scheduled"
        )
        entered = next(
            item for item in events
            if item["event"] == "heartbeat_renewal_attempt_entered"
        )
        stages = {
            (item["stage"], item["phase"])
            for item in events
            if item["event"] == "heartbeat_renewal_stage"
        }
        for stage in (
            "connection_opening", "schema_validation",
            "transaction_acquisition", "lease_update", "commit",
        ):
            self.assertIn((stage, "started"), stages)
            self.assertIn((stage, "completed"), stages)
        self.assertEqual(scheduled["attempt_sequence"], 1)
        self.assertEqual(entered["attempt_sequence"], 1)
        self.assertGreaterEqual(entered["schedule_delay_seconds"], 0)
        self.assertGreaterEqual(completed["attempt_duration_seconds"], 0)
        self.assertEqual(completed["pid"], scheduled["pid"])
        self.assertIsInstance(completed["native_thread_id"], int)
        self.assertIn("process_cpu_seconds", completed)
        self.assertIn("voluntary_context_switches", completed)

    def test_expired_heartbeat_keeps_stage_evidence_and_fencing(self):
        session = self.fresh()
        session.configure_queries(QUERIES)
        self.expire_lease()
        timing = []

        with self.assertRaisesRegex(RunLeaseError, "missing, expired, or owned"):
            session.heartbeat(
                timing_callback=lambda *values: timing.append(values)
            )

        self.assertIn(("connection_opening", "started", None, None), timing)
        self.assertTrue(any(
            stage == "lease_update" and phase == "completed"
            for stage, phase, _, _ in timing
        ))
        self.assertFalse(any(stage == "commit" for stage, _, _, _ in timing))
        self.assertEqual(self.run_row()["status"], "RUNNING")
        session.release_lease()

    def test_opt_in_stack_snapshots_are_private_rotated_and_cleaned_up(self):
        session = self.fresh(
            heartbeat_interval_seconds=5,
            stack_snapshot_interval_seconds=0.03,
        )
        session.configure_queries(QUERIES)
        session.start_heartbeat()

        deadline = monotonic() + 1
        while monotonic() < deadline:
            if (
                session.stack_diagnostic_path.exists()
                and "Thread" in session.stack_diagnostic_path.read_text(
                    encoding="utf-8"
                )
            ):
                break
            sleep(0.01)
        else:
            self.fail("stack snapshot was not written")

        sleep(0.12)
        session.fail("INTERRUPTED")
        stack_files = list(
            session.stack_diagnostic_path.parent.glob("writer_stacks.log*")
        )
        self.assertLessEqual(len(stack_files), 4)
        self.assertTrue(stack_files)
        for path in stack_files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        names = [item["event"] for item in self.diagnostic_events(session)]
        self.assertIn("stack_snapshot_thread_started", names)
        self.assertIn("stack_snapshot_initial_written", names)
        self.assertIn("stack_snapshot_thread_stopped", names)
        self.assertIsNone(session._stack_snapshots)

    def test_stack_snapshot_interval_is_opt_in_and_positive(self):
        session = self.fresh()
        self.assertIsNone(session.stack_snapshot_interval_seconds)
        self.assertFalse(session.stack_diagnostic_path.exists())
        session.release_lease()
        with self.assertRaisesRegex(ValueError, "must be > 0"):
            self.fresh(run_id="invalid-stack", stack_snapshot_interval_seconds=0)

    def test_abrupt_process_exit_retains_startup_and_initial_stack_evidence(self):
        script = """
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

from company_registry.maps_authoritative import AuthoritativeMapsSession

session = AuthoritativeMapsSession(
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    run_id="abrupt-run",
    new_company_limit=2,
    heartbeat_interval_seconds=30,
    stack_snapshot_interval_seconds=30,
)
session.configure_queries(["software Tunis", "technology Sousse"])
session.start_heartbeat()
deadline = monotonic() + 2
while monotonic() < deadline:
    if session.diagnostic_log_path.exists():
        events = [
            json.loads(line).get("event")
            for line in session.diagnostic_log_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        if (
            "stack_snapshot_initial_written" in events
            and "heartbeat_renewal_scheduled" in events
            and session.stack_diagnostic_path.exists()
        ):
            break
    sleep(0.01)
else:
    raise RuntimeError("startup diagnostics were not written")
os._exit(7)
"""
        result = subprocess.run(
            [
                sys.executable, "-c", script,
                str(self.database), str(self.exports),
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
        )

        self.assertEqual(result.returncode, 7)
        log_path = self.database.parent / "logs/writer_lease.jsonl"
        events = [
            json.loads(line)["event"]
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertIn("heartbeat_startup_completed", events)
        self.assertIn("heartbeat_renewal_scheduled", events)
        self.assertIn("stack_snapshot_initial_written", events)
        stack_path = self.database.parent / "logs/writer_stacks.log"
        self.assertIn("Thread", stack_path.read_text(encoding="utf-8"))
        self.assertEqual(self.run_row("abrupt-run")["status"], "RUNNING")

    def test_heartbeat_renewal_exception_is_logged_and_visible(self):
        session = self.fresh(heartbeat_interval_seconds=0.01)
        session.configure_queries(QUERIES)
        with patch.object(
            session,
            "heartbeat",
            side_effect=ValueError("injected renewal failure"),
        ):
            session.start_heartbeat()
            failed = self.wait_for_diagnostic(
                session, "heartbeat_renewal_failed"
            )
            with self.assertRaisesRegex(RuntimeError, "heartbeat failed"):
                session.assert_heartbeat_healthy()

        self.assertEqual(failed["error_type"], "ValueError")
        self.assertEqual(failed["error_message"], "injected renewal failure")
        self.assertIsNotNone(failed["last_successful_renewal_at"])
        self.assertEqual(failed["heartbeat_error_type"], "ValueError")
        session.fail("INTERRUPTED")

    def test_expiration_diagnostic_retains_last_renewal_and_thread_state(self):
        session = self.fresh(heartbeat_interval_seconds=0.05)
        session.configure_queries(QUERIES)
        session.start_heartbeat()
        self.wait_for_diagnostic(session, "heartbeat_thread_started")
        session._heartbeat_stop.set()
        session._heartbeat_thread.join(timeout=1)
        self.expire_lease()

        with self.assertRaisesRegex(RegistryUnavailableError, "lease has expired"):
            session.resolve(self.observation(), RunAcceptanceBudget(None))
        expiration = self.wait_for_diagnostic(
            session, "lease_expiration_detected"
        )
        self.assertEqual(expiration["operation"], "decision_commit")
        self.assertIsNotNone(expiration["last_successful_renewal_at"])
        self.assertIsNotNone(
            expiration["elapsed_since_successful_renewal_seconds"]
        )
        self.assertFalse(expiration["heartbeat_thread_alive"])
        self.assertTrue(expiration["heartbeat_stop_requested"])

        with self.assertRaisesRegex(RuntimeError, "lease has expired"):
            session.fail("INTERRUPTED")
        retained = self.diagnostic_events(session)
        self.assertTrue(any(
            item["event"] == "lease_expiration_detected"
            and item["operation"] == "transition_to_INTERRUPTED"
            for item in retained
        ))
