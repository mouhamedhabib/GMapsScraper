"""Guarded lifecycle transitions for durable discovery runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from company_registry.storage import DEFAULT_DATABASE, open_registry


RUNNING = "RUNNING"
FINALIZING = "FINALIZING"
TERMINAL_STATES = frozenset({"SUCCESS", "PARTIAL", "FAILED", "INTERRUPTED"})
ALLOWED_TRANSITIONS = {
    RUNNING: frozenset({FINALIZING, "FAILED", "INTERRUPTED"}),
    FINALIZING: frozenset({"SUCCESS", "PARTIAL"}),
}


class RunStateTransitionError(RuntimeError):
    """A run does not exist or cannot make the requested state transition."""


def transition_run(
    database: str | Path,
    run_id: str,
    target_status: str,
    *,
    now: str | None = None,
    lease_owner: str | None = None,
) -> None:
    """Atomically apply one documented run-state transition or fail closed."""
    timestamp = now or datetime.now(timezone.utc).isoformat()
    connection = open_registry(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status, lease_owner, lease_expires_at
                 FROM discovery_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunStateTransitionError(f"Unknown discovery run: {run_id}")
        source_status = row["status"]
        if target_status not in ALLOWED_TRANSITIONS.get(source_status, frozenset()):
            raise RunStateTransitionError(
                f"Invalid discovery-run transition: {source_status} -> {target_status}"
            )
        if row["lease_owner"] is not None and lease_owner is None:
            raise RunStateTransitionError(
                f"Discovery run {run_id!r} requires its writer lease token"
            )
        if lease_owner is not None:
            if row["lease_owner"] != lease_owner:
                raise RunStateTransitionError(
                    f"Discovery run {run_id!r} writer lease is owned elsewhere"
                )
            if not row["lease_expires_at"] or row["lease_expires_at"] <= timestamp:
                raise RunStateTransitionError(
                    f"Discovery run {run_id!r} writer lease has expired"
                )
        finished_at = timestamp if target_status in TERMINAL_STATES else None
        cursor = connection.execute(
            """UPDATE discovery_runs
                  SET status=?, finished_at=?, updated_at=?
                WHERE run_id=? AND status=?
                  AND (? IS NULL OR lease_owner=?)""",
            (
                target_status, finished_at, timestamp, run_id, source_status,
                lease_owner, lease_owner,
            ),
        )
        if cursor.rowcount != 1:
            raise RunStateTransitionError(
                f"Discovery run {run_id!r} changed state concurrently"
            )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
