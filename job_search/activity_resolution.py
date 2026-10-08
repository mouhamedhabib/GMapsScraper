"""Resolve job activity from authoritative, job-specific posting evidence."""

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

from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.normalization import normalize_job_url
from job_search.providers import (
    ATS_PROVIDERS, ParsedJob, classify_source_context, detect_provider,
    extract_source_job_id, fetch_job, generic_listing_reason,
)
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


ACTIVITY_POLICY_VERSION = "activity-evidence-v1"
ACTIVITY_STATUSES = ("ACTIVE", "INACTIVE", "UNKNOWN")
SOURCE_RANK = {
    "DIRECT_COMPANY": 0, "ATS": 1, "RECRUITER": 2,
    "JOB_PLATFORM": 3, "UNKNOWN": 4,
}
SUCCESS_FETCH_STATUSES = {"FETCHED", "SUCCESS", "BROWSER_FETCHED"}
INACTIVE_JOB_STATUSES = {"CLOSED", "REMOVED"}
TEMPORARY_MARKERS = (
    "403", "401", "429", "captcha", "cloudflare", "challenge", "forbidden",
    "timeout", "timed out", "dns", "name resolution", "connection reset",
    "connection refused", "temporary", "temporarily", "500", "502", "503", "504",
)
_CLOSED_PATTERN = re.compile(
    r"\b(?:this (?:job|position|vacancy|posting) (?:is |has been )?(?:closed|expired|"
    r"removed|filled|no longer available)|applications? (?:are|is) closed|"
    r"job posting has expired|position has been filled|posting no longer exists|"
    r"poste n['’]est plus disponible|offre (?:est )?expir[ée]e?|poste (?:est )?pourvu)\b",
    re.I,
)


@dataclass(frozen=True)
class AuthoritativeSource:
    url: str
    source_type: str
    provider: str
    job_source_id: int
    is_source_url: bool
    source_job_id: str
    fetch_status: str
    fetch_error: str
    raw_content_hash: str


@dataclass(frozen=True)
class ActivityResolution:
    job_id: int
    activity_status: str
    authoritative_url: str
    source_type: str
    provider: str
    http_status: int | None
    evidence_method: str
    raw_evidence_summary: str
    evidence: dict
    fetch_status: str
    fetch_error: str
    checked_at: str
    reused: bool = False


def _source_classification(source: Mapping, url: str) -> tuple[str, str]:
    provider = detect_provider(url)
    stored_relationship = str(source["employer_relationship"] or "").upper()
    stored_type = str(source["source_type"] or "").upper()
    context = classify_source_context(url, provider)
    is_original_source = normalize_job_url(source["source_url"] or "") == url
    if stored_relationship == "DIRECT" and stored_type == "COMPANY_SITE":
        return "DIRECT_COMPANY", provider
    if is_original_source and stored_relationship == "RECRUITER":
        return "RECRUITER", provider
    if is_original_source and (
        stored_relationship == "AGGREGATOR" or stored_type == "JOB_PLATFORM"
    ):
        return "JOB_PLATFORM", provider
    if provider in ATS_PROVIDERS or context.source_type == "ATS":
        return "ATS", provider
    if stored_type == "COMPANY_SITE" or context.source_type == "COMPANY_SITE":
        return "DIRECT_COMPANY", provider
    return "UNKNOWN", provider


def choose_authoritative_source(sources: Sequence[Mapping]) -> AuthoritativeSource | None:
    """Prefer persisted official company/ATS URLs without inventing a URL."""
    candidates: list[tuple[int, int, int, int, str, AuthoritativeSource]] = []
    for source in sources:
        for is_source_url, field in ((True, "source_url"), (False, "apply_url")):
            url = normalize_job_url(source[field] or "")
            if not url:
                continue
            source_type, provider = _source_classification(source, url)
            candidate = AuthoritativeSource(
                url=url, source_type=source_type, provider=provider,
                job_source_id=int(source["job_source_id"]),
                is_source_url=is_source_url,
                source_job_id=(str(source["source_job_id"] or "") if is_source_url else "")
                or extract_source_job_id(provider, url),
                fetch_status=str(source["fetch_status"] or "") if is_source_url else "",
                fetch_error=str(source["fetch_error"] or "") if is_source_url else "",
                raw_content_hash=str(source["raw_content_hash"] or "") if is_source_url else "",
            )
            candidates.append((
                0 if _individual_url(candidate) else 1,
                SOURCE_RANK[source_type], 0 if is_source_url else 1,
                candidate.job_source_id, candidate.url, candidate,
            ))
    return min(candidates, key=lambda item: item[:5])[-1] if candidates else None


def _http_status(error: str) -> int | None:
    match = re.search(r"(?<!\d)([1-5]\d\d)(?!\d)", error or "")
    return int(match.group(1)) if match else None


def _individual_url(source: AuthoritativeSource) -> bool:
    if source.provider in ATS_PROVIDERS:
        return bool(source.source_job_id or extract_source_job_id(source.provider, source.url))
    path = [part.casefold() for part in urlsplit(source.url).path.split("/") if part]
    marker_indexes = [index for index, part in enumerate(path) if part in {"job", "jobs", "career", "careers", "position", "positions"}]
    has_identity_segment = any(index + 1 < len(path) for index in marker_indexes)
    return has_identity_segment and not generic_listing_reason("", source.url, page_fetched=False)


def _result(
    job_id: int, status: str, source: AuthoritativeSource | None,
    method: str, summary: str, *, http_status: int | None = None,
    fetch_status: str = "NOT_REQUIRED", fetch_error: str = "",
    checked_at: str | None = None, details: dict | None = None,
) -> ActivityResolution:
    timestamp = checked_at or utc_now()
    evidence = {
        "job_id": job_id, "activity_status": status,
        "authoritative_url": source.url if source else "",
        "source_type": source.source_type if source else "UNKNOWN",
        "provider": source.provider if source else "generic",
        "http_status": http_status, "evidence_method": method,
        "raw_evidence_summary": summary, "checked_at": timestamp,
    }
    if details:
        evidence.update(details)
    return ActivityResolution(
        job_id=job_id, activity_status=status,
        authoritative_url=source.url if source else "",
        source_type=source.source_type if source else "UNKNOWN",
        provider=source.provider if source else "generic",
        http_status=http_status, evidence_method=method,
        raw_evidence_summary=summary, evidence=evidence,
        fetch_status=fetch_status, fetch_error=fetch_error, checked_at=timestamp,
    )


def evaluate_persisted_activity(
    row: Mapping, source: AuthoritativeSource | None,
    prior_qualification: Mapping | None = None,
) -> ActivityResolution:
    """Resolve authoritative stored evidence before considering network work."""
    job_id = int(row["job_id"])
    status = str(row["job_status"] or "").upper()
    if status in INACTIVE_JOB_STATUSES:
        return _result(
            job_id, "INACTIVE", source, "PERSISTED_JOB_STATUS",
            f"Stored job status is {status}.",
        )
    if source:
        code = _http_status(source.fetch_error)
        if code in {404, 410}:
            return _result(
                job_id, "INACTIVE", source, "PERSISTED_AUTHORITATIVE_HTTP_STATUS",
                f"Authoritative individual posting returned HTTP {code}.",
                http_status=code, fetch_status=source.fetch_status or "FAILED",
                fetch_error=source.fetch_error,
            )
        if _CLOSED_PATTERN.search(source.fetch_error):
            return _result(
                job_id, "INACTIVE", source, "PERSISTED_PROVIDER_CLOSED_STATUS",
                source.fetch_error[:500], fetch_status=source.fetch_status or "FAILED",
                fetch_error=source.fetch_error,
            )
        persisted_closed = _CLOSED_PATTERN.search(
            " ".join((str(row["title"] or ""), str(row["description"] or "")))
        )
        if persisted_closed and source.is_source_url:
            return _result(
                job_id, "INACTIVE", source, "PERSISTED_EXPLICIT_CLOSED_STATE",
                persisted_closed.group(0)[:500], fetch_status=source.fetch_status,
            )
        has_job_content = bool(str(row["title"] or "").strip() and str(row["description"] or "").strip())
        if (
            source.is_source_url and source.fetch_status.upper() in SUCCESS_FETCH_STATUSES
            and _individual_url(source) and has_job_content
            and source.source_type in {"DIRECT_COMPANY", "ATS"}
        ):
            return _result(
                job_id, "ACTIVE", source, "PERSISTED_AUTHORITATIVE_INDIVIDUAL_FETCH",
                "Successful authoritative individual posting fetch with stored title and description.",
                fetch_status=source.fetch_status,
                details={"source_job_id": source.source_job_id,
                         "raw_content_hash": source.raw_content_hash},
            )
    if (
        prior_qualification and source
        and str(prior_qualification["activity_status"] or "") == "ACTIVE"
        and not source.fetch_error
        and source.source_type in {"DIRECT_COMPANY", "ATS"}
        and _individual_url(source)
    ):
        try:
            prior_evidence = json.loads(prior_qualification["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            prior_evidence = {}
        verification = prior_evidence.get("verification", {})
        if (
            prior_evidence.get("activity_evidence") == "verification_individual_posting"
            and str(verification.get("fetch_status") or "").upper() in SUCCESS_FETCH_STATUSES
        ):
            return _result(
                job_id, "ACTIVE", source, "EXISTING_QUALIFICATION_ACTIVITY_EVIDENCE",
                "Existing qualification recorded an authoritative individual verification.",
            )
    return _result(
        job_id, "UNKNOWN", source, "INSUFFICIENT_PERSISTED_ACTIVITY_EVIDENCE",
        "No authoritative persisted active or inactive state was available.",
    )


def evaluate_fetched_activity(
    job_id: int, source: AuthoritativeSource, parsed: ParsedJob,
    checked_at: str | None = None,
) -> ActivityResolution:
    error = parsed.fetch_error or ""
    code = _http_status(error)
    fetch_status = parsed.fetch_status or "UNKNOWN"
    parsed_url = normalize_job_url(parsed.canonical_url)
    if parsed_url and parsed_url != normalize_job_url(source.url):
        return _result(
            job_id, "UNKNOWN", source, "AMBIGUOUS_REDIRECT",
            f"Fetch resolved to a different URL: {parsed_url}",
            fetch_status=fetch_status, fetch_error=error, checked_at=checked_at,
        )
    if code in {404, 410}:
        return _result(
            job_id, "INACTIVE", source, "AUTHORITATIVE_HTTP_STATUS",
            f"Authoritative individual posting returned HTTP {code}.",
            http_status=code, fetch_status=fetch_status, fetch_error=error,
            checked_at=checked_at,
        )
    explicit_text = " ".join((parsed.title or "", parsed.description or "", error))
    closed = _CLOSED_PATTERN.search(explicit_text)
    if parsed.status.upper() in INACTIVE_JOB_STATUSES or closed:
        summary = closed.group(0) if closed else f"Provider status: {parsed.status.upper()}"
        return _result(
            job_id, "INACTIVE", source, "AUTHORITATIVE_EXPLICIT_CLOSED_STATE",
            summary[:500], fetch_status=fetch_status, fetch_error=error,
            checked_at=checked_at,
        )
    if code and (code >= 500 or code in {401, 403, 429}):
        return _result(
            job_id, "UNKNOWN", source, "TEMPORARY_OR_PROTECTED_FETCH_FAILURE",
            error[:500] or f"HTTP {code}", http_status=code,
            fetch_status=fetch_status, fetch_error=error, checked_at=checked_at,
        )
    if fetch_status.upper() not in SUCCESS_FETCH_STATUSES:
        method = "TEMPORARY_OR_PROTECTED_FETCH_FAILURE" if any(
            marker in error.casefold() for marker in TEMPORARY_MARKERS
        ) else "INCONCLUSIVE_AUTHORITATIVE_FETCH"
        return _result(
            job_id, "UNKNOWN", source, method, error[:500] or "Fetch did not confirm posting activity.",
            http_status=code, fetch_status=fetch_status, fetch_error=error,
            checked_at=checked_at,
        )
    has_identity = bool(parsed.title.strip() and (
        parsed.description.strip() or parsed.has_structured_job_posting or parsed.apply_url
    ))
    if source.provider in ATS_PROVIDERS:
        individual = bool(parsed.source_job_id or source.source_job_id)
    else:
        individual = parsed.has_structured_job_posting or (
            _individual_url(source) and not generic_listing_reason(
                parsed.title, source.url, parsed.description, page_fetched=True,
                has_structured_job_posting=False,
            )
        )
    if individual and has_identity:
        method = (
            "AUTHORITATIVE_STRUCTURED_JOB_POSTING"
            if parsed.has_structured_job_posting else "AUTHORITATIVE_INDIVIDUAL_POSTING"
        )
        return _result(
            job_id, "ACTIVE", source, method,
            "Authoritative individual posting loaded with job-specific structured content."
            if parsed.has_structured_job_posting else
            "Authoritative individual posting loaded with job-specific title and content.",
            fetch_status=fetch_status, checked_at=checked_at,
            details={"source_job_id": parsed.source_job_id or source.source_job_id,
                     "structured_job_posting": parsed.has_structured_job_posting},
        )
    return _result(
        job_id, "UNKNOWN", source, "NON_INDIVIDUAL_OR_AMBIGUOUS_POSTING",
        "Loaded page did not prove an active individual job posting.",
        fetch_status=fetch_status, checked_at=checked_at,
    )


def _safe_url(url: str) -> bool:
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
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
        """SELECT j.job_id,j.canonical_url,j.title,j.description,j.status AS job_status,
                  j.content_hash,j.updated_at
           FROM jobs j WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _sources(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT job_source_id,provider,source_job_id,source_url,apply_url,
                  fetch_status,fetch_error,last_fetched_at,raw_content_hash,
                  source_type,employer_relationship
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""", (job_id,),
    ).fetchall()


def _prior_qualification(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT policy_version,activity_status,evidence_json,input_evidence_hash
           FROM job_qualifications WHERE job_id=? ORDER BY qualification_id DESC LIMIT 1""",
        (job_id,),
    ).fetchone()


def _fingerprint(row: Mapping, sources: Sequence[Mapping], prior: Mapping | None) -> str:
    payload = {
        "job": dict(row), "sources": [dict(source) for source in sources],
        "prior_qualification": dict(prior) if prior else None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> ActivityResolution:
    return ActivityResolution(
        job_id=row["job_id"], activity_status=row["activity_status"],
        authoritative_url=row["authoritative_url"] or "", source_type=row["source_type"],
        provider=row["provider"], http_status=row["http_status"],
        evidence_method=row["evidence_method"], raw_evidence_summary=row["raw_evidence_summary"],
        evidence=json.loads(row["evidence_json"]), fetch_status=row["fetch_status"],
        fetch_error=row["fetch_error"] or "", checked_at=row["checked_at"], reused=True,
    )


def _persist(connection: sqlite3.Connection, result: ActivityResolution, fingerprint: str):
    evidence = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_activity_evidence
           (job_id,policy_version,activity_status,authoritative_url,source_type,provider,
            http_status,evidence_method,raw_evidence_summary,evidence_json,fetch_status,
            fetch_error,input_fingerprint,checked_at,created_at,updated_at)
           VALUES (?, ?, ?, NULLIF(?,''), ?, ?, ?, ?, ?, ?, ?, NULLIF(?,''), ?, ?, ?, ?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             activity_status=excluded.activity_status,authoritative_url=excluded.authoritative_url,
             source_type=excluded.source_type,provider=excluded.provider,http_status=excluded.http_status,
             evidence_method=excluded.evidence_method,raw_evidence_summary=excluded.raw_evidence_summary,
             evidence_json=excluded.evidence_json,fetch_status=excluded.fetch_status,
             fetch_error=excluded.fetch_error,input_fingerprint=excluded.input_fingerprint,
             checked_at=excluded.checked_at,updated_at=excluded.updated_at""",
        (result.job_id, ACTIVITY_POLICY_VERSION, result.activity_status,
         result.authoritative_url, result.source_type, result.provider, result.http_status,
         result.evidence_method, result.raw_evidence_summary, evidence, result.fetch_status,
         result.fetch_error, fingerprint, result.checked_at, result.checked_at, result.checked_at),
    )


def resolve_activity(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, timeout: int = 15, browser_fallback: bool = False,
    verbose: bool = False, fetcher=fetch_job, network_relay=None,
    driver_factory=None, browser_fetcher=None,
) -> list[ActivityResolution]:
    rows = _select_rows(connection, job_ids, run_id)
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results, driver = [], None
    try:
        for row in rows:
            sources = _sources(connection, int(row["job_id"]))
            prior = _prior_qualification(connection, int(row["job_id"]))
            fingerprint = _fingerprint(row, sources, prior)
            existing = connection.execute(
                """SELECT * FROM job_activity_evidence
                   WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
                (row["job_id"], ACTIVITY_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                result = _from_stored(existing)
                results.append(result)
                if verbose: print(f"{result.job_id}: reused unchanged activity ({result.activity_status})")
                continue
            source = choose_authoritative_source(sources)
            result = evaluate_persisted_activity(row, source, prior)
            if (
                result.activity_status == "UNKNOWN" and source
                and _individual_url(source) and _safe_url(source.url)
            ):
                parsed = None
                try:
                    parsed = relay.protect(
                        lambda: fetcher(source.url, timeout=timeout),
                        context=f"activity resolution job {row['job_id']}",
                    )
                except NetworkPauseExceeded as error:
                    parsed = ParsedJob(source.url, source.provider, source.source_job_id)
                    parsed.fetch_status = "FAILED"
                    parsed.fetch_error = f"NETWORK/{error.error_type}: {error}"
                except Exception as error:
                    parsed = ParsedJob(source.url, source.provider, source.source_job_id)
                    parsed.fetch_status = "FAILED"
                    parsed.fetch_error = f"{type(error).__name__}: {error}"
                if parsed.fetch_status == "FAILED" and browser_fallback:
                    try:
                        if browser_fetcher is None:
                            from job_search.repair_reviews import _browser_fetch
                            browser_fetcher = _browser_fetch
                        if driver is None:
                            if driver_factory is None:
                                from utils.google_search_discovery import create_chrome_driver
                                driver_factory = create_chrome_driver
                            driver = relay.protect(
                                lambda: driver_factory(windowed=False),
                                context="activity browser startup",
                            )
                        browser_result = relay.protect(
                            lambda: browser_fetcher(source.url, timeout, driver),
                            context=f"activity browser job {row['job_id']}",
                        )
                        if browser_result is not None:
                            parsed = browser_result
                            parsed.fetch_status = "BROWSER_FETCHED"
                    except Exception as error:
                        parsed.fetch_error = f"{type(error).__name__}: {error}"
                result = evaluate_fetched_activity(
                    int(row["job_id"]), source, parsed, checked_at=result.checked_at,
                )
            with connection:
                _persist(connection, result, fingerprint)
            results.append(result)
            if verbose:
                print(f"{result.job_id}: {result.activity_status} | {result.evidence_method} | {result.authoritative_url or '-'}")
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[ActivityResolution]):
    counts = Counter(result.activity_status for result in results)
    print(f"Jobs selected: {len(results)}")
    for status in ACTIVITY_STATUSES: print(f"{status}: {counts[status]}")
    for result in results:
        print(f"\njob_id: {result.job_id}")
        print(f"activity_status: {result.activity_status}")
        print(f"authoritative_url: {result.authoritative_url or '-'}")
        print(f"source_type: {result.source_type}")
        print(f"provider: {result.provider}")
        print(f"http_status: {result.http_status or '-'}")
        print(f"evidence_method: {result.evidence_method}")
        print(f"raw_evidence_summary: {result.raw_evidence_summary}")


def build_parser():
    parser = argparse.ArgumentParser(description="Deterministic job activity evidence resolution")
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
            results = resolve_activity(
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
