"""Transactional writer leases for resumable schema-v6 Maps runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_registry.service import RunBudgetSnapshot, get_run_budget_snapshot
from company_registry.storage import open_registry


class RunLeaseError(RuntimeError):
    """A run lease cannot be safely acquired, renewed, or released."""


class ActiveRunLeaseError(RunLeaseError):
    """Another non-expired writer owns the run."""


class RunResumeError(RunLeaseError):
    """Stored run state or configuration is ineligible for resume."""


class FinalizationRecoveryError(RunLeaseError):
    """Stored run state or configuration is ineligible for export recovery."""


@dataclass(frozen=True)
class RecoveryCheckpoint:
    """Last durable decision and budget state observed when resuming."""

    decision_count: int
    last_decision_id: str | None
    last_decision_created_at: str | None
    budget: RunBudgetSnapshot


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("lease timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _active(row, now_text: str) -> bool:
    return bool(
        row["lease_owner"]
        and row["lease_expires_at"]
        and row["lease_expires_at"] > now_text
    )


def acquire_resume_lease(
    database: str | Path,
    run_id: str,
    owner: str,
    *,
    expected_config_hash: str,
    expected_new_company_limit: int | None,
    lease_seconds: int,
    now: datetime | None = None,
) -> RecoveryCheckpoint:
    """Validate an explicit resume and atomically acquire/take over its lease."""
    if not owner.strip():
        raise ValueError("lease owner must not be empty")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be >= 1")
    timestamp = _utc_now(now)
    now_text = _iso(timestamp)
    expires_text = _iso(timestamp + timedelta(seconds=lease_seconds))
    connection = open_registry(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            raise RunResumeError(f"Unknown discovery run: {run_id}")
        if row["run_config_hash"] is None:
            raise RunResumeError(
                f"Run {run_id!r} has no durable configuration and cannot resume"
            )
        if row["run_config_hash"] != expected_config_hash:
            raise RunResumeError(f"Run {run_id!r} configuration hash mismatch")
        if row["new_company_limit"] != expected_new_company_limit:
            raise RunResumeError(f"Run {run_id!r} NEW-company limit mismatch")
        if row["status"] not in {"RUNNING", "INTERRUPTED"}:
            raise RunResumeError(
                f"Run {run_id!r} in {row['status']} state cannot resume"
            )
        if _active(row, now_text):
            raise ActiveRunLeaseError(
                f"Run {run_id!r} has an active writer lease"
            )
        connection.execute(
            """UPDATE discovery_runs
                  SET status='RUNNING', finished_at=NULL, lease_owner=?,
                      lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE run_id=? AND status IN ('RUNNING', 'INTERRUPTED')""",
            (owner, expires_text, now_text, now_text, run_id),
        )
        checkpoint_row = connection.execute(
            """SELECT decision_id, created_at
                 FROM discovery_run_decisions
                WHERE run_id=?
                ORDER BY created_at DESC, decision_id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        decision_count = connection.execute(
            "SELECT count(*) FROM discovery_run_decisions WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return RecoveryCheckpoint(
        decision_count=decision_count,
        last_decision_id=(checkpoint_row["decision_id"] if checkpoint_row else None),
        last_decision_created_at=(
            checkpoint_row["created_at"] if checkpoint_row else None
        ),
        budget=get_run_budget_snapshot(database, run_id),
    )


def acquire_finalization_lease(
    database: str | Path,
    run_id: str,
    owner: str,
    *,
    expected_config_hash: str,
    expected_new_company_limit: int | None,
    lease_seconds: int,
    now: datetime | None = None,
) -> None:
    """Atomically fence one explicit recovery writer for a FINALIZING run."""
    if not owner.strip():
        raise ValueError("lease owner must not be empty")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be >= 1")
    timestamp = _utc_now(now)
    now_text = _iso(timestamp)
    expires_text = _iso(timestamp + timedelta(seconds=lease_seconds))
    connection = open_registry(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            raise FinalizationRecoveryError(f"Unknown discovery run: {run_id}")
        if row["status"] != "FINALIZING":
            raise FinalizationRecoveryError(
                f"Run {run_id!r} in {row['status']} state cannot recover finalization"
            )
        if row["run_config_hash"] is None:
            raise FinalizationRecoveryError(
                f"Run {run_id!r} has no durable configuration"
            )
        if row["run_config_hash"] != expected_config_hash:
            raise FinalizationRecoveryError(
                f"Run {run_id!r} configuration hash mismatch"
            )
        if row["new_company_limit"] != expected_new_company_limit:
            raise FinalizationRecoveryError(
                f"Run {run_id!r} NEW-company limit mismatch"
            )
        if _active(row, now_text):
            raise ActiveRunLeaseError(
                f"Run {run_id!r} has an active finalization writer lease"
            )
        cursor = connection.execute(
            """UPDATE discovery_runs
                  SET lease_owner=?, lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE run_id=? AND status='FINALIZING'""",
            (owner, expires_text, now_text, now_text, run_id),
        )
        if cursor.rowcount != 1:
            raise FinalizationRecoveryError(
                f"Run {run_id!r} changed state during finalization recovery"
            )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def heartbeat_run_lease(
    database: str | Path,
    run_id: str,
    owner: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> str:
    """Renew an unexpired owned lease and return its new expiration."""
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be >= 1")
    timestamp = _utc_now(now)
    now_text = _iso(timestamp)
    expires_text = _iso(timestamp + timedelta(seconds=lease_seconds))
    connection = open_registry(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE discovery_runs
                  SET lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE run_id=? AND status IN ('RUNNING', 'FINALIZING')
                  AND lease_owner=?
                  AND lease_expires_at>?""",
            (expires_text, now_text, now_text, run_id, owner, now_text),
        )
        if cursor.rowcount != 1:
            raise RunLeaseError(
                f"Run {run_id!r} lease is missing, expired, or owned elsewhere"
            )
        connection.commit()
        return expires_text
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def release_run_lease(
    database: str | Path,
    run_id: str,
    owner: str,
) -> None:
    """Release only the caller's lease; never clear another writer's lease."""
    connection = open_registry(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE discovery_runs
                  SET lease_owner=NULL, lease_expires_at=NULL,
                      heartbeat_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND lease_owner=?""",
            (run_id, owner),
        )
        if cursor.rowcount != 1:
            raise RunLeaseError(
                f"Run {run_id!r} lease is not owned by {owner!r}"
            )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
