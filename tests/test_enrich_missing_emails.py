"""Offline tests for source-agnostic missing-email enrichment."""

from csv import DictReader, DictWriter
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from unittest import TestCase

from utils.build_leads import MASTER_FIELDS, build_lead_files
from utils.enrich_missing_emails import enrich_missing_emails
from utils.enrich_search_emails import (
    OUTPUT_FIELDS,
    WebsiteCheckFailed,
    atomic_write_csv,
    extract_public_emails,
)
from utils.web_site_scraper import PatternScrapper
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
        return [website, *(website.rstrip("/") + "/" + path for path in paths)]

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
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(DictReader(handle))


def master_row(
    name="Company",
    website="https://company.test/",
    source="google_maps",
    email="",
    country="Tunisia",
    city="Tunis",
    location="Tunis, Tunisia",
):
    row = {field: "" for field in MASTER_FIELDS}
    row.update({
        "name": name,
        "website": website,
        "source": source,
        "email": email,
        "country": country,
        "city": city,
        "location": location,
    })
    return row


class MissingEmailEnrichmentTests(TestCase):
    def run_batch(self, rows, outcomes, existing=None, **kwargs):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        input_path = root / "leads_master.csv"
        output_path = root / "missing_email_enriched.csv"
        write_csv(input_path, MASTER_FIELDS, rows)
        if existing is not None:
            atomic_write_csv(output_path, existing)
        scraper = FakeScraper(outcomes)
        drivers = []

        def driver_factory():
            driver = FakeDriver()
            drivers.append(driver)
            return driver

        summary = enrich_missing_emails(
            input_path=input_path,
            output_path=output_path,
            driver_factory=driver_factory,
            scraper_factory=lambda: scraper,
            **kwargs,
        )
        return read_csv(output_path), summary, scraper, drivers

    def test_all_sources_are_eligible_and_share_one_browser(self):
        rows = [
            master_row("Maps", "https://maps.test/", "google_maps"),
            master_row("Search", "https://search.test/", "google_search"),
            master_row("Mixed", "https://mixed.test/", "google_maps;google_search"),
        ]
        outcomes = {
            "https://maps.test/": ["info@maps.test"],
            "https://search.test/": ["info@search.test"],
            "https://mixed.test/": ["info@mixed.test"],
        }
        enriched, summary, scraper, drivers = self.run_batch(rows, outcomes)
        self.assertEqual([row["email_enrichment_status"] for row in enriched], ["FOUND"] * 3)
        self.assertEqual(summary["Eligible with website"], 3)
        self.assertEqual(len(scraper.calls), 3)
        self.assertEqual(len(drivers), 1)
        self.assertTrue(drivers[0].closed)

    def test_existing_valid_email_is_skipped_without_opening(self):
        rows, summary, scraper, drivers = self.run_batch(
            [master_row(email="hello@company.test")],
            {"https://company.test/": RuntimeError("must not open")},
        )
        self.assertEqual(summary["Missing-email leads"], 0)
        self.assertEqual(summary["Attempted this run"], 0)
        self.assertEqual(scraper.calls, [])
        self.assertEqual(drivers, [])
        self.assertEqual(rows[0]["email"], "hello@company.test")

    def test_missing_or_unusable_website_is_not_scraped(self):
        rows, summary, scraper, drivers = self.run_batch([
            master_row("Missing", website=""),
            master_row("Bad Scheme", website="ftp://files.test"),
        ], {})
        self.assertEqual(summary["Without website"], 2)
        self.assertTrue(all(row["email_enrichment_status"] == "NO_WEBSITE" for row in rows))
        self.assertEqual(scraper.calls, [])
        self.assertEqual(drivers, [])

    def test_found_result_persists_across_rerun(self):
        first, _, _, _ = self.run_batch(
            [master_row()], {"https://company.test/": ["info@company.test"]}
        )
        second, summary, scraper, drivers = self.run_batch(
            [master_row()],
            {"https://company.test/": RuntimeError("must not retry")},
            existing=first,
        )
        self.assertEqual(second[0]["email"], "info@company.test")
        self.assertEqual(second[0]["email_enrichment_attempts"], "1")
        self.assertEqual(summary["Skipped existing result"], 1)
        self.assertEqual(scraper.calls, [])
        self.assertEqual(drivers, [])

    def test_not_found_stops_at_lifetime_max(self):
        prior = master_row()
        prior.update({
            "email_enrichment_status": "NOT_FOUND",
            "email_enrichment_attempts": "3",
        })
        rows, summary, scraper, drivers = self.run_batch(
            [master_row()],
            {"https://company.test/": ["info@company.test"]},
            existing=[prior],
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "NOT_FOUND")
        self.assertEqual(summary["Skipped max attempts"], 1)
        self.assertEqual(scraper.calls, [])
        self.assertEqual(drivers, [])

    def test_failed_requires_retry_flag_and_still_respects_max(self):
        prior = master_row()
        prior.update({"email_enrichment_status": "FAILED", "email_enrichment_attempts": "1"})
        skipped, summary, scraper, _ = self.run_batch(
            [master_row()], {"https://company.test/": ["info@company.test"]},
            existing=[prior],
        )
        self.assertEqual(skipped[0]["email_enrichment_status"], "FAILED")
        self.assertEqual(summary["Skipped existing result"], 1)
        self.assertEqual(scraper.calls, [])

        found, _, _, _ = self.run_batch(
            [master_row()], {"https://company.test/": ["info@company.test"]},
            existing=[prior], retry_failed=True,
        )
        self.assertEqual(found[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(found[0]["email_enrichment_attempts"], "2")

        prior["email_enrichment_attempts"] = "3"
        capped, summary, scraper, _ = self.run_batch(
            [master_row()], {"https://company.test/": ["info@company.test"]},
            existing=[prior], retry_failed=True,
        )
        self.assertEqual(capped[0]["email_enrichment_status"], "FAILED")
        self.assertEqual(summary["Skipped max attempts"], 1)
        self.assertEqual(scraper.calls, [])

    def test_invalid_placeholder_email_is_rejected_and_geography_survives(self):
        rows, summary, _, _ = self.run_batch(
            [master_row()],
            {"https://company.test/": ["test@example.com", "image@file.png"]},
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "NOT_FOUND")
        self.assertEqual(rows[0]["email"], "")
        self.assertEqual(summary["Not found"], 1)
        self.assertEqual(rows[0]["country"], "Tunisia")
        self.assertEqual(rows[0]["city"], "Tunis")
        self.assertEqual(rows[0]["location"], "Tunis, Tunisia")

    def test_enrichment_interprets_real_scraper_email_list_as_found(self):
        scraper = PatternScrapper(verbose=False)
        sources = ['<html><body>sales@company.test</body></html>']
        scraper.get_source_code = lambda driver, urls: sources
        emails = extract_public_emails(scraper, FakeDriver(), "https://company.test/")
        self.assertEqual(emails, ["sales@company.test"])

        rows, _, _, _ = self.run_batch(
            [master_row()],
            {"https://company.test/": emails},
        )
        self.assertEqual(rows[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(rows[0]["email"], "sales@company.test")

    def test_partial_pages_followed_by_timeout_are_failed_not_not_found(self):
        scraper = PatternScrapper(verbose=False)
        scraper.get_source_code = lambda driver, urls: ["<html><body>No email</body></html>"]
        scraper.last_failure_kind = "timeout"
        scraper.last_failure_message = "overall company deadline exceeded"
        with self.assertRaises(WebsiteCheckFailed) as raised:
            extract_public_emails(scraper, FakeDriver(), "https://company.test/")
        self.assertEqual(raised.exception.kind, "timeout")

    def test_one_failure_does_not_stop_later_leads(self):
        rows, summary, scraper, _ = self.run_batch([
            master_row("Broken", "https://broken.test/"),
            master_row("Working", "https://working.test/"),
        ], {
            "https://broken.test/": RuntimeError("broken website"),
            "https://working.test/": ["info@working.test"],
        })
        self.assertEqual(rows[0]["email_enrichment_status"], "FAILED")
        self.assertEqual(rows[1]["email_enrichment_status"], "FOUND")
        self.assertEqual(summary["Attempted this run"], 2)
        self.assertEqual(len(scraper.calls), 2)

    def test_verbose_dns_failure_uses_concise_diagnostic_label(self):
        class DnsScraper(FakeScraper):
            last_failure_kind = "dns"
            last_failure_message = "dead.test could not be resolved"

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        input_path = root / "leads_master.csv"
        output_path = root / "missing_email_enriched.csv"
        write_csv(input_path, MASTER_FIELDS, [master_row("Dead", "https://dead.test/")])
        output = StringIO()
        with redirect_stdout(output):
            enrich_missing_emails(
                input_path=input_path,
                output_path=output_path,
                verbose=True,
                driver_factory=FakeDriver,
                scraper_factory=lambda: DnsScraper({"https://dead.test/": None}),
            )
        self.assertIn("[FAILED_DNS] Dead - dead.test could not be resolved", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_limit_bounds_attempts_without_losing_pending_rows(self):
        rows, summary, scraper, _ = self.run_batch([
            master_row("One", "https://one.test/"),
            master_row("Two", "https://two.test/"),
        ], {
            "https://one.test/": ["info@one.test"],
            "https://two.test/": ["info@two.test"],
        }, limit=1)
        self.assertEqual(summary["Attempted this run"], 1)
        self.assertEqual(len(scraper.calls), 1)
        self.assertEqual(rows[0]["email_enrichment_status"], "FOUND")
        self.assertEqual(rows[1]["email_enrichment_status"], "PENDING")

    def test_random_sample_is_seeded_and_not_the_ordered_prefix(self):
        rows = [
            master_row(f"Company {index}", f"https://company{index}.test/")
            for index in range(10)
        ]
        outcomes = {row["website"]: [] for row in rows}
        _, first_summary, first_scraper, _ = self.run_batch(
            rows, outcomes, limit=3, random_sample=True, seed=42,
        )
        _, second_summary, second_scraper, _ = self.run_batch(
            rows, outcomes, limit=3, random_sample=True, seed=42,
        )
        expected = {
            row["website"] for row in Random(42).sample(rows, 3)
        }
        self.assertEqual(set(first_scraper.calls), expected)
        self.assertEqual(first_scraper.calls, second_scraper.calls)
        self.assertNotEqual(first_scraper.calls, [row["website"] for row in rows[:3]])
        self.assertEqual(first_summary["Attempted this run"], 3)
        self.assertEqual(second_summary["Attempted this run"], 3)

    def test_random_sample_excludes_terminal_and_ineligible_retry_states(self):
        rows = [
            master_row("Found", "https://found.test/"),
            master_row("Capped", "https://capped.test/"),
            master_row("Failed", "https://failed.test/"),
            master_row("Pending", "https://pending.test/"),
        ]
        found, capped, failed = (dict(row) for row in rows[:3])
        found.update({
            "email": "info@found.test",
            "email_enrichment_status": "FOUND",
            "email_enrichment_attempts": "1",
        })
        capped.update({
            "email_enrichment_status": "NOT_FOUND",
            "email_enrichment_attempts": "3",
        })
        failed.update({
            "email_enrichment_status": "FAILED",
            "email_enrichment_attempts": "1",
        })
        _, summary, scraper, _ = self.run_batch(
            rows,
            {"https://pending.test/": ["info@pending.test"]},
            existing=[found, capped, failed],
            limit=4,
            random_sample=True,
            seed=42,
        )
        self.assertEqual(scraper.calls, ["https://pending.test/"])
        self.assertEqual(summary["Attempted this run"], 1)
        self.assertEqual(summary["Skipped existing result"], 2)
        self.assertEqual(summary["Skipped max attempts"], 1)

        _, retry_summary, retry_scraper, _ = self.run_batch(
            rows,
            {
                "https://failed.test/": ["info@failed.test"],
                "https://pending.test/": ["info@pending.test"],
            },
            existing=[found, capped, failed],
            limit=4,
            random_sample=True,
            seed=42,
            retry_failed=True,
        )
        self.assertEqual(
            set(retry_scraper.calls),
            {"https://failed.test/", "https://pending.test/"},
        )
        self.assertEqual(retry_summary["Attempted this run"], 2)

    def test_old_input_schema_does_not_crash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "leads_master.csv",
                ("name", "email", "website", "source"),
                [{
                    "name": "Old", "email": "", "website": "https://old.test/",
                    "source": "google_maps",
                }],
            )
            enrich_missing_emails(
                input_path=root / "leads_master.csv",
                output_path=root / "missing_email_enriched.csv",
                driver_factory=lambda: FakeDriver(),
                scraper_factory=lambda: FakeScraper({"https://old.test/": ["info@old.test"]}),
            )
            rows = read_csv(root / "missing_email_enriched.csv")
            self.assertEqual(tuple(rows[0]), OUTPUT_FIELDS)
            self.assertEqual(rows[0]["email"], "info@old.test")


class MissingEmailBuildIntegrationTests(TestCase):
    def test_build_consumes_both_enrichment_files_without_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "google_maps_data.csv",
                ("title", "webpage", "phone_number", "site_email"),
                [{
                    "title": "Maps Company", "webpage": "https://maps.test/",
                    "phone_number": "+216 70 000 001", "site_email": "",
                }],
            )
            write_csv(
                root / "google_search_companies.csv",
                DISCOVERY_FIELDS,
                [{
                    "company_name": "Search Company", "website": "https://search.test/",
                    "source": "google_search", "source_query": "software Tunisia",
                    "source_url": "https://search.test/about", "country": "Tunisia",
                    "city": "", "location": "Tunisia",
                }],
            )
            maps_enriched = master_row("Maps Company", "https://maps.test/", "google_maps")
            maps_enriched.update({
                "email": "info@maps.test", "email_enrichment_status": "FOUND",
                "email_enrichment_attempts": "1",
            })
            search_enriched = master_row("Search Company", "https://search.test/", "google_search")
            search_enriched.update({
                "email": "info@search.test", "email_enrichment_status": "FOUND",
                "email_enrichment_attempts": "1",
            })
            atomic_write_csv(root / "missing_email_enriched.csv", [maps_enriched])
            atomic_write_csv(root / "search_email_enriched.csv", [search_enriched])

            build_lead_files(root / "google_maps_data.csv", root)
            master = read_csv(root / "leads_master.csv")
            self.assertEqual(len(master), 2)
            self.assertEqual(
                {row["email"] for row in master},
                {"info@maps.test", "info@search.test"},
            )

    def test_build_revalidates_enrichment_email(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "google_maps_data.csv",
                ("title", "webpage", "phone_number", "site_email"),
                [{
                    "title": "Maps Company", "webpage": "https://maps.test/",
                    "phone_number": "", "site_email": "",
                }],
            )
            invalid = master_row("Maps Company", "https://maps.test/", "google_maps")
            invalid.update({
                "email": "test@example.com", "email_enrichment_status": "FOUND",
                "email_enrichment_attempts": "1",
            })
            atomic_write_csv(root / "missing_email_enriched.csv", [invalid])
            build_lead_files(root / "google_maps_data.csv", root)
            master = read_csv(root / "leads_master.csv")
            self.assertEqual(len(master), 1)
            self.assertEqual(master[0]["email"], "")
