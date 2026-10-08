"""Deterministic application preparation and manual state tracking.

This module never opens or submits an application. It consumes persisted pipeline
evidence and records only application-management state and append-only events.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from job_search.normalization import normalize_job_url
from job_search.providers import extract_source_job_id
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


APPLICATION_TRACKING_POLICY_VERSION = "application-tracking-v1"
APPLICATION_STATES = (
    "READY_TO_APPLY", "APPLYING", "APPLIED", "FAILED", "SKIPPED",
    "WITHDRAWN", "CLOSED_BEFORE_APPLY",
)
DUPLICATE_STATUSES = (
    "NO_DUPLICATE", "ALREADY_APPLIED", "POSSIBLE_DUPLICATE", "IDENTITY_CONFLICT",
)
ELIGIBLE_CONTACT_OUTCOMES = {"APPLY_ONLY", "EMAIL_DISCOVERY_READY"}
ALLOWED_TRANSITIONS = {
    "READY_TO_APPLY": {"APPLYING", "APPLIED", "SKIPPED", "CLOSED_BEFORE_APPLY"},
    "APPLYING": {"APPLIED", "FAILED"},
    "FAILED": {"READY_TO_APPLY"},
    "APPLIED": {"WITHDRAWN"},
    "SKIPPED": set(),
    "WITHDRAWN": set(),
    "CLOSED_BEFORE_APPLY": set(),
}
MANUAL_REASON_CODES = {
    "APPLYING": "APPLICATION_MARKED_APPLYING",
    "APPLIED": "APPLICATION_MARKED_APPLIED",
    "FAILED": "APPLICATION_MARKED_FAILED",
    "SKIPPED": "APPLICATION_MARKED_SKIPPED",
    "WITHDRAWN": "APPLICATION_MARKED_WITHDRAWN",
}


@dataclass(frozen=True)
class ApplicationAction:
    application_action_id: int | None
    job_id: int
    title: str
    company: str
    application_identity: str
    state: str
    duplicate_status: str
    application_channel: str
    application_url: str
    canonical_application_url: str
    provider: str
    provider_job_id: str
    contact_outcome: str
    qualification_status: str
    reason_codes: tuple[str, ...]
    input_fingerprint: str
    ready_at: str
    applied_at: str
    withdrawn_at: str
    created_at: str
    updated_at: str
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _select_prepare_rows(
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
    clauses.extend((
        "q.qualification_status='QUALIFIED'",
        "o.outcome IN ('APPLY_ONLY','EMAIL_DISCOVERY_READY')",
    ))
    return connection.execute(
        """SELECT j.job_id,j.title,j.canonical_url AS canonical_job_url,j.content_hash,
                  c.canonical_name AS company,
                  q.qualification_id,q.policy_version AS qualification_policy_version,
                  q.qualification_status,q.input_evidence_hash,
                  o.contact_outcome_id,o.policy_version AS contact_outcome_policy_version,
                  o.outcome AS contact_outcome,o.input_fingerprint AS contact_outcome_fingerprint,
                  d.application_destination_id,d.policy_version AS destination_policy_version,
                  d.application_status,d.application_channel,d.application_url,
                  d.provider,d.input_fingerprint AS destination_fingerprint,
                  a.activity_evidence_id,a.policy_version AS activity_policy_version,
                  a.activity_status,a.input_fingerprint AS activity_fingerprint
           FROM jobs j
           JOIN job_qualifications q ON q.qualification_id=(
               SELECT q2.qualification_id FROM job_qualifications q2
               WHERE q2.job_id=j.job_id ORDER BY q2.qualification_id DESC LIMIT 1)
           JOIN job_contact_outcomes o ON o.contact_outcome_id=(
               SELECT o2.contact_outcome_id FROM job_contact_outcomes o2
               WHERE o2.job_id=j.job_id ORDER BY o2.contact_outcome_id DESC LIMIT 1)
           LEFT JOIN job_application_destinations d ON d.application_destination_id=(
               SELECT d2.application_destination_id FROM job_application_destinations d2
               WHERE d2.job_id=j.job_id ORDER BY d2.application_destination_id DESC LIMIT 1)
           LEFT JOIN job_activity_evidence a ON a.activity_evidence_id=(
               SELECT a2.activity_evidence_id FROM job_activity_evidence a2
               WHERE a2.job_id=j.job_id ORDER BY a2.activity_evidence_id DESC LIMIT 1)
           LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id",
        parameters,
    ).fetchall()


def _identity(row: Mapping) -> tuple[str, str, str]:
    canonical_url = normalize_job_url(_value(row, "application_url"))
    provider = _value(row, "provider").casefold() or "generic"
    provider_job_id = extract_source_job_id(provider, canonical_url) if canonical_url else ""
    if provider_job_id:
        application_identity = f"provider:{provider}:{provider_job_id}"
    else:
        application_identity = f"url:{canonical_url}"
    return application_identity, canonical_url, provider_job_id or ""


def _fingerprint(row: Mapping, identity: str, canonical_url: str, provider_job_id: str) -> str:
    payload = {
        "input": dict(row), "application_identity": identity,
        "canonical_application_url": canonical_url,
        "provider_job_id": provider_job_id,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _duplicate_status(
    connection: sqlite3.Connection, job_id: int, canonical_url: str,
    provider: str, provider_job_id: str,
) -> tuple[str, tuple[int, ...]]:
    clauses, parameters = ["canonical_application_url=?"], [canonical_url]
    if provider_job_id:
        clauses.append("(provider=? AND provider_job_id=?)")
        parameters.extend((provider, provider_job_id))
    rows = connection.execute(
        """SELECT application_action_id,state,canonical_application_url,provider,provider_job_id
           FROM job_application_actions
           WHERE policy_version=? AND job_id<>?
             AND NOT (state='SKIPPED' AND duplicate_status<>'NO_DUPLICATE') AND ("""
        + " OR ".join(clauses) + ") ORDER BY application_action_id",
        (APPLICATION_TRACKING_POLICY_VERSION, job_id, *parameters),
    ).fetchall()
    if not rows:
        return "NO_DUPLICATE", ()
    conflict = any(
        row["canonical_application_url"] == canonical_url
        and provider_job_id and row["provider"] == provider
        and row["provider_job_id"] and row["provider_job_id"] != provider_job_id
        for row in rows
    )
    ids = tuple(row["application_action_id"] for row in rows)
    if conflict:
        return "IDENTITY_CONFLICT", ids
    if any(row["state"] in {"APPLIED", "WITHDRAWN"} for row in rows):
        return "ALREADY_APPLIED", ids
    return "POSSIBLE_DUPLICATE", ids


def _reason_codes(state: str, duplicate_status: str) -> tuple[str, ...]:
    reasons = [
        "APPLICATION_QUALIFIED_JOB", "APPLICATION_CONTACT_OUTCOME_ELIGIBLE",
        "APPLICATION_DESTINATION_CONFIRMED", "APPLICATION_IDENTITY_CONFIRMED",
    ]
    if state == "CLOSED_BEFORE_APPLY":
        reasons.append("APPLICATION_POSTING_INACTIVE")
    elif duplicate_status == "NO_DUPLICATE":
        reasons.append("APPLICATION_NO_DUPLICATE")
    else:
        reasons.extend((f"APPLICATION_{duplicate_status}", "APPLICATION_DUPLICATE_BLOCKED"))
    return tuple(reasons)


def _event(
    connection: sqlite3.Connection, action_id: int, job_id: int,
    event_type: str, old_state: str | None, new_state: str,
    reason: str, metadata: Mapping | None, timestamp: str,
) -> None:
    connection.execute(
        """INSERT INTO job_application_events
           (application_action_id,job_id,event_type,old_state,new_state,reason,
            metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (action_id, job_id, event_type, old_state, new_state, reason,
         json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")), timestamp),
    )


def _stored_action(
    row: sqlite3.Row, *, title: str = "", company: str = "", reused: bool = False,
) -> ApplicationAction:
    return ApplicationAction(
        row["application_action_id"], row["job_id"], title, company,
        row["application_identity"], row["state"], row["duplicate_status"],
        row["application_channel"], row["application_url"],
        row["canonical_application_url"], row["provider"], row["provider_job_id"] or "",
        row["contact_outcome"], row["qualification_status"],
        tuple(json.loads(row["reason_codes_json"])), row["input_fingerprint"],
        row["ready_at"] or "", row["applied_at"] or "", row["withdrawn_at"] or "",
        row["created_at"], row["updated_at"], reused,
    )


def _load_action(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM job_application_actions
           WHERE job_id=? AND policy_version=?""",
        (job_id, APPLICATION_TRACKING_POLICY_VERSION),
    ).fetchone()


def _job_label(connection: sqlite3.Connection, job_id: int) -> tuple[str, str]:
    row = connection.execute(
        """SELECT j.title,c.canonical_name AS company FROM jobs j
           LEFT JOIN companies c ON c.company_id=j.company_id WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()
    return ((row["title"] or ""), (row["company"] or "")) if row else ("", "")


def prepare_application_actions(
    connection: sqlite3.Connection,
    job_ids: Sequence[int] | None = None,
    run_id: str | None = None,
    verbose: bool = False,
) -> list[ApplicationAction]:
    """Prepare bounded jobs using persisted evidence and zero network operations."""
    results = []
    for row in _select_prepare_rows(connection, job_ids, run_id):
        identity, canonical_url, provider_job_id = _identity(row)
        destination_confirmed = (
            _value(row, "application_status").upper() == "CONFIRMED"
            and bool(canonical_url)
        )
        if not destination_confirmed:
            if verbose:
                print(f"{row['job_id']}: not prepared (APPLICATION_DESTINATION_NOT_CONFIRMED)")
            continue
        provider = _value(row, "provider").casefold() or "generic"
        fingerprint = _fingerprint(row, identity, canonical_url, provider_job_id)
        duplicate_status, duplicate_action_ids = _duplicate_status(
            connection, row["job_id"], canonical_url, provider, provider_job_id,
        )
        existing = _load_action(connection, row["job_id"])
        activity_inactive = _value(row, "activity_status").upper() == "INACTIVE"
        timestamp = utc_now()
        if existing is None:
            if activity_inactive:
                state = "CLOSED_BEFORE_APPLY"
            elif duplicate_status != "NO_DUPLICATE":
                state = "SKIPPED"
            else:
                state = "READY_TO_APPLY"
            ready_at = timestamp if state == "READY_TO_APPLY" else None
            reasons = _reason_codes(state, duplicate_status)
            with connection:
                cursor = connection.execute(
                    """INSERT INTO job_application_actions
                       (job_id,policy_version,application_identity,state,duplicate_status,
                        application_channel,application_url,canonical_application_url,
                        provider,provider_job_id,contact_outcome,qualification_status,
                        reason_codes_json,input_fingerprint,ready_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["job_id"], APPLICATION_TRACKING_POLICY_VERSION, identity, state,
                     duplicate_status, row["application_channel"], row["application_url"],
                     canonical_url, provider, provider_job_id or None, row["contact_outcome"],
                     row["qualification_status"], json.dumps(reasons, separators=(",", ":")),
                     fingerprint, ready_at, timestamp, timestamp),
                )
                event_type = "DUPLICATE_BLOCKED" if duplicate_status != "NO_DUPLICATE" else "PREPARED"
                event_reason = (
                    "APPLICATION_DUPLICATE_BLOCKED" if duplicate_status != "NO_DUPLICATE"
                    else "APPLICATION_POSTING_INACTIVE" if state == "CLOSED_BEFORE_APPLY"
                    else "APPLICATION_PREPARED"
                )
                _event(
                    connection, cursor.lastrowid, row["job_id"], event_type, None, state,
                    event_reason,
                    {"duplicate_action_ids": duplicate_action_ids}, timestamp,
                )
        else:
            old_state = existing["state"]
            state = old_state
            if old_state in {"READY_TO_APPLY", "FAILED"}:
                if activity_inactive:
                    state = "CLOSED_BEFORE_APPLY"
                elif duplicate_status != "NO_DUPLICATE":
                    state = "SKIPPED"
                elif old_state == "FAILED":
                    state = "READY_TO_APPLY"
            reasons = _reason_codes(state, duplicate_status)
            unchanged = (
                existing["input_fingerprint"] == fingerprint
                and old_state == state
                and existing["duplicate_status"] == duplicate_status
            )
            if unchanged:
                action = _stored_action(
                    existing, title=row["title"] or "", company=row["company"] or "", reused=True,
                )
                results.append(action)
                if verbose:
                    print(f"{action.job_id}: {action.state} (reused)")
                continue
            ready_at = existing["ready_at"]
            if state == "READY_TO_APPLY" and not ready_at:
                ready_at = timestamp
            with connection:
                connection.execute(
                    """UPDATE job_application_actions SET
                         application_identity=?,state=?,duplicate_status=?,application_channel=?,
                         application_url=?,canonical_application_url=?,provider=?,provider_job_id=?,
                         contact_outcome=?,qualification_status=?,reason_codes_json=?,
                         input_fingerprint=?,ready_at=?,updated_at=?
                       WHERE application_action_id=?""",
                    (identity, state, duplicate_status, row["application_channel"],
                     row["application_url"], canonical_url, provider, provider_job_id or None,
                     row["contact_outcome"], row["qualification_status"],
                     json.dumps(reasons, separators=(",", ":")), fingerprint, ready_at,
                     timestamp, existing["application_action_id"]),
                )
                if state != old_state:
                    event_reason = (
                        "APPLICATION_POSTING_INACTIVE" if state == "CLOSED_BEFORE_APPLY"
                        else "APPLICATION_DUPLICATE_BLOCKED" if state == "SKIPPED"
                        else "APPLICATION_RETRY_PREPARED"
                    )
                    _event(
                        connection, existing["application_action_id"], row["job_id"],
                        "PREPARED_STATE_CHANGE", old_state, state, event_reason,
                        {"duplicate_action_ids": duplicate_action_ids}, timestamp,
                    )
        stored = _load_action(connection, row["job_id"])
        action = _stored_action(
            stored, title=row["title"] or "", company=row["company"] or "",
        )
        results.append(action)
        if verbose:
            print(f"{action.job_id}: {action.state} ({action.duplicate_status})")
    return results


def transition_application_action(
    connection: sqlite3.Connection, job_id: int, new_state: str, reason: str = "",
) -> ApplicationAction:
    """Record an explicit manual transition; this never performs the application."""
    new_state = new_state.upper()
    if new_state not in MANUAL_REASON_CODES:
        raise ValueError(f"unsupported manual state: {new_state}")
    existing = _load_action(connection, job_id)
    if existing is None:
        raise ValueError(f"application action not prepared for job {job_id}")
    old_state = existing["state"]
    if new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
        raise ValueError(f"invalid application transition: {old_state} -> {new_state}")
    if new_state in {"APPLYING", "APPLIED"}:
        duplicate_status, duplicate_ids = _duplicate_status(
            connection, job_id, existing["canonical_application_url"],
            existing["provider"], existing["provider_job_id"] or "",
        )
        if duplicate_status != "NO_DUPLICATE":
            raise ValueError(
                f"duplicate application blocked: {duplicate_status} "
                f"(actions {','.join(map(str, duplicate_ids))})"
            )
    timestamp = utc_now()
    reasons = tuple(dict.fromkeys((
        *json.loads(existing["reason_codes_json"]), MANUAL_REASON_CODES[new_state],
    )))
    applied_at = timestamp if new_state == "APPLIED" else existing["applied_at"]
    withdrawn_at = timestamp if new_state == "WITHDRAWN" else existing["withdrawn_at"]
    with connection:
        connection.execute(
            """UPDATE job_application_actions
               SET state=?,reason_codes_json=?,applied_at=?,withdrawn_at=?,updated_at=?
               WHERE application_action_id=?""",
            (new_state, json.dumps(reasons, separators=(",", ":")), applied_at,
             withdrawn_at, timestamp, existing["application_action_id"]),
        )
        _event(
            connection, existing["application_action_id"], job_id, "MANUAL_TRANSITION",
            old_state, new_state, reason.strip(), {"command": MANUAL_REASON_CODES[new_state]},
            timestamp,
        )
    title, company = _job_label(connection, job_id)
    return _stored_action(_load_action(connection, job_id), title=title, company=company)


def print_results(results: Sequence[ApplicationAction]) -> None:
    counts = Counter(action.state for action in results)
    print(f"Jobs selected: {len(results)}")
    for state in APPLICATION_STATES:
        print(f"{state}: {counts[state]}")
    print(f"Duplicate blocked: {sum(a.duplicate_status != 'NO_DUPLICATE' for a in results)}")
    print(f"Manual review required: {sum(a.duplicate_status in {'POSSIBLE_DUPLICATE', 'IDENTITY_CONFLICT'} for a in results)}")
    for action in results:
        print(f"\njob_id: {action.job_id}")
        print(f"title: {action.title or '-'}")
        print(f"company: {action.company or '-'}")
        print(f"application_state: {action.state}")
        print(f"duplicate_status: {action.duplicate_status}")
        print(f"application_channel: {action.application_channel}")
        print(f"application_url: {action.application_url}")
        print(f"contact_outcome: {action.contact_outcome}")
        print(f"qualification: {action.qualification_status}")
        print(f"ready_at: {action.ready_at or '-'}")
        print(f"applied_at: {action.applied_at or '-'}")
        print("reason_codes: " + ", ".join(action.reason_codes))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic application action tracking")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--mark-applying", action="store_true")
    actions.add_argument("--mark-applied", action="store_true")
    actions.add_argument("--mark-failed", action="store_true")
    actions.add_argument("--mark-skipped", action="store_true")
    actions.add_argument("--mark-withdrawn", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    manual = next((
        state for flag, state in (
            (args.mark_applying, "APPLYING"), (args.mark_applied, "APPLIED"),
            (args.mark_failed, "FAILED"), (args.mark_skipped, "SKIPPED"),
            (args.mark_withdrawn, "WITHDRAWN"),
        ) if flag
    ), None)
    if manual and args.run_id:
        parser.error("manual transitions support --job-id only")
    if manual and not args.job_id:
        parser.error("manual transitions require --job-id")
    if args.reason and manual not in {"FAILED", "SKIPPED"}:
        parser.error("--reason is supported only with --mark-failed or --mark-skipped")
    connection = connect_database(args.database)
    try:
        try:
            if args.prepare:
                results = prepare_application_actions(
                    connection, args.job_id, args.run_id, args.verbose,
                )
            else:
                results = [
                    transition_application_action(connection, job_id, manual, args.reason)
                    for job_id in args.job_id
                ]
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
