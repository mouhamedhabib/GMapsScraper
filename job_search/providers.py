"""Provider detection, source identity extraction, and shallow page parsing."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import json
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from job_search.normalization import normalize_job_url
from job_search.geography import (
    infer_known_city, infer_title_location, normalize_geography,
)
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
    "free-work.com", "remotive.com", "simplyhired.co.uk", "jobleads.com",
    "bebee.com", "internshala.com", "careerkit.me", "tuniatlas.com",
    "tanitjobs.com", "bayt.com",
}
HOSTED_ATS_DOMAINS = {"careers-page.com"}
TRUSTED_DIRECT_COMPANIES = {
    "group.bnpparibas": "BNP Paribas",
}
DIRECT_COMPANY_DOMAINS = {
    "infineon.com", "capgemini.com", "bitwarden.com", "craegroup.com",
} | set(TRUSTED_DIRECT_COMPANIES)
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
    region: str = ""
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
    evidence_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobResultClassification:
    accepted: bool
    normalized_url: str = ""
    provider: str = "generic"
    rejection_reason: str = ""


@dataclass(frozen=True)
class SourceContext:
    source_type: str
    employer_relationship: str


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
    """Legacy coarse host classification; ATS never proves employer directness."""
    provider = (provider or detect_provider(url)).casefold()
    if provider in ATS_PROVIDERS:
        return "ATS"
    domain = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    if any(_domain_matches(domain, host) for host in HOSTED_ATS_DOMAINS):
        return "ATS"
    if any(_domain_matches(domain, platform) for platform in JOB_PLATFORM_DOMAINS):
        return "JOB_PLATFORM"
    # URL vocabulary is not ownership evidence: third-party platforms commonly
    # use /job/ and careers.* themselves. Keep this allowlist conservative.
    if any(_domain_matches(domain, company) for company in DIRECT_COMPANY_DOMAINS):
        return "DIRECT_COMPANY"
    return "UNKNOWN"


def infer_trusted_company(url, title=""):
    """Recover a company only from trusted first-party or validated tenant evidence."""
    parsed = urlsplit(url)
    domain = (parsed.hostname or "").casefold().removeprefix("www.")
    for host, company in TRUSTED_DIRECT_COMPANIES.items():
        if _domain_matches(domain, host):
            return company, "direct_domain_company"

    if any(_domain_matches(domain, host) for host in HOSTED_ATS_DOMAINS):
        parts = [unquote(part).strip() for part in parsed.path.split("/") if part]
        match = re.search(
            r"\s-\s(?P<company>[^|]{2,100}?)\s*\|\s*Career Page\s*$",
            title or "", re.I,
        )
        if parts and match:
            candidate = " ".join(match.group("company").split())
            candidate_key = re.sub(r"[^a-z0-9]+", "-", _fold_tokens(candidate)[0]).strip("-")
            tenant_key = re.sub(r"[^a-z0-9]+", "-", _fold_tokens(parts[0])[0]).strip("-")
            if candidate_key == tenant_key and is_safe_company_name(candidate):
                return candidate, "validated_hosted_tenant_title"
    return "", ""


def classify_source_context(url, provider="", company_name=""):
    """Separate hosting/source type from the relationship to the employer."""
    provider = (provider or detect_provider(url)).casefold()
    quality = classify_source_quality(url, provider)
    source_type = {
        "ATS": "ATS", "JOB_PLATFORM": "JOB_PLATFORM",
        "DIRECT_COMPANY": "COMPANY_SITE", "UNKNOWN": "UNKNOWN",
    }[quality]
    domain = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    path_parts = [part.casefold() for part in urlsplit(url).path.split("/") if part]
    company = " ".join((company_name or "").casefold().split())
    recruiter_tenant = provider in ATS_PROVIDERS and bool(path_parts) and path_parts[0] in {
        "jobgether",
    }
    if "jobgether" in company or _domain_matches(domain, "jobgether.com") or recruiter_tenant:
        relationship = "RECRUITER"
    elif source_type == "JOB_PLATFORM":
        relationship = "AGGREGATOR"
    elif source_type == "COMPANY_SITE":
        relationship = "DIRECT"
    else:
        # An ATS hosts jobs for direct employers, recruiters, and aggregators.
        relationship = "UNKNOWN"
    return SourceContext(source_type, relationship)


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
        or bool(re.search(
            r"/(?:job-search|search-jobs|explore-careers|area-of-interest)(?:/|$)",
            path,
        ))
    )
    title_signal = any((
        re.search(r"\b\d+\s+(?:open|fresh|available)?\s*jobs\b", cleaned_title, re.I),
        re.search(r"\b\d+\s+vacanc(?:y|ies)\b", cleaned_title, re.I),
        re.search(r"\bjobs in\s+[^|]+", cleaned_title, re.I),
        re.search(r"\bmissions? freelance et emplois?\b", cleaned_title, re.I),
        re.search(r"\bcareers?\s*(?:&|and)\s*job opportunities\b", cleaned_title, re.I),
        re.fullmatch(
            r"(?:explore careers?|job opportunities|search jobs)"
            r"(?:\s*[|–—-]\s*[^|]+)?",
            cleaned_title,
            re.I,
        ),
    ))
    multiple_jobs = bool(re.search(
        r"\b(?:browse|explore)\s+\d+\s+(?:fresh\s+)?jobs\b|"
        r"\b\d+\s+missions? et offres? d['’]emploi\b|"
        r"\b(?:browse|explore|search)\s+(?:our\s+)?(?:open\s+)?(?:jobs|roles|vacancies)\b",
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
    if isinstance(value, (list, tuple)):
        return ", ".join(filter(None, (_plain_text(item) for item in value)))
    cleaned = unescape(str(value))
    if "<" not in cleaned:
        return " ".join(cleaned.split())
    return " ".join(BeautifulSoup(cleaned, "html.parser").stripped_strings)


COMPANY_NOISE = {
    "alias", "h/f", "f/h", "cdi", "cdd", "junior", "senior", "remote",
    "hybrid", "job application", "linkedin job wrapping",
}
PLATFORM_COMPANY_NOISE = {"wizbii", "waytolearnx"}
PLATFORM_COMPANY_NOISE.update({
    "indeed", "linkedin", "glassdoor", "ziprecruiter", "monster", "jooble",
    "wellfound", "careerjet", "simplyhired", "talent", "careerkit",
    "tuniatlas", "manatal",
})
COMPANY_NOISE_TOKENS = {
    "alias", "cdi", "cdd", "junior", "senior", "remote", "hybrid",
}
DERIVED_LOCATION_NOISE = {
    "developpeur", "developpeuse", "developer", "developers", "engineer",
    "engineers", "junior", "senior", "backend", "frontend", "fullstack",
    "full", "stack", "software", "cdi", "cdd", "h/f", "f/h", "remote",
    "hybrid", "ai", "ml", "java", "python", "javascript", "typescript",
    "react", "reactjs", "nestjs", "node", "nodejs", "angular", "php",
    "laravel", "django", "fastapi", "dotnet", "golang", "devops", "cloud",
}
STRUCTURED_COMPANY_SOURCES = {
    "jsonld_hiringOrganization", "provider_company_field",
}
DERIVED_LOCATION_SOURCES = {
    "title_structured_location", "url_structured_location",
    "TITLE_PATTERN", "URL_PATTERN",
}


def _fold_tokens(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    folded = "".join(char for char in normalized if not unicodedata.combining(char)).casefold()
    return folded.strip(), set(re.findall(r"[a-z0-9]+(?:/[a-z])?", folded))


def is_safe_company_name(value, source=""):
    """Reject known labels and platform branding unless evidence is explicit."""
    folded, tokens = _fold_tokens(" ".join(str(value or "").split()))
    if (
        not folded or folded in COMPANY_NOISE or tokens & COMPANY_NOISE_TOKENS
        or "job application" in folded
    ):
        return False
    if tokens & PLATFORM_COMPANY_NOISE and source not in STRUCTURED_COMPANY_SOURCES:
        return False
    return len(folded) >= 2


def is_safe_location_value(value, source="", city=False):
    """Validate locations, applying strict vocabulary checks to derived cities."""
    folded, tokens = _fold_tokens(" ".join(str(value or "").split()))
    words = " ".join(re.findall(r"[a-z0-9]+", folded))
    if not folded or words in {"idf", "ile de france"} and city:
        return False
    if words == "idf":
        return False
    # A value consisting entirely of role/technology vocabulary is never useful
    # location evidence, even if older rows did not retain extraction provenance.
    if tokens and tokens <= DERIVED_LOCATION_NOISE:
        return False
    if source in DERIVED_LOCATION_SOURCES and tokens & DERIVED_LOCATION_NOISE:
        return False
    return True


def _location(job):
    location = job.get("jobLocation") or ""
    if isinstance(location, list):
        location = location[0] if location else ""
    address = location.get("address", {}) if isinstance(location, dict) else {}
    if isinstance(address, str):
        return address, "", "", ""
    city = str(address.get("addressLocality") or "").strip()
    country_value = address.get("addressCountry") or ""
    country = str(country_value.get("name") or country_value.get("@id") or "").strip() if isinstance(country_value, dict) else str(country_value).strip()
    region = str(address.get("addressRegion") or "").strip()
    text = ", ".join(part for part in (city, region, country) if part)
    return text, country, city, region


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
        "region": region,
        "published_at": _element_value(scope.select_one('[itemprop="datePosted"]')),
        "employment_type": _element_value(scope.select_one('[itemprop="employmentType"]')),
    }


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def _greenhouse_job_fields(canonical, soup):
    """Read the job object embedded in Greenhouse's current board renderer."""
    if detect_provider(canonical) != "greenhouse":
        return {}
    payload = None
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        marker = "window.__remixContext ="
        if marker not in text:
            continue
        raw = text.split(marker, 1)[1].strip().removesuffix(";")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        break
    if payload is None:
        return {}
    job = next((item for item in _walk_dicts(payload)
                if item.get("title") and "job_post_location" in item), {})
    if not job:
        return {}
    description = "\n".join(filter(None, (
        _plain_text(job.get("introduction")), _plain_text(job.get("content")),
        _plain_text(job.get("conclusion")),
    )))
    fields = {
        "title": (job.get("title") or "", "provider_title_field"),
        "company_name": (job.get("company_name") or "", "provider_company_field"),
        "location_text": (job.get("job_post_location") or "", "provider_location_field"),
        "description": (description, "provider_description_field"),
        "published_at": (job.get("published_at") or "", "provider_date_field"),
    }
    employment = str(job.get("employment") or "").strip()
    if employment.casefold() != "hidden":
        fields["employment_type"] = (employment, "provider_employment_field")
    return fields


def _internshala_job_fields(canonical, soup):
    domain = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
    if domain != "internshala.com" or "/job/detail/" not in urlsplit(canonical).path:
        return {}
    location = _element_value(soup.select_one("#location_names a"))
    company = _element_value(soup.select_one(".company_name a, .company-name a"))
    return {
        "location_text": (location, "provider_location_field"),
        "city": (location, "provider_location_field"),
        "company_name": (company, "provider_company_field"),
    }


def _bitwarden_job_fields(canonical, soup):
    """Read Bitwarden's page-local Inertia job payload when its id matches."""
    domain = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
    if domain != "bitwarden.com" or "/careers/" not in urlsplit(canonical).path:
        return {}
    page = soup.select_one("#app[data-page]")
    if not page:
        return {}
    try:
        payload = json.loads(page.get("data-page") or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    job = payload.get("props", {}).get("job", {}) if isinstance(payload, dict) else {}
    if not isinstance(job, dict):
        return {}
    requested_id = (parse_qs(urlsplit(canonical).query).get("gh_jid") or [""])[0]
    payload_id = str(job.get("greenhouse_id") or "").strip()
    if not requested_id or requested_id != payload_id:
        return {}
    return {
        "location_text": (job.get("location") or "", "provider_location_field"),
        "description": (job.get("content") or "", "provider_description_field"),
        "published_at": (job.get("createdAt") or "", "provider_date_field"),
    }


def _waytolearnx_job_fields(canonical, soup):
    """Read reusable fields from one WayToLearnX job detail block."""
    domain = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
    if domain != "jobs.waytolearnx.com" or not urlsplit(canonical).path.startswith("/job/"):
        return {}
    scope = soup.select_one("section.job-detail-section")
    if not scope:
        return {}
    title = _element_value(scope.select_one(".job-block-seven h4"))
    logo = scope.select_one(".job-block-seven .company-logo img[alt]")
    company = (logo.get("alt") or "").strip() if logo else ""
    fields = {"company_name": (company, "provider_company_field")}
    # IDF is a region, never a city. Keep the explicit title evidence at the
    # region level even if another page widget happens to name a nearby city.
    if re.search(r"(?:-|–|—)\s*IDF\s*$", title, re.I):
        fields.update({
            "location_text": ("Île-de-France", "title_structured_location"),
            "region": ("Île-de-France", "title_structured_location"),
            "country": ("France", "title_structured_location"),
        })
    return fields


def _welovedevs_job_fields(canonical, soup):
    """Read WeLoveDevs' server-rendered job header, prose, and payload date."""
    domain = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
    path = urlsplit(canonical).path
    if domain != "welovedevs.com" or "/app/job/" not in path:
        return {}
    scope = soup.select_one("main")
    if not scope or not scope.select_one("h1"):
        return {}
    application = scope.select_one(
        'a[href*="jobs.stationf.co/companies/"][href*="/jobs/"]'
    )
    company = _element_value(application.select_one("span")) if application else ""
    location_link = scope.select_one('a[href*="google.com/maps/search/"]')
    location = _element_value(location_link)
    fields = {
        "company_name": (company, "provider_company_field"),
        "location_text": (location, "provider_location_field"),
    }
    location_parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(location_parts) >= 2 and location_parts[-1].casefold() == "france":
        fields["country"] = ("France", "provider_location_field")
        fields["city"] = (location_parts[0], "provider_location_field")
    description = _element_value(scope.select_one("span.prose"))
    if description:
        fields["description"] = (description, "provider_description_field")
    raw_html = str(soup)
    alias = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if re.search(rf'\\?"seoAlias\\?":\\?"{re.escape(alias)}\\?"', raw_html):
        published = re.search(r'\\?"publishDate\\?":(?P<timestamp>\d{10,13})', raw_html)
        if published:
            timestamp = int(published.group("timestamp"))
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            fields["published_at"] = (
                datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat(),
                "provider_date_field",
            )
    return fields


def _tuniatlas_job_fields(canonical, soup):
    """Read one TuniAtlas job card without following its external apply link."""
    domain = (urlsplit(canonical).hostname or "").casefold().removeprefix("www.")
    if domain != "tuniatlas.com" or not urlsplit(canonical).path.startswith("/jobs/"):
        return {}
    heading = soup.select_one("h1.page-title")
    description = soup.select_one(".job-desc")
    if not heading or not description:
        return {}
    fields = {
        "title": (_element_value(heading), "provider_title_field"),
        "description": (_element_value(description), "provider_description_field"),
    }
    published = _element_value(soup.select_one(".pill.good"))
    match = re.search(
        r"Publi[ée]e?\s+le\s+(?P<day>\d{1,2})\s+(?P<month>[A-Za-zÀ-ÖØ-öø-ÿ]{3,5})\s+(?P<year>\d{4})",
        published, re.I,
    )
    if match:
        month_key = _fold_tokens(match.group("month"))[0]
        months = {
            "jan": 1, "fev": 2, "mar": 3, "avr": 4, "mai": 5,
            "jun": 6, "juin": 6, "jul": 7, "juil": 7, "juillet": 7,
            "aou": 8, "sep": 9, "oct": 10,
            "nov": 11, "dec": 12,
        }
        month_key = month_key if month_key in months else month_key[:3]
        if month_key in months:
            fields["published_at"] = (
                f"{int(match.group('year')):04d}-{months[month_key]:02d}-{int(match.group('day')):02d}",
                "provider_date_field",
            )
    return fields


def _provider_job_fields(canonical, soup):
    for parser in (
        _greenhouse_job_fields, _internshala_job_fields, _bitwarden_job_fields,
        _waytolearnx_job_fields, _welovedevs_job_fields,
        _tuniatlas_job_fields,
    ):
        fields = parser(canonical, soup)
        if fields:
            return fields
    return {}


_LOCATION_PART = r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*(?:[ ]+[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.]*){0,3}"
_ROLE_START = r"(?:d[ée]veloppeu(?:r|se)s?|developers?|full[ -]?stack|software)"


def _strong_url_title(url):
    """Recover a title only from an individual job/careers slug with a role noun."""
    segments = [unquote(part).strip() for part in urlsplit(url).path.split("/") if part]
    if len(segments) < 2 or not any(
        part.casefold() in JOB_PATH_SEGMENTS for part in segments[:-1]
    ):
        return ""
    slug = re.sub(r"[-_]+", " ", segments[-1]).strip()
    slug = re.sub(r"\s+", " ", slug)
    slug = re.sub(r"\s+\d{5,}$", "", slug).strip()
    if not re.search(
        r"\b(?:developers?|engineers?|programmers?|d[ée]veloppeu(?:r|se)s?)\b",
        slug,
        re.I,
    ):
        return ""
    if not 2 <= len(slug.split()) <= 12 or re.fullmatch(r"[0-9a-f-]+", slug, re.I):
        return ""
    return " ".join(
        word.upper() if word.casefold() in {"ai", "it"}
        else word if re.search(r"[A-Z]", word[1:]) else word.capitalize()
        for word in slug.split()
    )


def is_valid_job_title(value, url=""):
    """Reject only demonstrably generic page titles, slogans, and labels."""
    title = " ".join(str(value or "").split())
    if not title:
        return False
    folded = title.casefold()
    if re.fullmatch(
        r"offres? d['’]emploi et travail en tunisie(?:\s*[|–—-].*)?",
        folded,
    ):
        return False
    if folded in {"careers", "jobs", "job application", "open positions"} or re.search(
        r"\b(?:404|not found|page not found|access denied|error)\b", folded,
    ):
        return False
    inferred = _strong_url_title(url)
    if inferred and not re.search(
        r"\b(?:developer|engineer|programmer|manager|analyst|designer|consultant|"
        r"intern|researcher|specialist|architect)\b", title, re.I,
    ) and re.search(r"\b(?:technology partner|welcome to|company|homepage)\b", folded):
        return False
    return True


def _strong_title_location(title):
    """Extract only delimiter-structured title locations, never free-text cities."""
    suffix = re.search(
        rf"\s+(?:-|–|—)\s+(?P<city>{_LOCATION_PART})\s+(?:-|–|—)\s+"
        rf"(?:F\s*/\s*H(?:\s*/\s*X)?|H\s*/\s*F(?:\s*/\s*X)?|M\s*/\s*F|F\s*/\s*M|CDI|CDD|FULL[ -]?TIME|PERMANENT)\s*$",
        title or "", re.I,
    )
    if suffix:
        candidate = suffix.group("city").strip()
        return candidate if is_safe_location_value(candidate, "title_structured_location", city=True) else ""
    job_in = re.search(
        rf"\bjob in (?P<city>{_LOCATION_PART})\s+at\b", title or "", re.I,
    )
    if job_in:
        candidate = job_in.group("city").strip()
        return candidate if is_safe_location_value(candidate, "title_structured_location", city=True) else ""
    prefix = re.match(
        rf"^\s*(?P<city>{_LOCATION_PART})\s+(?:-|–|—)\s+{_ROLE_START}\b",
        title or "", re.I,
    )
    if not prefix:
        return ""
    candidate = prefix.group("city").strip()
    return candidate if is_safe_location_value(candidate, "title_structured_location", city=True) else ""


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
            if is_safe_location_value(candidate, "url_structured_location", city=True):
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
    for field, (candidate, source) in _provider_job_fields(canonical, soup).items():
        candidate = _plain_text(candidate)
        safe = (
            is_valid_job_title(candidate, canonical) if field == "title"
            else is_safe_company_name(candidate, source) if field == "company_name"
            else is_safe_location_value(candidate, source, city=field == "city")
            if field in {"location_text", "city"} else True
        )
        if not getattr(result, field) and candidate and safe:
            setattr(result, field, candidate)
            result.evidence_sources[field] = source
    if result.location_text:
        if not result.city:
            provider_city = infer_known_city(result.location_text)
            if provider_city:
                result.city = provider_city
                result.evidence_sources["city"] = result.evidence_sources["location_text"]
        provider_geography = normalize_geography(
            result.location_text, result.city, result.country, result.title,
        )
        if not result.country and provider_geography.country:
            result.country = provider_geography.country
            result.evidence_sources["country"] = result.evidence_sources.get(
                "location_text", "provider_location_field",
            )

    job = next(_iter_json_ld(soup), {})
    if job:
        organization = job.get("hiringOrganization") or {}
        json_title = _plain_text(job.get("title"))
        if not result.title and is_valid_job_title(json_title, canonical):
            result.title = json_title
            result.evidence_sources["title"] = "jsonld_title"
        if not result.description:
            result.description = _plain_text(job.get("description"))
            if result.description:
                result.evidence_sources["description"] = "jsonld_description"
        company = _plain_text(organization.get("name")) if isinstance(organization, dict) else ""
        if not result.company_name and is_safe_company_name(company, "jsonld_hiringOrganization"):
            result.company_name = company
            result.evidence_sources["company_name"] = "jsonld_hiringOrganization"
        if not result.company_url and isinstance(organization, dict):
            result.company_url = normalize_job_url(organization.get("sameAs") or organization.get("url") or "")
        json_location, json_country, json_city, json_region = _location(job)
        for field, candidate in (
            ("location_text", json_location), ("country", json_country),
            ("city", json_city), ("region", json_region),
        ):
            if not getattr(result, field) and is_safe_location_value(candidate, "jsonld_jobLocation", city=field == "city"):
                setattr(result, field, candidate)
                result.evidence_sources[field] = "jsonld_jobLocation"
        if not result.employment_type:
            result.employment_type = _plain_text(job.get("employmentType"))
            if result.employment_type:
                result.evidence_sources["employment_type"] = "jsonld_employmentType"
        if not result.published_at:
            result.published_at = str(job.get("datePosted") or "").strip()
            if result.published_at:
                result.evidence_sources["published_at"] = "jsonld_datePosted"
        result.apply_url = normalize_job_url(job.get("url") or canonical)
        if job.get("jobLocationType") == "TELECOMMUTE":
            result.remote_policy = "REMOTE"
        result.status = "OPEN"
    microdata = _microdata_job(soup)
    for field in ("title", "description", "company_name", "location_text", "country", "city", "region", "published_at", "employment_type"):
        if not getattr(result, field) and microdata.get(field):
            source = {
                "company_name": "microdata_hiringOrganization",
                "location_text": "microdata_jobLocation",
                "country": "microdata_jobLocation",
                "city": "microdata_jobLocation",
                "region": "microdata_jobLocation",
                "description": "microdata_description",
                "published_at": "microdata_datePosted",
                "employment_type": "microdata_employmentType",
            }.get(field, "microdata_title")
            candidate = microdata[field]
            safe = (
                is_safe_company_name(candidate, source) if field == "company_name"
                else is_safe_location_value(candidate, source, city=field == "city")
                if field in {"location_text", "city", "region"} else True
            )
            if safe:
                setattr(result, field, candidate)
                result.evidence_sources[field] = source
    if microdata:
        result.status = "OPEN"

    job_meta_title = _meta_content(soup, 'meta[name="job:title"]')
    if not result.title and is_valid_job_title(job_meta_title, canonical):
        result.title = job_meta_title
        result.evidence_sources["title"] = "job_title_meta"
    h1_title = _element_value(soup.select_one("main h1, article h1, h1"))
    if not result.title and is_valid_job_title(h1_title, canonical):
        result.title = h1_title
        result.evidence_sources["title"] = "html_job_heading"
    generic_meta_title = _meta_content(
        soup, 'meta[property="og:title"]', 'meta[name="twitter:title"]',
        'meta[name="title"]',
    )
    if not result.title and is_valid_job_title(generic_meta_title, canonical):
        result.title = generic_meta_title
        result.evidence_sources["title"] = "PAGE_METADATA"
    document_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not result.title and is_valid_job_title(document_title, canonical):
        result.title = document_title
        result.evidence_sources["title"] = "PAGE_CONTENT"
    if not result.title:
        inferred_title = _strong_url_title(canonical)
        if inferred_title:
            result.title = inferred_title
            result.evidence_sources["title"] = "url_structured_title"
    job_meta_description = _plain_text(_meta_content(soup, 'meta[name="job:description"]'))
    if not result.description and job_meta_description:
        result.description = job_meta_description
        result.evidence_sources["description"] = "job_description_meta"
    # Provider fields outrank generic page metadata for location evidence.
    if provider == "greenhouse":
        company = _plain_text(soup.select_one(".company-name"))
        location = _plain_text(soup.select_one(".location"))
        if not result.company_name and is_safe_company_name(company, "provider_company_field"):
            result.company_name = company
            result.evidence_sources["company_name"] = "provider_company_field"
        if not result.location_text and is_safe_location_value(location, "provider_location_field"):
            result.location_text = location
            result.evidence_sources["location_text"] = "provider_location_field"
    elif provider == "lever":
        company = _plain_text(soup.select_one(".main-header-logo"))
        location = _plain_text(soup.select_one(".posting-categories .location"))
        if not result.company_name and is_safe_company_name(company, "provider_company_field"):
            result.company_name = company
            result.evidence_sources["company_name"] = "provider_company_field"
        if not result.location_text and is_safe_location_value(location, "provider_location_field"):
            result.location_text = location
            result.evidence_sources["location_text"] = "provider_location_field"
    job_company_meta = _meta_content(soup, 'meta[name="job:company"]')
    open_graph_company = _meta_content(soup, 'meta[property="og:site_name"]')
    for company_meta, source in (
        (job_company_meta, "job_company_meta"),
        (open_graph_company, "open_graph_site_name"),
    ):
        if not result.company_name and is_safe_company_name(company_meta, source):
            result.company_name = company_meta
            result.evidence_sources["company_name"] = source
    location_meta = _meta_content(
        soup, 'meta[name="job:location"]', 'meta[name="jobLocation"]',
    )
    if not result.location_text and is_safe_location_value(location_meta, "job_location_meta"):
        result.location_text = location_meta
        result.evidence_sources["location_text"] = "job_location_meta"
    published_meta = _meta_content(
        soup, 'meta[name="datePosted"]', 'meta[property="article:published_time"]',
    )
    if not result.published_at and published_meta:
        result.published_at = published_meta
        result.evidence_sources["published_at"] = "PAGE_METADATA"
    employment_meta = _meta_content(
        soup, 'meta[name="employmentType"]', 'meta[name="job:employment_type"]',
    )
    if not result.employment_type and employment_meta:
        result.employment_type = employment_meta
        result.evidence_sources["employment_type"] = "PAGE_METADATA"
    if not result.description:
        result.description = _element_value(soup.select_one(
            '[itemprop="description"], .job-description, #job-description, main article'
        ))
        if result.description:
            result.evidence_sources["description"] = "PAGE_CONTENT"
    title_region = infer_title_location(result.title)
    title_location = _strong_title_location(result.title) if not title_region else ""
    if not result.location_text:
        url_location = (
            _strong_url_location(canonical)
            if not title_region and not title_location else ""
        )
        result.location_text = (
            title_region.location_text if title_region
            else title_location or url_location
        )
        if result.location_text:
            if not title_region:
                result.city = result.city or result.location_text
            source = (
                title_region.source if title_region
                else "title_structured_location" if title_location
                else "url_structured_location"
            )
            result.evidence_sources["location_text"] = source
            if result.city:
                result.evidence_sources["city"] = source
            if title_region and not result.country:
                result.country = title_region.country
                result.evidence_sources["country"] = source
            if title_region and not result.region:
                result.region = title_region.location_text
                result.evidence_sources["region"] = source
    elif not result.city and title_location:
        result.city = title_location
        result.evidence_sources["city"] = "title_structured_location"
    if not result.city:
        known_city = infer_known_city(result.location_text)
        if known_city:
            result.city = known_city
            result.evidence_sources["city"] = result.evidence_sources.get(
                "location_text", "deterministic_geography",
            )
    geography = normalize_geography(
        result.location_text, result.city, result.country, result.title,
    )
    if not result.country and geography.country:
        result.country = geography.country
        result.evidence_sources["country"] = (
            result.evidence_sources.get("location_text")
            or result.evidence_sources.get("city")
            or "title_structured_location"
        )
    if not result.apply_url:
        apply_link = soup.select_one('a[href*="apply"]')
        if apply_link and apply_link.get("href"):
            result.apply_url = normalize_job_url(urljoin(canonical, apply_link["href"]))
    if not result.company_name:
        trusted_company, source = infer_trusted_company(canonical, result.title)
        if trusted_company:
            result.company_name = trusted_company
            result.evidence_sources["company_name"] = source
    content = "\n".join((result.title, result.company_name, result.location_text, result.description))
    result.content_hash = sha256(content.encode("utf-8")).hexdigest() if content.strip() else ""
    return result


def fetch_job(url, timeout=15, session=None):
    provider = detect_provider(url)
    canonical = normalize_job_url(url)
    base = ParsedJob(canonical, provider, extract_source_job_id(provider, canonical))
    fallback_title = _strong_url_title(canonical)
    if fallback_title:
        base.title = fallback_title
        base.evidence_sources["title"] = "url_structured_title"
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
