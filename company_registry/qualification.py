"""Deterministic, dual-purpose qualification of committed Maps companies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from company_registry.normalization import (
    canonical_json,
    clean_text,
    is_trustworthy_domain,
    payload_hash,
)
from company_registry.repository import stable_id
from company_registry.storage import open_registry


RELEVANCE_VALUES = {"TARGET", "POSSIBLE", "NOISE", "UNKNOWN"}
ELIGIBILITY_VALUES = {"ELIGIBLE", "REVIEW", "EXCLUDED"}


@dataclass(frozen=True)
class QualificationResult:
    employment_relevance: str
    mission_relevance: str
    employment_eligibility: str
    mission_eligibility: str
    employment_reasons: tuple[str, ...]
    mission_reasons: tuple[str, ...]
    evidence: dict


@dataclass(frozen=True)
class QualificationPolicy:
    """Versioned policy configuration; changing rules requires a new version."""

    version: str = "maps-dual-purpose-v1"
    employment_strong_terms: tuple[str, ...] = (
        "software company", "software development", "software developer",
        "web development", "mobile app development", "it consulting",
        "it consultant", "information technology company", "it services",
        "technology company", "technology consultant",
    )
    employment_possible_terms: tuple[str, ...] = (
        "computer support", "computer service", "computer repair",
        "software retailer", "software store", "computer store",
        "digital agency", "web agency", "internet service provider",
    )
    mission_strong_terms: tuple[str, ...] = (
        "software development", "web development", "mobile app development",
        "it consulting", "it services", "digital agency", "web agency",
        "automation", "systems integration", "api development",
    )
    mission_possible_terms: tuple[str, ...] = (
        "travel agency", "tour operator", "hotel", "resort", "e-commerce",
        "ecommerce", "online store", "retailer", "retail", "marketing agency",
        "advertising agency", "logistics", "manufacturer", "clinic",
        "restaurant", "real estate agency",
    )
    definite_noise_terms: tuple[str, ...] = (
        "government office", "police station", "place of worship", "cemetery",
        "public park", "bus stop",
    )

    def assess(
        self,
        row: dict,
        *,
        identity_conflict: bool = False,
        location_conflict: bool = False,
    ) -> QualificationResult:
        return assess_maps_company(
            row,
            policy=self,
            identity_conflict=identity_conflict,
            location_conflict=location_conflict,
        )


DEFAULT_QUALIFICATION_POLICY = QualificationPolicy()


def _contains(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def _first(row: dict, *fields: str) -> str:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value
    return ""


def _evidence_snapshot(
    row: dict, *, identity_conflict: bool, location_conflict: bool,
) -> dict:
    website = _first(row, "webpage", "website", "website_url")
    domain = website if is_trustworthy_domain(website) else ""
    return {
        "name": _first(row, "title", "company_name", "name"),
        "category": _first(row, "category", "categories"),
        "description": _first(
            row, "about_desc", "business_description", "description", "about",
        ),
        "website": website,
        "usable_website": bool(domain),
        "phone": _first(row, "phone_number", "phone"),
        "email": _first(row, "email", "emails", "email_address"),
        "address": _first(row, "address", "location"),
        "country": _first(row, "country"),
        "city": _first(row, "city"),
        "identity_conflict": bool(identity_conflict),
        "location_conflict": bool(location_conflict),
    }


def assess_maps_company(
    row: dict,
    *,
    policy: QualificationPolicy = DEFAULT_QUALIFICATION_POLICY,
    identity_conflict: bool = False,
    location_conflict: bool = False,
) -> QualificationResult:
    """Assess only supplied Maps evidence; no need or hiring claim is inferred."""
    evidence = _evidence_snapshot(
        row, identity_conflict=identity_conflict, location_conflict=location_conflict,
    )
    name = evidence["name"].casefold()
    category = evidence["category"].casefold()
    description = evidence["description"].casefold()
    descriptive = " ".join(part for part in (category, description) if part)
    all_text = " ".join(part for part in (name, descriptive) if part)
    has_business_identity = bool(name and category)
    usable_website = evidence["usable_website"]
    has_direct_contact = bool(evidence["phone"] or evidence["email"])
    has_location = bool(evidence["address"])
    conflicted = identity_conflict or location_conflict

    employment_reasons: list[str] = []
    # Category/description is business evidence. A keyword in the name or source
    # query alone can never produce TARGET.
    if has_business_identity and _contains(descriptive, policy.employment_strong_terms):
        employment_relevance = "TARGET"
        employment_reasons.append("strong_software_or_it_business_evidence")
    elif _contains(descriptive, policy.employment_possible_terms) or _contains(
        name, policy.employment_strong_terms + policy.employment_possible_terms,
    ):
        employment_relevance = "POSSIBLE"
        employment_reasons.append("adjacent_or_name_only_technical_evidence")
    elif _contains(all_text, policy.definite_noise_terms) or (category and name):
        employment_relevance = "NOISE"
        employment_reasons.append("no_software_or_it_business_evidence")
    else:
        employment_relevance = "UNKNOWN"
        employment_reasons.append("insufficient_business_evidence")

    if employment_relevance == "NOISE":
        employment_eligibility = "EXCLUDED"
    elif employment_relevance != "TARGET":
        employment_eligibility = "REVIEW"
    elif conflicted:
        employment_eligibility = "REVIEW"
        employment_reasons.append("conflicting_identity_or_location_evidence")
    elif not usable_website:
        employment_eligibility = "REVIEW"
        employment_reasons.append("usable_official_website_not_available")
    elif not has_location:
        employment_eligibility = "REVIEW"
        employment_reasons.append("location_evidence_not_available")
    else:
        employment_eligibility = "ELIGIBLE"
        employment_reasons.append("strong_relevance_website_and_location")

    mission_reasons: list[str] = []
    strong_mission = has_business_identity and _contains(
        descriptive, policy.mission_strong_terms,
    )
    possible_mission = has_business_identity and _contains(
        descriptive, policy.mission_possible_terms,
    )
    # Development capability explicitly attached to a marketing category is a
    # partnership/overflow fit, not a claim that the company currently needs help.
    marketing_with_development = (
        "marketing agency" in category or "advertising agency" in category
    ) and _contains(description, policy.mission_strong_terms)
    if strong_mission or marketing_with_development:
        mission_relevance = "TARGET"
        mission_reasons.append("explicit_software_service_or_partnership_fit")
    elif possible_mission:
        mission_relevance = "POSSIBLE"
        mission_reasons.append("industry_may_have_service_fit_but_need_is_unproven")
    elif _contains(all_text, policy.definite_noise_terms):
        mission_relevance = "NOISE"
        mission_reasons.append("no_plausible_commercial_service_fit")
    elif has_business_identity:
        mission_relevance = "POSSIBLE"
        mission_reasons.append("commercial_business_with_unproven_service_fit")
    else:
        mission_relevance = "UNKNOWN"
        mission_reasons.append("insufficient_business_evidence")

    if mission_relevance == "NOISE":
        mission_eligibility = "EXCLUDED"
    elif mission_relevance != "TARGET":
        mission_eligibility = "REVIEW"
    elif conflicted:
        mission_eligibility = "REVIEW"
        mission_reasons.append("conflicting_identity_or_location_evidence")
    elif not usable_website:
        mission_eligibility = "REVIEW"
        mission_reasons.append("usable_website_not_available")
    elif not has_direct_contact:
        mission_eligibility = "REVIEW"
        mission_reasons.append("direct_contact_not_available")
    elif not has_location:
        mission_eligibility = "REVIEW"
        mission_reasons.append("location_evidence_not_available")
    else:
        mission_eligibility = "ELIGIBLE"
        mission_reasons.append("explicit_fit_with_contact_and_evidence_quality")

    return QualificationResult(
        employment_relevance=employment_relevance,
        mission_relevance=mission_relevance,
        employment_eligibility=employment_eligibility,
        mission_eligibility=mission_eligibility,
        employment_reasons=tuple(employment_reasons),
        mission_reasons=tuple(mission_reasons),
        evidence=evidence,
    )


def assess_run(
    database: str | Path,
    run_id: str,
    *,
    policy: QualificationPolicy = DEFAULT_QUALIFICATION_POLICY,
    assessed_at: str | None = None,
) -> dict:
    """Idempotently persist qualifications for resolved Google Maps decisions."""
    assessed_at = assessed_at or datetime.now(timezone.utc).isoformat()
    connection = open_registry(database)
    inserted = reused = skipped = 0
    try:
        if connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone() is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        records = connection.execute(
            """SELECT decision.*, observation.source_system,
                      observation.raw_payload_json
                 FROM discovery_run_decisions decision
                 JOIN discovery_observations observation
                   ON observation.observation_id=decision.observation_id
                WHERE decision.run_id=? AND observation.source_system='GOOGLE_MAPS'
                ORDER BY decision.decision_id""",
            (run_id,),
        ).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            if not record["company_id"]:
                skipped += 1
                continue
            raw = json.loads(record["raw_payload_json"])
            raw = raw if isinstance(raw, dict) else {}
            conflicts = json.loads(record["conflicts_json"])
            identity_conflict = bool(conflicts) or bool(record["requires_review"])
            location_conflict = clean_text(raw.get("geography_validation")).upper() == "MISMATCH"
            result = policy.assess(
                raw,
                identity_conflict=identity_conflict,
                location_conflict=location_conflict,
            )
            evidence_json = canonical_json(result.evidence)
            assessment_id = stable_id(
                "qualification", f"{record['decision_id']}|{policy.version}",
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO qualification_assessments
                   (assessment_id, decision_id, company_id, policy_version,
                    evidence_hash, evidence_json, employment_relevance,
                    mission_relevance, employment_eligibility,
                    mission_eligibility, employment_reasons_json,
                    mission_reasons_json, assessed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    assessment_id, record["decision_id"], record["company_id"],
                    policy.version, payload_hash(result.evidence), evidence_json,
                    result.employment_relevance, result.mission_relevance,
                    result.employment_eligibility, result.mission_eligibility,
                    canonical_json(result.employment_reasons),
                    canonical_json(result.mission_reasons), assessed_at, assessed_at,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                reused += 1
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "run_id": run_id,
        "policy_version": policy.version,
        "inserted": inserted,
        "reused": reused,
        "skipped_unresolved": skipped,
    }
