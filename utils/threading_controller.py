"""
Google Maps Worker Coordinator
==============================

Purpose:
    Divide query-file entries across browser workers and coordinate shutdown.

Pipeline:
    maps.py -> threading_controller.py -> google_maps_scraper.py

Input:
    A list of Maps search queries and scraper settings supplied by ``maps.py``.

Output:
    No direct file output; each worker delegates records to ``GoogleMaps``,
    which writes ``google_maps_data.*`` through the output-format helpers.

Previous / next:
    ``maps.py`` runs before this module; ``google_maps_scraper.py`` runs next.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.google_maps_scraper import GoogleMaps
from signal import signal, SIGINT, SIGTERM
from threading import Lock, Event
from pathlib import Path
from utils.known_companies import KnownCompanies
from utils.run_acceptance_budget import RunAcceptanceBudget
from company_registry.discovery_adapter import (
    DiscoveryMode, RegistryUnavailableError, parse_discovery_mode,
)
from company_registry.maps_authoritative import (
    DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY,
    AuthoritativeMapsSession,
)
from psutil import Process


class FastSearchAlgo:
    """Run independent Maps queries across a fixed pool of scraper workers."""

    def __init__(self,
                 unavailable_text: str = "Not Available", headless: bool = False, wait_time: int = 15,
                 suggested_ext: list = None, output_path: str = "./CSV_FILES", result_range: int = None,
                 workers: int = 1, verbose: bool = True,
                 output_format: str = "CSV",
                 scroll_minutes: int = 1,
                 incremental: bool = False,
                 low_resource: bool = False,
                 shadow_observer=None,
                 known_companies_dir: str = "./CSV_FILES",
                 allow_empty_known_companies: bool = False,
                 query_file_path=None,
                 discovery_mode: str = "legacy",
                 registry_database=None,
                 registry_run_id=None,
                 resume_run_id=None,
                 authoritative_export_directory=DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY,
                 allow_production_registry: bool = False,
                 ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        if result_range is not None and result_range < 1:
            raise ValueError("result_range must be >= 1 when provided")
        if wait_time < 1:
            raise ValueError("wait_time must be >= 1")
        if scroll_minutes < 1:
            raise ValueError("scroll_minutes must be >= 1")
        if suggested_ext is None:
            suggested_ext = ["contact-us", "contact"]

        self._unavailable_text = unavailable_text
        self._headless = headless
        self._wait_time = wait_time
        self._suggested_ext = suggested_ext
        self._output_path = output_path
        self._result_range = result_range
        self._scroll_minutes = scroll_minutes
        self._verbose = verbose
        self._output_format = output_format
        self._incremental = incremental
        self._low_resource = low_resource
        self._shadow_observer = shadow_observer
        self._discovery_mode = parse_discovery_mode(discovery_mode)
        if registry_run_id is not None and resume_run_id is not None:
            raise ValueError("registry_run_id and resume_run_id are mutually exclusive")
        if resume_run_id is not None and not self._discovery_mode.is_authoritative:
            raise ValueError("resume_run_id requires an authoritative discovery mode")
        self._authoritative_session = None
        if self._discovery_mode.is_authoritative:
            if registry_database is None:
                raise ValueError("authoritative mode requires --registry-database")
            self._authoritative_session = AuthoritativeMapsSession(
                registry_database,
                authoritative_export_directory,
                mode=self._discovery_mode,
                run_id=registry_run_id,
                resume_run_id=resume_run_id,
                new_company_limit=result_range,
                allow_production_registry=allow_production_registry,
            )
            # Authoritative workers never initialize a writer in the legacy
            # output destination, even though their writer is never invoked.
            self._output_path = str(self._authoritative_session.export_directory)
        self._known_companies = (
            KnownCompanies.from_directory(
                known_companies_dir,
                require_nonempty=not allow_empty_known_companies,
            )
            if incremental and not self._discovery_mode.is_authoritative else None
        )
        self._acceptance_budget = RunAcceptanceBudget(result_range)
        self._query_file_path = (
            str(Path(query_file_path).expanduser().resolve())
            if query_file_path is not None else None
        )
        self._summary_lock = Lock()
        self._summary = {
            "queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0,
            "updated": 0, "branch": 0, "ambiguous": 0, "quarantined": 0,
        }
        self._resource_summary = {
            "browser_instances_created": 0,
            "browser_instances_recreated": 0,
            "temporary_tabs_opened": 0,
            "temporary_tabs_closed": 0,
            "search_results_found": 0,
            "place_urls_extracted": 0,
            "search_page_urls_rejected": 0,
            "individual_place_urls_accepted": 0,
            "detail_pages_opened": 0,
            "companies_persisted": 0,
            "maps_readiness_retries": 0,
            "maps_verification_prompts": 0,
            "maps_search_not_ready": 0,
            "maps_no_results_confirmed": 0,
            "maps_queries_completed": 0,
            "maps_queries_blocked": 0,
        }
        self._run_rows = []
        self._query_states = []
        self._queries_loaded = 0
        self._query_indexes_scheduled = []
        self._query_indexes_completed = []
        self._termination_reason = None

        self._workers = workers
        self._query_list = list()
        self._threads_handlers = list()
        self._print_lock = Lock()
        self._thread_stop_event = Event()
        self._executor = ThreadPoolExecutor(max_workers=self._workers)
        super().__init__()

    def signal_handler(self, sig, frame):
        """Request worker shutdown when the process receives a termination signal."""
        print('[+] Exiting and releasing memory')
        self._note_termination("INTERRUPTED")
        self._thread_stop_event.set()
        self._executor.shutdown(wait=False)  # Shut down threads immediately

    def fast_search_algorithm(self, query_list: list[str]):
        """Submit all queries to the worker pool and surface worker exceptions."""
        if not query_list:
            raise ValueError("query_list must contain at least one usable query")
        query_list_range = len(query_list)
        if self._authoritative_session is not None:
            self._authoritative_session.configure_queries(query_list)
            durable_budget = self._authoritative_session.budget_snapshot()
            self._acceptance_budget = RunAcceptanceBudget(
                durable_budget.configured_limit,
                committed=durable_budget.committed_new,
            )
            self._authoritative_session.start_heartbeat()
        self._query_list = query_list
        self._queries_loaded = query_list_range
        previous_sigint = signal(SIGINT, self.signal_handler)
        previous_sigterm = signal(SIGTERM, self.signal_handler)

        futures = []
        for thread_index in range(self._workers):
            future = self._executor.submit(self._start_scrapper_threads, thread_index, query_list_range)
            futures.append(future)

        try:
            for future in as_completed(futures):
                # This will ensure that if an exception occurred in the thread, it will be raised here.
                future.result()
            if self._authoritative_session is not None:
                self._authoritative_session.assert_heartbeat_healthy()
        except BaseException as exception:
            self._note_termination(
                "INTERRUPTED" if isinstance(exception, KeyboardInterrupt) else "ERROR"
            )
            if self._authoritative_session is not None:
                try:
                    self._authoritative_session.fail(
                        "INTERRUPTED" if isinstance(exception, KeyboardInterrupt) else "FAILED"
                    )
                except BaseException:
                    pass
            raise
        finally:
            # All worker ``finally`` blocks quit their browser before this join
            # returns, including after Ctrl+C sets the shared stop event.
            self._thread_stop_event.set()
            self._executor.shutdown(wait=True, cancel_futures=True)
            signal(SIGINT, previous_sigint)
            signal(SIGTERM, previous_sigterm)
        self._finalize_termination_reason()
        observability = self.run_observability()
        if self._incremental or self._discovery_mode.is_authoritative:
            print("Overall:")
            print(f"Queries processed: {self._summary['queries']}")
            print(f"Results inspected: {self._summary['inspected']}")
            print(f"Known duplicates skipped: {self._summary['known']}")
            print(f"Same-run duplicates skipped: {self._summary['same_run']}")
            print(f"New companies added: {self._summary['new']}")
            if self._discovery_mode.is_authoritative:
                print(f"Updated companies: {self._summary['updated']}")
                print(f"New branches: {self._summary['branch']}")
                print(f"Review-only ambiguous: {self._summary['ambiguous']}")
                print(f"Review-only quarantined: {self._summary['quarantined']}")
        print(f"Workers used: {self._workers}")
        print(f"Python RSS at completion: {Process().memory_info().rss / 1024 / 1024:.2f} MB")
        print(f"Chrome instances created: {self._resource_summary['browser_instances_created']}")
        print(f"Chrome instances recreated: {self._resource_summary['browser_instances_recreated']}")
        print(
            "Temporary tabs opened/closed: "
            f"{self._resource_summary['temporary_tabs_opened']}/"
            f"{self._resource_summary['temporary_tabs_closed']}"
        )
        for name in (
            "search_results_found", "place_urls_extracted",
            "search_page_urls_rejected", "individual_place_urls_accepted",
            "detail_pages_opened", "companies_persisted",
            "maps_readiness_retries", "maps_verification_prompts",
            "maps_search_not_ready", "maps_no_results_confirmed",
            "maps_queries_completed", "maps_queries_blocked",
        ):
            print(f"{name}: {self._resource_summary[name]}")
        print(f"Query file: {observability['query_file_path'] or '<programmatic>'}")
        print(f"Queries loaded: {observability['queries_loaded']}")
        print(f"Queries scheduled: {observability['queries_scheduled']}")
        print(f"Queries completed: {observability['queries_completed']}")
        print(f"Query indexes scheduled: {observability['query_indexes_scheduled']}")
        print(f"Query indexes completed: {observability['query_indexes_completed']}")
        print(f"Termination reason: {observability['termination_reason']}")
        print(f"Global acceptance budget: {observability['global_acceptance_budget']}")
        result = {
            **self._summary, **self._resource_summary,
            "query_states": [dict(item) for item in self._query_states],
            "run_observability": observability,
        }
        if self._authoritative_session is not None:
            result["registry_run_id"] = self._authoritative_session.run_id
            if observability["termination_reason"] == "INTERRUPTED":
                self._authoritative_session.fail("INTERRUPTED")
                return result
            status = (
                "SUCCESS"
                if observability["termination_reason"] in {
                    "NORMAL_COMPLETION", "GLOBAL_LIMIT_REACHED",
                }
                else "PARTIAL"
            )
            exported = self._authoritative_session.complete(status=status)
            result["authoritative_export"] = {
                "csv": str(exported["csv"]),
                "manifest": str(exported["manifest"]),
                "rows": exported["rows"],
                "sha256": exported["sha256"],
            }
        return result

    def _start_scrapper_threads(self, thread_id: int, query_list_range: int) -> None:
        maps_obj = GoogleMaps(unavailable_text=self._unavailable_text, headless=self._headless,
                              wait_time=self._wait_time,
                              output_format=self._output_format,
                              suggested_ext=self._suggested_ext, output_path=self._output_path,
                              print_lock=self._print_lock,
                              result_range=self._result_range, verbose=self._verbose,
                              stop_event=self._thread_stop_event,
                              scroll_minutes=self._scroll_minutes,
                              incremental=self._incremental,
                              known_companies=self._known_companies,
                              summary=self._summary,
                              summary_lock=self._summary_lock,
                              low_resource=self._low_resource,
                              record_sink=self._record_run_row,
                              shadow_observer=self._shadow_observer,
                              acceptance_budget=self._acceptance_budget,
                              authoritative_session=self._authoritative_session,
                              )

        # Round-robin partitioning across workers so no queries are dropped when
        # the query count isn't evenly divisible by the worker count (the previous
        # integer-division split silently skipped the remainder, e.g. 5 queries /
        # 2 workers only processed 4).
        try:
            for thread_index in range(thread_id, query_list_range, self._workers):
                if self._thread_stop_event.is_set():
                    self._note_termination("INTERRUPTED")
                    break
                with self._summary_lock:
                    self._query_indexes_scheduled.append(thread_index)
                try:
                    state = maps_obj.start_scrapper(self._query_list[thread_index])
                    if state in {"COMPLETED", "NO_RESULTS"}:
                        with self._summary_lock:
                            self._query_indexes_completed.append(thread_index)
                    self._note_query_state(state)
                except RegistryUnavailableError:
                    self._note_termination("ERROR")
                    self._thread_stop_event.set()
                    raise
                except Exception as e:
                    self._note_termination("ERROR")
                    print(f"Exception in thread {thread_id}: {e}")
                    continue
        finally:
            maps_obj.quit_driver()
            metrics = maps_obj.resource_metrics()
            with self._summary_lock:
                for key, value in metrics.items():
                    self._resource_summary[key] += value
                self._query_states.extend(
                    getattr(maps_obj, "query_states", lambda: [])()
                )

    _TERMINATION_PRIORITY = {
        "NORMAL_COMPLETION": 0,
        "GLOBAL_LIMIT_REACHED": 1,
        "NETWORK_FAILURE": 2,
        "INTERRUPTED": 3,
        "VERIFICATION_ABORT": 4,
        "CONSENT_ABORT": 4,
        "ERROR": 5,
    }

    def _note_termination(self, reason: str) -> None:
        with self._summary_lock:
            current = self._termination_reason
            if (
                current is None
                or self._TERMINATION_PRIORITY[reason]
                > self._TERMINATION_PRIORITY[current]
            ):
                self._termination_reason = reason

    def _note_query_state(self, state: str) -> None:
        reasons = {
            "VERIFICATION_ABORTED": "VERIFICATION_ABORT",
            "CONSENT_ABORTED": "CONSENT_ABORT",
            "NETWORK_INTERRUPTED": "NETWORK_FAILURE",
            "FAILED": "ERROR",
            "SEARCH_TIMEOUT": "ERROR",
        }
        if state in reasons:
            self._note_termination(reasons[state])

    def _finalize_termination_reason(self) -> None:
        with self._summary_lock:
            reason = self._termination_reason
            completed = len(self._query_indexes_completed)
            loaded = self._queries_loaded
        if reason is not None:
            return
        budget = self._acceptance_budget.snapshot()
        if budget["limit"] is not None and budget["remaining"] == 0:
            self._note_termination("GLOBAL_LIMIT_REACHED")
        elif completed == loaded:
            self._note_termination("NORMAL_COMPLETION")
        else:
            self._note_termination("ERROR")

    def run_observability(self) -> dict:
        """Return sanitized, deterministic run-level scheduling telemetry."""
        with self._summary_lock:
            scheduled = sorted(set(self._query_indexes_scheduled))
            completed = sorted(set(self._query_indexes_completed))
            reason = self._termination_reason or "ERROR"
            loaded = self._queries_loaded
        checkpoint = (
            self._authoritative_session.recovery_checkpoint
            if self._authoritative_session is not None else None
        )
        return {
            "query_file_path": self._query_file_path,
            "queries_loaded": loaded,
            "queries_scheduled": len(scheduled),
            "queries_completed": len(completed),
            "query_indexes_scheduled": scheduled,
            "query_indexes_completed": completed,
            "termination_reason": reason,
            "global_acceptance_budget": self._acceptance_budget.snapshot(),
            "discovery_mode": self._discovery_mode.value,
            "registry_run_id": (
                self._authoritative_session.run_id
                if self._authoritative_session is not None else None
            ),
            "registry_run_resumed": bool(
                self._authoritative_session is not None
                and self._authoritative_session.is_resume
            ),
            "registry_resume_checkpoint": (
                {
                    "decision_count": checkpoint.decision_count,
                    "last_decision_id": checkpoint.last_decision_id,
                    "last_decision_created_at": checkpoint.last_decision_created_at,
                    "committed_new": checkpoint.budget.committed_new,
                    "remaining_capacity": checkpoint.budget.remaining_capacity,
                }
                if checkpoint is not None else None
            ),
        }

    @staticmethod
    def load_query_file(file_name: str):
        """Return non-empty, stripped query lines from a UTF-8 text file."""
        with open(file_name, "r", encoding="utf-8") as query_file:
            return [line.strip() for line in query_file if line.strip()]

    def _record_run_row(self, row: dict) -> None:
        with self._summary_lock:
            self._run_rows.append(dict(row))

    def run_records(self) -> list[dict]:
        """Return this invocation's unique, durably written rows in stable order."""
        with self._summary_lock:
            rows = [dict(row) for row in self._run_rows]
        rows = sorted(
            rows,
            key=lambda row: (
                str(row.get("map_link") or ""),
                str(row.get("title") or "").casefold(),
                str(row.get("source_query") or "").casefold(),
            ),
        )
        unique = []
        identities = KnownCompanies()
        for row in rows:
            if identities.check_and_add(row):
                unique.append(row)
        return unique
