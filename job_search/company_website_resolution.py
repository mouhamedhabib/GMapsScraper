"""Deterministic official employer-website resolution for bounded job sets."""

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
from urllib.parse import urlsplit, urlunsplit

from job_search.activity_resolution import choose_authoritative_source
from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.providers import (
    AGGREGATOR_DOMAINS, ATS_PROVIDERS, JOB_PLATFORM_DOMAINS, ParsedJob,
    detect_provider, fetch_job,
)
from job_search.relationship_resolution import normalize_company_identity
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


WEBSITE_POLICY_VERSION = "company-website-v1"
WEBSITE_STATUSES = ("CONFIRMED", "UNKNOWN", "CONFLICT", "UNAVAILABLE")
BLOCKED_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "github.com", "glassdoor.com", "indeed.com", "crunchbase.com",
    "wikipedia.org", "google.com", "bing.com", "yahoo.com",
    "job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co",
    "jobs.ashbyhq.com", "apply.workable.com", "smartrecruiters.com",
    "teamtailor.com", "jobgether.com",
} | set(JOB_PLATFORM_DOMAINS) | set(AGGREGATOR_DOMAINS)
COUNTRY_SECOND_LEVEL = {"co.uk", "com.au", "co.nz", "co.in", "com.br", "co.za"}
METHOD_RANK = {
    "DIRECT_COMPANY_SOURCE": 0,
    "ATS_STRUCTURED_COMPANY_WEBSITE": 1,
    "JSONLD_HIRING_ORGANIZATION_SAME_AS": 2,
    "JSONLD_HIRING_ORGANIZATION_URL": 3,
    "JSONLD_ORGANIZATION_WEBSITE": 4,
    "ATS_EXPLICIT_EMPLOYER_LINK": 5,
    "ATS_EXPLICIT_EMPLOYER_DOMAIN": 5,
    "TRUSTED_COMPANY_RECORD": 6,
    "BOUNDED_PUBLIC_RESOLUTION": 7,
}


@dataclass(frozen=True)
class WebsiteResolution:
    job_id: int
    status: str
    company_name: str
    website_url: str
    canonical_domain: str
    source_url: str
    original_candidate_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    evidence_method: str
    source_type: str
    reason_codes: tuple[str, ...]
    evidence: tuple[dict, ...]
    http_status: int | None = None
    fetch_result: str = "NOT_REQUIRED"
    fetch_error: str = ""
    network_used: bool = False
    input_fingerprint: str = ""
    resolved_at: str = ""
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


def _host(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold().removeprefix("www.")


def _domain_matches(domain: str, blocked: str) -> bool:
    return domain == blocked or domain.endswith("." + blocked)


def _blocked_domain(domain: str) -> bool:
    return (
        not domain
        or detect_provider("https://" + domain) in ATS_PROVIDERS
        or any(_domain_matches(domain, item) for item in BLOCKED_DOMAINS)
    )


def _registrable_domain(domain: str) -> str:
    labels = [label for label in domain.casefold().split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    suffix2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix2 in COUNTRY_SECOND_LEVEL else suffix2


def _method(source: str) -> str:
    return {
        "jsonld_hiringOrganization.sameAs": "JSONLD_HIRING_ORGANIZATION_SAME_AS",
        "jsonld_hiringOrganization.url": "JSONLD_HIRING_ORGANIZATION_URL",
        "jsonld_organization.sameAs": "JSONLD_ORGANIZATION_WEBSITE",
        "jsonld_organization.url": "JSONLD_ORGANIZATION_WEBSITE",
        "ats_explicit_employer_link": "ATS_EXPLICIT_EMPLOYER_LINK",
        "ats_explicit_employer_domain_statement": "ATS_EXPLICIT_EMPLOYER_DOMAIN",
    }.get(source, "ATS_STRUCTURED_COMPANY_WEBSITE")


def _fact(
    url: str, company_name: str, method: str, source_url: str,
    *, final_url: str = "", redirect_chain: Sequence[str] = (), source_type: str = "STRUCTURED",
) -> dict | None:
    original = _canonical_url(url)
    final = _canonical_url(final_url) or original
    domain = _host(final)
    if not original or _blocked_domain(domain):
        return None
    return {
        "company_name": company_name, "original_candidate_url": original,
        "final_url": final, "canonical_domain": _registrable_domain(domain),
        "evidence_method": method, "source_url": source_url,
        "source_type": source_type, "redirect_chain": list(redirect_chain),
    }


def _page_identity(html: str) -> tuple[str, ...]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    names = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text() or "null")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        stack = value if isinstance(value, list) else [value]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type") or ""
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(kind).rstrip("/").rsplit("/", 1)[-1] == "Organization" for kind in types):
                if item.get("name"):
                    names.append(str(item["name"]).strip())
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title:
        names.extend(part.strip() for part in re.split(r"\s+[|–—-]\s+", title) if part.strip())
    return tuple(dict.fromkeys(names))


def evaluate_website(
    row: Mapping, sources: Sequence[Mapping], parsed: ParsedJob | None = None,
    *, redirected_candidate: Mapping | None = None, resolved_at: str | None = None,
) -> WebsiteResolution:
    timestamp = resolved_at or utc_now()
    company = _value(row, "actual_employer") or _value(row, "company")
    company_key = normalize_company_identity(company)
    facts: list[dict] = []
    rejected: list[dict] = []

    for source in sources:
        relationship = _value(source, "employer_relationship").upper()
        source_type = _value(source, "source_type").upper()
        if relationship == "DIRECT" and source_type == "COMPANY_SITE":
            fact = _fact(
                _value(source, "source_url"), company, "DIRECT_COMPANY_SOURCE",
                _value(source, "source_url"), source_type="PERSISTED_SOURCE",
            )
            if fact:
                facts.append(fact)

    existing = _value(row, "existing_website_url")
    if existing:
        fact = _fact(existing, company, "TRUSTED_COMPANY_RECORD", existing, source_type="COMPANY_RECORD")
        if fact:
            facts.append(fact)
        else:
            rejected.append({"url": existing, "reason": "BLOCKED_OR_INVALID_DOMAIN"})

    if parsed is not None:
        evidence = list(parsed.company_website_evidence)
        if parsed.company_url and not evidence:
            evidence.append({
                "url": parsed.company_url, "company_name": parsed.company_name,
                "source": parsed.evidence_sources.get("company_url", "provider_company_url"),
            })
        for item in evidence:
            evidence_company = str(item.get("company_name") or parsed.company_name or "").strip()
            if (
                not evidence_company
                or normalize_company_identity(evidence_company) != company_key
            ):
                rejected.append({"url": item.get("url", ""), "reason": "EMPLOYER_IDENTITY_MISMATCH"})
                continue
            fact = _fact(
                str(item.get("url") or ""), evidence_company,
                _method(str(item.get("source") or "")), parsed.canonical_url,
                source_type="ATS_STRUCTURED_PAYLOAD",
            )
            if fact:
                facts.append(fact)
            else:
                rejected.append({"url": item.get("url", ""), "reason": "BLOCKED_OR_INVALID_DOMAIN"})

    if redirected_candidate:
        original = _value(redirected_candidate, "original_url")
        final = _value(redirected_candidate, "final_url")
        identities = tuple(redirected_candidate.get("page_identities") or ())
        identity_match = any(normalize_company_identity(item) == company_key for item in identities)
        if identity_match:
            fact = _fact(
                original, company, "BOUNDED_PUBLIC_RESOLUTION", original,
                final_url=final, redirect_chain=redirected_candidate.get("redirect_chain") or (),
                source_type="BOUNDED_PUBLIC_RESOLUTION",
            )
            if fact:
                facts.append(fact)
            else:
                rejected.append({"url": final, "reason": "REDIRECT_BLOCKED_DOMAIN"})
        else:
            rejected.append({"url": final, "reason": "REDIRECT_EMPLOYER_MISMATCH"})

    deduped = {}
    for fact in facts:
        key = (fact["canonical_domain"], fact["evidence_method"], fact["source_url"])
        deduped[key] = fact
    facts = list(deduped.values())
    domains = {fact["canonical_domain"] for fact in facts}
    evidence = tuple([*facts, *rejected])
    if len(domains) > 1:
        return WebsiteResolution(
            int(row["job_id"]), "CONFLICT", company, "", "", "", "", "", (),
            "CONFLICTING_STRONG_COMPANY_DOMAINS", "MULTIPLE", ("WEBSITE_DOMAIN_CONFLICT",),
            evidence, resolved_at=timestamp,
        )
    if facts:
        selected = min(facts, key=lambda item: METHOD_RANK.get(item["evidence_method"], 99))
        return WebsiteResolution(
            int(row["job_id"]), "CONFIRMED", company, selected["final_url"],
            selected["canonical_domain"], selected["source_url"],
            selected["original_candidate_url"], selected["final_url"],
            tuple(selected["redirect_chain"]), selected["evidence_method"],
            selected["source_type"], ("WEBSITE_EMPLOYER_IDENTITY_CONFIRMED",),
            evidence, resolved_at=timestamp,
        )
    return WebsiteResolution(
        int(row["job_id"]), "UNKNOWN", company, "", "", "", "", "", (),
        "INSUFFICIENT_OFFICIAL_WEBSITE_EVIDENCE", "NONE",
        ("WEBSITE_EVIDENCE_INSUFFICIENT",), evidence, resolved_at=timestamp,
    )


def _select_rows(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None,
    run_id: str | None, include_review: bool,
) -> list[sqlite3.Row]:
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
    statuses = ("QUALIFIED", "REVIEW") if include_review else ("QUALIFIED",)
    clauses.append("q.qualification_status IN (" + ",".join("?" for _ in statuses) + ")")
    parameters.extend(statuses)
    return connection.execute(
        """SELECT j.job_id,j.canonical_url,j.title,j.content_hash,j.updated_at,
                  c.canonical_name AS company,c.website_url AS existing_website_url,
                  q.qualification_id,q.qualification_status,q.input_evidence_hash,
                  e.employer_evidence_id,e.employer_status,e.actual_employer,
                  e.input_fingerprint AS employer_fingerprint
           FROM jobs j
           JOIN job_qualifications q ON q.qualification_id=(
               SELECT q2.qualification_id FROM job_qualifications q2 WHERE q2.job_id=j.job_id
               ORDER BY q2.qualification_id DESC LIMIT 1)
           LEFT JOIN job_employer_evidence e ON e.employer_evidence_id=(
               SELECT e2.employer_evidence_id FROM job_employer_evidence e2 WHERE e2.job_id=j.job_id
               ORDER BY e2.employer_evidence_id DESC LIMIT 1)
           LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _sources(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT job_source_id,provider,source_job_id,source_url,apply_url,fetch_status,
                  fetch_error,raw_content_hash,source_type,employer_relationship,last_fetched_at
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""", (job_id,),
    ).fetchall()


def _fingerprint(row: Mapping, sources: Sequence[Mapping]) -> str:
    payload = {"job": dict(row), "sources": [dict(source) for source in sources]}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> WebsiteResolution:
    return WebsiteResolution(
        row["job_id"], row["status"], row["company_name"], row["website_url"] or "",
        row["canonical_domain"] or "", row["source_url"] or "",
        row["original_candidate_url"] or "", row["final_url"] or "",
        tuple(json.loads(row["redirect_chain_json"])), row["evidence_method"],
        row["source_type"], tuple(json.loads(row["reason_codes_json"])),
        tuple(json.loads(row["evidence_json"])), row["http_status"], row["fetch_result"],
        row["fetch_error"] or "", bool(row["network_used"]), row["input_fingerprint"],
        row["resolved_at"], True,
    )


def _persist(connection: sqlite3.Connection, result: WebsiteResolution) -> None:
    connection.execute(
        """INSERT INTO job_company_websites
           (job_id,policy_version,status,company_name,website_url,canonical_domain,source_url,
            original_candidate_url,final_url,redirect_chain_json,evidence_method,source_type,
            reason_codes_json,evidence_json,http_status,fetch_result,fetch_error,network_used,
            input_fingerprint,resolved_at,created_at,updated_at)
           VALUES (?,?,?,?,NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),
                   ?,?,?,?, ?,?,?,NULLIF(?,''),?,?,?,?,?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             status=excluded.status,company_name=excluded.company_name,website_url=excluded.website_url,
             canonical_domain=excluded.canonical_domain,source_url=excluded.source_url,
             original_candidate_url=excluded.original_candidate_url,final_url=excluded.final_url,
             redirect_chain_json=excluded.redirect_chain_json,evidence_method=excluded.evidence_method,
             source_type=excluded.source_type,reason_codes_json=excluded.reason_codes_json,
             evidence_json=excluded.evidence_json,http_status=excluded.http_status,
             fetch_result=excluded.fetch_result,fetch_error=excluded.fetch_error,
             network_used=excluded.network_used,input_fingerprint=excluded.input_fingerprint,
             resolved_at=excluded.resolved_at,updated_at=excluded.updated_at""",
        (result.job_id, WEBSITE_POLICY_VERSION, result.status, result.company_name,
         result.website_url, result.canonical_domain, result.source_url,
         result.original_candidate_url, result.final_url,
         json.dumps(result.redirect_chain, separators=(",", ":")), result.evidence_method,
         result.source_type, json.dumps(result.reason_codes, separators=(",", ":")),
         json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         result.http_status, result.fetch_result, result.fetch_error, int(result.network_used),
         result.input_fingerprint, result.resolved_at, result.resolved_at, result.resolved_at),
    )


def resolve_company_websites(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, include_review: bool = False, timeout: int = 15,
    browser_fallback: bool = False, verbose: bool = False, fetcher=fetch_job,
    network_relay=None, driver_factory=None, browser_fetcher=None,
) -> list[WebsiteResolution]:
    rows = _select_rows(connection, job_ids, run_id, include_review)
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results, driver = [], None
    try:
        for row in rows:
            sources = _sources(connection, row["job_id"])
            fingerprint = _fingerprint(row, sources)
            existing = connection.execute(
                """SELECT * FROM job_company_websites
                   WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
                (row["job_id"], WEBSITE_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                results.append(_from_stored(existing))
                continue
            result = evaluate_website(row, sources)
            fetch_result, fetch_error, network_used = "NOT_REQUIRED", "", False
            if result.status == "UNKNOWN":
                authoritative = choose_authoritative_source(sources)
                if authoritative:
                    network_used = True
                    parsed = None
                    try:
                        parsed = relay.protect(
                            lambda: fetcher(authoritative.url, timeout=timeout),
                            context=f"company website resolution job {row['job_id']}",
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
                                driver = relay.protect(lambda: driver_factory(windowed=False), context="website browser startup")
                            browser_result = relay.protect(
                                lambda: browser_fetcher(authoritative.url, timeout, driver),
                                context=f"company website browser job {row['job_id']}",
                            )
                            if browser_result is not None:
                                parsed, fetch_result, fetch_error = browser_result, "BROWSER_FETCHED", ""
                        except Exception as error:
                            fetch_error = f"{type(error).__name__}: {error}"
                    if parsed.fetch_status != "FAILED":
                        result = evaluate_website(row, sources, parsed, resolved_at=result.resolved_at)
            result = replace(
                result, fetch_result=fetch_result, fetch_error=fetch_error,
                network_used=network_used, input_fingerprint=fingerprint,
            )
            with connection:
                _persist(connection, result)
            results.append(result)
            if verbose:
                print(
                    f"{result.job_id}: {result.status} {result.website_url or '-'} "
                    f"({result.evidence_method}; network={'yes' if result.network_used else 'no'})"
                )
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[WebsiteResolution]) -> None:
    counts = Counter(result.status for result in results)
    print(f"Jobs selected: {len(results)}")
    for status in WEBSITE_STATUSES:
        print(f"{status}: {counts[status]}")
    for result in results:
        print(f"\njob_id: {result.job_id}")
        print(f"company: {result.company_name}")
        print(f"status: {result.status}")
        print(f"website_url: {result.website_url or '-'}")
        print(f"canonical_domain: {result.canonical_domain or '-'}")
        print(f"evidence_method: {result.evidence_method}")
        print(f"source_url: {result.source_url or '-'}")
        print(f"network_used: {'yes' if result.network_used else 'no'}")
        print("reason_codes: " + ", ".join(result.reason_codes))
        if result.fetch_error:
            print(f"fetch_error: {result.fetch_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic official company website resolution")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--include-review", action="store_true")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--browser-fallback", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_company_websites(
                connection, args.job_id, args.run_id, args.include_review, args.timeout,
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
