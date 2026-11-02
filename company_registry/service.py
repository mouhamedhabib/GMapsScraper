"""Atomic write service for standalone company identity resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from company_registry.models import IdentityObservation, Resolution, ResolutionAction
from company_registry.normalization import canonical_json
from company_registry.repository import RegistryRepository, stable_id
from company_registry.resolver import RESOLVER_VERSION, ResolutionPolicy, normalize_observation
from company_registry.storage import DEFAULT_DATABASE, connect_registry


FailureInjector = Callable[[str], None]


@dataclass(frozen=True)
class ResolveCommit:
    """Result plus whether this transaction created the run decision."""

    resolution: Resolution
    decision_created: bool


@dataclass(frozen=True)
class RunBudgetSnapshot:
    """SQLite-derived NEW-company capacity for one durable run."""

    configured_limit: int | None
    committed_new: int
    remaining_capacity: int | None
    exhausted: bool


@dataclass(frozen=True)
class QueryBudgetSnapshot:
    """SQLite-derived NEW-company capacity for one configured run query."""

    query_index: int
    query_text: str
    configured_limit: int
    committed_new: int
    remaining_capacity: int
    exhausted: bool


class NewCompanyBudgetExhausted(RuntimeError):
    """A RUNNING run has no durable capacity for another NEW company."""

    def __init__(self, run_id: str, snapshot: RunBudgetSnapshot) -> None:
        self.run_id = run_id
        self.snapshot = snapshot
        super().__init__(
            f"Discovery run {run_id!r} exhausted its SQLite NEW-company "
            f"limit ({snapshot.committed_new}/{snapshot.configured_limit})"
        )


class QueryNewCompanyBudgetExhausted(RuntimeError):
    """A configured query has no durable capacity for another NEW company."""

    def __init__(self, run_id: str, snapshot: QueryBudgetSnapshot) -> None:
        self.run_id = run_id
        self.snapshot = snapshot
        super().__init__(
            f"Discovery run {run_id!r} query {snapshot.query_index} exhausted "
            f"its SQLite NEW-company limit "
            f"({snapshot.committed_new}/{snapshot.configured_limit})"
        )


def _budget_snapshot(connection, run_id: str) -> RunBudgetSnapshot:
    run = connection.execute(
        "SELECT new_company_limit FROM discovery_runs WHERE run_id=?", (run_id,),
    ).fetchone()
    if run is None:
        raise LookupError(f"Unknown discovery run: {run_id}")
    committed = connection.execute(
        """SELECT count(*) FROM discovery_run_decisions
            WHERE run_id=?
              AND classification='NEW'
              AND resolution_action='CREATE_COMPANY'
              AND requires_review=0""",
        (run_id,),
    ).fetchone()[0]
    limit = run["new_company_limit"]
    if limit is not None and committed > limit:
        raise RuntimeError(
            f"Discovery run {run_id!r} has {committed} committed NEW decisions, "
            f"exceeding its configured limit {limit}"
        )
    remaining = None if limit is None else limit - committed
    return RunBudgetSnapshot(
        configured_limit=limit,
        committed_new=committed,
        remaining_capacity=remaining,
        exhausted=limit is not None and remaining == 0,
    )


def get_run_budget_snapshot(
    database: str | Path,
    run_id: str,
) -> RunBudgetSnapshot:
    """Return a read-only, restart-safe budget snapshot derived from SQLite."""
    connection = connect_registry(database)
    try:
        connection.execute("BEGIN")
        snapshot = _budget_snapshot(connection, run_id)
        connection.commit()
        return snapshot
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _query_budget_snapshot(
    connection,
    run_id: str,
    query_index: int,
) -> QueryBudgetSnapshot:
    run = connection.execute(
        "SELECT max_new_per_query FROM discovery_runs WHERE run_id=?", (run_id,),
    ).fetchone()
    if run is None:
        raise LookupError(f"Unknown discovery run: {run_id}")
    limit = run["max_new_per_query"]
    if limit is None:
        raise LookupError(f"Discovery run {run_id!r} has no per-query limit")
    query = connection.execute(
        """SELECT query_text, committed_new_count
             FROM discovery_run_queries
            WHERE run_id=? AND query_index=?""",
        (run_id, query_index),
    ).fetchone()
    if query is None:
        raise LookupError(
            f"Discovery run {run_id!r} has no configured query index {query_index}"
        )
    committed = query["committed_new_count"]
    if committed > limit:
        raise RuntimeError(
            f"Discovery run {run_id!r} query {query_index} has {committed} "
            f"committed NEW decisions, exceeding its configured limit {limit}"
        )
    remaining = limit - committed
    return QueryBudgetSnapshot(
        query_index=query_index,
        query_text=query["query_text"],
        configured_limit=limit,
        committed_new=committed,
        remaining_capacity=remaining,
        exhausted=remaining == 0,
    )


def get_query_budget_snapshot(
    database: str | Path,
    run_id: str,
    query_index: int,
) -> QueryBudgetSnapshot:
    """Return one read-only, restart-safe per-query budget snapshot."""
    connection = connect_registry(database)
    try:
        connection.execute("BEGIN")
        snapshot = _query_budget_snapshot(connection, run_id, query_index)
        connection.commit()
        return snapshot
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


class RegistryService:
    """Resolve and persist one observation in one immediate transaction."""

    def __init__(
        self,
        database: str | Path = DEFAULT_DATABASE,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.database = Path(database)
        self._failure_injector = failure_injector

    def _checkpoint(self, name: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(name)

    @staticmethod
    def _normalized_json(item) -> str:
        return canonical_json({
            "name": item.name_key,
            "place_id": item.place_id,
            "domain": item.domain,
            "trustworthy_domain": item.trustworthy_domain,
            "phone": item.phone_key,
            "address": item.address_key,
        })

    @staticmethod
    def _ensure_run(
        repository: RegistryRepository,
        run_id: str,
        lease_owner: str | None = None,
    ):
        row = repository.connection.execute(
            """SELECT status, new_company_limit, lease_owner, lease_expires_at
                 FROM discovery_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        if row["status"] != "RUNNING":
            raise RuntimeError(
                f"Discovery run {run_id!r} does not accept decisions while "
                f"in {row['status']} state"
            )
        if row["lease_owner"] is not None and lease_owner is None:
            raise RuntimeError(
                f"Discovery run {run_id!r} requires its writer lease token"
            )
        if lease_owner is not None:
            now = datetime.now(timezone.utc).isoformat()
            if row["lease_owner"] != lease_owner:
                raise RuntimeError(
                    f"Discovery run {run_id!r} writer lease is owned elsewhere"
                )
            if not row["lease_expires_at"] or row["lease_expires_at"] <= now:
                raise RuntimeError(
                    f"Discovery run {run_id!r} writer lease has expired"
                )
        return row

    @staticmethod
    def _consumes_new_budget(result: Resolution) -> bool:
        return bool(
            result.classification.value == "NEW"
            and result.action is ResolutionAction.CREATE_COMPANY
            and not result.requires_review
        )

    @staticmethod
    def _add_company_evidence(repository, company_id, item, now) -> None:
        repository.add_company_identity(
            company_id, "NAME", item.name, item.name_key, item.observed_at, now,
        )
        if item.trustworthy_domain:
            repository.add_company_identity(
                company_id, "WEBSITE_DOMAIN", item.website_url or item.domain,
                item.domain, item.observed_at, now,
            )

    @staticmethod
    def _add_branch_evidence(repository, branch_id, item, now) -> None:
        for identity_type, raw, normalized in (
            ("GOOGLE_MAPS_PLACE_ID", item.place_id, item.place_id),
            ("NAME", item.name, item.name_key),
            ("PHONE", item.phone, item.phone_key),
            ("ADDRESS", item.address, item.address_key),
        ):
            repository.add_branch_identity(
                branch_id, identity_type, raw, normalized, item.observed_at, now,
            )

    def _apply(self, repository: RegistryRepository, item, result: Resolution,
               now: str) -> None:
        if result.action is ResolutionAction.NONE:
            return
        if result.action is ResolutionAction.CREATE_COMPANY:
            repository.create_company(result.company_id, item.name, item.observed_at, now)
            self._add_company_evidence(repository, result.company_id, item, now)
            if result.branch_id:
                repository.create_branch(
                    result.branch_id, result.company_id, item.name, item.place_id,
                    item.website_url, item.phone, item.address, item.observed_at, now,
                )
                self._add_branch_evidence(repository, result.branch_id, item, now)
        elif result.action is ResolutionAction.CREATE_BRANCH:
            repository.create_branch(
                result.branch_id, result.company_id, item.name, item.place_id,
                item.website_url, item.phone, item.address, item.observed_at, now,
            )
            self._add_company_evidence(repository, result.company_id, item, now)
            self._add_branch_evidence(repository, result.branch_id, item, now)
        elif result.action in {
            ResolutionAction.UPDATE_ENTITY, ResolutionAction.ADD_PLACE_ALIAS,
        }:
            self._add_company_evidence(repository, result.company_id, item, now)
            if result.branch_id:
                self._add_branch_evidence(repository, result.branch_id, item, now)

        repository.touch_company(result.company_id, item.observed_at, now)
        if result.branch_id:
            repository.touch_branch(result.branch_id, item.observed_at, now)

    def resolve_with_metadata(
        self,
        run_id: str,
        observation: IdentityObservation,
        *,
        lease_owner: str | None = None,
        query_index: int | None = None,
    ) -> ResolveCommit:
        item = normalize_observation(observation)
        now = datetime.now(timezone.utc).isoformat()
        connection = connect_registry(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            repository = RegistryRepository(connection)
            self._ensure_run(repository, run_id, lease_owner)
            prior = repository.prior_run_decision(
                run_id, item.source_system, item.source_record_key, item.payload_hash,
            )
            if prior is not None:
                result = repository.resolution_from_row(prior)
                repository.record_run_company(run_id, result, item.observed_at, now)
                connection.commit()
                return ResolveCommit(result, decision_created=False)

            result = ResolutionPolicy(repository).evaluate(item)
            query_budget_enabled = False
            if self._consumes_new_budget(result):
                snapshot = _budget_snapshot(connection, run_id)
                if snapshot.exhausted:
                    raise NewCompanyBudgetExhausted(run_id, snapshot)
                run = connection.execute(
                    "SELECT max_new_per_query FROM discovery_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                query_budget_enabled = run["max_new_per_query"] is not None
                if query_budget_enabled:
                    if query_index is None or isinstance(query_index, bool):
                        raise ValueError(
                            "query_index is required when max_new_per_query is configured"
                        )
                    query_snapshot = _query_budget_snapshot(
                        connection, run_id, query_index,
                    )
                    if query_snapshot.exhausted:
                        raise QueryNewCompanyBudgetExhausted(
                            run_id, query_snapshot,
                        )
            self._apply(repository, item, result, now)
            self._checkpoint("after_entity_changes")
            observation = repository.observation(
                item.source_system, item.source_record_key, item.payload_hash,
            )
            if observation is None:
                observation_id = stable_id(
                    "observation",
                    f"{item.source_system}|{item.source_record_key}|{item.payload_hash}",
                )
                repository.persist_observation(
                    observation_id, run_id, item, result, self._normalized_json(item),
                    RESOLVER_VERSION, now,
                )
            else:
                observation_id = observation["observation_id"]
            decision_id = stable_id("decision", f"{run_id}|{observation_id}")
            repository.persist_run_decision(
                decision_id, run_id, observation_id, result,
                RESOLVER_VERSION, item.observed_at, now,
            )
            if query_index is not None:
                configured_query = connection.execute(
                    """SELECT 1 FROM discovery_run_queries
                        WHERE run_id=? AND query_index=?""",
                    (run_id, query_index),
                ).fetchone()
                if configured_query is not None:
                    connection.execute(
                        """INSERT INTO discovery_run_query_decisions
                           (run_id, query_index, decision_id, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (run_id, query_index, decision_id, now),
                    )
            if self._consumes_new_budget(result) and query_budget_enabled:
                cursor = connection.execute(
                    """UPDATE discovery_run_queries
                          SET committed_new_count=committed_new_count + 1,
                              updated_at=?
                        WHERE run_id=? AND query_index=?""",
                    (now, run_id, query_index),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Missing durable query ledger for index {query_index}"
                    )
            repository.record_run_company(run_id, result, item.observed_at, now)
            self._checkpoint("before_commit")
            connection.commit()
            return ResolveCommit(result, decision_created=True)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def resolve(
        self,
        run_id: str,
        observation: IdentityObservation,
        *,
        lease_owner: str | None = None,
    ) -> Resolution:
        return self.resolve_with_metadata(
            run_id, observation, lease_owner=lease_owner,
        ).resolution
