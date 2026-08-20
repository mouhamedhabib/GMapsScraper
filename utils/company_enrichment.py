"""Conservative, source-backed company context extraction from public websites."""

from re import IGNORECASE, compile
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


EMPTY_ENRICHMENT = {
    "website_title": "",
    "website_meta_description": "",
    "hero_text": "",
    "about_text": "",
    "services": "",
    "linkedin_url": "",
}

PAGE_PATHS = (
    ("about", "/about"),
    ("about", "/about-us"),
    ("about", "/en/about"),
    ("about", "/fr/a-propos"),
    ("services", "/services"),
    ("services", "/solutions"),
    ("services", "/products"),
)

ABOUT_MARKER = compile(
    r"(?:^|[-_\s])(about|company|story|who[-_\s]?we[-_\s]?are|a[-_\s]?propos|presentation)(?:$|[-_\s])",
    flags=IGNORECASE,
)
SERVICE_MARKER = compile(
    r"(?:^|[-_\s])(services?|solutions?|products?|offerings?|expertise|what[-_\s]?we[-_\s]?do)(?:$|[-_\s])",
    flags=IGNORECASE,
)
BOILERPLATE_MARKER = compile(
    r"cookie|consent|privacy|legal|newsletter|popup|modal|gdpr|terms[-_\s]?of[-_\s]?use",
    flags=IGNORECASE,
)
BOILERPLATE_PHRASES = (
    "accept all cookies", "cookie policy", "privacy policy", "terms and conditions",
    "all rights reserved", "subscribe to our newsletter", "manage consent",
)
GENERIC_SERVICE_ITEMS = {
    "home", "contact", "contact us", "about", "about us", "careers", "career",
    "jobs", "blog", "services", "our services", "solutions", "our solutions",
    "products", "our products", "what we do", "learn more", "read more",
}


def normalize_whitespace(value):
    return " ".join((value or "").split()).strip()


def trim_text(value, maximum):
    value = normalize_whitespace(value)
    if len(value) <= maximum:
        return value
    shortened = value[:maximum + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened or value[:maximum]


def unique_text(values):
    unique = []
    seen = set()
    for value in values:
        cleaned = normalize_whitespace(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        # Nested responsive blocks often repeat the same paragraph. Treat a
        # meaningful block already contained in a larger block as duplicate.
        if len(key) >= 30 and any(key in existing for existing in seen):
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


def is_boilerplate(value):
    lowered = normalize_whitespace(value).casefold()
    return not lowered or any(phrase in lowered for phrase in BOILERPLATE_PHRASES)


def _empty_result():
    return dict(EMPTY_ENRICHMENT)


def _candidate_urls(website_url):
    website_url = normalize_whitespace(website_url)
    if not website_url:
        return []
    if not urlsplit(website_url).scheme:
        website_url = "http://" + website_url

    parsed = urlsplit(website_url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return []

    homepage = urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), "/", "", ""))
    candidates = [("homepage", homepage)]
    candidates.extend((kind, urljoin(homepage, path)) for kind, path in PAGE_PATHS)

    deduplicated = []
    seen = set()
    for kind, url in candidates:
        normalized = urlunsplit(urlsplit(url)._replace(fragment=""))
        key = normalized.rstrip("/").casefold()
        if key not in seen:
            seen.add(key)
            deduplicated.append((kind, normalized))
    return deduplicated


def _load_pages(driver, candidates, timeout, verbose):
    pages = []
    original_handle = driver.current_window_handle
    candidate_timeout = max(1, timeout or 15)

    for kind, url in candidates:
        temporary_handle = None
        try:
            driver.switch_to.new_window("tab")
            temporary_handle = driver.current_window_handle
            deadline = monotonic() + candidate_timeout

            def remaining_timeout():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutException("company enrichment URL timeout exceeded")
                return remaining

            driver.set_page_load_timeout(candidate_timeout)
            driver.get(url)
            WebDriverWait(driver, remaining_timeout()).until(
                lambda current_driver: current_driver.execute_script(
                    "return document.readyState") == "complete"
            )
            WebDriverWait(driver, remaining_timeout()).until(
                lambda current_driver: current_driver.find_elements(By.TAG_NAME, "body")
            )

            previous_size = [None]
            stable_polls = [0]

            def dom_is_stable(current_driver):
                current_size = len(current_driver.page_source)
                if current_size == previous_size[0]:
                    stable_polls[0] += 1
                else:
                    previous_size[0] = current_size
                    stable_polls[0] = 0
                return stable_polls[0] >= 2

            try:
                WebDriverWait(driver, min(remaining_timeout(), 3), poll_frequency=0.25).until(dom_is_stable)
            except TimeoutException:
                pass

            pages.append({"kind": kind, "url": driver.current_url, "source": driver.page_source})
        except TimeoutException as error:
            if verbose:
                print(f"[-] Company enrichment URL timed out: {url} "
                      f"({type(error).__name__}: {error})")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except Exception as error:
            if verbose:
                print(f"[-] Company enrichment URL failed: {url} "
                      f"({type(error).__name__}: {error})")
        finally:
            try:
                if temporary_handle and temporary_handle in driver.window_handles:
                    driver.switch_to.window(temporary_handle)
                    driver.close()
            except Exception as error:
                if verbose:
                    print(f"[-] Company enrichment tab cleanup failed: {url} "
                          f"({type(error).__name__}: {error})")
            finally:
                try:
                    driver.switch_to.window(original_handle)
                except Exception as error:
                    if verbose:
                        print(f"[-] Could not restore Google Maps tab after {url}: "
                              f"{type(error).__name__}: {error}")
    return pages


def _soup(source):
    try:
        return BeautifulSoup(source, "lxml")
    except Exception:
        return BeautifulSoup(source, "html.parser")


def _remove_noise(soup):
    for element in soup.find_all(("script", "style", "noscript", "nav", "footer", "form", "button", "svg")):
        element.decompose()
    for element in list(soup.find_all(True)):
        if element.parent is None or element.attrs is None:
            continue
        marker = " ".join((element.get("id", ""), " ".join(element.get("class", []))))
        if marker and BOILERPLATE_MARKER.search(marker):
            element.decompose()
    return soup


def _meaningful_text(element, minimum=3, maximum=1200):
    if element is None:
        return ""
    value = normalize_whitespace(element.get_text(" ", strip=True))
    if len(value) < minimum or is_boilerplate(value):
        return ""
    return trim_text(value, maximum)


def _extract_title(homepage_soup):
    if homepage_soup is None or homepage_soup.title is None:
        return ""
    return trim_text(homepage_soup.title.get_text(" ", strip=True), 300)


def _extract_meta_description(homepage_soup):
    if homepage_soup is None:
        return ""
    fallback = ""
    for meta in homepage_soup.find_all("meta"):
        key = normalize_whitespace(meta.get("name") or meta.get("property")).casefold()
        content = trim_text(meta.get("content", ""), 500)
        if not content:
            continue
        if key == "description":
            return content
        if key == "og:description" and not fallback:
            fallback = content
    return fallback


def _extract_hero(homepage_soup):
    if homepage_soup is None:
        return ""
    soup = _remove_noise(_soup(str(homepage_soup)))
    search_root = soup.find("main") or soup.body or soup
    heading = search_root.find("h1")
    if heading is None:
        return ""

    parts = [_meaningful_text(heading)]
    container = heading.find_parent(("section", "header", "article")) or heading.parent
    if container is not None:
        secondary = container.find("h2")
        paragraph = container.find("p")
        parts.extend((_meaningful_text(secondary), _meaningful_text(paragraph, minimum=20)))
    return trim_text(" ".join(unique_text(parts)), 700)


def _marker_value(element):
    return " ".join((element.get("id", ""), " ".join(element.get("class", []))))


def _block_text(element):
    parts = []
    for child in element.find_all(("h1", "h2", "h3", "p")):
        value = _meaningful_text(child, minimum=20 if child.name == "p" else 3)
        if value:
            parts.append(value)
    return " ".join(unique_text(parts))


def _extract_about(parsed_pages):
    preferred_blocks = []
    about_fallbacks = []
    for page in parsed_pages:
        if page["kind"] not in {"homepage", "about"}:
            continue
        soup = _remove_noise(_soup(page["source"]))
        for element in soup.find_all(("section", "article", "div")):
            if ABOUT_MARKER.search(_marker_value(element)):
                value = _block_text(element)
                if len(value) >= 40:
                    preferred_blocks.append(value)
        if page["kind"] == "about":
            main = soup.find("main") or soup.find("article")
            value = _block_text(main) if main else ""
            if len(value) >= 60:
                about_fallbacks.append(value)

    blocks = unique_text(preferred_blocks or about_fallbacks)
    return trim_text(" ".join(blocks), 1200)


def _service_item(value):
    value = normalize_whitespace(value).strip(" -–—|:;")
    lowered = value.casefold()
    if (not value or lowered in GENERIC_SERVICE_ITEMS or is_boilerplate(value)
            or len(value) > 100 or len(value.split()) > 12):
        return ""
    return value


def _extract_services(parsed_pages):
    items = []
    for page in parsed_pages:
        if page["kind"] not in {"homepage", "services"}:
            continue
        soup = _remove_noise(_soup(page["source"]))
        containers = [
            element for element in soup.find_all(("section", "article", "div"))
            if SERVICE_MARKER.search(_marker_value(element))
        ]
        if page["kind"] == "services" and not containers:
            main = soup.find("main") or soup.find("article")
            containers = [main] if main else []
        for container in containers:
            for element in container.find_all(("h2", "h3", "h4", "li")):
                value = _service_item(element.get_text(" ", strip=True))
                if value:
                    items.append(value)

    selected = []
    for item in unique_text(items):
        candidate = "; ".join(selected + [item])
        if len(selected) >= 10 or len(candidate) > 500:
            break
        selected.append(item)
    return "; ".join(selected)


def _normalize_linkedin_url(href):
    href = normalize_whitespace(href)
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    elif not urlsplit(href).scheme:
        href = "https://" + href.lstrip("/")
    parsed = urlsplit(href)
    domain = (parsed.hostname or "").casefold()
    if domain != "linkedin.com" and not domain.endswith(".linkedin.com"):
        return ""
    path = parsed.path.rstrip("/")
    if not path or path.casefold().startswith(("/share", "/feed")):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def _extract_linkedin(parsed_pages):
    company_urls = []
    other_urls = []
    for page in parsed_pages:
        soup = _soup(page["source"])
        for anchor in soup.select("a[href]"):
            url = _normalize_linkedin_url(anchor.get("href", ""))
            if not url:
                continue
            if urlsplit(url).path.casefold().startswith("/company/"):
                company_urls.append(url)
            else:
                other_urls.append(url)
    urls = unique_text(company_urls) or unique_text(other_urls)
    return urls[0] if urls else ""


def enrich_company(driver, website_url, timeout=15, verbose=False) -> dict:
    """Return conservative company context extracted directly from website HTML."""
    candidates = _candidate_urls(website_url)
    if not candidates:
        return _empty_result()

    pages = _load_pages(driver, candidates, timeout, verbose)
    if not pages:
        return _empty_result()

    homepage_page = next((page for page in pages if page["kind"] == "homepage"), None)
    homepage_soup = _soup(homepage_page["source"]) if homepage_page else None
    result = _empty_result()
    result["website_title"] = _extract_title(homepage_soup)
    result["website_meta_description"] = _extract_meta_description(homepage_soup)
    result["hero_text"] = _extract_hero(homepage_soup)
    result["about_text"] = _extract_about(pages)
    result["services"] = _extract_services(pages)
    result["linkedin_url"] = _extract_linkedin(pages)
    return result
