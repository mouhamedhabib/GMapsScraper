"""Deterministic prioritization for jobs already classified as REVIEW.

This module ranks review work; it never changes hard-filter decisions or job data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Mapping, Sequence

from job_search.filtering import DEFAULT_POLICY_VERSION
from job_search.providers import is_safe_location_value
from job_search.storage import DEFAULT_DATABASE, connect_database


PRIORITIES = ("HIGH", "MEDIUM", "LOW")
OBSERVATION_STATUSES = ("NEW", "UPDATED", "KNOWN")
MODERATE_REVIEW_REASONS = frozenset({
    "REVIEW_EXPERIENCE_3_YEARS",
    "REVIEW_EXPERIENCE_PREFERRED",
    "REVIEW_LOCATION_PREFERRED_MARKET",
    "REVIEW_LOCATION_EUROPE",
    "REVIEW_RELOCATION_POSSIBLE",
})
MIN_SUFFICIENT_DESCRIPTION_CHARS = 200


@dataclass(frozen=True)
class ReviewPriority:
    job_id: int
    review_priority: str
    title: str
    company: str
    location: str
    published_at: str
    provider: str
    source_type: str
    employer_relationship: str
    filter_reasons: tuple[str, ...]
    review_priority_reasons: tuple[str, ...]
    job_url: str
    review_reason_count: int
    fresh: bool
    source_strength: int

    @property
    def priority(self) -> str:
        """Short display alias for the stored-evidence classification."""
        return self.review_priority

    @property
    def priority_reasons(self) -> tuple[str, ...]:
        """Short display alias for review_priority_reasons."""
        return self.review_priority_reasons


def _value(row: Mapping, name: str) -> str:
    try:
        value = row[name]
    except (KeyError, IndexError):
        value = ""
    return str(value or "").strip()


def _filter_reason_codes(row: Mapping) -> tuple[str, ...]:
    try:
        payload = json.loads(_value(row, "reasons_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        payload = []
    return tuple(
        str(item.get("code") or "").strip()
        for item in payload
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    )


def _source_strength(source_type: str, relationship: str, provider: str) -> int:
    if relationship == "DIRECT":
        return 4
    if source_type in {"ATS", "COMPANY_SITE"}:
        return 3
    if source_type == "JOB_PLATFORM":
        return 2
    if relationship in {"RECRUITER", "AGGREGATOR"} or provider:
        return 1
    return 0


def prioritize_review_row(row: Mapping) -> ReviewPriority | None:
    """Classify one stored filter row, returning None unless it is REVIEW."""
    if _value(row, "filter_status") != "REVIEW":
        return None

    codes = _filter_reason_codes(row)
    review_codes = tuple(code for code in codes if code.startswith("REVIEW_"))
    relevant = "PASS_RELEVANT_ROLE" in codes
    fresh = "PASS_FRESH_JOB" in codes
    title = _value(row, "title")
    company = _value(row, "company")
    raw_location = _value(row, "location_text")
    location = raw_location if is_safe_location_value(raw_location) else ""
    description = _value(row, "description")
    provider = _value(row, "provider")
    source_type = _value(row, "source_type").upper() or "UNKNOWN"
    relationship = _value(row, "employer_relationship").upper() or "UNKNOWN"
    source_strength = _source_strength(source_type, relationship, provider)

    missing_location = not location
    missing_description = not description
    sparse_description = bool(description) and len(description) < MIN_SUFFICIENT_DESCRIPTION_CHARS
    role_unclear = "REVIEW_ROLE_UNCLEAR" in review_codes
    important_missing = sum((not title, not company, missing_location, missing_description))
    multiple_unknowns = len(review_codes) >= 2 or important_missing >= 2
    sufficient_data = bool(title and location and description) and not sparse_description
    single_moderate_gap = (
        len(review_codes) == 1 and review_codes[0] in MODERATE_REVIEW_REASONS
    )

    reasons: list[str] = []
    if relevant:
        reasons.append("PRIORITY_RELEVANT_ROLE")
    if fresh:
        reasons.append("PRIORITY_FRESH")
    if relationship == "DIRECT":
        reasons.append("PRIORITY_DIRECT_SOURCE")
    if source_type == "ATS":
        reasons.append("PRIORITY_ATS_SOURCE")
    if relationship == "RECRUITER":
        reasons.append("PRIORITY_RECRUITER_SOURCE")
    if single_moderate_gap:
        reasons.append("PRIORITY_SINGLE_MODERATE_GAP")
    if missing_location:
        reasons.append("PRIORITY_MISSING_LOCATION")
    if missing_description:
        reasons.append("PRIORITY_MISSING_DESCRIPTION")
    elif sparse_description:
        reasons.append("PRIORITY_SPARSE_DESCRIPTION")
    if role_unclear:
        reasons.append("PRIORITY_ROLE_UNCLEAR")
    if multiple_unknowns:
        reasons.append("PRIORITY_MULTIPLE_UNKNOWNS")
    if source_strength == 0:
        reasons.append("PRIORITY_WEAK_SOURCE")

    if (
        relevant
        and single_moderate_gap
        and sufficient_data
        and source_strength >= 3
    ):
        priority = "HIGH"
    elif role_unclear or not relevant or len(review_codes) >= 3 or (
        important_missing >= 3 and source_strength == 0
    ):
        priority = "LOW"
    else:
        priority = "MEDIUM"

    return ReviewPriority(
        job_id=int(row["job_id"]), review_priority=priority, title=title,
        company=company, location=location,
        published_at=_value(row, "published_at"), provider=provider,
        source_type=source_type, employer_relationship=relationship,
        filter_reasons=codes, review_priority_reasons=tuple(reasons),
        job_url=_value(row, "canonical_url"),
        review_reason_count=len(review_codes), fresh=fresh,
        source_strength=source_strength,
    )


def _published_sort_value(value: str) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return float("-inf")


def priority_sort_key(item: ReviewPriority) -> tuple:
    """Implement the documented deterministic ordering within a priority."""
    return (
        0 if item.fresh else 1,
        item.review_reason_count,
        -item.source_strength,
        -_published_sort_value(item.published_at),
        item.job_id,
    )


def load_review_priorities(
    connection: sqlite3.Connection, policy_version: str = DEFAULT_POLICY_VERSION,
    job_ids: Sequence[int] | None = None,
    run_id: str | None = None, observation_status: str | None = None,
) -> list[ReviewPriority]:
    """Compute priorities from current stored evidence without persisting them."""
    if observation_status is not None and run_id is None:
        raise ValueError("observation_status requires run_id")
    if observation_status not in (None, *OBSERVATION_STATUSES):
        raise ValueError(f"invalid observation_status: {observation_status}")
    if run_id is not None:
        run_exists = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run_exists is None:
            raise ValueError(f"workflow run not found: {run_id}")

    parameters: list[object] = [policy_version]
    scope_clause = ""
    if run_id is not None:
        state_clause = ""
        if observation_status is not None:
            state_clause = " AND wrj.discovery_state=?"
        scope_clause = (
            " AND EXISTS (SELECT 1 FROM workflow_run_jobs wrj"
            " WHERE wrj.run_id=? AND wrj.job_id=j.job_id"
            f"{state_clause})"
        )
        parameters.append(run_id)
        if observation_status is not None:
            parameters.append(observation_status)
    job_clause = ""
    if job_ids is not None:
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        job_clause = f" AND j.job_id IN ({placeholders})"
        parameters.extend(job_ids)
    rows = connection.execute(
        """SELECT j.job_id, j.title, c.canonical_name AS company,
                  j.location_text, j.description, j.published_at, j.canonical_url,
                  f.status AS filter_status, f.reasons_json,
                  COALESCE(s.provider, '') AS provider,
                  COALESCE(s.source_type, 'UNKNOWN') AS source_type,
                  COALESCE(s.employer_relationship, 'UNKNOWN') AS employer_relationship
           FROM job_filter_results f
           JOIN jobs j ON j.job_id=f.job_id
           LEFT JOIN companies c ON c.company_id=j.company_id
           LEFT JOIN job_sources s ON s.job_source_id=(
               SELECT candidate.job_source_id FROM job_sources candidate
               WHERE candidate.job_id=j.job_id
               ORDER BY
                   CASE candidate.employer_relationship
                       WHEN 'DIRECT' THEN 0 WHEN 'RECRUITER' THEN 2
                       WHEN 'AGGREGATOR' THEN 3 ELSE 1 END,
                   CASE candidate.source_type
                       WHEN 'COMPANY_SITE' THEN 0 WHEN 'ATS' THEN 1
                       WHEN 'JOB_PLATFORM' THEN 2 ELSE 3 END,
                   candidate.job_source_id
               LIMIT 1
           )
           WHERE f.policy_version=? AND f.status='REVIEW'"""
        + scope_clause + job_clause + " ORDER BY j.job_id",
        parameters,
    ).fetchall()
    results = [item for row in rows if (item := prioritize_review_row(row))]
    return sorted(
        results,
        key=lambda item: (PRIORITIES.index(item.priority), *priority_sort_key(item)),
    )


def _print_result(item: ReviewPriority) -> None:
    print(f"job_id: {item.job_id}")
    print(f"priority: {item.priority}")
    print(f"title: {item.title}")
    print(f"company: {item.company}")
    print(f"location: {item.location}")
    print(f"published_at: {item.published_at}")
    print(f"provider: {item.provider}")
    print(f"source_type: {item.source_type}")
    print(f"employer_relationship: {item.employer_relationship}")
    print(f"filter_reasons: {', '.join(item.filter_reasons)}")
    print(f"priority_reasons: {', '.join(item.priority_reasons)}")
    print(f"job_url: {item.job_url}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministically prioritize stored REVIEW jobs"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--observation-status", choices=OBSERVATION_STATUSES,
        help="limit a workflow run to jobs with this observation status",
    )
    parser.add_argument("--show-high", action="store_true")
    parser.add_argument("--show-medium", action="store_true")
    parser.add_argument("--show-low", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.observation_status and not args.run_id:
        parser.error("--observation-status requires --run-id")
    selected = {
        priority for enabled, priority in (
            (args.show_high, "HIGH"),
            (args.show_medium, "MEDIUM"),
            (args.show_low, "LOW"),
        ) if enabled
    }
    if not selected:
        selected = set(PRIORITIES)
    connection = connect_database(args.database)
    try:
        try:
            results = load_review_priorities(
                connection, args.policy_version, run_id=args.run_id,
                observation_status=args.observation_status,
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    for priority in PRIORITIES:
        print(f"{priority}: {sum(item.priority == priority for item in results)}")
    print()
    for item in results:
        if item.priority in selected:
            _print_result(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
