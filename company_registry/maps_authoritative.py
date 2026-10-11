"""Opt-in SQLite-authoritative coordination for Google Maps discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import resource
from threading import Event, Lock, Thread, current_thread, get_native_id
from time import monotonic, process_time
from uuid import uuid4

from company_registry.discovery_adapter import (
    AuthoritativeDiscoveryAdapter,
    DiscoveryDecision,
    DiscoveryMode,
    RegistryUnavailableError,
    parse_discovery_mode,
)
from company_registry.exports import (
    FailureInjector,
    export_new_companies,
    export_qualified_leads,
    recover_run_exports,
    verify_export_manifest,
    verify_run_exports,
)
from company_registry.models import Classification, IdentityObservation, ResolutionAction
from company_registry.normalization import canonical_json, normalize_google_place_id
from company_registry.qualification import DEFAULT_QUALIFICATION_POLICY, assess_run
from company_registry.resolver import RESOLVER_VERSION
from company_registry.run_lifecycle import transition_run
from company_registry.run_lease import (
    RecoveryCheckpoint,
    acquire_finalization_lease,
    acquire_resume_lease,
    heartbeat_run_lease,
    release_run_lease,
)
from company_registry.service import (
    NewCompanyBudgetExhausted,
    RunBudgetSnapshot,
    get_run_budget_snapshot,
)
from company_registry.shadow import DEFAULT_SHADOW_DATABASE
from company_registry.storage import DEFAULT_DATABASE, open_registry


DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY = Path("data/authoritative_maps")
LEASE_DIAGNOSTIC_MAX_BYTES = 1024 * 1024
LEASE_DIAGNOSTIC_BACKUP_COUNT = 3


class _LeaseDiagnosticLog:
    """Write bounded local JSONL evidence for one authoritative Maps session."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._logger = logging.getLogger(f"gmaps.lease.{uuid4()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=LEASE_DIAGNOSTIC_MAX_BYTES,
            backupCount=LEASE_DIAGNOSTIC_BACKUP_COUNT,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)
        os.chmod(self.path, 0o600)

    def write(self, event: str, *, level: int = logging.INFO, **details) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "run_id": self.run_id,
            **details,
        }
        self._logger.log(level, json.dumps(record, sort_keys=True))

    def close(self) -> None:
        self._handler.flush()
        self._handler.close()
        self._logger.removeHandler(self._handler)


def _process_diagnostics() -> dict:
    """Return low-cost process and scheduler evidence for runtime events."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    thread_usage = (
        resource.getrusage(resource.RUSAGE_THREAD)
        if hasattr(resource, "RUSAGE_THREAD") else None
    )
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = None
    return {
        "pid": os.getpid(),
        "native_thread_id": get_native_id(),
        "monotonic_seconds": round(monotonic(), 6),
        "process_cpu_seconds": round(process_time(), 6),
        "process_user_cpu_seconds": round(usage.ru_utime, 6),
        "process_system_cpu_seconds": round(usage.ru_stime, 6),
        "voluntary_context_switches": usage.ru_nvcsw,
        "involuntary_context_switches": usage.ru_nivcsw,
        "thread_user_cpu_seconds": (
            round(thread_usage.ru_utime, 6) if thread_usage else None
        ),
        "thread_system_cpu_seconds": (
            round(thread_usage.ru_stime, 6) if thread_usage else None
        ),
        "thread_voluntary_context_switches": (
            thread_usage.ru_nvcsw if thread_usage else None
        ),
        "thread_involuntary_context_switches": (
            thread_usage.ru_nivcsw if thread_usage else None
        ),
        "max_rss_kib": usage.ru_maxrss,
        "system_load_1m": round(load_1m, 6) if load_1m is not None else None,
        "system_load_5m": round(load_5m, 6) if load_5m is not None else None,
        "system_load_15m": round(load_15m, 6) if load_15m is not None else None,
        "cpu_count": os.cpu_count(),
    }


class UnsafeAuthoritativePathError(ValueError):
    """An authoritative run targeted production or legacy output state."""


class AuthoritativeResumeError(RuntimeError):
    """Phase 3C.1 deliberately rejects reuse of a durable run ID."""


def maps_run_configuration_hash(
    queries: list[str],
    *,
    mode: DiscoveryMode,
    new_company_limit: int | None,
    export_directory: Path,
) -> str:
    """Build the exact durable configuration identity used by Maps runs."""
    configuration = {
        "queries": [query.strip() for query in queries if query.strip()],
        "discovery_mode": mode.value,
        "new_company_limit": new_company_limit,
        "resolver_version": RESOLVER_VERSION,
        "qualification_policy_version": DEFAULT_QUALIFICATION_POLICY.version,
        "export_root": str(export_directory),
    }
    return sha256(canonical_json(configuration).encode("utf-8")).hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_authoritative_paths(
    database: str | Path,
    export_directory: str | Path,
    *,
    mode: str | DiscoveryMode = DiscoveryMode.AUTHORITATIVE_CANARY,
    allow_production_registry: bool = False,
) -> tuple[Path, Path]:
    """Validate authoritative paths, keeping production denied by default."""
    project = Path(__file__).resolve().parents[1]
    database = Path(database).expanduser().resolve()
    export_directory = Path(export_directory).expanduser().resolve()
    production_database = (project / DEFAULT_DATABASE).resolve()
    shadow_database = (project / DEFAULT_SHADOW_DATABASE).resolve()
    parsed_mode = parse_discovery_mode(mode)
    if database == shadow_database:
        raise UnsafeAuthoritativePathError(
            f"Authoritative mode refuses the shadow registry: {database}"
        )
    if database == production_database:
        if not allow_production_registry:
            raise UnsafeAuthoritativePathError(
                "Authoritative mode refuses the production registry by default; "
                "explicit production activation intent is required"
            )
        if parsed_mode is not DiscoveryMode.AUTHORITATIVE:
            raise UnsafeAuthoritativePathError(
                "Production registry opt-in requires discovery mode 'authoritative'"
            )
        try:
            connection = open_registry(database)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchall()
                if [row[0] for row in integrity] != ["ok"]:
                    raise RuntimeError("SQLite integrity_check did not return ok")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("SQLite foreign-key violations are present")
            finally:
                connection.close()
        except Exception as error:
            raise UnsafeAuthoritativePathError(
                "Production registry activation requires an existing, complete, "
                f"integrity-clean schema-v6 database: {type(error).__name__}: {error}"
            ) from error
    elif allow_production_registry:
        raise UnsafeAuthoritativePathError(
            "Production registry opt-in is valid only for the canonical production path"
        )
    forbidden_outputs = (
        (project / "CSV_FILES").resolve(),
        (project / "exports").resolve(),
        (project / "data" / "reports").resolve(),
    )
    if any(_inside(export_directory, protected) for protected in forbidden_outputs):
        raise UnsafeAuthoritativePathError(
            f"Authoritative exports must be isolated from legacy outputs: {export_directory}"
        )
    if export_directory == database or _inside(database, export_directory):
        raise UnsafeAuthoritativePathError(
            "Authoritative export directory cannot contain the SQLite database"
        )
    return database, export_directory


def maps_observation(
    row: dict,
    *,
    query: str,
    observed_at: str,
) -> IdentityObservation:
    """Build a deterministic resolver observation from one Maps result."""
    place_id = normalize_google_place_id(
        row.get("place_id") or row.get("map_place_id") or row.get("map_link")
    )
    identity_seed = {
        "place_id": place_id,
        "name": row.get("title") or row.get("company_name") or row.get("name") or "",
        "website": row.get("webpage") or row.get("website") or "",
        "phone": row.get("phone_number") or row.get("phone") or "",
        "address": row.get("address") or row.get("location") or "",
    }
    source_key = (
        f"place:{place_id}"
        if place_id
        else "identity:" + sha256(canonical_json(identity_seed).encode("utf-8")).hexdigest()
    )
    raw = dict(row)
    raw.setdefault("source_query", query)
    return IdentityObservation(
        source_system="GOOGLE_MAPS",
        source_record_key=source_key,
        observed_at=observed_at,
        name=str(identity_seed["name"] or ""),
        place_id=place_id,
        website_url=str(identity_seed["website"] or ""),
        phone=str(identity_seed["phone"] or ""),
        address=str(identity_seed["address"] or ""),
        raw_payload=raw,
    )


@dataclass(frozen=True)
class AuthoritativeOutcome:
    outcome: str
    decision: DiscoveryDecision


class AuthoritativeMapsSession:
    """Own one fresh or explicitly resumed Maps run under a writer lease."""

    def __init__(
        self,
        database: str | Path,
        export_directory: str | Path = DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY,
        *,
        mode: str | DiscoveryMode = DiscoveryMode.AUTHORITATIVE_CANARY,
        run_id: str | None = None,
        resume_run_id: str | None = None,
        new_company_limit: int | None = None,
        lease_seconds: int = 120,
        heartbeat_interval_seconds: float = 40.0,
        stack_snapshot_interval_seconds: float | None = None,
        lease_owner: str | None = None,
        allow_production_registry: bool = False,
    ) -> None:
        if run_id is not None and resume_run_id is not None:
            raise ValueError("run_id and resume_run_id are mutually exclusive")
        if new_company_limit is not None and new_company_limit < 1:
            raise ValueError("new_company_limit must be >= 1 when provided")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be >= 1")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be > 0")
        if stack_snapshot_interval_seconds is not None:
            raise ValueError(
                "Periodic stack snapshots are disabled because the CPython "
                "faulthandler watchdog is unsafe"
            )
        self.mode = parse_discovery_mode(mode)
        if not self.mode.is_authoritative:
            raise ValueError("AuthoritativeMapsSession requires an authoritative mode")
        self.database, self.export_directory = validate_authoritative_paths(
            database,
            export_directory,
            mode=self.mode,
            allow_production_registry=allow_production_registry,
        )
        self.is_resume = resume_run_id is not None
        self.run_id = resume_run_id or run_id or f"maps-{uuid4()}"
        self.new_company_limit = new_company_limit
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.lease_owner = lease_owner or f"maps-writer-{uuid4()}"
        self.adapter = AuthoritativeDiscoveryAdapter(self.database, mode=self.mode)
        self.export_result: dict | None = None
        self.qualification_result: dict | None = None
        self.qualified_export_results: dict[str, dict] = {}
        self.recovery_checkpoint: RecoveryCheckpoint | None = None
        self._closed = False
        self._heartbeat_stop = Event()
        self._heartbeat_thread: Thread | None = None
        self._heartbeat_error: BaseException | None = None
        self._heartbeat_lock = Lock()
        self._last_successful_renewal_at: str | None = None
        self._last_successful_renewal_monotonic: float | None = None
        self.diagnostic_log_path = self.database.parent / "logs/writer_lease.jsonl"
        self._diagnostic_log: _LeaseDiagnosticLog | None = None
        if self.is_resume:
            # Explicit resume remains read-only until configure_queries() can
            # validate the complete query-dependent configuration.
            connection = open_registry(self.database)
            connection.close()
        else:
            self._create_run()

    def _configuration_hash(self, queries: list[str]) -> str:
        return maps_run_configuration_hash(
            queries,
            mode=self.mode,
            new_company_limit=self.new_company_limit,
            export_directory=self.export_directory,
        )

    def _create_run(self) -> None:
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        expires = (now_value + timedelta(seconds=self.lease_seconds)).isoformat()
        try:
            connection = open_registry(self.database)
            try:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM discovery_runs WHERE run_id=?", (self.run_id,),
                ).fetchone()
                if exists is not None:
                    raise AuthoritativeResumeError(
                        f"Run ID {self.run_id!r} already exists; Phase 3C.1 does not "
                        "support resume. Start with a new run ID."
                    )
                connection.execute(
                    """INSERT INTO discovery_runs
                       (run_id, run_type, status, started_at, created_at,
                        new_company_limit, run_config_hash, lease_owner,
                        lease_expires_at, heartbeat_at, updated_at)
                       VALUES (?, 'SCRAPE', 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.run_id, now, now, self.new_company_limit,
                        self._configuration_hash([]), self.lease_owner,
                        expires, now, now,
                    ),
                )
                connection.commit()
                self._remember_successful_renewal(now)
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        except AuthoritativeResumeError:
            raise
        except Exception as error:
            raise RegistryUnavailableError(
                f"Authoritative registry initialization failed closed: "
                f"{type(error).__name__}: {error}"
            ) from error

    def configure_queries(self, queries: list[str]) -> None:
        """Persist fresh configuration or validate/acquire an explicit resume."""
        config_hash = self._configuration_hash(queries)
        if self.is_resume:
            self.recovery_checkpoint = acquire_resume_lease(
                self.database,
                self.run_id,
                self.lease_owner,
                expected_config_hash=config_hash,
                expected_new_company_limit=self.new_company_limit,
                lease_seconds=self.lease_seconds,
            )
            self._remember_successful_renewal(
                datetime.now(timezone.utc).isoformat()
            )
            return
        now = datetime.now(timezone.utc).isoformat()
        connection = open_registry(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            decision_count = connection.execute(
                "SELECT count(*) FROM discovery_run_decisions WHERE run_id=?",
                (self.run_id,),
            ).fetchone()[0]
            if decision_count:
                raise RuntimeError("Run configuration cannot change after decisions")
            cursor = connection.execute(
                """UPDATE discovery_runs
                      SET run_config_hash=?, updated_at=?
                    WHERE run_id=? AND status='RUNNING' AND lease_owner=?
                      AND lease_expires_at>?""",
                (config_hash, now, self.run_id, self.lease_owner, now),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Only a RUNNING run can be configured")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(self, *, timing_callback=None) -> str:
        """Renew this session's lease or fail if ownership was lost."""
        expiration = heartbeat_run_lease(
            self.database,
            self.run_id,
            self.lease_owner,
            lease_seconds=self.lease_seconds,
            timing_callback=timing_callback,
        )
        self._remember_successful_renewal(datetime.now(timezone.utc).isoformat())
        return expiration

    def _remember_successful_renewal(self, timestamp: str) -> None:
        with self._heartbeat_lock:
            self._last_successful_renewal_at = timestamp
            self._last_successful_renewal_monotonic = monotonic()

    def _ensure_diagnostic_log(self) -> _LeaseDiagnosticLog:
        if self._diagnostic_log is None:
            self._diagnostic_log = _LeaseDiagnosticLog(
                self.diagnostic_log_path, self.run_id,
            )
        return self._diagnostic_log

    def _thread_diagnostics(self) -> dict:
        thread = self._heartbeat_thread
        return {
            "heartbeat_thread_name": thread.name if thread else None,
            "heartbeat_thread_ident": thread.ident if thread else None,
            "heartbeat_thread_alive": bool(thread and thread.is_alive()),
            "heartbeat_stop_requested": self._heartbeat_stop.is_set(),
        }

    def _renewal_diagnostics(self) -> dict:
        now = datetime.now(timezone.utc)
        with self._heartbeat_lock:
            last_at = self._last_successful_renewal_at
            last_monotonic = self._last_successful_renewal_monotonic
            heartbeat_error = self._heartbeat_error
        return {
            "last_successful_renewal_at": last_at,
            "elapsed_since_successful_renewal_seconds": (
                round(
                    (
                        now
                        - datetime.fromisoformat(last_at).astimezone(timezone.utc)
                    ).total_seconds(),
                    3,
                )
                if last_at is not None else None
            ),
            "active_elapsed_since_successful_renewal_seconds": (
                round(monotonic() - last_monotonic, 3)
                if last_monotonic is not None else None
            ),
            "heartbeat_error_type": (
                type(heartbeat_error).__name__ if heartbeat_error else None
            ),
            **self._thread_diagnostics(),
            **_process_diagnostics(),
        }

    def record_runtime_diagnostic(self, event: str, **details) -> None:
        """Append a bounded non-content runtime diagnostic event."""
        self._ensure_diagnostic_log().write(
            event,
            **details,
            **_process_diagnostics(),
        )

    @staticmethod
    def _is_lease_expiration(error: BaseException) -> bool:
        current: BaseException | None = error
        while current is not None:
            if "writer lease has expired" in str(current):
                return True
            current = current.__cause__
        return False

    def _log_lease_expiration(self, error: BaseException, operation: str) -> None:
        if not self._is_lease_expiration(error):
            return
        self._ensure_diagnostic_log().write(
            "lease_expiration_detected",
            level=logging.ERROR,
            operation=operation,
            error_type=type(error).__name__,
            error_message=str(error),
            **self._renewal_diagnostics(),
        )

    def _transition(self, target_status: str) -> None:
        log = self._ensure_diagnostic_log()
        log.write(
            "lifecycle_transition_started",
            target_status=target_status,
            **self._thread_diagnostics(),
        )
        try:
            transition_run(
                self.database,
                self.run_id,
                target_status,
                lease_owner=self.lease_owner,
            )
        except BaseException as error:
            log.write(
                "lifecycle_transition_failed",
                level=logging.ERROR,
                target_status=target_status,
                error_type=type(error).__name__,
                error_message=str(error),
                **self._renewal_diagnostics(),
            )
            self._log_lease_expiration(error, f"transition_to_{target_status}")
            raise
        log.write(
            "lifecycle_transition_succeeded",
            target_status=target_status,
            **self._thread_diagnostics(),
        )

    def _heartbeat_loop(self) -> None:
        log = self._ensure_diagnostic_log()
        log.write(
            "heartbeat_thread_started",
            thread_name=current_thread().name,
            thread_ident=current_thread().ident,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            lease_seconds=self.lease_seconds,
            **_process_diagnostics(),
        )
        stop_reason = "shutdown_requested"
        attempt_sequence = 0
        try:
            while True:
                attempt_sequence += 1
                scheduled_monotonic = monotonic() + self.heartbeat_interval_seconds
                scheduled_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.heartbeat_interval_seconds)
                ).isoformat()
                log.write(
                    "heartbeat_renewal_scheduled",
                    attempt_sequence=attempt_sequence,
                    scheduled_at=scheduled_at,
                    scheduled_monotonic_seconds=round(scheduled_monotonic, 6),
                    **_process_diagnostics(),
                )
                if self._heartbeat_stop.wait(self.heartbeat_interval_seconds):
                    break
                attempt_started = monotonic()
                attempt_details = {
                    "attempt_sequence": attempt_sequence,
                    "scheduled_at": scheduled_at,
                    "scheduled_monotonic_seconds": round(
                        scheduled_monotonic, 6
                    ),
                    "attempt_monotonic_seconds": round(attempt_started, 6),
                    "schedule_delay_seconds": round(
                        max(0.0, attempt_started - scheduled_monotonic), 6
                    ),
                }
                log.write(
                    "heartbeat_renewal_attempt_entered",
                    **attempt_details,
                    **_process_diagnostics(),
                )

                def stage_timing(
                    stage: str,
                    phase: str,
                    duration_seconds: float | None,
                    error_type: str | None,
                ) -> None:
                    log.write(
                        "heartbeat_renewal_stage",
                        attempt_sequence=attempt_sequence,
                        stage=stage,
                        phase=phase,
                        duration_seconds=(
                            round(duration_seconds, 6)
                            if duration_seconds is not None else None
                        ),
                        error_type=error_type,
                        **_process_diagnostics(),
                    )

                try:
                    expiration = self.heartbeat(timing_callback=stage_timing)
                except BaseException as error:
                    with self._heartbeat_lock:
                        self._heartbeat_error = error
                    stop_reason = "renewal_failure"
                    log.write(
                        "heartbeat_renewal_failed",
                        level=logging.ERROR,
                        error_type=type(error).__name__,
                        error_message=str(error),
                        thread_name=current_thread().name,
                        thread_ident=current_thread().ident,
                        attempt_duration_seconds=round(
                            monotonic() - attempt_started, 6
                        ),
                        **attempt_details,
                        **self._renewal_diagnostics(),
                    )
                    self._heartbeat_stop.set()
                    return
                log.write(
                    "heartbeat_renewal_completed",
                    lease_expires_at=expiration,
                    attempt_duration_seconds=round(
                        monotonic() - attempt_started, 6
                    ),
                    **attempt_details,
                    **_process_diagnostics(),
                )
        finally:
            log.write(
                "heartbeat_thread_stopped",
                reason=stop_reason,
                thread_name=current_thread().name,
                thread_ident=current_thread().ident,
                **self._renewal_diagnostics(),
            )

    def start_heartbeat(self) -> None:
        """Start process-local renewal after configuration validation."""
        if self._heartbeat_thread is not None:
            return
        log = self._ensure_diagnostic_log()
        self._heartbeat_stop.clear()
        self._heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            name=f"registry-lease-{self.run_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        log.write("heartbeat_startup_completed", **self._thread_diagnostics())

    def assert_heartbeat_healthy(self) -> None:
        with self._heartbeat_lock:
            error = self._heartbeat_error
        if error is not None:
            raise RuntimeError(
                f"Writer lease heartbeat failed for run {self.run_id!r}"
            ) from error

    def release_lease(self) -> None:
        """Stop renewal and clear this writer's lease on graceful shutdown."""
        log = self._ensure_diagnostic_log()
        log.write("heartbeat_shutdown_requested", **self._thread_diagnostics())
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=max(self.heartbeat_interval_seconds, 1))
            self._heartbeat_thread = None
        try:
            release_run_lease(self.database, self.run_id, self.lease_owner)
        except BaseException as error:
            log.write(
                "lease_release_failed",
                level=logging.ERROR,
                error_type=type(error).__name__,
                error_message=str(error),
                **self._renewal_diagnostics(),
            )
            raise
        else:
            log.write("lease_released", **self._renewal_diagnostics())
        finally:
            log.close()
            self._diagnostic_log = None

    def resolve(self, observation: IdentityObservation, budget) -> AuthoritativeOutcome:
        """Preview, reserve possible NEW, then transactionally re-resolve."""
        preview = self.adapter.preview(observation)
        possible_new = (
            preview.resolution.classification is Classification.NEW
            and preview.resolution.action is ResolutionAction.CREATE_COMPANY
        )
        reservation = budget.try_reserve() if possible_new else None
        if possible_new and reservation is None:
            return AuthoritativeOutcome("limit", preview)
        try:
            decision = self.adapter.commit(
                self.run_id, observation, lease_owner=self.lease_owner,
            )
        except NewCompanyBudgetExhausted:
            if reservation is not None:
                reservation.release()
            return AuthoritativeOutcome("limit", preview)
        except BaseException as error:
            if reservation is not None:
                reservation.release()
            self._log_lease_expiration(error, "decision_commit")
            raise

        resolution = decision.resolution
        is_new = (
            resolution.classification is Classification.NEW
            and resolution.action is ResolutionAction.CREATE_COMPANY
            and not resolution.requires_review
        )
        if is_new and decision.newly_committed:
            if reservation is None:
                raise RegistryUnavailableError(
                    "Authoritative decision became NEW without a budget reservation"
                )
            reservation.commit()
            return AuthoritativeOutcome("new", decision)
        if reservation is not None:
            reservation.release()
        if is_new:
            return AuthoritativeOutcome("same_run", decision)
        return AuthoritativeOutcome(
            {
                Classification.KNOWN: "known",
                Classification.UPDATED: (
                    "branch"
                    if resolution.action is ResolutionAction.CREATE_BRANCH
                    else "updated"
                ),
                Classification.AMBIGUOUS: "ambiguous",
                Classification.QUARANTINED: "quarantined",
                Classification.LEGACY_UNKNOWN: "known",
            }[resolution.classification],
            decision,
        )

    def budget_snapshot(self) -> RunBudgetSnapshot:
        """Read durable NEW-company capacity directly from SQLite."""
        return get_run_budget_snapshot(self.database, self.run_id)

    def complete(
        self,
        *,
        status: str = "SUCCESS",
        failure_injector: FailureInjector | None = None,
    ) -> dict:
        """Publish and verify required outputs before recording terminal state."""
        if self._closed:
            raise RuntimeError(f"Discovery run {self.run_id!r} is already closed")
        if status not in {"SUCCESS", "PARTIAL"}:
            raise ValueError("Completed Maps runs must end in SUCCESS or PARTIAL")
        self._transition("FINALIZING")
        try:
            def inject(artifact: str) -> FailureInjector | None:
                if failure_injector is None:
                    return None
                return lambda point: failure_injector(f"{artifact}:{point}")

            export_new_companies(
                self.database,
                self.run_id,
                self.export_directory,
                failure_injector=inject("discovery"),
                verified_maps_publication=True,
            )
            self.qualification_result = assess_run(self.database, self.run_id)
            for purpose in ("employment", "mission"):
                export_qualified_leads(
                    self.database,
                    self.run_id,
                    self.export_directory,
                    purpose=purpose,
                    failure_injector=inject(purpose),
                )
            verified = verify_run_exports(
                self.database, self.run_id, self.export_directory,
            )
            self.export_result = verified["discovery"]
            self.qualified_export_results = {
                purpose: verified[purpose]
                for purpose in ("employment", "mission")
            }
            for result in verified.values():
                verify_export_manifest(result["manifest"])
            if failure_injector:
                failure_injector("after_exports_verified_before_success")
            self._transition(status)
            self._closed = True
            return self.export_result
        except BaseException:
            # Leave FINALIZING as truthful evidence of incomplete publication.
            # An explicit, fenced recovery can safely retry this publication.
            self._closed = True
            raise
        finally:
            self.release_lease()

    def fail(self, status: str = "FAILED") -> None:
        if self._closed:
            raise RuntimeError(f"Discovery run {self.run_id!r} is already closed")
        if status not in {"FAILED", "INTERRUPTED"}:
            raise ValueError("Failed Maps runs must end in FAILED or INTERRUPTED")
        try:
            self._transition(status)
            self._closed = True
        finally:
            self.release_lease()


def recover_finalizing_run(
    database: str | Path,
    export_directory: str | Path,
    run_id: str,
    queries: list[str],
    *,
    mode: str | DiscoveryMode = DiscoveryMode.AUTHORITATIVE_CANARY,
    new_company_limit: int | None = None,
    lease_seconds: int = 120,
    lease_owner: str | None = None,
    failure_injector: FailureInjector | None = None,
    allow_production_registry: bool = False,
) -> dict:
    """Explicitly rebuild, verify, and finish one fenced FINALIZING Maps run."""
    parsed_mode = parse_discovery_mode(mode)
    if not parsed_mode.is_authoritative:
        raise ValueError("Finalization recovery requires an authoritative mode")
    database_path, export_path = validate_authoritative_paths(
        database,
        export_directory,
        mode=parsed_mode,
        allow_production_registry=allow_production_registry,
    )
    owner = lease_owner or f"maps-finalizer-{uuid4()}"
    config_hash = maps_run_configuration_hash(
        queries,
        mode=parsed_mode,
        new_company_limit=new_company_limit,
        export_directory=export_path,
    )
    acquired = False
    acquire_finalization_lease(
        database_path,
        run_id,
        owner,
        expected_config_hash=config_hash,
        expected_new_company_limit=new_company_limit,
        lease_seconds=lease_seconds,
    )
    acquired = True
    try:
        qualification = assess_run(database_path, run_id)
        heartbeat_run_lease(
            database_path, run_id, owner, lease_seconds=lease_seconds,
        )
        publication = recover_run_exports(
            database_path,
            run_id,
            export_path,
            failure_injector=failure_injector,
        )
        heartbeat_run_lease(
            database_path, run_id, owner, lease_seconds=lease_seconds,
        )
        if failure_injector:
            failure_injector("after_exports_verified_before_success")
        transition_run(database_path, run_id, "SUCCESS", lease_owner=owner)
        return {**publication, "qualification": qualification, "status": "SUCCESS"}
    finally:
        if acquired:
            release_run_lease(database_path, run_id, owner)


def verify_finalized_run(
    database: str | Path,
    export_directory: str | Path,
    run_id: str,
    *,
    allow_production_registry: bool = False,
) -> dict[str, dict]:
    """Read-only verification for a terminal SUCCESS run; never acquires a lease."""
    database_path, export_path = validate_authoritative_paths(
        database,
        export_directory,
        mode=DiscoveryMode.AUTHORITATIVE,
        allow_production_registry=allow_production_registry,
    )
    connection = open_registry(database_path)
    try:
        row = connection.execute(
            "SELECT status FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise LookupError(f"Unknown discovery run: {run_id}")
    if row["status"] != "SUCCESS":
        raise RuntimeError(
            f"Read-only finalized verification requires SUCCESS, got {row['status']}"
        )
    return verify_run_exports(database_path, run_id, export_path)
