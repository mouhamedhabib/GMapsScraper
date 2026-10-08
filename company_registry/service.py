"""Atomic write service for standalone company identity resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from company_registry.models import IdentityObservation, Resolution, ResolutionAction
from company_registry.normalization import canonical_json
from company_registry.repository import RegistryRepository, stable_id
from company_registry.resolver import RESOLVER_VERSION, ResolutionPolicy, normalize_observation
from company_registry.storage import DEFAULT_DATABASE, connect_registry


FailureInjector = Callable[[str], None]


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
    def _ensure_run(repository: RegistryRepository, run_id: str) -> None:
        row = repository.connection.execute(
            "SELECT run_id FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown discovery run: {run_id}")

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

    def resolve(self, run_id: str, observation: IdentityObservation) -> Resolution:
        item = normalize_observation(observation)
        now = datetime.now(timezone.utc).isoformat()
        connection = connect_registry(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            repository = RegistryRepository(connection)
            self._ensure_run(repository, run_id)
            prior = repository.prior_observation(
                item.source_system, item.source_record_key, item.payload_hash,
            )
            if prior is not None:
                result = repository.resolution_from_row(prior)
                repository.record_run_company(run_id, result, item.observed_at, now)
                connection.commit()
                return result

            result = ResolutionPolicy(repository).evaluate(item)
            self._apply(repository, item, result, now)
            self._checkpoint("after_entity_changes")
            observation_id = stable_id(
                "observation",
                f"{item.source_system}|{item.source_record_key}|{item.payload_hash}",
            )
            repository.persist_observation(
                observation_id, run_id, item, result, self._normalized_json(item),
                RESOLVER_VERSION, now,
            )
            repository.record_run_company(run_id, result, item.observed_at, now)
            self._checkpoint("before_commit")
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
