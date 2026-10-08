"""Deterministic Search Strategy / Coverage v2 recommendations.

The stage reads persisted discovery evidence, writes versioned analysis results,
and never edits or activates the active query file.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from job_search.discovery import DEFAULT_QUERY_FILE, classify_query
from job_search.query_performance import QueryPerformance, analyze
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now
from utils.google_search_discovery import load_queries


SEARCH_STRATEGY_POLICY_VERSION = "search-strategy-v2"
RECOMMENDATIONS = ("KEEP", "EXPAND", "REVIEW", "RETIRE_CANDIDATE")
MATURITIES = ("INSUFFICIENT", "EARLY", "MATURE")
PROVIDERS = {
    "jobs.lever.co": "Lever", "job-boards.greenhouse.io": "Greenhouse",
    "boards.greenhouse.io": "Greenhouse", "jobs.ashbyhq.com": "Ashby",
    "apply.workable.com": "Workable", "jobs.smartrecruiters.com": "SmartRecruiters",
    "teamtailor.com": "Teamtailor",
}
PROVIDER_DOMAINS = {
    "Lever": "jobs.lever.co", "Greenhouse": "job-boards.greenhouse.io",
    "Ashby": "jobs.ashbyhq.com", "Workable": "apply.workable.com",
    "SmartRecruiters": "jobs.smartrecruiters.com", "Teamtailor": "teamtailor.com",
}
ROLE_PATTERNS = (
    ("PHP_LARAVEL", r"\b(?:php|laravel)\b"),
    ("FULLSTACK", r"\b(?:full[ -]?stack|fullstack)\b"),
    ("BACKEND", r"\b(?:back[ -]?end|backend)\b"),
    ("FRONTEND", r"\b(?:front[ -]?end|frontend)\b"),
    ("PYTHON", r"\bpython\b"), ("JAVA", r"\bjava\b"),
    ("SOFTWARE_ENGINEERING", r"\b(?:software|developer|engineer|programmer)\b"),
)
TECHNOLOGIES = {
    "NestJS": r"\bnestjs\b", "Node.js": r"\b(?:node\.js|nodejs)\b",
    "Python": r"\bpython\b", "FastAPI": r"\bfastapi\b", "PHP": r"\bphp\b",
    "Laravel": r"\blaravel\b", "React": r"\breact(?:js|\.js)?\b",
    "Next.js": r"\bnext(?:js|\.js)\b", "Java": r"\bjava\b",
}
GEOGRAPHIES = (
    ("Remote Europe", r"\bremote\s+(?:in\s+)?europe\b|\beurope\s+remote\b"),
    ("Tunisia", r"\b(?:tunisia|tunis)\b"), ("France", r"\bfrance\b"),
    ("Switzerland", r"\b(?:switzerland|suisse|schweiz)\b"),
    ("Belgium", r"\b(?:belgium|belgique)\b"), ("Europe", r"\beurope\b"),
)


@dataclass(frozen=True)
class StrategyConfig:
    insufficient_runs: int = 2
    insufficient_results: int = 10
    mature_runs: int = 3
    mature_results: int = 30
    expand_min_new_jobs: int = 2
    expand_new_job_yield: float = 0.08
    keep_min_new_jobs: int = 1
    keep_new_job_yield: float = 0.03
    retire_max_new_job_yield: float = 0.01
    high_noise_rate: float = 0.80
    high_failure_rate: float = 0.50
    redundancy_min_jobs: int = 2
    redundancy_overlap: float = 0.75
    redundancy_cost_multiplier: float = 1.50
    target_geographies: tuple[str, ...] = (
        "Tunisia", "France", "Switzerland", "Belgium", "Remote Europe",
    )
    target_role_families: tuple[str, ...] = (
        "BACKEND", "FULLSTACK", "SOFTWARE_ENGINEERING", "PYTHON", "PHP_LARAVEL",
    )
    target_source_families: tuple[str, ...] = (
        "ATS_SCOPED", "ROLE_LOCATION", "ROLE_TECH",
    )
    target_combinations: tuple[tuple[str, str], ...] = (
        ("BACKEND", "France"), ("BACKEND", "Switzerland"),
        ("FULLSTACK", "France"), ("SOFTWARE_ENGINEERING", "Tunisia"),
        ("PYTHON", "Remote Europe"), ("PHP_LARAVEL", "Tunisia"),
    )


DEFAULT_CONFIG = StrategyConfig()


@dataclass(frozen=True)
class QueryDimensions:
    query_category: str
    role_family: str
    seniority_intent: str
    geography: str
    source_intent: str
    technologies: tuple[str, ...]


@dataclass(frozen=True)
class StrategyRecommendation:
    query: str
    dimensions: QueryDimensions
    maturity: str
    recommendation: str
    reasons: tuple[str, ...]
    metrics: dict


@dataclass(frozen=True)
class QueryProposal:
    query: str
    parent_query: str
    rank: int
    reasons: tuple[str, ...]
    dimensions: QueryDimensions


@dataclass(frozen=True)
class StrategyAnalysis:
    strategy_run_id: int | None
    run_ids: tuple[str, ...]
    recommendations: tuple[StrategyRecommendation, ...]
    families: dict
    coverage: tuple[dict, ...]
    redundancies: tuple[dict, ...]
    proposals: tuple[QueryProposal, ...]
    input_fingerprint: str
    created_at: str
    reused: bool = False


def normalize_query(query: str) -> str:
    return " ".join(str(query or "").casefold().split())


def parse_dimensions(query: str) -> QueryDimensions:
    folded = normalize_query(query)
    category = classify_query(query)
    role = next((name for name, pattern in ROLE_PATTERNS if re.search(pattern, folded)), "UNKNOWN")
    if re.search(r"\bintern(?:ship)?\b", folded):
        seniority = "INTERNSHIP"
    elif re.search(r"\bentry[ -]?level\b|\bgraduate\b", folded):
        seniority = "ENTRY_LEVEL"
    elif re.search(r"\bjunior\b|\bjr\.?\b", folded):
        seniority = "JUNIOR"
    else:
        seniority = "UNSPECIFIED"
    geography = next((name for name, pattern in GEOGRAPHIES if re.search(pattern, folded)), "UNKNOWN")
    provider = next((name for domain, name in PROVIDERS.items() if domain in folded), None)
    if provider:
        source = provider
    elif category == "DIRECT_CAREERS":
        source = "direct company"
    elif category in {"ROLE_LOCATION", "ROLE_TECH", "GENERIC"}:
        source = "generic web"
    else:
        source = "UNKNOWN"
    technologies = tuple(name for name, pattern in TECHNOLOGIES.items() if re.search(pattern, folded))
    return QueryDimensions(category, role, seniority, geography, source, technologies)


def _ratio(numerator, denominator):
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def select_run_ids(
    connection: sqlite3.Connection, recent_runs: int = 10, since: str | None = None,
) -> list[str]:
    clauses, parameters = ["status IN ('SUCCESS','PARTIAL')"], []
    if since:
        clauses.append("started_at>=?")
        parameters.append(since)
    parameters.append(recent_runs)
    return [row[0] for row in connection.execute(
        "SELECT run_id FROM workflow_runs WHERE " + " AND ".join(clauses)
        + " ORDER BY started_at DESC,run_id DESC LIMIT ?", parameters,
    )]


def _downstream_counts(
    connection: sqlite3.Connection, run_ids: Sequence[str], query: str,
    expected_new: int | None,
) -> tuple[int | None, int | None]:
    if expected_new is None:
        return None, None
    if expected_new == 0:
        return 0, 0
    if not run_ids or not _has_table(connection, "workflow_run_job_queries"):
        return None, None
    marks = ",".join("?" for _ in run_ids)
    job_ids = [row[0] for row in connection.execute(
        f"""SELECT job_id FROM workflow_run_job_queries
            WHERE run_id IN ({marks}) AND source_query=? AND is_primary_new_source=1
            ORDER BY run_id,job_id""", (*run_ids, query),
    )]
    if len(job_ids) != expected_new:
        return None, None
    marks = ",".join("?" for _ in job_ids)
    qualified = ready = None
    if _has_table(connection, "job_qualifications"):
        rows = connection.execute(
            f"""SELECT j.job_id,q.qualification_status FROM jobs j
                LEFT JOIN job_qualifications q ON q.qualification_id=(
                  SELECT max(q2.qualification_id) FROM job_qualifications q2 WHERE q2.job_id=j.job_id)
                WHERE j.job_id IN ({marks})""", job_ids,
        ).fetchall()
        if len(rows) == len(set(job_ids)) and all(row[1] is not None for row in rows):
            qualified = sum(row[1] == "QUALIFIED" for row in rows)
    if _has_table(connection, "job_application_actions"):
        rows = connection.execute(
            f"""SELECT j.job_id,a.ready_at FROM jobs j
                LEFT JOIN job_application_actions a ON a.application_action_id=(
                  SELECT max(a2.application_action_id) FROM job_application_actions a2 WHERE a2.job_id=j.job_id)
                WHERE j.job_id IN ({marks})""", job_ids,
        ).fetchall()
        if len(rows) == len(set(job_ids)) and all(row[1] is not None for row in rows):
            ready = len(rows)
    return qualified, ready


def metrics_for(item: QueryPerformance, qualified=None, ready=None) -> dict:
    values = {
        name: getattr(item, name) for name in (
            "runs_seen", "pages_inspected", "results_inspected", "job_candidates",
            "new_jobs", "known_jobs", "rejected_noise", "resolution_failures",
            "browser_resolutions", "http_job_fetches", "duration_seconds", "pass_jobs",
            "review_jobs", "reject_jobs", "high_reviews", "medium_reviews", "low_reviews",
            "shortlisted_jobs",
        )
    }
    values.update({"qualified_jobs": qualified, "ready_to_apply_jobs": ready})
    values.update({
        "candidate_yield": _ratio(item.job_candidates, item.results_inspected),
        "new_job_yield": _ratio(item.new_jobs, item.results_inspected),
        "noise_rate": _ratio(item.rejected_noise, item.results_inspected),
        "resolution_failure_rate": _ratio(item.resolution_failures, item.results_inspected),
        "shortlist_yield": _ratio(item.shortlisted_jobs, item.new_jobs),
        "qualified_yield": _ratio(qualified, item.new_jobs),
        "ready_to_apply_yield": _ratio(ready, item.new_jobs),
        "seconds_per_new_job": _ratio(item.duration_seconds, item.new_jobs),
        "results_per_new_job": _ratio(item.results_inspected, item.new_jobs),
    })
    return values


def evidence_maturity(metrics: Mapping, config: StrategyConfig = DEFAULT_CONFIG) -> str:
    runs, results = metrics["runs_seen"], metrics["results_inspected"]
    if runs < config.insufficient_runs or results is None or results < config.insufficient_results:
        return "INSUFFICIENT"
    core_available = all(metrics[name] is not None for name in (
        "new_jobs", "rejected_noise", "resolution_failures",
    ))
    if runs >= config.mature_runs and results >= config.mature_results and core_available:
        return "MATURE"
    return "EARLY"


def recommend(metrics: Mapping, maturity: str, config: StrategyConfig = DEFAULT_CONFIG):
    reasons = []
    new_yield, noise, failure = (
        metrics["new_job_yield"], metrics["noise_rate"], metrics["resolution_failure_rate"],
    )
    if maturity == "INSUFFICIENT":
        return "REVIEW", ("STRATEGY_INSUFFICIENT_EVIDENCE",)
    if new_yield is None or noise is None or failure is None or metrics["new_jobs"] is None:
        return "REVIEW", ("STRATEGY_METRICS_UNAVAILABLE",)
    if noise >= config.high_noise_rate:
        reasons.append("STRATEGY_HIGH_NOISE")
    if failure >= config.high_failure_rate:
        reasons.append("STRATEGY_HIGH_RESOLUTION_FAILURE")
    downstream = [metrics[name] for name in (
        "shortlisted_jobs", "qualified_jobs", "ready_to_apply_jobs",
    ) if metrics[name] is not None]
    downstream_positive = not downstream or any(value > 0 for value in downstream)
    zero_downstream = bool(downstream) and all(value == 0 for value in downstream)
    bad = bool(reasons)
    if (
        maturity == "MATURE" and new_yield <= config.retire_max_new_job_yield
        and (bad or zero_downstream)
    ):
        reasons.append("STRATEGY_MATURE_PERSISTENT_POOR_VALUE")
        return "RETIRE_CANDIDATE", tuple(reasons)
    if (
        metrics["new_jobs"] >= config.expand_min_new_jobs
        and new_yield >= config.expand_new_job_yield and not bad and downstream_positive
    ):
        reasons.extend(("STRATEGY_PRODUCTIVE_QUERY", "STRATEGY_EXPANSION_WARRANTED"))
        return "EXPAND", tuple(reasons)
    if metrics["new_jobs"] >= config.keep_min_new_jobs and new_yield >= config.keep_new_job_yield and not bad:
        return "KEEP", ("STRATEGY_USEFUL_YIELD", "STRATEGY_ACCEPTABLE_COST")
    reasons.append("STRATEGY_MIXED_OR_LOW_VALUE")
    return "REVIEW", tuple(reasons)


def _aggregate(items: Sequence[StrategyRecommendation]) -> dict:
    metric_names = (
        "pages_inspected", "results_inspected", "job_candidates", "new_jobs", "known_jobs",
        "rejected_noise", "resolution_failures", "shortlisted_jobs", "qualified_jobs",
        "ready_to_apply_jobs", "duration_seconds",
    )
    output = {"queries": len(items), "measured_runs": sum(i.metrics["runs_seen"] for i in items)}
    for name in metric_names:
        values = [item.metrics[name] for item in items]
        output[name] = None if any(value is None for value in values) else sum(values)
    output.update({
        "candidate_yield": _ratio(output["job_candidates"], output["results_inspected"]),
        "new_job_yield": _ratio(output["new_jobs"], output["results_inspected"]),
        "noise_rate": _ratio(output["rejected_noise"], output["results_inspected"]),
        "seconds_per_new_job": _ratio(output["duration_seconds"], output["new_jobs"]),
    })
    return output


def family_analysis(items: Sequence[StrategyRecommendation]) -> dict:
    axes = {
        "category": lambda item: item.dimensions.query_category,
        "role_family": lambda item: item.dimensions.role_family,
        "geography": lambda item: item.dimensions.geography,
        "source_intent": lambda item: item.dimensions.source_intent,
    }
    output = {}
    for axis, getter in axes.items():
        groups = {}
        for item in items:
            groups.setdefault(getter(item), []).append(item)
        output[axis] = {name: _aggregate(group) for name, group in sorted(groups.items())}
    technology_groups = {}
    for item in items:
        for technology in item.dimensions.technologies:
            technology_groups.setdefault(technology, []).append(item)
    output["technology"] = {
        name: _aggregate(group) for name, group in sorted(technology_groups.items())
    }
    return output


def _job_sets(connection: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, set[int]]:
    if not run_ids or not _has_table(connection, "workflow_run_job_queries"):
        return {}
    marks = ",".join("?" for _ in run_ids)
    output = {}
    for row in connection.execute(
        f"SELECT source_query,job_id FROM workflow_run_job_queries WHERE run_id IN ({marks})",
        run_ids,
    ):
        output.setdefault(row[0], set()).add(row[1])
    return output


def redundancy_analysis(
    items: Sequence[StrategyRecommendation], job_sets: Mapping[str, set[int]],
    config: StrategyConfig = DEFAULT_CONFIG,
) -> tuple[dict, ...]:
    findings = []
    for index, left in enumerate(items):
        for right in items[index + 1:]:
            a, b = job_sets.get(left.query, set()), job_sets.get(right.query, set())
            overlap = len(a & b)
            union = len(a | b)
            if overlap < config.redundancy_min_jobs or not union:
                continue
            ratio = overlap / union
            if ratio < config.redundancy_overlap:
                continue
            ly, ry = left.metrics["new_job_yield"], right.metrics["new_job_yield"]
            lc, rc = left.metrics["seconds_per_new_job"], right.metrics["seconds_per_new_job"]
            worse = None
            if ly is not None and ry is not None and ly != ry:
                worse = left if ly < ry * 0.5 else right if ry < ly * 0.5 else None
            if worse is None and lc is not None and rc is not None:
                worse = left if lc > rc * config.redundancy_cost_multiplier else right if rc > lc * config.redundancy_cost_multiplier else None
            if worse:
                findings.append({
                    "query_a": left.query, "query_b": right.query,
                    "overlap_jobs": overlap, "overlap_rate": ratio,
                    "redundant_candidate": worse.query,
                    "reason_codes": ["REDUNDANCY_ACTUAL_JOB_OVERLAP", "REDUNDANCY_WORSE_COST_OR_YIELD"],
                })
    return tuple(sorted(findings, key=lambda item: (item["redundant_candidate"].casefold(), item["query_a"].casefold())))


def coverage_analysis(
    items: Sequence[StrategyRecommendation], redundancies: Sequence[Mapping],
    config: StrategyConfig = DEFAULT_CONFIG,
) -> tuple[dict, ...]:
    redundant = {item["redundant_candidate"] for item in redundancies}
    targets = [
        *(("GEOGRAPHY", value, lambda item, v=value: item.dimensions.geography == v) for value in config.target_geographies),
        *(("ROLE", value, lambda item, v=value: item.dimensions.role_family == v) for value in config.target_role_families),
        *(("SOURCE_CATEGORY", value, lambda item, v=value: item.dimensions.query_category == v) for value in config.target_source_families),
        *(("ROLE_GEOGRAPHY", f"{role}|{geo}", lambda item, r=role, g=geo: item.dimensions.role_family == r and item.dimensions.geography == g) for role, geo in config.target_combinations),
    ]
    output = []
    for axis, target, matcher in targets:
        matches = [item for item in items if matcher(item)]
        if not matches:
            status = "UNTESTED"
        elif all(item.query in redundant for item in matches):
            status = "REDUNDANT"
        elif any(item.maturity in {"EARLY", "MATURE"} and (item.metrics["new_jobs"] or 0) > 0 for item in matches):
            status = "COVERED"
        else:
            status = "WEAK_COVERAGE"
        output.append({"axis": axis, "target": target, "status": status,
                       "queries": [item.query for item in matches]})
    return tuple(output)


def _render_query(dimensions: QueryDimensions, geography: str, seniority: str = "UNSPECIFIED") -> str:
    role = {
        "BACKEND": "backend developer", "FRONTEND": "frontend developer",
        "FULLSTACK": "full stack developer", "SOFTWARE_ENGINEERING": "software engineer",
        "PYTHON": "python developer", "PHP_LARAVEL": "php laravel developer",
        "JAVA": "java developer",
    }.get(dimensions.role_family, "software developer")
    prefix = "junior " if seniority == "JUNIOR" else ""
    site = PROVIDER_DOMAINS.get(dimensions.source_intent)
    return " ".join(part for part in (f"site:{site}" if site else "", prefix + role, geography) if part)


def generate_proposals(
    items: Sequence[StrategyRecommendation], existing_queries: Sequence[str],
    max_proposals: int, config: StrategyConfig = DEFAULT_CONFIG,
) -> tuple[QueryProposal, ...]:
    existing = {normalize_query(query) for query in existing_queries}
    candidates = []
    covered_geographies = {item.dimensions.geography for item in items if item.maturity != "INSUFFICIENT"}
    for parent in items:
        if parent.recommendation != "EXPAND" or parent.dimensions.role_family in {"UNKNOWN", "OTHER"}:
            continue
        for geography in config.target_geographies:
            if geography == parent.dimensions.geography:
                continue
            query = _render_query(parent.dimensions, geography, parent.dimensions.seniority_intent)
            key = normalize_query(query)
            if key in existing:
                continue
            reasons = ["EXPANSION_FROM_PRODUCTIVE_ROLE"]
            if parent.dimensions.source_intent in PROVIDER_DOMAINS:
                reasons.append("EXPANSION_FROM_PRODUCTIVE_PROVIDER")
            if geography not in covered_geographies:
                reasons.append("TARGET_GEOGRAPHY_UNDERCOVERED")
            candidates.append((
                -(parent.metrics["new_job_yield"] or 0), parent.query.casefold(), key,
                query, parent.query, tuple(reasons),
            ))
    unique = {}
    for candidate in sorted(candidates):
        unique.setdefault(candidate[2], candidate)
    proposals = []
    for rank, candidate in enumerate(list(unique.values())[:max_proposals], 1):
        proposals.append(QueryProposal(
            candidate[3], candidate[4], rank, candidate[5], parse_dimensions(candidate[3]),
        ))
    return tuple(proposals)


def analyze_strategy(
    connection: sqlite3.Connection, active_queries: Sequence[str], run_ids: Sequence[str],
    max_proposals: int = 10, config: StrategyConfig = DEFAULT_CONFIG,
) -> StrategyAnalysis:
    measured = {item.query: item for item in analyze(connection, run_ids)}
    recommendations = []
    for query in active_queries:
        item = measured.get(query, QueryPerformance(query, classify_query(query), 0))
        qualified, ready = _downstream_counts(connection, run_ids, query, item.new_jobs)
        metrics = metrics_for(item, qualified, ready)
        maturity = evidence_maturity(metrics, config)
        recommendation, reasons = recommend(metrics, maturity, config)
        recommendations.append(StrategyRecommendation(
            query, parse_dimensions(query), maturity, recommendation, reasons, metrics,
        ))
    recommendations.sort(key=lambda item: item.query.casefold())
    job_sets = _job_sets(connection, run_ids)
    redundancies = redundancy_analysis(recommendations, job_sets, config)
    coverage = coverage_analysis(recommendations, redundancies, config)
    families = family_analysis(recommendations)
    proposals = generate_proposals(recommendations, active_queries, max_proposals, config)
    payload = {
        "run_ids": list(run_ids), "active_queries": list(active_queries),
        "config": asdict(config),
        "recommendations": [asdict(item) for item in recommendations],
        "job_sets": {key: sorted(value) for key, value in sorted(job_sets.items())},
    }
    fingerprint = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return StrategyAnalysis(None, tuple(run_ids), tuple(recommendations), families, coverage,
                            redundancies, proposals, fingerprint, utc_now())


def persist_analysis(
    connection: sqlite3.Connection, analysis: StrategyAnalysis,
    config: StrategyConfig = DEFAULT_CONFIG,
) -> StrategyAnalysis:
    existing = connection.execute(
        "SELECT * FROM query_strategy_runs WHERE policy_version=? AND input_fingerprint=?",
        (SEARCH_STRATEGY_POLICY_VERSION, analysis.input_fingerprint),
    ).fetchone()
    if existing:
        return StrategyAnalysis(
            existing["strategy_run_id"], analysis.run_ids, analysis.recommendations,
            analysis.families, analysis.coverage, analysis.redundancies, analysis.proposals,
            analysis.input_fingerprint, existing["created_at"], True,
        )
    summary = {
        "recommendations": dict(Counter(item.recommendation for item in analysis.recommendations)),
        "maturity": dict(Counter(item.maturity for item in analysis.recommendations)),
        "families": analysis.families, "coverage": analysis.coverage,
        "redundancies": analysis.redundancies,
    }
    with connection:
        cursor = connection.execute(
            """INSERT INTO query_strategy_runs
               (policy_version,source_run_scope,input_fingerprint,created_at,config_json,summary_json)
               VALUES (?,?,?,?,?,?)""",
            (SEARCH_STRATEGY_POLICY_VERSION, json.dumps(analysis.run_ids, separators=(",", ":")),
             analysis.input_fingerprint, analysis.created_at,
             json.dumps(asdict(config), sort_keys=True, separators=(",", ":")),
             json.dumps(summary, sort_keys=True, separators=(",", ":"))),
        )
        strategy_run_id = cursor.lastrowid
        for item in analysis.recommendations:
            connection.execute(
                """INSERT INTO query_strategy_recommendations
                   (strategy_run_id,query,recommendation,evidence_maturity,reason_codes_json,
                    metrics_json,dimensions_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (strategy_run_id, item.query, item.recommendation, item.maturity,
                 json.dumps(item.reasons, separators=(",", ":")),
                 json.dumps(item.metrics, sort_keys=True, separators=(",", ":")),
                 json.dumps(asdict(item.dimensions), sort_keys=True, separators=(",", ":")),
                 analysis.created_at),
            )
        for proposal in analysis.proposals:
            connection.execute(
                """INSERT INTO query_strategy_proposals
                   (strategy_run_id,query,parent_query,proposal_rank,reason_codes_json,
                    dimensions_json,created_at) VALUES (?,?,?,?,?,?,?)""",
                (strategy_run_id, proposal.query, proposal.parent_query, proposal.rank,
                 json.dumps(proposal.reasons, separators=(",", ":")),
                 json.dumps(asdict(proposal.dimensions), sort_keys=True, separators=(",", ":")),
                 analysis.created_at),
            )
    return replace(analysis, strategy_run_id=strategy_run_id)


def _format(value, ratio=False):
    if value is None:
        return "unavailable"
    return f"{value:.3f}" if ratio or isinstance(value, float) else str(value)


def print_report(analysis: StrategyAnalysis, *, show_families=False, show_coverage=False,
                 show_proposals=False, verbose=False):
    print("SEARCH STRATEGY v2")
    print(f"Runs analyzed: {len(analysis.run_ids)}")
    print(f"Queries analyzed: {len(analysis.recommendations)}")
    print(f"Queries with sufficient evidence: {sum(i.maturity != 'INSUFFICIENT' for i in analysis.recommendations)}")
    for value in RECOMMENDATIONS:
        print(f"{value}: {sum(i.recommendation == value for i in analysis.recommendations)}")
    maturity = Counter(item.maturity for item in analysis.recommendations)
    print("Maturity: " + ", ".join(f"{name}={maturity[name]}" for name in MATURITIES))
    coverage = Counter(item["status"] for item in analysis.coverage)
    print("Coverage: " + ", ".join(f"{name}={coverage[name]}" for name in ("COVERED", "WEAK_COVERAGE", "UNTESTED", "REDUNDANT")))
    print(f"Redundancy findings: {len(analysis.redundancies)}")
    print(f"Proposed queries: {len(analysis.proposals)}")
    if verbose:
        for item in analysis.recommendations:
            m = item.metrics
            print(f"\nquery: {item.query}\ncategory: {item.dimensions.query_category}\nmaturity: {item.maturity}\nrecommendation: {item.recommendation}")
            print(f"runs: {m['runs_seen']}\nresults: {_format(m['results_inspected'])}\nnew_jobs: {_format(m['new_jobs'])}\nnew_job_yield: {_format(m['new_job_yield'], True)}")
            print(f"noise_rate: {_format(m['noise_rate'], True)}\nshortlisted: {_format(m['shortlisted_jobs'])}\nqualified: {_format(m['qualified_jobs'])}\nready_to_apply: {_format(m['ready_to_apply_jobs'])}\nseconds_per_new: {_format(m['seconds_per_new_job'], True)}")
            print("reason_codes: " + ", ".join(item.reasons))
    if show_families:
        for axis, groups in analysis.families.items():
            print(f"\n{axis.upper()} PERFORMANCE")
            for name, metrics in groups.items():
                print(f"{name}: queries={metrics['queries']}; results={_format(metrics['results_inspected'])}; new={_format(metrics['new_jobs'])}; yield={_format(metrics['new_job_yield'], True)}")
    if show_coverage:
        print("\nCOVERAGE MATRIX")
        for item in analysis.coverage:
            print(f"{item['axis']} {item['target']}: {item['status']}")
    if show_proposals:
        print("\nTOP PROPOSED QUERIES")
        for item in analysis.proposals:
            print(f"{item.rank}. {item.query} <- {item.parent_query}; {', '.join(item.reasons)}")


def build_parser():
    parser = argparse.ArgumentParser(description="Deterministic Search Strategy / Coverage v2")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--query-file", type=Path, default=DEFAULT_QUERY_FILE)
    parser.add_argument("--recent-runs", type=int, default=10)
    parser.add_argument("--since")
    parser.add_argument("--max-proposals", type=int, default=10)
    parser.add_argument("--show-families", action="store_true")
    parser.add_argument("--show-coverage", action="store_true")
    parser.add_argument("--show-proposals", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.recent_runs < 1:
        parser.error("--recent-runs must be at least 1")
    if not 0 <= args.max_proposals <= 10:
        parser.error("--max-proposals must be between 0 and 10")
    queries = load_queries(args.query_file)
    connection = connect_database(args.database)
    try:
        run_ids = select_run_ids(connection, args.recent_runs, args.since)
        analysis = analyze_strategy(connection, queries, run_ids, args.max_proposals)
        analysis = persist_analysis(connection, analysis)
    finally:
        connection.close()
    print_report(analysis, show_families=args.show_families,
                 show_coverage=args.show_coverage, show_proposals=args.show_proposals,
                 verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
