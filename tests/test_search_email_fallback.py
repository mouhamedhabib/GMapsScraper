"""Offline tests for the bounded Google Search email fallback."""

from csv import DictReader, DictWriter
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from utils.build_leads import MASTER_FIELDS, build_lead_files, is_valid_email
from utils.search_email_fallback import (
    OUTPUT_FIELDS,
    accepted_result_emails,
    build_queries,
    is_valid_email as fallback_validator,
    prepare_records,
    search_email_fallback,
)
from utils.google_search_discovery import wait_for_manual_verification


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(DictReader(handle))


def master_row(name="Acme", website="https://acme.test/", email=""):
    row = {field: "" for field in MASTER_FIELDS}
    row.update({"name": name, "website": website, "email": email, "source": "google_maps"})
    return row


def enrichment_row(name="Acme", website="https://acme.test/", status="NOT_FOUND"):
    row = master_row(name, website)
    row.update({"email_enrichment_status": status, "email_enrichment_attempts": "1"})
    return row


def result(title="Contact | Acme", url="https://acme.test/contact", snippet=""):
    return {"title": title, "url": url, "snippet": snippet}


class FakeDriver:
    def __init__(self):
        self.closed = False

    def quit(self):
        self.closed = True


class NoPageScraper:
    last_failure_kind = None

    def get_source_code(self, driver, urls):
        return []

    def get_pattern_data(self, sources):
        return {"site_email": []}


class SearchEmailFallbackTests(TestCase):
    def paths(self, rows, enrichment, previous=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_csv(root / "leads_master.csv", MASTER_FIELDS, rows)
        write_csv(
            root / "missing_email_enriched.csv",
            (*MASTER_FIELDS, "email_enrichment_status", "email_enrichment_attempts"),
            enrichment,
        )
        if previous is not None:
            write_csv(root / "search_email_fallback.csv", OUTPUT_FIELDS, previous)
        return root

    def run_batch(self, rows, enrichment, results_by_query=None, previous=None, **kwargs):
        root = self.paths(rows, enrichment, previous)
        calls = []

        def search(driver, query, limit, timeout, verbose=False):
            calls.append((query, timeout))
            outcome = (results_by_query or {}).get(query, [])
            if isinstance(outcome, Exception):
                raise outcome
            return outcome, ""

        summary = search_email_fallback(
            input_path=root / "leads_master.csv",
            enrichment_input_path=root / "missing_email_enriched.csv",
            output_path=root / "search_email_fallback.csv",
            driver_factory=FakeDriver,
            search_function=search,
            scraper_factory=lambda remaining: NoPageScraper(),
            **kwargs,
        )
        return read_csv(root / "search_email_fallback.csv"), summary, calls, root

    def test_not_found_and_failed_direct_enrichment_are_eligible(self):
        rows = [master_row("One", "https://one.test/"), master_row("Two", "https://two.test/")]
        direct = [
            enrichment_row("One", "https://one.test/", "NOT_FOUND"),
            enrichment_row("Two", "https://two.test/", "FAILED"),
        ]
        records, eligible = prepare_records(rows, direct, [])
        self.assertEqual(eligible, 2)
        self.assertEqual([row["status"] for row in records], ["PENDING", "PENDING"])

    def test_found_direct_enrichment_and_existing_valid_email_are_skipped(self):
        rows = [
            master_row("Found", "https://found.test/"),
            master_row("Has Email", "https://email.test/", "hello@email.test"),
        ]
        direct = [
            enrichment_row("Found", "https://found.test/", "FOUND"),
            enrichment_row("Has Email", "https://email.test/", "NOT_FOUND"),
        ]
        records, eligible = prepare_records(rows, direct, [])
        self.assertEqual(records, [])
        self.assertEqual(eligible, 0)

    def test_domain_result_is_accepted_and_queries_are_bounded(self):
        queries = build_queries("Acme", "acme.test")
        rows, summary, calls, _ = self.run_batch(
            [master_row()],
            [enrichment_row()],
            {queries[0]: [result(snippet="Email info@acme.test")]},
        )
        self.assertEqual(len(queries), 4)
        self.assertTrue(queries[0].startswith("site:acme.test"))
        self.assertEqual(rows[0]["email"], "info@acme.test")
        self.assertEqual(rows[0]["status"], "FOUND")
        self.assertEqual(summary["Found"], 1)
        self.assertEqual(len(calls), 1)

    def test_unrelated_directory_result_is_rejected(self):
        candidate = result(
            title="Acme",
            url="https://www.crunchbase.com/organization/acme",
            snippet="info@acme.test",
        )
        self.assertEqual(
            accepted_result_emails(candidate, "Acme", "acme.test", ["info@acme.test"]),
            [],
        )

    def test_email_domain_mismatch_needs_two_strong_signals(self):
        weak = result(title="Welcome", url="https://acme.test/", snippet="team@gmail.com")
        strong = result(title="Contact | Acme", url="https://acme.test/contact", snippet="team@gmail.com")
        self.assertEqual(accepted_result_emails(weak, "Acme", "acme.test", ["team@gmail.com"]), [])
        self.assertEqual(
            accepted_result_emails(strong, "Acme", "acme.test", ["team@gmail.com"]),
            ["team@gmail.com"],
        )

    def test_existing_production_validator_is_reused(self):
        self.assertIs(fallback_validator, is_valid_email)
        candidate = result(snippet="test@example.com")
        self.assertEqual(
            accepted_result_emails(candidate, "Acme", "acme.test", ["test@example.com"]),
            [],
        )

    def test_manual_captcha_flow_uses_existing_waiter(self):
        root = self.paths([master_row()], [enrichment_row()])

        def blocked(driver, query, limit, timeout, verbose=False):
            return [], "verify you are human"

        solved = [result(snippet="info@acme.test")]
        with patch(
            "utils.search_email_fallback.wait_for_manual_verification",
            return_value=(solved, False),
        ) as waiter:
            summary = search_email_fallback(
                root / "leads_master.csv",
                root / "missing_email_enriched.csv",
                root / "search_email_fallback.csv",
                windowed=True,
                driver_factory=FakeDriver,
                search_function=blocked,
                scraper_factory=lambda remaining: NoPageScraper(),
            )
        self.assertTrue(waiter.called)
        self.assertEqual(summary["Found"], 1)

    def test_company_deadline_is_shared_across_queries(self):
        class Clock:
            value = 0

            def __call__(self):
                return self.value

        clock = Clock()
        root = self.paths([master_row()], [enrichment_row()])
        calls = []

        def slow_search(driver, query, limit, timeout, verbose=False):
            calls.append(timeout)
            clock.value += 13
            return [], ""

        summary = search_email_fallback(
            root / "leads_master.csv",
            root / "missing_email_enriched.csv",
            root / "search_email_fallback.csv",
            timeout=12,
            driver_factory=FakeDriver,
            search_function=slow_search,
            scraper_factory=lambda remaining: NoPageScraper(),
            clock=clock,
        )
        self.assertEqual(calls, [12])
        self.assertEqual(summary["Failed"], 1)

    def test_one_failure_does_not_stop_next_lead(self):
        rows = [master_row("Broken", "https://broken.test/"), master_row("Working", "https://working.test/")]
        direct = [
            enrichment_row("Broken", "https://broken.test/"),
            enrichment_row("Working", "https://working.test/"),
        ]
        working_query = build_queries("Working", "working.test")[0]
        broken_query = build_queries("Broken", "broken.test")[0]
        output, summary, _, _ = self.run_batch(
            rows,
            direct,
            {
                broken_query: RuntimeError("ERR_NAME_NOT_RESOLVED"),
                working_query: [result("Contact | Working", "https://working.test/contact", "info@working.test")],
            },
        )
        self.assertEqual([row["status"] for row in output], ["FAILED", "FOUND"])
        self.assertEqual(summary["Attempted this run"], 2)

    def test_found_persists_and_is_not_requeried(self):
        previous = [{
            "company_name": "Acme", "website": "https://acme.test/",
            "normalized_domain": "acme.test", "email": "info@acme.test",
            "source_query": "site:acme.test email", "source_url": "https://acme.test/contact",
            "status": "FOUND", "attempts": "1",
        }]
        rows, summary, calls, _ = self.run_batch(
            [master_row()], [enrichment_row()], previous=previous,
        )
        self.assertEqual(rows[0]["status"], "FOUND")
        self.assertEqual(rows[0]["attempts"], "1")
        self.assertEqual(summary["Skipped already found"], 1)
        self.assertEqual(calls, [])

    def test_lifetime_attempt_bound_is_respected(self):
        previous = [{
            "company_name": "Acme", "website": "https://acme.test/",
            "normalized_domain": "acme.test", "email": "",
            "source_query": "", "source_url": "",
            "status": "NOT_FOUND", "attempts": "3",
        }]
        rows, summary, calls, _ = self.run_batch(
            [master_row()], [enrichment_row()], previous=previous,
        )
        self.assertEqual(rows[0]["status"], "NOT_FOUND")
        self.assertEqual(summary["Skipped max attempts"], 1)
        self.assertEqual(calls, [])

    def test_verbose_dns_failure_has_requested_label(self):
        root = self.paths([master_row()], [enrichment_row()])

        def dns_failure(driver, query, limit, timeout, verbose=False):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

        output = StringIO()
        with redirect_stdout(output):
            search_email_fallback(
                root / "leads_master.csv",
                root / "missing_email_enriched.csv",
                root / "search_email_fallback.csv",
                verbose=True,
                driver_factory=FakeDriver,
                search_function=dns_failure,
                scraper_factory=lambda remaining: NoPageScraper(),
            )
        self.assertIn("[FAILED_DNS] Acme", output.getvalue())

    def test_shared_manual_waiter_supports_fallback_callbacks(self):
        checkpoints = []
        expected = [result(snippet="info@acme.test")]
        with patch("utils.google_search_discovery.blocked_search_page", return_value=""):
            rows, stopped = wait_for_manual_verification(
                FakeDriver(),
                "site:acme.test email",
                5,
                12,
                Path("unused.csv"),
                [],
                input_function=lambda prompt: "",
                checkpoint_function=lambda: checkpoints.append(True),
                result_reader=lambda *args, **kwargs: (expected, ""),
            )
        self.assertEqual(rows, expected)
        self.assertFalse(stopped)
        self.assertEqual(checkpoints, [True])

    def test_old_csv_schemas_are_compatible(self):
        master = {"name": "Old", "email": "", "website": "https://old.test/"}
        direct = {"name": "Old", "website": "https://old.test/", "email_enrichment_status": "NOT_FOUND"}
        records, eligible = prepare_records([master], [direct], [])
        self.assertEqual(eligible, 1)
        self.assertEqual(records[0]["company_name"], "Old")


class SearchFallbackBuildIntegrationTests(TestCase):
    def build_with_fallback(self, email):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_csv(
            root / "google_maps_data.csv",
            ("title", "webpage", "phone_number", "site_email"),
            [{"title": "Acme", "webpage": "https://acme.test/", "phone_number": "", "site_email": ""}],
        )
        fallback = {
            "company_name": "Acme", "website": "https://acme.test/",
            "normalized_domain": "acme.test", "email": email,
            "source_query": "site:acme.test email", "source_url": "https://acme.test/contact",
            "status": "FOUND", "attempts": "1",
        }
        write_csv(root / "search_email_fallback.csv", OUTPUT_FIELDS, [fallback])
        build_lead_files(root / "google_maps_data.csv", root)
        return read_csv(root / "leads_master.csv")[0]

    def test_build_merges_valid_fallback_email(self):
        built = self.build_with_fallback("info@acme.test")
        self.assertEqual(built["email"], "info@acme.test")
        self.assertEqual(built["source"], "google_maps")

    def test_build_rejects_invalid_fallback_email(self):
        self.assertEqual(self.build_with_fallback("test@example.com")["email"], "")
