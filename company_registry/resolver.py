"""Deterministic company/branch identity resolution policy."""

from __future__ import annotations

import json
from pathlib import Path

from company_registry.models import (
    Classification,
    IdentityObservation,
    NormalizedObservation,
    Resolution,
    ResolutionAction,
)
from company_registry.normalization import (
    canonical_json,
    clean_text,
    is_trustworthy_domain,
    normalize_address,
    normalize_domain,
    normalize_google_place_id,
    normalize_name,
    normalize_phone,
    payload_hash,
)
from company_registry.repository import RegistryRepository, stable_id
from company_registry.storage import DEFAULT_DATABASE, connect_registry


RESOLVER_VERSION = "2A.1"


def normalize_observation(observation: IdentityObservation) -> NormalizedObservation:
    raw = dict(observation.raw_payload) or {
        "name": observation.name,
        "place_id": observation.place_id,
        "website_url": observation.website_url,
        "phone": observation.phone,
        "address": observation.address,
    }
    domain = normalize_domain(observation.website_url)
    return NormalizedObservation(
        source_system=clean_text(observation.source_system).upper(),
        source_record_key=clean_text(observation.source_record_key),
        observed_at=clean_text(observation.observed_at),
        name=clean_text(observation.name),
        name_key=normalize_name(observation.name),
        place_id=normalize_google_place_id(observation.place_id),
        website_url=clean_text(observation.website_url),
        domain=domain,
        trustworthy_domain=is_trustworthy_domain(domain),
        phone=clean_text(observation.phone),
        phone_key=normalize_phone(observation.phone),
        address=clean_text(observation.address),
        address_key=normalize_address(observation.address),
        raw_payload_json=canonical_json(raw),
        payload_hash=payload_hash(raw),
    )


class ResolutionPolicy:
    """Apply explicit evidence rules without mutating the database."""

    def __init__(self, repository: RegistryRepository):
        self.repository = repository

    @staticmethod
    def unresolved(classification: Classification, reason: str, *,
                   companies=(), branches=(), conflicts=()) -> Resolution:
        return Resolution(
            classification=classification, action=ResolutionAction.NONE,
            candidate_company_ids=tuple(sorted(companies)),
            candidate_branch_ids=tuple(sorted(branches)),
            conflicting_evidence=tuple(sorted(conflicts)),
            requires_review=True, reason=reason,
        )

    def _historical_guard(self, item: NormalizedObservation) -> Resolution | None:
        """Block NEW on exact or strong unresolved history overlap.

        Exact payload/place matches are always protected. Without a new Place ID,
        name plus phone, address, or trustworthy domain is strong overlap. Domain
        alone is deliberately ignored. A new unmatched Place ID is decisive unless
        that same Place ID appeared in unresolved history.
        """
        overlaps = []
        for row in self.repository.unresolved_history():
            if row["source_content_hash"] == item.payload_hash:
                status = Classification(row["resolution_status"])
                return self.unresolved(
                    status, "exact unresolved historical source fingerprint",
                    conflicts=(row["reason"] or "historical unresolved record",),
                )
            raw = json.loads(row["raw_record_json"])
            old_name = normalize_name(raw.get("title") or raw.get("company_name") or raw.get("name"))
            old_place = normalize_google_place_id(
                raw.get("place_id") or raw.get("map_place_id")
                or raw.get("map_link") or raw.get("maps_identity")
            )
            old_domain = normalize_domain(
                raw.get("webpage") or raw.get("website") or raw.get("source_url")
            )
            old_phone = normalize_phone(raw.get("phone_number") or raw.get("phone"))
            old_address = normalize_address(raw.get("address") or raw.get("location"))
            if item.place_id and old_place == item.place_id:
                overlaps.append("historical unresolved Place ID")
                continue
            if not item.place_id and item.name_key and item.name_key == old_name:
                if item.phone_key and item.phone_key == old_phone:
                    overlaps.append("historical unresolved name+phone")
                elif item.address_key and item.address_key == old_address:
                    overlaps.append("historical unresolved name+address")
                elif (item.trustworthy_domain and item.domain == old_domain):
                    overlaps.append("historical unresolved name+domain")
        if overlaps:
            return self.unresolved(
                Classification.AMBIGUOUS,
                "strong overlap with unresolved historical evidence",
                conflicts=overlaps,
            )
        return None

    def _place_match(self, item: NormalizedObservation,
                     branches: set[str]) -> Resolution:
        if len(branches) != 1:
            return self.unresolved(
                Classification.AMBIGUOUS, "Place ID maps to multiple branches",
                branches=branches, conflicts=("non-unique Place ID",),
            )
        branch_id = next(iter(branches))
        company_id = self.repository.branch_company(branch_id)
        company_values = self.repository.company_identity_values(company_id)
        branch_values = self.repository.branch_identity_values(branch_id)
        conflicts = []
        name_conflict = bool(
            item.name_key and company_values["NAME"]
            and item.name_key not in company_values["NAME"]
        )
        domain_conflict = bool(
            item.trustworthy_domain and company_values["WEBSITE_DOMAIN"]
            and item.domain not in company_values["WEBSITE_DOMAIN"]
        )
        phone_conflict = bool(
            item.phone_key and branch_values["PHONE"]
            and item.phone_key not in branch_values["PHONE"]
        )
        address_conflict = bool(
            item.address_key and branch_values["ADDRESS"]
            and item.address_key not in branch_values["ADDRESS"]
        )
        if name_conflict and domain_conflict:
            conflicts.extend(("company name conflicts", "company domain conflicts"))
        if name_conflict and phone_conflict and address_conflict:
            conflicts.extend(("branch phone conflicts", "branch address conflicts"))
        if conflicts:
            return self.unresolved(
                Classification.AMBIGUOUS,
                "Place ID matched but company ownership evidence conflicts",
                companies=(company_id,), branches=(branch_id,), conflicts=conflicts,
            )

        new_evidence = []
        for label, value, known in (
            ("company name", item.name_key, company_values["NAME"]),
            ("company domain", item.domain if item.trustworthy_domain else "",
             company_values["WEBSITE_DOMAIN"]),
            ("branch name", item.name_key, branch_values["NAME"]),
            ("branch phone", item.phone_key, branch_values["PHONE"]),
            ("branch address", item.address_key, branch_values["ADDRESS"]),
        ):
            if value and value not in known:
                new_evidence.append(label)
        classification = Classification.UPDATED if new_evidence else Classification.KNOWN
        action = ResolutionAction.UPDATE_ENTITY if new_evidence else ResolutionAction.MATCH_ONLY
        return Resolution(
            classification, action, company_id, branch_id,
            matched_evidence=("exact Place ID", *tuple(sorted(new_evidence))),
            reason=("exact Place ID with new compatible evidence" if new_evidence
                    else "exact Place ID match"),
        )

    def evaluate(self, item: NormalizedObservation) -> Resolution:
        if not item.source_system or not item.source_record_key or not item.observed_at:
            return self.unresolved(
                Classification.QUARANTINED,
                "missing source system, source key, or observation timestamp",
            )
        place_matches = self.repository.branch_candidates(
            "GOOGLE_MAPS_PLACE_ID", item.place_id,
        )
        if place_matches:
            return self._place_match(item, place_matches)

        if not item.name_key:
            return self.unresolved(
                Classification.QUARANTINED,
                "unmatched observation is missing a company name",
            )
        if not item.place_id and not any((
            item.trustworthy_domain, item.phone_key, item.address_key,
        )):
            return self.unresolved(Classification.QUARANTINED, "insufficient identity evidence")

        name_branches = self.repository.branch_candidates("NAME", item.name_key)
        phone_branches = self.repository.branch_candidates("PHONE", item.phone_key)
        address_branches = self.repository.branch_candidates("ADDRESS", item.address_key)
        name_phone = name_branches & phone_branches if item.name_key and item.phone_key else set()
        name_address = name_branches & address_branches if item.name_key and item.address_key else set()
        branch_matches = name_phone | name_address

        if item.place_id:
            strict_alias = (
                name_branches & phone_branches & address_branches
                if item.name_key and item.phone_key and item.address_key else set()
            )
            if len(strict_alias) == 1:
                branch_id = next(iter(strict_alias))
                company_id = self.repository.branch_company(branch_id)
                return Resolution(
                    Classification.UPDATED, ResolutionAction.ADD_PLACE_ALIAS,
                    company_id, branch_id,
                    candidate_company_ids=(company_id,),
                    candidate_branch_ids=(branch_id,),
                    matched_evidence=("exact name", "exact phone", "exact address"),
                    reason="new Place ID accepted as a strongly proven branch alias",
                )
            if branch_matches:
                return self.unresolved(
                    Classification.AMBIGUOUS,
                    "new Place ID overlaps existing branch evidence but alias proof is incomplete",
                    branches=branch_matches,
                    companies={self.repository.branch_company(branch) for branch in branch_matches},
                    conflicts=("different Place ID",),
                )
        elif len(branch_matches) == 1:
            branch_id = next(iter(branch_matches))
            company_id = self.repository.branch_company(branch_id)
            matched = []
            if branch_id in name_phone:
                matched.append("exact name+phone")
            if branch_id in name_address:
                matched.append("exact name+address")
            return Resolution(
                Classification.KNOWN, ResolutionAction.MATCH_ONLY,
                company_id, branch_id,
                candidate_company_ids=(company_id,),
                candidate_branch_ids=(branch_id,),
                matched_evidence=tuple(matched), reason="unique branch evidence match",
            )
        elif len(branch_matches) > 1:
            return self.unresolved(
                Classification.AMBIGUOUS, "supporting evidence matches multiple branches",
                branches=branch_matches,
                companies={self.repository.branch_company(branch) for branch in branch_matches},
            )

        name_companies = self.repository.company_candidates("NAME", item.name_key)
        domain_companies = self.repository.company_candidates(
            "WEBSITE_DOMAIN", item.domain if item.trustworthy_domain else "",
        ) | self.repository.company_candidates(
            "EMAIL_DOMAIN", item.domain if item.trustworthy_domain else "",
        )
        company_matches = (
            name_companies & domain_companies
            if item.name_key and item.trustworthy_domain else set()
        )
        if len(company_matches) > 1:
            return self.unresolved(
                Classification.AMBIGUOUS,
                "exact company name and domain match multiple companies",
                companies=company_matches,
            )
        if len(company_matches) == 1:
            company_id = next(iter(company_matches))
            if item.place_id:
                branch_id = stable_id("branch", f"place|{item.place_id}")
                return Resolution(
                    Classification.UPDATED, ResolutionAction.CREATE_BRANCH,
                    company_id, branch_id,
                    candidate_company_ids=(company_id,),
                    matched_evidence=("exact company name+domain",),
                    reason="new Place branch for an existing company",
                )
            return Resolution(
                Classification.KNOWN, ResolutionAction.MATCH_ONLY,
                company_id, None, candidate_company_ids=(company_id,),
                matched_evidence=("exact company name+domain",),
                reason="company-only identity match",
            )

        if name_companies or domain_companies or branch_matches:
            candidates = name_companies | domain_companies
            return self.unresolved(
                Classification.AMBIGUOUS,
                "partial identity overlap does not prove company ownership",
                companies=candidates,
                branches=branch_matches,
                conflicts=("shared or conflicting identity evidence",),
            )

        protected = self._historical_guard(item)
        if protected is not None:
            return protected

        if item.trustworthy_domain:
            company_seed = f"name-domain|{item.name_key}|{item.domain}"
        elif item.place_id:
            company_seed = f"name-place|{item.name_key}|{item.place_id}"
        else:
            company_seed = (
                f"name-support|{item.name_key}|{item.phone_key}|{item.address_key}"
            )
        company_id = stable_id("company", company_seed)
        branch_id = None
        if item.place_id:
            branch_id = stable_id("branch", f"place|{item.place_id}")
        elif item.phone_key or item.address_key:
            branch_id = stable_id(
                "branch", f"{company_id}|{item.name_key}|{item.phone_key}|{item.address_key}",
            )
        return Resolution(
            Classification.NEW, ResolutionAction.CREATE_COMPANY,
            company_id, branch_id,
            matched_evidence=tuple(value for value in (
                "valid Place ID" if item.place_id else "",
                "name+trustworthy domain" if item.trustworthy_domain else "",
                "name+phone" if item.phone_key else "",
                "name+address" if item.address_key else "",
            ) if value),
            reason="no existing or unresolved historical identity conflict",
        )


class IdentityResolver:
    """Standalone Phase 2A facade. It is not connected to live scrapers."""

    def __init__(self, database: str | Path = DEFAULT_DATABASE):
        self.database = Path(database)

    def preview(self, observation: IdentityObservation) -> Resolution:
        connection = connect_registry(self.database)
        try:
            return ResolutionPolicy(RegistryRepository(connection)).evaluate(
                normalize_observation(observation)
            )
        finally:
            connection.close()

    def resolve(self, run_id: str, observation: IdentityObservation) -> Resolution:
        from company_registry.service import RegistryService
        return RegistryService(self.database).resolve(run_id, observation)
