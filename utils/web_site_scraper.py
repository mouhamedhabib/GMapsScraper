"""
Public Website Contact Extractor
================================

Purpose:
    Visit a company's main/contact/about pages and extract public email and
    social-profile links from their rendered HTML.

Pipeline:
    google_maps_scraper.py --------> web_site_scraper.py -> Maps record fields
    enrich_search_emails.py -------> web_site_scraper.py -> enriched emails

Input:
    A Selenium driver, company website URL, and suggested page paths.

Output:
    A dictionary of validated email addresses and social links. Its caller
    either stores these with Maps data or feeds them back through build_leads.py.
"""

from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from re import compile
from time import monotonic
from typing import Callable, Optional


class PatternScrapper:
    """Extract contact patterns from a bounded set of rendered website pages."""

    _EMAIL_LOCAL_CHARACTERS = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
    )
    _EMAIL_DOMAIN_CHARACTERS = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
    )
    _EMAIL_TLD_CHARACTERS = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    _MAX_EMAIL_LOCAL_CHARACTERS = 64
    _MAX_EMAIL_DOMAIN_CHARACTERS = 253

    def __init__(
        self,
        wait_time: int = 15,
        verbose: bool = True,
        overall_timeout: Optional[float] = None,
        timing_callback: Callable[..., None] | None = None,
    ):
        self._last_opened_handler = None
        self._wait_time = wait_time
        self._verbose = verbose
        self._overall_timeout = overall_timeout
        self._timing_callback = timing_callback
        self.last_attempted_urls = []
        self.last_failure_kind = None
        self.last_failure_message = ""
        self.temporary_tabs_opened = 0
        self.temporary_tabs_closed = 0
        self._email_pattern = compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
        self._fb_pattern = compile(r'(?:https?://)?(?:www\.)?facebook\.com/\S+')
        self._twitter_pattern = compile(r'(?:https?://)?(?:www\.)?twitter\.com/\S+')
        self._insta_pattern = compile(r'(?:https?://)?(?:www\.)?instagram\.com/\S+')
        self._youtube_pattern = compile(r'(?:https?://)?(?:www\.)?youtube\.com/\S+')
        self._linkedin_pattern = compile(r'(?:https?://)?(?:www\.)?linkedin\.com/\S+')
        # Resolve the parser once; fall back to the stdlib parser when lxml is
        # absent so website parsing never hard-crashes mid-run.
        self._html_parser = self._resolve_parser()
        self._timing = {}
        self._reset_timing()

    def _reset_timing(self) -> None:
        self._timing = {
            "navigation_seconds": 0.0,
            "dom_retrieval_seconds": 0.0,
            "parsing_seconds": 0.0,
            "serialization_seconds": 0.0,
            "regex_seconds": 0.0,
            "dom_retrieval_count": 0,
            "source_page_count": 0,
            "dom_characters": 0,
            "max_dom_characters": 0,
            "serialized_characters": 0,
        }

    def _page_source(self, driver: WebDriver) -> str:
        started = monotonic()
        source = driver.page_source
        self._timing["dom_retrieval_seconds"] += monotonic() - started
        self._timing["dom_retrieval_count"] += 1
        self._timing["dom_characters"] += len(source)
        self._timing["max_dom_characters"] = max(
            self._timing["max_dom_characters"], len(source)
        )
        return source

    def _emit_timing(self, total_seconds: float, outcome: str) -> None:
        if self._timing_callback is None:
            return
        details = {
            key: round(value, 6) if key.endswith("_seconds") else value
            for key, value in self._timing.items()
        }
        try:
            self._timing_callback(
                "website_enrichment_timing",
                outcome=outcome,
                parser=self._html_parser,
                total_seconds=round(total_seconds, 6),
                candidate_url_count=len(self.last_attempted_urls),
                failure_kind=self.last_failure_kind,
                **details,
            )
        except BaseException:
            # Timing evidence must never affect scraping or persistence.
            pass

    @staticmethod
    def _resolve_parser() -> str:
        """
        Return the best available BeautifulSoup parser: 'lxml' when installed
        (fast and lenient), otherwise the standard-library 'html.parser'.
        """
        for parser in ("lxml", "html.parser"):
            try:
                BeautifulSoup("", parser)
                return parser
            except Exception:
                continue
        return "html.parser"

    @staticmethod
    def create_urls(site_url: str, url_ext: list):
        """Return unique homepage and suggested subpage URLs for one website."""
        site_url = site_url.strip()
        if not urlsplit(site_url).scheme:
            site_url = "http://" + site_url

        site_parser = urlsplit(site_url)
        # Fragments are client-side locations and should not create duplicate
        # requests. Keep the original path and query otherwise.
        original_url = urlunsplit((site_parser.scheme.lower(), site_parser.netloc.lower(),
                                   site_parser.path or "/", site_parser.query, ""))
        base_url = urlunsplit((site_parser.scheme.lower(), site_parser.netloc.lower(), "/", "", ""))

        contact_aliases = ("contact", "contacts", "contact-us")
        about_aliases = ("about", "about-us")
        paths = []
        for extension in url_ext:
            normalized_extension = str(extension).strip().strip("/")
            if not normalized_extension:
                continue
            alias_key = normalized_extension.lower()
            if alias_key in contact_aliases:
                paths.extend(contact_aliases)
            elif alias_key in about_aliases:
                paths.extend(about_aliases)
            else:
                paths.append(normalized_extension)

        created_urls = []
        seen = set()
        for url in [original_url] + [urljoin(base_url, path) for path in paths]:
            normalized_url = urlunsplit(urlsplit(url)._replace(fragment=""))
            dedupe_key = normalized_url.rstrip("/") or normalized_url
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                created_urls.append(normalized_url)
        return created_urls

    @staticmethod
    def _navigation_failure(error, url=""):
        """Return a concise failure category and message for navigation errors."""
        message = " ".join(str(error).split())
        normalized = message.upper()
        hostname = urlsplit(url).hostname or url or "website"
        if "ERR_NAME_NOT_RESOLVED" in normalized:
            return "dns", f"{hostname} could not be resolved"
        if "ERR_CONNECTION_REFUSED" in normalized:
            return "connection", f"{hostname} refused the connection"
        if "ERR_ADDRESS_UNREACHABLE" in normalized:
            return "connection", f"{hostname} is unreachable"
        if "ERR_INTERNET_DISCONNECTED" in normalized:
            return "connection", "internet connection is unavailable"
        if isinstance(error, TimeoutException) or "TIMED OUT" in normalized:
            return "timeout", f"{hostname} exceeded the navigation timeout"
        return "other", message

    def get_source_code(self, driver: WebDriver, urls: list):
        """Load candidate URLs in temporary tabs and return successful HTML sources."""
        source_codes = []
        original_handle = driver.current_window_handle
        try:
            original_handles = set(driver.window_handles)
        except Exception:
            original_handles = {original_handle}
        self.last_attempted_urls = []
        self.last_failure_kind = None
        self.last_failure_message = ""
        company_deadline = (
            monotonic() + self._overall_timeout
            if self._overall_timeout is not None else None
        )

        for url in urls:
            if company_deadline is not None and monotonic() >= company_deadline:
                self.last_failure_kind = "timeout"
                self.last_failure_message = "overall company deadline exceeded"
                break
            self.last_attempted_urls.append(url)
            try:
                driver.switch_to.new_window("tab")
                self.temporary_tabs_opened += 1
                candidate_timeout = max(1, self._wait_time)
                deadline = monotonic() + candidate_timeout
                if company_deadline is not None:
                    deadline = min(deadline, company_deadline)

                def remaining_timeout():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutException("candidate URL timeout exceeded")
                    return remaining

                driver.set_page_load_timeout(remaining_timeout())
                navigation_started = monotonic()
                try:
                    driver.get(url)
                finally:
                    self._timing["navigation_seconds"] += (
                        monotonic() - navigation_started
                    )

                wait = WebDriverWait(driver, remaining_timeout())
                wait.until(lambda current_driver: current_driver.execute_script(
                    "return document.readyState") == "complete")
                wait = WebDriverWait(driver, remaining_timeout())
                wait.until(lambda current_driver: current_driver.find_elements(By.TAG_NAME, "body"))

                # readyState can become complete before a SPA finishes rendering.
                # Wait briefly for the DOM size to remain unchanged across two polls.
                previous_size = [None]
                stable_polls = [0]

                def dom_is_stable(current_driver):
                    current_size = len(self._page_source(current_driver))
                    if current_size == previous_size[0]:
                        stable_polls[0] += 1
                    else:
                        previous_size[0] = current_size
                        stable_polls[0] = 0
                    return stable_polls[0] >= 2

                try:
                    WebDriverWait(driver, min(remaining_timeout(), 3),
                                  poll_frequency=0.25).until(dom_is_stable)
                except TimeoutException:
                    # A continuously changing page is still safe to inspect at the
                    # end of this bounded rendering wait.
                    pass

                source_codes.append(self._page_source(driver))
            except TimeoutException as e:
                self.last_failure_kind, self.last_failure_message = self._navigation_failure(e, url)
                if self._verbose:
                    print(f"[-] Website URL timed out: {url} ({type(e).__name__}: {e})")
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass
            except Exception as e:
                self.last_failure_kind, self.last_failure_message = self._navigation_failure(e, url)
                if self._verbose:
                    print(f"[-] Website URL failed: {url} ({type(e).__name__}: {e})")
                if self.last_failure_kind in {"dns", "connection"}:
                    # These failures apply to the hostname, not merely this path.
                    break
            finally:
                # Close every handle created during this candidate, including
                # unexpected popups. Each operation is isolated so one broken
                # handle cannot prevent cleanup of the others or focus restore.
                try:
                    current_handles = list(driver.window_handles)
                except Exception:
                    current_handles = []
                for handle in current_handles:
                    if handle in original_handles:
                        continue
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                        self.temporary_tabs_closed += 1
                    except Exception as e:
                        if self._verbose:
                            print(
                                f"[-] Website tab cleanup failed: {url} "
                                f"({type(e).__name__}: {e})"
                            )
                try:
                    if original_handle in driver.window_handles:
                        driver.switch_to.window(original_handle)
                except Exception as e:
                    if self._verbose:
                        print(
                            f"[-] Website focus restore failed: {url} "
                            f"({type(e).__name__}: {e})"
                        )
        return source_codes

    @staticmethod
    def email_decoder(email):
        """Decode a Cloudflare-protected hexadecimal email string."""
        decoded_mail = ""
        k = int(email[:2], 16)

        for i in range(2, len(email) - 1, 2):
            decoded_mail += chr(int(email[i:i + 2], 16) ^ k)

        return decoded_mail

    def _scan_emails(self, text: str) -> list[str]:
        """Extract bounded email candidates without rescanning every suffix."""
        emails = []
        search_from = 0
        text_length = len(text)

        while True:
            at_index = text.find("@", search_from)
            if at_index < 0:
                break
            search_from = at_index + 1

            left_limit = max(0, at_index - self._MAX_EMAIL_LOCAL_CHARACTERS)
            left = at_index
            while (
                left > left_limit
                and text[left - 1] in self._EMAIL_LOCAL_CHARACTERS
            ):
                left -= 1
            if left == at_index:
                continue
            if (
                left == left_limit
                and left > 0
                and text[left - 1] in self._EMAIL_LOCAL_CHARACTERS
            ):
                continue

            right_limit = min(
                text_length,
                at_index + 1 + self._MAX_EMAIL_DOMAIN_CHARACTERS,
            )
            right = at_index + 1
            while (
                right < right_limit
                and text[right] in self._EMAIL_DOMAIN_CHARACTERS
            ):
                right += 1
            if (
                right == right_limit
                and right < text_length
                and text[right] in self._EMAIL_DOMAIN_CHARACTERS
            ):
                continue

            # An unanchored regex used to stop before sentence punctuation or
            # other non-letter domain suffixes. Preserve that behavior while
            # keeping the final regex check bounded to an RFC-sized candidate.
            while (
                right > at_index + 1
                and text[right - 1] not in self._EMAIL_TLD_CHARACTERS
            ):
                right -= 1
            candidate = text[left:right]
            if self._email_pattern.fullmatch(candidate):
                emails.append(candidate)

        return emails

    def _href_emails(self, soup: BeautifulSoup) -> list:
        email_list = []
        mail_tos = soup.select('a[href]')
        for mail in mail_tos:
            href = mail['href'].strip()
            href_lower = href.lower()
            try:
                if "email-protect" in href_lower and "#" in href:
                    email_list.append(self.email_decoder(href.split("#", 1)[1]))
                elif href_lower.startswith("mailto:"):
                    mailto_value = unquote(href[len("mailto:"):]).split("?", 1)[0]
                    email_list.extend(self._scan_emails(mailto_value))
            except (ValueError, IndexError):
                continue

        return email_list

    def _is_valid_email(self, email: str) -> bool:
        email = email.strip()
        if not self._email_pattern.fullmatch(email):
            return False

        local_part, domain = email.rsplit("@", 1)
        placeholder_domains = {
            "example.com", "example.org", "example.net", "company.com",
            "domain.com", "yourcompany.com", "email.com"
        }
        placeholder_local_parts = {
            "test", "user", "name", "email", "foulen", "firstname",
            "lastname", "firstname.lastname", "yourname"
        }
        asset_extensions = {"jpg", "jpeg", "png", "gif", "svg", "webp", "css", "js", "pdf"}

        domain_lower = domain.lower()
        if domain_lower in placeholder_domains:
            return False
        if local_part.lower() in placeholder_local_parts:
            return False
        if domain_lower.rsplit(".", 1)[-1] in asset_extensions:
            return False
        return True

    def _collect_pattern_data(self, source_codes: list, validate_emails: bool):
        patterns_data = {"site_email": [], "facebook_links": [], "twitter_links": [], "instagram_links": [],
                         "youtube_links": [], "linkedin_links": []}

        for source in source_codes:
            parsing_started = monotonic()
            soup = BeautifulSoup(source, self._html_parser)
            self._timing["parsing_seconds"] += monotonic() - parsing_started
            self._timing["source_page_count"] += 1

            serialization_started = monotonic()
            serialized = str(soup)
            self._timing["serialization_seconds"] += (
                monotonic() - serialization_started
            )
            self._timing["serialized_characters"] += len(serialized)

            regex_started = monotonic()
            site_email = self._scan_emails(serialized)
            site_email.extend(self._href_emails(soup))

            facebook_links = [link['href'] for link in soup.find_all('a', href=self._fb_pattern)]
            twitter_links = [link['href'] for link in soup.find_all('a', href=self._twitter_pattern)]
            instagram_links = [link['href'] for link in soup.find_all('a', href=self._insta_pattern)]
            youtube_links = [link['href'] for link in soup.find_all('a', href=self._youtube_pattern)]
            linkedin_links = [link['href'] for link in soup.find_all('a', href=self._linkedin_pattern)]
            self._timing["regex_seconds"] += monotonic() - regex_started

            patterns_data["site_email"].extend(site_email)
            patterns_data["facebook_links"].extend(facebook_links)
            patterns_data["twitter_links"].extend(twitter_links)
            patterns_data["instagram_links"].extend(instagram_links)
            patterns_data["youtube_links"].extend(youtube_links)
            patterns_data["linkedin_links"].extend(linkedin_links)
        unique_emails = []
        seen_emails = set()
        for email in patterns_data["site_email"]:
            if validate_emails and not self._is_valid_email(email):
                continue
            email_key = email.lower()
            if email_key not in seen_emails:
                seen_emails.add(email_key)
                unique_emails.append(email)
        patterns_data["site_email"] = unique_emails
        return patterns_data

    def get_raw_pattern_data(self, source_codes: list):
        """Return extracted patterns before email validation, for diagnostics."""
        return self._collect_pattern_data(source_codes, validate_emails=False)

    def get_pattern_data(self, source_codes: list):
        """Return deduplicated, validated emails and social links from HTML pages."""
        return self._collect_pattern_data(source_codes, validate_emails=True)

    def find_patterns(self, driver: WebDriver, site_url: str, suggested_ext: list, unavailable: str = "Not Available"):
        """Collect contact fields for a site, using the unavailable marker on failure."""
        patterns_data = {"site_email": "", "facebook_links": "", "twitter_links": "", "instagram_links": "",
                         "youtube_links": "", "linkedin_links": ""}

        if not str(site_url or "").strip() or site_url == unavailable:
            for key in patterns_data.keys():
                patterns_data[key] = unavailable
            return patterns_data

        self._reset_timing()
        timing_started = monotonic()
        timing_outcome = "failed"
        valid_urls = self.create_urls(site_url, suggested_ext)

        self._last_opened_handler = driver.current_window_handle
        try:
            sources = self.get_source_code(driver, valid_urls)
        except Exception as e:
            if self._verbose:
                print(f"[-] Website scraping failed: {site_url} ({type(e).__name__}: {e})")
            for key in patterns_data.keys():
                patterns_data[key] = unavailable
            timing_outcome = "navigation_failed"
            return patterns_data
        finally:
            if "sources" not in locals():
                self._emit_timing(monotonic() - timing_started, timing_outcome)

        try:
            try:
                social_data = self.get_pattern_data(sources)
            finally:
                # HTML strings can be several megabytes. Release them as soon as
                # this company's parse finishes rather than retaining the list.
                sources.clear()

            for key in social_data.keys():
                if not social_data[key]:
                    patterns_data[key] = unavailable
                else:
                    patterns_data[key] = social_data[key][0]
            timing_outcome = "completed"
            return patterns_data
        finally:
            self._emit_timing(monotonic() - timing_started, timing_outcome)


"""Testing samples"""
# if __name__ == '__main__':
#     from selenium.webdriver import Chrome
#     from selenium.webdriver.chrome.service import Service
#     from webdriver_manager.chrome import ChromeDriverManager
#
#     App = PatternScrapper()
#     d = Chrome(service=Service(ChromeDriverManager().install()))
#     print(App.find_patterns(d, site_url="https://husknashville.com/about/", suggested_ext=["about"]))
# print(App.find_patterns(d, site_url="http://gofitnessng.com/contact-us/", suggested_ext=["contact-us", "contact"]))
# print(App.find_patterns(d, site_url="https://octavebytes.com/contact-us/", suggested_ext=["contact-us", "contact"]))
