"""Offline contract for future SQLite-authoritative discovery integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from company_registry.models import IdentityObservation, Resolution
from company_registry.repository import stable_id
from company_registry.resolver import IdentityResolver, normalize_observation
from company_registry.service import NewCompanyBudgetExhausted, RegistryService
from company_registry.storage import DEFAULT_DATABASE


class DiscoveryMode(str, Enum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    AUTHORITATIVE_CANARY = "authoritative-canary"
    AUTHORITATIVE = "authoritative"

    @property
    def is_authoritative(self) -> bool:
        return self in {
            DiscoveryMode.AUTHORITATIVE_CANARY,
            DiscoveryMode.AUTHORITATIVE,
        }


DEFAULT_DISCOVERY_MODE = DiscoveryMode.LEGACY


class RegistryUnavailableError(RuntimeError):
    """Fail-closed authoritative registry access error."""


@dataclass(frozen=True)
class DiscoveryDecision:
    resolution: Resolution
    committed: bool
    decision_id: str | None = None
    observation_id: str | None = None
    newly_committed: bool = field(default=False, compare=False)

    @property
    def authorizes_new_company_export(self) -> bool:
        return bool(
            self.committed
            and not self.resolution.requires_review
            and self.resolution.classification.value == "NEW"
            and self.resolution.action.value == "CREATE_COMPANY"
        )


def parse_discovery_mode(value: str | DiscoveryMode) -> DiscoveryMode:
    if isinstance(value, DiscoveryMode):
        return value
    try:
        return DiscoveryMode(str(value).strip().casefold())
    except ValueError as error:
        choices = ", ".join(mode.value for mode in DiscoveryMode)
        raise ValueError(f"Unknown discovery mode {value!r}; expected one of: {choices}") from error


class AuthoritativeDiscoveryAdapter:
    """Fail-closed preview/commit facade; not wired to any live scraper."""

    def __init__(
        self,
        database: str | Path = DEFAULT_DATABASE,
        *,
        mode: str | DiscoveryMode = DiscoveryMode.AUTHORITATIVE_CANARY,
    ) -> None:
        self.database = Path(database)
        self.mode = parse_discovery_mode(mode)
        if not self.mode.is_authoritative:
            raise ValueError("AuthoritativeDiscoveryAdapter requires an authoritative mode")

    @staticmethod
    def _ids(run_id: str, observation: IdentityObservation) -> tuple[str, str]:
        normalized = normalize_observation(observation)
        observation_id = stable_id(
            "observation",
            f"{normalized.source_system}|{normalized.source_record_key}|{normalized.payload_hash}",
        )
        return observation_id, stable_id("decision", f"{run_id}|{observation_id}")

    def preview(self, observation: IdentityObservation) -> DiscoveryDecision:
        """Evaluate current evidence without authorizing persistence or export."""
        try:
            result = IdentityResolver(self.database).preview(observation)
            return DiscoveryDecision(result, committed=False)
        except Exception as error:
            raise RegistryUnavailableError(
                f"Authoritative registry preview failed closed: {type(error).__name__}: {error}"
            ) from error

    def commit(
        self,
        run_id: str,
        observation: IdentityObservation,
        *,
        lease_owner: str | None = None,
    ) -> DiscoveryDecision:
        """Perform final transactional resolution and return a committed decision."""
        try:
            commit = RegistryService(self.database).resolve_with_metadata(
                run_id, observation, lease_owner=lease_owner,
            )
            result = commit.resolution
            observation_id, decision_id = self._ids(run_id, observation)
            return DiscoveryDecision(
                result, committed=True, decision_id=decision_id,
                observation_id=observation_id,
                newly_committed=commit.decision_created,
            )
        except NewCompanyBudgetExhausted:
            raise
        except Exception as error:
            raise RegistryUnavailableError(
                f"Authoritative registry commit failed closed: {type(error).__name__}: {error}"
            ) from error
