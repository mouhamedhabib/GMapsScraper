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
)
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
    "full stack developer": r"\bfull[ -]?stack\b(?:\s+[.&+#\w-]+){0,5}\s+(?:developers?|engineers?)\b",
    "frontend developer": r"\bfront[ -]?end (?:developers?|engineers?)\b",
    "backend developer": r"\bback[ -]?end (?:developers?|engineers?)\b",
    "web developer": r"\bweb developers?\b",
    "technology developer": r"\b(?:java|python|php|laravel|javascript|typescript|react(?:\.js)?|next(?:\.js|js)|node(?:\.js|js)|nest(?:\.js|js)|fastapi|django|angular|\.net|dotnet) (?:developers?|engineers?)\b",
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
    ("REJECT_ROLE_CONSULTING", "consulting/value-engineering role", r"\b(?:value engineer|solutions? consultant|pre[ -]?sales engineer)\b"),
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
}
NUMBER_TOKEN = r"(?:\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
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
OPTIONAL_RE = re.compile(r"\b(?:preferred|nice to have|nice-to-have|bonus|desirable|a plus)\b", re.I)


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


def extract_experience(text: str) -> tuple[ExperienceRequirement, ...]:
    """Extract explicit experience requirements, retaining optional context."""
    requirements = []
    matches = list(EXPERIENCE_RE.finditer(text or ""))
    for pattern in (OPTIONAL_EXPERIENCE_RE, RANGE_OR_PLUS_EXPERIENCE_RE):
        matches.extend(
            match for match in pattern.finditer(text or "")
            if not any(match.start() < existing.end() and existing.start() < match.end() for existing in matches)
        )
    for match in sorted(matches, key=lambda item: item.start()):
        minimum = _number(match.group("min") or match.group("min2"))
        if minimum is None:
            continue
        maximum = _number(match.group("max"))
        start, end = match.span()
        # Optional qualifiers usually occur in the same sentence, commonly just
        # before or after the numeric requirement.
        sentence_start = max((text.rfind(mark, 0, start) for mark in ".;\n"), default=-1) + 1
        candidates = [text.find(mark, end) for mark in ".;\n"]
        candidates = [position for position in candidates if position >= 0]
        sentence_end = min(candidates) if candidates else min(len(text), end + 80)
        context = text[sentence_start:sentence_end]
        requirements.append(ExperienceRequirement(
            minimum=minimum,
            maximum=maximum,
            preferred=bool(OPTIONAL_RE.search(context)),
            text=" ".join(match.group(0).split()),
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


def evaluate_job(row: Mapping, now: datetime | None = None) -> FilterDecision:
    """Evaluate one stored job using deterministic policy-v1.1 rules."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    title = _value(row, "title")
    folded_title = _fold(title)
    description = _value(row, "description")
    country = _value(row, "country")
    location = " ".join((_value(row, "location_text"), _value(row, "city"), country)).strip()
    all_text = " ".join((title, description, location))
    relevant_roles = [name for name, pattern in RELEVANT_ROLE_PATTERNS.items() if re.search(pattern, folded_title, re.I)]
    technologies = extract_technologies(" ".join((title, description)))
    experience = extract_experience(description)
    remote_policy = detect_remote_policy(row)
    hard: list[Reason] = []
    review: list[Reason] = []
    passed: list[Reason] = []

    listing = generic_listing_reason(title, _value(row, "canonical_url"), description)
    if listing:
        hard.append(Reason(GENERIC_LISTING_REASON, "The stored URL/title represents a collection of jobs, not an individual posting."))

    sponsorship_available = bool(re.search(r"\b(?:visa sponsorship (?:is )?available|sponsorship available|we (?:can|will) sponsor|relocation (?:is )?(?:available|provided|possible))\b", all_text, re.I))
    explicitly_foreign_auth = re.search(
        r"\b(?:must (?:already )?(?:be )?authorized to work in (?:the )?(?:us|u\.s\.|united states|canada|eu|europe|france|belgium|switzerland|ireland|uk|united kingdom)|must have (?:existing|unrestricted) work authorization(?: in [^.;\n]+)?|(?:eu|us|u\.s\.) citizenship required)\b",
        all_text, re.I,
    )
    no_sponsorship = re.search(r"\b(?:no (?:visa )?sponsorship|we do not sponsor)\b", all_text, re.I)
    tunisian_location = bool(re.search(r"\b(?:tunisia|tunisian|tunis)\b", location, re.I) or country.casefold() in {"tn", "tun"})
    if (explicitly_foreign_auth or (no_sponsorship and not tunisian_location)) and not sponsorship_available:
        hard.append(Reason("REJECT_WORK_AUTHORIZATION", "The posting explicitly requires existing work authorization or offers no sponsorship."))

    us_only = re.search(r"\b(?:us|u\.s\.|united states)[ -]only\b|\bmust reside in (?:the )?(?:us|u\.s\.|united states)\b", all_text, re.I)
    restricted_resident = re.search(r"\bcanada residents? only\b", all_text, re.I)
    if (us_only or restricted_resident) and not sponsorship_available:
        code = "REJECT_LOCATION_US_ONLY" if us_only else "REJECT_LOCATION_CANADA_ONLY"
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

    worldwide = re.search(r"\b(?:worldwide|work from anywhere|anywhere in the world|global(?:ly)? remote)\b", all_text, re.I)
    tunisia = tunisian_location or re.search(r"remote (?:from|in) tunisia", all_text, re.I)
    europe = re.search(r"\b(?:europe|eu remote|remote (?:in|within|across) (?:the )?eu)\b", all_text, re.I)
    preferred_country = re.search(r"\b(?:france|belgium|switzerland|ireland|united kingdom|uk|canada)\b", location, re.I) or country.casefold() in {"fr", "fra", "be", "bel", "ch", "che", "ie", "irl", "gb", "gbr", "ca", "can"}
    relocation = re.search(r"\brelocation (?:is )?(?:available|provided|possible|offered)\b", all_text, re.I)
    if worldwide:
        passed.append(Reason("PASS_REMOTE_WORLDWIDE", "The posting explicitly allows worldwide remote work."))
    elif tunisia:
        passed.append(Reason("PASS_LOCATION_TUNISIA", "The posting is located in Tunisia or explicitly permits remote work from Tunisia."))
    elif europe:
        review.append(Reason("REVIEW_LOCATION_EUROPE", "The posting is Europe/EU remote and work authorization needs review."))
    elif preferred_country:
        review.append(Reason("REVIEW_LOCATION_PREFERRED_MARKET", "The posting is in a preferred market but eligibility is not established."))
    elif relocation:
        review.append(Reason("REVIEW_RELOCATION_POSSIBLE", "The posting indicates that relocation may be possible."))
    elif not location:
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
        "source_quality": [classify_source_quality(
            _value(row, "canonical_url"), _value(row, "source_provider") or _value(row, "provider")
        )],
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


def filter_stored_jobs(connection: sqlite3.Connection, policy_version: str = DEFAULT_POLICY_VERSION, rebuild: bool = False, limit: int | None = None, verbose: bool = False) -> dict:
    """Evaluate pending jobs (or all jobs on rebuild) and persist results."""
    where = "" if rebuild else "WHERE NOT EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id=j.job_id AND f.policy_version=?)"
    parameters: list[object] = [] if rebuild else [policy_version]
    query = f"""SELECT j.*, c.canonical_name AS company_name,
                       (SELECT s.provider FROM job_sources s WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1) AS source_provider
                FROM jobs j LEFT JOIN companies c ON c.company_id=j.company_id
                {where} ORDER BY j.job_id"""
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    rows = connection.execute(query, parameters).fetchall()
    counts = Counter()
    reason_counts = {"REJECT": Counter(), "REVIEW": Counter()}
    evaluated_at = utc_now()
    with connection:
        for row in rows:
            decision = evaluate_job(row)
            persist_result(connection, row["job_id"], policy_version, decision, evaluated_at)
            counts[decision.status] += 1
            if decision.status in reason_counts:
                reason_counts[decision.status][decision.primary_reason] += 1
            if verbose:
                print(f'{row["job_id"]}: {decision.status} {decision.primary_reason} - {row["title"] or "(untitled)"}')
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
        print(f"job_id: {row['job_id']}")
        print(f"title: {row['title'] or ''}")
        print(f"company: {row['company'] or ''}")
        print(f"location: {row['location_text'] or ''}")
        print(f"provider: {row['provider'] or ''}")
        print(f"source quality: {', '.join(terms.get('source_quality', ['UNKNOWN']))}")
        print(f"published_at: {row['published_at'] or ''}")
        print(f"matched technologies: {', '.join(terms.get('technologies', []))}")
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
