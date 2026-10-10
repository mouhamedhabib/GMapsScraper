"""Offline regressions for deterministic Google Maps search readiness."""

from threading import Event
from unittest import TestCase
from unittest.mock import Mock

from job_search.network import NetworkProtectionRelay
from utils.google_maps_scraper import GoogleMaps, MapsQueryState, MapsReadiness
from utils.known_companies import KnownCompanies


SEARCH = "https://www.google.com/maps/search/software+company+Malta"
PLACE = "https://www.google.com/maps/place/Example/@35.9,14.5/data=!4m1!1sChIJExample"


class Element:
    def __init__(self, *, text="", href=""):
        self.text = text
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else ""


class ReadinessDriver:
    def __init__(self, *, url=SEARCH, text="", feed=False, anchors=None):
        self.current_url = url
        self.page_source = text
        self.current_window_handle = "main"
        self.text = text
        self.feed = feed
        self.anchors = list(anchors or [])
        self.refresh_calls = 0

    def find_elements(self, by, selector):
        if selector == "body":
            return [Element(text=self.text)] if self.text else []
        if selector == "div[role='feed']":
            return [Element()] if self.feed else []
        if "a[href" in selector:
            return self.anchors
        return []

    def refresh(self):
        self.refresh_calls += 1


class LifecycleScraper(GoogleMaps):
    def __init__(self, readiness, **kwargs):
        super().__init__(**kwargs)
        self.readiness = list(readiness)
        self.load_calls = []
        self.search_calls = []
        self.driver = ReadinessDriver()

    def create_chrome_driver(self):
        return self.driver

    def load_url(self, driver, url):
        self.load_calls.append(url)

    def search_query(self, query):
        self.search_calls.append(query)

    def classify_maps_readiness(self, driver):
        if len(self.readiness) > 1:
            return self.readiness.pop(0)
        return self.readiness[0]

    def scroll_to_the_end_event(self, driver):
        return [PLACE]

    def _scrape_result_and_store(self, driver, result, query, results_indices):
        return "new"

    def close_extra_tabs(self, driver):
        pass


class MapsReadinessClassificationTests(TestCase):
    def test_search_page_without_feed_is_not_ready(self):
        scraper = GoogleMaps(verbose=False)
        self.assertEqual(
            scraper.classify_maps_readiness(ReadinessDriver()),
            MapsReadiness.SEARCH_PAGE_NOT_READY,
        )

    def test_place_and_healthy_feed_are_ready(self):
        scraper = GoogleMaps(verbose=False)
        self.assertEqual(
            scraper.classify_maps_readiness(ReadinessDriver(url=PLACE)),
            MapsReadiness.READY_RESULTS,
        )
        self.assertEqual(
            scraper.classify_maps_readiness(ReadinessDriver(feed=True)),
            MapsReadiness.READY_RESULTS,
        )

    def test_explicit_no_results_is_confirmed(self):
        scraper = GoogleMaps(verbose=False)
        driver = ReadinessDriver(text="No results found for this search")
        self.assertEqual(
            scraper.classify_maps_readiness(driver),
            MapsReadiness.NO_RESULTS_CONFIRMED,
        )

    def test_consent_and_verification_are_not_no_results(self):
        scraper = GoogleMaps(verbose=False)
        self.assertEqual(
            scraper.classify_maps_readiness(
                ReadinessDriver(url="https://consent.google.com/m")
            ),
            MapsReadiness.CONSENT_REQUIRED,
        )
        self.assertEqual(
            scraper.classify_maps_readiness(
                ReadinessDriver(text="Our systems have detected unusual traffic")
            ),
            MapsReadiness.VERIFICATION_REQUIRED,
        )


class MapsReadinessLifecycleTests(TestCase):
    def make_scraper(self, readiness, **kwargs):
        return LifecycleScraper(
            readiness,
            incremental=True,
            result_range=1,
            known_companies=KnownCompanies(),
            summary={"queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0},
            stop_event=Event(),
            verbose=False,
            readiness_delays=(),
            network_relay=NetworkProtectionRelay(enabled=False),
            **kwargs,
        )

    def test_readiness_succeeds_on_retry(self):
        delays = []
        scraper = LifecycleScraper(
            [MapsReadiness.SEARCH_PAGE_NOT_READY, MapsReadiness.READY_RESULTS],
            verbose=False, readiness_delays=(5,), sleep_function=delays.append,
        )
        self.assertEqual(
            scraper.wait_for_maps_readiness(scraper.driver, "query"),
            MapsReadiness.READY_RESULTS,
        )
        self.assertEqual(delays, [5])
        self.assertEqual(scraper.resource_metrics()["maps_readiness_retries"], 1)

    def test_verification_prompt_resumes_same_query_without_renavigation(self):
        answers = iter([""])
        scraper = self.make_scraper(
            [MapsReadiness.VERIFICATION_REQUIRED, MapsReadiness.READY_RESULTS],
            input_function=lambda prompt: next(answers),
        )
        self.assertEqual(scraper.start_scrapper("software company Malta"), "COMPLETED")
        self.assertEqual(scraper.search_calls, ["software company Malta"])
        self.assertEqual(len(scraper.load_calls), 1)
        self.assertEqual(scraper.resource_metrics()["maps_verification_prompts"], 1)
        self.assertEqual(scraper.query_states(), [
            {"query": "software company Malta", "state": "COMPLETED"}
        ])

    def test_verification_abort_is_blocked_not_completed(self):
        scraper = self.make_scraper(
            [MapsReadiness.VERIFICATION_REQUIRED], input_function=lambda prompt: "q",
        )
        self.assertEqual(
            scraper.start_scrapper("software company Malta"),
            MapsQueryState.VERIFICATION_ABORTED.value,
        )
        metrics = scraper.resource_metrics()
        self.assertEqual(metrics["maps_queries_completed"], 0)
        self.assertEqual(metrics["maps_queries_blocked"], 1)
        self.assertTrue(scraper._stop_event.is_set())

    def test_consent_resumes_and_never_becomes_zero_results(self):
        scraper = self.make_scraper(
            [MapsReadiness.CONSENT_REQUIRED, MapsReadiness.READY_RESULTS],
            input_function=lambda prompt: "",
        )
        self.assertEqual(scraper.start_scrapper("query"), "COMPLETED")
        self.assertEqual(scraper.resource_metrics()["maps_no_results_confirmed"], 0)

    def test_consent_abort_has_distinct_observability_state(self):
        scraper = self.make_scraper(
            [MapsReadiness.CONSENT_REQUIRED], input_function=lambda prompt: "q",
        )
        self.assertEqual(
            scraper.start_scrapper("query"), MapsQueryState.CONSENT_ABORTED.value,
        )
        metrics = scraper.resource_metrics()
        self.assertEqual(metrics["maps_queries_completed"], 0)
        self.assertEqual(metrics["maps_queries_blocked"], 1)
        self.assertTrue(scraper._stop_event.is_set())

    def test_explicit_no_results_finishes_without_collection(self):
        scraper = self.make_scraper([MapsReadiness.NO_RESULTS_CONFIRMED])
        scraper.scroll_to_the_end_event = Mock(side_effect=AssertionError("collection ran"))
        self.assertEqual(scraper.start_scrapper("query"), "NO_RESULTS")
        metrics = scraper.resource_metrics()
        self.assertEqual(metrics["maps_no_results_confirmed"], 1)
        self.assertEqual(metrics["maps_queries_completed"], 1)

    def test_timeout_after_bounded_retries_is_blocked(self):
        delays = []
        scraper = LifecycleScraper(
            [MapsReadiness.SEARCH_PAGE_NOT_READY],
            incremental=True, known_companies=KnownCompanies(), verbose=False,
            readiness_delays=(5, 10, 15), sleep_function=delays.append,
            network_relay=NetworkProtectionRelay(enabled=False),
        )
        self.assertEqual(scraper.start_scrapper("query"), "SEARCH_TIMEOUT")
        self.assertEqual(delays, [5, 10, 15])
        metrics = scraper.resource_metrics()
        self.assertEqual(metrics["maps_readiness_retries"], 3)
        self.assertEqual(metrics["maps_search_not_ready"], 4)
        self.assertEqual(metrics["maps_queries_blocked"], 1)

    def test_network_failure_is_routed_through_relay_and_same_query_resumes(self):
        relay = Mock()
        relay.protect.side_effect = lambda operation, **kwargs: operation()
        scraper = GoogleMaps(verbose=False, network_relay=relay)
        scraper.classify_maps_readiness = Mock(side_effect=[
            MapsReadiness.NETWORK_DEGRADED,
            MapsReadiness.READY_RESULTS,
            MapsReadiness.READY_RESULTS,
        ])
        driver = ReadinessDriver()
        self.assertEqual(
            scraper.wait_for_maps_readiness(driver, "query"),
            MapsReadiness.READY_RESULTS,
        )
        relay.protect.assert_called_once()
        self.assertEqual(driver.refresh_calls, 1)
