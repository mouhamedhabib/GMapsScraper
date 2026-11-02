"""Durable query telemetry and read-only Phase 6E report generation."""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from company_registry.normalization import canonical_json
from company_registry.storage import open_registry, open_registry_readonly
from utils.geography import extract_query_geography


REPORT_FORMAT = "company-registry-query-observability-v1"
DEFAULT_QUERY_REPORT_DIRECTORY = Path("data/reports/query_observability")
AVAILABLE = "available"
UNAVAILABLE_HISTORICAL = "unavailable_historical"
NOT_MEASURED = "not_measured"
HISTORICAL_NA = "N/A (historical data unavailable)"
NOT_MEASURED_NA = "N/A (not measured)"
TERMINAL_QUERY_STATUSES = {
    "COMPLETED", "NO_RESULTS", "VERIFICATION_ABORTED", "CONSENT_ABORTED",
    "NETWORK_INTERRUPTED", "SEARCH_TIMEOUT", "FAILED", "INTERRUPTED",
}
OUTCOME_MAP = {
    "new": "NEW",
    "known": "KNOWN",
    "same_run": "DUPLICATE",
    "updated": "UPDATED",
    "branch": "BRANCH",
    "ambiguous": "REVIEW",
    "quarantined": "REVIEW",
    "limit": "GLOBAL_LIMIT",
    "query_limit": "QUERY_LIMIT",
}


def stable_result_key(value: object) -> str:
    """Hash a stable Maps result identity without persisting its URL."""
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        raise ValueError("result identity must not be empty")
    return sha256(cleaned.encode("utf-8")).hexdigest()


def _writer_connection(database: str | Path, run_id: str, lease_owner: str):
    connection = open_registry(database)
    connection.execute("BEGIN IMMEDIATE")
    now = datetime.now(timezone.utc).isoformat()
    row = connection.execute(
        """SELECT status, lease_owner, lease_expires_at
             FROM discovery_runs WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        connection.close()
        raise LookupError(f"Unknown discovery run: {run_id}")
    if row["status"] != "RUNNING":
        connection.close()
        raise RuntimeError(f"Run {run_id!r} is not RUNNING")
    if row["lease_owner"] != lease_owner or not row["lease_expires_at"]:
        connection.close()
        raise RuntimeError(f"Run {run_id!r} writer lease is owned elsewhere")
    if row["lease_expires_at"] <= now:
        connection.close()
        raise RuntimeError(f"Run {run_id!r} writer lease has expired")
    return connection


def start_query_attempt(
    database: str | Path,
    run_id: str,
    query_index: int,
    lease_owner: str,
) -> None:
    """Durably mark a query attempt without changing discovery decisions."""
    now = datetime.now(timezone.utc).isoformat()
    connection = _writer_connection(database, run_id, lease_owner)
    try:
        cursor = connection.execute(
            """UPDATE discovery_run_queries
                  SET query_status='RUNNING', attempt_count=attempt_count + 1,
                      last_started_at=?, last_finished_at=NULL, updated_at=?
                WHERE run_id=? AND query_index=?""",
            (now, now, run_id, query_index),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"Unknown query index {query_index} for run {run_id!r}")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def finish_query_attempt(
    database: str | Path,
    run_id: str,
    query_index: int,
    status: str,
    lease_owner: str,
) -> None:
    """Durably record the latest terminal state for one query."""
    if status not in TERMINAL_QUERY_STATUSES:
        raise ValueError(f"Unsupported terminal query status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    connection = _writer_connection(database, run_id, lease_owner)
    try:
        cursor = connection.execute(
            """UPDATE discovery_run_queries
                  SET query_status=?, last_finished_at=?, updated_at=?
                WHERE run_id=? AND query_index=?""",
            (status, now, now, run_id, query_index),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"Unknown query index {query_index} for run {run_id!r}")
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def record_discovered_results(
    database: str | Path,
    run_id: str,
    query_index: int,
    result_identities: list[str],
    lease_owner: str,
) -> None:
    """Idempotently persist unique result identities discovered for a query."""
    keys = sorted({stable_result_key(item) for item in result_identities if str(item).strip()})
    if not keys:
        return
    now = datetime.now(timezone.utc).isoformat()
    connection = _writer_connection(database, run_id, lease_owner)
    try:
        connection.executemany(
            """INSERT INTO discovery_run_query_results
               (run_id, query_index, result_key, first_discovered_at,
                last_observed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(run_id, query_index, result_key) DO UPDATE SET
                   last_observed_at=excluded.last_observed_at""",
            [(run_id, query_index, key, now, now) for key in keys],
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def record_result_outcome(
    database: str | Path,
    run_id: str,
    query_index: int,
    result_identity: str,
    lease_owner: str,
    *,
    inspected: bool = False,
    outcome: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    """Upsert one unique result's latest outcome without incrementing counters."""
    normalized_outcome = OUTCOME_MAP.get(outcome, outcome)
    if normalized_outcome is not None and normalized_outcome not in set(OUTCOME_MAP.values()) | {"REJECTED"}:
        raise ValueError(f"Unsupported query result outcome: {outcome}")
    if rejection_reason and normalized_outcome is None:
        normalized_outcome = "REJECTED"
    key = stable_result_key(result_identity)
    now = datetime.now(timezone.utc).isoformat()
    connection = _writer_connection(database, run_id, lease_owner)
    try:
        connection.execute(
            """INSERT INTO discovery_run_query_results
               (run_id, query_index, result_key, detail_inspected,
                duplicate_observed, rejected_observed, outcome,
                rejection_reason, first_discovered_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, query_index, result_key) DO UPDATE SET
                   detail_inspected=max(detail_inspected, excluded.detail_inspected),
                   duplicate_observed=max(duplicate_observed, excluded.duplicate_observed),
                   rejected_observed=max(rejected_observed, excluded.rejected_observed),
                   outcome=COALESCE(excluded.outcome, outcome),
                   rejection_reason=COALESCE(excluded.rejection_reason, rejection_reason),
                   last_observed_at=excluded.last_observed_at""",
            (
                run_id, query_index, key, int(inspected),
                int(normalized_outcome == "DUPLICATE"),
                int(normalized_outcome == "REJECTED"), normalized_outcome,
                rejection_reason, now, now,
            ),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _safe_token(value: str) -> str:
    token = "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
    return token or "run"


def _linked_decision_counts(connection, run_id: str, schema_version: int) -> dict:
    by_index: dict[int, dict[str, int]] = {}
    if schema_version < 8:
        return by_index
    rows = connection.execute(
        """SELECT link.query_index, decision.classification,
                  decision.requires_review, count(*) AS count
             FROM discovery_run_query_decisions link
             JOIN discovery_run_decisions decision
               ON decision.run_id=link.run_id
              AND decision.decision_id=link.decision_id
            WHERE link.run_id=?
            GROUP BY link.query_index, decision.classification,
                     decision.requires_review""",
        (run_id,),
    ).fetchall()
    for row in rows:
        counts = by_index.setdefault(row["query_index"], {})
        key = "REVIEW" if row["requires_review"] else row["classification"]
        counts[key] = counts.get(key, 0) + row["count"]
    return by_index


def _global_decision_counts(connection, run_id: str) -> dict[str, int]:
    counts = {"NEW": 0, "KNOWN": 0, "UPDATED": 0, "REVIEW": 0}
    rows = connection.execute(
        """SELECT classification, requires_review, count(*) AS count
             FROM discovery_run_decisions
            WHERE run_id=?
            GROUP BY classification, requires_review""",
        (run_id,),
    ).fetchall()
    for row in rows:
        key = "REVIEW" if row["requires_review"] else row["classification"]
        if key in counts:
            counts[key] += row["count"]
    return counts


def _availability_summary(values: list[str]) -> str:
    if not values:
        return UNAVAILABLE_HISTORICAL
    if len(set(values)) == 1:
        return values[0]
    return "mixed"


def _query_rows(connection, run_id: str, schema_version: int, queries: list[str] | None):
    if schema_version >= 7:
        rows = connection.execute(
            "SELECT * FROM discovery_run_queries WHERE run_id=? ORDER BY query_index",
            (run_id,),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows], "durable_query_ledger"
    normalized = [item.strip() for item in (queries or []) if item.strip()]
    if normalized:
        return [
            {"query_index": index, "query_text": text}
            for index, text in enumerate(normalized)
        ], "caller_supplied_not_persisted"
    return [], "unavailable"


def build_query_observability_report(
    database: str | Path,
    run_id: str,
    output_directory: str | Path = DEFAULT_QUERY_REPORT_DIRECTORY,
    *,
    queries: list[str] | None = None,
) -> dict:
    """Generate deterministic JSON/text reports using read-only SQLite access."""
    database = Path(database)
    connection = open_registry_readonly(database)
    try:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        run = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if run is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        query_rows, identity_source = _query_rows(
            connection, run_id, schema_version, queries,
        )
        by_index = _linked_decision_counts(connection, run_id, schema_version)
        global_counts = _global_decision_counts(connection, run_id)
        decision_total = connection.execute(
            "SELECT count(*) FROM discovery_run_decisions WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        linked_decision_total = 0
        if schema_version >= 8:
            linked_decision_total = connection.execute(
                "SELECT count(*) FROM discovery_run_query_decisions WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        result_counts = {}
        if schema_version >= 8:
            for row in connection.execute(
                """SELECT query_index, count(*) AS discovered,
                          sum(detail_inspected) AS inspected,
                          sum(duplicate_observed) AS duplicates,
                          sum(rejected_observed) AS rejected
                     FROM discovery_run_query_results
                    WHERE run_id=? GROUP BY query_index""",
                (run_id,),
            ):
                result_counts[row["query_index"]] = dict(row)
        global_committed = global_counts["NEW"]
    finally:
        connection.close()

    report_queries = []
    totals = {
        "results_discovered": 0,
        "detail_pages_inspected": 0,
        "new_companies_committed": 0,
        "known_records": 0,
        "duplicate_records": 0,
        "review_only_records": 0,
        "rejected_records": 0,
        "updated_records": 0,
    }
    totals_available = {key: False for key in totals}
    durable_query_ledger = identity_source == "durable_query_ledger"
    linked_decisions_complete = (
        schema_version >= 8
        and durable_query_ledger
        and linked_decision_total == decision_total
    )
    ledger_new_total = sum(
        int(item.get("committed_new_count", 0)) for item in query_rows
    ) if durable_query_ledger else 0
    ledger_new_complete = (
        durable_query_ledger and ledger_new_total == global_committed
    )
    new_counts_available = linked_decisions_complete or ledger_new_complete
    decision_counts_available = linked_decisions_complete
    result_availability = []
    status_availability = []
    for item in query_rows:
        index = item["query_index"]
        text = item["query_text"]
        counts = by_index.get(index, {})
        results = result_counts.get(index, {})
        attempted = (
            schema_version >= 8
            and durable_query_ledger
            and int(item.get("attempt_count", 0)) > 0
        )
        instrumentation_complete = (
            schema_version >= 8
            and durable_query_ledger
            and linked_decisions_complete
        )
        results_measured = instrumentation_complete and (
            attempted or index in result_counts
        )
        status_measured = instrumentation_complete and attempted
        unavailable_state = (
            NOT_MEASURED if instrumentation_complete else UNAVAILABLE_HISTORICAL
        )
        result_availability.append(
            AVAILABLE if results_measured else unavailable_state
        )
        status_availability.append(
            AVAILABLE if status_measured else unavailable_state
        )
        if linked_decisions_complete:
            committed_new = counts.get("NEW", 0)
        elif ledger_new_complete:
            committed_new = int(item.get("committed_new_count", 0))
        else:
            committed_new = None
        per_query_limit = run["max_new_per_query"] if schema_version >= 7 else None
        remaining = (
            max(0, per_query_limit - committed_new)
            if per_query_limit is not None and committed_new is not None else None
        )
        status = item.get("query_status") if status_measured else None
        if status == "RUNNING" and run["status"] in {"FAILED", "INTERRUPTED"}:
            status = "INTERRUPTED"
        result_counter_availability = (
            AVAILABLE if results_measured else unavailable_state
        )
        decision_counter_availability = (
            AVAILABLE if decision_counts_available else UNAVAILABLE_HISTORICAL
        )
        new_counter_availability = (
            AVAILABLE if new_counts_available else UNAVAILABLE_HISTORICAL
        )
        status_counter_availability = (
            AVAILABLE if status_measured else unavailable_state
        )
        geo = extract_query_geography(text)
        query_report = {
            "query_index": index,
            "query_text": text,
            "target_country": geo["country"] or None,
            "target_city": geo["city"] or None,
            "target_location": geo["location"] or None,
            "results_discovered": results.get("discovered", 0) if results_measured else None,
            "detail_pages_inspected": results.get("inspected", 0) if results_measured else None,
            "new_companies_committed": committed_new,
            "known_records": counts.get("KNOWN", 0) if decision_counts_available else None,
            "duplicate_records": results.get("duplicates", 0) if results_measured else None,
            "review_only_records": counts.get("REVIEW", 0) if decision_counts_available else None,
            "rejected_records": results.get("rejected", 0) if results_measured else None,
            "updated_records": counts.get("UPDATED", 0) if decision_counts_available else None,
            "per_query_budget_limit": per_query_limit,
            "per_query_budget_remaining": remaining,
            "budget_exhausted": remaining == 0 if remaining is not None else None,
            "status": status,
            "attempt_count": item.get("attempt_count") if status_measured else None,
            "counter_availability": {
                "results_discovered": result_counter_availability,
                "detail_pages_inspected": result_counter_availability,
                "new_companies_committed": new_counter_availability,
                "known_records": decision_counter_availability,
                "duplicate_records": result_counter_availability,
                "review_only_records": decision_counter_availability,
                "rejected_records": result_counter_availability,
                "updated_records": decision_counter_availability,
                "status": status_counter_availability,
                "attempt_count": status_counter_availability,
            },
            "durability": {
                "decisions": (
                    "durable_query_attribution"
                    if decision_counts_available else UNAVAILABLE_HISTORICAL
                ),
                "budget": (
                    "durable" if new_counts_available else UNAVAILABLE_HISTORICAL
                ),
                "result_counters": (
                    "durable_unique_results"
                    if results_measured else unavailable_state
                ),
                "status": "durable" if status_measured else unavailable_state,
            },
        }
        report_queries.append(query_report)
        for key in totals:
            value = query_report[key]
            if value is not None:
                totals[key] += value
                totals_available[key] = True

    for key, available in totals_available.items():
        if not available:
            totals[key] = None

    historical_limitations = []
    if not durable_query_ledger:
        historical_limitations.append(
            "The run has no durable ordered query ledger; supplied query text does not prove per-query attribution."
        )
    if not new_counts_available:
        historical_limitations.append(
            "Per-query NEW counts were not durably attributed and are not reconstructed from global totals or company records."
        )
    if not decision_counts_available:
        historical_limitations.append(
            "Per-query KNOWN, REVIEW, and UPDATED counts lack complete durable decision links."
        )
    if UNAVAILABLE_HISTORICAL in result_availability:
        historical_limitations.append(
            "Historical queries lack recorded v8 attempts, so result counters are unavailable rather than zero."
        )

    global_limit = run["new_company_limit"]
    report = {
        "format": REPORT_FORMAT,
        "registry_schema_version": schema_version,
        "run_id": run_id,
        "run_status": run["status"],
        "query_identity_source": identity_source,
        "counter_availability": {
            "global_authoritative_totals": AVAILABLE,
            "per_query_new_companies_committed": (
                AVAILABLE if new_counts_available else UNAVAILABLE_HISTORICAL
            ),
            "per_query_decision_classifications": (
                AVAILABLE if decision_counts_available else UNAVAILABLE_HISTORICAL
            ),
            "per_query_result_counters": _availability_summary(result_availability),
            "per_query_status": _availability_summary(status_availability),
        },
        "historical_data_limitations": historical_limitations,
        "semantics": {
            "null": "counter lacks complete historical evidence or has not been measured; it is not reconstructed",
            "results_discovered": "unique stable result identities observed for the query",
            "detail_pages_inspected": "unique results whose detail inspection started",
            "duplicate_records": "unique results resolved as same-run duplicates",
            "decision_counts": "unique authoritative run decisions; retries do not add decisions",
            "report_generation": "read-only SQLite projection",
        },
        "global_authoritative_totals": {
            "new_companies_committed": global_counts["NEW"],
            "known_records": global_counts["KNOWN"],
            "review_only_records": global_counts["REVIEW"],
            "updated_records": global_counts["UPDATED"],
            "counter_availability": AVAILABLE,
        },
        "global_budget": {
            "limit": global_limit,
            "committed_new": global_committed,
            "remaining": max(0, global_limit - global_committed) if global_limit is not None else None,
            "exhausted": global_committed >= global_limit if global_limit is not None else None,
        },
        "queries": report_queries,
        "totals": totals,
    }
    json_payload = (canonical_json(report) + "\n").encode("utf-8")
    lines = [
        f"Per-query observability: {run_id}",
        f"Run status: {run['status']}",
        f"Query identity: {identity_source}",
        f"Counter availability: {canonical_json(report['counter_availability'])}",
        "Historical data limitations:",
        *(
            [f"- {limitation}" for limitation in historical_limitations]
            or ["- None"]
        ),
        "",
        "IDX | STATUS | LOCATION | FOUND | INSPECTED | NEW | KNOWN | DUP | REVIEW | REJECT | REMAINING | QUERY",
    ]
    def counter_display(item: dict, field: str) -> str:
        value = item[field]
        if value is not None:
            return str(value)
        availability = item["counter_availability"].get(field)
        return HISTORICAL_NA if availability == UNAVAILABLE_HISTORICAL else NOT_MEASURED_NA

    optional_display = lambda value: "N/A" if value is None else str(value)
    for item in report_queries:
        lines.append(
            " | ".join([
                str(item["query_index"]), counter_display(item, "status"),
                optional_display(item["target_location"]),
                counter_display(item, "results_discovered"),
                counter_display(item, "detail_pages_inspected"),
                counter_display(item, "new_companies_committed"),
                counter_display(item, "known_records"),
                counter_display(item, "duplicate_records"),
                counter_display(item, "review_only_records"),
                counter_display(item, "rejected_records"),
                optional_display(item["per_query_budget_remaining"]), item["query_text"],
            ])
        )
    text_payload = ("\n".join(lines) + "\n").encode("utf-8")

    destination = Path(output_directory) / "query-observability-v1" / _safe_token(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "query_observability.json"
    text_path = destination / "query_observability.txt"
    reused = json_path.is_file() and text_path.is_file() and (
        json_path.read_bytes() == json_payload and text_path.read_bytes() == text_payload
    )
    if not reused:
        for path, payload, prefix in (
            (json_path, json_payload, ".query-observability-json."),
            (text_path, text_payload, ".query-observability-text."),
        ):
            temporary = None
            try:
                with NamedTemporaryFile("wb", dir=destination, prefix=prefix, delete=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary = Path(handle.name)
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    return {
        **report,
        "json": json_path,
        "text": text_path,
        "json_sha256": sha256(json_payload).hexdigest(),
        "text_sha256": sha256(text_payload).hexdigest(),
        "reused": reused,
    }


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--query-file", type=Path,
        help="Original ordered query file for a historical run without a durable ledger",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    queries = None
    if arguments.query_file is not None:
        queries = [
            line.strip() for line in arguments.query_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    result = build_query_observability_report(
        arguments.database, arguments.run_id, arguments.output_dir, queries=queries,
    )
    print(canonical_json({
        "json": str(result["json"]),
        "text": str(result["text"]),
        "json_sha256": result["json_sha256"],
        "text_sha256": result["text_sha256"],
        "reused": result["reused"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
