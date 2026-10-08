"""Browser-mocked regressions for Google Maps result-card collection."""

from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from utils.google_maps_scraper import GoogleMaps, MapsReadiness
from utils.known_companies import KnownCompanies
from utils.threading_controller import FastSearchAlgo


PLACE_ONE = "https://www.google.com/maps/place/One/@35.1,14.1,15z/data=!4m1!1sChIJOne"
PLACE_TWO = "https://www.google.com/maps/place/Two/@35.2,14.2,15z/data=!4m1!1sChIJTwo"
PLACE_THREE = "https://www.google.com/maps/place/Three/@35.3,14.3,15z/data=!4m1!1sChIJThree"
PLACE_FOUR = "https://www.google.com/maps/place/Four/@35.4,14.4,15z/data=!4m1!1sChIJFour"
SEARCH = "https://www.google.com/maps/search/software+company+Malta"


class Anchor:
    def __init__(self, href):
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else ""


class CollectionDriver:
    current_url = SEARCH

    def __init__(self, anchors):
        self.anchors = anchors

    def find_elements(self, by, selector):
        if "a[href" in selector:
            return self.anchors
        if selector == "div[role='feed']":
            return [object()]
        if selector == "span.HlvSq":
            marker = Mock()
            marker.text = "You've reached the end of the list."
            return [marker]
        return []

    def execute_script(self, script, *args):
        pass

    def implicitly_wait(self, seconds):
        pass


class OpenDriver:
    def __init__(self):
        self.window_handles = ["main"]
        self.current_window_handle = "main"
        self.current_url = SEARCH
        self.switch_to = Mock()
        self.switch_to.window.side_effect = self._switch

    def _switch(self, handle):
        self.current_window_handle = handle

    def execute_script(self, script, url):
        self.window_handles.append("detail")
        self.current_url = url


class ImmediateWait:
    def until(self, condition):
        return True


class MapsResultCollectionTests(TestCase):
    def test_search_page_rejected_and_place_url_accepted(self):
        self.assertTrue(GoogleMaps.is_search_page_url(SEARCH))
        self.assertFalse(GoogleMaps.is_individual_place_url(SEARCH))
        self.assertTrue(GoogleMaps.is_individual_place_url(PLACE_ONE))

    def test_multiple_semantic_place_anchors_are_collected(self):
        scraper = GoogleMaps(incremental=True, verbose=False)
        scraper._wait = ImmediateWait()
        driver = CollectionDriver([
            Anchor(PLACE_ONE), Anchor(SEARCH), Anchor(PLACE_TWO), Anchor(PLACE_ONE),
        ])

        self.assertEqual(scraper.scroll_to_the_end_event(driver), [PLACE_ONE, PLACE_TWO])
        counters = scraper.resource_metrics()
        self.assertEqual(counters["search_results_found"], 3)
        self.assertEqual(counters["place_urls_extracted"], 3)
        self.assertEqual(counters["search_page_urls_rejected"], 1)
        self.assertEqual(counters["individual_place_urls_accepted"], 2)

    def test_timeout_on_search_page_returns_no_placeholder(self):
        scraper = GoogleMaps(incremental=True, verbose=False)
        scraper._wait = Mock()
        scraper._wait.until.side_effect = TimeoutException()
        driver = Mock(current_url=SEARCH)
        self.assertEqual(scraper.scroll_to_the_end_event(driver), [])
        self.assertEqual(scraper.resource_metrics()["search_page_urls_rejected"], 1)

    def test_rejected_candidate_does_not_consume_limit_and_more_cards_are_inspected(self):
        class LifecycleScraper(GoogleMaps):
            def create_chrome_driver(self):
                return Mock(current_window_handle="main")

            def load_url(self, driver, url):
                pass

            def search_query(self, query):
                pass

            def scroll_to_the_end_event(self, driver):
                return [SEARCH, PLACE_ONE, PLACE_TWO, PLACE_THREE, PLACE_FOUR]

            def wait_for_maps_readiness(self, driver, query):
                return MapsReadiness.READY_RESULTS

            def _scrape_result_and_store(
                self, driver, result, query, results_indices,
            ):
                return "known" if result == PLACE_ONE else "new"

            def close_extra_tabs(self, driver):
                pass

        summary = {"queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0}
        scraper = LifecycleScraper(
            incremental=True, result_range=3, known_companies=KnownCompanies(),
            summary=summary, stop_event=Event(), verbose=False,
        )
        scraper.start_scrapper("software company Malta")
        self.assertEqual(summary["new"], 3)
        self.assertEqual(summary["inspected"], 4)
        self.assertEqual(summary["known"], 1)
        self.assertEqual(scraper.resource_metrics()["search_page_urls_rejected"], 1)

    def test_individual_place_opens_detail_and_reaches_persistence(self):
        persisted = []
        scraper = GoogleMaps(verbose=False, record_sink=persisted.append)
        scraper._wait = ImmediateWait()
        scraper._main_handler = "main"
        driver = OpenDriver()
        scraper.get_title = lambda driver: "Neural AI"
        scraper.get_website_link = lambda driver: "https://neuralai.mt"
        scraper.get_phone_number = lambda driver: "+356 1234 5678"
        scraper.get_cover_image = lambda driver: ""
        scraper.get_rating_in_card = lambda driver: "5"
        scraper.get_privacy_price = lambda driver: ""
        scraper.get_category = lambda driver: "Software company"
        scraper.get_address = lambda driver: "San Gwann, Malta"
        scraper.get_working_hours = lambda driver: ""
        scraper.get_menu_link = lambda driver: ""
        scraper.get_related_images_list = lambda driver: ""
        scraper.get_about_description = lambda driver: {}
        scraper._web_pattern_scraper.find_patterns = lambda *args: {}
        scraper.reset_driver_for_next_run = lambda result, driver: None
        scraper._file_creator.create = Mock()

        outcome = scraper._scrape_result_and_store(
            driver, PLACE_ONE, "software company Malta", [1, 1]
        )

        self.assertEqual(outcome, "new")
        self.assertEqual(len(persisted), 1)
        counters = scraper.resource_metrics()
        self.assertEqual(counters["detail_pages_opened"], 1)
        self.assertEqual(counters["companies_persisted"], 1)
        scraper._file_creator.create.assert_called_once()

    def test_default_maps_website_configuration_persists_mailto_email(self):
        with patch("utils.threading_controller.ThreadPoolExecutor"):
            coordinator = FastSearchAlgo()

        persisted = []
        scraper = GoogleMaps(
            suggested_ext=coordinator._suggested_ext,
            verbose=False,
            record_sink=persisted.append,
        )
        driver = Mock(current_window_handle="main")
        scraper.validate_result_link = lambda result, driver: (
            "35.1", "14.1", PLACE_ONE,
        )
        scraper.get_title = lambda driver: "Lexa"
        scraper.get_website_link = lambda driver: "https://lexa.tn/"
        scraper.get_phone_number = lambda driver: "+216 00 000 000"
        scraper.get_cover_image = lambda driver: ""
        scraper.get_rating_in_card = lambda driver: "5"
        scraper.get_privacy_price = lambda driver: ""
        scraper.get_category = lambda driver: "Software company"
        scraper.get_address = lambda driver: "Tunis, Tunisia"
        scraper.get_working_hours = lambda driver: ""
        scraper.get_menu_link = lambda driver: ""
        scraper.get_related_images_list = lambda driver: ""
        scraper.get_about_description = lambda driver: {}
        scraper.reset_driver_for_next_run = lambda result, driver: None
        scraper._web_pattern_scraper.get_source_code = Mock(return_value=[
            '<a href="mailto:info@lexa.tn">Contact</a>',
        ])
        scraper._file_creator.create = Mock()

        scraper._scrape_result_and_store(
            driver, "continue", "software company Tunis", [1, 1],
        )

        self.assertEqual(persisted[0]["site_email"], "info@lexa.tn")
        attempted_urls = scraper._web_pattern_scraper.get_source_code.call_args.args[1]
        self.assertEqual(attempted_urls[0], "https://lexa.tn/")
        self.assertIn("https://lexa.tn/contact", attempted_urls)
