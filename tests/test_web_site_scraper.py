"""Offline behavioral tests for the production PatternScrapper."""

from unittest import TestCase
from unittest.mock import patch

from utils.web_site_scraper import PatternScrapper


class ImmediateWait:
    def __init__(self, driver, timeout, poll_frequency=None):
        self.driver = driver

    def until(self, predicate):
        for _ in range(3):
            result = predicate(self.driver)
            if result:
                return result
        return result


class SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, kind):
        handle = f"tab-{len(self.driver.window_handles)}"
        self.driver.window_handles.append(handle)
        self.driver.current_window_handle = handle

    def window(self, handle):
        self.driver.current_window_handle = handle


class FakeNavigationDriver:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.visited = []
        self.current_window_handle = "main"
        self.window_handles = ["main"]
        self.switch_to = SwitchTo(self)
        self._page_source = ""

    @property
    def page_source(self):
        return self._page_source

    def set_page_load_timeout(self, timeout):
        self.timeout = timeout

    def get(self, url):
        self.visited.append(url)
        outcome = self.outcomes.get(url, "<html><body>empty</body></html>")
        if isinstance(outcome, Exception):
            raise outcome
        self._page_source = outcome

    def execute_script(self, script):
        return "complete"

    def find_elements(self, by, value):
        return [object()]

    def close(self):
        self.window_handles.remove(self.current_window_handle)


class PatternScrapperTests(TestCase):
    def test_direct_scraper_returns_known_valid_email(self):
        scraper = PatternScrapper(verbose=False)
        result = scraper.get_pattern_data([
            '<html><a href="mailto:info@lexa.tn">Email us</a></html>'
        ])
        self.assertEqual(result["site_email"], ["info@lexa.tn"])

    @patch("utils.web_site_scraper.WebDriverWait", ImmediateWait)
    def test_dns_failure_skips_remaining_candidate_paths(self):
        urls = ["http://dead.test/", "http://dead.test/contact", "http://dead.test/about"]
        driver = FakeNavigationDriver({
            urls[0]: RuntimeError("unknown error: net::ERR_NAME_NOT_RESOLVED")
        })
        scraper = PatternScrapper(verbose=False)
        self.assertEqual(scraper.get_source_code(driver, urls), [])
        self.assertEqual(driver.visited, urls[:1])
        self.assertEqual(scraper.last_failure_kind, "dns")

    @patch("utils.web_site_scraper.WebDriverWait", ImmediateWait)
    def test_connection_refused_skips_remaining_candidate_paths(self):
        urls = ["http://dead.test/", "http://dead.test/contact"]
        driver = FakeNavigationDriver({
            urls[0]: RuntimeError("unknown error: net::ERR_CONNECTION_REFUSED")
        })
        scraper = PatternScrapper(verbose=False)
        scraper.get_source_code(driver, urls)
        self.assertEqual(driver.visited, urls[:1])
        self.assertEqual(scraper.last_failure_kind, "connection")

    @patch("utils.web_site_scraper.WebDriverWait", ImmediateWait)
    def test_404_page_does_not_abort_other_candidates(self):
        urls = ["https://live.test/", "https://live.test/contact", "https://live.test/about"]
        driver = FakeNavigationDriver({
            urls[0]: "<html><body>Home</body></html>",
            urls[1]: "<html><body>404 Not Found</body></html>",
            urls[2]: "<html><body>info@live.test</body></html>",
        })
        scraper = PatternScrapper(verbose=False)
        sources = scraper.get_source_code(driver, urls)
        self.assertEqual(driver.visited, urls)
        self.assertEqual(scraper.get_pattern_data(sources)["site_email"], ["info@live.test"])

    @patch("utils.web_site_scraper.WebDriverWait", ImmediateWait)
    def test_overall_company_deadline_bounds_candidate_loop(self):
        urls = [f"https://slow.test/{index}" for index in range(6)]
        driver = FakeNavigationDriver({})
        clock = [0.0]

        def advancing_clock():
            clock[0] += 0.2
            return clock[0]

        scraper = PatternScrapper(wait_time=10, overall_timeout=0.5, verbose=False)
        with patch("utils.web_site_scraper.monotonic", side_effect=advancing_clock):
            scraper.get_source_code(driver, urls)
        self.assertLessEqual(len(driver.visited), 1)
        self.assertEqual(scraper.last_failure_kind, "timeout")
