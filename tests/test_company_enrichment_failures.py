"""Regression tests for company website inspection failure classification."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException, WebDriverException

from utils import company_enrichment
from utils.enrich_leads import (
    ENRICHMENT_FIELDS,
    enrich_leads_batch,
    enrichment_status,
    inspection_failure_metadata,
    merge_enrichment,
)


EMPTY_HTML = "<html><head><title>Home</title></head><body>Hi</body></html>"
USEFUL_HOME_HTML = """
<html><head>
  <title>Acme Software</title>
  <meta name="description" content="Acme builds business software for growing teams.">
</head><body><main><h1>Business software for growing teams</h1></main></body></html>
""" + (" " * 220)
USEFUL_ABOUT_HTML = """
<html><body><main><h1>About us</h1><p>
Acme builds custom software applications and SaaS platforms for growing
businesses and distributed teams around the world.
</p></main></body></html>
""" + (" " * 220)


class ImmediateWait:
    def __init__(self, driver, timeout, poll_frequency=None):
        self.driver = driver

    def until(self, predicate):
        result = None
        for _ in range(4):
            result = predicate(self.driver)
            if result:
                return result
        raise TimeoutException("condition did not become true")


class FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, kind):
        handle = f"tab-{self.driver.next_handle}"
        self.driver.next_handle += 1
        self.driver.window_handles.append(handle)
        self.driver.current_window_handle = handle

    def window(self, handle):
        self.driver.current_window_handle = handle


class FakeNavigationDriver:
    def __init__(self, navigate):
        self.navigate = navigate
        self.current_window_handle = "maps"
        self.window_handles = ["maps"]
        self.next_handle = 1
        self.switch_to = FakeSwitchTo(self)
        self.current_url = ""
        self.page_source = EMPTY_HTML
        self.visible_text = "Hi"
        self.calls = []

    def set_page_load_timeout(self, timeout):
        pass

    def get(self, url):
        self.calls.append(url)
        self.current_url = url
        source, visible = self.navigate(url, len(self.calls))
        self.page_source = source
        self.visible_text = visible

    def execute_script(self, script):
        if "readyState" in script:
            return "complete"
        if "innerText" in script:
            return self.visible_text
        return None

    def find_elements(self, by, value):
        return [object()]

    def close(self):
        self.window_handles.remove(self.current_window_handle)

    def quit(self):
        pass


def classified(result):
    record = {
        "website": "https://example.test",
        "enrichment_attempts": "1",
        **{field: "" for field in ENRICHMENT_FIELDS},
    }
    merge_enrichment(record, result)
    failed, _ = inspection_failure_metadata(result)
    return enrichment_status(record, inspection_failure=failed)


def all_navigation_fails(error):
    def navigate(url, call_number):
        raise error
    driver = FakeNavigationDriver(navigate)
    with patch.object(company_enrichment, "WebDriverWait", ImmediateWait):
        return company_enrichment.enrich_company(driver, "https://example.test")


class CompanyInspectionFailureTests(TestCase):
    def test_all_pages_timeout_is_failed(self):
        result = all_navigation_fails(TimeoutException("timed out"))
        self.assertEqual(classified(result), "FAILED")
        self.assertEqual(result["_meta"]["failure_type"], "TIMEOUT")
        self.assertEqual(result["_meta"]["pages_loaded"], 0)

    def test_dns_failure_before_any_page_load_is_failed(self):
        result = all_navigation_fails(
            WebDriverException("net::ERR_NAME_NOT_RESOLVED")
        )
        self.assertEqual(classified(result), "FAILED")
        self.assertEqual(result["_meta"]["failure_type"], "DNS")

    def test_connection_refusal_on_all_pages_is_failed(self):
        result = all_navigation_fails(
            WebDriverException("net::ERR_CONNECTION_REFUSED")
        )
        self.assertEqual(classified(result), "FAILED")
        self.assertEqual(result["_meta"]["failure_type"], "CONNECTION")

    def test_homepage_loads_without_useful_content_is_incomplete(self):
        driver = FakeNavigationDriver(
            lambda url, call_number: (EMPTY_HTML, "Hi")
        )
        with patch.object(company_enrichment, "WebDriverWait", ImmediateWait):
            result = company_enrichment.enrich_company(
                driver, "https://example.test"
            )
        self.assertEqual(classified(result), "INCOMPLETE")
        self.assertGreater(result["_meta"]["pages_loaded"], 0)

    def test_empty_homepage_and_useful_about_page_is_partial_or_success(self):
        def navigate(url, call_number):
            if "/about" in url:
                return USEFUL_ABOUT_HTML, (
                    "Acme builds custom software applications and SaaS platforms "
                    "for growing businesses and distributed teams around the world."
                )
            return EMPTY_HTML, "Hi"

        driver = FakeNavigationDriver(navigate)
        with patch.object(company_enrichment, "WebDriverWait", ImmediateWait):
            result = company_enrichment.enrich_company(
                driver, "https://example.test"
            )
        self.assertIn(classified(result), {"SUCCESS", "PARTIAL"})
        self.assertTrue(result["about_text"] or result["description"])

    def test_one_page_loads_then_later_pages_fail_is_incomplete(self):
        def navigate(url, call_number):
            if call_number <= 2:
                return EMPTY_HTML, "Hi"
            raise WebDriverException("net::ERR_CONNECTION_REFUSED")

        driver = FakeNavigationDriver(navigate)
        with patch.object(company_enrichment, "WebDriverWait", ImmediateWait):
            result = company_enrichment.enrich_company(
                driver, "https://example.test"
            )
        self.assertEqual(classified(result), "INCOMPLETE")
        self.assertEqual(result["_meta"]["pages_loaded"], 1)
        self.assertTrue(result["_meta"]["had_navigation_failure"])

    def test_useful_fields_survive_later_failures(self):
        def navigate(url, call_number):
            if call_number == 1:
                return USEFUL_HOME_HTML, (
                    "Acme builds business software for growing teams and "
                    "distributed organizations around the world."
                )
            raise WebDriverException("net::ERR_CONNECTION_RESET")

        driver = FakeNavigationDriver(navigate)
        with patch.object(company_enrichment, "WebDriverWait", ImmediateWait):
            result = company_enrichment.enrich_company(
                driver, "https://example.test"
            )
        self.assertIn(classified(result), {"SUCCESS", "PARTIAL"})
        self.assertEqual(result["website_title"], "Acme Software")
        self.assertTrue(result["_meta"]["had_navigation_failure"])

    def test_runner_persists_structured_inspection_failure_as_failed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "leads_ready.csv"
            output_path = root / "leads_enriched.csv"
            with input_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = DictWriter(
                    handle, fieldnames=("name", "email", "phone", "website")
                )
                writer.writeheader()
                writer.writerow({
                    "name": "Unavailable", "email": "info@example.test",
                    "phone": "1", "website": "https://example.test",
                })

            summary = enrich_leads_batch(
                input_path=input_path,
                output_path=output_path,
                driver_factory=lambda: FakeNavigationDriver(
                    lambda url, call: (EMPTY_HTML, "Hi")
                ),
                enrichment_function=lambda *args, **kwargs: {
                    "_meta": {
                        "pages_attempted": 5,
                        "pages_loaded": 0,
                        "had_navigation_failure": True,
                        "failure_type": "DNS",
                    }
                },
            )
            with output_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(DictReader(handle))

        self.assertEqual(summary["Failed"], 1)
        self.assertEqual(rows[0]["enrichment_status"], "FAILED")
        self.assertNotIn("_meta", rows[0])

    def test_existing_success_partial_and_no_website_rules_are_unchanged(self):
        success = {
            "website": "https://example.test",
            "description": "Supported context",
            "industry": "Software",
            "website_title": "Acme",
            "enrichment_attempts": "1",
        }
        partial = {
            "website": "https://example.test",
            "linkedin_url": "https://linkedin.com/company/acme",
            "enrichment_attempts": "1",
        }
        self.assertEqual(enrichment_status(success), "SUCCESS")
        self.assertEqual(enrichment_status(partial), "PARTIAL")
        self.assertEqual(enrichment_status({"website": "", "enrichment_attempts": "1"}), "NO_WEBSITE")

    def test_old_csv_resume_and_retry_behavior_remain_compatible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "leads_ready.csv"
            output_path = root / "leads_enriched.csv"
            with input_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = DictWriter(handle, fieldnames=("name", "email", "phone", "website"))
                writer.writeheader()
                writer.writerow({
                    "name": "Old Failure", "email": "info@example.test",
                    "phone": "1", "website": "https://example.test",
                })
            with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = DictWriter(handle, fieldnames=(
                    "company_name", "email", "website", "enrichment_status",
                    "enrichment_attempts",
                ))
                writer.writeheader()
                writer.writerow({
                    "company_name": "Old Failure", "email": "info@example.test",
                    "website": "https://example.test", "enrichment_status": "FAILED",
                    "enrichment_attempts": "1",
                })

            result = {
                "website_title": "Acme",
                "description": "Supported software context",
                "industry": "Software",
            }
            summary = enrich_leads_batch(
                input_path=input_path,
                output_path=output_path,
                driver_factory=lambda: FakeNavigationDriver(lambda url, call: (EMPTY_HTML, "Hi")),
                enrichment_function=lambda *args, **kwargs: result,
            )
            with output_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(DictReader(handle))

        self.assertEqual(summary["Attempted this run"], 1)
        self.assertEqual(rows[0]["enrichment_status"], "SUCCESS")
        self.assertEqual(rows[0]["enrichment_attempts"], "2")
        self.assertNotIn("_meta", rows[0])
