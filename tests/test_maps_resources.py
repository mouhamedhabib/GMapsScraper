"""Offline tests for Maps browser ownership and resource cleanup."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from maps import GMapsScraper
from utils.google_maps_scraper import GoogleMaps
from utils.threading_controller import FastSearchAlgo
from utils.web_site_scraper import PatternScrapper


class FakeOptions:
    def __init__(self):
        self.arguments = []
        self.experimental = {}

    def add_argument(self, argument):
        self.arguments.append(argument)

    def add_experimental_option(self, name, value):
        self.experimental[name] = value


class ReusableDriver:
    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


class SwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def new_window(self, kind):
        handle = f"tab-{self.driver.next_handle}"
        self.driver.next_handle += 1
        self.driver.window_handles.append(handle)
        self.driver.current_window_handle = handle

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise RuntimeError("missing window")
        self.driver.current_window_handle = handle


class TabDriver:
    def __init__(self, outcome="<html><body>ok</body></html>", popup=False):
        self.outcome = outcome
        self.popup = popup
        self.window_handles = ["maps"]
        self.current_window_handle = "maps"
        self.next_handle = 1
        self.switch_to = SwitchTo(self)
        self._source = ""

    @property
    def page_source(self):
        return self._source

    def set_page_load_timeout(self, timeout):
        pass

    def get(self, url):
        if self.popup:
            self.window_handles.append(f"popup-{self.next_handle}")
            self.next_handle += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        self._source = self.outcome

    def execute_script(self, script):
        return "complete"

    def find_elements(self, by, value):
        return [object()]

    def close(self):
        self.window_handles.remove(self.current_window_handle)


class ImmediateWait:
    def __init__(self, driver, timeout, poll_frequency=None):
        self.driver = driver

    def until(self, predicate):
        result = None
        for _ in range(3):
            result = predicate(self.driver)
            if result:
                return result
        return result


class BrowserLifecycleTests(TestCase):
    def test_one_browser_is_reused_and_full_process_tree_is_quit(self):
        scraper = GoogleMaps(verbose=False)
        driver = ReusableDriver()
        scraper.create_chrome_driver = Mock(return_value=driver)
        self.assertIs(scraper.get_or_create_driver(), driver)
        self.assertIs(scraper.get_or_create_driver(), driver)
        self.assertEqual(scraper.create_chrome_driver.call_count, 1)
        scraper.quit_driver()
        self.assertEqual(driver.quit_calls, 1)

    def test_worker_quits_browser_after_normal_and_exceptional_queries(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                instances = []

                class FakeMaps:
                    def __init__(self, **kwargs):
                        self.quit_calls = 0
                        instances.append(self)

                    def start_scrapper(self, query):
                        if raises:
                            raise RuntimeError("query failed")

                    def quit_driver(self):
                        self.quit_calls += 1

                    def resource_metrics(self):
                        return {
                            "browser_instances_created": 1,
                            "browser_instances_recreated": 0,
                            "temporary_tabs_opened": 0,
                            "temporary_tabs_closed": 0,
                        }

                with patch("utils.threading_controller.GoogleMaps", FakeMaps):
                    coordinator = FastSearchAlgo(workers=1, verbose=False)
                    coordinator.fast_search_algorithm(["one", "two"])
                self.assertEqual(len(instances), 1)
                self.assertEqual(instances[0].quit_calls, 1)


class TemporaryTabTests(TestCase):
    def run_case(self, outcome, popup=False):
        driver = TabDriver(outcome, popup=popup)
        scraper = PatternScrapper(verbose=False)
        with patch("utils.web_site_scraper.WebDriverWait", ImmediateWait):
            scraper.get_source_code(driver, ["https://company.test"])
        self.assertEqual(driver.window_handles, ["maps"])
        self.assertEqual(driver.current_window_handle, "maps")
        self.assertEqual(scraper.temporary_tabs_opened, 1)
        self.assertEqual(scraper.temporary_tabs_closed, 2 if popup else 1)

    def test_temporary_tabs_close_on_success(self):
        self.run_case("<html><body>ok</body></html>")

    def test_temporary_tabs_close_after_timeout(self):
        self.run_case(TimeoutException("timed out"))

    def test_temporary_tabs_close_after_exception(self):
        self.run_case(RuntimeError("parse/navigation failure"))

    def test_unexpected_popup_does_not_grow_window_handles(self):
        self.run_case("<html><body>ok</body></html>", popup=True)


class ResourceConfigurationTests(TestCase):
    def test_low_resource_preferences_and_safe_flags(self):
        options = FakeOptions()
        GoogleMaps.configure_low_resource_options(options)
        self.assertIn("--disable-background-networking", options.arguments)
        self.assertIn("--disable-notifications", options.arguments)
        self.assertNotIn("--disable-javascript", options.arguments)
        self.assertEqual(
            options.experimental["prefs"]["profile.managed_default_content_settings.images"],
            2,
        )

    def test_fonts_and_media_are_blocked_but_css_is_not(self):
        driver = Mock()
        GoogleMaps.apply_low_resource_blocking(driver)
        blocked = driver.execute_cdp_cmd.call_args_list[1].args[1]["urls"]
        self.assertIn("*.woff2", blocked)
        self.assertIn("*.mp4", blocked)
        self.assertNotIn("*.css", blocked)

    def test_scroll_polling_sleeps_instead_of_busy_spinning(self):
        scraper = GoogleMaps(scroll_minutes=1, verbose=False)
        scraper._wait = Mock()
        scraper._wait.until.return_value = True
        driver = Mock()
        driver.find_elements.side_effect = [[], [], [], [], []]
        with patch("utils.google_maps_scraper.sleep") as sleeper:
            self.assertEqual(scraper.scroll_to_the_end_event(driver), [])
        self.assertGreaterEqual(sleeper.call_count, 4)


class CliCompatibilityTests(TestCase):
    def test_legacy_cli_defaults_are_unchanged(self):
        app = GMapsScraper()
        with patch("sys.argv", ["maps.py"]):
            app.arg_parser()
        self.assertEqual(app._args.threads, 1)
        self.assertFalse(app._args.low_resource)

    def test_low_resource_caps_workers_to_one(self):
        captured = {}

        class FakeAlgo:
            @staticmethod
            def load_query_file(file_name):
                return ["one", "two", "three"]

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def fast_search_algorithm(self, queries):
                captured["queries"] = queries

        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("one\ntwo\nthree\n", encoding="utf-8")
            app = GMapsScraper()
            with patch("sys.argv", [
                "maps.py", "--query-file", str(query_file), "-w", "3", "--low-resource",
            ]), patch("maps.FastSearchAlgo", FakeAlgo):
                app.arg_parser()
                app.scrape_maps_data()
        self.assertEqual(captured["workers"], 1)
        self.assertTrue(captured["low_resource"])
