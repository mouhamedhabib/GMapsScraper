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
from utils.known_companies import KnownCompanies
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
        self._known_companies = (
            KnownCompanies.from_directory(output_path) if incremental else None
        )
        self._summary_lock = Lock()
        self._summary = {
            "queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0,
        }
        self._resource_summary = {
            "browser_instances_created": 0,
            "browser_instances_recreated": 0,
            "temporary_tabs_opened": 0,
            "temporary_tabs_closed": 0,
        }

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
        self._thread_stop_event.set()
        self._executor.shutdown(wait=False)  # Shut down threads immediately

    def fast_search_algorithm(self, query_list: list[str]):
        """Submit all queries to the worker pool and surface worker exceptions."""
        if not query_list:
            raise ValueError("query_list must contain at least one usable query")
        query_list_range = len(query_list)
        self._query_list = query_list
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
        finally:
            # All worker ``finally`` blocks quit their browser before this join
            # returns, including after Ctrl+C sets the shared stop event.
            self._thread_stop_event.set()
            self._executor.shutdown(wait=True, cancel_futures=True)
            signal(SIGINT, previous_sigint)
            signal(SIGTERM, previous_sigterm)
        if self._incremental:
            print("Overall:")
            print(f"Queries processed: {self._summary['queries']}")
            print(f"Results inspected: {self._summary['inspected']}")
            print(f"Known duplicates skipped: {self._summary['known']}")
            print(f"Same-run duplicates skipped: {self._summary['same_run']}")
            print(f"New companies added: {self._summary['new']}")
        print(f"Workers used: {self._workers}")
        print(f"Python RSS at completion: {Process().memory_info().rss / 1024 / 1024:.2f} MB")
        print(f"Chrome instances created: {self._resource_summary['browser_instances_created']}")
        print(f"Chrome instances recreated: {self._resource_summary['browser_instances_recreated']}")
        print(
            "Temporary tabs opened/closed: "
            f"{self._resource_summary['temporary_tabs_opened']}/"
            f"{self._resource_summary['temporary_tabs_closed']}"
        )
        return dict(self._summary)

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
                              )

        # Round-robin partitioning across workers so no queries are dropped when
        # the query count isn't evenly divisible by the worker count (the previous
        # integer-division split silently skipped the remainder, e.g. 5 queries /
        # 2 workers only processed 4).
        try:
            for thread_index in range(thread_id, query_list_range, self._workers):
                if self._thread_stop_event.is_set():
                    break
                try:
                    maps_obj.start_scrapper(self._query_list[thread_index])
                except Exception as e:
                    print(f"Exception in thread {thread_id}: {e}")
                    continue
        finally:
            maps_obj.quit_driver()
            metrics = maps_obj.resource_metrics()
            with self._summary_lock:
                for key, value in metrics.items():
                    self._resource_summary[key] += value

    @staticmethod
    def load_query_file(file_name: str):
        """Return non-empty, stripped query lines from a UTF-8 text file."""
        with open(file_name, "r", encoding="utf-8") as query_file:
            return [line.strip() for line in query_file if line.strip()]
