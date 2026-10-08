"""Bounded, deterministic discovery of public professional contacts.

The stage discovers people only. It never derives email addresses, performs
outreach, or changes qualification state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping, Sequence
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

from job_search.relationship_resolution import normalize_company_identity
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


DISCOVERY_POLICY_VERSION = "contact-discovery-v2"
MAX_FIRST_PARTY_PAGES = 5
FIRST_PARTY_PATHS = (
    "/team", "/about/team", "/leadership", "/careers", "/people",
)
SELECTION_STATUSES = (
    "SELECTED_PRIMARY", "SELECTED_BACKUP", "REJECTED_ROLE_MISMATCH",
    "REJECTED_COMPANY_MISMATCH", "REJECTED_FORMER_EMPLOYEE",
    "REJECTED_LOW_CONFIDENCE",
)
SOURCE_RANK = {
    "JOB_POSTING": 0, "OFFICIAL_COMPANY": 1, "LINKEDIN": 2,
    "GITHUB": 3, "OTHER_PROFESSIONAL": 4,
}
ROLE_PATTERNS = {
    "Engineering Manager": re.compile(
        r"\b(?:software\s+)?engineering manager\b|\bengineering lead\b|"
        r"\bhead of engineering\b|\bdirector of engineering\b", re.I,
    ),
    "Technical Recruiter": re.compile(
        r"\btechnical recruiter\b|\bengineering recruiter\b|"
        r"\btalent acquisition partner\s*[-–—:]?\s*engineering\b|\btech talent partner\b", re.I,
    ),
    "Talent Acquisition - Engineering": re.compile(
        r"\btalent acquisition\b.*\b(?:engineering|technical|technology)\b", re.I,
    ),
    "Talent Acquisition Partner": re.compile(r"\btalent acquisition partner\b", re.I),
    "Early Careers Recruiter": re.compile(
        r"\bearly careers recruiter\b|\bcampus recruiter\b|\bgraduate recruiter\b|"
        r"\buniversity recruiter\b", re.I,
    ),
    "Campus Recruiter": re.compile(
        r"\bcampus recruiter\b|\buniversity recruiter\b|\bearly careers recruiter\b", re.I,
    ),
    "Head of Engineering": re.compile(
        r"\bhead of engineering\b|\bdirector of engineering\b|\bvp(?: of)? engineering\b", re.I,
    ),
    "CTO": re.compile(r"\bchief technology officer\b|\bCTO\b", re.I),
    "VP Engineering": re.compile(r"\b(?:vice president|vp)(?: of)? engineering\b", re.I),
    "Technical Founder": re.compile(r"\btechnical (?:co-)?founder\b|\bfounder\b.*\bengineer", re.I),
    "EXPLICIT_CONTACT": re.compile(r".+", re.S),
}
GENERIC_HR = re.compile(r"\b(?:human resources|hr director|hr manager|people director)\b", re.I)
FORMER = re.compile(r"\b(?:former|formerly|ex[- ]|previously|until\s+20\d\d|left\s+)\b", re.I)
NAME = r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+){1,3}"


@dataclass(frozen=True)
class PersonCandidate:
    person_name: str
    current_title: str
    company: str
    target_role_category: str
    source_url: str
    source_type: str
    relationship_to_job: str
    confidence: str
    selection_status: str
    reason_codes: tuple[str, ...]
    evidence: tuple[dict, ...]
    discovery_query: str
    person_key: str = ""
    candidate_id: int | None = None


@dataclass(frozen=True)
class ContactDiscoveryResult:
    job_id: int
    company: str
    status: str
    primary: PersonCandidate | None
    backup: PersonCandidate | None
    candidates: tuple[PersonCandidate, ...]
    search_queries_used: int
    first_party_pages_inspected: int
    people_inspected: int
    input_fingerprint: str
    discovered_at: str
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _canonical_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), "", ""))


def _source_type(url: str, company_domain: str) -> str:
    domain = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    if company_domain and (domain == company_domain or domain.endswith("." + company_domain)):
        return "OFFICIAL_COMPANY"
    if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
        return "LINKEDIN"
    if domain == "github.com" or domain.endswith(".github.com"):
        return "GITHUB"
    return "OTHER_PROFESSIONAL"


def _person_key(name: str, company: str, url: str) -> str:
    payload = "\0".join((
        " ".join(name.casefold().split()), normalize_company_identity(company), _canonical_url(url),
    ))
    return sha256(payload.encode()).hexdigest()


def _explicit_contacts(row: Mapping) -> list[dict]:
    text = " ".join(_value(row, "description").split())
    patterns = (
        re.compile(
            rf"\b(?i:recruiter|hiring contact|talent contact)\s*:\s*(?P<name>{NAME})"
            r"(?:\s*[,–—-]\s*(?P<title>[^.;]{3,80}))?"
        ),
        re.compile(
            rf"\b(?i:contact|reach out to)\s+(?P<name>{NAME})"
            r"(?:\s*[,–—-]\s*(?P<title>[^.;]{3,80}))?"
        ),
    )
    contacts = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            title = (match.groupdict().get("title") or "Hiring Contact").strip()
            contacts.append({
                "person_name": match.group("name"), "current_title": title,
                "company": _value(row, "company"), "source_url": _value(row, "canonical_url"),
                "source_type": "JOB_POSTING", "result_snippet": match.group(0),
                "discovery_query": "",
            })
    return contacts


def _parse_search_result(result: Mapping, company: str, company_domain: str, query: str) -> dict | None:
    title = _value(result, "title")
    snippet = _value(result, "result_snippet") or _value(result, "snippet")
    url = _canonical_url(_value(result, "url") or _value(result, "source_url"))
    if not title or not url:
        return None
    domain = (urlsplit(url).hostname or "").casefold()
    if "google." in domain or _value(result, "resolution_error"):
        return None
    path_parts = [part for part in urlsplit(url).path.split("/") if part]
    if (domain == "linkedin.com" or domain.endswith(".linkedin.com")) and (
        len(path_parts) < 2 or path_parts[0].casefold() != "in"
    ):
        return None
    if (domain == "github.com" or domain.endswith(".github.com")) and len(path_parts) != 1:
        return None
    parts = [part.strip() for part in re.split(r"\s+(?:\||[-–—])\s+", title) if part.strip()]
    name = _value(result, "person_name")
    current_title = _value(result, "current_title")
    result_company = _value(result, "company")
    if not name and parts and re.fullmatch(NAME, parts[0]):
        name = parts[0]
    visible = " ".join((title, snippet))
    company_match = normalize_company_identity(company) in normalize_company_identity(visible)
    if not result_company and company_match:
        result_company = company
    if not current_title:
        for part in parts[1:]:
            if normalize_company_identity(company) not in normalize_company_identity(part) and not re.search(r"\blinkedin\b", part, re.I):
                current_title = part
                break
    if not name or not current_title or not result_company:
        return None
    return {
        "person_name": name, "current_title": current_title, "company": result_company,
        "source_url": url, "source_type": _value(result, "source_type") or _source_type(url, company_domain),
        "result_snippet": snippet, "result_title": title, "discovery_query": query,
    }


def _region_match(row: Mapping, visible: str) -> bool:
    fields = (_value(row, "city"), _value(row, "country"), _value(row, "region"))
    folded = visible.casefold()
    return any(value and value.casefold() in folded for value in fields)


def evaluate_person(row: Mapping, raw: Mapping, target_roles: Sequence[str]) -> PersonCandidate:
    name = _value(raw, "person_name")
    title = _value(raw, "current_title")
    company = _value(raw, "company")
    url = _canonical_url(_value(raw, "source_url") or _value(raw, "url"))
    source_type = _value(raw, "source_type") or "OTHER_PROFESSIONAL"
    snippet = _value(raw, "result_snippet") or _value(raw, "snippet")
    query = _value(raw, "discovery_query")
    visible = " ".join((title, company, snippet, _value(raw, "result_title")))
    target_company = _value(row, "company")
    company_matches = (
        bool(normalize_company_identity(company))
        and normalize_company_identity(company) == normalize_company_identity(target_company)
    )
    role = next((
        item for item in target_roles
        if item != "EXPLICIT_CONTACT" and ROLE_PATTERNS.get(item, re.compile(r"a^")).search(title)
    ), "")
    explicit = source_type == "JOB_POSTING" and bool(name and company_matches)
    if explicit:
        role = "EXPLICIT_CONTACT"

    reasons = []
    relationship = "NONE"
    if FORMER.search(visible):
        status, confidence = "REJECTED_FORMER_EMPLOYEE", "LOW"
        reasons.append("CONTACT_FORMER_EMPLOYEE")
    elif not company_matches:
        status, confidence = "REJECTED_COMPANY_MISMATCH", "LOW"
        reasons.append("CONTACT_COMPANY_MISMATCH")
    elif not role or GENERIC_HR.search(title):
        status, confidence = "REJECTED_ROLE_MISMATCH", "LOW"
        reasons.append("CONTACT_ROLE_MISMATCH")
    else:
        reasons.extend(("CONTACT_ROLE_MATCH", "CONTACT_CURRENT_COMPANY"))
        region = _region_match(row, visible)
        if explicit:
            confidence, relationship = "HIGH", "EXPLICIT_JOB_CONTACT"
            reasons.append("CONTACT_EXPLICIT_POSTING")
        elif source_type == "OFFICIAL_COMPANY":
            confidence, relationship = "HIGH", "OFFICIAL_COMPANY_ROLE"
            reasons.append("CONTACT_OFFICIAL_COMPANY_PAGE")
        elif region:
            confidence, relationship = "HIGH", "REGION_ALIGNED_HIRING_ROLE"
            reasons.append("CONTACT_REGION_MATCH")
        else:
            confidence, relationship = "MEDIUM", "COMPANY_HIRING_ROLE"
        status = "REJECTED_LOW_CONFIDENCE"  # promoted after cross-candidate ranking

    evidence = ({
        "name": name, "title": title, "company": company, "source_url": url,
        "source_type": source_type, "raw_title": _value(raw, "result_title"),
        "raw_snippet": snippet, "discovery_query": query,
    },)
    return PersonCandidate(
        name, title, company, role or target_roles[0], url, source_type, relationship,
        confidence, status, tuple(reasons), evidence, query,
        _person_key(name, company, url),
    )


def select_contacts(
    row: Mapping, raw_candidates: Sequence[Mapping], *, search_queries_used: int = 0,
    first_party_pages_inspected: int = 0, input_fingerprint: str = "",
    discovered_at: str | None = None,
) -> ContactDiscoveryResult:
    primary_role = _value(row, "primary_role")
    secondary = json.loads(_value(row, "secondary_roles_json") or "[]")
    roles = list(dict.fromkeys([primary_role, *secondary]))
    evaluated: dict[str, PersonCandidate] = {}
    for raw in raw_candidates:
        candidate = evaluate_person(row, raw, roles)
        previous = evaluated.get(candidate.person_key)
        prefer_candidate = previous is None or (
            candidate.confidence == "HIGH" and previous.confidence != "HIGH"
        ) or SOURCE_RANK.get(candidate.source_type, 9) < SOURCE_RANK.get(previous.source_type, 9)
        if previous is None:
            evaluated[candidate.person_key] = candidate
        else:
            preferred = candidate if prefer_candidate else previous
            combined_evidence = list(previous.evidence)
            for item in candidate.evidence:
                if item not in combined_evidence:
                    combined_evidence.append(item)
            queries = "; ".join(dict.fromkeys(filter(None, (
                previous.discovery_query, candidate.discovery_query,
            ))))
            evaluated[candidate.person_key] = replace(
                preferred, evidence=tuple(combined_evidence), discovery_query=queries,
            )

    role_rank = {role: index for index, role in enumerate(roles)}
    eligible = [candidate for candidate in evaluated.values() if candidate.confidence in {"HIGH", "MEDIUM"}]
    eligible.sort(key=lambda item: (
        role_rank.get(item.target_role_category, 99),
        0 if item.confidence == "HIGH" else 1,
        SOURCE_RANK.get(item.source_type, 9), item.person_name.casefold(), item.source_url,
    ))
    primary = replace(eligible[0], selection_status="SELECTED_PRIMARY") if eligible else None
    backup = replace(eligible[1], selection_status="SELECTED_BACKUP") if len(eligible) > 1 else None
    selected = {item.person_key: item for item in (primary, backup) if item}
    candidates = tuple(
        selected.get(
            item.person_key,
            replace(
                item, confidence="LOW", selection_status="REJECTED_LOW_CONFIDENCE",
                reason_codes=(*item.reason_codes, "CONTACT_LOWER_SELECTION_PRIORITY"),
            ) if item.confidence in {"HIGH", "MEDIUM"} else item,
        )
        for item in evaluated.values()
    )
    return ContactDiscoveryResult(
        int(row["job_id"]), _value(row, "company"),
        "CONTACTS_SELECTED" if primary else "NO_CONFIDENT_CONTACT",
        primary, backup, candidates, search_queries_used, first_party_pages_inspected,
        len(raw_candidates),
        input_fingerprint, discovered_at or utc_now(),
    )


def build_search_queries(row: Mapping, max_queries: int) -> list[str]:
    company = _value(row, "company")
    roles = [_value(row, "primary_role"), *json.loads(_value(row, "secondary_roles_json") or "[]")]
    roles = [role for role in roles if role != "EXPLICIT_CONTACT"]
    queries = [f'site:linkedin.com/in "{company}" "{role}"' for role in roles]
    queries.extend(f'site:github.com "{company}" "{role}"' for role in roles)
    return list(dict.fromkeys(queries))[:max_queries]


def _jsonld_people(value) -> list[dict]:
    people = []
    if isinstance(value, list):
        for item in value:
            people.extend(_jsonld_people(item))
    elif isinstance(value, dict):
        types = value.get("@type", ())
        if isinstance(types, str):
            types = (types,)
        if "Person" in types and value.get("name") and value.get("jobTitle"):
            people.append({"person_name": value["name"], "current_title": value["jobTitle"]})
        for item in value.values():
            if isinstance(item, (dict, list)):
                people.extend(_jsonld_people(item))
    return people


def extract_first_party_people(html: str, page_url: str, company: str) -> list[dict]:
    """Extract only explicit name/title pairs from a bounded official page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    people = []
    for script in soup.select("script[type='application/ld+json']"):
        try:
            people.extend(_jsonld_people(json.loads(script.string or "")))
        except (TypeError, json.JSONDecodeError):
            continue
    for container in soup.select(
        "[class*='team-member'], [class*='team_member'], [class*='person-card'], "
        "[class*='person_card'], [class*='leadership-card'], article"
    ):
        heading = container.find(["h2", "h3", "h4", "strong"])
        if heading is None:
            continue
        name = " ".join(heading.get_text(" ", strip=True).split())
        if not re.fullmatch(NAME, name):
            continue
        lines = [" ".join(item.split()) for item in container.stripped_strings]
        title = next((
            line for line in lines if line != name
            and any(pattern.search(line) for role, pattern in ROLE_PATTERNS.items() if role != "EXPLICIT_CONTACT")
        ), "")
        if title:
            people.append({"person_name": name, "current_title": title})
    result, seen = [], set()
    for person in people:
        name = " ".join(str(person.get("person_name") or "").split())
        title = " ".join(str(person.get("current_title") or "").split())
        key = (name.casefold(), title.casefold())
        if not re.fullmatch(NAME, name) or not title or key in seen:
            continue
        seen.add(key)
        result.append({
            "person_name": name, "current_title": title, "company": company,
            "source_url": page_url, "source_type": "OFFICIAL_COMPANY",
            "result_title": name, "result_snippet": title, "discovery_query": "",
        })
    return result


class FirstPartyPersonDiscoverer:
    """Inspect at most five deterministic, shallow pages on the company site."""

    def __init__(self, timeout: int = 8, fetcher=None):
        self.timeout, self.fetcher = timeout, fetcher

    def _fetch(self, url: str):
        if self.fetcher is not None:
            return self.fetcher(url, self.timeout)
        import requests
        return requests.get(
            url, timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; bounded-contact-research/2.0)"},
        )

    def __call__(self, row: Mapping, max_pages: int = MAX_FIRST_PARTY_PAGES):
        website = _canonical_url(_value(row, "company_website"))
        if not website:
            return [], 0
        origin = urlunsplit((*urlsplit(website)[:2], "", "", ""))
        expected_domain = (urlsplit(origin).hostname or "").casefold().removeprefix("www.")
        people, inspected = [], 0
        for path in FIRST_PARTY_PATHS[:max_pages]:
            page_url = urljoin(origin + "/", path.lstrip("/"))
            inspected += 1
            try:
                response = self._fetch(page_url)
                if isinstance(response, str):
                    html, final_url, status = response, page_url, 200
                else:
                    html = response.text
                    final_url = _canonical_url(getattr(response, "url", page_url))
                    status = getattr(response, "status_code", 200)
                final_domain = (urlsplit(final_url).hostname or "").casefold().removeprefix("www.")
                if status != 200 or not final_url or not (
                    final_domain == expected_domain or final_domain.endswith("." + expected_domain)
                ):
                    continue
                people.extend(extract_first_party_people(html, final_url, _value(row, "company")))
            except Exception:
                continue
        return people, inspected


class BrowserPersonSearcher:
    """Small Google result reader; it does not open or crawl profile pages."""

    def __init__(self, timeout: int = 15, verbose: bool = False):
        self.timeout, self.verbose, self.driver = timeout, verbose, None
        self.unavailable = False

    def __call__(self, query: str, limit: int) -> list[dict]:
        from selenium.webdriver.support.ui import WebDriverWait
        from utils.google_search_client import extract_organic_results
        from utils.google_search_discovery import blocked_search_page, create_chrome_driver
        if self.unavailable:
            return []
        if self.driver is None:
            self.driver = create_chrome_driver(windowed=False)
        try:
            self.driver.set_page_load_timeout(self.timeout)
            self.driver.get("https://www.google.com/search?q=" + quote_plus(query))
            WebDriverWait(self.driver, self.timeout).until(
                lambda current: blocked_search_page(current)
                or current.find_elements("css selector", "div.MjjYud h3, div.g h3")
            )
        except Exception:
            self.unavailable = True
            raise
        if blocked_search_page(self.driver):
            self.unavailable = True
            return []
        return extract_organic_results(
            self.driver, limit, include_diagnostics=True, source_query=query,
        )

    def close(self):
        if self.driver is not None:
            self.driver.quit()
            self.driver = None


def _select_rows(connection: sqlite3.Connection, job_ids: Sequence[int] | None, run_id: str | None):
    if job_ids is None and run_id is None:
        raise ValueError("provide at least one --job-id or --run-id")
    clauses, parameters = [], []
    if job_ids is not None:
        if not job_ids:
            return []
        clauses.append("j.job_id IN (" + ",".join("?" for _ in job_ids) + ")")
        parameters.extend(job_ids)
    if run_id:
        if not connection.execute("SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError(f"workflow run not found: {run_id}")
        clauses.append("EXISTS (SELECT 1 FROM workflow_run_jobs w WHERE w.run_id=? AND w.job_id=j.job_id)")
        parameters.append(run_id)
    clauses.append("q.qualification_status='QUALIFIED'")
    return connection.execute(
        """SELECT j.job_id,j.canonical_url,j.title,j.description,j.location_text,j.country,
                  j.region,j.city,j.content_hash,j.updated_at,c.canonical_name AS company,
                  CASE WHEN w.status='CONFIRMED' THEN w.website_url ELSE '' END AS company_website,
                  w.company_website_id,w.status AS company_website_status,
                  w.canonical_domain AS confirmed_company_domain,
                  w.input_fingerprint AS company_website_fingerprint,
                  q.qualification_id,q.input_evidence_hash,
                  q.qualification_status,s.contact_strategy_id,s.policy_version AS strategy_policy_version,
                  s.primary_role,s.secondary_roles_json,s.avoid_roles_json,s.input_fingerprint AS strategy_fingerprint
           FROM jobs j
           JOIN job_qualifications q ON q.qualification_id=(
               SELECT q2.qualification_id FROM job_qualifications q2 WHERE q2.job_id=j.job_id
               ORDER BY q2.qualification_id DESC LIMIT 1)
           JOIN job_contact_strategies s ON s.contact_strategy_id=(
               SELECT s2.contact_strategy_id FROM job_contact_strategies s2 WHERE s2.job_id=j.job_id
               ORDER BY s2.contact_strategy_id DESC LIMIT 1)
           LEFT JOIN job_company_websites w ON w.company_website_id=(
               SELECT w2.company_website_id FROM job_company_websites w2
               WHERE w2.job_id=j.job_id AND w2.status='CONFIRMED'
               ORDER BY w2.company_website_id DESC LIMIT 1)
           LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _fingerprint(row: Mapping, max_queries: int, max_people: int) -> str:
    payload = {"input": dict(row), "max_queries": max_queries, "max_people": max_people}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _persist(connection: sqlite3.Connection, result: ContactDiscoveryResult) -> ContactDiscoveryResult:
    connection.execute(
        "DELETE FROM job_selected_contacts WHERE job_id=? AND policy_version=?",
        (result.job_id, DISCOVERY_POLICY_VERSION),
    )
    connection.execute(
        "DELETE FROM job_contact_candidates WHERE job_id=? AND policy_version=?",
        (result.job_id, DISCOVERY_POLICY_VERSION),
    )
    stored = []
    for candidate in result.candidates:
        cursor = connection.execute(
            """INSERT INTO job_contact_candidates
               (job_id,policy_version,person_key,person_name,current_title,company,
                target_role_category,source_url,source_type,relationship_to_job,confidence,
                selection_status,reason_codes_json,evidence_json,discovery_query,input_fingerprint,
                checked_at,discovered_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result.job_id, DISCOVERY_POLICY_VERSION, candidate.person_key,
             candidate.person_name, candidate.current_title, candidate.company,
             candidate.target_role_category, candidate.source_url, candidate.source_type,
             candidate.relationship_to_job, candidate.confidence, candidate.selection_status,
             json.dumps(candidate.reason_codes, separators=(",", ":")),
             json.dumps(candidate.evidence, ensure_ascii=False, separators=(",", ":")),
             candidate.discovery_query, result.input_fingerprint, result.discovered_at,
             result.discovered_at, result.discovered_at),
        )
        stored.append(replace(candidate, candidate_id=cursor.lastrowid))
    by_key = {item.person_key: item for item in stored}
    primary = by_key.get(result.primary.person_key) if result.primary else None
    backup = by_key.get(result.backup.person_key) if result.backup else None
    connection.execute(
        """INSERT INTO job_selected_contacts
           (job_id,policy_version,discovery_status,primary_contact_candidate_id,
            backup_contact_candidate_id,search_queries_used,people_inspected,input_fingerprint,
            discovered_at,created_at,updated_at,first_party_pages_inspected)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (result.job_id, DISCOVERY_POLICY_VERSION, result.status,
         primary.candidate_id if primary else None, backup.candidate_id if backup else None,
         result.search_queries_used, result.people_inspected, result.input_fingerprint,
         result.discovered_at, result.discovered_at, result.discovered_at,
         result.first_party_pages_inspected),
    )
    return replace(result, primary=primary, backup=backup, candidates=tuple(stored))


def _stored_result(connection: sqlite3.Connection, selected: sqlite3.Row, company: str):
    rows = connection.execute(
        "SELECT * FROM job_contact_candidates WHERE job_id=? AND policy_version=? ORDER BY contact_candidate_id",
        (selected["job_id"], DISCOVERY_POLICY_VERSION),
    ).fetchall()
    candidates = tuple(PersonCandidate(
        row["person_name"], row["current_title"], row["company"], row["target_role_category"],
        row["source_url"], row["source_type"], row["relationship_to_job"], row["confidence"],
        row["selection_status"], tuple(json.loads(row["reason_codes_json"])),
        tuple(json.loads(row["evidence_json"])), row["discovery_query"], row["person_key"],
        row["contact_candidate_id"],
    ) for row in rows)
    by_id = {item.candidate_id: item for item in candidates}
    return ContactDiscoveryResult(
        selected["job_id"], company, selected["discovery_status"],
        by_id.get(selected["primary_contact_candidate_id"]),
        by_id.get(selected["backup_contact_candidate_id"]), candidates,
        selected["search_queries_used"], selected["first_party_pages_inspected"],
        selected["people_inspected"],
        selected["input_fingerprint"], selected["discovered_at"], True,
    )


def discover_contacts(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, max_search_queries: int = 5, max_people: int = 10,
    verbose: bool = False, searcher: Callable[[str, int], Sequence[Mapping]] | None = None,
    first_party_discoverer: Callable[[Mapping, int], tuple[Sequence[Mapping], int]] | None = None,
) -> list[ContactDiscoveryResult]:
    if not 1 <= max_search_queries <= 5:
        raise ValueError("--max-search-queries must be between 1 and 5")
    if not 1 <= max_people <= 10:
        raise ValueError("--max-people must be between 1 and 10")
    rows = _select_rows(connection, job_ids, run_id)
    owned_searcher = BrowserPersonSearcher(verbose=verbose) if searcher is None else None
    search = searcher or owned_searcher
    if first_party_discoverer is None:
        first_party = FirstPartyPersonDiscoverer() if searcher is None else lambda row, limit: ([], 0)
    else:
        first_party = first_party_discoverer
    results = []
    try:
        for row in rows:
            fingerprint = _fingerprint(row, max_search_queries, max_people)
            existing = connection.execute(
                """SELECT * FROM job_selected_contacts
                   WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
                (row["job_id"], DISCOVERY_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                result = _stored_result(connection, existing, row["company"] or "")
                results.append(result)
                continue

            raw = _explicit_contacts(row)[:max_people]
            first_party_people, pages_inspected = first_party(row, MAX_FIRST_PARTY_PAGES)
            raw.extend(list(first_party_people)[:max(0, max_people - len(raw))])
            queries_used = 0
            company_domain = (urlsplit(_value(row, "company_website")).hostname or "").casefold().removeprefix("www.")
            for query in build_search_queries(row, max_search_queries):
                if len(raw) >= max_people:
                    break
                if getattr(search, "unavailable", False) is True:
                    break
                provisional = select_contacts(row, raw)
                if provisional.primary and provisional.backup and provisional.primary.confidence == "HIGH":
                    break
                queries_used += 1
                limit = min(3, max_people - len(raw))
                try:
                    found = search(query, limit)
                except Exception as error:
                    if verbose:
                        print(
                            f"{row['job_id']}: search failed for query {queries_used}: "
                            f"{type(error).__name__}"
                        )
                    found = ()
                for item in found:
                    parsed = _parse_search_result(item, row["company"], company_domain, query)
                    if parsed is not None:
                        raw.append(parsed)
                    if len(raw) >= max_people:
                        break
                provisional = select_contacts(row, raw)
                if provisional.primary and provisional.backup and provisional.primary.confidence == "HIGH":
                    break
            result = select_contacts(
                row, raw, search_queries_used=queries_used,
                first_party_pages_inspected=pages_inspected,
                input_fingerprint=fingerprint, discovered_at=utc_now(),
            )
            with connection:
                result = _persist(connection, result)
            results.append(result)
            if verbose:
                print(
                    f"{result.job_id}: {result.status}; queries={queries_used}, "
                    f"first_party_pages={pages_inspected}, people={len(raw)}"
                )
    finally:
        if owned_searcher is not None:
            owned_searcher.close()
    return results


def print_results(results: Sequence[ContactDiscoveryResult]) -> None:
    for result in results:
        print(f"job_id: {result.job_id}")
        print(f"company: {result.company}")
        print(f"status: {result.status}")
        for label, candidate in (("primary", result.primary), ("backup", result.backup)):
            if candidate is None:
                print(f"{label}: null")
            else:
                print(f"{label}: {candidate.person_name} | {candidate.current_title} | {candidate.confidence}")
                print(f"{label}_source_url: {candidate.source_url}")
        print(f"search_queries_used: {result.search_queries_used}")
        print(f"first_party_pages_inspected: {result.first_party_pages_inspected}")
        print(f"people_inspected: {result.people_inspected}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded public professional contact discovery")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--max-search-queries", type=int, default=5)
    parser.add_argument("--max-people", type=int, default=10)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    connection = connect_database(args.database)
    try:
        try:
            results = discover_contacts(
                connection, args.job_id, args.run_id, args.max_search_queries,
                args.max_people, args.verbose,
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
