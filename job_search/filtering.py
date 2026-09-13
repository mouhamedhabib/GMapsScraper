"""Deterministic, versioned hard filtering for stored job postings.

This module deliberately has no network or model dependencies.  It evaluates only
the structured fields and text already stored in the job-search SQLite database.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Mapping
import unicodedata

from job_search.providers import (
    GENERIC_LISTING_REASON,
    classify_source_quality,
    generic_listing_reason,
    is_safe_location_value,
)
from job_search.geography import normalize_geography
from job_search.query_intent import evaluate_query_location
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


DEFAULT_POLICY_VERSION = "v1.1"
STATUSES = ("PASS", "REVIEW", "REJECT")


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass(frozen=True)
class ExperienceRequirement:
    minimum: int
    maximum: int | None
    preferred: bool
    text: str
    approximate: bool = False


@dataclass(frozen=True)
class FilterDecision:
    status: str
    primary_reason: str
    reasons: tuple[Reason, ...]
    matched_terms: dict[str, list[str]]
    experience_min_years: int | None
    experience_max_years: int | None
    remote_policy: str


RELEVANT_ROLE_PATTERNS = {
    "software developer": r"\bsoftware developer\b",
    "software engineer": r"\bsoftware engineer(?:\s+i)?\b",
    "full stack developer": r"\bfull[ -]?stack\b(?:\s+[().&+#\w-]+){0,5}\s+(?:developers?|engineers?)\b",
    "frontend developer": r"\bfront[ -]?end(?:\s+[.&+#\w-]+){0,3}\s+(?:developers?|engineers?)\b",
    "backend developer": r"\bback[ -]?end (?:developers?|engineers?)\b",
    "web developer": r"\bweb developers?\b",
    "technology developer": r"\b(?:ai|artificial intelligence|machine learning|ml|java|python|php|laravel|javascript|typescript|react(?:\.?js)?|next(?:\.js|js)|node(?:\.js|js)|nest(?:\.js|js)|fastapi|django|angular|\.net|dotnet) (?:developers?|engineers?)\b",
    "programmer": r"\b(?:(?:software|java|python|php|javascript|typescript) )?programmers?\b",
    "cloud developer": r"\bcloud developers?\b",
    "developpeur": r"\bdeveloppeu(?:r|se)s?\b",
    "developpeur logiciel": r"\bdeveloppeu(?:r|se)s? logiciel(?:le)?s?\b",
    "developpeur web": r"\bdeveloppeu(?:r|se)s? web\b",
    "developpeur full stack": r"\bdeveloppeu(?:r|se)s? full[ -]?stack\b",
    "developpeur backend": r"\bdeveloppeu(?:r|se)s? back[ -]?end\b",
    "developpeur frontend": r"\bdeveloppeu(?:r|se)s? front[ -]?end\b",
}

REJECT_ROLE_PATTERNS = (
    ("REJECT_ROLE_PRODUCT", "product/project-management role", r"\b(?:product manager|product owner|project manager|scrum master)\b"),
    ("REJECT_ROLE_CONSULTING", "consulting/value-engineering role", r"\b(?:consultant(?:e|\.e)?s?|consultant\(e\)|value engineer|pre[ -]?sales engineer)(?!\w)"),
    ("REJECT_ROLE_SALES", "sales/business-development role", r"\b(?:sales|account executive|business development|customer success)\b"),
    ("REJECT_ROLE_DESIGN", "design role", r"\b(?:ux|ui|graphic) designer\b"),
    ("REJECT_ROLE_NON_SOFTWARE", "non-software research/security role", r"\b(?:(?:ai|ml|machine learning) researcher|security analyst|soc analyst)\b"),
    ("REJECT_ROLE_QA", "QA/test role", r"\b(?:qa|quality assurance|test automation|automation test)\b"),
    ("REJECT_ROLE_DEVOPS", "DevOps role", r"\bdevops\b"),
    ("REJECT_ROLE_SRE", "site reliability role", r"\b(?:sre|site reliability)\b"),
    ("REJECT_ROLE_NETWORK", "network engineering role", r"\bnetwork engineer\b"),
    ("REJECT_ROLE_IT_SUPPORT", "IT support/helpdesk role", r"\b(?:it support|help[ -]?desk)\b"),
    ("REJECT_ROLE_DATA", "data science/analysis role", r"\bdata (?:scientist|analyst)\b"),
    ("REJECT_ROLE_ML_RESEARCH", "machine-learning research role", r"\bmachine learning researcher\b"),
    ("REJECT_ROLE_EMBEDDED", "embedded/firmware role", r"\b(?:embedded|firmware) engineer\b"),
    ("REJECT_ROLE_SAP", "SAP consulting role", r"\bsap (?:consultant|developer)\b"),
    ("REJECT_ROLE_SERVICENOW", "ServiceNow role", r"\bservicenow\b"),
    ("REJECT_ROLE_CYBERSECURITY", "cybersecurity analyst role", r"\bcyber ?security analyst\b"),
    ("REJECT_ROLE_SYSADMIN", "system administration role", r"\b(?:system|database) administrator\b"),
    ("REJECT_ROLE_MOBILE_ONLY", "mobile-only development role", r"\b(?:android|ios|mobile) (?:developer|engineer)\b"),
)

SENIORITY_PATTERN = re.compile(
    r"(?:\bsenior\b|\bsr\.?\b|\blead\b|\bstaff\b|\bprincipal\b|\barchitect\b|"
    r"\bhead of engineering\b|\bengineering manager\b|\bdirector\b|\bvp\b|\bchief\b)",
    re.I,
)
JUNIORITY_PATTERN = re.compile(
    r"\b(?:junior|graduate|entry[ -]?level|associate)\b|\b(?:software engineer|developer)\s+i\b",
    re.I,
)

TECHNOLOGY_PATTERNS = {
    "Python": r"\bpython\b", "FastAPI": r"\bfastapi\b", "Django": r"\bdjango\b",
    "JavaScript": r"\bjavascript\b", "TypeScript": r"\btypescript\b", "React": r"\breact(?:\.js|js)?\b",
    "Next.js": r"\bnext(?:\.js|js)\b", "Node.js": r"\bnode(?:\.js|js)\b", "NestJS": r"\bnest(?:\.js|js)\b",
    "PHP": r"\bphp\b", "Laravel": r"\blaravel\b", "Java": r"\bjava\b",
    "SQL": r"\bsql\b", "PostgreSQL": r"\bpostgres(?:ql)?\b", "MySQL": r"\bmysql\b",
    "MongoDB": r"\bmongodb\b", "Docker": r"\bdocker\b", "REST": r"\brest(?:ful)?\b",
    "OpenAPI": r"\bopenapi\b", "Git": r"\bgit\b",
}

STACK_MISMATCH_PATTERNS = (
    ("REJECT_STACK_EMBEDDED_CPP", "embedded C/C++ stack", r"\b(?:embedded|firmware)\b.*\bc\+\+(?!\w)|\bc\+\+(?!\w).*\b(?:embedded|firmware)\b"),
    ("REJECT_STACK_MAINFRAME_COBOL", "mainframe COBOL stack", r"\b(?:mainframe.*cobol|cobol.*mainframe)\b"),
    ("REJECT_STACK_SAP_ABAP", "SAP ABAP stack", r"\b(?:sap.*abap|abap.*sap)\b"),
    ("REJECT_STACK_SALESFORCE", "Salesforce-only stack", r"\bsalesforce(?:-only)? (?:developer|engineer)\b"),
    ("REJECT_STACK_SERVICENOW", "ServiceNow stack", r"\bservicenow (?:developer|engineer)\b"),
    ("REJECT_STACK_UNITY", "Unity game-development stack", r"\bunity (?:game )?(?:developer|engineer)\b"),
)

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
}
NUMBER_TOKEN = (
    r"(?:\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"un|une|deux|trois|quatre|cinq|sept|huit|neuf|dix)"
)
EXPERIENCE_RE = re.compile(
    rf"(?P<prefix>minimum(?: of)?|at least)?\s*(?P<min>{NUMBER_TOKEN})\s*"
    rf"(?:(?:-|–|—|to)\s*(?P<max>{NUMBER_TOKEN})\s*)?(?P<plus>\+)?\s*"
    r"years?(?:\s+of)?(?:\s+(?:professional|relevant|work|industry))?\s+experience|"
    rf"(?P<prefix2>minimum(?: of)?|at least)\s+(?P<min2>{NUMBER_TOKEN})\s+years?",
    re.I,
)
OPTIONAL_EXPERIENCE_RE = re.compile(
    rf"(?P<min>{NUMBER_TOKEN})\s*(?:(?:-|–|—|to)\s*(?P<max>{NUMBER_TOKEN})\s*)?"
    r"(?P<plus>\+)?\s*years?\s+(?=(?:preferred|desirable|would be (?:a )?plus|is (?:a )?bonus)\b)",
    re.I,
)
RANGE_OR_PLUS_EXPERIENCE_RE = re.compile(
    rf"(?P<min>{NUMBER_TOKEN})\s*(?:(?:-|–|—|to)\s*(?P<max>{NUMBER_TOKEN})|(?P<plus>\+))\s*years?",
    re.I,
)
FRENCH_EXPERIENCE_SUFFIX = r"d[\'\u2019\u02bc]exp[ée]rience(?:\s+professionnelle)?"
FRENCH_YEAR_TOKEN = r"(?:ann(?:[ée]e|ee)s?|ans?)\b"
FRENCH_RANGE_SEPARATOR = r"(?:-|–|—|à|a)"
FRENCH_LEADING_EXPERIENCE_RE = re.compile(
    rf"\bexp[ée]rience(?:\s+professionnelle)?\s+de\s+"
    rf"(?P<approx>environ\s+)?(?P<min>{NUMBER_TOKEN})\s*"
    rf"(?:{FRENCH_RANGE_SEPARATOR}\s*(?P<max>{NUMBER_TOKEN})\s*)?"
    rf"{FRENCH_YEAR_TOKEN}(?:\s+minimum)?",
    re.I,
)
FRENCH_POSSESSION_EXPERIENCE_RE = re.compile(
    rf"\bvous\s+(?:(?:justifiez|disposez)\s+(?:de\s+|d[\'\u2019\u02bc])|avez\s+)"
    rf"(?P<approx>environ\s+)?(?P<prefix>au\s+moins\s+)?(?P<min>{NUMBER_TOKEN})\s*"
    rf"(?:{FRENCH_RANGE_SEPARATOR}\s*(?P<max>{NUMBER_TOKEN})\s*)?"
    rf"{FRENCH_YEAR_TOKEN}(?:\s+{FRENCH_EXPERIENCE_SUFFIX})?(?:\s+minimum)?",
    re.I,
)
FRENCH_BETWEEN_EXPERIENCE_RE = re.compile(
    rf"\bentre\s+(?P<min>{NUMBER_TOKEN})\s+et\s+(?P<max>{NUMBER_TOKEN})\s+"
    rf"{FRENCH_YEAR_TOKEN}(?:\s+{FRENCH_EXPERIENCE_SUFFIX})?",
    re.I,
)
FRENCH_EXPERIENCE_RE = re.compile(
    rf"\b(?P<approx>environ\s+)?"
    rf"(?P<prefix>au\s+moins|un\s+minimum\s+de|minimum(?:\s+de)?)?\s*"
    rf"(?P<min>{NUMBER_TOKEN})\s*"
    rf"(?:{FRENCH_RANGE_SEPARATOR}\s*(?P<max>{NUMBER_TOKEN})\s*)?"
    rf"{FRENCH_YEAR_TOKEN}"
    rf"(?P<experience>\s+{FRENCH_EXPERIENCE_SUFFIX})?"
    rf"(?P<minimum_after>\s+minimum)?",
    re.I,
)
OPTIONAL_RE = re.compile(
    r"\b(?:preferred|nice to have|nice-to-have|bonus|desirable|a plus|"
    r"id[ée]alement|de pr[ée]f[ée]rence|souhait[ée]e?|serait un plus|un plus|"
    r"appr[ée]ci[ée]e?)\b",
    re.I,
)

WORLDWIDE_REMOTE_RE = re.compile(
    r"\bremote(?:ly)?\s*(?:[-,:/]\s*)?(?:worldwide|globally|"
    r"anywhere(?:\s+in the world|(?!\s+(?:in|within|across|throughout)\b))|"
    r"from anywhere(?:\s+in the world|(?!\s+(?:in|within|across|throughout)\b)))\b|"
    r"\b(?:worldwide|globally)\s*(?:[-,:/]\s*)?remote\b|"
    r"\bwork(?:ing)?\s+(?:remotely\s+)?from anywhere"
    r"(?:\s+in the world|(?!\s+(?:in|within|across|throughout)\b))\b|"
    r"\bhiring\s+worldwide\s+for\s+(?:a\s+)?remote\b|"
    r"\blocation\s*:\s*(?:remote\s*[-,/]\s*worldwide|worldwide\s*[-,/]\s*remote)\b",
    re.I,
)
CANDIDATES_WORLDWIDE_RE = re.compile(
    r"\b(?:open to|hiring)\s+(?:remote\s+)?candidates?\s+worldwide\b",
    re.I,
)
REMOTE_JOB_CONTEXT_RE = re.compile(
    r"\bremote\s+(?:role|position|job|work(?:ing)?|candidates?)\b|"
    r"\b(?:role|position|job|work)\s+(?:is\s+)?remote\b|"
    r"\bwork(?:ing)?\s+remotely\b|\bwork from home\b|\btelecommut",
    re.I,
)
STRUCTURED_WORLDWIDE_LOCATION_RE = re.compile(
    r"^(?:remote\s*[-,/]\s*)?(?:worldwide|anywhere(?:\s+in the world)?)(?:\s*[-,/]\s*remote)?$",
    re.I,
)


def _value(row: Mapping, name: str) -> str:
    try:
        value = row[name]
    except (KeyError, IndexError):
        value = ""
    return str(value or "").strip()


def _fold(value: str) -> str:
    """Case-fold and remove accents for matching without changing stored text."""
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(character for character in normalized if not unicodedata.combining(character)).casefold()


def _number(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else NUMBER_WORDS.get(value.casefold())


def _match_group(match: re.Match, name: str) -> str | None:
    """Return an optional named group shared by only some parser patterns."""
    try:
        return match.group(name)
    except IndexError:
        return None


def _sentence_context(text: str, start: int, end: int) -> str:
    sentence_start = max((text.rfind(mark, 0, start) for mark in ".;\n"), default=-1) + 1
    candidates = [text.find(mark, end) for mark in ".;\n"]
    candidates = [position for position in candidates if position >= 0]
    sentence_end = min(candidates) if candidates else min(len(text), end + 80)
    return text[sentence_start:sentence_end]


def extract_experience(text: str) -> tuple[ExperienceRequirement, ...]:
    """Extract explicit experience requirements, retaining optional context."""
    requirements = []
    text = text or ""
    matches: list[re.Match] = []
    patterns = (
        EXPERIENCE_RE,
        FRENCH_LEADING_EXPERIENCE_RE,
        FRENCH_POSSESSION_EXPERIENCE_RE,
        FRENCH_BETWEEN_EXPERIENCE_RE,
        FRENCH_EXPERIENCE_RE,
        OPTIONAL_EXPERIENCE_RE,
        RANGE_OR_PLUS_EXPERIENCE_RE,
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < existing.end() and existing.start() < match.end() for existing in matches):
                continue
            if pattern is FRENCH_EXPERIENCE_RE:
                context = _sentence_context(text, *match.span())
                has_clear_context = bool(
                    _match_group(match, "prefix")
                    or _match_group(match, "max")
                    or _match_group(match, "experience")
                    or _match_group(match, "minimum_after")
                    or OPTIONAL_RE.search(context)
                )
                if not has_clear_context:
                    continue
            matches.append(match)
    for match in sorted(matches, key=lambda item: item.start()):
        minimum = _number(_match_group(match, "min") or _match_group(match, "min2"))
        if minimum is None:
            continue
        maximum = _number(_match_group(match, "max"))
        start, end = match.span()
        # Optional qualifiers usually occur in the same sentence, commonly just
        # before or after the numeric requirement.
        context = _sentence_context(text, start, end)
        requirements.append(ExperienceRequirement(
            minimum=minimum,
            maximum=maximum,
            preferred=bool(OPTIONAL_RE.search(context)),
            text=" ".join(match.group(0).split()),
            approximate=bool(_match_group(match, "approx")),
        ))
    return tuple(requirements)


def extract_technologies(text: str) -> list[str]:
    return [name for name, pattern in TECHNOLOGY_PATTERNS.items() if re.search(pattern, text or "", re.I)]


def detect_remote_policy(row: Mapping) -> str:
    structured = _value(row, "remote_policy").upper()
    if structured in {"REMOTE", "HYBRID", "ONSITE"}:
        return structured
    text = " ".join((_value(row, "title"), _value(row, "location_text"), _value(row, "description")))
    if re.search(r"\bhybrid\b", text, re.I):
        return "HYBRID"
    if re.search(r"\b(?:remote|work from home|telecommut)\b", text, re.I):
        return "REMOTE"
    if re.search(r"\b(?:on[ -]?site|in[ -]?office)\b", text, re.I):
        return "ONSITE"
    return "UNKNOWN"


def has_worldwide_remote_eligibility(
    row: Mapping, *, title: str, description: str, location_text: str, location: str
) -> bool:
    """Return whether stored evidence explicitly permits worldwide remote work.

    Bare marketing or company-scope words such as ``worldwide`` and ``globally``
    are intentionally insufficient. Structured REMOTE data can establish the
    remote half of the claim when the structured location supplies its scope.
    """
    structured_remote = _value(row, "remote_policy").upper() == "REMOTE"
    if structured_remote and STRUCTURED_WORLDWIDE_LOCATION_RE.fullmatch(location_text):
        return True

    text = " ".join((title, location, description))
    return bool(
        WORLDWIDE_REMOTE_RE.search(text)
        or (
            CANDIDATES_WORLDWIDE_RE.search(text)
            and (
                structured_remote
                or REMOTE_JOB_CONTEXT_RE.search(text)
            )
        )
    )


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def evaluate_job(
    row: Mapping, now: datetime | None = None, source_queries: tuple[str, ...] = (),
) -> FilterDecision:
    """Evaluate one stored job using deterministic policy-v1.1 rules."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    title = _value(row, "title")
    folded_title = _fold(title)
    description = _value(row, "description")
    raw_location = _value(row, "location_text")
    raw_city = _value(row, "city")
    raw_country = _value(row, "country")
    location_text = raw_location if is_safe_location_value(raw_location) else ""
    city = raw_city if is_safe_location_value(raw_city, city=True) else ""
    country = raw_country if is_safe_location_value(raw_country) else ""
    location = " ".join(filter(None, (location_text, city, country))).strip()
    geography = normalize_geography(location_text, city, country, title)
    all_text = " ".join((title, description, location))
    relevant_roles = [name for name, pattern in RELEVANT_ROLE_PATTERNS.items() if re.search(pattern, folded_title, re.I)]
    technologies = extract_technologies(" ".join((title, description)))
    experience = extract_experience(description)
    remote_policy = detect_remote_policy(row)
    hard: list[Reason] = []
    review: list[Reason] = []
    passed: list[Reason] = []

    listing = generic_listing_reason(
        title, _value(row, "canonical_url"), description, page_fetched=True,
        has_structured_job_posting=_value(row, "status") == "OPEN",
    )
    if listing:
        hard.append(Reason(GENERIC_LISTING_REASON, "The stored URL/title represents a collection of jobs, not an individual posting."))

    sponsorship_available = bool(re.search(r"\b(?:visa sponsorship (?:is )?available|sponsorship available|we (?:can|will) sponsor|relocation (?:is )?(?:available|provided|possible))\b", all_text, re.I))
    explicitly_foreign_auth = re.search(
        r"\b(?:must (?:already )?(?:be )?authorized to work in (?:the )?(?:us|u\.s\.|united states|canada|eu|europe|france|belgium|switzerland|ireland|uk|united kingdom)|must have (?:existing|unrestricted) work authorization(?: in [^.;\n]+)?|(?:eu|us|u\.s\.) citizenship required)\b",
        all_text, re.I,
    )
    no_sponsorship = re.search(r"\b(?:no (?:visa )?sponsorship|we do not sponsor)\b", all_text, re.I)
    worldwide = has_worldwide_remote_eligibility(
        row, title=title, description=description,
        location_text=location_text, location=location,
    )
    query_location = evaluate_query_location(
        source_queries, geography, row, remote_policy=remote_policy,
        worldwide_eligible=worldwide,
    )
    if query_location.state == "MISMATCH":
        hard.append(Reason(
            "REJECT_QUERY_LOCATION_MISMATCH",
            "The known job geography contradicts every geographic discovery query.",
        ))
    elif query_location.state == "UNKNOWN":
        review.append(Reason(
            "REVIEW_QUERY_LOCATION_UNKNOWN",
            "The discovery query has geographic intent, but compatible job geography or remote eligibility is not established.",
        ))
    tunisian_location = geography.region == "TUNISIA"
    if (explicitly_foreign_auth or (no_sponsorship and not tunisian_location)) and not sponsorship_available:
        hard.append(Reason("REJECT_WORK_AUTHORIZATION", "The posting explicitly requires existing work authorization or offers no sponsorship."))

    us_only = re.search(r"\b(?:us|u\.s\.|united states)[ -]only\b|\bmust reside in (?:the )?(?:us|u\.s\.|united states)\b", all_text, re.I)
    us_location = geography.region == "UNITED_STATES"
    restricted_resident = re.search(r"\bcanada residents? only\b", all_text, re.I)
    if (us_only or us_location or restricted_resident) and not sponsorship_available and not worldwide:
        code = "REJECT_LOCATION_US_ONLY" if us_only or us_location else "REJECT_LOCATION_CANADA_ONLY"
        hard.append(Reason(code, "The posting explicitly restricts applicants to an incompatible location."))

    if SENIORITY_PATTERN.search(title):
        hard.append(Reason("REJECT_SENIORITY", "The job title explicitly identifies a senior or leadership role."))
    elif JUNIORITY_PATTERN.search(title):
        passed.append(Reason("PASS_JUNIOR_ROLE", "The title explicitly identifies a junior, graduate, entry-level, associate, or level-I role."))

    role_reject = None
    for code, label, pattern in REJECT_ROLE_PATTERNS:
        if re.search(pattern, title, re.I):
            role_reject = Reason(code, f"The title identifies this as a {label}.")
            break
    if role_reject and relevant_roles:
        review.append(Reason("REVIEW_ROLE_MIXED", "The title contains both target-role and excluded-role signals."))
    elif role_reject:
        hard.append(role_reject)
    elif relevant_roles:
        passed.append(Reason("PASS_RELEVANT_ROLE", "The title matches a target software-development role."))
    else:
        review.append(Reason("REVIEW_ROLE_UNCLEAR", "The title does not clearly match a target or excluded role family."))

    mandatory = [item for item in experience if not item.preferred]
    optional = [item for item in experience if item.preferred]
    if any(item.minimum >= 4 for item in mandatory):
        years = max(item.minimum for item in mandatory)
        suffix = "5_PLUS" if years >= 5 else "4_PLUS"
        hard.append(Reason(f"REJECT_EXPERIENCE_{suffix}", f"The role requires at least {years} years of experience."))
    elif any(item.minimum == 3 for item in mandatory):
        review.append(Reason("REVIEW_EXPERIENCE_3_YEARS", "The role requires at least 3 years of experience."))
    elif mandatory and min(item.minimum for item in mandatory) <= 2:
        passed.append(Reason("PASS_EXPERIENCE_0_2", "The stated mandatory experience minimum is no more than 2 years."))
    if optional and any(item.minimum >= 4 for item in optional):
        review.append(Reason("REVIEW_EXPERIENCE_PREFERRED", "Higher experience is stated only as preferred, not mandatory."))

    tunisia = tunisian_location or re.search(r"remote (?:from|in) tunisia", all_text, re.I)
    europe = geography.region == "EUROPE" or bool(re.search(
        r"\b(?:europe|eu remote|remote (?:in|within|across) (?:the )?eu)\b",
        " ".join((title, location)), re.I,
    )) or bool(re.search(r"\bremote (?:in|within|across) (?:the )?(?:eu|europe)\b|\b(?:eu|europe)[ -]remote\b", description, re.I))
    preferred_country = geography.country in {
        "France", "Belgium", "Switzerland", "Ireland", "United Kingdom", "Canada",
    }
    relocation = re.search(r"\brelocation (?:is )?(?:available|provided|possible|offered)\b", all_text, re.I)
    if worldwide:
        passed.append(Reason("PASS_REMOTE_WORLDWIDE", "The posting explicitly allows worldwide remote work."))
    elif tunisia:
        passed.append(Reason("PASS_LOCATION_TUNISIA", "The posting is located in Tunisia or explicitly permits remote work from Tunisia."))
    elif preferred_country:
        review.append(Reason("REVIEW_LOCATION_PREFERRED_MARKET", "The posting is in a preferred market but eligibility is not established."))
    elif europe:
        review.append(Reason("REVIEW_LOCATION_EUROPE", "The posting is in Europe or explicitly targets Europe/EU remote work, so authorization needs review."))
    elif relocation:
        review.append(Reason("REVIEW_RELOCATION_POSSIBLE", "The posting indicates that relocation may be possible."))
    elif not location and query_location.state != "UNKNOWN":
        review.append(Reason("REVIEW_LOCATION_UNKNOWN", "No usable location was stored for this posting."))

    stack_reject = None
    for code, label, pattern in STACK_MISMATCH_PATTERNS:
        if re.search(pattern, title, re.I):
            stack_reject = Reason(code, f"The title identifies a clearly unrelated {label}.")
            break
    if stack_reject:
        # A title that also clearly names a target web/software role is mixed,
        # so ambiguity wins over a hard stack rejection.
        if relevant_roles:
            review.append(Reason("REVIEW_STACK_MIXED", "The title contains both target-role and unrelated-stack signals."))
        else:
            hard.append(stack_reject)
    elif re.search(r"(?:\bc\+\+(?!\w)|\b(?:cobol|abap|salesforce|servicenow|unity)\b)", title, re.I) and relevant_roles:
        review.append(Reason("REVIEW_STACK_MIXED", "The title contains a target role with a potentially unrelated stack."))

    if not description:
        review.append(Reason("REVIEW_DESCRIPTION_MISSING", "The stored job description is missing."))

    published = _date(_value(row, "published_at"))
    if published:
        if published > now:
            review.append(Reason("REVIEW_INVALID_PUBLISHED_DATE", "The published date is in the future and was not corrected."))
        else:
            age_days = (now - published).days
            if age_days > 60:
                hard.append(Reason("REJECT_STALE_JOB", "The posting is over 60 days old."))
            elif age_days > 30:
                review.append(Reason("REVIEW_OLD_JOB", "The posting is 31 to 60 days old."))
            elif age_days <= 14:
                passed.append(Reason("PASS_FRESH_JOB", "The posting is no more than 14 days old."))

    # Preserve exact precedence for the primary reason regardless of discovery order.
    precedence = {
        "REJECT_WORK_AUTHORIZATION": 0,
        "REJECT_ROLE_PRODUCT": 1, "REJECT_ROLE_SALES": 1,
        "REJECT_ROLE_DESIGN": 1, "REJECT_ROLE_DATA": 1,
        "REJECT_ROLE_CONSULTING": 1, "REJECT_ROLE_NON_SOFTWARE": 1,
        "REJECT_ROLE_QA": 1, "REJECT_ROLE_DEVOPS": 1, "REJECT_ROLE_SRE": 1,
        "REJECT_ROLE_NETWORK": 1, "REJECT_ROLE_IT_SUPPORT": 1,
        "REJECT_ROLE_ML_RESEARCH": 1, "REJECT_ROLE_EMBEDDED": 1, "REJECT_ROLE_SAP": 1,
        "REJECT_ROLE_SERVICENOW": 1, "REJECT_ROLE_CYBERSECURITY": 1,
        "REJECT_ROLE_SYSADMIN": 1, "REJECT_ROLE_MOBILE_ONLY": 1,
        "REJECT_STACK_EMBEDDED_CPP": 1, "REJECT_STACK_MAINFRAME_COBOL": 1,
        "REJECT_STACK_SAP_ABAP": 1, "REJECT_STACK_SALESFORCE": 1,
        "REJECT_STACK_SERVICENOW": 1, "REJECT_STACK_UNITY": 1,
        "REJECT_SENIORITY": 2,
        "REJECT_EXPERIENCE_5_PLUS": 3,
        "REJECT_EXPERIENCE_4_PLUS": 3,
        "REJECT_STALE_JOB": 4,
        "REJECT_LOCATION_US_ONLY": 5,
        "REJECT_LOCATION_CANADA_ONLY": 5,
        "REJECT_QUERY_LOCATION_MISMATCH": 6,
        GENERIC_LISTING_REASON: -1,
    }
    hard.sort(key=lambda reason: precedence.get(reason.code, 99))
    reasons = hard + review + passed
    if hard:
        status = "REJECT"
    elif review:
        status = "REVIEW"
    else:
        status = "PASS"
    if not reasons:
        reasons = [Reason("PASS_NO_HARD_FILTER", "No hard rejection or review rule matched.")]

    minimums = [item.minimum for item in experience]
    maximums = [item.maximum for item in experience if item.maximum is not None]
    matched_terms = {
        "roles": relevant_roles,
        "technologies": technologies,
        "experience": [item.text for item in experience],
        "optional_experience": [item.text for item in optional],
        "experience_evidence": [
            {
                "experience_min_years": item.minimum,
                "experience_max_years": item.maximum,
                "experience_text": item.text,
                "preferred": item.preferred,
                "approximate": item.approximate,
                "source": "description",
            }
            for item in experience
        ],
        "source_quality": [classify_source_quality(
            _value(row, "canonical_url"), _value(row, "source_provider") or _value(row, "provider")
        )],
        "normalized_country": [geography.country] if geography.country else [],
        "location_region": [geography.region],
        "source_queries": [intent.source_query for intent in query_location.intents],
        "query_intents": [intent.as_dict() for intent in query_location.intents],
        "job_geography": [{"country": geography.country, "region": geography.region}],
        "query_location_match": [query_location.state],
        "matched_source_query": (
            [query_location.matched_source_query]
            if query_location.matched_source_query else []
        ),
    }
    return FilterDecision(
        status=status,
        primary_reason=reasons[0].code,
        reasons=tuple(reasons),
        matched_terms=matched_terms,
        experience_min_years=min(minimums) if minimums else None,
        experience_max_years=max(maximums) if maximums else None,
        remote_policy=remote_policy,
    )


def persist_result(connection: sqlite3.Connection, job_id: int, policy_version: str, decision: FilterDecision, evaluated_at: str | None = None) -> None:
    evaluated_at = evaluated_at or utc_now()
    reasons_json = json.dumps([{"code": item.code, "message": item.message} for item in decision.reasons], ensure_ascii=False, separators=(",", ":"))
    terms_json = json.dumps(decision.matched_terms, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_filter_results
           (job_id, policy_version, evaluated_at, status, primary_reason,
            reasons_json, matched_terms_json, experience_min_years,
            experience_max_years, detected_remote_policy, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id, policy_version) DO UPDATE SET
             evaluated_at=excluded.evaluated_at, status=excluded.status,
             primary_reason=excluded.primary_reason, reasons_json=excluded.reasons_json,
             matched_terms_json=excluded.matched_terms_json,
             experience_min_years=excluded.experience_min_years,
             experience_max_years=excluded.experience_max_years,
             detected_remote_policy=excluded.detected_remote_policy,
             updated_at=excluded.updated_at""",
        (job_id, policy_version, evaluated_at, decision.status, decision.primary_reason,
         reasons_json, terms_json, decision.experience_min_years,
         decision.experience_max_years, decision.remote_policy, evaluated_at, evaluated_at),
    )


def filter_stored_jobs(connection: sqlite3.Connection, policy_version: str = DEFAULT_POLICY_VERSION, rebuild: bool = False, limit: int | None = None, verbose: bool = False, job_ids: list[int] | tuple[int, ...] | None = None) -> dict:
    """Evaluate pending jobs (or all jobs on rebuild) and persist results."""
    clauses = []
    parameters: list[object] = []
    if not rebuild:
        clauses.append("NOT EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id=j.job_id AND f.policy_version=?)")
        parameters.append(policy_version)
    if job_ids is not None:
        if not job_ids:
            return {"evaluated": 0, "counts": Counter(), "reasons": {"REJECT": Counter(), "REVIEW": Counter()}}
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"j.job_id IN ({placeholders})")
        parameters.extend(job_ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""SELECT j.*, c.canonical_name AS company_name,
                       (SELECT s.provider FROM job_sources s WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1) AS source_provider
                FROM jobs j LEFT JOIN companies c ON c.company_id=j.company_id
                {where} ORDER BY j.job_id"""
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    rows = connection.execute(query, parameters).fetchall()
    queries_by_job: dict[int, list[str]] = {}
    if rows:
        row_ids = [int(row["job_id"]) for row in rows]
        query_placeholders = ",".join("?" for _ in row_ids)
        for source_query in connection.execute(
            f"""SELECT job_id, source_query FROM job_source_queries
                WHERE job_id IN ({query_placeholders})
                ORDER BY job_id, job_source_query_id""",
            row_ids,
        ):
            queries_by_job.setdefault(int(source_query["job_id"]), []).append(
                source_query["source_query"]
            )
    counts = Counter()
    reason_counts = {"REJECT": Counter(), "REVIEW": Counter()}
    evaluated_at = utc_now()
    with connection:
        for row in rows:
            decision = evaluate_job(
                row, source_queries=tuple(queries_by_job.get(int(row["job_id"]), ()))
            )
            persist_result(connection, row["job_id"], policy_version, decision, evaluated_at)
            counts[decision.status] += 1
            if decision.status in reason_counts:
                reason_counts[decision.status][decision.primary_reason] += 1
            if verbose:
                print(f'{row["job_id"]}: {decision.status} {decision.primary_reason} - {row["title"] or "(untitled)"}')
                state = decision.matched_terms.get("query_location_match", ["NEUTRAL"])[0]
                if state != "NEUTRAL":
                    matched = decision.matched_terms.get("matched_source_query", [])
                    print(
                        f"  query_location_match={state}; "
                        f"job_geography={decision.matched_terms['job_geography'][0]}; "
                        f"matched_source_query={matched[0] if matched else 'none'}"
                    )
    return {"evaluated": len(rows), "counts": counts, "reasons": reason_counts}


def print_summary(summary: dict) -> None:
    print(f"Jobs evaluated: {summary['evaluated']}")
    for status in STATUSES:
        print(f"{status}: {summary['counts'][status]}")
    for status in ("REJECT", "REVIEW"):
        print(f"Top {status.lower()} reasons:")
        reasons = summary["reasons"][status].most_common(5)
        if not reasons:
            print("  (none)")
        for code, count in reasons:
            print(f"  {code}: {count}")


def show_results(connection: sqlite3.Connection, statuses: tuple[str, ...], policy_version: str) -> None:
    placeholders = ",".join("?" for _ in statuses)
    rows = connection.execute(
        f"""SELECT j.job_id, j.title, c.canonical_name AS company, j.location_text,
                   j.published_at, j.canonical_url,
                   (SELECT s.provider FROM job_sources s WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1) provider,
                   (SELECT s.source_type FROM job_sources s WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1) source_type,
                   (SELECT s.employer_relationship FROM job_sources s WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1) employer_relationship,
                   f.status, f.reasons_json, f.matched_terms_json
            FROM job_filter_results f JOIN jobs j ON j.job_id=f.job_id
            LEFT JOIN companies c ON c.company_id=j.company_id
            WHERE f.policy_version=? AND f.status IN ({placeholders})
            ORDER BY CASE f.status WHEN 'PASS' THEN 0 ELSE 1 END, j.job_id""",
        (policy_version, *statuses),
    ).fetchall()
    if not rows:
        print("No matching filter results.")
        return
    for row in rows:
        terms = json.loads(row["matched_terms_json"])
        reasons = json.loads(row["reasons_json"])
        stored_location = row["location_text"] or ""
        display_location = stored_location if is_safe_location_value(stored_location) else ""
        print(f"job_id: {row['job_id']}")
        print(f"title: {row['title'] or ''}")
        print(f"company: {row['company'] or ''}")
        print(f"location: {display_location}")
        print(f"provider: {row['provider'] or ''}")
        print(f"source type: {row['source_type'] or 'UNKNOWN'}")
        print(f"employer relationship: {row['employer_relationship'] or 'UNKNOWN'}")
        print(f"source quality: {', '.join(terms.get('source_quality', ['UNKNOWN']))}")
        print(f"published_at: {row['published_at'] or ''}")
        print(f"matched technologies: {', '.join(terms.get('technologies', []))}")
        print(f"query location match: {', '.join(terms.get('query_location_match', ['NEUTRAL']))}")
        print(f"matched source query: {', '.join(terms.get('matched_source_query', [])) or 'none'}")
        print(f"filter reasons: {', '.join(item['code'] for item in reasons)}")
        print(f"job URL: {row['canonical_url']}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministically filter stored jobs")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)
    parser.add_argument("--rebuild", action="store_true", help="Re-evaluate all jobs for this policy")
    parser.add_argument("--limit", type=int, help="Maximum jobs to evaluate")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show-pass", action="store_true", help="Read-only display of PASS results")
    parser.add_argument("--show-review", action="store_true", help="Read-only display of REVIEW results")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    connection = connect_database(args.database)
    try:
        shown = tuple(status for enabled, status in ((args.show_pass, "PASS"), (args.show_review, "REVIEW")) if enabled)
        if shown:
            show_results(connection, shown, args.policy_version)
        else:
            print_summary(filter_stored_jobs(connection, args.policy_version, args.rebuild, args.limit, args.verbose))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
