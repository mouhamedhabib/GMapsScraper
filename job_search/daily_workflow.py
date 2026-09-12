"""Daily, deterministic orchestration for the existing job-search pipeline.

The safe default runs job discovery, bounded completion, scoped hard filtering,
review priority, and reporting. Google Maps is opt-in with ``--with-maps``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Sequence
from uuid import uuid4

from job_search.discovery import DEFAULT_QUERY_FILE, discover_jobs
from job_search.filtering import DEFAULT_POLICY_VERSION, filter_stored_jobs
from job_search.repair_reviews import repair_review_jobs
from job_search.review_priority import PRIORITIES, load_review_priorities
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


DEFAULT_CONFIG = Path("config/daily_workflow.json")
DEFAULT_REPORT_DIR = Path("data/reports")


@dataclass
class WorkflowOptions:
    database: Path = DEFAULT_DATABASE
    report_dir: Path = DEFAULT_REPORT_DIR
    job_query_file: Path = DEFAULT_QUERY_FILE
    maps_query_file: Path = Path("queries.txt")
    maps_output_folder: Path = Path("CSV_FILES")
    job_limit: int = 3
    recent_days: int = 14
    delay: float = 3
    timeout: float = 15
    windowed: bool = True
    completion_limit: int = 20
    maps_limit: int = 3
    maps_threads: int = 1
    run_maps: bool = False
    job_discovery: bool = True
    completion: bool = True
    hard_filter: bool = True
    priority: bool = True
    full_refilter: bool = False
    include_medium: bool = False
    verbose: bool = False
    mode: str = "DEFAULT"


def _json_value(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _run_id(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]


def _create_run(connection: sqlite3.Connection, run_id: str, options: WorkflowOptions) -> str:
    started_at = utc_now()
    with connection:
        connection.execute(
            """INSERT INTO workflow_runs
               (run_id, started_at, status, mode, maps_enabled,
                job_discovery_enabled, completion_enabled, filter_enabled,
                priority_enabled, created_at)
               VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, started_at, options.mode, int(options.run_maps),
                int(options.job_discovery), int(options.completion),
                int(options.hard_filter), int(options.priority), started_at,
            ),
        )
    return started_at


def _finish_run(
    connection: sqlite3.Connection, run_id: str, status: str,
    errors: Sequence[str],
) -> str:
    finished_at = utc_now()
    with connection:
        connection.execute(
            """UPDATE workflow_runs SET finished_at=?, status=?, error_summary=?
               WHERE run_id=?""",
            (finished_at, status, "\n".join(errors) or None, run_id),
        )
    return finished_at


def _attach_jobs(
    connection: sqlite3.Connection, run_id: str, states: dict[int, str],
) -> None:
    now = utc_now()
    with connection:
        for job_id, state in states.items():
            connection.execute(
                """INSERT INTO workflow_run_jobs
                   (run_id, job_id, discovery_state, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id, job_id) DO UPDATE SET discovery_state=
                     CASE WHEN workflow_run_jobs.discovery_state='NEW' THEN 'NEW'
                          ELSE excluded.discovery_state END""",
                (run_id, int(job_id), state, now),
            )


def _run_job_ids(connection: sqlite3.Connection, run_id: str, state: str = "NEW") -> list[int]:
    return [
        row["job_id"] for row in connection.execute(
            """SELECT job_id FROM workflow_run_jobs
               WHERE run_id=? AND discovery_state=? ORDER BY job_id""",
            (run_id, state),
        )
    ]


def _decision_rows(
    connection: sqlite3.Connection, run_id: str, policy_version: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT j.job_id, f.status AS decision, j.title,
                  COALESCE(c.canonical_name, '') AS company,
                  COALESCE(j.location_text, '') AS location,
                  COALESCE(j.published_at, '') AS published_at,
                  COALESCE(s.provider, '') AS provider,
                  COALESCE(s.source_type, 'UNKNOWN') AS source_type,
                  COALESCE(s.employer_relationship, 'UNKNOWN') AS employer_relationship,
                  f.reasons_json, j.canonical_url AS job_url
           FROM workflow_run_jobs w
           JOIN jobs j ON j.job_id=w.job_id
           LEFT JOIN companies c ON c.company_id=j.company_id
           LEFT JOIN job_filter_results f
             ON f.job_id=j.job_id AND f.policy_version=?
           LEFT JOIN job_sources s ON s.job_source_id=(
               SELECT candidate.job_source_id FROM job_sources candidate
               WHERE candidate.job_id=j.job_id ORDER BY candidate.job_source_id LIMIT 1
           )
           WHERE w.run_id=? AND w.discovery_state='NEW'
           ORDER BY j.job_id""",
        (policy_version, run_id),
    ).fetchall()


def build_report(
    connection: sqlite3.Connection, run_id: str, options: WorkflowOptions,
    status: str, started_at: str, finished_at: str, errors: Sequence[str],
    maps_stats: dict | None = None, discovery_stats: dict | None = None,
    completion_stats: dict | None = None,
    priority_items: Sequence | None = None,
) -> dict:
    """Build a report using only jobs explicitly attached as NEW to this run."""
    rows = _decision_rows(connection, run_id, DEFAULT_POLICY_VERSION)
    filter_counts = Counter(row["decision"] for row in rows if row["decision"])
    new_ids = [row["job_id"] for row in rows]
    priorities = list(priority_items) if priority_items is not None else (
        load_review_priorities(connection, DEFAULT_POLICY_VERSION, job_ids=new_ids)
        if options.priority else []
    )
    priority_by_id = {item.job_id: item for item in priorities}
    priority_counts = Counter(item.priority for item in priorities)
    shortlist = []
    for row in rows:
        priority_item = priority_by_id.get(row["job_id"])
        priority = priority_item.priority if priority_item else ""
        if row["decision"] != "PASS" and not (
            row["decision"] == "REVIEW" and (
                priority == "HIGH" or (options.include_medium and priority == "MEDIUM")
            )
        ):
            continue
        try:
            reasons = [item.get("code", "") for item in json.loads(row["reasons_json"] or "[]")]
        except (TypeError, json.JSONDecodeError):
            reasons = []
        shortlist.append({
            "job_id": row["job_id"], "decision": row["decision"],
            "priority": priority or None, "title": row["title"] or "",
            "company": row["company"], "location": row["location"],
            "published_at": row["published_at"], "provider": row["provider"],
            "source_type": row["source_type"],
            "employer_relationship": row["employer_relationship"],
            "filter_reasons": reasons,
            "priority_reasons": list(priority_item.priority_reasons) if priority_item else [],
            "job_url": row["job_url"],
        })
    membership = Counter(
        row["discovery_state"] for row in connection.execute(
            "SELECT discovery_state FROM workflow_run_jobs WHERE run_id=?", (run_id,)
        )
    )
    return _json_value({
        "run": {
            "run_id": run_id, "started_at": started_at,
            "finished_at": finished_at, "status": status, "mode": options.mode,
            "maps_enabled": options.run_maps,
            "job_discovery_enabled": options.job_discovery,
            "completion_enabled": options.completion,
            "filter_enabled": options.hard_filter,
            "priority_enabled": options.priority,
            "errors": list(errors),
        },
        "company_discovery": maps_stats or {"new": 0},
        "job_discovery": {
            "new": membership["NEW"], "known": membership["KNOWN"],
            "updated": membership["UPDATED"],
            "pipeline": discovery_stats or {},
        },
        "completion": completion_stats or {},
        "filter_counts": {name: filter_counts[name] for name in ("PASS", "REVIEW", "REJECT")},
        "priority_counts": {name: priority_counts[name] for name in PRIORITIES},
        "shortlist": shortlist,
    })


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish_report(report_dir: Path, report: dict, update_latest: bool = True) -> Path:
    report_path = Path(report_dir) / f"run_{report['run']['run_id']}.json"
    _atomic_json(report_path, report)
    if update_latest:
        _atomic_json(Path(report_dir) / "latest.json", report)
    return report_path


def _maps_runner(options: WorkflowOptions) -> dict:
    from maps import run_maps_discovery
    return run_maps_discovery(
        query_file=options.maps_query_file, limit=options.maps_limit,
        threads=options.maps_threads, output_folder=options.maps_output_folder,
        browser_wait=max(1, int(options.timeout)), windowed=options.windowed,
        low_resource=True, verbose=options.verbose,
    )


def run_workflow(
    options: WorkflowOptions,
    *,
    maps_runner: Callable[[WorkflowOptions], dict] = _maps_runner,
    discovery_runner: Callable[..., dict] = discover_jobs,
    completion_runner: Callable[..., dict] = repair_review_jobs,
    filter_runner: Callable[..., dict] = filter_stored_jobs,
) -> dict:
    """Execute one persistent workflow run; phase failures degrade to PARTIAL."""
    run_id = _run_id()
    errors: list[str] = []
    maps_stats: dict = {"new": 0}
    discovery_stats: dict = {}
    completion_stats: dict = {}
    try:
        connection = connect_database(options.database)
    except Exception as error:
        return {
            "run": {"run_id": run_id, "status": "FAILED", "mode": options.mode,
                    "errors": [f"DATABASE: {type(error).__name__}: {error}"]},
            "company_discovery": maps_stats, "job_discovery": {},
            "completion": {}, "filter_counts": {}, "priority_counts": {},
            "shortlist": [],
        }

    started_at = _create_run(connection, run_id, options)
    try:
        if options.run_maps:
            try:
                maps_stats = maps_runner(options) or {"new": 0}
            except KeyboardInterrupt:
                raise
            except Exception as error:
                errors.append(f"MAPS_DISCOVERY: {type(error).__name__}: {error}")

        if options.job_discovery:
            try:
                discovery_stats = discovery_runner(
                    query_file=options.job_query_file, database=options.database,
                    limit=options.job_limit, delay=options.delay,
                    timeout=options.timeout, windowed=options.windowed,
                    verbose=options.verbose, recent_days=options.recent_days,
                ) or {}
                states = {
                    int(job_id): state
                    for job_id, state in discovery_stats.get("job_states", {}).items()
                }
                _attach_jobs(connection, run_id, states)
            except KeyboardInterrupt:
                raise
            except Exception as error:
                errors.append(f"JOB_DISCOVERY: {type(error).__name__}: {error}")

        new_ids = _run_job_ids(connection, run_id)
        updated_ids = _run_job_ids(connection, run_id, "UPDATED")
        work_ids = new_ids + updated_ids

        # Completion operates on REVIEW rows, so establish the current v1.1
        # decision for newly discovered jobs before asking the existing
        # completion module to select from that bounded set.
        if options.hard_filter:
            try:
                filter_runner(
                    connection, DEFAULT_POLICY_VERSION, rebuild=True,
                    job_ids=work_ids, verbose=options.verbose,
                )
            except Exception as error:
                errors.append(f"HARD_FILTER_INITIAL: {type(error).__name__}: {error}")

        if options.completion and work_ids:
            try:
                completion_stats = completion_runner(
                    connection, DEFAULT_POLICY_VERSION,
                    limit=options.completion_limit, job_ids=work_ids,
                    timeout=options.timeout, windowed=options.windowed,
                    verbose=options.verbose, refilter=False,
                ) or {}
                if completion_stats.get("failed") or completion_stats.get("blocked"):
                    errors.append(
                        "JOB_COMPLETION: "
                        f"{completion_stats.get('failed', 0)} failed, "
                        f"{completion_stats.get('blocked', 0)} blocked"
                    )
            except KeyboardInterrupt:
                raise
            except Exception as error:
                errors.append(f"JOB_COMPLETION: {type(error).__name__}: {error}")

        if options.hard_filter:
            try:
                filter_runner(
                    connection, DEFAULT_POLICY_VERSION, rebuild=True,
                    job_ids=None if options.full_refilter else work_ids,
                    verbose=options.verbose,
                )
            except Exception as error:
                errors.append(f"HARD_FILTER: {type(error).__name__}: {error}")

        priority_items = []
        if options.priority:
            try:
                priority_items = load_review_priorities(
                    connection, DEFAULT_POLICY_VERSION, job_ids=new_ids,
                )
            except Exception as error:
                errors.append(f"REVIEW_PRIORITY: {type(error).__name__}: {error}")

        status = "PARTIAL" if errors else "SUCCESS"
        finished_at = _finish_run(connection, run_id, status, errors)
        try:
            report = build_report(
                connection, run_id, options, status, started_at, finished_at, errors,
                maps_stats, discovery_stats, completion_stats, priority_items,
            )
        except Exception as error:
            errors.append(f"DAILY_REPORT: {type(error).__name__}: {error}")
            status = "PARTIAL"
            finished_at = _finish_run(connection, run_id, status, errors)
            report = _json_value({
                "run": {
                    "run_id": run_id, "started_at": started_at,
                    "finished_at": finished_at, "status": status,
                    "mode": options.mode, "errors": list(errors),
                },
                "company_discovery": maps_stats,
                "job_discovery": discovery_stats,
                "completion": completion_stats, "filter_counts": {},
                "priority_counts": {}, "shortlist": [],
            })
        try:
            report_path = publish_report(options.report_dir, report, update_latest=True)
            report["report_path"] = str(report_path)
        except Exception as error:
            errors.append(f"DAILY_REPORT: {type(error).__name__}: {error}")
            status = "PARTIAL"
            _finish_run(connection, run_id, status, errors)
            report["run"]["status"] = status
            report["run"]["errors"] = list(errors)
        return report
    except KeyboardInterrupt:
        _finish_run(connection, run_id, "INTERRUPTED", errors + ["KeyboardInterrupt"])
        return {
            "run": {"run_id": run_id, "started_at": started_at,
                    "finished_at": utc_now(), "status": "INTERRUPTED",
                    "mode": options.mode, "errors": errors + ["KeyboardInterrupt"]},
            "company_discovery": _json_value(maps_stats),
            "job_discovery": _json_value(discovery_stats),
            "completion": _json_value(completion_stats), "filter_counts": {},
            "priority_counts": {}, "shortlist": [],
        }
    finally:
        connection.close()


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("daily workflow config must contain a JSON object")
    return payload


def _option(args, config: dict, name: str, default):
    value = getattr(args, name, None)
    return value if value is not None else config.get(name, default)


def options_from_args(args) -> WorkflowOptions:
    config = _load_config(args.config)
    options = WorkflowOptions(
        database=Path(_option(args, config, "database", DEFAULT_DATABASE)),
        report_dir=Path(_option(args, config, "report_dir", DEFAULT_REPORT_DIR)),
        job_query_file=Path(_option(args, config, "job_query_file", DEFAULT_QUERY_FILE)),
        maps_query_file=Path(_option(args, config, "maps_query_file", "queries.txt")),
        maps_output_folder=Path(_option(args, config, "maps_output_folder", "CSV_FILES")),
        job_limit=int(_option(args, config, "job_limit", 3)),
        recent_days=int(_option(args, config, "recent_days", 14)),
        delay=float(_option(args, config, "delay", 3)),
        timeout=float(_option(args, config, "timeout", 15)),
        windowed=bool(_option(args, config, "windowed", True)),
        completion_limit=int(_option(args, config, "completion_limit", 20)),
        maps_limit=int(_option(args, config, "maps_limit", 3)),
        maps_threads=int(_option(args, config, "maps_threads", 1)),
        run_maps=bool(_option(args, config, "run_maps", False)),
        full_refilter=args.full_refilter, include_medium=args.include_medium,
        verbose=args.verbose,
    )
    if args.with_maps:
        options.run_maps = True
    if args.skip_maps:
        options.run_maps = False
    if args.skip_discovery:
        options.job_discovery = False
    if args.jobs_only:
        options.mode, options.run_maps = "JOBS_ONLY", False
    elif args.maps_only:
        options.mode = "MAPS_ONLY"
        options.run_maps, options.job_discovery = True, False
        options.completion = options.hard_filter = options.priority = False
    else:
        options.mode = "DEFAULT_WITH_MAPS" if options.run_maps else "DEFAULT"
    return options


def validate_dry_run(options: WorkflowOptions) -> dict:
    """Validate without opening a browser, creating paths, or migrating SQLite."""
    checks = {}
    if options.job_discovery:
        checks["job_query_file"] = options.job_query_file.is_file()
    if options.run_maps:
        checks["maps_query_file"] = options.maps_query_file.is_file()
    if options.database.exists():
        try:
            connection = sqlite3.connect(f"file:{options.database.resolve()}?mode=ro", uri=True)
            connection.execute("PRAGMA schema_version").fetchone()
            connection.close()
            checks["database"] = True
        except sqlite3.Error:
            checks["database"] = False
    else:
        checks["database_parent"] = options.database.parent.exists() and os.access(options.database.parent, os.W_OK)
    report_parent = options.report_dir
    while not report_parent.exists() and report_parent != report_parent.parent:
        report_parent = report_parent.parent
    checks["report_directory"] = report_parent.is_dir() and os.access(report_parent, os.W_OK)
    checks["modules"] = all(callable(item) for item in (
        discover_jobs, repair_review_jobs, filter_stored_jobs, load_review_priorities,
    ))
    return {"dry_run": True, "valid": all(checks.values()), "checks": checks,
            "options": _json_value(asdict(options))}


def load_report_only(options: WorkflowOptions, run_id: str | None = None) -> dict:
    path = options.report_dir / (f"run_{run_id}.json" if run_id else "latest.json")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def print_terminal_summary(report: dict) -> None:
    run = report.get("run", {})
    discovery = report.get("job_discovery", {})
    print(f"Run: {run.get('run_id', '-')}")
    print(f"Status: {run.get('status', '-')}")
    print(f"New companies: {report.get('company_discovery', {}).get('new', 0)}")
    print(f"New jobs: {discovery.get('new', 0)}")
    print("Filter results for NEW jobs:")
    for name in ("PASS", "REVIEW", "REJECT"):
        print(f"  {name}: {report.get('filter_counts', {}).get(name, 0)}")
    print("Review priorities for NEW REVIEW jobs:")
    for name in PRIORITIES:
        print(f"  {name}: {report.get('priority_counts', {}).get(name, 0)}")
    print(f"Shortlist: {len(report.get('shortlist', []))}")
    if report.get("report_path"):
        print(f"Report: {report['report_path']}")
    for error in run.get("errors", []):
        print(f"Warning: {error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--jobs-only", action="store_true")
    modes.add_argument("--maps-only", action="store_true")
    modes.add_argument("--report-only", action="store_true")
    parser.add_argument("--run-id", help="Run to display with --report-only")
    parser.add_argument("--with-maps", action="store_true")
    parser.add_argument("--skip-maps", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--job-query-file", type=Path)
    parser.add_argument("--maps-query-file", type=Path)
    parser.add_argument("--maps-output-folder", type=Path)
    parser.add_argument("--job-limit", type=int)
    parser.add_argument("--recent-days", type=int)
    parser.add_argument("--delay", type=float)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--completion-limit", type=int)
    parser.add_argument("--maps-limit", type=int)
    parser.add_argument("--maps-threads", type=int)
    parser.add_argument("--windowed", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--full-refilter", action="store_true")
    parser.add_argument("--include-medium", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _validate_values(parser: argparse.ArgumentParser, options: WorkflowOptions) -> None:
    for name in ("job_limit", "completion_limit"):
        if getattr(options, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be zero or greater")
    for name in ("recent_days", "maps_limit", "maps_threads"):
        if getattr(options, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if options.delay < 0:
        parser.error("--delay must be zero or greater")
    if options.timeout <= 0:
        parser.error("--timeout must be greater than zero")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        options = options_from_args(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    _validate_values(parser, options)
    if args.report_only:
        try:
            report = load_report_only(options, args.run_id)
        except (OSError, json.JSONDecodeError) as error:
            parser.error(f"could not read report: {error}")
        print_terminal_summary(report)
        return 0
    if args.run_id:
        parser.error("--run-id requires --report-only")
    if args.dry_run:
        result = validate_dry_run(options)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["valid"] else 2
    report = run_workflow(options)
    print_terminal_summary(report)
    return 0 if report["run"]["status"] in {"SUCCESS", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
