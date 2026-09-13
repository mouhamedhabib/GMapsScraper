"""Small, job-agnostic helpers for reading rendered Google Search results."""

from dataclasses import dataclass
from re import IGNORECASE, search
from socket import timeout as SocketTimeout
from time import monotonic
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from job_search.network import NetworkPauseExceeded, classify_network_error


GOOGLE_REDIRECT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/120.0 Safari/537.36"
)


@dataclass(frozen=True)
class GoogleUrlResolution:
    url: str
    failure_reason: str = ""
    resolution_method: str = ""
    http_resolution: str = ""
    browser_resolution: str = ""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_open(request, timeout):
    opener = build_opener(_NoRedirectHandler())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as response:
        # A disabled redirect is exposed as HTTPError while still carrying the
        # status and Location header needed by the resolver.
        return response


def _is_google_domain(hostname):
    domain = (hostname or "").casefold().rstrip(".")
    labels = domain.split(".")
    try:
        google_index = len(labels) - 1 - labels[::-1].index("google")
    except ValueError:
        return False
    suffix = labels[google_index + 1:]
    return (
        len(suffix) == 1
        and 2 <= len(suffix[0]) <= 3
    ) or (
        len(suffix) == 2
        and suffix[0] in {"co", "com"}
        and len(suffix[1]) == 2
    )


def _valid_http_target(value):
    candidate = (value or "").strip()
    parsed = urlsplit(candidate)
    return (
        candidate
        if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname
        else ""
    )


def unwrap_google_result_url(url, max_depth=3):
    """Purely parse a Google ``/url`` link and return its HTTP(S) target.

    Absolute wrappers must use a Google hostname. Relative ``/url`` values are
    accepted because Selenium can expose an unresolved Google anchor. Invalid
    or targetless wrappers are returned unchanged so downstream classifiers can
    reject them explicitly.
    """
    value = (url or "").strip()
    for _ in range(max_depth):
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.path.rstrip("/").casefold() != "/url":
            break
        if hostname and not _is_google_domain(hostname):
            break
        parameters = parse_qs(parsed.query)
        target = next(
            (
                valid
                for key in ("url", "q")
                for candidate in parameters.get(key, [])
                if (valid := _valid_http_target(candidate))
            ),
            "",
        )
        if not target:
            break
        value = target
    return value


def _google_goto_url(url):
    parsed = urlsplit((url or "").strip())
    hostname = parsed.hostname or ""
    return (
        parsed.path.rstrip("/").casefold() == "/goto"
        and (not hostname or _is_google_domain(hostname))
        and bool(parse_qs(parsed.query).get("url"))
    )


def _response_status(response):
    return getattr(response, "status", None) or response.getcode()


def _close_response(response):
    close = getattr(response, "close", None)
    if close:
        close()


def _is_timeout_error(error):
    reason = getattr(error, "reason", None)
    return isinstance(error, (TimeoutError, SocketTimeout)) or isinstance(
        reason, (TimeoutError, SocketTimeout)
    )


def _request_without_redirects(url, method, timeout, open_request):
    request = Request(
        url,
        method=method,
        headers={
            "User-Agent": GOOGLE_REDIRECT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    return open_request(request, timeout)


def resolve_google_result_url(
    url, timeout=3.0, cache=None, open_request=None, raise_network_errors=False,
):
    """Resolve one Google result URL without following redirect chains.

    Direct URLs and legacy ``/url`` wrappers require no network. Opaque
    ``/goto`` tokens receive one HEAD request, with a GET fallback only when
    HEAD is explicitly unsupported.
    """
    raw_url = (url or "").strip()
    locally_unwrapped = unwrap_google_result_url(raw_url)
    if locally_unwrapped != raw_url or not _google_goto_url(raw_url):
        return GoogleUrlResolution(locally_unwrapped)

    cache = cache if cache is not None else {}
    if raw_url in cache:
        return cache[raw_url]

    request_url = raw_url
    if request_url.startswith("/"):
        request_url = "https://www.google.com" + request_url
    open_request = open_request or _default_open
    response = None
    deadline = monotonic() + max(0.1, timeout)
    try:
        response = _request_without_redirects(
            request_url, "HEAD", max(0.1, deadline - monotonic()), open_request
        )
        status = _response_status(response)
        if status in {405, 501}:
            _close_response(response)
            response = _request_without_redirects(
                request_url, "GET", max(0.1, deadline - monotonic()), open_request
            )
            status = _response_status(response)

        location = (response.headers.get("Location") or "").strip()
        content_type = (response.headers.get("Content-Type") or "").casefold()
        if status in {403, 429, 503} or (
            status == 200 and "text/html" in content_type and not location
        ):
            result = GoogleUrlResolution(
                raw_url, "GOOGLE_GOTO_BLOCKED", "http",
                "GOOGLE_GOTO_BLOCKED",
            )
        elif status >= 400:
            result = GoogleUrlResolution(
                raw_url, "GOOGLE_GOTO_RESOLVE_FAILED", "http",
                "GOOGLE_GOTO_RESOLVE_FAILED",
            )
        elif not location:
            result = GoogleUrlResolution(
                raw_url, "GOOGLE_GOTO_NO_LOCATION", "http",
                "GOOGLE_GOTO_NO_LOCATION",
            )
        else:
            target = _valid_http_target(location)
            target_host = urlsplit(target).hostname if target else ""
            if not target:
                result = GoogleUrlResolution(
                    raw_url, "GOOGLE_GOTO_INVALID_LOCATION", "http",
                    "GOOGLE_GOTO_INVALID_LOCATION",
                )
            elif _is_google_domain(target_host):
                target_path = urlsplit(target).path.casefold()
                reason = (
                    "GOOGLE_GOTO_BLOCKED"
                    if "consent" in target_host or target_path.startswith("/sorry")
                    else "GOOGLE_GOTO_RESOLVE_FAILED"
                )
                result = GoogleUrlResolution(
                    raw_url, reason, "http", reason,
                )
            else:
                result = GoogleUrlResolution(
                    target, resolution_method="http", http_resolution=target,
                )
    except (OSError, URLError) as error:
        if raise_network_errors and classify_network_error(error):
            raise
        reason = (
            "GOOGLE_GOTO_TIMEOUT"
            if _is_timeout_error(error)
            else "GOOGLE_GOTO_RESOLVE_FAILED"
        )
        result = GoogleUrlResolution(raw_url, reason, "http", reason)
    except Exception:
        result = GoogleUrlResolution(
            raw_url, "GOOGLE_GOTO_RESOLVE_FAILED", "http",
            "GOOGLE_GOTO_RESOLVE_FAILED",
        )
    finally:
        if response is not None:
            _close_response(response)

    cache[raw_url] = result
    return result


GOOGLE_VERIFICATION_MARKERS = (
    "unusual traffic",
    "verify you are human",
    "verify that you're not a robot",
    "recaptcha",
    "before you continue to google",
)

PROVIDER_QUERY_HOSTS = {
    "lever": ("jobs.lever.co",),
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "ashby": ("jobs.ashbyhq.com",),
    "workable": ("apply.workable.com", "jobs.workable.com"),
}


def provider_hint_from_query(source_query):
    """Return an ATS hint from an explicit ``site:`` query, if present."""
    query = (source_query or "").casefold()
    for provider, hosts in PROVIDER_QUERY_HOSTS.items():
        if any(
            search(r"(?:^|\s)site:\s*" + host.replace(".", r"\."), query, IGNORECASE)
            for host in hosts
        ):
            return provider
    return ""


def plausible_goto_job_result(title, source_query="", displayed_url_text="", snippet=""):
    """Cheap result-type gate used before opening an opaque Google link."""
    provider = provider_hint_from_query(source_query)
    visible = " ".join(
        value for value in (title, displayed_url_text, snippet) if value
    ).casefold()
    if provider:
        return bool((title or "").strip() or visible)
    if any(host in visible for hosts in PROVIDER_QUERY_HOSTS.values() for host in hosts):
        return True
    return any(
        marker in visible
        for marker in (" job ", " jobs ", "career", "apply for", "position")
    )


def _browser_verification_required(driver):
    try:
        current = (driver.current_url or "").casefold()
        parsed = urlsplit(current)
        if "consent" in (parsed.hostname or "") or parsed.path.startswith("/sorry"):
            return True
        page_text = " ".join((driver.title or "", driver.page_source or "")).casefold()
        return any(marker in page_text for marker in GOOGLE_VERIFICATION_MARKERS)
    except Exception:
        return False


def resolve_google_goto_in_browser(
    driver, goto_url, anchor=None, timeout=4.0, cache=None,
    http_resolution="", click_action=None, raise_network_errors=False,
):
    """Resolve one opaque ``/goto`` by modifier-clicking its result anchor."""
    raw_url = (goto_url or "").strip()
    cache = cache if cache is not None else {}
    cached = cache.get(raw_url)
    if cached and (
        cached.url != raw_url
        or cached.failure_reason == "GOOGLE_GOTO_VERIFICATION_REQUIRED"
        or cached.resolution_method == "click"
    ):
        return cached

    original_handle = None
    temporary_handle = None
    before_handles = set()
    result = GoogleUrlResolution(
        raw_url, "GOOGLE_GOTO_CLICK_FAILED", "click",
        http_resolution, "GOOGLE_GOTO_CLICK_FAILED",
    )
    try:
        from selenium.common.exceptions import (
            StaleElementReferenceException,
            TimeoutException,
        )
        from selenium.webdriver import ActionChains, Keys
        from selenium.webdriver.support.ui import WebDriverWait

        if anchor is None:
            raise RuntimeError("result anchor is required for click resolution")
        original_handle = driver.current_window_handle
        before_handles = set(driver.window_handles)
        if click_action is None:
            platform_name = str(
                getattr(driver, "capabilities", {}).get("platformName", "")
            ).casefold()
            modifier = Keys.COMMAND if platform_name in {"mac", "mac os x"} else Keys.CONTROL
            click_action = lambda: (
                ActionChains(driver)
                .key_down(modifier)
                .click(anchor)
                .key_up(modifier)
                .perform()
            )
        click_action()

        deadline = monotonic() + max(0.1, timeout)

        def click_created_tab(current_driver):
            new_handles = set(current_driver.window_handles) - before_handles
            if new_handles:
                return next(iter(new_handles))
            if _browser_verification_required(current_driver):
                return "GOOGLE_GOTO_VERIFICATION_REQUIRED"
            return False

        temporary_handle = WebDriverWait(
            driver, max(0.1, deadline - monotonic())
        ).until(click_created_tab)
        if temporary_handle == "GOOGLE_GOTO_VERIFICATION_REQUIRED":
            result = GoogleUrlResolution(
                raw_url, temporary_handle, "click", http_resolution,
                temporary_handle,
            )
            temporary_handle = None
        else:
            driver.switch_to.window(temporary_handle)

            def redirected(current_driver):
                current_url = _valid_http_target(current_driver.current_url)
                if current_url and not _is_google_domain(urlsplit(current_url).hostname):
                    return current_url
                if _browser_verification_required(current_driver):
                    return "GOOGLE_GOTO_VERIFICATION_REQUIRED"
                return False

            outcome = WebDriverWait(
                driver, max(0.1, deadline - monotonic())
            ).until(redirected)
            if outcome == "GOOGLE_GOTO_VERIFICATION_REQUIRED":
                result = GoogleUrlResolution(
                    raw_url, outcome, "click", http_resolution, outcome,
                )
            else:
                result = GoogleUrlResolution(
                    outcome, resolution_method="click",
                    http_resolution=http_resolution, browser_resolution=outcome,
                )
    except StaleElementReferenceException:
        result = GoogleUrlResolution(
            raw_url, "GOOGLE_GOTO_STALE_RESULT", "click",
            http_resolution, "GOOGLE_GOTO_STALE_RESULT",
        )
    except TimeoutException as error:
        if raise_network_errors and classify_network_error(error):
            raise
        result = GoogleUrlResolution(
            raw_url, "GOOGLE_GOTO_CLICK_TIMEOUT", "click",
            http_resolution, "GOOGLE_GOTO_CLICK_TIMEOUT",
        )
    except Exception as error:
        if raise_network_errors and classify_network_error(error):
            raise
        result = GoogleUrlResolution(
            raw_url, "GOOGLE_GOTO_CLICK_FAILED", "click",
            http_resolution, "GOOGLE_GOTO_CLICK_FAILED",
        )
    finally:
        try:
            current_handles = set(driver.window_handles)
            temporary_handles = (
                current_handles - before_handles if before_handles else set()
            )
            if temporary_handle in current_handles:
                temporary_handles.add(temporary_handle)
            for handle in temporary_handles:
                driver.switch_to.window(handle)
                driver.close()
        except Exception:
            pass
        try:
            if original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass

    cache[raw_url] = result
    return result


def _visible_result_text(container, selectors):
    for selector in selectors:
        try:
            for element in container.find_elements("css selector", selector):
                value = (element.text or "").strip()
                if value:
                    return value
        except (AttributeError, TypeError):
            continue
    return ""


def _displayed_domain(displayed_url_text):
    value = (displayed_url_text or "").strip().split()[0].rstrip(" ›>")
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    return (parsed.hostname or "").casefold().rstrip(".")


def _google_result_date_text(snippet):
    """Keep Google's visible relative date as evidence, never as a job date."""
    match = search(
        r"\b(?:today|yesterday|\d+\s+(?:minutes?|hours?|days?|weeks?|months?|years?)\s+ago)\b",
        snippet or "", IGNORECASE,
    )
    return match.group(0) if match else ""


def _find_result_anchor(driver, result_index, expected_title, expected_raw_url):
    """Re-identify one result exactly after earlier click resolutions."""
    try:
        containers = driver.find_elements("css selector", "div.MjjYud, div.g")
        container = containers[result_index]
        heading = container.find_element("css selector", "h3")
        anchor = heading.find_element("xpath", "ancestor::a[1]")
        title = (heading.text or "").strip()
        raw_url = (anchor.get_attribute("href") or "").strip()
        if title == expected_title and raw_url == expected_raw_url:
            return anchor
    except Exception:
        pass
    return None


def extract_organic_results(
    driver, limit, verbose=False, accept_result: Optional[Callable] = None,
    include_incomplete=False, include_diagnostics=False,
    resolution_timeout=3.0, resolution_cache=None, open_request=None,
    source_query="", browser_resolve_goto=False, network_relay=None,
):
    """Return visible ``title``/``url`` pairs, optionally filtered by a callback."""
    results = []
    seen_urls = set()
    resolution_cache = (
        resolution_cache if resolution_cache is not None else {}
    )
    result_count = len(
        driver.find_elements("css selector", "div.MjjYud, div.g")
    )
    for result_index in range(result_count):
        if len(results) >= limit:
            break
        try:
            current_containers = driver.find_elements(
                "css selector", "div.MjjYud, div.g"
            )
            container = current_containers[result_index]
            heading = container.find_element("css selector", "h3")
            anchor = heading.find_element("xpath", "ancestor::a[1]")
            title = (heading.text or "").strip()
            raw_url = (anchor.get_attribute("href") or "").strip()
            displayed_url_text = _visible_result_text(
                container, ("cite", "div.TbwUpd", "span.VuuXrf")
            )
            snippet = _visible_result_text(
                container, ("div.VwiC3b", "div.IsZvec")
            )
            resolve_http = lambda: resolve_google_result_url(
                raw_url, timeout=resolution_timeout, cache=resolution_cache,
                open_request=open_request,
                raise_network_errors=network_relay is not None,
            )
            resolution = (
                network_relay.protect(resolve_http, context="Google /goto HTTP resolution")
                if network_relay is not None else resolve_http()
            )
            if (
                browser_resolve_goto
                and resolution.failure_reason
                and _google_goto_url(raw_url)
                and plausible_goto_job_result(
                    title, source_query, displayed_url_text, snippet
                )
            ):
                http_outcome = (
                    resolution.http_resolution or resolution.failure_reason
                )
                click_anchor = _find_result_anchor(
                    driver, result_index, title, raw_url
                )
                if click_anchor is None:
                    resolution = GoogleUrlResolution(
                        raw_url, "GOOGLE_GOTO_STALE_RESULT", "click",
                        http_outcome, "GOOGLE_GOTO_STALE_RESULT",
                    )
                    resolution_cache[raw_url] = resolution
                else:
                    resolve_browser = lambda: resolve_google_goto_in_browser(
                        driver, raw_url, anchor=click_anchor,
                        timeout=min(5.0, max(3.0, resolution_timeout)),
                        cache=resolution_cache, http_resolution=http_outcome,
                        raise_network_errors=network_relay is not None,
                    )
                    resolution = (
                        network_relay.protect(
                            resolve_browser, context="Google /goto browser resolution"
                        ) if network_relay is not None else resolve_browser()
                    )
            url = resolution.url
            if (not title or not url) and not include_incomplete:
                continue
            if url and url in seen_urls:
                continue
            if accept_result is not None and not accept_result(title, url):
                continue
            if url:
                seen_urls.add(url)
            row = {
                "title": title,
                "url": url,
                "raw_url": raw_url,
            }
            if browser_resolve_goto or include_diagnostics:
                row["result_index"] = result_index
            if displayed_url_text:
                row["displayed_url_text"] = displayed_url_text
                displayed_domain = _displayed_domain(displayed_url_text)
                if displayed_domain:
                    row["displayed_domain"] = displayed_domain
            if snippet:
                row["result_snippet"] = snippet
                date_text = _google_result_date_text(snippet)
                if date_text:
                    row["google_result_date_text"] = date_text
            if resolution.http_resolution:
                row["http_resolution"] = resolution.http_resolution
            if resolution.browser_resolution:
                row["browser_resolution"] = resolution.browser_resolution
                if resolution.resolution_method == "click":
                    row["click_resolution"] = resolution.browser_resolution
            if resolution.failure_reason:
                row["resolution_error"] = resolution.failure_reason
            if include_diagnostics:
                try:
                    row["href_attribute"] = (
                        anchor.get_dom_attribute("href") or ""
                    ).strip()
                except (AttributeError, TypeError):
                    row["href_attribute"] = ""
                row["href_property"] = raw_url
                row["outer_html"] = " ".join(
                    (anchor.get_attribute("outerHTML") or "").split()
                )[:500]
            results.append(row)
            if resolution.failure_reason == "GOOGLE_GOTO_VERIFICATION_REQUIRED":
                break
        except Exception as error:
            if isinstance(error, NetworkPauseExceeded):
                raise
            if verbose:
                print(f"[-] Skipping unreadable result: {type(error).__name__}: {error}")
    return results
