"""Resolve safe application destinations from deterministic posting evidence."""

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

from job_search.activity_resolution import choose_authoritative_source
from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.normalization import normalize_job_url
from job_search.providers import (
    ATS_PROVIDERS, ParsedJob, detect_provider, extract_source_job_id, fetch_job,
    generic_listing_reason,
)
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


APPLICATION_POLICY_VERSION = "application-destination-v1"
APPLICATION_STATUSES = ("CONFIRMED", "UNKNOWN", "CONFLICT", "UNAVAILABLE")
APPLICATION_CHANNELS = ("DIRECT_COMPANY", "ATS", "RECRUITER", "JOB_PLATFORM", "UNKNOWN")
CHANNEL_RANK = {channel: rank for rank, channel in enumerate(APPLICATION_CHANNELS)}
SUCCESS_FETCH_STATUSES = {"FETCHED", "SUCCESS", "BROWSER_FETCHED"}
TEMPORARY_MARKERS = (
    "403", "401", "429", "captcha", "cloudflare", "challenge", "forbidden",
    "timeout", "timed out", "dns", "name resolution", "connection reset",
    "connection refused", "500", "502", "503", "504",
)
UNAVAILABLE_PATTERN = re.compile(
    r"\b(?:applications? (?:are|is) closed|no longer accepting applications|"
    r"application (?:is )?unavailable|position has been filled|poste (?:est )?pourvu)\b",
    re.I,
)


@dataclass(frozen=True)
class DestinationCandidate:
    raw_url: str
    canonical_url: str
    source: str
    method: str
    channel: str
    provider: str
    source_type: str
    employer_relationship: str
    accepted: bool
    reason: str
    rank: int
    endpoint_rank: int

    def evidence(self, timestamp: str) -> dict:
        return {
            "raw_url": self.raw_url,
            "canonical_url": self.canonical_url,
            "source": self.source,
            "method": self.method,
            "channel": self.channel,
            "provider": self.provider,
            "source_type": self.source_type,
            "employer_relationship": self.employer_relationship,
            "reason": self.reason,
            "accepted": self.accepted,
            "timestamp": timestamp,
        }


@dataclass(frozen=True)
class ApplicationDestination:
    job_id: int
    application_status: str
    application_channel: str
    application_url: str
    provider: str
    source_type: str
    employer_relationship: str
    authoritative_source_url: str
    http_status: int | None
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


def _http_status(error: str) -> int | None:
    match = re.search(r"(?:http(?:error)?|status(?: code)?)\s*[:=]?\s*([1-5]\d\d)", error or "", re.I)
    if not match:
        match = re.search(r"\b(401|403|404|410|429|500|502|503|504)\b", error or "")
    return int(match.group(1)) if match else None


def _safe_url(url: str) -> bool:
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
        return not (address.is_private or address.is_loopback or address.is_link_local)
    except ValueError:
        return True


def _individual_ats(url: str, provider: str) -> bool:
    return provider in ATS_PROVIDERS and bool(extract_source_job_id(provider, url))


def _generic_path_state(url: str, title: str, description: str, *, fetched: bool, structured: bool) -> tuple[bool, str]:
    parsed = urlsplit(url)
    parts = [part.casefold() for part in parsed.path.split("/") if part]
    if not parts or parts[-1] in {"jobs", "job", "careers", "career", "search"}:
        return False, "GENERIC_CAREERS_OR_LISTING_PAGE"
    if any(part in {"search", "job-search", "search-jobs", "listings", "vacancies"} for part in parts):
        return False, "GENERIC_CAREERS_OR_LISTING_PAGE"
    if generic_listing_reason(
        title, url, description, page_fetched=fetched,
        has_structured_job_posting=structured,
    ):
        return False, "GENERIC_JOB_LISTING_PAGE"
    markers = {"job", "jobs", "career", "careers", "position", "positions"}
    individual = any(part in markers and index + 1 < len(parts) for index, part in enumerate(parts))
    return (individual, "INDIVIDUAL_JOB_PATH" if individual else "NON_INDIVIDUAL_PATH")


def _hosted_portfolio_job(url: str) -> bool:
    parsed = urlsplit(url)
    parts = [part.casefold() for part in parsed.path.split("/") if part]
    return (
        (parsed.hostname or "").casefold() == "careers.franciscopartners.com"
        and len(parts) >= 4 and parts[0] == "companies" and parts[2] == "jobs"
    )


def _same_recruiter_surface(source: Mapping, url: str, provider: str) -> bool:
    original = normalize_job_url(_value(source, "source_url"))
    return bool(
        _value(source, "employer_relationship").upper() == "RECRUITER"
        and provider == detect_provider(original)
        and urlsplit(url).hostname == urlsplit(original).hostname
    )


def classify_candidate(
    row: Mapping, source: Mapping, raw_url: str, field: str, *,
    fetched: bool = False, structured: bool = False, source_label: str = "",
) -> DestinationCandidate:
    canonical = normalize_job_url(raw_url)
    stored_type = _value(source, "source_type").upper() or "UNKNOWN"
    relationship = _value(source, "employer_relationship").upper() or "UNKNOWN"
    provider = detect_provider(canonical) if canonical else "generic"
    label = source_label or f"job_sources[{_value(source, 'job_source_id')}].{field}"
    is_apply = field == "apply_url"
    channel, method, accepted, reason = "UNKNOWN", "REJECTED_CANDIDATE", False, "MALFORMED_URL"

    if canonical:
        ats_individual = _individual_ats(canonical, provider)
        source_url = normalize_job_url(_value(source, "source_url"))
        source_provider = detect_provider(source_url)
        source_specific = _individual_ats(source_url, source_provider)
        if not source_specific and source_url:
            source_specific = _generic_path_state(
                source_url, _value(row, "title"), _value(row, "description"),
                fetched=_value(source, "fetch_status").upper() in SUCCESS_FETCH_STATUSES,
                structured=False,
            )[0]
        generic_individual, generic_reason = _generic_path_state(
            canonical, _value(row, "title"), _value(row, "description"),
            fetched=fetched or _value(source, "fetch_status").upper() in SUCCESS_FETCH_STATUSES,
            structured=structured,
        ) if provider == "generic" else (False, "")

        if ats_individual:
            channel = "RECRUITER" if _same_recruiter_surface(source, canonical, provider) else "ATS"
            method = "AUTHORITATIVE_ATS_INDIVIDUAL"
            accepted, reason = True, "SUPPORTED_ATS_INDIVIDUAL_POSTING"
        elif relationship == "DIRECT" and stored_type == "COMPANY_SITE" and (
            (is_apply and source_specific)
            or (
                generic_individual
                and (fetched or _value(source, "fetch_status").upper() in SUCCESS_FETCH_STATUSES)
            )
        ):
            channel, method = "DIRECT_COMPANY", "DIRECT_COMPANY_APPLICATION_ENDPOINT"
            accepted, reason = True, "DIRECT_INDIVIDUAL_APPLICATION_DESTINATION"
        elif relationship == "RECRUITER" and (generic_individual or (is_apply and source_specific)):
            channel, method = "RECRUITER", "RECRUITER_INDIVIDUAL_ENDPOINT"
            accepted, reason = True, "RECRUITER_SPECIFIC_POSTING"
        elif (relationship == "AGGREGATOR" or stored_type == "JOB_PLATFORM") and generic_individual:
            parts = [part.casefold() for part in urlsplit(canonical).path.split("/") if part]
            shallow_role = len(parts) == 2 and parts[0] == "jobs" and not re.search(r"\d", parts[1])
            channel, method = "JOB_PLATFORM", "JOB_PLATFORM_INDIVIDUAL_POSTING"
            accepted, reason = (False, "GENERIC_ROLE_COLLECTION") if shallow_role else (True, "PLATFORM_INDIVIDUAL_POSTING")
        elif _hosted_portfolio_job(canonical) and (
            is_apply or fetched or _value(source, "fetch_status").upper() in SUCCESS_FETCH_STATUSES
        ):
            channel, method = "JOB_PLATFORM", "HOSTED_PORTFOLIO_INDIVIDUAL_POSTING"
            accepted, reason = True, "HOSTED_INDIVIDUAL_APPLICATION_SURFACE"
        else:
            reason = generic_reason or "SOURCE_RELATIONSHIP_OR_INDIVIDUAL_IDENTITY_UNPROVEN"

    return DestinationCandidate(
        raw_url, canonical, label, method, channel, provider,
        "ATS" if provider in ATS_PROVIDERS else (
            "COMPANY_SITE" if channel == "DIRECT_COMPANY" else
            "JOB_PLATFORM" if channel == "JOB_PLATFORM" else stored_type
        ),
        relationship, accepted, reason, CHANNEL_RANK[channel],
        0 if is_apply else 1,
    )


def _stored_candidates(row: Mapping, sources: Sequence[Mapping]) -> list[DestinationCandidate]:
    candidates = []
    for source in sources:
        for field in ("apply_url", "source_url"):
            raw_url = _value(source, field)
            if raw_url:
                candidates.append(classify_candidate(row, source, raw_url, field))
    return candidates


def _resolution(
    row: Mapping, candidates: Sequence[DestinationCandidate], timestamp: str,
    *, authoritative_source_url: str = "", http_status: int | None = None,
    fetch_result: str = "NOT_REQUIRED", fetch_error: str = "",
    unavailable: bool = False,
) -> ApplicationDestination:
    evidence = tuple(candidate.evidence(timestamp) for candidate in candidates)
    accepted = [candidate for candidate in candidates if candidate.accepted]
    if unavailable:
        return ApplicationDestination(
            int(row["job_id"]), "UNAVAILABLE", "UNKNOWN", "", "generic", "UNKNOWN",
            "UNKNOWN", authoritative_source_url, http_status,
            "EXPLICIT_APPLICATION_UNAVAILABLE", evidence,
            fetch_result=fetch_result, fetch_error=fetch_error, resolved_at=timestamp,
        )
    if not accepted:
        return ApplicationDestination(
            int(row["job_id"]), "UNKNOWN", "UNKNOWN", "", "generic", "UNKNOWN",
            "UNKNOWN", authoritative_source_url, http_status,
            "INSUFFICIENT_APPLICATION_DESTINATION_EVIDENCE", evidence,
            fetch_result=fetch_result, fetch_error=fetch_error, resolved_at=timestamp,
        )
    best_score = min((candidate.rank, candidate.endpoint_rank) for candidate in accepted)
    strongest = [candidate for candidate in accepted if (candidate.rank, candidate.endpoint_rank) == best_score]
    urls = {candidate.canonical_url for candidate in strongest}
    if len(urls) > 1:
        representative = strongest[0]
        return ApplicationDestination(
            int(row["job_id"]), "CONFLICT", "UNKNOWN", "", representative.provider,
            representative.source_type, representative.employer_relationship,
            authoritative_source_url, http_status,
            "CONFLICTING_APPLICATION_DESTINATIONS", evidence,
            fetch_result=fetch_result, fetch_error=fetch_error, resolved_at=timestamp,
        )
    selected = min(strongest, key=lambda candidate: (candidate.canonical_url, candidate.source))
    return ApplicationDestination(
        int(row["job_id"]), "CONFIRMED", selected.channel, selected.canonical_url,
        selected.provider, selected.source_type, selected.employer_relationship,
        authoritative_source_url or selected.canonical_url, http_status,
        selected.method, evidence, fetch_result=fetch_result,
        fetch_error=fetch_error, resolved_at=timestamp,
    )


def evaluate_application_destination(
    row: Mapping, sources: Sequence[Mapping], parsed: ParsedJob | None = None,
    *, resolved_at: str | None = None, fetch_result: str = "NOT_REQUIRED",
    fetch_error: str = "",
) -> ApplicationDestination:
    timestamp = resolved_at or utc_now()
    candidates = _stored_candidates(row, sources)
    authoritative = choose_authoritative_source(sources)
    authoritative_url = authoritative.url if authoritative else ""
    unavailable = False
    status = _http_status(fetch_error or (parsed.fetch_error if parsed else ""))
    if parsed is not None:
        source = next((item for item in sources if authoritative and int(item["job_source_id"]) == authoritative.job_source_id), sources[0] if sources else {})
        fetch_succeeded = parsed.fetch_status.upper() in SUCCESS_FETCH_STATUSES
        if fetch_succeeded:
            final_url = normalize_job_url(parsed.canonical_url)
            if final_url:
                candidates.append(classify_candidate(
                    row, source, final_url, "source_url", fetched=True,
                    structured=parsed.has_structured_job_posting,
                    source_label="bounded_fetch.final_url",
                ))
            if parsed.apply_url:
                candidates.append(classify_candidate(
                    row, source, parsed.apply_url, "apply_url", fetched=True,
                    structured=parsed.has_structured_job_posting,
                    source_label="bounded_fetch.apply_url",
                ))
            unavailable = bool(UNAVAILABLE_PATTERN.search(" ".join((
                parsed.title or "", parsed.description or "", parsed.fetch_error or "",
            ))))
    return _resolution(
        row, candidates, timestamp, authoritative_source_url=authoritative_url,
        http_status=status, fetch_result=fetch_result, fetch_error=fetch_error,
        unavailable=unavailable,
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
        """SELECT j.job_id,j.canonical_url,j.title,j.description,j.content_hash,j.updated_at
           FROM jobs j WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id", parameters,
    ).fetchall()


def _sources(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """SELECT job_source_id,provider,source_job_id,source_url,apply_url,fetch_status,
                  fetch_error,last_fetched_at,raw_content_hash,source_type,employer_relationship
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""", (job_id,),
    ).fetchall()


def _fingerprint(row: Mapping, sources: Sequence[Mapping]) -> str:
    payload = {"job": dict(row), "sources": [dict(source) for source in sources]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> ApplicationDestination:
    return ApplicationDestination(
        row["job_id"], row["application_status"], row["application_channel"],
        row["application_url"] or "", row["provider"], row["source_type"],
        row["employer_relationship"], row["authoritative_source_url"] or "",
        row["http_status"], row["evidence_method"], tuple(json.loads(row["evidence_json"])),
        row["input_fingerprint"], row["fetch_result"], row["fetch_error"] or "",
        row["resolved_at"], True,
    )


def _persist(connection: sqlite3.Connection, result: ApplicationDestination) -> None:
    evidence = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_application_destinations
           (job_id,policy_version,application_status,application_channel,application_url,
            provider,source_type,employer_relationship,authoritative_source_url,http_status,
            evidence_method,evidence_json,input_fingerprint,fetch_result,fetch_error,
            resolved_at,created_at,updated_at)
           VALUES (?,?,?,?,NULLIF(?,''),?,?,?,NULLIF(?,''),?,?,?, ?,?,NULLIF(?,''),?,?,?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             application_status=excluded.application_status,
             application_channel=excluded.application_channel,
             application_url=excluded.application_url,provider=excluded.provider,
             source_type=excluded.source_type,
             employer_relationship=excluded.employer_relationship,
             authoritative_source_url=excluded.authoritative_source_url,
             http_status=excluded.http_status,evidence_method=excluded.evidence_method,
             evidence_json=excluded.evidence_json,input_fingerprint=excluded.input_fingerprint,
             fetch_result=excluded.fetch_result,fetch_error=excluded.fetch_error,
             resolved_at=excluded.resolved_at,updated_at=excluded.updated_at""",
        (result.job_id, APPLICATION_POLICY_VERSION, result.application_status,
         result.application_channel, result.application_url, result.provider,
         result.source_type, result.employer_relationship,
         result.authoritative_source_url, result.http_status, result.evidence_method,
         evidence, result.input_fingerprint, result.fetch_result, result.fetch_error,
         result.resolved_at, result.resolved_at, result.resolved_at),
    )


def resolve_application_destinations(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, timeout: int = 15, browser_fallback: bool = False,
    verbose: bool = False, fetcher=fetch_job, network_relay=None,
    driver_factory=None, browser_fetcher=None,
) -> list[ApplicationDestination]:
    rows = _select_rows(connection, job_ids, run_id)
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results, driver = [], None
    try:
        for row in rows:
            sources = _sources(connection, row["job_id"])
            fingerprint = _fingerprint(row, sources)
            existing = connection.execute(
                """SELECT * FROM job_application_destinations
                   WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
                (row["job_id"], APPLICATION_POLICY_VERSION, fingerprint),
            ).fetchone()
            if existing:
                result = _from_stored(existing)
                results.append(result)
                if verbose:
                    print(f"{result.job_id}: reused unchanged destination ({result.application_status})")
                continue

            result = evaluate_application_destination(row, sources)
            if result.application_status == "UNKNOWN":
                authoritative = choose_authoritative_source(sources)
                if authoritative and _safe_url(authoritative.url):
                    parsed = None
                    fetch_result, fetch_error = "FAILED", ""
                    try:
                        parsed = relay.protect(
                            lambda: fetcher(authoritative.url, timeout=timeout),
                            context=f"application destination job {row['job_id']}",
                        )
                        fetch_result = parsed.fetch_status or "UNKNOWN"
                        fetch_error = parsed.fetch_error or ""
                    except NetworkPauseExceeded as error:
                        fetch_error = f"NETWORK/{error.error_type}: {error}"
                    except Exception as error:
                        fetch_error = f"{type(error).__name__}: {error}"
                    if parsed is None:
                        parsed = ParsedJob(authoritative.url, authoritative.provider, authoritative.source_job_id)
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
                                driver = relay.protect(lambda: driver_factory(windowed=False), context="application browser startup")
                            browser_result = relay.protect(
                                lambda: browser_fetcher(authoritative.url, timeout, driver),
                                context=f"application browser job {row['job_id']}",
                            )
                            if browser_result is not None:
                                parsed, fetch_result, fetch_error = browser_result, "BROWSER_FETCHED", ""
                        except Exception as error:
                            fetch_error = f"{type(error).__name__}: {error}"
                    result = evaluate_application_destination(
                        row, sources, parsed, resolved_at=result.resolved_at,
                        fetch_result=fetch_result, fetch_error=fetch_error,
                    )
            result = replace(result, input_fingerprint=fingerprint)
            with connection:
                _persist(connection, result)
            results.append(result)
            if verbose:
                print(
                    f"{result.job_id}: {result.application_status} "
                    f"{result.application_channel} {result.application_url or '-'}"
                )
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[ApplicationDestination]) -> None:
    counts = Counter(result.application_status for result in results)
    print(f"Jobs selected: {len(results)}")
    for status in APPLICATION_STATUSES:
        print(f"{status}: {counts[status]}")
    for result in results:
        print(f"\njob_id: {result.job_id}")
        print(f"application_status: {result.application_status}")
        print(f"application_channel: {result.application_channel}")
        print(f"application_url: {result.application_url or '-'}")
        print(f"provider: {result.provider}")
        print(f"employer_relationship: {result.employer_relationship}")
        print(f"evidence_method: {result.evidence_method}")
        print(f"fetch_result: {result.fetch_result}")
        if result.fetch_error:
            print(f"fetch_error: {result.fetch_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic application destination resolution")
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
            results = resolve_application_destinations(
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
