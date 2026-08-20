from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from re import compile
from time import monotonic


class PatternScrapper:

    def __init__(self, wait_time: int = 15, verbose: bool = True):
        self._last_opened_handler = None
        self._wait_time = wait_time
        self._verbose = verbose
        self._email_pattern = compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
        self._fb_pattern = compile(r'(?:https?://)?(?:www\.)?facebook\.com/\S+')
        self._twitter_pattern = compile(r'(?:https?://)?(?:www\.)?twitter\.com/\S+')
        self._insta_pattern = compile(r'(?:https?://)?(?:www\.)?instagram\.com/\S+')
        self._youtube_pattern = compile(r'(?:https?://)?(?:www\.)?youtube\.com/\S+')
        self._linkedin_pattern = compile(r'(?:https?://)?(?:www\.)?linkedin\.com/\S+')
        # Resolve the parser once; fall back to the stdlib parser when lxml is
        # absent so website parsing never hard-crashes mid-run.
        self._html_parser = self._resolve_parser()

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

    def get_source_code(self, driver: WebDriver, urls: list):
        source_codes = []
        original_handle = driver.current_window_handle

        for url in urls:
            temporary_handle = None
            try:
                driver.switch_to.new_window("tab")
                temporary_handle = driver.current_window_handle
                candidate_timeout = max(1, self._wait_time)
                deadline = monotonic() + candidate_timeout

                def remaining_timeout():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutException("candidate URL timeout exceeded")
                    return remaining

                driver.set_page_load_timeout(candidate_timeout)
                driver.get(url)

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
                    current_size = len(current_driver.page_source)
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

                source_codes.append(driver.page_source)
            except TimeoutException as e:
                if self._verbose:
                    print(f"[-] Website URL timed out: {url} ({type(e).__name__}: {e})")
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass
            except Exception as e:
                if self._verbose:
                    print(f"[-] Website URL failed: {url} ({type(e).__name__}: {e})")
            finally:
                try:
                    if temporary_handle and temporary_handle in driver.window_handles:
                        driver.switch_to.window(temporary_handle)
                        driver.close()
                except Exception as e:
                    if self._verbose:
                        print(f"[-] Website tab cleanup failed: {url} ({type(e).__name__}: {e})")
                finally:
                    driver.switch_to.window(original_handle)
        return source_codes

    @staticmethod
    def email_decoder(email):
        decoded_mail = ""
        k = int(email[:2], 16)

        for i in range(2, len(email) - 1, 2):
            decoded_mail += chr(int(email[i:i + 2], 16) ^ k)

        return decoded_mail

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
                    email_list.extend(self._email_pattern.findall(mailto_value))
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

    def get_pattern_data(self, source_codes: list):
        patterns_data = {"site_email": [], "facebook_links": [], "twitter_links": [], "instagram_links": [],
                         "youtube_links": [], "linkedin_links": []}

        for source in source_codes:
            soup = BeautifulSoup(source, self._html_parser)

            site_email = self._email_pattern.findall(str(soup))
            site_email.extend(self._href_emails(soup))

            facebook_links = [link['href'] for link in soup.find_all('a', href=self._fb_pattern)]
            twitter_links = [link['href'] for link in soup.find_all('a', href=self._twitter_pattern)]
            instagram_links = [link['href'] for link in soup.find_all('a', href=self._insta_pattern)]
            youtube_links = [link['href'] for link in soup.find_all('a', href=self._youtube_pattern)]
            linkedin_links = [link['href'] for link in soup.find_all('a', href=self._linkedin_pattern)]

            patterns_data["site_email"].extend(site_email)
            patterns_data["facebook_links"].extend(facebook_links)
            patterns_data["twitter_links"].extend(twitter_links)
            patterns_data["instagram_links"].extend(instagram_links)
            patterns_data["youtube_links"].extend(youtube_links)
            patterns_data["linkedin_links"].extend(linkedin_links)
        unique_emails = []
        seen_emails = set()
        for email in patterns_data["site_email"]:
            if not self._is_valid_email(email):
                continue
            email_key = email.lower()
            if email_key not in seen_emails:
                seen_emails.add(email_key)
                unique_emails.append(email)
        patterns_data["site_email"] = unique_emails
        return patterns_data

    def find_patterns(self, driver: WebDriver, site_url: str, suggested_ext: list, unavailable: str = "Not Available"):
        patterns_data = {"site_email": "", "facebook_links": "", "twitter_links": "", "instagram_links": "",
                         "youtube_links": "", "linkedin_links": ""}

        if site_url == unavailable or suggested_ext == []:
            for key in patterns_data.keys():
                patterns_data[key] = unavailable
            return patterns_data

        valid_urls = self.create_urls(site_url, suggested_ext)

        self._last_opened_handler = driver.current_window_handle
        try:
            sources = self.get_source_code(driver, valid_urls)
        except Exception as e:
            if self._verbose:
                print(f"[-] Website scraping failed: {site_url} ({type(e).__name__}: {e})")
            for key in patterns_data.keys():
                patterns_data[key] = unavailable
            return patterns_data

        social_data = self.get_pattern_data(sources)

        for key in social_data.keys():
            if not social_data[key]:
                patterns_data[key] = unavailable
            else:
                patterns_data[key] = social_data[key][0]

        return patterns_data


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
