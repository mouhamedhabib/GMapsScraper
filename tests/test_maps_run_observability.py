"""Offline regression tests for Maps run-level observability."""

import json
from pathlib import Path
from signal import SIGINT, SIGTERM
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from company_registry.shadow import ShadowObserver, create_consistent_shadow_snapshot
from company_registry.schema import MIGRATION_1, MIGRATION_2
from maps import run_maps_discovery
from utils.threading_controller import FastSearchAlgo


class FakeWorker:
    states = {}
    commit_count = 0
    release_count = 0
    stop_after = None

    def __init__(self, **kwargs):
        self.budget = kwargs["acceptance_budget"]
        self.stop_event = kwargs["stop_event"]
        self.summary = kwargs["summary"]
        self.summary_lock = kwargs["summary_lock"]
        self.completed = 0
        self.blocked = 0

    def start_scrapper(self, query):
        for _ in range(self.commit_count):
            reservation = self.budget.try_reserve()
            if reservation is not None:
                reservation.commit()
        for _ in range(self.release_count):
            reservation = self.budget.try_reserve()
            if reservation is not None:
                reservation.release()
        state = self.states.get(query, "COMPLETED")
        if state in {"COMPLETED", "NO_RESULTS"}:
            self.completed += 1
        else:
            self.blocked += 1
        with self.summary_lock:
            self.summary["queries"] += 1
        if query == self.stop_after:
            self.stop_event.set()
        return state

    def quit_driver(self):
        pass

    def resource_metrics(self):
        return {
            "maps_queries_completed": self.completed,
            "maps_queries_blocked": self.blocked,
        }

    def query_states(self):
        return []


class MapsRunObservabilityTests(TestCase):
    def setUp(self):
        FakeWorker.states = {}
        FakeWorker.commit_count = 0
        FakeWorker.release_count = 0
        FakeWorker.stop_after = None
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.query_file = self.root / "queries.txt"
        self.query_file.write_text("known one\nknown two\nknown three\n", encoding="utf-8")

    def run_coordinator(self, queries=None, *, limit=5, workers=1):
        queries = queries or ["known one", "known two", "known three"]
        coordinator = FastSearchAlgo(
            incremental=True, result_range=limit, workers=workers,
            known_companies_dir=str(self.root / "empty-history"),
            allow_empty_known_companies=True,
            query_file_path=self.query_file,
            verbose=False,
        )
        with patch("utils.threading_controller.GoogleMaps", FakeWorker):
            result = coordinator.fast_search_algorithm(queries)
        return result

    def test_known_only_run_records_all_queries_and_unused_budget(self):
        result = self.run_coordinator()
        telemetry = result["run_observability"]
        self.assertEqual(telemetry["query_file_path"], str(self.query_file.resolve()))
        self.assertEqual(telemetry["queries_loaded"], 3)
        self.assertEqual(telemetry["queries_scheduled"], 3)
        self.assertEqual(telemetry["queries_completed"], 3)
        self.assertEqual(telemetry["query_indexes_scheduled"], [0, 1, 2])
        self.assertEqual(telemetry["query_indexes_completed"], [0, 1, 2])
        self.assertEqual(telemetry["termination_reason"], "NORMAL_COMPLETION")
        self.assertEqual(telemetry["global_acceptance_budget"], {
            "limit": 5, "reserved": 0, "committed": 0, "remaining": 5,
        })
        self.assertEqual(result["queries"], telemetry["queries_completed"])
        self.assertEqual(result["maps_queries_completed"], telemetry["queries_completed"])

    def test_global_limit_and_failed_reservations_are_distinguished(self):
        FakeWorker.commit_count = 5
        limited = self.run_coordinator()
        self.assertEqual(
            limited["run_observability"]["termination_reason"],
            "GLOBAL_LIMIT_REACHED",
        )
        self.assertEqual(
            limited["run_observability"]["global_acceptance_budget"]["remaining"], 0,
        )

        FakeWorker.commit_count = 0
        FakeWorker.release_count = 3
        released = self.run_coordinator()
        self.assertEqual(
            released["run_observability"]["termination_reason"],
            "NORMAL_COMPLETION",
        )
        self.assertEqual(released["run_observability"]["global_acceptance_budget"], {
            "limit": 5, "reserved": 0, "committed": 0, "remaining": 5,
        })

    def test_stop_event_records_partial_interrupted_run(self):
        FakeWorker.stop_after = "known one"
        telemetry = self.run_coordinator()["run_observability"]
        self.assertEqual(telemetry["queries_scheduled"], 1)
        self.assertEqual(telemetry["queries_completed"], 1)
        self.assertEqual(telemetry["query_indexes_scheduled"], [0])
        self.assertEqual(telemetry["termination_reason"], "INTERRUPTED")

    def test_sigint_and_sigterm_request_the_same_clean_interruption(self):
        for signal_number in (SIGINT, SIGTERM):
            with self.subTest(signal_number=signal_number):
                coordinator = FastSearchAlgo(
                    incremental=True, result_range=1, workers=1,
                    known_companies_dir=str(self.root / "empty-history"),
                    allow_empty_known_companies=True,
                )
                coordinator.signal_handler(signal_number, None)
                self.assertTrue(coordinator._thread_stop_event.is_set())
                self.assertEqual(
                    coordinator.run_observability()["termination_reason"],
                    "INTERRUPTED",
                )

    def test_terminal_query_states_map_to_specific_reasons(self):
        cases = {
            "VERIFICATION_ABORTED": "VERIFICATION_ABORT",
            "CONSENT_ABORTED": "CONSENT_ABORT",
            "NETWORK_INTERRUPTED": "NETWORK_FAILURE",
            "FAILED": "ERROR",
        }
        for state, expected in cases.items():
            with self.subTest(state=state):
                FakeWorker.states = {"known one": state}
                result = self.run_coordinator(queries=["known one"])
                self.assertEqual(
                    result["run_observability"]["termination_reason"], expected,
                )
                self.assertEqual(
                    result["run_observability"]["query_indexes_completed"], [],
                )
                self.assertEqual(result["maps_queries_completed"], 0)


class ShadowRunObservabilityTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        production = self.root / "production.db"
        import sqlite3
        connection = sqlite3.connect(production)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        self.shadow = self.root / "shadow.db"
        create_consistent_shadow_snapshot(production, self.shadow)

    def test_shadow_summary_contains_only_sanitized_run_metadata(self):
        observer = ShadowObserver(
            self.shadow, source_system="GOOGLE_MAPS",
            report_directory=self.root / "reports", run_id="observability-run",
        )
        metadata = {
            "query_file_path": "/safe/queries.txt",
            "queries_loaded": 2,
            "queries_scheduled": 2,
            "queries_completed": 2,
            "query_indexes_scheduled": [0, 1],
            "query_indexes_completed": [0, 1],
            "termination_reason": "NORMAL_COMPLETION",
            "global_acceptance_budget": {
                "limit": 5, "reserved": 0, "committed": 0, "remaining": 5,
            },
            "secret": "must-not-be-recorded",
        }
        self.assertTrue(observer.set_run_observability(metadata))
        summary = observer.close()
        self.assertEqual(summary["run_observability"]["queries_loaded"], 2)
        self.assertNotIn("secret", summary["run_observability"])
        written = json.loads(observer.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(written["run_observability"], summary["run_observability"])

    def test_invalid_shadow_telemetry_is_ignored(self):
        observer = ShadowObserver(
            self.shadow, source_system="GOOGLE_MAPS",
            report_directory=self.root / "reports", run_id="invalid-run",
        )
        self.assertFalse(observer.set_run_observability({
            "global_acceptance_budget": object(),
        }))
        self.assertNotIn("run_observability", observer.close())

    def test_shadow_finalization_failure_does_not_block_maps_result(self):
        query_file = self.root / "query.txt"
        query_file.write_text("one\n", encoding="utf-8")
        shadow = Mock()
        shadow.set_run_observability.side_effect = RuntimeError("telemetry unavailable")

        class FakeAlgo:
            load_query_file = staticmethod(lambda file_name: ["one"])

            def __init__(self, **kwargs):
                pass

            def fast_search_algorithm(self, queries):
                return {"new": 0}

            def run_observability(self):
                return {"queries_loaded": 1}

            def run_records(self):
                return []

        with patch("maps.open_shadow_observer", return_value=shadow), patch(
            "maps.FastSearchAlgo", FakeAlgo,
        ), patch("maps.export_maps_rows", return_value={
            "timestamped": self.root / "timestamped.csv",
            "latest": self.root / "latest.csv",
            "rows": 0,
        }):
            result = run_maps_discovery(
                query_file=query_file, company_registry_shadow=True,
                allow_empty_known_companies=True,
            )
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["csv_rows"], 0)
        shadow.close.assert_called_once_with()
