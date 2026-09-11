"""Offline tests for Google Search discovery and lead integration."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch

from utils.build_leads import MASTER_FIELDS, READY_FIELDS, build_lead_files, website_domain
from utils.google_search_discovery import (
    DISCOVERY_FIELDS,
    atomic_write_discoveries,
    is_suitable_company_url,
    is_suitable_company_result,
    merge_discoveries,
    read_discoveries,
    extract_organic_results as extract_company_results,
)
from utils.google_search_client import (
    GoogleUrlResolution,
    extract_organic_results as extract_raw_results,
    plausible_goto_job_result,
    provider_hint_from_query,
    resolve_google_goto_in_browser,
    resolve_google_result_url,
)


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def maps_row(name="Maps Company", website="https://maps.test", email="info@maps.test"):
    return {
        "title": name,
        "webpage": website,
        "phone_number": "+216 70 000 000",
        "site_email": email,
    }


def search_row(name="Search Company", website="https://search.test/about", query="software"):
    return {
        "company_name": name,
        "website": website,
        "source": "google_search",
        "source_query": query,
        "source_url": website,
    }


class NormalizationAndFilteringTests(TestCase):
    def test_domain_normalization(self):
        self.assertEqual(website_domain("HTTPS://Example.COM./path?q=1#x"), "example.com")

    def test_www_and_path_normalization(self):
        values = (
            "https://example.com",
            "http://example.com/",
            "https://www.example.com/about",
            "https://example.com/services",
        )
        self.assertEqual({website_domain(value) for value in values}, {"example.com"})

    def test_blocked_domain(self):
        self.assertFalse(is_suitable_company_url("https://jobs.linkedin.com/view/1"))
        self.assertFalse(is_suitable_company_url("https://www.google.tn/search?q=x"))

    def test_pdf_filtering(self):
        self.assertFalse(is_suitable_company_url("https://company.test/brochure.PDF?download=1"))

    def test_ranked_startup_article_is_discovery_noise(self):
        self.assertFalse(is_suitable_company_result(
            "163 Top startups in Tunisia for August 2026",
            "https://directory.test/blog/top-startups-tunisia",
        ))


class SharedGoogleResultTests(TestCase):
    def driver_for(self, title, url):
        anchor = Mock()
        anchor.get_attribute.return_value = url
        heading = Mock()
        heading.text = title
        heading.find_element.return_value = anchor
        container = Mock()
        container.find_element.return_value = heading
        driver = Mock()
        driver.find_elements.return_value = [container]
        return driver

    def test_shared_boundary_unwraps_before_deduplication(self):
        raw_url = (
            "https://www.google.com/url?sa=t&"
            "url=https%3A%2F%2Facme.test%2Fabout"
        )
        rows = extract_raw_results(self.driver_for("Acme", raw_url), 3)
        self.assertEqual(rows, [{
            "title": "Acme",
            "url": "https://acme.test/about",
            "raw_url": raw_url,
        }])

    def test_company_search_accepts_unwrapped_official_site(self):
        raw_url = (
            "https://www.google.com/url?"
            "q=https%3A%2F%2Facme.test%2Fabout"
        )
        rows = extract_company_results(self.driver_for("Acme", raw_url), 3)
        self.assertEqual(rows, [{
            "company_name": "Acme",
            "source_url": "https://acme.test/about",
        }])

    def test_anchor_diagnostics_keep_property_attribute_and_bounded_html(self):
        raw_url = (
            "https://www.google.com/url?"
            "url=https%3A%2F%2Facme.test%2Fabout"
        )
        anchor = Mock()
        anchor.get_attribute.side_effect = lambda name: (
            raw_url if name == "href" else '<a href="/url?url=target">Acme</a>'
        )
        anchor.get_dom_attribute.return_value = "/url?url=target"
        heading = Mock(text="Acme")
        heading.find_element.return_value = anchor
        container = Mock()
        container.find_element.return_value = heading
        driver = Mock()
        driver.find_elements.return_value = [container]

        row = extract_raw_results(
            driver, 1, include_diagnostics=True
        )[0]

        self.assertEqual(row["href_property"], raw_url)
        self.assertEqual(row["href_attribute"], "/url?url=target")
        self.assertIn("<a href=", row["outer_html"])
        self.assertLessEqual(len(row["outer_html"]), 500)

    def response(self, status, location="", content_type=""):
        response = Mock()
        response.status = status
        response.headers = {
            "Location": location,
            "Content-Type": content_type,
        }
        return response

    def test_goto_uses_one_head_hop_and_returns_location(self):
        raw_url = "https://www.google.com/goto?url=CAESopaque"
        target = "https://jobs.lever.co/acme/posting-id"
        open_request = Mock(return_value=self.response(302, target))

        result = resolve_google_result_url(
            raw_url, timeout=2, cache={}, open_request=open_request
        )

        self.assertEqual(result.url, target)
        self.assertEqual(result.failure_reason, "")
        request, timeout = open_request.call_args.args
        self.assertEqual(request.get_method(), "HEAD")
        self.assertGreater(timeout, 1.9)
        self.assertLessEqual(timeout, 2)

    def test_goto_falls_back_to_get_when_head_is_unsupported(self):
        target = "https://jobs.ashbyhq.com/acme/posting-id"
        open_request = Mock(side_effect=(
            self.response(405),
            self.response(302, target),
        ))

        result = resolve_google_result_url(
            "/goto?url=CAESopaque", open_request=open_request
        )

        self.assertEqual(result.url, target)
        methods = [call.args[0].get_method() for call in open_request.call_args_list]
        self.assertEqual(methods, ["HEAD", "GET"])

    def test_goto_resolution_is_cached_within_a_run(self):
        raw_url = "https://www.google.com/goto?url=CAESopaque"
        target = "https://boards.greenhouse.io/acme/jobs/123"
        cache = {}
        open_request = Mock(return_value=self.response(302, target))

        first = resolve_google_result_url(raw_url, cache=cache, open_request=open_request)
        second = resolve_google_result_url(raw_url, cache=cache, open_request=open_request)

        self.assertEqual(first, second)
        open_request.assert_called_once()

    def test_goto_failure_reasons_are_specific(self):
        raw_url = "https://www.google.com/goto?url=CAESopaque"
        cases = (
            (self.response(204), "GOOGLE_GOTO_NO_LOCATION"),
            (self.response(302, "javascript:alert(1)"), "GOOGLE_GOTO_INVALID_LOCATION"),
            (self.response(429), "GOOGLE_GOTO_BLOCKED"),
            (self.response(200, content_type="text/html"), "GOOGLE_GOTO_BLOCKED"),
        )
        for response, reason in cases:
            with self.subTest(reason=reason):
                result = resolve_google_result_url(
                    raw_url, cache={},
                    open_request=Mock(return_value=response),
                )
                self.assertEqual(result.url, raw_url)
                self.assertEqual(result.failure_reason, reason)

    def test_goto_timeout_and_request_failure_are_distinct(self):
        raw_url = "https://www.google.com/goto?url=CAESopaque"
        cases = (
            (TimeoutError(), "GOOGLE_GOTO_TIMEOUT"),
            (OSError("network unavailable"), "GOOGLE_GOTO_RESOLVE_FAILED"),
        )
        for error, reason in cases:
            with self.subTest(reason=reason):
                result = resolve_google_result_url(
                    raw_url, cache={},
                    open_request=Mock(side_effect=error),
                )
                self.assertEqual(result.failure_reason, reason)

    def test_direct_and_legacy_links_do_not_make_requests(self):
        open_request = Mock()
        direct = "https://jobs.lever.co/acme/posting-id"
        legacy = (
            "https://www.google.com/url?"
            "url=https%3A%2F%2Fjobs.lever.co%2Facme%2Fposting-id"
        )

        self.assertEqual(
            resolve_google_result_url(direct, open_request=open_request).url,
            direct,
        )
        self.assertEqual(
            resolve_google_result_url(legacy, open_request=open_request).url,
            direct,
        )
        open_request.assert_not_called()


class FakeTabSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise RuntimeError("unknown window")
        self.driver.current_window_handle = handle
        if handle == "search":
            self.driver.current_url = "https://www.google.com/search?q=jobs"
        else:
            self.driver.current_url = (
                self.driver.destination or "https://www.google.com/goto?url=opaque"
            )


class FakeTabDriver:
    def __init__(self, destination=None, navigation_error=None, opens_tab=True):
        self.window_handles = ["search"]
        self.current_window_handle = "search"
        self.current_url = "https://www.google.com/search?q=jobs"
        self.title = "Google"
        self.page_source = "results"
        self.destination = destination
        self.navigation_error = navigation_error
        self.opens_tab = opens_tab
        self.created = 0
        self.switch_to = FakeTabSwitch(self)

    def native_click(self):
        if self.navigation_error:
            raise self.navigation_error
        if self.opens_tab:
            self.created += 1
            self.window_handles.append(f"temporary-{self.created}")

    def close(self):
        self.window_handles.remove(self.current_window_handle)


class BrowserGotoResolutionTests(TestCase):
    raw_url = "https://www.google.com/goto?url=CAESopaque"

    def test_temporary_tab_closes_on_success_and_search_is_restored(self):
        target = "https://jobs.lever.co/acme/posting"
        driver = FakeTabDriver(target)

        result = resolve_google_goto_in_browser(
            driver, self.raw_url, anchor=Mock(), timeout=.1,
            click_action=driver.native_click,
        )

        self.assertEqual(result.url, target)
        self.assertEqual(result.browser_resolution, target)
        self.assertEqual(driver.window_handles, ["search"])
        self.assertEqual(driver.current_window_handle, "search")

    def test_greenhouse_lever_and_ashby_targets_resolve(self):
        targets = (
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://jobs.lever.co/acme/posting",
            "https://jobs.ashbyhq.com/acme/posting",
        )
        for target in targets:
            with self.subTest(target=target):
                driver = FakeTabDriver(target)
                result = resolve_google_goto_in_browser(
                    driver, self.raw_url, anchor=Mock(), timeout=.1,
                    click_action=driver.native_click,
                )
                self.assertEqual(result.url, target)
                self.assertEqual(driver.window_handles, ["search"])

    def test_default_action_uses_native_modifier_click(self):
        driver = FakeTabDriver("https://jobs.lever.co/acme/posting")
        anchor = Mock()
        chain = Mock()
        chain.key_down.return_value = chain
        chain.click.return_value = chain
        chain.key_up.return_value = chain
        chain.perform.side_effect = driver.native_click

        with patch("selenium.webdriver.ActionChains", return_value=chain):
            result = resolve_google_goto_in_browser(
                driver, self.raw_url, anchor=anchor, timeout=.1
            )

        self.assertEqual(result.url, driver.destination)
        chain.click.assert_called_once_with(anchor)
        chain.perform.assert_called_once()

    def test_temporary_tab_closes_on_timeout(self):
        driver = FakeTabDriver()

        result = resolve_google_goto_in_browser(
            driver, self.raw_url, anchor=Mock(), timeout=.1,
            click_action=driver.native_click,
        )

        self.assertEqual(result.failure_reason, "GOOGLE_GOTO_CLICK_TIMEOUT")
        self.assertEqual(driver.window_handles, ["search"])
        self.assertEqual(driver.current_window_handle, "search")

    def test_temporary_tab_closes_on_webdriver_exception(self):
        driver = FakeTabDriver(navigation_error=RuntimeError("driver failed"))

        result = resolve_google_goto_in_browser(
            driver, self.raw_url, anchor=Mock(), timeout=.1,
            click_action=driver.native_click,
        )

        self.assertEqual(result.failure_reason, "GOOGLE_GOTO_CLICK_FAILED")
        self.assertEqual(driver.window_handles, ["search"])
        self.assertEqual(driver.current_window_handle, "search")

    def test_stale_result_has_precise_failure(self):
        from selenium.common.exceptions import StaleElementReferenceException

        driver = FakeTabDriver(
            navigation_error=StaleElementReferenceException("stale")
        )
        result = resolve_google_goto_in_browser(
            driver, self.raw_url, anchor=Mock(), timeout=.1,
            click_action=driver.native_click,
        )

        self.assertEqual(result.failure_reason, "GOOGLE_GOTO_STALE_RESULT")
        self.assertEqual(driver.window_handles, ["search"])

    def test_verification_is_reported_without_bypass_and_tab_is_closed(self):
        driver = FakeTabDriver("https://consent.google.com/m")

        result = resolve_google_goto_in_browser(
            driver, self.raw_url, anchor=Mock(), timeout=.1,
            click_action=driver.native_click,
        )

        self.assertEqual(
            result.failure_reason, "GOOGLE_GOTO_VERIFICATION_REQUIRED"
        )
        self.assertEqual(driver.window_handles, ["search"])
        self.assertEqual(driver.current_window_handle, "search")

    def test_cache_prevents_repeat_navigation_and_handles_do_not_grow(self):
        driver = FakeTabDriver("https://jobs.ashbyhq.com/acme/posting")
        cache = {}

        for _ in range(20):
            resolve_google_goto_in_browser(
                driver, self.raw_url, anchor=Mock(), timeout=.1, cache=cache,
                click_action=driver.native_click,
            )

        self.assertEqual(driver.created, 1)
        self.assertEqual(driver.window_handles, ["search"])

    def test_handles_do_not_grow_across_many_distinct_resolutions(self):
        driver = FakeTabDriver("https://jobs.lever.co/acme/posting")

        for index in range(20):
            resolve_google_goto_in_browser(
                driver,
                f"https://www.google.com/goto?url=CAESopaque{index}",
                anchor=Mock(), timeout=.1,
                cache={},
                click_action=driver.native_click,
            )

        self.assertEqual(driver.created, 20)
        self.assertEqual(driver.window_handles, ["search"])
        self.assertEqual(driver.current_window_handle, "search")

    def test_provider_query_hints_and_cheap_gate(self):
        self.assertEqual(
            provider_hint_from_query("site:job-boards.greenhouse.io engineer"),
            "greenhouse",
        )
        self.assertTrue(plausible_goto_job_result(
            "Backend Engineer", "site:jobs.lever.co remote"
        ))
        self.assertTrue(plausible_goto_job_result(
            "Backend Engineer", displayed_url_text="jobs.ashbyhq.com › acme"
        ))
        self.assertFalse(plausible_goto_job_result(
            "Acme homepage", displayed_url_text="acme.example"
        ))

    def test_extractor_uses_browser_only_after_http_failure_and_prefilter(self):
        driver = SharedGoogleResultTests().driver_for(
            "Backend Engineer", self.raw_url
        )
        target = "https://jobs.lever.co/acme/posting"
        browser_result = GoogleUrlResolution(
            target,
            resolution_method="click",
            http_resolution="GOOGLE_GOTO_BLOCKED",
            browser_resolution=target,
        )
        with patch(
            "utils.google_search_client.resolve_google_goto_in_browser",
            return_value=browser_result,
        ) as browser_resolver:
            rows = extract_raw_results(
                driver,
                1,
                source_query="site:jobs.lever.co engineer",
                browser_resolve_goto=True,
                open_request=Mock(return_value=SharedGoogleResultTests().response(429)),
            )

        browser_resolver.assert_called_once()
        self.assertEqual(rows[0]["url"], target)
        self.assertEqual(rows[0]["http_resolution"], "GOOGLE_GOTO_BLOCKED")
        self.assertEqual(rows[0]["browser_resolution"], target)

    def test_extractor_does_not_open_implausible_goto_result(self):
        driver = SharedGoogleResultTests().driver_for(
            "Acme homepage", self.raw_url
        )
        with patch(
            "utils.google_search_client.resolve_google_goto_in_browser"
        ) as browser_resolver:
            rows = extract_raw_results(
                driver,
                1,
                source_query="company information",
                browser_resolve_goto=True,
                open_request=Mock(return_value=SharedGoogleResultTests().response(429)),
            )

        browser_resolver.assert_not_called()
        self.assertEqual(rows[0]["resolution_error"], "GOOGLE_GOTO_BLOCKED")

    def test_displayed_domain_does_not_become_a_target(self):
        driver = SharedGoogleResultTests().driver_for(
            "Acme careers", self.raw_url
        )
        with patch(
            "utils.google_search_client._visible_result_text",
            side_effect=("boards.greenhouse.io › Acme", "Open positions"),
        ):
            rows = extract_raw_results(
                driver,
                1,
                open_request=Mock(return_value=SharedGoogleResultTests().response(429)),
            )

        self.assertEqual(rows[0]["displayed_domain"], "boards.greenhouse.io")
        self.assertEqual(rows[0]["url"], self.raw_url)
        self.assertEqual(rows[0]["resolution_error"], "GOOGLE_GOTO_BLOCKED")


class DiscoveryMergeTests(TestCase):
    def test_duplicate_same_domain(self):
        rows = merge_discoveries([], [
            search_row(website="https://www.acme.test/about"),
            search_row(website="http://acme.test/services"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["website"], "https://acme.test/")

    def test_duplicate_across_queries_preserves_queries(self):
        rows = merge_discoveries([], [
            search_row(website="https://acme.test/a", query="software Tunisia"),
            search_row(website="https://acme.test/b", query="SaaS Tunisia"),
        ])
        self.assertEqual(rows[0]["source_query"], "software Tunisia;SaaS Tunisia")

    def test_empty_results(self):
        self.assertEqual(merge_discoveries([], []), [])

    def test_repeated_output_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "discoveries.csv"
            row = search_row(website="https://acme.test/about")
            first = merge_discoveries(read_discoveries(path), [row])
            atomic_write_discoveries(path, first)
            second = merge_discoveries(read_discoveries(path), [row])
            atomic_write_discoveries(path, second)
            self.assertEqual(len(read_discoveries(path)), 1)
            self.assertEqual(tuple(read_csv(path)[0]), DISCOVERY_FIELDS)


class BuildLeadIntegrationTests(TestCase):
    def build(self, maps_rows, search_rows=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        maps_path = root / "google_maps_data.csv"
        write_csv(
            maps_path,
            ("title", "webpage", "phone_number", "site_email"),
            maps_rows,
        )
        if search_rows is not None:
            write_csv(root / "google_search_companies.csv", DISCOVERY_FIELDS, search_rows)
        build_lead_files(maps_path, root)
        return read_csv(root / "leads_master.csv"), read_csv(root / "leads_ready.csv")

    def test_maps_and_search_same_domain_merge_with_maps_values(self):
        master, ready = self.build(
            [maps_row("Lexa Technologies", "https://www.lexa.tn/", "info@lexa.tn")],
            [search_row("Lexa ERP", "https://lexa.tn/solutions", "ERP Tunisia")],
        )
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["name"], "Lexa Technologies")
        self.assertEqual(master[0]["email"], "info@lexa.tn")
        self.assertEqual(master[0]["phone"], "+216 70 000 000")
        self.assertEqual(len(ready), 1)

    def test_search_only_is_preserved_but_not_ready(self):
        master, ready = self.build([], [search_row()])
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["name"], "Search Company")
        self.assertEqual(master[0]["source"], "google_search")
        self.assertEqual(ready, [])

    def test_maps_only_behavior_and_ready_contract(self):
        master, ready = self.build([maps_row()], None)
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["source"], "google_maps")
        self.assertEqual(tuple(master[0]), MASTER_FIELDS)
        self.assertEqual(tuple(ready[0]), READY_FIELDS)
        self.assertEqual(ready[0]["email"], "info@maps.test")

    def test_source_provenance_merge(self):
        master, _ = self.build(
            [maps_row("Acme Maps", "https://acme.test")],
            [search_row("Acme Search", "https://www.acme.test/about", "query one;query two")],
        )
        self.assertEqual(master[0]["source"], "google_maps;google_search")
        self.assertEqual(master[0]["source_queries"], "query one;query two")


if __name__ == "__main__":
    main()
