"""Deterministic verification of actionable jobs that survived hard filtering."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from job_search.filtering import DEFAULT_POLICY_VERSION
from job_search.geography import normalize_geography
from job_search.network import NetworkPauseExceeded, NetworkProtectionRelay
from job_search.normalization import normalize_job_url
from job_search.providers import (
    ParsedJob, classify_source_context, detect_provider, fetch_job,
    generic_listing_reason,
)
from job_search.review_priority import (
    OBSERVATION_STATUSES, prioritize_review_row,
)
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


QUALIFICATION_STATUSES = ("QUALIFIED", "REVIEW", "DISQUALIFIED")
ACTIVITY_STATUSES = ("ACTIVE", "INACTIVE", "UNKNOWN")
APPLICATION_CHANNELS = (
    "DIRECT_COMPANY", "ATS", "RECRUITER", "JOB_PLATFORM", "UNKNOWN",
)
CHANNEL_RANK = {name: rank for rank, name in enumerate(APPLICATION_CHANNELS)}
ACTIVE_FETCH_STATUSES = {"FETCHED", "SUCCESS"}
INACTIVE_JOB_STATUSES = {"REMOVED", "CLOSED"}
PROTECTION_MARKERS = (
    "403", "captcha", "cloudflare", "verification", "timeout", "timed out",
    "connection", "network/", "target_site_bad",
)
STRONG_COMPANY_EVIDENCE = {
    "jsonld_hiringOrganization", "microdata_hiringOrganization",
    "provider_company_field", "job_company_meta", "direct_domain_company",
    "validated_hosted_tenant_title",
}


@dataclass(frozen=True)
class QualificationResult:
    job_id: int
    title: str
    company: str
    qualification_status: str
    activity_status: str
    employer_status: str
    location: str
    country: str
    region: str
    city: str
    remote_status: str
    source_type: str
    employer_relationship: str
    application_channel: str
    application_url: str
    reason_codes: tuple[str, ...]
    evidence: dict
    qualified_at: str
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _reason_codes(row: Mapping) -> tuple[str, ...]:
    try:
        payload = json.loads(_value(row, "reasons_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        payload = []
    return tuple(
        str(item.get("code") or "").strip()
        for item in payload if isinstance(item, dict) and item.get("code")
    )


def _source_channel(source: Mapping, url: str) -> str:
    source_type = _value(source, "source_type").upper() or "UNKNOWN"
    relationship = _value(source, "employer_relationship").upper() or "UNKNOWN"
    provider = _value(source, "provider") or detect_provider(url)
    if relationship == "DIRECT" and source_type == "COMPANY_SITE":
        return "DIRECT_COMPANY"
    if source_type == "ATS" or provider != "generic":
        return "ATS"
    if relationship == "RECRUITER":
        return "RECRUITER"
    if relationship == "AGGREGATOR" or source_type == "JOB_PLATFORM":
        return "JOB_PLATFORM"
    return "UNKNOWN"


def _application_destination(
    sources: Sequence[Mapping], fetched: ParsedJob | None = None,
) -> tuple[str, str, dict]:
    candidates: list[tuple[int, int, str, str, dict]] = []
    for index, source in enumerate(sources):
        raw_url = _value(source, "apply_url") or _value(source, "source_url")
        url = normalize_job_url(raw_url)
        if not url:
            continue
        channel = _source_channel(source, url)
        if channel != "UNKNOWN":
            candidates.append((CHANNEL_RANK[channel], index, channel, url, dict(source)))
    if fetched and fetched.apply_url:
        url = normalize_job_url(fetched.apply_url)
        context = classify_source_context(url, detect_provider(url), fetched.company_name)
        source = {
            "provider": detect_provider(url), "source_type": context.source_type,
            "employer_relationship": context.employer_relationship,
            "source_url": url, "apply_url": url,
        }
        channel = _source_channel(source, url)
        if channel != "UNKNOWN":
            candidates.append((CHANNEL_RANK[channel], -1, channel, url, source))
    if not candidates:
        return "UNKNOWN", "", {}
    _, _, channel, url, source = min(candidates, key=lambda item: (item[0], item[1], item[3]))
    return channel, url, source


def _stored_activity(row: Mapping, sources: Sequence[Mapping]) -> tuple[str, str]:
    job_status = _value(row, "job_status").upper()
    if job_status in INACTIVE_JOB_STATUSES:
        return "INACTIVE", f"stored_job_status:{job_status}"
    for source in sources:
        error = _value(source, "fetch_error").casefold()
        if "404" in error or "410" in error:
            return "INACTIVE", "stored_http_not_found"
    if job_status == "OPEN" and any(
        _value(source, "fetch_status").upper() in ACTIVE_FETCH_STATUSES
        for source in sources
    ):
        return "ACTIVE", "stored_successful_job_fetch"
    return "UNKNOWN", "stored_activity_insufficient"


def _fetched_activity(parsed: ParsedJob) -> tuple[str, str]:
    if parsed.status.upper() in INACTIVE_JOB_STATUSES:
        return "INACTIVE", f"verification_status:{parsed.status.upper()}"
    error = (parsed.fetch_error or "").casefold()
    if "404" in error or "410" in error:
        return "INACTIVE", "verification_http_not_found"
    if parsed.fetch_status.upper() in ACTIVE_FETCH_STATUSES:
        listing = generic_listing_reason(
            parsed.title, parsed.canonical_url, parsed.description,
            page_fetched=True,
            has_structured_job_posting=parsed.has_structured_job_posting,
        )
        if not listing and (parsed.title or parsed.has_structured_job_posting):
            return "ACTIVE", "verification_individual_posting"
    if any(marker in error for marker in PROTECTION_MARKERS):
        return "UNKNOWN", "verification_protected_or_temporary_failure"
    return "UNKNOWN", "verification_inconclusive"


def _employer_status(
    row: Mapping, sources: Sequence[Mapping], fetched: ParsedJob | None,
) -> tuple[str, str]:
    company = _value(row, "company")
    if fetched and fetched.company_name and fetched.evidence_sources.get(
        "company_name"
    ) in STRONG_COMPANY_EVIDENCE:
        return "CONFIRMED", "verification_company_evidence"
    if not company:
        return "UNKNOWN", "company_missing"
    if any(
        _value(source, "employer_relationship").upper() == "DIRECT"
        or (
            _value(source, "source_type").upper() == "ATS"
            and _value(source, "employer_relationship").upper()
            not in {"RECRUITER", "AGGREGATOR"}
        )
        for source in sources
    ):
        return "CONFIRMED", "stored_company_source_provenance"
    return "UNKNOWN", "company_relationship_unconfirmed"


def _input_payload(row: Mapping, sources: Sequence[Mapping]) -> dict:
    return {
        "job": {name: row[name] for name in (
            "job_id", "canonical_url", "title", "company", "location_text",
            "country", "region", "city", "remote_policy", "job_status",
            "content_hash", "filter_status", "reasons_json", "matched_terms_json",
            "detected_remote_policy",
        )},
        "sources": [dict(source) for source in sources],
    }


def _payload_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _result_from_stored(row: sqlite3.Row, source_row: Mapping) -> QualificationResult:
    evidence = json.loads(row["evidence_json"])
    location = evidence.get("location", {})
    return QualificationResult(
        job_id=row["job_id"], title=_value(source_row, "title"),
        company=_value(source_row, "company"),
        qualification_status=row["qualification_status"],
        activity_status=row["activity_status"], employer_status=row["employer_status"],
        location=_value(source_row, "location_text"),
        country=location.get("country", ""), region=location.get("region", ""),
        city=location.get("city", ""),
        remote_status=location.get("remote_status", "UNKNOWN"),
        source_type=evidence.get("selected_source", {}).get("source_type", "UNKNOWN"),
        employer_relationship=evidence.get("selected_source", {}).get(
            "employer_relationship", "UNKNOWN"
        ),
        application_channel=row["application_channel"],
        application_url=row["application_url"] or "",
        reason_codes=tuple(json.loads(row["reason_codes_json"])),
        evidence=evidence, qualified_at=row["qualified_at"], reused=True,
    )


def _evaluate(
    row: Mapping, sources: Sequence[Mapping], parsed: ParsedJob | None,
    verification_attempted: bool, verification_error: str,
) -> QualificationResult:
    codes = _reason_codes(row)
    canonical = _value(row, "canonical_url")
    listing = generic_listing_reason(
        _value(row, "title"), canonical, _value(row, "description"),
        page_fetched=True,
    )
    activity, activity_evidence = _stored_activity(row, sources)
    if parsed is not None:
        fetched_activity, fetched_evidence = _fetched_activity(parsed)
        if fetched_activity != "UNKNOWN" or activity == "UNKNOWN":
            activity, activity_evidence = fetched_activity, fetched_evidence
        fetched_listing = generic_listing_reason(
            parsed.title, parsed.canonical_url, parsed.description,
            page_fetched=True,
            has_structured_job_posting=parsed.has_structured_job_posting,
        )
        listing = listing or fetched_listing
    employer, employer_evidence = _employer_status(row, sources, parsed)
    channel, application_url, selected_source = _application_destination(sources, parsed)

    geography = normalize_geography(
        _value(row, "location_text"), _value(row, "city"),
        _value(row, "country"), _value(row, "title"),
    )
    normalized_country = geography.country or _value(row, "country")
    normalized_region = geography.region or _value(row, "region")
    normalized_city = _value(row, "city")
    remote_status = (
        _value(row, "remote_policy")
        or _value(row, "detected_remote_policy") or "UNKNOWN"
    )
    location_present = any(_value(row, name) for name in (
        "location_text", "country", "region", "city", "remote_policy",
    ))
    location_eligible = any(code in codes for code in (
        "PASS_LOCATION_TUNISIA", "PASS_REMOTE_WORLDWIDE",
    ))
    query_mismatch = "REJECT_QUERY_LOCATION_MISMATCH" in codes
    reasons: list[str] = []
    if listing:
        reasons.append("DISQUALIFIED_LISTING_PAGE")
    if query_mismatch:
        reasons.append("DISQUALIFIED_QUERY_MISMATCH")
    if activity == "INACTIVE":
        reasons.append("DISQUALIFIED_POSTING_INACTIVE")
    elif activity == "ACTIVE":
        reasons.append("QUALIFIED_ACTIVE_POSTING")
    else:
        reasons.append("REVIEW_ACTIVITY_UNKNOWN")
    if "PASS_RELEVANT_ROLE" in codes:
        reasons.append("QUALIFIED_ROLE_CONFIRMED")
    if employer == "CONFIRMED":
        reasons.append("QUALIFIED_EMPLOYER_CONFIRMED")
    else:
        reasons.append("REVIEW_EMPLOYER_UNKNOWN")
    if application_url and channel != "UNKNOWN":
        reasons.append("QUALIFIED_APPLICATION_URL")
    else:
        reasons.append("REVIEW_APPLICATION_CHANNEL_UNKNOWN")
    if location_present:
        reasons.append("QUALIFIED_LOCATION_CONFIRMED")
    if not location_eligible:
        reasons.append("REVIEW_LOCATION_ELIGIBILITY_UNKNOWN")
    relationship = _value(selected_source, "employer_relationship").upper() or "UNKNOWN"
    if relationship == "UNKNOWN":
        reasons.append("REVIEW_SOURCE_RELATIONSHIP_UNKNOWN")

    if listing or query_mismatch or activity == "INACTIVE":
        status = "DISQUALIFIED"
    elif (
        activity == "ACTIVE" and employer == "CONFIRMED"
        and channel != "UNKNOWN" and bool(application_url) and location_eligible
        and relationship != "UNKNOWN"
    ):
        status = "QUALIFIED"
    else:
        status = "REVIEW"
    evidence = {
        "activity_evidence": activity_evidence,
        "employer_evidence": employer_evidence,
        "filter_status": _value(row, "filter_status"),
        "filter_reason_codes": list(codes),
        "location": {
            "text": _value(row, "location_text"), "country": normalized_country,
            "region": normalized_region, "city": normalized_city,
            "remote_status": remote_status,
            "eligibility": "CONFIRMED" if location_eligible else "UNKNOWN",
        },
        "posting_identity": "LISTING" if listing else "INDIVIDUAL",
        "sources": [dict(source) for source in sources],
        "selected_source": selected_source,
        "verification": {
            "attempted": verification_attempted,
            "fetch_status": parsed.fetch_status if parsed else "NOT_REQUIRED",
            "fetch_error": parsed.fetch_error if parsed else verification_error,
            "structured_job_posting": bool(parsed and parsed.has_structured_job_posting),
        },
    }
    now = utc_now()
    return QualificationResult(
        job_id=int(row["job_id"]), title=_value(row, "title"),
        company=_value(row, "company"), qualification_status=status,
        activity_status=activity, employer_status=employer,
        location=_value(row, "location_text"), country=normalized_country,
        region=normalized_region, city=normalized_city,
        remote_status=remote_status,
        source_type=_value(selected_source, "source_type").upper() or "UNKNOWN",
        employer_relationship=relationship, application_channel=channel,
        application_url=application_url, reason_codes=tuple(reasons),
        evidence=evidence, qualified_at=now,
    )


def _select_rows(
    connection: sqlite3.Connection, policy_version: str,
    job_ids: Sequence[int] | None, run_id: str | None,
    observation_status: str | None, include_medium: bool,
) -> list[sqlite3.Row]:
    if observation_status and not run_id:
        raise ValueError("observation_status requires run_id")
    if observation_status not in (None, *OBSERVATION_STATUSES):
        raise ValueError(f"invalid observation_status: {observation_status}")
    clauses = ["f.policy_version=?", "f.status IN ('PASS', 'REVIEW')"]
    parameters: list[object] = [policy_version]
    if job_ids is not None:
        if not job_ids:
            return []
        clauses.append("j.job_id IN (" + ",".join("?" for _ in job_ids) + ")")
        parameters.extend(job_ids)
    if run_id:
        if not connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ValueError(f"workflow run not found: {run_id}")
        membership = "EXISTS (SELECT 1 FROM workflow_run_jobs wrj WHERE wrj.run_id=? AND wrj.job_id=j.job_id"
        parameters.append(run_id)
        if observation_status:
            membership += " AND wrj.discovery_state=?"
            parameters.append(observation_status)
        clauses.append(membership + ")")
    rows = connection.execute(
        """SELECT j.job_id, j.canonical_url, j.title, j.location_text,
                  j.country, j.region, j.city, j.remote_policy,
                  j.description, j.status AS job_status, j.content_hash,
                  COALESCE(c.canonical_name, '') AS company,
                  f.status AS filter_status, f.reasons_json, f.matched_terms_json,
                  f.detected_remote_policy,
                  COALESCE(s.provider, '') AS provider,
                  COALESCE(s.source_type, 'UNKNOWN') AS source_type,
                  COALESCE(s.employer_relationship, 'UNKNOWN') AS employer_relationship
           FROM jobs j JOIN job_filter_results f ON f.job_id=j.job_id
           LEFT JOIN companies c ON c.company_id=j.company_id
           LEFT JOIN job_sources s ON s.job_source_id=(
             SELECT candidate.job_source_id FROM job_sources candidate
             WHERE candidate.job_id=j.job_id ORDER BY candidate.job_source_id LIMIT 1)
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id",
        parameters,
    ).fetchall()
    eligible = []
    for row in rows:
        if row["filter_status"] == "PASS":
            eligible.append(row)
            continue
        priority = prioritize_review_row(row)
        if priority and (
            priority.priority == "HIGH"
            or (include_medium and priority.priority == "MEDIUM")
        ):
            eligible.append(row)
    return eligible


def _sources(connection: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT job_source_id, provider, source_url, apply_url, fetch_status,
                  fetch_error, last_fetched_at, source_type, employer_relationship
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""", (job_id,)
    ).fetchall()


def _persist(
    connection: sqlite3.Connection, result: QualificationResult,
    policy_version: str, input_hash: str,
) -> None:
    reasons = json.dumps(result.reason_codes, ensure_ascii=False, separators=(",", ":"))
    evidence = json.dumps(result.evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_qualifications
           (job_id, policy_version, qualification_status, activity_status,
            employer_status, application_channel, application_url,
            reason_codes_json, evidence_json, input_evidence_hash,
            qualified_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id, policy_version) DO UPDATE SET
             qualification_status=excluded.qualification_status,
             activity_status=excluded.activity_status,
             employer_status=excluded.employer_status,
             application_channel=excluded.application_channel,
             application_url=excluded.application_url,
             reason_codes_json=excluded.reason_codes_json,
             evidence_json=excluded.evidence_json,
             input_evidence_hash=excluded.input_evidence_hash,
             qualified_at=excluded.qualified_at,
             updated_at=excluded.updated_at""",
        (
            result.job_id, policy_version, result.qualification_status,
            result.activity_status, result.employer_status,
            result.application_channel, result.application_url, reasons, evidence,
            input_hash, result.qualified_at, result.qualified_at, result.qualified_at,
        ),
    )


def qualify_jobs(
    connection: sqlite3.Connection, policy_version: str = DEFAULT_POLICY_VERSION,
    job_ids: Sequence[int] | None = None, run_id: str | None = None,
    observation_status: str | None = None, include_medium: bool = False,
    timeout: int = 15, browser_fallback: bool = False, windowed: bool = False,
    verbose: bool = False, fetcher=fetch_job, network_relay=None,
    driver_factory=None, browser_fetcher=None,
) -> list[QualificationResult]:
    """Qualify eligible jobs and persist only the dedicated result rows."""
    rows = _select_rows(
        connection, policy_version, job_ids, run_id, observation_status,
        include_medium,
    )
    relay = network_relay or NetworkProtectionRelay(enabled=True)
    results: list[QualificationResult] = []
    driver = None
    try:
        for row in rows:
            sources = _sources(connection, int(row["job_id"]))
            input_payload = _input_payload(row, sources)
            input_hash = _payload_hash(input_payload)
            existing = connection.execute(
                """SELECT * FROM job_qualifications
                   WHERE job_id=? AND policy_version=? AND input_evidence_hash=?""",
                (row["job_id"], policy_version, input_hash),
            ).fetchone()
            if existing:
                result = _result_from_stored(existing, row)
                results.append(result)
                if verbose:
                    print(f"{result.job_id}: reused unchanged qualification")
                continue

            activity, _ = _stored_activity(row, sources)
            channel, _, _ = _application_destination(sources)
            needs_fetch = activity == "UNKNOWN" or channel == "UNKNOWN"
            parsed = None
            verification_error = ""
            if needs_fetch:
                try:
                    parsed = relay.protect(
                        lambda: fetcher(row["canonical_url"], timeout=timeout),
                        context=f"qualification job {row['job_id']}",
                    )
                except NetworkPauseExceeded as error:
                    verification_error = f"NETWORK/{error.error_type}: {error}"
                except Exception as error:
                    verification_error = f"{type(error).__name__}: {error}"
                if parsed is None:
                    parsed = ParsedJob(row["canonical_url"], detect_provider(row["canonical_url"]))
                    parsed.fetch_status = "FAILED"
                    parsed.fetch_error = verification_error
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
                                lambda: driver_factory(windowed=windowed),
                                context="qualification browser startup",
                            )
                        browser_result = relay.protect(
                            lambda: browser_fetcher(row["canonical_url"], timeout, driver),
                            context=f"qualification browser job {row['job_id']}",
                        )
                        if browser_result is not None:
                            parsed = browser_result
                    except Exception as error:
                        verification_error = f"{type(error).__name__}: {error}"

            result = _evaluate(row, sources, parsed, needs_fetch, verification_error)
            with connection:
                _persist(connection, result, policy_version, input_hash)
            results.append(result)
            if verbose:
                print(f"{result.job_id}: {result.qualification_status}")
    finally:
        if driver is not None:
            driver.quit()
    return results


def print_results(results: Sequence[QualificationResult]) -> None:
    counts = Counter(result.qualification_status for result in results)
    print(f"Jobs selected: {len(results)}")
    for status in QUALIFICATION_STATUSES:
        print(f"{status}: {counts[status]}")
    for result in results:
        print()
        print(f"job_id: {result.job_id}")
        print(f"title: {result.title}")
        print(f"company: {result.company}")
        print(f"qualification: {result.qualification_status}")
        print(f"activity: {result.activity_status}")
        print(f"employer_status: {result.employer_status}")
        print(f"location: {result.location}")
        print(f"source_type: {result.source_type}")
        print(f"employer_relationship: {result.employer_relationship}")
        print(f"application_channel: {result.application_channel}")
        print(f"application_url: {result.application_url or '-'}")
        print(f"qualification_reasons: {', '.join(result.reason_codes)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic Job Qualification v1")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--observation-status", choices=OBSERVATION_STATUSES)
    parser.add_argument("--include-medium", action="store_true")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--browser-fallback", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.observation_status and not args.run_id:
        parser.error("--observation-status requires --run-id")
    if args.windowed and not args.browser_fallback:
        parser.error("--windowed requires --browser-fallback")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    connection = connect_database(args.database)
    try:
        try:
            results = qualify_jobs(
                connection, args.policy_version, args.job_id, args.run_id,
                args.observation_status, args.include_medium, args.timeout,
                args.browser_fallback, args.windowed, args.verbose,
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
