"""Read-only, deterministic analytics for persisted Google job queries.

Downstream outcomes are credited only to the query marked as the primary NEW
source for a canonical job in a workflow run. Secondary observations never
duplicate PASS/REVIEW/REJECT or priority counts. Historical evidence that
predates that run-scoped provenance is represented by ``None``, not zero.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Sequence
from urllib.parse import quote

from job_search.filtering import DEFAULT_POLICY_VERSION
from job_search.discovery import classify_query
from job_search.review_priority import prioritize_review_row
from job_search.storage import DEFAULT_DATABASE


DEFAULT_RECENT_RUNS = 10
QUERY_CATEGORIES = (
    "ATS_SCOPED", "DIRECT_CAREERS", "ROLE_LOCATION", "ROLE_TECH", "GENERIC",
)
MIN_DROP_RUNS = 2
MIN_DROP_RESULTS = 20
MIN_KEEP_RUNS = 2
MIN_KEEP_RESULTS = 10
MIN_KEEP_NEW_JOBS = 2
GOOD_JOB_YIELD = 0.05
HIGH_NOISE_RATE = 0.80
HIGH_RESOLUTION_FAILURE_RATE = 0.50
EXPENSIVE_SECONDS_PER_NEW_JOB = 120.0

COUNT_METRICS = (
    "pages_inspected", "results_inspected", "job_candidates", "new_jobs",
    "known_jobs", "rejected_noise",
    "resolution_failures", "browser_resolutions", "http_job_fetches",
    "pass_jobs", "review_jobs", "reject_jobs", "high_reviews",
    "medium_reviews", "low_reviews", "shortlisted_jobs",
)
RATIO_DEFINITIONS = {
    "candidate_yield": ("job_candidates", "results_inspected"),
    "new_job_yield": ("new_jobs", "results_inspected"),
    "noise_rate": ("rejected_noise", "results_inspected"),
    "resolution_failure_rate": ("resolution_failures", "results_inspected"),
    "pass_yield": ("pass_jobs", "new_jobs"),
    "high_review_yield": ("high_reviews", "new_jobs"),
    "shortlist_yield": ("shortlisted_jobs", "new_jobs"),
    "seconds_per_new_job": ("duration_seconds", "new_jobs"),
    "results_per_new_job": ("results_inspected", "new_jobs"),
}


@dataclass
class QueryPerformance:
    query: str
    category: str
    runs_seen: int
    pages_inspected: int | None = None
    results_inspected: int | None = None
    job_candidates: int | None = None
    new_jobs: int | None = None
    known_jobs: int | None = None
    rejected_noise: int | None = None
    resolution_failures: int | None = None
    browser_resolutions: int | None = None
    http_job_fetches: int | None = None
    duration_seconds: float | None = None
    pass_jobs: int | None = None
    review_jobs: int | None = None
    reject_jobs: int | None = None
    high_reviews: int | None = None
    medium_reviews: int | None = None
    low_reviews: int | None = None
    shortlisted_jobs: int | None = None
    recommendation: str = "REVIEW"
    reasons: tuple[str, ...] = ()

    def ratio(self, name: str) -> float | None:
        numerator_name, denominator_name = RATIO_DEFINITIONS[name]
        numerator = getattr(self, numerator_name)
        denominator = getattr(self, denominator_name)
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    absolute = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{quote(str(absolute))}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def select_run_ids(
    connection: sqlite3.Connection, run_id: str | None = None,
    recent_runs: int = DEFAULT_RECENT_RUNS,
) -> list[str]:
    if run_id:
        row = connection.execute(
            "SELECT run_id FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"workflow run not found: {run_id}")
        return [run_id]
    return [
        row["run_id"] for row in connection.execute(
            """SELECT run_id FROM workflow_runs
               WHERE status IN ('SUCCESS', 'PARTIAL')
               ORDER BY started_at DESC, run_id DESC LIMIT ?""",
            (recent_runs,),
        )
    ]


def _sum_available(rows: Sequence[sqlite3.Row], column: str) -> int | float | None:
    if not rows or any(row[column] is None for row in rows):
        return None
    return sum(row[column] for row in rows)


def _outcomes(
    connection: sqlite3.Connection, run_ids: Sequence[str], query: str,
    expected_new_jobs: int | None,
) -> dict[str, int | None]:
    names = (
        "pass_jobs", "review_jobs", "reject_jobs", "high_reviews",
        "medium_reviews", "low_reviews", "shortlisted_jobs",
    )
    unavailable = dict.fromkeys(names)
    if expected_new_jobs == 0:
        return dict.fromkeys(names, 0)
    if expected_new_jobs is None or not _has_table(connection, "workflow_run_job_queries"):
        return unavailable
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""SELECT j.job_id, j.title, j.location_text, j.description,
                   j.published_at, j.canonical_url,
                   COALESCE(c.canonical_name, '') AS company,
                   f.status AS filter_status, f.reasons_json,
                   COALESCE(s.provider, '') AS provider,
                   COALESCE(s.source_type, 'UNKNOWN') AS source_type,
                   COALESCE(s.employer_relationship, 'UNKNOWN') AS employer_relationship
            FROM workflow_run_job_queries q
            JOIN jobs j ON j.job_id=q.job_id
            LEFT JOIN companies c ON c.company_id=j.company_id
            LEFT JOIN job_filter_results f
              ON f.job_id=j.job_id AND f.policy_version=?
            LEFT JOIN job_sources s ON s.job_source_id=(
                SELECT candidate.job_source_id FROM job_sources candidate
                WHERE candidate.job_id=j.job_id ORDER BY
                  CASE candidate.employer_relationship WHEN 'DIRECT' THEN 0
                    WHEN 'RECRUITER' THEN 2 WHEN 'AGGREGATOR' THEN 3 ELSE 1 END,
                  CASE candidate.source_type WHEN 'COMPANY_SITE' THEN 0
                    WHEN 'ATS' THEN 1 WHEN 'JOB_PLATFORM' THEN 2 ELSE 3 END,
                  candidate.job_source_id LIMIT 1)
            WHERE q.run_id IN ({placeholders}) AND q.source_query=?
              AND q.is_primary_new_source=1
            ORDER BY q.run_id, j.job_id""",
        (DEFAULT_POLICY_VERSION, *run_ids, query),
    ).fetchall()
    if len(rows) != expected_new_jobs or any(row["filter_status"] is None for row in rows):
        return unavailable
    counts = dict.fromkeys(names, 0)
    for row in rows:
        decision = row["filter_status"]
        counts[{"PASS": "pass_jobs", "REVIEW": "review_jobs", "REJECT": "reject_jobs"}[decision]] += 1
        priority = prioritize_review_row(row) if decision == "REVIEW" else None
        if priority:
            counts[f"{priority.priority.lower()}_reviews"] += 1
        # The analytics shortlist uses the workflow's conservative default:
        # PASS plus HIGH-priority REVIEW; optional medium inclusion is not stored.
        if decision == "PASS" or (priority and priority.priority == "HIGH"):
            counts["shortlisted_jobs"] += 1
    return counts


def classify_performance(item: QueryPerformance) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    candidate_yield = item.ratio("candidate_yield")
    noise_rate = item.ratio("noise_rate")
    failure_rate = item.ratio("resolution_failure_rate")
    seconds_per_new = item.ratio("seconds_per_new_job")
    useful = (item.shortlisted_jobs or 0) > 0
    if candidate_yield is not None and candidate_yield >= GOOD_JOB_YIELD:
        reasons.append("QUERY_GOOD_JOB_YIELD")
    if (item.shortlisted_jobs or 0) > 0:
        reasons.append("QUERY_SHORTLIST_YIELD")
    if noise_rate is not None and noise_rate >= HIGH_NOISE_RATE:
        reasons.append("QUERY_HIGH_NOISE")
    if failure_rate is not None and failure_rate >= HIGH_RESOLUTION_FAILURE_RATE:
        reasons.append("QUERY_HIGH_RESOLUTION_FAILURE")
    if seconds_per_new is not None and seconds_per_new >= EXPENSIVE_SECONDS_PER_NEW_JOB:
        reasons.append("QUERY_EXPENSIVE")
    if item.new_jobs == 0 or (
        item.new_jobs is not None and item.pass_jobs == 0 and item.high_reviews == 0
        and item.shortlisted_jobs == 0
    ):
        reasons.append("QUERY_NO_USEFUL_OUTCOMES")

    drop_sample = (
        item.runs_seen >= MIN_DROP_RUNS
        and item.results_inspected is not None
        and item.results_inspected >= MIN_DROP_RESULTS
    )
    bad_cost = (
        (noise_rate is not None and noise_rate >= HIGH_NOISE_RATE)
        or (failure_rate is not None and failure_rate >= HIGH_RESOLUTION_FAILURE_RATE)
    )
    if drop_sample and item.new_jobs == 0 and bad_cost:
        return "DROP", tuple(reasons)
    keep_sample = (
        item.runs_seen >= MIN_KEEP_RUNS
        and (item.results_inspected or 0) >= MIN_KEEP_RESULTS
        and (item.new_jobs or 0) >= MIN_KEEP_NEW_JOBS
    )
    reasonable_cost = (
        noise_rate is not None and noise_rate < HIGH_NOISE_RATE
        and failure_rate is not None and failure_rate < HIGH_RESOLUTION_FAILURE_RATE
    )
    if keep_sample and useful and reasonable_cost and (
        candidate_yield is not None and candidate_yield >= GOOD_JOB_YIELD
    ):
        return "KEEP", tuple(reasons)
    if not drop_sample and not keep_sample:
        reasons.append("QUERY_INSUFFICIENT_SAMPLE")
    return "REVIEW", tuple(reasons)


def analyze(
    connection: sqlite3.Connection, run_ids: Sequence[str],
    query: str | None = None, category: str | None = None,
) -> list[QueryPerformance]:
    if not run_ids:
        return []
    columns = _columns(connection, "workflow_run_queries")
    optional = (
        "job_candidates", "known_jobs", "rejected_noise", "resolution_failures",
        "browser_resolutions", "http_job_fetches", "duration_seconds",
    )
    selections = [name if name in columns else f"NULL AS {name}" for name in optional]
    selections.append(
        "query_category" if "query_category" in columns else "NULL AS query_category"
    )
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""SELECT run_id, source_query, pages_inspected, results_inspected,
                   new_jobs, {', '.join(selections)}
            FROM workflow_run_queries WHERE run_id IN ({placeholders})
              AND status != 'PLANNED'
            ORDER BY workflow_run_query_id""",
        tuple(run_ids),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["source_query"], []).append(row)
    results: list[QueryPerformance] = []
    for source_query, query_rows in grouped.items():
        item_category = next(
            (row["query_category"] for row in query_rows if row["query_category"]),
            classify_query(source_query),
        )
        if query is not None and source_query != query:
            continue
        if category is not None and item_category != category:
            continue
        item = QueryPerformance(
            query=source_query, category=item_category,
            runs_seen=len({row["run_id"] for row in query_rows}),
            pages_inspected=_sum_available(query_rows, "pages_inspected"),
            results_inspected=_sum_available(query_rows, "results_inspected"),
            new_jobs=_sum_available(query_rows, "new_jobs"),
            **{name: _sum_available(query_rows, name) for name in optional},
        )
        for name, value in _outcomes(
            connection, run_ids, source_query, item.new_jobs
        ).items():
            setattr(item, name, value)
        item.recommendation, item.reasons = classify_performance(item)
        results.append(item)
    return sorted(results, key=lambda item: item.query.casefold())


def aggregate_categories(items: Sequence[QueryPerformance]) -> list[QueryPerformance]:
    output = []
    for category in QUERY_CATEGORIES:
        members = [item for item in items if item.category == category]
        if not members:
            continue
        values = {}
        for name in (*COUNT_METRICS, "duration_seconds"):
            metric_values = [getattr(item, name) for item in members]
            values[name] = (
                None if any(value is None for value in metric_values)
                else sum(metric_values)
            )
        output.append(QueryPerformance(
            query=f"[{category}]", category=category,
            runs_seen=sum(item.runs_seen for item in members), **values,
        ))
    return output


def _format(value: int | float | None, ratio: bool = False) -> str:
    if value is None:
        return "unavailable"
    if ratio:
        return f"{value:.3f}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _print_item(item: QueryPerformance) -> None:
    print(f"QUERY: {item.query}")
    print(f"CATEGORY: {item.category}")
    print(f"RECOMMENDATION: {item.recommendation}")
    print()
    print(f"runs_seen: {item.runs_seen}")
    for name in (*COUNT_METRICS, "duration_seconds"):
        print(f"{name}: {_format(getattr(item, name))}")
    for name in RATIO_DEFINITIONS:
        print(f"{name}: {_format(item.ratio(name), ratio=True)}")
    print("reasons:")
    for reason in item.reasons:
        print(f"  {reason}")
    print()


def _rank_value(item: QueryPerformance, name: str) -> float:
    value = item.ratio(name)
    return -1.0 if value is None else value


def print_report(items: Sequence[QueryPerformance]) -> None:
    for item in items:
        _print_item(item)
    print("CATEGORY PERFORMANCE")
    print()
    for item in aggregate_categories(items):
        print(item.category)
        print(f"queries: {sum(member.category == item.category for member in items)}")
        for name in ("results_inspected", "new_jobs", "shortlisted_jobs"):
            print(f"{name}: {_format(getattr(item, name))}")
        print(f"noise_rate: {_format(item.ratio('noise_rate'), ratio=True)}")
        print("resolution_failure_rate: " + _format(
            item.ratio("resolution_failure_rate"), ratio=True
        ))
        print()
    top = sorted(
        items, key=lambda item: (
            -_rank_value(item, "shortlist_yield"),
            -_rank_value(item, "new_job_yield"), -(item.new_jobs or 0),
            item.query.casefold(),
        ),
    )
    print("TOP QUERIES")
    for index, item in enumerate(top, 1):
        print(f"{index}. {item.query} — {item.recommendation}; new={_format(item.new_jobs)}; shortlist={_format(item.shortlisted_jobs)}")
    print()
    expensive = sorted(
        items, key=lambda item: (
            -_rank_value(item, "seconds_per_new_job"),
            _rank_value(item, "new_job_yield"), item.query.casefold(),
        ),
    )
    print("EXPENSIVE / LOW-YIELD QUERIES")
    for index, item in enumerate(expensive, 1):
        print(f"{index}. {item.query} — seconds/new={_format(item.ratio('seconds_per_new_job'), True)}; yield={_format(item.ratio('new_job_yield'), True)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only deterministic query performance analytics"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--run-id")
    scope.add_argument("--recent-runs", type=int, default=DEFAULT_RECENT_RUNS)
    parser.add_argument("--query")
    parser.add_argument("--category", choices=QUERY_CATEGORIES)
    parser.add_argument("--show-keep", action="store_true")
    parser.add_argument("--show-review", action="store_true")
    parser.add_argument("--show-drop", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.recent_runs < 1:
        parser.error("--recent-runs must be at least 1")
    selected = {
        name for enabled, name in (
            (args.show_keep, "KEEP"), (args.show_review, "REVIEW"),
            (args.show_drop, "DROP"),
        ) if enabled
    }
    try:
        connection = _readonly_connection(args.database)
    except (OSError, sqlite3.Error) as error:
        parser.error(str(error))
    try:
        try:
            run_ids = select_run_ids(connection, args.run_id, args.recent_runs)
            items = analyze(connection, run_ids, args.query, args.category)
        except (ValueError, sqlite3.Error) as error:
            parser.error(str(error))
    finally:
        connection.close()
    if selected:
        items = [item for item in items if item.recommendation in selected]
    print_report(items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
