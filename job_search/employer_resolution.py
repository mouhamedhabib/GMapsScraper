"""Deterministic hiring-employer resolution from job-specific evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from job_search.activity_resolution import choose_authoritative_source
from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.providers import (
    ATS_PROVIDERS, ParsedJob, detect_provider, extract_source_job_id, fetch_job,
    generic_listing_reason,
)
from job_search.relationship_resolution import (
    extract_ats_tenant, normalize_company_identity,
)
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


EMPLOYER_POLICY_VERSION = "employer-evidence-v1"
EMPLOYER_STATUSES = ("CONFIRMED", "UNKNOWN", "CONFLICT")
STRUCTURED_METHODS = {
    "provider_company_field": "PROVIDER_STRUCTURED_EMPLOYER",
    "jsonld_hiringOrganization": "JSONLD_HIRING_ORGANIZATION",
    "microdata_hiringOrganization": "MICRODATA_HIRING_ORGANIZATION",
    "job_company_meta": "JOB_SPECIFIC_COMPANY_METADATA",
}
METHOD_RANK = {
    "PROVIDER_STRUCTURED_EMPLOYER": 0,
    "JSONLD_HIRING_ORGANIZATION": 1,
    "MICRODATA_HIRING_ORGANIZATION": 2,
    "EXPLICIT_RECRUITER_CLIENT": 3,
    "EXPLICIT_JOB_EMPLOYER_STATEMENT": 3,
    "JOB_SPECIFIC_COMPANY_METADATA": 4,
    "DIRECT_RELATIONSHIP_COMPANY": 5,
    "ATS_TENANT_COMPANY_MATCH": 5,
}


@dataclass(frozen=True)
class EmployerResolution:
    job_id: int
    employer_status: str
    actual_employer: str
    source_type: str
    employer_relationship: str
    provider: str
    authoritative_url: str
    evidence_method: str
    evidence: tuple[dict, ...]
    input_fingerprint: str = ""
    fetch_result: str = "NOT_REQUIRED"
    fetch_error: str = ""
    resolved_at: str = ""
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _fact(value: str, source: str, method: str, timestamp: str, field: str = "employer") -> dict:
    display = str(value or "").strip().strip(" ,;:–—-")
    return {
        "field": field,
        "raw_value": display,
        "normalized_value": normalize_company_identity(display),
        "source": source,
        "method": method,
        "timestamp": timestamp,
    }


def _dedupe_facts(facts: Sequence[dict]) -> list[dict]:
    result, seen = [], set()
    for item in facts:
        key = (item.get("normalized_value"), item.get("source"), item.get("method"))
        if item.get("normalized_value") and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _explicit_employer_evidence(description: str, company: str, timestamp: str) -> list[dict]:
    """Extract only explicit, bounded employer statements from posting prose."""
    text = " ".join((description or "").split())
    facts: list[dict] = []
    if company:
        escaped = re.escape(company)
        patterns = (
            rf"\b(?:join|joining|rejoignez|chez|at|about|à propos d['’e]*)\s+{escaped}\b",
            rf"\b{escaped}\s+(?:is|est)\s+(?:an?|une?)\s+(?:company|employer|soci[ée]t[ée]|entreprise|éditeur)\b",
            rf"\b{escaped}\s+is an equal opportunity employer\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                facts.append(_fact(company, "job_posting.description", "EXPLICIT_JOB_EMPLOYER_STATEMENT", timestamp))
                break

    name = r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'’+-]*(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'’+-]*){0,5}"
    recruiter_patterns = (
        rf"\b(?:our client|on behalf of|we are hiring for|hiring for)\s*[:,\-]?\s*(?P<name>{name})(?=\s*(?:,|\.|;|who\b|is\b|are\b|seeks?\b|has\b))",
        rf"\b(?:notre client|pour le compte de)\s*[:,\-]?\s*(?P<name>{name})(?=\s*(?:,|\.|;|qui\b|est\b|recherche\b))",
    )
    for pattern in recruiter_patterns:
        for match in re.finditer(pattern, text, re.I):
            candidate = match.group("name").strip()
            if not candidate[0].isupper() or candidate.casefold() in {
                "partner", "partner company", "hiring company", "company", "client",
            }:
                continue
            facts.append(_fact(candidate, "job_posting.description", "EXPLICIT_RECRUITER_CLIENT", timestamp))

    company_statement = re.compile(
        rf"\b(?P<name>{name})\s+(?:is|est)\s+(?:an?|une?)\s+"
        r"(?:international\s+)?(?:company|employer|soci[ée]t[ée]|entreprise|éditeur)\b"
    )
    for match in company_statement.finditer(text):
        facts.append(_fact(match.group("name"), "job_posting.description", "EXPLICIT_JOB_EMPLOYER_STATEMENT", timestamp))
    return _dedupe_facts(facts)


def _repair_evidence(repairs: Sequence[Mapping], timestamp: str) -> list[dict]:
    facts = []
    for repair in repairs:
        try:
            changes = json.loads(_value(repair, "field_changes_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        item = changes.get("company_name")
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        method = STRUCTURED_METHODS.get(source)
        if method and item.get("new_value"):
            facts.append(_fact(item["new_value"], f"job_repair_results:{source}", method, timestamp, "company_name"))
    return facts


def _parsed_evidence(parsed: ParsedJob | None, timestamp: str) -> list[dict]:
    if not isinstance(parsed, ParsedJob):
        return []
    items = list(parsed.employer_evidence)
    if not items and parsed.company_name:
        items.append({
            "value": parsed.company_name,
            "source": parsed.evidence_sources.get("company_name", ""),
        })
    facts = []
    for item in items:
        source = str(item.get("source") or "")
        method = STRUCTURED_METHODS.get(source)
        if method and item.get("value"):
            facts.append(_fact(item["value"], f"bounded_fetch:{source}", method, timestamp, "company_name"))
    return _dedupe_facts(facts)


def evaluate_employer(
    row: Mapping, sources: Sequence[Mapping], repairs: Sequence[Mapping] = (),
    parsed: ParsedJob | None = None, *, resolved_at: str | None = None,
) -> EmployerResolution:
    timestamp = resolved_at or utc_now()
    job_id = int(row["job_id"])
    company = _value(row, "company")
    description = "\n".join(filter(None, (
        _value(row, "description"), parsed.description if parsed else "",
    )))
    authoritative = choose_authoritative_source(sources)
    primary = authoritative or None
    source_type = primary.source_type if primary else "UNKNOWN"
    provider = primary.provider if primary else detect_provider(_value(row, "canonical_url"))
    url = primary.url if primary else ""
    relationship = "UNKNOWN"
    if primary:
        for source in sources:
            if int(source["job_source_id"]) == primary.job_source_id:
                relationship = _value(source, "employer_relationship").upper() or "UNKNOWN"
                break
    elif sources:
        relationship = _value(sources[0], "employer_relationship").upper() or "UNKNOWN"

    facts = _repair_evidence(repairs, timestamp)
    facts.extend(_parsed_evidence(parsed, timestamp))
    facts.extend(_explicit_employer_evidence(description, company, timestamp))

    tenant = extract_ats_tenant(url, provider) if url else ""
    company_key = normalize_company_identity(company)
    tenant_key = normalize_company_identity(tenant)
    if company and relationship == "DIRECT":
        facts.append(_fact(company, "stored_direct_company", "DIRECT_RELATIONSHIP_COMPANY", timestamp, "company.canonical_name"))
    elif company and provider in ATS_PROVIDERS and tenant_key and tenant_key == company_key:
        facts.append(_fact(company, "stored_ats_tenant_and_company", "ATS_TENANT_COMPANY_MATCH", timestamp, "company.canonical_name"))

    # An intermediary's ATS tenant and branding describe the source, not its client.
    if relationship == "RECRUITER" and tenant_key:
        facts = [
            item for item in facts
            if item["normalized_value"] != tenant_key
            or item["method"] in {"EXPLICIT_RECRUITER_CLIENT", "EXPLICIT_JOB_EMPLOYER_STATEMENT"}
        ]
    if relationship == "AGGREGATOR":
        facts = [item for item in facts if item["method"] in {
            "PROVIDER_STRUCTURED_EMPLOYER", "JSONLD_HIRING_ORGANIZATION",
            "MICRODATA_HIRING_ORGANIZATION", "EXPLICIT_RECRUITER_CLIENT",
            "EXPLICIT_JOB_EMPLOYER_STATEMENT",
        }]
    facts = _dedupe_facts(facts)

    identities = {item["normalized_value"] for item in facts}
    if len(identities) > 1:
        status, actual, method = "CONFLICT", "", "CONFLICTING_STRONG_EMPLOYER_EVIDENCE"
    elif facts:
        selected = min(facts, key=lambda item: (METHOD_RANK.get(item["method"], 99), facts.index(item)))
        status, actual, method = "CONFIRMED", selected["raw_value"], selected["method"]
    else:
        status, actual, method = "UNKNOWN", "", "INSUFFICIENT_JOB_SPECIFIC_EMPLOYER_EVIDENCE"
    return EmployerResolution(
        job_id, status, actual, source_type, relationship, provider or "generic",
        url, method, tuple(facts), resolved_at=timestamp,
    )


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
    return connection.execute(
        """SELECT j.job_id,j.canonical_url,j.title,j.description,j.content_hash,j.updated_at,
                  COALESCE(c.canonical_name,'') AS company
           FROM jobs j LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _sources(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT job_source_id,provider,source_job_id,source_url,apply_url,fetch_status,
                  fetch_error,last_fetched_at,raw_content_hash,source_type,employer_relationship
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""", (job_id,),
    ).fetchall()


def _repairs(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT repair_id,policy_version,completion_status,field_changes_json
           FROM job_repair_results WHERE job_id=? ORDER BY repair_id""", (job_id,),
    ).fetchall()


def _fingerprint(row: Mapping, sources: Sequence[Mapping], repairs: Sequence[Mapping]) -> str:
    payload = {"job": dict(row), "sources": [dict(item) for item in sources], "repairs": [dict(item) for item in repairs]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> EmployerResolution:
    return EmployerResolution(
        row["job_id"], row["employer_status"], row["actual_employer"] or "",
        row["source_type"], row["employer_relationship"], row["provider"],
        row["authoritative_url"] or "", row["evidence_method"],
        tuple(json.loads(row["raw_evidence_json"])), row["input_fingerprint"],
        row["fetch_result"], row["fetch_error"] or "", row["resolved_at"], True,
    )


def _persist(connection: sqlite3.Connection, result: EmployerResolution) -> None:
    evidence = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_employer_evidence
           (job_id,policy_version,employer_status,actual_employer,source_type,
            employer_relationship,provider,authoritative_url,evidence_method,
            raw_evidence_json,input_fingerprint,fetch_result,fetch_error,resolved_at,
            created_at,updated_at)
           VALUES (?,?,?,NULLIF(?,''),?,?,?,?,?,?,?, ?,NULLIF(?,''),?,?,?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             employer_status=excluded.employer_status,actual_employer=excluded.actual_employer,
             source_type=excluded.source_type,employer_relationship=excluded.employer_relationship,
             provider=excluded.provider,authoritative_url=excluded.authoritative_url,
             evidence_method=excluded.evidence_method,raw_evidence_json=excluded.raw_evidence_json,
             input_fingerprint=excluded.input_fingerprint,fetch_result=excluded.fetch_result,
             fetch_error=excluded.fetch_error,resolved_at=excluded.resolved_at,
             updated_at=excluded.updated_at""",
        (result.job_id, EMPLOYER_POLICY_VERSION, result.employer_status,
         result.actual_employer, result.source_type, result.employer_relationship,
         result.provider, result.authoritative_url, result.evidence_method, evidence,
         result.input_fingerprint, result.fetch_result, result.fetch_error,
         result.resolved_at, result.resolved_at, result.resolved_at),
    )


def _individual_posting(source) -> bool:
    if source.provider in ATS_PROVIDERS:
        return bool(source.source_job_id or extract_source_job_id(source.provider, source.url))
    parts = [part.casefold() for part in urlsplit(source.url).path.split("/") if part]
    markers = {"job", "jobs", "career", "careers", "position", "positions"}
    return (
        any(part in markers and index + 1 < len(parts) for index, part in enumerate(parts))
        and not generic_listing_reason("", source.url, page_fetched=False)
    )


def resolve_employers(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, timeout: int = 15, browser_fallback: bool = False,
    verbose: bool = False, fetcher=fetch_job, network_relay=None,
    driver_factory=None, browser_fetcher=None,
) -> list[EmployerResolution]:
    rows = _select_rows(connection, job_ids, run_id)
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results, driver = [], None
    try:
        for row in rows:
            sources, repairs = _sources(connection, row["job_id"]), _repairs(connection, row["job_id"])
            fingerprint = _fingerprint(row, sources, repairs)
            existing = connection.execute(
                "SELECT * FROM job_employer_evidence WHERE job_id=? AND policy_version=? AND input_fingerprint=?",
                (row["job_id"], EMPLOYER_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                result = _from_stored(existing)
                results.append(result)
                if verbose:
                    print(f"{result.job_id}: reused unchanged employer evidence ({result.employer_status})")
                continue

            result = evaluate_employer(row, sources, repairs)
            fetch_result, fetch_error = "NOT_REQUIRED", ""
            authoritative = choose_authoritative_source(sources)
            individual = bool(authoritative and _individual_posting(authoritative))
            if result.employer_status == "UNKNOWN" and authoritative and individual:
                parsed = None
                try:
                    parsed = relay.protect(
                        lambda: fetcher(authoritative.url, timeout=timeout),
                        context=f"employer resolution job {row['job_id']}",
                    )
                    fetch_result = parsed.fetch_status or "UNKNOWN"
                    fetch_error = parsed.fetch_error or ""
                except NetworkPauseExceeded as error:
                    fetch_result, fetch_error = "FAILED", f"NETWORK/{error.error_type}: {error}"
                except Exception as error:
                    fetch_result, fetch_error = "FAILED", f"{type(error).__name__}: {error}"
                if parsed is None:
                    parsed = ParsedJob(authoritative.url, authoritative.provider)
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
                            driver = relay.protect(lambda: driver_factory(windowed=False), context="employer browser startup")
                        browser_result = relay.protect(
                            lambda: browser_fetcher(authoritative.url, timeout, driver),
                            context=f"employer browser job {row['job_id']}",
                        )
                        if browser_result is not None:
                            parsed, fetch_result, fetch_error = browser_result, "BROWSER_FETCHED", ""
                    except Exception as error:
                        fetch_error = f"{type(error).__name__}: {error}"
                if parsed.fetch_status != "FAILED":
                    result = evaluate_employer(row, sources, repairs, parsed, resolved_at=result.resolved_at)
            result = replace(result, input_fingerprint=fingerprint, fetch_result=fetch_result, fetch_error=fetch_error)
            with connection:
                _persist(connection, result)
            results.append(result)
            if verbose:
                print(f"{result.job_id}: {result.employer_status} {result.actual_employer or '-'} ({result.evidence_method})")
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[EmployerResolution]) -> None:
    counts = Counter(item.employer_status for item in results)
    print(f"Jobs selected: {len(results)}")
    for status in EMPLOYER_STATUSES:
        print(f"{status}: {counts[status]}")
    for item in results:
        print(f"\njob_id: {item.job_id}")
        print(f"employer_status: {item.employer_status}")
        print(f"actual_employer: {item.actual_employer or '-'}")
        print(f"employer_relationship: {item.employer_relationship}")
        print(f"provider: {item.provider}")
        print(f"authoritative_url: {item.authoritative_url or '-'}")
        print(f"evidence_method: {item.evidence_method}")
        print(f"fetch_result: {item.fetch_result}")
        if item.fetch_error:
            print(f"fetch_error: {item.fetch_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic hiring-employer evidence resolution")
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
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_employers(
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
