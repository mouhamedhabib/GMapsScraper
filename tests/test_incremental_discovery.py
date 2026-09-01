"""Offline coverage for incremental Maps and Search discovery."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from utils.google_maps_scraper import GoogleMaps
from utils.google_search_discovery import DISCOVERY_FIELDS, discover_companies
from utils.known_companies import KnownCompanies


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class KnownCompanyTests(TestCase):
    def test_existing_domain_matches_www_path(self):
        registry = KnownCompanies()
        registry.add({"website": "https://example.com"})
        self.assertTrue(registry.contains({"source_url": "https://www.example.com/contact"}))

    def test_known_place_id_matches_maps_url(self):
        registry = KnownCompanies()
        registry.add({"place_id": "ChIJabc"})
        self.assertTrue(registry.contains({
            "map_link": "https://www.google.com/maps/place/X/@1,2/data=!4m2!3m1!1sChIJabc!8m2"
        }))

    def test_exact_name_and_phone_matches(self):
        registry = KnownCompanies()
        registry.add({"name": "Acme Labs", "phone": "+216 71 234 567"})
        self.assertTrue(registry.contains({
            "title": "acme labs", "phone_number": "+216 (71) 234-567"
        }))

    def test_similar_names_with_different_strong_identities_do_not_match(self):
        registry = KnownCompanies()
        registry.add({
            "name": "Acme Labs", "phone": "+216 71 111 111",
            "website": "https://acme-one.test",
        })
        self.assertFalse(registry.contains({
            "title": "Acme Lab", "phone_number": "+216 71 222 222",
            "webpage": "https://acme-two.test",
        }))

    def test_same_run_duplicate_is_rejected_on_second_query(self):
        registry = KnownCompanies()
        first = {"website": "https://same-company.test/about"}
        second = {"website": "https://www.same-company.test/contact"}
        self.assertTrue(registry.check_and_add(first))
        self.assertFalse(registry.check_and_add(second))
        self.assertEqual(registry.duplicate_kind(second), "same_run")

    def test_old_csv_schema_and_restart_loading(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "google_maps_data.csv",
                ("title", "webpage"),
                [{"title": "Old Co", "webpage": "https://old.test/about"}],
            )
            first = KnownCompanies.from_directory(root)
            self.assertTrue(first.contains({"website": "https://www.old.test/contact"}))
            first.add({"website": "https://new.test"})
            write_csv(
                root / "google_search_companies.csv",
                ("company_name", "website"),
                [{"company_name": "New", "website": "https://new.test/"}],
            )
            restarted = KnownCompanies.from_directory(root)
            self.assertTrue(restarted.contains({"webpage": "https://new.test/team"}))


class FakeResult:
    def __init__(self, href=""):
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else ""


class FakeMapsDriver:
    current_window_handle = "main"

    def close(self):
        pass


class MapsIncrementalTests(TestCase):
    def test_known_result_avoids_expensive_enrichment(self):
        registry = KnownCompanies()
        maps_url = "https://www.google.com/maps/place/X/@1,2/data=!4m2!3m1!1sChIJKnown!8m2"
        registry.add({"map_link": maps_url})
        scraper = GoogleMaps(incremental=True, known_companies=registry, verbose=False)
        scraper.validate_result_link = lambda result, driver: ("1", "2", maps_url)
        scraper.get_title = lambda driver: "Known Company"
        scraper.get_website_link = lambda driver: "https://known.test"
        scraper.get_phone_number = lambda driver: "+1 555 111 1111"
        scraper.reset_driver_for_next_run = lambda result, driver: None

        def should_not_run(*args, **kwargs):
            self.fail("expensive enrichment ran for a known company")

        scraper._web_pattern_scraper.find_patterns = should_not_run
        scraper.get_about_description = should_not_run
        self.assertEqual(
            scraper._scrape_result_and_store(FakeMapsDriver(), "continue", "query", [1, 1]),
            "same_run",
        )

    def test_limit_counts_new_companies_not_first_results(self):
        class LimitScraper(GoogleMaps):
            def create_chrome_driver(self):
                return FakeMapsDriver()

            def load_url(self, driver, url):
                pass

            def search_query(self, query):
                pass

            def scroll_to_the_end_event(self, driver):
                return [FakeResult() for _ in range(27)]

            def _scrape_result_and_store(self, driver, result, query, results_indices):
                return "known" if results_indices[1] <= 12 else "new"

        summary = {"queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0}
        scraper = LimitScraper(
            incremental=True, result_range=15, known_companies=KnownCompanies(),
            summary=summary, stop_event=Event(), verbose=False,
        )
        scraper.start_scrapper("query")
        self.assertEqual(summary["inspected"], 27)
        self.assertEqual(summary["known"], 12)
        self.assertEqual(summary["new"], 15)


class FakeSearchDriver:
    def quit(self):
        pass


class SearchIncrementalTests(TestCase):
    def test_known_domains_are_skipped_and_later_pages_continue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            query_path = root / "queries.txt"
            query_path.write_text("software Paris\n", encoding="utf-8")
            output = root / "google_search_companies.csv"
            known = [
                {
                    "company_name": f"Known {index}",
                    "website": f"https://known{index}.test/",
                    "source": "google_search", "source_query": "old",
                    "source_url": f"https://known{index}.test/",
                    "country": "", "city": "", "location": "",
                }
                for index in range(12)
            ]
            write_csv(output, DISCOVERY_FIELDS, known)

            pages = []
            candidates = [
                {"company_name": f"Known {index}", "source_url": f"https://known{index}.test/about", "source_query": "software Paris"}
                for index in range(12)
            ] + [
                {"company_name": f"New {index}", "source_url": f"https://new{index}.test/about", "source_query": "software Paris"}
                for index in range(15)
            ]
            for offset in range(0, len(candidates), 10):
                pages.append(candidates[offset:offset + 10])

            calls = []

            def fake_search(driver, query, limit, timeout, verbose=False, start=0):
                calls.append(start)
                page = pages[start // 10] if start // 10 < len(pages) else []
                return page[:limit], ""

            with patch("utils.google_search_discovery.search_query", side_effect=fake_search):
                rows = discover_companies(
                    query_file=query_path, output_path=output, limit=15,
                    delay=0, incremental=True,
                    driver_factory=lambda windowed=False: FakeSearchDriver(),
                )

            self.assertEqual(len(rows), 27)
            self.assertEqual(calls, [0, 10, 20])
            with output.open(encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(DictReader(handle))), 27)
