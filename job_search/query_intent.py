"""Deterministic parsing and geographic evaluation of discovery queries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Iterable, Mapping

from job_search.geography import COUNTRY_ALIASES, Geography, normalize_country


SITE_RE = re.compile(r"(?:^|\s)site:(?P<host>[^\s]+)", re.I)
EUROPE_RE = re.compile(r"\b(?:europe|european)\b", re.I)
REMOTE_RE = re.compile(r"\bremote(?:ly)?\b|\bwork from (?:home|anywhere)\b", re.I)
WORLDWIDE_RE = re.compile(r"\b(?:worldwide|global(?:ly)?|anywhere in the world)\b", re.I)
EUROPE_REMOTE_RE = re.compile(
    r"\bremote(?:ly)?\s*(?:[-,:/]\s*)?(?:in|within|across|throughout)?\s*(?:the\s+)?(?:eu|europe)\b|"
    r"\b(?:eu|europe)[ -]remote\b|\bremote eligibility[^.;\n]*\b(?:eu|europe)\b",
    re.I,
)

PROVIDER_HOSTS = {
    "jobs.lever.co": "lever",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
}


@dataclass(frozen=True)
class QueryIntent:
    source_query: str
    role_terms: tuple[str, ...]
    country: str
    region_scope: str
    remote: bool
    worldwide: bool
    provider_scope: str

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["role_terms"] = list(self.role_terms)
        return payload


@dataclass(frozen=True)
class QueryLocationDecision:
    state: str
    intents: tuple[QueryIntent, ...]
    matched_source_query: str


def _query_country(query: str) -> str:
    # Full country names are safe in free text. Short codes are accepted only
    # when the original query supplies an uppercase standalone token, avoiding
    # false matches for words such as "in" and "us".
    country = normalize_country(query, allow_codes=False)
    if country:
        return country
    for token in re.findall(r"(?<![\w])([A-Z]{2,3})(?![\w])", query):
        country = COUNTRY_ALIASES.get(token.casefold(), "")
        if country:
            return country
    return ""


def parse_query_intent(query: str) -> QueryIntent:
    """Parse only explicit query text; no profile or job attributes are used."""
    source_query = " ".join(str(query or "").split())
    site_match = SITE_RE.search(source_query)
    host = (site_match.group("host").casefold().strip("./") if site_match else "")
    provider = PROVIDER_HOSTS.get(host, host.removeprefix("www.") if host else "")
    without_site = SITE_RE.sub(" ", source_query)
    worldwide = bool(WORLDWIDE_RE.search(without_site) and REMOTE_RE.search(without_site))
    region_scope = "WORLDWIDE" if worldwide else ("EUROPE" if EUROPE_RE.search(without_site) else "")
    country = "" if region_scope else _query_country(without_site)

    role_text = without_site
    for pattern in (WORLDWIDE_RE, EUROPE_RE, REMOTE_RE):
        role_text = pattern.sub(" ", role_text)
    if country:
        aliases = [alias for alias, canonical in COUNTRY_ALIASES.items() if canonical == country]
        for alias in sorted(aliases, key=len, reverse=True):
            role_text = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", " ", role_text, flags=re.I)
    role_terms = tuple(re.findall(r"[a-z0-9+#.]+", role_text.casefold()))
    return QueryIntent(
        source_query=source_query,
        role_terms=role_terms,
        country=country,
        region_scope=region_scope,
        remote=bool(REMOTE_RE.search(without_site)),
        worldwide=worldwide,
        provider_scope=provider,
    )


def evaluate_query_location(
    queries: Iterable[str], geography: Geography, row: Mapping, *,
    remote_policy: str, worldwide_eligible: bool,
) -> QueryLocationDecision:
    """Apply any-match semantics across every geographic discovery query."""
    intents = tuple(parse_query_intent(query) for query in queries if str(query or "").strip())
    scoped = tuple(intent for intent in intents if intent.country or intent.region_scope)
    if not scoped:
        return QueryLocationDecision("NEUTRAL", intents, "")

    job_text = " ".join(str(row.get(name, "") or "") for name in (
        "title", "location_text", "country", "city", "description",
    )) if hasattr(row, "get") else " ".join(
        str(row[name] or "") if name in row.keys() else "" for name in
        ("title", "location_text", "country", "city", "description")
    )
    europe_remote = bool(EUROPE_REMOTE_RE.search(job_text))
    outcomes: list[tuple[QueryIntent, str]] = []
    for intent in scoped:
        if worldwide_eligible:
            outcome = "MATCH"
        elif intent.worldwide:
            outcome = "UNKNOWN" if remote_policy != "ONSITE" else "MISMATCH"
        elif intent.region_scope == "EUROPE":
            geographic_match = geography.region == "EUROPE" or europe_remote
            if intent.remote and geographic_match:
                outcome = "MATCH" if remote_policy == "REMOTE" or europe_remote else (
                    "MISMATCH" if remote_policy == "ONSITE" else "UNKNOWN"
                )
            elif geographic_match:
                outcome = "MATCH"
            elif geography.region == "UNKNOWN":
                outcome = "UNKNOWN"
            else:
                outcome = "MISMATCH"
        else:
            geographic_match = geography.country == intent.country
            if intent.remote and geographic_match:
                outcome = "MATCH" if remote_policy == "REMOTE" else (
                    "MISMATCH" if remote_policy == "ONSITE" else "UNKNOWN"
                )
            elif geographic_match:
                outcome = "MATCH"
            elif geography.region == "UNKNOWN":
                outcome = "UNKNOWN"
            else:
                outcome = "MISMATCH"
        outcomes.append((intent, outcome))

    for intent, outcome in outcomes:
        if outcome == "MATCH":
            return QueryLocationDecision("MATCH", intents, intent.source_query)
    if any(outcome == "UNKNOWN" for _, outcome in outcomes):
        return QueryLocationDecision("UNKNOWN", intents, "")
    return QueryLocationDecision("MISMATCH", intents, "")
