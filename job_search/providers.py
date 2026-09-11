"""Provider detection, source identity extraction, and shallow page parsing."""

from dataclasses import dataclass
from hashlib import sha256
from html import unescape
import json
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from job_search.normalization import normalize_job_url
from utils.google_search_client import unwrap_google_result_url


PROVIDER_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "jobs.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}
AGGREGATOR_DOMAINS = {
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "jooble.org", "wellfound.com", "careerjet.com",
    "simplyhired.com", "talent.com",
}
JOB_PLATFORM_DOMAINS = AGGREGATOR_DOMAINS | {
    "wizbii.com", "waytolearnx.com", "welovedevs.com", "wearedevelopers.com",
    "free-work.com",
}
ATS_PROVIDERS = {
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "teamtailor",
}
SOURCE_QUALITIES = {"DIRECT_COMPANY", "ATS", "JOB_PLATFORM", "UNKNOWN"}
JOB_PATH_SEGMENTS = {"careers", "career", "jobs", "job", "positions", "open-positions"}
ARTICLE_SEGMENTS = {"blog", "blogs", "news", "article", "articles", "press", "directory"}
GENERIC_LISTING_SEGMENTS = {"category", "categories", "listing", "listings", "search"}
PROVIDER_BOARD_REASON = "PROVIDER_BOARD_NOT_POSTING"
GENERIC_LISTING_REASON = "GENERIC_JOB_LISTING_PAGE"


@dataclass
class ParsedJob:
    canonical_url: str
    provider: str
    source_job_id: str = ""
    title: str = ""
    company_name: str = ""
    company_url: str = ""
    location_text: str = ""
    country: str = ""
    city: str = ""
    remote_policy: str = ""
    employment_type: str = ""
    seniority: str = ""
    description: str = ""
    published_at: str = ""
    apply_url: str = ""
    status: str = "UNKNOWN"
    content_hash: str = ""
    raw_content_hash: str = ""
    fetch_status: str = "NOT_FETCHED"
    fetch_error: str = ""


@dataclass(frozen=True)
class JobResultClassification:
    accepted: bool
    normalized_url: str = ""
    provider: str = "generic"
    rejection_reason: str = ""


def _domain_matches(domain, expected):
    return domain == expected or domain.endswith("." + expected)


def detect_provider(url):
    domain = (urlsplit(url if "://" in url else "//" + url).hostname or "").casefold()
    for hostname, provider in PROVIDER_HOSTS.items():
        if _domain_matches(domain, hostname):
            return provider
    if domain.endswith(".teamtailor.com") and domain != "teamtailor.com":
        return "teamtailor"
    return "generic"


def classify_source_quality(url, provider=""):
    """Classify where a posting is hosted without rejecting the posting."""
    provider = (provider or detect_provider(url)).casefold()
    if provider in ATS_PROVIDERS:
        return "ATS"
    domain = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    if any(_domain_matches(domain, platform) for platform in JOB_PLATFORM_DOMAINS):
        return "JOB_PLATFORM"
    first_label = domain.split(".", 1)[0]
    path_segments = {
        unquote(segment).casefold() for segment in urlsplit(url).path.split("/") if segment
    }
    if domain and (
        first_label in {"career", "careers", "job", "jobs"}
        or bool(path_segments & {"career", "careers", "job", "jobs", "positions", "open-positions"})
    ):
        return "DIRECT_COMPANY"
    return "UNKNOWN"


def _looks_like_unknown_job_host(domain):
    first_label = domain.split(".", 1)[0]
    return first_label in {"apply", "career", "careers", "job", "jobs"}


def generic_listing_reason(title, url, description=""):
    """Return a stable reason for strong generic collection-page signals.

    Provider-specific posting URLs are deliberately exempt: this classifier is
    for generic sites that can host both listings and individual vacancies.
    """
    if detect_provider(url) != "generic":
        return ""
    parsed = urlsplit(url)
    domain = (parsed.hostname or "").casefold().removeprefix("www.")
    path = unquote(parsed.path).casefold().rstrip("/")
    cleaned_title = " ".join((title or "").split())
    cleaned_description = " ".join((description or "").split())

    known_listing_path = (
        (domain == "wearedevelopers.com" and re.search(r"/jobs/ls(?:/|$)", path))
        or (
            domain == "free-work.com"
            and re.fullmatch(r"/(?:[a-z]{2}/)?tech-it/jobs/[^/]+", path)
        )
    )
    title_signal = any((
        re.search(r"\b\d+\s+(?:open|fresh|available)?\s*jobs\b", cleaned_title, re.I),
        re.search(r"\bjobs in\s+[^|]+", cleaned_title, re.I),
        re.search(r"\bmissions? freelance et emplois?\b", cleaned_title, re.I),
    ))
    multiple_jobs = bool(re.search(
        r"\b(?:browse|explore)\s+\d+\s+(?:fresh\s+)?jobs\b|"
        r"\b\d+\s+missions? et offres? d['’]emploi\b",
        cleaned_description, re.I,
    ))
    return GENERIC_LISTING_REASON if known_listing_path or title_signal or multiple_jobs else ""


def classify_job_result(title, url):
    """Classify one raw Google result without fetching its destination."""
    if not (url or "").strip():
        return JobResultClassification(False, rejection_reason="NO_URL")
    unwrapped = unwrap_google_result_url(url)
    try:
        canonical = normalize_job_url(unwrapped)
    except (TypeError, ValueError):
        canonical = ""
    if not canonical:
        return JobResultClassification(False, rejection_reason="MALFORMED_URL")
    parsed = urlsplit(canonical)
    domain = (parsed.hostname or "").casefold()
    labels = domain.split(".")
    if (
        "google" in labels
        or domain == "googleusercontent.com"
        or domain.endswith(".googleusercontent.com")
    ):
        return JobResultClassification(
            False, canonical, rejection_reason="GOOGLE_URL"
        )
    if any(_domain_matches(domain, blocked) for blocked in AGGREGATOR_DOMAINS):
        return JobResultClassification(
            False, canonical, rejection_reason="AGGREGATOR"
        )
    provider = detect_provider(canonical)
    # Supported ATS identity is stronger than generic article/path heuristics.
    if provider != "generic":
        if extract_source_job_id(provider, canonical):
            return JobResultClassification(True, canonical, provider)
        return JobResultClassification(
            False, canonical, provider, PROVIDER_BOARD_REASON
        )

    listing_reason = generic_listing_reason(title, canonical)
    if listing_reason:
        return JobResultClassification(False, canonical, provider, listing_reason)

    ordered_segments = [
        unquote(segment).casefold()
        for segment in parsed.path.split("/")
        if segment
    ]
    segments = set(ordered_segments)
    if segments & GENERIC_LISTING_SEGMENTS:
        return JobResultClassification(False, canonical, provider, GENERIC_LISTING_REASON)
    if segments & ARTICLE_SEGMENTS:
        return JobResultClassification(False, canonical, rejection_reason="ARTICLE")
    marker_indexes = [
        index for index, segment in enumerate(ordered_segments)
        if segment in JOB_PATH_SEGMENTS
    ]
    has_individual_path = any(
        any(
            later not in JOB_PATH_SEGMENTS | GENERIC_LISTING_SEGMENTS
            for later in ordered_segments[index + 1:]
        )
        for index in marker_indexes
    )
    if not has_individual_path:
        reason = "UNKNOWN_PROVIDER" if _looks_like_unknown_job_host(domain) else "NON_JOB_PATH"
        return JobResultClassification(False, canonical, rejection_reason=reason)
    lowered_title = (title or "").casefold()
    if any(word in lowered_title for word in ("top jobs", "best jobs", "career advice", "salary guide")):
        return JobResultClassification(False, canonical, rejection_reason="ARTICLE")
    return JobResultClassification(True, canonical, provider)


def is_job_result(title, url):
    """Backward-compatible boolean wrapper around structured classification."""
    return classify_job_result(title, url).accepted


def extract_source_job_id(provider, url):
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    folded = [part.casefold() for part in parts]
    query = parse_qs(parsed.query)
    if provider == "greenhouse":
        for key in ("gh_jid", "job_id"):
            if query.get(key) and query[key][0].strip():
                return query[key][0].strip()
        if "job_app" in folded and query.get("token") and query["token"][0].strip():
            return query["token"][0].strip()
        if "jobs" in folded:
            index = folded.index("jobs")
            return parts[index + 1] if index + 1 < len(parts) else ""
    if provider == "lever":
        return parts[1] if len(parts) >= 2 and folded[1] not in {"apply", "jobs"} else ""
    if provider == "ashby":
        return parts[1] if len(parts) >= 2 and folded[1] not in {"jobs"} else ""
    if provider == "workable":
        for marker in ("j", "view"):
            if marker in folded:
                index = folded.index(marker)
                return parts[index + 1] if index + 1 < len(parts) else ""
        return ""
    if provider == "smartrecruiters":
        if len(parts) >= 3 and folded[1] in {"job", "jobs"}:
            return parts[2]
        return parts[1] if len(parts) >= 2 else ""
    if provider == "teamtailor":
        if "jobs" in folded:
            index = folded.index("jobs")
            return parts[index + 1] if index + 1 < len(parts) else ""
        return ""
    return ""


def _json_ld_items(value):
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_items(item)
    elif isinstance(value, dict):
        yield value
        if isinstance(value.get("@graph"), list):
            yield from _json_ld_items(value["@graph"])


def _is_job_posting(item):
    item_type = item.get("@type") or ""
    values = item_type if isinstance(item_type, list) else [item_type]
    return any(str(value).rstrip("/").rsplit("/", 1)[-1] == "JobPosting" for value in values)


def _iter_json_ld(soup):
    for element in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(element.string or element.get_text() or "null")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _json_ld_items(value):
            if _is_job_posting(item):
                yield item


def _plain_text(value):
    if not value:
        return ""
    cleaned = unescape(str(value))
    if "<" not in cleaned:
        return " ".join(cleaned.split())
    return " ".join(BeautifulSoup(cleaned, "html.parser").stripped_strings)


def _location(job):
    location = job.get("jobLocation") or ""
    if isinstance(location, list):
        location = location[0] if location else ""
    address = location.get("address", {}) if isinstance(location, dict) else {}
    if isinstance(address, str):
        return address, "", ""
    city = str(address.get("addressLocality") or "").strip()
    country_value = address.get("addressCountry") or ""
    country = str(country_value.get("name") or country_value.get("@id") or "").strip() if isinstance(country_value, dict) else str(country_value).strip()
    region = str(address.get("addressRegion") or "").strip()
    text = ", ".join(part for part in (city, region, country) if part)
    return text, country, city


def _meta_content(soup, *selectors):
    for selector in selectors:
        element = soup.select_one(selector)
        if element and element.get("content"):
            return element["content"].strip()
    return ""


def _element_value(element):
    if not element:
        return ""
    return _plain_text(
        element.get("content") or element.get("datetime") or element.get("value")
        or element.get_text(" ", strip=True)
    )


def _microdata_job(soup):
    scope = soup.select_one('[itemscope][itemtype*="JobPosting"]')
    if not scope:
        return {}
    organization = scope.select_one('[itemprop="hiringOrganization"]')
    location = scope.select_one('[itemprop="jobLocation"]')
    address = location.select_one('[itemprop="address"]') if location else None
    address = address or location
    country = _element_value(address.select_one('[itemprop="addressCountry"]')) if address else ""
    city = _element_value(address.select_one('[itemprop="addressLocality"]')) if address else ""
    region = _element_value(address.select_one('[itemprop="addressRegion"]')) if address else ""
    return {
        "title": _element_value(scope.select_one('[itemprop="title"]')),
        "description": _element_value(scope.select_one('[itemprop="description"]')),
        "company_name": _element_value(organization.select_one('[itemprop="name"]')) if organization else "",
        "location_text": ", ".join(part for part in (city, region, country) if part) or _element_value(location),
        "country": country,
        "city": city,
        "published_at": _element_value(scope.select_one('[itemprop="datePosted"]')),
        "employment_type": _element_value(scope.select_one('[itemprop="employmentType"]')),
    }


_LOCATION_PART = r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*(?:[ ]+[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*){0,3}"
_ROLE_START = r"(?:d[ée]veloppeu(?:r|se)s?|developers?|full[ -]?stack|software)"


def _strong_title_location(title):
    """Extract only delimiter-structured title locations, never free-text cities."""
    suffix = re.search(
        rf"\s+-\s+(?P<city>{_LOCATION_PART})\s+-\s+(?:F\s*/\s*H|H\s*/\s*F|M\s*/\s*F|F\s*/\s*M)\s*$",
        title or "", re.I,
    )
    if suffix:
        return suffix.group("city").strip()
    prefix = re.match(
        rf"^\s*(?P<city>{_LOCATION_PART})\s+-\s+{_ROLE_START}\b",
        title or "", re.I,
    )
    return prefix.group("city").strip() if prefix else ""


def _strong_url_location(url):
    """Extract a leading city only when a slug immediately introduces the role."""
    url_city = (
        r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*"
        r"(?:[- ][A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*){0,3}?"
    )
    for raw_segment in reversed(urlsplit(url).path.split("/")):
        segment = unquote(raw_segment).strip()
        if not segment:
            continue
        match = re.match(
            rf"^(?P<city>{url_city})[- ]+{_ROLE_START}(?:[- ]|$)",
            segment, re.I,
        )
        if match:
            candidate = re.sub(r"[-_]+", " ", match.group("city")).strip()
            if candidate.casefold() not in {
                "backend", "front end", "frontend", "full stack", "fullstack",
                "software", "cloud", "java", "python", "angular", "react",
            }:
                return candidate
    return ""


def parse_job_html(url, html, provider=None):
    canonical = normalize_job_url(url)
    provider = provider or detect_provider(canonical)
    result = ParsedJob(
        canonical_url=canonical,
        provider=provider,
        source_job_id=extract_source_job_id(provider, canonical),
        raw_content_hash=sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        fetch_status="FETCHED",
    )
    soup = BeautifulSoup(html, "html.parser")
    job = next(_iter_json_ld(soup), {})
    if job:
        organization = job.get("hiringOrganization") or {}
        result.title = _plain_text(job.get("title"))
        result.description = _plain_text(job.get("description"))
        result.company_name = _plain_text(organization.get("name")) if isinstance(organization, dict) else ""
        result.company_url = normalize_job_url(organization.get("sameAs") or organization.get("url") or "") if isinstance(organization, dict) else ""
        result.location_text, result.country, result.city = _location(job)
        result.employment_type = _plain_text(job.get("employmentType"))
        result.published_at = str(job.get("datePosted") or "").strip()
        result.apply_url = normalize_job_url(job.get("url") or canonical)
        if job.get("jobLocationType") == "TELECOMMUTE":
            result.remote_policy = "REMOTE"
        result.status = "OPEN"
    microdata = _microdata_job(soup)
    for field in ("title", "description", "company_name", "location_text", "country", "city", "published_at", "employment_type"):
        if not getattr(result, field) and microdata.get(field):
            setattr(result, field, microdata[field])
    if microdata:
        result.status = "OPEN"

    result.title = result.title or _meta_content(
        soup, 'meta[property="og:title"]', 'meta[name="twitter:title"]',
        'meta[name="job:title"]', 'meta[name="title"]',
    )
    result.description = result.description or _plain_text(_meta_content(
        soup, 'meta[property="og:description"]', 'meta[name="twitter:description"]',
        'meta[name="description"]', 'meta[name="job:description"]',
    ))
    result.title = result.title or _element_value(soup.select_one("main h1, article h1, h1"))
    if not result.title and soup.title:
        result.title = soup.title.get_text(" ", strip=True)
    # Provider fields outrank generic page metadata for location evidence.
    if provider == "greenhouse":
        result.company_name = result.company_name or _plain_text(soup.select_one(".company-name"))
        result.location_text = result.location_text or _plain_text(soup.select_one(".location"))
    elif provider == "lever":
        result.company_name = result.company_name or _plain_text(soup.select_one(".main-header-logo"))
        result.location_text = result.location_text or _plain_text(soup.select_one(".posting-categories .location"))
    result.company_name = result.company_name or _meta_content(
        soup, 'meta[name="job:company"]', 'meta[property="og:site_name"]',
    )
    result.location_text = result.location_text or _meta_content(
        soup, 'meta[name="job:location"]', 'meta[name="jobLocation"]',
    )
    result.published_at = result.published_at or _meta_content(
        soup, 'meta[name="datePosted"]', 'meta[property="article:published_time"]',
    )
    result.employment_type = result.employment_type or _meta_content(
        soup, 'meta[name="employmentType"]', 'meta[name="job:employment_type"]',
    )
    if not result.description:
        result.description = _element_value(soup.select_one(
            '[itemprop="description"], .job-description, #job-description, main article'
        ))
    if not result.location_text:
        result.location_text = _strong_title_location(result.title) or _strong_url_location(canonical)
        if result.location_text:
            result.city = result.city or result.location_text
    if not result.apply_url:
        apply_link = soup.select_one('a[href*="apply"]')
        if apply_link and apply_link.get("href"):
            result.apply_url = normalize_job_url(urljoin(canonical, apply_link["href"]))
    content = "\n".join((result.title, result.company_name, result.location_text, result.description))
    result.content_hash = sha256(content.encode("utf-8")).hexdigest() if content.strip() else ""
    return result


def fetch_job(url, timeout=15, session=None):
    provider = detect_provider(url)
    canonical = normalize_job_url(url)
    base = ParsedJob(canonical, provider, extract_source_job_id(provider, canonical))
    try:
        response = (session or requests).get(
            canonical,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GMapsScraper-JobDiscovery/1.0)"},
        )
        response.raise_for_status()
        return parse_job_html(canonical, response.text, provider)
    except requests.RequestException as error:
        base.fetch_status = "FAILED"
        base.fetch_error = f"{type(error).__name__}: {error}"[:1000]
        return base
