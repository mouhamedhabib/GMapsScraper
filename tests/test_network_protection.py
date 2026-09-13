"""Offline tests for the hysteretic network protection relay."""

from urllib.error import HTTPError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from job_search.daily_workflow import WorkflowOptions, run_workflow
from job_search.discovery import discover_jobs
from job_search.filtering import filter_stored_jobs
from job_search.network import (
    NetworkPauseExceeded,
    NetworkProtectionRelay,
    NetworkState,
)
from job_search.providers import ParsedJob
from job_search.repair_reviews import repair_review_jobs
from job_search.storage import connect_database, upsert_job


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def sequence_probe(values, callback=None):
    remaining = iter(values)

    def probe(**kwargs):
        if callback:
            callback()
        return next(remaining)

    return probe


class NetworkProtectionRelayTests(TestCase):
    def relay(self, probes, **overrides):
        clock = overrides.pop("clock", FakeClock())
        relay = NetworkProtectionRelay(
            probe_function=sequence_probe(probes),
            sleep_function=clock.sleep, clock=clock, output=lambda message: None,
            probe_interval=1, probe_max_interval=2, max_pause_seconds=30,
            **overrides,
        )
        return relay, clock

    def test_one_timeout_degrades_but_does_not_pause(self):
        relay, _ = self.relay([False])
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("connection timed out")
            return "ok"

        self.assertEqual(relay.protect(operation, context="query"), "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(relay.network_pauses, 0)
        self.assertEqual(relay.state, NetworkState.DEGRADED)


class RelayWorkflowIntegrationTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "jobs.db"
        self.query_file = self.root / "queries.txt"
        self.query_file.write_text("backend developer France\n", encoding="utf-8")
        self.options = WorkflowOptions(
            database=self.database, report_dir=self.root / "reports",
            job_query_file=self.query_file, job_limit=1, delay=0, timeout=1,
            completion=False, windowed=False,
        )

    @staticmethod
    def parsed(url):
        return ParsedJob(
            canonical_url=url, provider="lever",
            source_job_id=url.rsplit("/", 1)[-1], title="Backend Engineer",
            location_text="France", description="Build Python APIs. " * 20,
            fetch_status="FETCHED", status="OPEN",
        )

    def relay(self, probes, max_pause=30):
        clock = FakeClock()
        return NetworkProtectionRelay(
            probe_function=sequence_probe(probes), sleep_function=clock.sleep,
            clock=clock, output=lambda message: None, probe_interval=1,
            probe_max_interval=2, max_pause_seconds=max_pause,
        )

    def discovery_runner(self, search_side_effect):
        def runner(**kwargs):
            with patch("job_search.discovery.search_query", side_effect=search_side_effect), \
                 patch("job_search.discovery.has_next_search_page", return_value=False):
                return discover_jobs(
                    **kwargs, driver_factory=lambda **unused: Mock(),
                    fetcher=lambda url, timeout: self.parsed(url),
                )
        return runner

    def test_recovered_outage_finishes_success_and_reports_relay_metrics(self):
        attempts = 0

        def search(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise ConnectionError("ERR_INTERNET_DISCONNECTED")
            return ([{
                "title": "Backend Engineer",
                "url": "https://jobs.lever.co/acme/recovered",
            }], "")

        relay = self.relay([False, False, False, True, True, True])
        report = run_workflow(
            self.options, discovery_runner=self.discovery_runner(search),
            network_relay=relay,
        )
        self.assertEqual(report["run"]["status"], "SUCCESS")
        self.assertEqual(report["job_discovery"]["new"], 1)
        self.assertEqual(report["network"]["network_pauses"], 1)
        self.assertEqual(report["network"]["network_recoveries"], 1)
        self.assertGreater(report["network"]["network_pause_seconds"], 0)

    def test_max_pause_persists_checkpoint_and_makes_workflow_partial(self):
        relay = self.relay([False] * 20, max_pause=3)
        report = run_workflow(
            self.options,
            discovery_runner=self.discovery_runner(
                ConnectionError("ERR_NETWORK_CHANGED")
            ),
            network_relay=relay,
        )
        self.assertEqual(report["run"]["status"], "PARTIAL")
        self.assertEqual(report["network"]["queries_network_interrupted"], 1)
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        query = connection.execute("SELECT * FROM workflow_run_queries").fetchone()
        self.assertEqual(query["status"], "NETWORK_INTERRUPTED")
        self.assertEqual(query["page_start_offset"], 0)

    def test_completion_uses_same_relay_and_retries_same_job_after_recovery(self):
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        job = self.parsed("https://jobs.lever.co/acme/complete")
        job.description = None
        job_id, _ = upsert_job(connection, job, "query")
        filter_stored_jobs(connection, "v1.1")
        with connection:
            connection.execute(
                "UPDATE job_filter_results SET status='REVIEW' WHERE job_id=?",
                (job_id,),
            )
        attempts = 0

        def fetch(url, timeout):
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise ConnectionResetError("connection reset")
            return self.parsed(url)

        relay = self.relay([False, False, False, True, True, True])
        summary = repair_review_jobs(
            connection, fetcher=fetch, network_relay=relay,
        )
        self.assertEqual((attempts, summary["network_failed"]), (4, 0))
        self.assertEqual(relay.network_recoveries, 1)

    def test_resume_runs_only_interrupted_query_and_keeps_committed_jobs(self):
        self.query_file.write_text("first query\nsecond query\n", encoding="utf-8")
        second_attempts = 0

        def initial_search(driver, query, *args, **kwargs):
            nonlocal second_attempts
            if query == "first query":
                return ([{
                    "title": "Backend Engineer",
                    "url": "https://jobs.lever.co/acme/first",
                }], "")
            second_attempts += 1
            raise ConnectionError("ERR_INTERNET_DISCONNECTED")

        first_report = run_workflow(
            self.options, discovery_runner=self.discovery_runner(initial_search),
            network_relay=self.relay([False] * 20, max_pause=3),
        )
        self.assertEqual(first_report["run"]["status"], "PARTIAL")
        self.assertEqual(first_report["job_discovery"]["new"], 1)

        resumed_queries = []

        def resumed_search(driver, query, *args, **kwargs):
            resumed_queries.append(query)
            return ([{
                "title": "Backend Engineer",
                "url": "https://jobs.lever.co/acme/second",
            }], "")

        self.options.resume_run_id = first_report["run"]["run_id"]
        resumed = run_workflow(
            self.options, discovery_runner=self.discovery_runner(resumed_search),
            network_relay=self.relay([]),
        )
        self.assertEqual(resumed["run"]["status"], "SUCCESS")
        self.assertEqual(resumed["job_discovery"]["new"], 2)
        self.assertEqual(resumed["network"]["network_pauses"], 1)
        self.assertEqual(resumed_queries, ["second query"])
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)


class NetworkProtectionRelayBehaviorTests(TestCase):
    def relay(self, probes, **overrides):
        clock = overrides.pop("clock", FakeClock())
        relay = NetworkProtectionRelay(
            probe_function=sequence_probe(probes),
            sleep_function=clock.sleep, clock=clock, output=lambda message: None,
            probe_interval=1, probe_max_interval=2, max_pause_seconds=30,
            **overrides,
        )
        return relay, clock

    def test_repeated_failures_pause_and_no_expensive_calls_occur_while_paused(self):
        calls = 0
        calls_during_probe = []
        clock = FakeClock()

        def operation():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise ConnectionResetError("connection reset")
            return "done"

        relay = NetworkProtectionRelay(
            probe_function=sequence_probe(
                [False, False, False, True, True, True],
                callback=lambda: calls_during_probe.append(calls),
            ),
            sleep_function=clock.sleep, clock=clock, output=lambda message: None,
            probe_interval=1, probe_max_interval=2, max_pause_seconds=30,
        )
        self.assertEqual(relay.protect(operation, context="query"), "done")
        self.assertEqual(relay.network_pauses, 1)
        self.assertEqual(relay.network_recoveries, 1)
        self.assertEqual(calls_during_probe[-3:], [3, 3, 3])
        self.assertEqual(calls, 4)
        self.assertEqual(clock.sleeps, [1, 1, 1])

    def test_one_success_does_not_resume_and_three_successes_do(self):
        relay, _ = self.relay([False, False, False, True, True, True])
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise OSError(101, "network is unreachable")
            return calls

        self.assertEqual(relay.protect(operation, context="query"), 4)
        self.assertEqual(relay.state, NetworkState.HEALTHY)
        self.assertEqual(relay.network_recoveries, 1)

    def test_unstable_probe_sequence_remains_paused_until_consecutive_successes(self):
        relay, clock = self.relay(
            [False, False, False, True, False, True, True, True]
        )
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise ConnectionError("ERR_INTERNET_DISCONNECTED")
            return "ok"

        self.assertEqual(relay.protect(operation, context="query"), "ok")
        self.assertEqual(calls, 4)
        self.assertEqual(len(clock.sleeps), 5)

    def test_target_http_503_does_not_probe_or_pause(self):
        probes = 0

        def probe(**kwargs):
            nonlocal probes
            probes += 1
            return False

        relay = NetworkProtectionRelay(probe_function=probe, output=lambda message: None)
        with self.assertRaises(HTTPError):
            relay.protect(
                lambda: (_ for _ in ()).throw(
                    HTTPError("https://target.test", 503, "unavailable", {}, None)
                ),
                context="target",
            )
        self.assertEqual((probes, relay.network_pauses), (0, 0))

    def test_captcha_does_not_trigger_network_pause(self):
        relay, _ = self.relay([])
        with self.assertRaisesRegex(RuntimeError, "CAPTCHA"):
            relay.protect(
                lambda: (_ for _ in ()).throw(RuntimeError("Google CAPTCHA")),
                context="query",
            )
        self.assertEqual(relay.state, NetworkState.HEALTHY)

    def test_max_pause_is_bounded_and_then_fails_fast(self):
        clock = FakeClock()
        relay = NetworkProtectionRelay(
            probe_function=lambda **kwargs: False,
            sleep_function=clock.sleep, clock=clock, output=lambda message: None,
            probe_interval=1, probe_max_interval=2, max_pause_seconds=3,
        )
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise ConnectionError("ERR_NETWORK_CHANGED")

        with self.assertRaises(NetworkPauseExceeded):
            relay.protect(operation, context="query")
        self.assertEqual(calls, 3)
        sleeps = len(clock.sleeps)
        with self.assertRaises(NetworkPauseExceeded):
            relay.protect(operation, context="next query")
        self.assertEqual((calls, len(clock.sleeps)), (3, sleeps))
        self.assertGreaterEqual(relay.network_pause_seconds, 3)

    def test_healthy_independent_probe_marks_target_bad_without_global_pause(self):
        relay, _ = self.relay([True])
        with self.assertRaisesRegex(RuntimeError, "TARGET_SITE_BAD"):
            relay.protect(
                lambda: (_ for _ in ()).throw(ConnectionRefusedError("refused")),
                context="employer",
            )
        self.assertEqual(relay.network_pauses, 0)
