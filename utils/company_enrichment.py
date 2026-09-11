"""
Company Website Enrichment
==========================

Purpose:
    Extract concise, source-backed company context from public website pages
    without inventing missing facts.

Pipeline:
    enrich_leads.py -> company_enrichment.py -> enriched company fields

Input:
    A Selenium driver, one company website URL, and a page-load timeout.

Output:
    Title, meta description, hero/about text, services, LinkedIn URL, a concise
    description, and a conservative industry classification. ``enrich_leads.py``
    writes these fields to ``leads_enriched.csv``.
"""

from re import IGNORECASE, compile, findall, split
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


# ---------------------------------------------------------------------------
# OUTPUT CONTRACT AND DESCRIPTION QUALITY RULES
# ---------------------------------------------------------------------------
# Empty strings explicitly represent unsupported facts. Noise and marketing
# filters keep navigation, error pages, and calls to action out of descriptions.

EMPTY_ENRICHMENT = {
    "website_title": "",
    "website_meta_description": "",
    "hero_text": "",
    "about_text": "",
    "services": "",
    "linkedin_url": "",
    "description": "",
    "industry": "",
}

FAILURE_TYPE_PRIORITY = {
    "UNKNOWN_NAVIGATION": 0,
    "BROWSER": 1,
    "CONNECTION": 2,
    "SSL": 3,
    "DNS": 4,
    "TIMEOUT": 5,
}

DESCRIPTION_FIELDS = (
    "website_meta_description",
    "about_text",
    "hero_text",
    "services",
    "website_title",
)

# ---------------------------------------------------------------------------
# CONSERVATIVE INDUSTRY CLASSIFICATION
# ---------------------------------------------------------------------------
# Evidence from descriptive page regions outweighs generic service/title terms;
# ambiguous or weak evidence intentionally produces an empty industry value.

INDUSTRY_FIELD_WEIGHTS = {
    "website_meta_description": 5,
    "about_text": 4,
    "hero_text": 4,
    "services": 1,
    "website_title": 1,
}

PRIMARY_INDUSTRY_FIELDS = {
    "website_meta_description",
    "about_text",
    "hero_text",
}

ERP_PRIMARY_EVIDENCE = compile(
    r"\berp\b|enterprise resource planning|progiciel de gestion int[ée]gr[ée]",
    flags=IGNORECASE,
)

ERP_SECONDARY_FEATURE_CATEGORIES = {
    "AI / Automation",
    "E-commerce",
}

RELATED_TECHNOLOGY_CATEGORIES = {
    "Web Development",
    "Mobile Development",
    "IT Services",
    "IT Consulting",
    "Cloud / Infrastructure",
    "Data / Analytics",
    "AI / Automation",
}

MARKETING_SENTENCE = compile(
    r"^(?:discover|learn more|find out|contact us|get in touch|reach us|"
    r"reach out|then reach out|we(?:'|’)d love to connect|need help|"
    r"start your project|let(?:'|’)s work together|talk to us|request a quote|"
    r"book a call|ready to\b|if you(?:'|’)?re passionate|if you are passionate|"
    r"get started|join us|"
    r"d[ée]couvrez|en savoir plus|contactez[- ]nous|commencez|rejoignez[- ]nous|"
    r"remplacez|boostez|transformez|r[ée]volutionnez)\b|"
    r"\b(?:best[- ]in[- ]class|world[- ]class|cutting[- ]edge|game[- ]changing|"
    r"industry[- ]leading|solution innovante|leader du march[ée])\b",
    flags=IGNORECASE,
)

ERROR_PAGE_MARKER = compile(
    r"\b(?:400\s+bad request|401\s+unauthorized|403\s+forbidden|"
    r"404\s+not found|429\s+too many requests|500\s+internal server error|"
    r"502\s+bad gateway|503\s+service unavailable|504\s+gateway timeout)\b|"
    r"\berr_connection(?:_[a-z_]+)?\b|site can(?:not|'t|’t) be reached|"
    r"temporarily unavailable|temporarily unable to service your request",
    flags=IGNORECASE,
)

INTERSTITIAL_PAGE_MARKER = compile(
    r"access denied|checking your browser|verify (?:that )?you are human|"
    r"enable javascript and cookies to continue|cloudflare ray id|"
    r"attention required!?(?:\s*\|\s*cloudflare)?|^just a moment",
    flags=IGNORECASE,
)

CONTACT_LEGAL_BLOCK = compile(
    r"^(?:(?:phone|telephone|t[ée]l(?:[ée]phone)?|e-?mail|"
    r"(?:office\s+)?address|office hours?)\s*:?(?:\s*$|\s+(?:\+?\d|[\w.+-]+@))|"
    r"follow us(?:\s*:|\s*$)|social links?(?:\s*:|\s*$)|"
    r"terms(?: and conditions)?\b|conditions? g[ée]n[ée]rales?\b|"
    r"privacy(?: policy)?\b|copyright\b|©|all rights reserved\b|"
    r"tous droits r[ée]serv[ée]s\b|contact us\b|get in touch\b|reach us\b|"
    r"we(?:'|’)d love to connect\b|entreprise\s*$|suivez[- ]nous\s*$)",
    flags=IGNORECASE,
)

ABOUT_LINK_MARKER = compile(
    r"\babout(?: us)?\b|\bcompany\b|who we are|our story|[àa] propos|"
    r"notre entreprise|pr[ée]sentation",
    flags=IGNORECASE,
)

SERVICE_LINK_MARKER = compile(
    r"\bservices?\b|\bsolutions?\b|\bproducts?\b|\bproduits?\b|"
    r"\bexpertises?\b|what we do|nos services|nos solutions",
    flags=IGNORECASE,
)

CONTACT_VALUE_BLOCK = compile(
    r"^(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\+?\d[\d\s().-]{6,}|"
    r"\d{1,6}\s+(?:[\w.'’ -]+\s)?(?:street|road|avenue|boulevard|rue)\b|"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)",
    flags=IGNORECASE,
)

INDUSTRY_RULES = (
    ("ERP / Business Software", (
        (r"\berp\b", 5),
        (r"enterprise resource planning|progiciel de gestion int[ée]gr[ée]", 5),
        (r"gestion (?:d['’]entreprise|int[ée]gr[ée]e)", 3),
        (r"business management software|logiciel de gestion", 3),
    )),
    ("Cybersecurity", (
        (r"cyber\s?s[ée]curit[ée]|cybersecurity|information security", 5),
        (r"penetration testing|threat detection|security operations|\bsoc\b", 4),
        (r"s[ée]curit[ée] (?:informatique|des syst[èe]mes|des donn[ée]es)", 4),
    )),
    ("Fintech", (
        (r"\bfintech\b|financial technology", 5),
        (r"payment platform|plateforme de paiement|digital banking|mobile banking", 4),
        (r"paiement en ligne|online payments?|payment processing", 3),
    )),
    ("E-commerce", (
        (r"e[- ]?commerce|commerce [ée]lectronique|online store|boutique en ligne", 5),
        (r"marketplace|shopping cart|panier d['’]achat", 3),
    )),
    ("Web Development", (
        (r"web development|d[ée]veloppement web|web application development", 5),
        (r"website development|cr[ée]ation de sites? web|conception de sites? web", 4),
    )),
    ("Mobile Development", (
        (
            r"mobile (?:app|application) development|"
            r"d[ée]veloppement (?:d['’])?applications? mobiles?",
            5,
        ),
        (r"ios and android|android and ios|applications? (?:ios|android)", 4),
    )),
    ("Cloud / Infrastructure", (
        (r"cloud infrastructure|infrastructure cloud|cloud computing", 5),
        (r"devops|cloud migration|migration vers le cloud|cloud hosting", 4),
        (r"infrastructure (?:it|informatique)|data cent(?:er|re)", 3),
    )),
    ("IT Consulting", (
        (r"it consulting|technology consulting|conseil (?:en )?(?:it|informatique|technologie)", 5),
        (r"digital transformation consulting|conseil en transformation num[ée]rique", 4),
    )),
    ("IT Services", (
        (r"managed it services|services? informatiques?|infog[ée]rance", 5),
        (r"systems? integration|int[ée]gration (?:de )?syst[èe]mes|it support", 4),
        (r"outsourcing (?:it|informatique)|externalisation informatique", 4),
    )),
    ("Digital Agency", (
        (r"digital agency|agence digitale|agence num[ée]rique|web agency", 5),
        (r"agence web|creative agency|agence cr[ée]ative", 4),
    )),
    ("Marketing / Communication", (
        (r"digital marketing|marketing digital|communication agency|agence de communication", 5),
        (r"social media marketing|content marketing|r[ée]f[ée]rencement|\bseo\b|\bsem\b", 4),
        (r"branding|gestion des r[ée]seaux sociaux", 3),
    )),
    ("Data / Analytics", (
        (r"data analytics|data science|business intelligence|analyse de donn[ée]es", 5),
        (r"data platform|plateforme de donn[ée]es|data engineering|\bbi\b", 4),
        (r"data products?|reporting and analytics|reporting et analytique", 3),
    )),
    ("AI / Automation", (
        (r"artificial intelligence|intelligence artificielle|machine learning", 5),
        (r"large language models?|\bllm applications?\b", 5),
        (
            r"ai[- ]powered|aliment[ée]e? par l['’]ia|\bai\b|\bia\b|"
            r"\bautomation\b|automatisation",
            4,
        ),
        (r"robotic process automation|\brpa\b", 4),
    )),
    ("Manufacturing", (
        (r"manufacturing (?:plant|facility)|fabrication industrielle", 5),
        (r"\b(?:usine|usines|factory)\b|production lines?|cha[îi]nes? de production", 5),
        (r"machinery|industrial equipment|[ée]quipements? industriels?", 4),
        (r"physical goods manufacturing|manufactur(?:e|er|ing) of physical goods", 5),
    )),
    ("Tourism", (
        (r"\btourism\b|\btourisme\b|travel agency|agence de voyages?", 5),
        (r"tour operator|h[ée]bergement touristique|hotel booking|r[ée]servation d['’]h[ôo]tel", 4),
    )),
    ("SaaS", (
        (r"\bsaas\b|software as a service|logiciel en tant que service", 5),
        (r"cloud[- ]based (?:software|platform)|plateforme (?:cloud|collaborative)", 4),
        (r"collaboration platform|plateforme tout[- ]en[- ]un", 3),
        (r"subscription[- ]based software|logiciel par abonnement", 3),
    )),
    ("Software", (
        (r"\bsoftware\b|\blogiciels?\b", 3),
        (r"software development|[ée]diteur de logiciels?|application platform", 4),
        (r"digital platforms?|enterprise applications?|applications? d['’]entreprise", 3),
    )),
)

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


def is_error_text(value):
    return bool(ERROR_PAGE_MARKER.search(normalize_whitespace(value)))


def is_contact_or_legal_text(value):
    cleaned = normalize_whitespace(value)
    return bool(
        CONTACT_LEGAL_BLOCK.search(cleaned)
        or CONTACT_VALUE_BLOCK.search(cleaned)
    )


def is_content_noise(value):
    return (
        is_boilerplate(value)
        or is_error_text(value)
        or is_contact_or_legal_text(value)
        or bool(MARKETING_SENTENCE.search(normalize_whitespace(value)))
    )


def _description_tokens(value):
    return {
        token.casefold()
        for token in findall(r"[^\W_]+", value)
        if len(token) >= 3
    }


def _description_candidates(field, value):
    value = normalize_whitespace(value)
    if not value:
        return []

    # A services value is already a compact, curated list; retain it as one
    # thought instead of turning each short service label into a sentence.
    if field == "services":
        parts = [value.replace(";", ",")]
    else:
        parts = split(r"(?<=[.!?])\s+|\s*;\s*", value)

    candidates = []
    for part in parts:
        candidate = normalize_whitespace(part).strip(" -–—|,;:")
        tokens = _description_tokens(candidate)
        if len(candidate) < 30 or len(tokens) < 4 or is_content_noise(candidate):
            continue
        candidates.append(candidate)
    return candidates


def _repeats_description(candidate_tokens, selected_tokens):
    for existing in selected_tokens:
        overlap = candidate_tokens & existing
        if (candidate_tokens <= existing or existing <= candidate_tokens
                or len(overlap) / max(len(candidate_tokens | existing), 1) >= 0.7):
            return True
    return False


def build_company_description(extracted):
    """Build a short description containing only phrases from extracted text."""
    selected = []
    selected_tokens = []
    for field in DESCRIPTION_FIELDS:
        for candidate in _description_candidates(field, extracted.get(field, "")):
            tokens = _description_tokens(candidate)
            if _repeats_description(tokens, selected_tokens):
                continue
            selected.append(candidate)
            selected_tokens.append(tokens)
            if len(selected) == 2:
                break
        if len(selected) == 2:
            break

    if not selected:
        return ""

    description = selected[0]
    if len(selected) == 2:
        separator = " " if description.endswith((".", "!", "?")) else ". "
        combined = description + separator + selected[1]
        if len(combined) <= 400:
            description = combined
    return trim_text(description, 400)


def classify_company_industry(extracted):
    """Return one explainable industry from keyword evidence in extracted text."""
    field_evidence = {
        field: normalize_whitespace(extracted.get(field, "")).casefold()
        for field in DESCRIPTION_FIELDS
    }
    if not any(field_evidence[field] for field in DESCRIPTION_FIELDS[:-1]):
        return ""

    scores = {}
    primary_scores = {}
    primary_detail_scores = {}
    service_detail_scores = {}
    for industry, rules in INDUSTRY_RULES:
        score = 0
        primary_score = 0
        primary_detail_score = 0
        service_detail_score = 0
        for field, evidence in field_evidence.items():
            if not evidence:
                continue
            matched_weights = [
                weight for pattern, weight in rules
                if compile(pattern, flags=IGNORECASE).search(evidence)
            ]
            field_score = max(matched_weights, default=0)
            weighted_score = field_score * INDUSTRY_FIELD_WEIGHTS[field]
            score += weighted_score
            if field in PRIMARY_INDUSTRY_FIELDS:
                primary_score += weighted_score
                primary_detail_score += (
                    sum(matched_weights) * INDUSTRY_FIELD_WEIGHTS[field]
                )
            elif field == "services":
                service_detail_score += sum(matched_weights)
        scores[industry] = score
        primary_scores[industry] = primary_score
        primary_detail_scores[industry] = primary_detail_score
        service_detail_scores[industry] = service_detail_score

    # SaaS and Software are deliberately fallbacks: a supported business
    # domain wins over a delivery model or generic technology label.
    specific_scores = {
        industry: score for industry, score in scores.items()
        if industry not in {"SaaS", "Software"} and score >= 3
    }
    saas_score = scores.get("SaaS", 0)
    if not specific_scores:
        if saas_score >= 3:
            return "SaaS"
        return "Software" if scores.get("Software", 0) >= 3 else ""

    best_score = max(specific_scores.values())
    if saas_score > best_score:
        return "SaaS"
    winners = [
        industry for industry, score in specific_scores.items()
        if score == best_score
    ]
    if len(winners) == 1:
        return winners[0]

    # When total scores tie, prefer the sole category supported most strongly
    # in product-defining fields. A genuine primary-field tie remains Other.
    best_primary_score = max(primary_scores[industry] for industry in winners)
    primary_winners = [
        industry for industry in winners
        if primary_scores[industry] == best_primary_score
    ]
    if best_primary_score and len(primary_winners) == 1:
        return primary_winners[0]

    winner_set = set(winners)
    if ("ERP / Business Software" in winner_set
            and winner_set <= ({"ERP / Business Software"}
                               | ERP_SECONDARY_FEATURE_CATEGORIES)
            and any(
                ERP_PRIMARY_EVIDENCE.search(field_evidence[field])
                for field in DESCRIPTION_FIELDS[:-1]
            )):
        return "ERP / Business Software"

    if winner_set <= RELATED_TECHNOLOGY_CATEGORIES:
        best_detail_score = max(
            primary_detail_scores[industry] for industry in winners
        )
        detail_winners = [
            industry for industry in winners
            if primary_detail_scores[industry] == best_detail_score
        ]
        if best_detail_score and len(detail_winners) == 1:
            return detail_winners[0]

        best_service_score = max(
            service_detail_scores[industry] for industry in detail_winners
        )
        service_winners = [
            industry for industry in detail_winners
            if service_detail_scores[industry] == best_service_score
        ]
        if best_service_score and len(service_winners) == 1:
            return service_winners[0]
        return "Software"
    return "Other"


def _empty_result():
    return dict(EMPTY_ENRICHMENT)


def _navigation_failure_type(error):
    """Return a concise category for an expected page-navigation failure."""
    if isinstance(error, TimeoutException):
        return "TIMEOUT"

    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if any(marker in message for marker in (
            "err_name_not_resolved", "name_not_resolved", "dns_probe",
            "no address associated with hostname", "temporary failure in name resolution",
            "nodename nor servname provided")):
        return "DNS"
    if any(marker in message for marker in (
            "err_cert_", "err_ssl_", "ssl error", "certificate error",
            "certificate verify failed")):
        return "SSL"
    if any(marker in message for marker in (
            "err_connection_", "connection refused", "connection reset",
            "connection aborted", "connection closed", "failed to establish a new connection",
            "network is unreachable")):
        return "CONNECTION"
    if ("webdriver" in name or "session" in name or "no such window" in message
            or "chrome not reachable" in message or "disconnected" in message):
        return "BROWSER"
    return "UNKNOWN_NAVIGATION"


def _record_navigation_failure(outcome, error):
    if outcome is None:
        return
    failure_type = _navigation_failure_type(error)
    outcome["had_navigation_failure"] = True
    current = outcome.get("failure_type", "")
    if (not current or FAILURE_TYPE_PRIORITY[failure_type]
            > FAILURE_TYPE_PRIORITY.get(current, -1)):
        outcome["failure_type"] = failure_type


def _candidate_urls(website_url):
    """Return normalized homepage and common context-page candidates."""
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


def _normalized_domain(url):
    domain = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def _deduplicate_candidates(candidates):
    deduplicated = []
    seen = set()
    for kind, url in candidates:
        normalized = urlunsplit(urlsplit(url)._replace(fragment=""))
        key = normalized.rstrip("/").casefold()
        if key not in seen:
            seen.add(key)
            deduplicated.append((kind, normalized))
    return deduplicated


def _discover_internal_pages(source, homepage_url, maximum=4):
    """Return a bounded set of same-domain about/service links from the homepage."""
    soup = _soup(source)
    homepage_domain = _normalized_domain(homepage_url)
    homepage_key = urlunsplit(
        urlsplit(homepage_url)._replace(fragment="")
    ).rstrip("/").casefold()
    discovered = []
    seen = set()
    per_kind = {"about": 0, "services": 0}
    for anchor in soup.select("a[href]"):
        href = normalize_whitespace(anchor.get("href", ""))
        if not href:
            continue
        url = urljoin(homepage_url, href)
        parsed = urlsplit(url)
        if (parsed.scheme.casefold() not in {"http", "https"}
                or _normalized_domain(url) != homepage_domain):
            continue

        label = normalize_whitespace(anchor.get_text(" ", strip=True))
        path_words = parsed.path.replace("-", " ").replace("_", " ")
        if SERVICE_LINK_MARKER.search(label):
            kind = "services"
        elif ABOUT_LINK_MARKER.search(label):
            kind = "about"
        elif SERVICE_LINK_MARKER.search(path_words):
            kind = "services"
        elif ABOUT_LINK_MARKER.search(path_words):
            kind = "about"
        else:
            continue
        if per_kind[kind] >= 2:
            continue

        normalized_url = urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, "")
        )
        key = normalized_url.rstrip("/").casefold()
        if key == homepage_key or key in seen:
            continue
        seen.add(key)
        discovered.append((kind, normalized_url))
        per_kind[kind] += 1
        if len(discovered) >= maximum:
            break
    return _deduplicate_candidates(discovered)


def _secondary_candidates(website_url, homepage_page):
    guessed = _candidate_urls(website_url)[1:]
    if homepage_page is None:
        return guessed

    discovered = _discover_internal_pages(
        homepage_page["source"],
        homepage_page["url"],
    )
    discovered_kinds = {kind for kind, url in discovered}
    fallback = [
        candidate for candidate in guessed
        if candidate[0] not in discovered_kinds
    ]
    return _deduplicate_candidates(discovered + fallback)


def _is_error_page(source, visible_text):
    soup = _soup(source)
    title = _meaningful_raw_text(soup.title)
    visible = normalize_whitespace(visible_text)
    if ERROR_PAGE_MARKER.search(title) or ERROR_PAGE_MARKER.search(visible):
        return True

    interstitial = (
        INTERSTITIAL_PAGE_MARKER.search(title)
        or INTERSTITIAL_PAGE_MARKER.search(visible)
    )
    if not interstitial:
        return False

    for meta in soup.find_all("meta"):
        key = normalize_whitespace(meta.get("name") or meta.get("property")).casefold()
        content = normalize_whitespace(meta.get("content", ""))
        if (key in {"description", "og:description"} and len(content) >= 40
                and not ERROR_PAGE_MARKER.search(content)
                and not INTERSTITIAL_PAGE_MARKER.search(content)):
            return False
    return True


def _has_structured_homepage_evidence(source):
    soup = _soup(source)
    title = _extract_title(soup)
    if title.casefold() not in {"", "home", "welcome", "index"}:
        return True
    if len(_extract_meta_description(soup)) >= 30:
        return True

    heading = soup.find("h1")
    if len(_meaningful_text(heading, minimum=8)) >= 8:
        return True
    for anchor in soup.select("a[href]"):
        linkedin_url = _normalize_linkedin_url(anchor.get("href", ""))
        if (linkedin_url
                and urlsplit(linkedin_url).path.casefold().startswith("/company/")):
            return True
    return False


def _has_useful_page_text(source, visible_text, is_homepage=False):
    if _is_error_page(source, visible_text):
        return False
    if is_homepage and _has_structured_homepage_evidence(source):
        return True
    if len(source or "") < 200:
        return False
    cleaned = normalize_whitespace(visible_text).casefold()
    for phrase in BOILERPLATE_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    words = findall(r"[^\W\d_]+", cleaned)
    return len(normalize_whitespace(cleaned)) >= 40 and len(words) >= 6


def _load_pages(driver, candidates, timeout, verbose, outcome=None):
    """Load useful candidate pages within time bounds and return their HTML."""
    pages = []
    original_handle = driver.current_window_handle
    candidate_timeout = max(1, timeout or 15)

    for kind, url in candidates:
        if outcome is not None:
            outcome["pages_attempted"] += 1
        temporary_handle = None
        candidate_loaded = False
        try:
            driver.switch_to.new_window("tab")
            temporary_handle = driver.current_window_handle
            deadline = monotonic() + candidate_timeout

            def remaining_timeout():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutException("company enrichment URL timeout exceeded")
                return remaining

            for attempt in range(2):
                driver.set_page_load_timeout(remaining_timeout())
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
                    WebDriverWait(
                        driver,
                        min(remaining_timeout(), 3),
                        poll_frequency=0.25,
                    ).until(dom_is_stable)
                except TimeoutException:
                    pass

                source = driver.page_source
                visible_text = driver.execute_script(
                    "return document.body ? document.body.innerText : '';"
                )
                if outcome is not None and not candidate_loaded:
                    outcome["pages_loaded"] += 1
                    candidate_loaded = True
                if _has_useful_page_text(
                    source,
                    visible_text,
                    is_homepage=kind == "homepage",
                ):
                    pages.append({
                        "kind": kind,
                        "url": driver.current_url,
                        "source": source,
                    })
                    break

                if verbose:
                    action = "retrying once" if attempt == 0 else "discarding snapshot"
                    print(f"[-] Company enrichment page had insufficient text: "
                          f"{url} ({action})")
        except TimeoutException as error:
            _record_navigation_failure(outcome, error)
            if verbose:
                print(f"[FAILED_TIMEOUT] Company enrichment URL timed out: {url} "
                      f"({type(error).__name__}: {error})")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except Exception as error:
            failure_type = _navigation_failure_type(error)
            _record_navigation_failure(outcome, error)
            if verbose:
                print(f"[FAILED_{failure_type}] Company enrichment URL failed: {url} "
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


def _load_company_pages(driver, website_url, timeout, verbose, outcome=None):
    candidates = _candidate_urls(website_url)
    if not candidates:
        return []

    load_options = (driver, candidates[:1], timeout, verbose)
    homepage_pages = (
        _load_pages(*load_options, outcome=outcome)
        if outcome is not None else _load_pages(*load_options)
    )
    homepage_page = homepage_pages[0] if homepage_pages else None
    secondary = _secondary_candidates(website_url, homepage_page)
    secondary_options = (driver, secondary, timeout, verbose)
    secondary_pages = (
        _load_pages(*secondary_options, outcome=outcome)
        if outcome is not None else _load_pages(*secondary_options)
    )
    return homepage_pages + secondary_pages


def _soup(source):
    try:
        return BeautifulSoup(source, "lxml")
    except Exception:
        return BeautifulSoup(source, "html.parser")


def _meaningful_raw_text(element):
    if element is None:
        return ""
    return normalize_whitespace(element.get_text(" ", strip=True))


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
    value = _meaningful_raw_text(element)
    if len(value) < minimum or is_content_noise(value):
        return ""
    return trim_text(value, maximum)


def _extract_title(homepage_soup):
    if homepage_soup is None or homepage_soup.title is None:
        return ""
    title = _meaningful_raw_text(homepage_soup.title)
    return "" if is_content_noise(title) else trim_text(title, 300)


def _extract_meta_description(homepage_soup):
    if homepage_soup is None:
        return ""
    fallback = ""
    for meta in homepage_soup.find_all("meta"):
        key = normalize_whitespace(meta.get("name") or meta.get("property")).casefold()
        content = trim_text(meta.get("content", ""), 500)
        if not content or is_content_noise(content):
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
    skip_contact_details = False
    for child in element.find_all(("h1", "h2", "h3", "p")):
        raw_value = _meaningful_raw_text(child)
        if is_contact_or_legal_text(raw_value):
            skip_contact_details = True
            continue
        if child.name in {"h1", "h2", "h3"}:
            skip_contact_details = False
        elif skip_contact_details:
            continue

        minimum = 20 if child.name == "p" else 3
        fragments = [
            normalize_whitespace(fragment)
            for fragment in split(r"(?<=[.!?])\s+", raw_value)
        ]
        value = " ".join(
            fragment for fragment in fragments
            if len(fragment) >= minimum and not is_content_noise(fragment)
        )
        if value:
            parts.append(trim_text(value, 1200))
    return " ".join(unique_text(parts))


def _extract_about(parsed_pages):
    """Select concise about/company text from parsed candidate pages."""
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
    if (not value or lowered in GENERIC_SERVICE_ITEMS or is_content_noise(value)
            or len(value) > 100 or len(value.split()) > 12):
        return ""
    return value


def _extract_services(parsed_pages):
    """Return distinct service labels supported by visible website content."""
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
    outcome = {
        "pages_attempted": 0,
        "pages_loaded": 0,
        "had_navigation_failure": False,
        "failure_type": "",
    }
    pages = _load_company_pages(
        driver, website_url, timeout, verbose, outcome=outcome,
    )
    if not pages:
        result = _empty_result()
        result["_meta"] = outcome
        return result

    homepage_page = next((page for page in pages if page["kind"] == "homepage"), None)
    homepage_soup = _soup(homepage_page["source"]) if homepage_page else None
    result = _empty_result()
    result["website_title"] = _extract_title(homepage_soup)
    result["website_meta_description"] = _extract_meta_description(homepage_soup)
    result["hero_text"] = _extract_hero(homepage_soup)
    result["about_text"] = _extract_about(pages)
    result["services"] = _extract_services(pages)
    result["linkedin_url"] = _extract_linkedin(pages)
    result["description"] = build_company_description(result)
    result["industry"] = classify_company_industry(result)
    result["_meta"] = outcome
    return result
