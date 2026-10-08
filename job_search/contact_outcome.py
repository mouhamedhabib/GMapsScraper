"""Deterministically choose the next action after contact discovery."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


CONTACT_OUTCOME_POLICY_VERSION = "contact-outcome-v1"
CONTACT_OUTCOMES = ("EMAIL_DISCOVERY_READY", "APPLY_ONLY", "CONTACT_REVIEW")


@dataclass(frozen=True)
class ContactOutcome:
    job_id: int
    outcome: str
    primary_contact_candidate_id: int | None
    backup_contact_candidate_id: int | None
    application_status: str
    application_channel: str
    reason_codes: tuple[str, ...]
    input_fingerprint: str = ""
    created_at: str = ""
    updated_at: str = ""
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _identifier(row: Mapping, name: str) -> int | None:
    try:
        value = row[name]
    except (KeyError, IndexError):
        return None
    return int(value) if value is not None else None


def evaluate_contact_outcome(row: Mapping) -> ContactOutcome:
    """Evaluate one already-qualified row without changing upstream evidence."""
    job_id = int(row["job_id"])
    primary_id = _identifier(row, "primary_contact_candidate_id")
    backup_id = _identifier(row, "backup_contact_candidate_id")
    application_status = _value(row, "application_status").upper() or "UNKNOWN"
    application_channel = _value(row, "application_channel").upper() or "UNKNOWN"
    discovery_status = _value(row, "discovery_status").upper()
    reasons = ["OUTCOME_QUALIFIED_JOB"]

    if _identifier(row, "contact_strategy_id") is None:
        return ContactOutcome(
            job_id, "CONTACT_REVIEW", primary_id, backup_id,
            application_status, application_channel,
            tuple([*reasons, "OUTCOME_CONTACT_DISCOVERY_INCOMPLETE"]),
        )

    contact_selected = discovery_status == "CONTACTS_SELECTED" and primary_id is not None
    primary_safe = (
        contact_selected
        and _identifier(row, "candidate_id") == primary_id
        and _value(row, "primary_selection_status").upper() == "SELECTED_PRIMARY"
        and _value(row, "primary_confidence").upper() in {"HIGH", "MEDIUM"}
    )
    if primary_safe:
        reasons.extend((
            "OUTCOME_PRIMARY_CONTACT_SELECTED",
            "OUTCOME_CONTACT_CONFIDENCE_ACCEPTABLE",
        ))
        outcome = "EMAIL_DISCOVERY_READY"
    elif discovery_status == "NO_CONFIDENT_CONTACT":
        reasons.append("OUTCOME_NO_CONFIDENT_CONTACT")
        destination_safe = application_status == "CONFIRMED" and bool(
            _value(row, "application_url")
        )
        if destination_safe:
            reasons.append("OUTCOME_APPLICATION_DESTINATION_CONFIRMED")
            outcome = "APPLY_ONLY"
        else:
            reasons.append("OUTCOME_APPLICATION_DESTINATION_UNKNOWN")
            outcome = "CONTACT_REVIEW"
    else:
        reasons.append("OUTCOME_CONTACT_DISCOVERY_INCOMPLETE")
        if contact_selected:
            reasons.append("OUTCOME_CONTACT_EVIDENCE_UNSAFE")
        outcome = "CONTACT_REVIEW"

    return ContactOutcome(
        job_id, outcome, primary_id, backup_id, application_status,
        application_channel, tuple(reasons),
    )


def _select_rows(
    connection: sqlite3.Connection,
    job_ids: Sequence[int] | None,
    run_id: str | None,
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
        if not connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,),
        ).fetchone():
            raise ValueError(f"workflow run not found: {run_id}")
        clauses.append(
            "EXISTS (SELECT 1 FROM workflow_run_jobs w "
            "WHERE w.run_id=? AND w.job_id=j.job_id)"
        )
        parameters.append(run_id)
    clauses.append("q.qualification_status='QUALIFIED'")
    return connection.execute(
        """SELECT j.job_id,
                  q.qualification_id,q.policy_version AS qualification_policy_version,
                  q.qualification_status,q.input_evidence_hash,
                  s.contact_strategy_id,s.policy_version AS strategy_policy_version,
                  s.input_fingerprint AS strategy_fingerprint,
                  sc.selected_contact_id,sc.policy_version AS discovery_policy_version,
                  sc.discovery_status,sc.primary_contact_candidate_id,
                  sc.backup_contact_candidate_id,sc.input_fingerprint AS discovery_fingerprint,
                  pc.contact_candidate_id AS candidate_id,
                  pc.confidence AS primary_confidence,
                  pc.selection_status AS primary_selection_status,
                  pc.input_fingerprint AS primary_fingerprint,
                  bc.contact_candidate_id AS backup_candidate_id,
                  bc.confidence AS backup_confidence,
                  bc.selection_status AS backup_selection_status,
                  bc.input_fingerprint AS backup_fingerprint,
                  a.application_destination_id,
                  a.policy_version AS application_policy_version,
                  a.application_status,a.application_channel,a.application_url,
                  a.input_fingerprint AS application_fingerprint
           FROM jobs j
           JOIN job_qualifications q ON q.qualification_id=(
               SELECT q2.qualification_id FROM job_qualifications q2
               WHERE q2.job_id=j.job_id ORDER BY q2.qualification_id DESC LIMIT 1)
           LEFT JOIN job_contact_strategies s ON s.contact_strategy_id=(
               SELECT s2.contact_strategy_id FROM job_contact_strategies s2
               WHERE s2.job_id=j.job_id ORDER BY s2.contact_strategy_id DESC LIMIT 1)
           LEFT JOIN job_selected_contacts sc ON sc.selected_contact_id=(
               SELECT sc2.selected_contact_id FROM job_selected_contacts sc2
               WHERE sc2.job_id=j.job_id ORDER BY sc2.selected_contact_id DESC LIMIT 1)
           LEFT JOIN job_contact_candidates pc
             ON pc.contact_candidate_id=sc.primary_contact_candidate_id
            AND pc.job_id=j.job_id AND pc.policy_version=sc.policy_version
           LEFT JOIN job_contact_candidates bc
             ON bc.contact_candidate_id=sc.backup_contact_candidate_id
            AND bc.job_id=j.job_id AND bc.policy_version=sc.policy_version
           LEFT JOIN job_application_destinations a ON a.application_destination_id=(
               SELECT a2.application_destination_id FROM job_application_destinations a2
               WHERE a2.job_id=j.job_id ORDER BY a2.application_destination_id DESC LIMIT 1)
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id",
        parameters,
    ).fetchall()


def _fingerprint(row: Mapping) -> str:
    return sha256(json.dumps(
        dict(row), sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> ContactOutcome:
    return ContactOutcome(
        row["job_id"], row["outcome"], row["primary_contact_candidate_id"],
        row["backup_contact_candidate_id"], row["application_status"],
        row["application_channel"], tuple(json.loads(row["reason_codes_json"])),
        row["input_fingerprint"], row["created_at"], row["updated_at"], True,
    )


def _persist(connection: sqlite3.Connection, result: ContactOutcome) -> None:
    connection.execute(
        """INSERT INTO job_contact_outcomes
           (job_id,policy_version,outcome,primary_contact_candidate_id,
            backup_contact_candidate_id,application_status,application_channel,
            reason_codes_json,input_fingerprint,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             outcome=excluded.outcome,
             primary_contact_candidate_id=excluded.primary_contact_candidate_id,
             backup_contact_candidate_id=excluded.backup_contact_candidate_id,
             application_status=excluded.application_status,
             application_channel=excluded.application_channel,
             reason_codes_json=excluded.reason_codes_json,
             input_fingerprint=excluded.input_fingerprint,
             updated_at=excluded.updated_at""",
        (
            result.job_id, CONTACT_OUTCOME_POLICY_VERSION, result.outcome,
            result.primary_contact_candidate_id, result.backup_contact_candidate_id,
            result.application_status, result.application_channel,
            json.dumps(result.reason_codes, separators=(",", ":")),
            result.input_fingerprint, result.created_at, result.updated_at,
        ),
    )


def resolve_contact_outcomes(
    connection: sqlite3.Connection,
    job_ids: Sequence[int] | None = None,
    run_id: str | None = None,
    verbose: bool = False,
) -> list[ContactOutcome]:
    results = []
    for row in _select_rows(connection, job_ids, run_id):
        fingerprint = _fingerprint(row)
        existing = connection.execute(
            """SELECT * FROM job_contact_outcomes
               WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
            (row["job_id"], CONTACT_OUTCOME_POLICY_VERSION, fingerprint),
        ).fetchone()
        if existing:
            result = _from_stored(existing)
        else:
            timestamp = utc_now()
            result = replace(
                evaluate_contact_outcome(row), input_fingerprint=fingerprint,
                created_at=timestamp, updated_at=timestamp,
            )
            with connection:
                _persist(connection, result)
        results.append(result)
        if verbose:
            print(f"{result.job_id}: {result.outcome}{' (reused)' if result.reused else ''}")
    return results


def print_results(results: Sequence[ContactOutcome]) -> None:
    counts = Counter(result.outcome for result in results)
    print(f"Jobs selected: {len(results)}")
    for outcome in CONTACT_OUTCOMES:
        print(f"{outcome}: {counts[outcome]}")
    for result in results:
        print(f"\njob_id: {result.job_id}")
        print(f"outcome: {result.outcome}")
        print(f"primary_contact_candidate_id: {result.primary_contact_candidate_id or '-'}")
        print(f"backup_contact_candidate_id: {result.backup_contact_candidate_id or '-'}")
        print(f"application_status: {result.application_status}")
        print(f"application_channel: {result.application_channel}")
        print("reason_codes: " + ", ".join(result.reason_codes))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic contact outcome policy")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_contact_outcomes(
                connection, args.job_id, args.run_id, args.verbose,
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
