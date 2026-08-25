"""Offline tests for Search-only website email enrichment."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.build_leads import MASTER_FIELDS, build_lead_files
from utils.enrich_search_emails import (
    OUTPUT_FIELDS,
    atomic_write_csv,
    enrich_search_emails,
)
from utils.google_search_discovery import DISCOVERY_FIELDS


class FakeDriver:
    def __init__(self):
        self.closed = False

    def quit(self):
        self.closed = True


class FakeScraper:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []
        self.current = []

    @staticmethod
    def create_urls(website, paths):
        return [website, *(
            website.rstrip("/") + "/" + path
            for path in paths
        )]

    def get_source_code(self, driver, urls):
        website = urls[0]
        self.calls.append(website)
        outcome = self.outcomes.get(website, [])
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return []
        self.current = outcome
        return ["<html>loaded</html>"]

    def get_pattern_data(self, sources):
        return {"site_email": self.current}


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def master_row(
    name="Search Company",
    website="https://search.test/",
    source="google_search",
    email="",
):
    return {
        "name": name,
        "email": email,
        "alternate_emails": "",
        "phone": "",
        "website": website,
        "email_status": "",
        "review_status": "READY",
        "review_reasons": "",
        "source": source,
        "source_queries": "software Tunisia",
    }


class SearchEmailEnrichmentTests(TestCase):
    def run_batch(self, rows, outcomes, existing=None, **kwargs):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        input_path = root / "leads_master.csv"
        output_path = root / "search_email_enriched.csv"
        write_csv(input_path, MASTER_FIELDS, rows)
        if existing is not None:
            atomic_write_csv(output_path, existing)
        scraper = FakeScraper(outcomes)
        driver = FakeDriver()
        summary = enrich_search_emails(
            input_path=input_path,
            output_path=output_path,
            driver_factory=lambda: driver,
            scraper_factory=lambda: scraper,
            **kwargs,
        )
        return read_csv(output_path), summary, scraper, driver

    def test_search_only_found_email(self):
        rows, _, _, _ = self.run_batch(
            [master_row()], {"https://search.test/": ["info@search.test"]}
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(rows[0]["email"], "info@search.test")
        self.assertEqual(rows[0]["email_enrichment_attempts"], "1")

    def test_search_only_no_email_is_not_found(self):
        rows, _, _, _ = self.run_batch([master_row()], {"https://search.test/": []})
        self.assertEqual(rows[0]["email_enrichment_status"], "NOT_FOUND")

    def test_search_only_without_website_is_not_opened(self):
        rows, summary, scraper, _ = self.run_batch([master_row(website="")], {})
        self.assertEqual(rows[0]["email_enrichment_status"], "NO_WEBSITE")
        self.assertEqual(summary["Attempted this run"], 0)
        self.assertEqual(scraper.calls, [])

    def test_maps_and_mixed_rows_are_ignored(self):
        rows, summary, scraper, _ = self.run_batch([
            master_row(name="Maps", website="https://maps.test/", source="google_maps"),
            master_row(
                name="Mixed",
                website="https://mixed.test/",
                source="google_maps;google_search",
            ),
        ], {})
        self.assertEqual(summary["Attempted this run"], 0)
        self.assertEqual(scraper.calls, [])
        self.assertTrue(all(not row["email_enrichment_status"] for row in rows))

    def test_invalid_email_is_rejected(self):
        rows, _, _, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": ["test@example.com", "image@file.png"]},
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "NOT_FOUND")
        self.assertEqual(rows[0]["email"], "")

    def test_domain_email_preferred_and_duplicates_removed(self):
        rows, _, _, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": [
                "hello@gmail.com",
                "INFO@search.test",
                "info@search.test",
                "sales@search.test",
            ]},
        )
        self.assertEqual(rows[0]["email"].casefold(), "info@search.test")
        alternates = rows[0]["alternate_emails"].casefold().split(";")
        self.assertEqual(len(alternates), 2)
        self.assertIn("sales@search.test", alternates)
        self.assertIn("hello@gmail.com", alternates)

    def test_found_rerun_is_idempotent_and_preserved(self):
        first, _, _, _ = self.run_batch(
            [master_row()], {"https://search.test/": ["info@search.test"]}
        )
        second, summary, scraper, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": RuntimeError("later failure")},
            existing=first,
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(second[0]["email"], "info@search.test")
        self.assertEqual(second[0]["email_enrichment_attempts"], "1")
        self.assertEqual(summary["Attempted this run"], 0)
        self.assertEqual(scraper.calls, [])

    def test_max_attempts_are_respected(self):
        prior = master_row()
        prior.update({
            "email_enrichment_status": "NOT_FOUND",
            "email_enrichment_attempts": "3",
        })
        rows, summary, scraper, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": ["info@search.test"]},
            existing=[prior],
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "NOT_FOUND")
        self.assertEqual(summary["Skipped max attempts"], 1)
        self.assertEqual(scraper.calls, [])

    def test_unexpected_failure_is_persisted(self):
        rows, _, _, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": RuntimeError("broken page")},
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "FAILED")
        self.assertEqual(rows[0]["email_enrichment_attempts"], "1")

    def test_retry_failed_retries_within_lifetime_limit(self):
        prior = master_row()
        prior.update({
            "email_enrichment_status": "FAILED",
            "email_enrichment_attempts": "1",
        })
        rows, _, _, _ = self.run_batch(
            [master_row()],
            {"https://search.test/": ["info@search.test"]},
            existing=[prior],
            retry_failed=True,
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(rows[0]["email_enrichment_attempts"], "2")

    def test_driver_failure_recreates_session_once_and_continues(self):
        class WebDriverException(RuntimeError):
            pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "leads_master.csv"
            output_path = root / "search_email_enriched.csv"
            write_csv(input_path, MASTER_FIELDS, [
                master_row(name="Broken", website="https://broken.test/"),
                master_row(name="Working", website="https://working.test/"),
            ])
            scraper = FakeScraper({
                "https://broken.test/": WebDriverException("driver crashed"),
                "https://working.test/": ["info@working.test"],
            })
            drivers = []

            def driver_factory():
                driver = FakeDriver()
                drivers.append(driver)
                return driver

            enrich_search_emails(
                input_path=input_path,
                output_path=output_path,
                driver_factory=driver_factory,
                scraper_factory=lambda: scraper,
            )
            rows = read_csv(output_path)

            self.assertEqual(len(drivers), 2)
            self.assertTrue(all(driver.closed for driver in drivers))
            self.assertEqual(rows[0]["email_enrichment_status"], "FAILED")
            self.assertEqual(rows[1]["email_enrichment_status"], "FOUND")


class SearchEmailBuildMergeTests(TestCase):
    def test_enriched_search_email_enters_ready_without_duplicate_master(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "google_maps_data.csv",
                ("title", "webpage", "phone_number", "site_email"),
                [],
            )
            discovery = {
                "company_name": "Search Company",
                "website": "https://search.test/",
                "source": "google_search",
                "source_query": "software Tunisia",
                "source_url": "https://search.test/about",
            }
            write_csv(
                root / "google_search_companies.csv",
                DISCOVERY_FIELDS,
                [discovery],
            )
            enriched = master_row()
            enriched.update({
                "email": "info@search.test",
                "email_status": "MATCH",
                "email_enrichment_status": "FOUND",
                "email_enrichment_attempts": "1",
            })
            atomic_write_csv(root / "search_email_enriched.csv", [enriched])

            build_lead_files(root / "google_maps_data.csv", root)
            master = read_csv(root / "leads_master.csv")
            ready = read_csv(root / "leads_ready.csv")

            self.assertEqual(len(master), 1)
            self.assertEqual(master[0]["email"], "info@search.test")
            self.assertEqual(master[0]["source"], "google_search")
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0]["email"], "info@search.test")


if __name__ == "__main__":
    main()
