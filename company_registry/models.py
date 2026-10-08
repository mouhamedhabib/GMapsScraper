"""Immutable public contracts for company identity resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Classification(str, Enum):
    NEW = "NEW"
    KNOWN = "KNOWN"
    UPDATED = "UPDATED"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"
    QUARANTINED = "QUARANTINED"


class ResolutionAction(str, Enum):
    CREATE_COMPANY = "CREATE_COMPANY"
    CREATE_BRANCH = "CREATE_BRANCH"
    UPDATE_ENTITY = "UPDATE_ENTITY"
    ADD_PLACE_ALIAS = "ADD_PLACE_ALIAS"
    MATCH_ONLY = "MATCH_ONLY"
    NONE = "NONE"


@dataclass(frozen=True)
class IdentityObservation:
    source_system: str
    source_record_key: str
    observed_at: str
    name: str = ""
    place_id: str = ""
    website_url: str = ""
    phone: str = ""
    address: str = ""
    raw_payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))


@dataclass(frozen=True)
class NormalizedObservation:
    source_system: str
    source_record_key: str
    observed_at: str
    name: str
    name_key: str
    place_id: str
    website_url: str
    domain: str
    trustworthy_domain: bool
    phone: str
    phone_key: str
    address: str
    address_key: str
    raw_payload_json: str
    payload_hash: str


@dataclass(frozen=True)
class Resolution:
    classification: Classification
    action: ResolutionAction
    company_id: str | None = None
    branch_id: str | None = None
    candidate_company_ids: tuple[str, ...] = ()
    candidate_branch_ids: tuple[str, ...] = ()
    matched_evidence: tuple[str, ...] = ()
    conflicting_evidence: tuple[str, ...] = ()
    requires_review: bool = False
    reason: str = ""

