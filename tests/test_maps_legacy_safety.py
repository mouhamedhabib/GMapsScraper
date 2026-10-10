"""Offline regression coverage for Phase 2B.4 legacy safety fixes."""

from concurrent.futures import ThreadPoolExecutor
from csv import DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest import TestCase
from unittest.mock import Mock

from utils.google_maps_scraper import GoogleMaps
from utils.known_companies import KnownCompanies
from utils.run_acceptance_budget import RunAcceptanceBudget
from utils.threading_controller import FastSearchAlgo


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=("name", "map_place_id"))
        writer.writeheader()
        writer.writerows(rows)


def place_url(index):
    return (
        "https://www.google.com/maps/place/Company/"
        f"data=!4m1!1s0xlegacy:{index:04x}"
    )


def configure_scraper(registry, budget, writer, *, observer=None, index=1):
    scraper = GoogleMaps(
        incremental=True,
        known_companies=registry,
        result_range=budget.limit,
        acceptance_budget=budget,
        shadow_observer=observer,
        verbose=False,
    )
    state = {"index": index}
    scraper._GoogleMaps__pprint_override = lambda *args, **kwargs: None
    scraper.validate_result_link = lambda result, driver: (
        "1", "2", place_url(state["index"]),
    )
    scraper.get_title = lambda driver: f"Company {state['index']}"
    scraper.get_website_link = lambda driver: f"https://company-{state['index']}.test"
    scraper.get_phone_number = lambda driver: f"+216 71 {state['index']:06d}"
    scraper.get_cover_image = lambda driver: ""
    scraper.get_rating_in_card = lambda driver: "5"
    scraper.get_privacy_price = lambda driver: ""
    scraper.get_category = lambda driver: "Software company"
    scraper.get_address = lambda driver: f"Address {state['index']}"
    scraper.get_working_hours = lambda driver: ""
    scraper.get_menu_link = lambda driver: ""
    scraper.get_related_images_list = lambda driver: ""
    scraper.get_about_description = lambda driver: {}
    scraper._web_pattern_scraper.find_patterns = lambda *args: {}
    scraper.reset_driver_for_next_run = lambda result, driver: None
    scraper._file_creator.create = writer
    return scraper, state


class HistoricalBaselineSafetyTests(TestCase):
    def test_separate_output_directory_uses_configured_historical_baseline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history"
            output = root / "isolated-output"
            rows = [
                {"name": f"Legacy {index}", "map_place_id": f"data:0xlegacy:{index:04x}"}
                for index in range(35)
            ]
            write_rows(history / "leads_master.csv", rows)
            coordinator = FastSearchAlgo(
                incremental=True, result_range=5, output_path=str(output),
                known_companies_dir=str(history), verbose=False,
            )
            self.addCleanup(coordinator._executor.shutdown)
            for row in rows:
                self.assertEqual(coordinator._known_companies.duplicate_kind(row), "known")
            self.assertFalse(output.exists())

    def test_missing_or_empty_baseline_fails_without_explicit_override(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                FastSearchAlgo(
                    incremental=True, known_companies_dir=str(root / "missing"),
                )
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaises(ValueError):
                FastSearchAlgo(incremental=True, known_companies_dir=str(empty))

            allowed = FastSearchAlgo(
                incremental=True, known_companies_dir=str(root / "missing"),
                allow_empty_known_companies=True,
            )
            allowed._executor.shutdown()
            self.assertFalse(allowed._known_companies._startup_identities)


class GlobalAcceptanceBudgetTests(TestCase):
    def test_atomic_budget_never_overshoots_or_deadlocks(self):
        budget = RunAcceptanceBudget(5)

        def accept(_):
            reservation = budget.try_reserve()
            if reservation is None:
                return False
            reservation.commit()
            return True

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(accept, index) for index in range(200)]
            accepted = [future.result(timeout=2) for future in futures]
        self.assertEqual(sum(accepted), 5)
        self.assertEqual(budget.snapshot(), {
            "limit": 5, "reserved": 0, "committed": 5, "remaining": 0,
        })

    def test_five_record_limit_is_global_across_query_labels(self):
        registry = KnownCompanies()
        budget = RunAcceptanceBudget(5)
        writer = Mock()
        observer = Mock()
        scraper, state = configure_scraper(
            registry, budget, writer, observer=observer,
        )
        outcomes = []
        for index in range(1, 9):
            state["index"] = index
            outcomes.append(scraper._scrape_result_and_store(
                Mock(), "continue", f"query {index % 3}", [8, index],
            ))
        self.assertEqual(outcomes.count("new"), 5)
        self.assertEqual(outcomes.count("limit"), 3)
        self.assertEqual(writer.call_count, 5)
        self.assertEqual(observer.observe.call_count, 5)
        self.assertEqual(scraper.resource_metrics()["companies_persisted"], 5)
        self.assertEqual(budget.snapshot()["committed"], 5)

    def test_failed_durable_write_releases_identity_and_budget(self):
        registry = KnownCompanies()
        budget = RunAcceptanceBudget(1)
        writer = Mock(side_effect=[OSError("disk full"), None])
        scraper, state = configure_scraper(registry, budget, writer)

        with self.assertRaises(OSError):
            scraper._scrape_result_and_store(Mock(), "continue", "query one", [1, 1])
        self.assertEqual(budget.snapshot()["reserved"], 0)
        self.assertEqual(budget.snapshot()["committed"], 0)
        self.assertFalse(registry.contains({"map_link": place_url(1)}))

        state["index"] = 2
        self.assertEqual(
            scraper._scrape_result_and_store(Mock(), "continue", "query two", [1, 1]),
            "new",
        )
        self.assertEqual(budget.snapshot()["committed"], 1)

    def test_known_and_same_run_duplicates_do_not_consume_budget(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_rows(
                root / "leads_master.csv",
                [{"name": "Known", "map_place_id": "data:0xlegacy:0001"}],
            )
            registry = KnownCompanies.from_directory(root, require_nonempty=True)
            registry.add({"map_place_id": "data:0xlegacy:0002"})
            budget = RunAcceptanceBudget(1)
            writer = Mock()
            observer = Mock()
            scraper, state = configure_scraper(
                registry, budget, writer, observer=observer, index=1,
            )
            self.assertEqual(
                scraper._scrape_result_and_store(Mock(), "continue", "known query", [1, 1]),
                "known",
            )
            state["index"] = 2
            self.assertEqual(
                scraper._scrape_result_and_store(Mock(), "continue", "duplicate query", [1, 1]),
                "same_run",
            )
            self.assertEqual(budget.snapshot()["committed"], 0)
            self.assertEqual(budget.snapshot()["reserved"], 0)
            writer.assert_not_called()
            self.assertEqual(
                [call.kwargs["duplicate_kind"] for call in observer.observe.call_args_list],
                ["known", "same_run"],
            )

    def test_concurrent_duplicate_acceptance_persists_once(self):
        registry = KnownCompanies()
        budget = RunAcceptanceBudget(5)
        persisted = []
        persisted_lock = Lock()

        def writer(*, list_of_dict_data):
            with persisted_lock:
                persisted.extend(list_of_dict_data)

        first, _ = configure_scraper(registry, budget, writer, index=7)
        second, _ = configure_scraper(registry, budget, writer, index=7)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    scraper._scrape_result_and_store,
                    Mock(), "continue", f"query {index}", [1, 1],
                )
                for index, scraper in enumerate((first, second))
            ]
            outcomes = [future.result(timeout=2) for future in futures]
        self.assertEqual(outcomes.count("new"), 1)
        self.assertEqual(outcomes.count("same_run"), 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(budget.snapshot()["committed"], 1)
        self.assertEqual(budget.snapshot()["reserved"], 0)

    def test_shared_coordinator_budget_is_global_across_workers(self):
        class FakeWorker:
            persisted = []
            lock = Lock()

            def __init__(self, **kwargs):
                self.budget = kwargs["acceptance_budget"]
                self.record_sink = kwargs["record_sink"]

            def start_scrapper(self, query):
                for index in range(20):
                    reservation = self.budget.try_reserve()
                    if reservation is None:
                        break
                    row = {"title": f"{query}-{index}", "map_link": f"{query}-{index}"}
                    with self.lock:
                        self.persisted.append(row)
                    self.record_sink(row)
                    reservation.commit()

            def quit_driver(self):
                pass

            def resource_metrics(self):
                return {}

            def query_states(self):
                return []

        with TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = FastSearchAlgo(
                incremental=True, result_range=5, workers=4,
                output_path=str(root / "output"),
                known_companies_dir=str(root / "history"),
                allow_empty_known_companies=True, verbose=False,
            )
            from unittest.mock import patch
            with patch("utils.threading_controller.GoogleMaps", FakeWorker):
                coordinator.fast_search_algorithm([f"query-{index}" for index in range(12)])
            self.assertEqual(len(FakeWorker.persisted), 5)
            self.assertLessEqual(len(coordinator.run_records()), 5)
            self.assertEqual(coordinator._acceptance_budget.snapshot()["committed"], 5)
