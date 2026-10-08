"""Deterministic, job-centric location eligibility evidence resolution."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from job_search.geography import infer_known_city, normalize_country
from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.providers import ParsedJob, detect_provider, fetch_job
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


LOCATION_ELIGIBILITY_POLICY_VERSION = "location-eligibility-v1"
WORK_MODELS = ("ONSITE", "HYBRID", "REMOTE", "UNKNOWN")
REMOTE_SCOPES = ("WORLDWIDE", "EUROPE", "EU", "EEA", "COUNTRY", "REGION", "CITY", "UNKNOWN")
EVIDENCE_STATUSES = ("KNOWN", "PARTIAL", "UNKNOWN")

_FALSE_REMOTE = re.compile(
    r"\b(?:remote monitoring|remote control|remote access|serve remote teams?|"
    r"customers? worldwide|worldwide customers?|european customers?|"
    r"distributed customers?|global (?:company|platform)|international team)\b", re.I,
)
_NON_CURRENT_ROLE = re.compile(r"\b(?:for certain roles|for some roles|eligible roles)\b", re.I)
_REMOTE_TEAM_CONTEXT = re.compile(r"\b(?:distributed,?\s+)?remote-first team\b", re.I)
_HYBRID = re.compile(
    r"\b(?:hybrid(?: working| work| role| position| culture)?|hybride|mode hybride)\b|"
    r"\b\d+\s+(?:days?|jours?)\s+(?:a|per|par)\s+(?:week|semaine)\s+(?:in (?:the )?office|sur site)\b",
    re.I,
)
_REMOTE = re.compile(
    r"\b(?:fully remote|remote-first|remote (?:role|position|job|working|environment)|"
    r"(?:role|position|job|work|working)\s+(?:is\s+)?remote|work remotely|"
    r"work from anywhere|remote\s+(?:worldwide|within|across|throughout|in|uk)|"
    r"t[ée]l[ée]travail|travail [àa] distance)\b", re.I,
)
_ONSITE = re.compile(
    r"\b(?:onsite|on-site|office-based|must work from (?:our|the) office|"
    r"on-site technical resource|pr[ée]sentiel)\b|\b(?:travail|poste) sur site\b",
    re.I,
)
_RESIDENCY = re.compile(
    r"\b(?:must be based in|need to reside in|must (?:live|reside) in|"
    r"only candidates? (?:located|based) in|candidates? must (?:live|reside) in|"
    r"(?:candidat(?:e)?s? |vous |tu )?(?:bas[ée]s?|r[ée]sider|domicili[ée]s?) en)\b",
    re.I,
)
_WORK_AUTH = re.compile(
    r"\b(?:must be authori[sz]ed to work in|must have (?:the )?right to work in|"
    r"work authori[sz]ation (?:in .+ )?required|(?:eu|eea) work authori[sz]ation required|"
    r"autoris[ée]s? [àa] travailler en|droit de travailler en|autorisation de travail)\b",
    re.I,
)
_VISA_NO = re.compile(
    r"\b(?:do not|don't|cannot|can't|unable to|no) (?:provide )?(?:visa )?sponsor(?:ship|ing)?\b|"
    r"\b(?:visa sponsorship (?:is )?not available|pas de sponsoring visa|"
    r"pas de parrainage de visa)\b", re.I,
)
_VISA_YES = re.compile(
    r"\b(?:visa sponsorship (?:is )?available|we can sponsor (?:eligible )?candidates?|"
    r"sponsoring (?:a )?visa|sponsoring visa|parrainage de visa|prise en charge du visa)\b",
    re.I,
)
_RELOCATION_NO = re.compile(r"\b(?:no|without) relocation (?:support|assistance|package)\b", re.I)
_RELOCATION_YES = re.compile(
    r"\b(?:relocation (?:assistance|support) (?:is )?available|"
    r"relocation package (?:is )?provided)\b", re.I,
)
_CITY_NAMES = ("London", "Lisbon", "Paris", "Tunis", "Amsterdam", "Brussels")


@dataclass(frozen=True)
class LocationEligibilityResult:
    job_id: int
    work_model: str = "UNKNOWN"
    remote_scope: str = "UNKNOWN"
    required_country: str = ""
    required_region: str = ""
    required_city: str = ""
    residency_requirement: str = "NOT_STATED"
    work_authorization_requirement: str = "NOT_STATED"
    work_authorization_jurisdiction: str = ""
    visa_sponsorship: str = "NOT_STATED"
    relocation_support: str = "NOT_STATED"
    location_eligibility_status: str = "UNKNOWN"
    evidence: tuple[dict, ...] = ()
    fetch_status: str = "NOT_REQUIRED"
    fetch_error: str = ""
    resolved_at: str = ""
    reused: bool = False


def _sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+|\s*[|•]\s*", cleaned) if item.strip()]


def _city(text: str) -> str:
    known = infer_known_city(text)
    if known:
        return known
    for city in _CITY_NAMES:
        if re.search(rf"\b{re.escape(city)}\b", text, re.I):
            return city
    return ""


def _country_in_statement(text: str) -> str:
    country = normalize_country(text, allow_codes=False)
    if country:
        return country
    if re.search(r"\b(?:UK|U\.K\.)\b", text):
        return "United Kingdom"
    return ""


def _jurisdiction(text: str) -> tuple[str, str, str]:
    country = _country_in_statement(text)
    if country:
        return country, "", ""
    if re.search(r"\bEEA\b", text, re.I):
        return "", "EEA", ""
    if re.search(r"\b(?:EU|European Union)\b", text, re.I):
        return "", "EU", ""
    if re.search(r"\bEurope\b", text, re.I):
        return "", "EUROPE", ""
    return "", "", _city(text)


def extract_location_evidence(
    *, job_id: int = 0, description: str = "", location_text: str = "",
    country: str = "", region: str = "", city: str = "",
    remote_policy: str = "", source: str = "stored_job",
    resolved_at: str | None = None,
) -> LocationEligibilityResult:
    """Extract requirements from posting text; no candidate data is accepted."""
    timestamp = resolved_at or utc_now()
    facts: list[dict] = []

    def fact(field: str, value: str, raw: str, method: str, fact_source: str = source):
        facts.append({
            "job_id": job_id, "field": field, "normalized_value": value,
            "raw_evidence_text": raw, "evidence_source": fact_source,
            "method": method, "timestamp": timestamp,
        })

    models: dict[str, list[str]] = {name: [] for name in WORK_MODELS[:-1]}
    scopes: dict[str, list[str]] = {name: [] for name in REMOTE_SCOPES[:-1]}
    residency_lines: list[str] = []
    auth_lines: list[str] = []
    visa_yes: list[str] = []
    visa_no: list[str] = []
    relocation_yes: list[str] = []
    relocation_no: list[str] = []

    structured_model = (remote_policy or "").strip().upper()
    if structured_model in models:
        models[structured_model].append(remote_policy)
        fact("work_model", structured_model, remote_policy, "STRUCTURED_WORK_MODEL")

    for sentence in _sentences(description):
        hybrid = bool(_HYBRID.search(sentence))
        if hybrid:
            models["HYBRID"].append(sentence)
        bare_remote = bool(re.fullmatch(
            r"(?:location|work model|workplace)?\s*:?\s*remote", sentence, re.I
        ))
        if (
            (_REMOTE.search(sentence) or bare_remote)
            and not _FALSE_REMOTE.search(sentence)
            and not _NON_CURRENT_ROLE.search(sentence)
            and not _REMOTE_TEAM_CONTEXT.search(sentence)
        ):
            models["REMOTE"].append(sentence)
        if _ONSITE.search(sentence) and not hybrid:
            models["ONSITE"].append(sentence)
        if _RESIDENCY.search(sentence):
            residency_lines.append(sentence)
        if _WORK_AUTH.search(sentence):
            auth_lines.append(sentence)
        if _VISA_NO.search(sentence):
            visa_no.append(sentence)
        elif _VISA_YES.search(sentence):
            visa_yes.append(sentence)
        if _RELOCATION_NO.search(sentence):
            relocation_no.append(sentence)
        elif _RELOCATION_YES.search(sentence):
            relocation_yes.append(sentence)

    model_values = [name for name, lines in models.items() if lines]
    conflict = len(model_values) > 1
    work_model = model_values[0] if len(model_values) == 1 else "UNKNOWN"
    for model in model_values:
        for raw in models[model]:
            if not any(f["field"] == "work_model" and f["raw_evidence_text"] == raw for f in facts):
                fact("work_model", model, raw, "EXPLICIT_WORK_ARRANGEMENT")

    remote_lines = models["REMOTE"]
    for sentence in remote_lines:
        if re.search(r"\b(?:worldwide|work from anywhere|anywhere in the world)\b", sentence, re.I):
            scopes["WORLDWIDE"].append(sentence)
        elif re.search(r"\b(?:within|across|throughout) (?:the )?EEA\b", sentence, re.I):
            scopes["EEA"].append(sentence)
        elif re.search(r"\b(?:within|across|throughout) (?:the )?(?:EU|European Union)\b", sentence, re.I):
            scopes["EU"].append(sentence)
        elif re.search(r"\b(?:within|across|throughout) Europe\b|\bpartout en Europe\b", sentence, re.I):
            scopes["EUROPE"].append(sentence)
        else:
            scoped_country = _country_in_statement(sentence)
            scoped_city = _city(sentence)
            if scoped_country and re.search(r"\b(?:remote|remotely|t[ée]l[ée]travail|distance)\b", sentence, re.I):
                scopes["COUNTRY"].append(sentence)
            elif scoped_city and re.search(r"within\s+\d+\s+hours?\s+of", sentence, re.I):
                scopes["CITY"].append(sentence)
    scope_values = [name for name, lines in scopes.items() if lines]
    if len(scope_values) > 1:
        conflict = True
    remote_scope = scope_values[0] if len(scope_values) == 1 else "UNKNOWN"
    for scope in scope_values:
        for raw in scopes[scope]:
            fact("remote_scope", scope, raw, "EXPLICIT_REMOTE_SCOPE")

    required_countries: list[tuple[str, str, str]] = []
    required_regions: list[tuple[str, str, str]] = []
    required_cities: list[tuple[str, str, str]] = []
    for sentence in residency_lines:
        ctry, reg, cty = _jurisdiction(sentence)
        fact("residency_requirement", "REQUIRED", sentence, "EXPLICIT_RESIDENCY_REQUIREMENT")
        if ctry: required_countries.append((ctry, sentence, "EXPLICIT_RESIDENCY_REQUIREMENT"))
        if reg: required_regions.append((reg, sentence, "EXPLICIT_RESIDENCY_REQUIREMENT"))
        if cty: required_cities.append((cty, sentence, "EXPLICIT_RESIDENCY_REQUIREMENT"))

    # Onsite/hybrid location fields describe the workplace, not personal residency.
    if work_model in {"ONSITE", "HYBRID"}:
        structured_country = normalize_country(country or location_text, allow_codes=True)
        structured_city = city.strip() or _city(location_text)
        if structured_country:
            required_countries.append((structured_country, country or location_text, "STRUCTURED_WORKPLACE_LOCATION"))
        if region.strip():
            required_regions.append((region.strip(), region, "STRUCTURED_WORKPLACE_LOCATION"))
        if structured_city:
            required_cities.append((structured_city, city or location_text, "STRUCTURED_WORKPLACE_LOCATION"))
    if remote_scope == "COUNTRY" and remote_lines:
        ctry = _country_in_statement(scopes["COUNTRY"][0])
        if ctry: required_countries.append((ctry, scopes["COUNTRY"][0], "EXPLICIT_REMOTE_SCOPE"))
    if remote_scope in {"EUROPE", "EU", "EEA"}:
        required_regions.append((remote_scope, scopes[remote_scope][0], "EXPLICIT_REMOTE_SCOPE"))
    if remote_scope == "CITY":
        cty = _city(scopes["CITY"][0])
        if cty: required_cities.append((cty, scopes["CITY"][0], "EXPLICIT_REMOTE_SCOPE"))

    def choose(values: list[tuple[str, str, str]], field_name: str) -> str:
        nonlocal conflict
        unique = {value for value, _, _ in values}
        if len(unique) > 1:
            conflict = True
            return ""
        if not values:
            return ""
        value = values[0][0]
        for _, raw, method in values:
            fact(field_name, value, raw, method, "structured_job_field" if method.startswith("STRUCTURED") else source)
        return value

    required_country = choose(required_countries, "required_country")
    required_region = choose(required_regions, "required_region")
    required_city = choose(required_cities, "required_city")
    residency = "REQUIRED" if residency_lines else "NOT_STATED"

    auth_jurisdictions = [_jurisdiction(line) for line in auth_lines]
    auth_values = {next((v for v in parts if v), "") for parts in auth_jurisdictions}
    auth_values.discard("")
    if len(auth_values) > 1:
        conflict = True
        authorization_jurisdiction = ""
    else:
        authorization_jurisdiction = next(iter(auth_values), "")
    authorization = "REQUIRED" if auth_lines else "NOT_STATED"
    for line in auth_lines:
        fact("work_authorization_requirement", "REQUIRED", line, "EXPLICIT_WORK_AUTHORIZATION")
        if authorization_jurisdiction:
            fact("work_authorization_jurisdiction", authorization_jurisdiction, line, "EXPLICIT_WORK_AUTHORIZATION")

    def polarity(positive: list[str], negative: list[str], field_name: str, method: str) -> str:
        nonlocal conflict
        if positive and negative:
            conflict = True
            for line in positive + negative:
                fact(field_name, "UNKNOWN", line, method + "_CONFLICT")
            return "UNKNOWN"
        value = "AVAILABLE" if positive else "NOT_AVAILABLE" if negative else "NOT_STATED"
        for line in positive + negative:
            fact(field_name, value, line, method)
        return value

    visa = polarity(visa_yes, visa_no, "visa_sponsorship", "EXPLICIT_VISA_SPONSORSHIP")
    relocation = polarity(relocation_yes, relocation_no, "relocation_support", "EXPLICIT_RELOCATION_SUPPORT")

    complete_location = (
        (work_model in {"ONSITE", "HYBRID"} and bool(required_country or required_region or required_city))
        or (work_model == "REMOTE" and remote_scope != "UNKNOWN")
        or (residency == "REQUIRED" and bool(required_country or required_region or required_city))
        or (authorization == "REQUIRED" and bool(authorization_jurisdiction))
    )
    has_any = bool(model_values or residency_lines or auth_lines or visa_yes or visa_no or relocation_yes or relocation_no)
    status = "PARTIAL" if conflict or (has_any and not complete_location) else "KNOWN" if complete_location else "UNKNOWN"
    return LocationEligibilityResult(
        job_id=job_id, work_model=work_model, remote_scope=remote_scope,
        required_country=required_country, required_region=required_region,
        required_city=required_city, residency_requirement=residency,
        work_authorization_requirement=authorization,
        work_authorization_jurisdiction=authorization_jurisdiction,
        visa_sponsorship=visa, relocation_support=relocation,
        location_eligibility_status=status, evidence=tuple(facts),
        resolved_at=timestamp,
    )


def _safe_url(url: str) -> bool:
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname.casefold() == "localhost":
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
        return not (address.is_private or address.is_loopback or address.is_link_local)
    except ValueError:
        return True


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
        clauses.append("EXISTS (SELECT 1 FROM workflow_run_jobs wrj WHERE wrj.run_id=? AND wrj.job_id=j.job_id)")
        parameters.append(run_id)
    return connection.execute(
        """SELECT j.job_id,j.canonical_url,j.description,j.location_text,j.country,j.region,j.city,j.remote_policy,j.content_hash
           FROM jobs j WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _inputs(connection: sqlite3.Connection, row: Mapping) -> dict:
    sources = connection.execute(
        """SELECT job_source_id,provider,source_url,raw_content_hash
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""",
        (row["job_id"],),
    ).fetchall()
    repairs = connection.execute(
        """SELECT policy_version,completion_status,field_changes_json,
                  fields_filled_json,source_type FROM job_repair_results
           WHERE job_id=? ORDER BY repair_id""", (row["job_id"],),
    ).fetchall()
    payload = {
        "job": dict(row), "sources": [dict(item) for item in sources],
        "repairs": [dict(item) for item in repairs],
    }
    return payload


def _fingerprint(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> LocationEligibilityResult:
    return LocationEligibilityResult(
        job_id=row["job_id"], work_model=row["work_model"], remote_scope=row["remote_scope"],
        required_country=row["required_country"] or "", required_region=row["required_region"] or "",
        required_city=row["required_city"] or "", residency_requirement=row["residency_requirement"],
        work_authorization_requirement=row["work_authorization_requirement"],
        work_authorization_jurisdiction=row["work_authorization_jurisdiction"] or "",
        visa_sponsorship=row["visa_sponsorship"], relocation_support=row["relocation_support"],
        location_eligibility_status=row["location_eligibility_status"],
        evidence=tuple(json.loads(row["evidence_json"])), fetch_status=row["fetch_status"],
        fetch_error=row["fetch_error"] or "", resolved_at=row["resolved_at"], reused=True,
    )


def _persist(connection: sqlite3.Connection, result: LocationEligibilityResult, fingerprint: str):
    evidence_json = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_location_eligibility
           (job_id,policy_version,work_model,remote_scope,required_country,required_region,
            required_city,residency_requirement,work_authorization_requirement,
            work_authorization_jurisdiction,visa_sponsorship,relocation_support,
            location_eligibility_status,evidence_json,input_evidence_hash,fetch_status,
            fetch_error,resolved_at,created_at,updated_at)
           VALUES (?, ?, ?, ?, NULLIF(?,''), NULLIF(?,''), NULLIF(?,''), ?, ?,
                   NULLIF(?,''), ?, ?, ?, ?, ?, ?, NULLIF(?,''), ?, ?, ?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             work_model=excluded.work_model,remote_scope=excluded.remote_scope,
             required_country=excluded.required_country,required_region=excluded.required_region,
             required_city=excluded.required_city,residency_requirement=excluded.residency_requirement,
             work_authorization_requirement=excluded.work_authorization_requirement,
             work_authorization_jurisdiction=excluded.work_authorization_jurisdiction,
             visa_sponsorship=excluded.visa_sponsorship,relocation_support=excluded.relocation_support,
             location_eligibility_status=excluded.location_eligibility_status,
             evidence_json=excluded.evidence_json,input_evidence_hash=excluded.input_evidence_hash,
             fetch_status=excluded.fetch_status,fetch_error=excluded.fetch_error,
             resolved_at=excluded.resolved_at,updated_at=excluded.updated_at""",
        (result.job_id, LOCATION_ELIGIBILITY_POLICY_VERSION, result.work_model,
         result.remote_scope, result.required_country, result.required_region, result.required_city,
         result.residency_requirement, result.work_authorization_requirement,
         result.work_authorization_jurisdiction, result.visa_sponsorship,
         result.relocation_support, result.location_eligibility_status, evidence_json,
         fingerprint, result.fetch_status, result.fetch_error, result.resolved_at,
         result.resolved_at, result.resolved_at),
    )


def resolve_location_eligibility(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, timeout: int = 15, browser_fallback: bool = False,
    verbose: bool = False, fetcher=fetch_job, network_relay=None,
    driver_factory=None, browser_fetcher=None,
) -> list[LocationEligibilityResult]:
    rows = _select_rows(connection, job_ids, run_id)
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results, driver = [], None
    try:
        for row in rows:
            payload = _inputs(connection, row)
            fingerprint = _fingerprint(payload)
            existing = connection.execute(
                "SELECT * FROM job_location_eligibility WHERE job_id=? AND policy_version=? AND input_evidence_hash=?",
                (row["job_id"], LOCATION_ELIGIBILITY_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                result = _from_stored(existing)
                results.append(result)
                if verbose: print(f"{result.job_id}: reused unchanged evidence ({result.location_eligibility_status})")
                continue
            result = extract_location_evidence(
                job_id=row["job_id"], description=row["description"] or "",
                location_text=row["location_text"] or "", country=row["country"] or "",
                region=row["region"] or "", city=row["city"] or "",
                remote_policy=row["remote_policy"] or "",
            )
            fetch_status, fetch_error = "NOT_REQUIRED", ""
            url = row["canonical_url"] or ""
            if result.location_eligibility_status != "KNOWN" and _safe_url(url):
                parsed = None
                try:
                    parsed = relay.protect(lambda: fetcher(url, timeout=timeout), context=f"location eligibility job {row['job_id']}")
                    fetch_status = parsed.fetch_status or "UNKNOWN"
                    fetch_error = parsed.fetch_error or ""
                except NetworkPauseExceeded as error:
                    fetch_status, fetch_error = "FAILED", f"NETWORK/{error.error_type}: {error}"
                except Exception as error:
                    fetch_status, fetch_error = "FAILED", f"{type(error).__name__}: {error}"
                if parsed is None:
                    parsed = ParsedJob(url, detect_provider(url))
                    parsed.fetch_status, parsed.fetch_error = "FAILED", fetch_error
                if parsed.fetch_status == "FAILED" and browser_fallback:
                    try:
                        if browser_fetcher is None:
                            from job_search.repair_reviews import _browser_fetch
                            browser_fetcher = _browser_fetch
                        if driver is None:
                            if driver_factory is None:
                                from utils.google_search_discovery import create_chrome_driver
                                driver_factory = create_chrome_driver
                            driver = relay.protect(lambda: driver_factory(windowed=False), context="location browser startup")
                        browser_result = relay.protect(lambda: browser_fetcher(url, timeout, driver), context=f"location browser job {row['job_id']}")
                        if browser_result is not None:
                            parsed = browser_result
                            fetch_status, fetch_error = "BROWSER_FETCHED", ""
                    except Exception as error:
                        fetch_error = f"{type(error).__name__}: {error}"
                if parsed is not None and parsed.fetch_status != "FAILED":
                    combined = "\n".join(filter(None, (row["description"] or "", parsed.description)))
                    result = extract_location_evidence(
                        job_id=row["job_id"], description=combined,
                        location_text=row["location_text"] or parsed.location_text,
                        country=row["country"] or parsed.country,
                        region=row["region"] or parsed.region, city=row["city"] or parsed.city,
                        remote_policy=row["remote_policy"] or parsed.remote_policy,
                        source="stored_job_and_bounded_fetch", resolved_at=result.resolved_at,
                    )
            result = replace(result, fetch_status=fetch_status, fetch_error=fetch_error)
            with connection:
                _persist(connection, result, fingerprint)
            results.append(result)
            if verbose: print(f"{result.job_id}: {result.location_eligibility_status} {result.work_model} {result.remote_scope}")
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[LocationEligibilityResult]):
    counts = Counter(item.location_eligibility_status for item in results)
    print(f"Jobs selected: {len(results)}")
    for status in EVIDENCE_STATUSES: print(f"{status}: {counts[status]}")
    for item in results:
        print(f"\njob_id: {item.job_id}")
        for field in ("work_model", "remote_scope", "required_country", "required_region", "required_city",
                      "residency_requirement", "work_authorization_requirement", "work_authorization_jurisdiction",
                      "visa_sponsorship", "relocation_support", "location_eligibility_status"):
            print(f"{field}: {getattr(item, field) or '-'}")
        for evidence in item.evidence:
            print(f"evidence: {evidence['field']}={evidence['normalized_value']} | {evidence['raw_evidence_text']}")


def build_parser():
    parser = argparse.ArgumentParser(description="Deterministic job location eligibility evidence resolution")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--browser-fallback", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1: parser.error("--timeout must be at least 1")
    if args.job_id is None and args.run_id is None: parser.error("provide at least one --job-id or --run-id")
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_location_eligibility(
                connection, args.job_id, args.run_id, args.timeout,
                args.browser_fallback, args.verbose,
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
