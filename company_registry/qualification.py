"""Deterministic, dual-purpose qualification of committed Maps companies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses
import json
from pathlib import Path
import re
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
EMAIL_DECISIONS = {"ACCEPT", "REVIEW", "REJECT"}
EMAIL_SOURCE_FIELDS = ("email", "emails", "email_address", "site_email")
MAX_EMAIL_FIELD_CHARACTERS = 4096
MAX_EMAIL_CANDIDATES_PER_FIELD = 50
MAX_EMAIL_ADDRESS_CHARACTERS = 320
EMAIL_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}",
    flags=re.IGNORECASE,
)
EMAIL_UNAVAILABLE_VALUES = {
    "", "-", "n/a", "na", "none", "not available", "null", "unknown",
}
EMAIL_PLACEHOLDER_DOMAINS = {
    "company.com", "domain.com", "email.com", "example.com", "example.net",
    "example.org", "yourcompany.com",
}
EMAIL_PLACEHOLDER_LOCAL_PARTS = {
    "email", "foulen", "lastname", "name", "test", "user", "yourname",
}
EMAIL_ASSET_EXTENSIONS = {
    "css", "gif", "jpeg", "jpg", "js", "pdf", "png", "svg", "webp",
}
EMAIL_AUTOMATED_LOCAL_PARTS = {
    "bounce", "do-not-reply", "donotreply", "mailer-daemon", "no-reply",
    "noreply", "postmaster",
}
EMAIL_TELEMETRY_LOCAL_PARTS = {
    "error", "errors", "monitoring", "sentry", "telemetry",
}
EMAIL_TELEMETRY_DOMAINS = {
    "sentry.io", "sentry.wixpress.com", "sentry-next.wixpress.com",
}
EMAIL_BUSINESS_LOCAL_PARTS = {
    "admin", "careers", "commercial", "contact", "hello", "hr", "info",
    "jobs", "office", "recruiting", "recruitment", "sales", "support",
}


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
class EmailValidation:
    """Auditable deterministic classification of one supplied address."""

    original: str
    normalized: str
    source_field: str
    decision: str
    reason_code: str


@dataclass(frozen=True)
class QualificationPolicy:
    """Versioned policy configuration; changing rules requires a new version."""

    version: str = "maps-dual-purpose-v3-business-relevance"
    employment_target_categories: tuple[str, ...] = (
        "computer consultant", "computer networking service",
        "computer security service", "cybersecurity service", "it consultant",
        "software company", "software development company", "web designer",
        "web hosting company", "website designer",
    )
    mission_target_categories: tuple[str, ...] = (
        "computer consultant", "computer networking service",
        "computer security service", "cybersecurity service", "it consultant",
        "software company", "software development company", "web designer",
        "web hosting company", "website designer",
    )
    unrelated_organization_categories: tuple[str, ...] = (
        "council", "government office", "government organization",
        "software training institute", "training center", "university",
    )
    employment_strong_terms: tuple[str, ...] = (
        "software company", "software development", "software developer",
        "application development", "app development", "backend development",
        "back-end development", "business software", "cloud computing",
        "cloud infrastructure", "cloud services", "cyber security services",
        "cybersecurity services", "devops services", "erp software",
        "enterprise resource planning", "frontend development",
        "front-end development", "information technology company",
        "it consulting", "it consultant", "it services",
        "managed it services", "mobile app development", "software engineering",
        "system integration", "systems integration", "web development",
        "website development",
    )
    employment_possible_terms: tuple[str, ...] = (
        "computer support", "computer service", "computer repair",
        "software retailer", "software store", "computer store",
        "digital agency", "web agency", "internet service provider",
    )
    mission_strong_terms: tuple[str, ...] = (
        "software development", "application development", "app development",
        "api development", "backend development", "back-end development",
        "business process automation", "business software", "cloud computing",
        "cloud infrastructure", "cloud services", "custom automation",
        "cyber security services", "cybersecurity services", "devops services",
        "erp software", "enterprise resource planning", "frontend development",
        "front-end development", "it consulting", "it services",
        "managed it services", "mobile app development", "software engineering",
        "system integration", "systems integration", "web development",
        "website development",
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
    employment_opportunity_terms: tuple[str, ...] = (
        "job opening", "job vacancy", "join our team", "now hiring",
        "we are hiring",
    )
    mission_opportunity_terms: tuple[str, ...] = (
        "freelance opportunity", "looking for a developer",
        "looking for technology partner", "request for proposal",
        "seeking a developer", "seeking technology partner",
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
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    padded = f" {normalized} "
    return any(
        f" {re.sub(r'[^a-z0-9]+', ' ', term.casefold()).strip()} " in padded
        for term in terms
    )


def _category_in(category: str, categories: Iterable[str]) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", category.casefold()).strip()
    return normalized in {
        re.sub(r"[^a-z0-9]+", " ", item.casefold()).strip()
        for item in categories
    }


def _first(row: dict, *fields: str) -> str:
    for field in fields:
        value = clean_text(row.get(field))
        if value:
            return value
    return ""


def validate_contact_email(candidate: object, source_field: str) -> EmailValidation:
    """Classify one bounded candidate without claiming mailbox deliverability."""
    original = clean_text(candidate)
    normalized = original.casefold()
    if normalized in EMAIL_UNAVAILABLE_VALUES:
        return EmailValidation(
            original, "", source_field, "REJECT", "empty_or_unavailable",
        )
    if len(original) > MAX_EMAIL_ADDRESS_CHARACTERS:
        return EmailValidation(
            original, "", source_field, "REJECT", "address_too_long",
        )
    if not EMAIL_PATTERN.fullmatch(original):
        return EmailValidation(
            original, "", source_field, "REJECT", "malformed_address",
        )

    local_part, domain = normalized.rsplit("@", 1)
    if (
        len(local_part) > 64
        or len(domain) > 253
        or ".." in normalized
        or local_part.startswith(".")
        or local_part.endswith(".")
    ):
        return EmailValidation(
            original, "", source_field, "REJECT", "malformed_address",
        )
    if (
        domain in EMAIL_PLACEHOLDER_DOMAINS
        or local_part in EMAIL_PLACEHOLDER_LOCAL_PARTS
    ):
        return EmailValidation(
            original, normalized, source_field, "REJECT", "placeholder_address",
        )
    if domain.rsplit(".", 1)[-1] in EMAIL_ASSET_EXTENSIONS:
        return EmailValidation(
            original, normalized, source_field, "REJECT", "asset_address",
        )
    compact_local = local_part.replace(".", "").replace("_", "").replace("-", "")
    if local_part in EMAIL_AUTOMATED_LOCAL_PARTS or compact_local in {
        "donotreply", "mailerdaemon", "noreply",
    }:
        return EmailValidation(
            original, normalized, source_field, "REJECT", "automated_sender",
        )
    if (
        local_part in EMAIL_TELEMETRY_LOCAL_PARTS
        or compact_local in EMAIL_TELEMETRY_LOCAL_PARTS
        or domain in EMAIL_TELEMETRY_DOMAINS
        or domain.startswith("sentry.")
        or ".sentry." in domain
        or domain.endswith(".sentry.io")
    ):
        return EmailValidation(
            original, normalized, source_field, "REJECT", "telemetry_address",
        )
    if local_part in EMAIL_BUSINESS_LOCAL_PARTS:
        return EmailValidation(
            original, normalized, source_field, "ACCEPT", "business_role_address",
        )
    return EmailValidation(
        original, normalized, source_field, "REVIEW", "uncertain_local_part",
    )


def _email_values(value: object) -> list[str]:
    """Return bounded candidates from a scalar or existing multi-value field."""
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)[:MAX_EMAIL_CANDIDATES_PER_FIELD]
    else:
        raw_values = [value]

    candidates: list[str] = []
    for raw_value in raw_values:
        text = clean_text(raw_value)
        if not text:
            continue
        text = text[:MAX_EMAIL_FIELD_CHARACTERS]
        for segment in re.split(r"[;\r\n]+", text):
            segment = clean_text(segment)
            if not segment:
                continue
            parsed = getaddresses([segment])[:MAX_EMAIL_CANDIDATES_PER_FIELD]
            addresses = [address for _, address in parsed if clean_text(address)]
            candidates.extend(addresses or [segment])
            if len(candidates) >= MAX_EMAIL_CANDIDATES_PER_FIELD:
                break
        if len(candidates) >= MAX_EMAIL_CANDIDATES_PER_FIELD:
            break
    return candidates[:MAX_EMAIL_CANDIDATES_PER_FIELD]


def _email_evidence(row: dict) -> tuple[str, list[dict]]:
    assessments: list[EmailValidation] = []
    seen: set[tuple[str, str]] = set()
    for source_field in EMAIL_SOURCE_FIELDS:
        for candidate in _email_values(row.get(source_field)):
            assessment = validate_contact_email(candidate, source_field)
            key = (
                assessment.normalized or assessment.original.casefold(),
                source_field,
            )
            if key in seen:
                continue
            seen.add(key)
            assessments.append(assessment)
    accepted = next(
        (assessment.normalized for assessment in assessments
         if assessment.decision == "ACCEPT"),
        "",
    )
    audit = [
        {
            "original": assessment.original,
            "normalized": assessment.normalized,
            "source_field": assessment.source_field,
            "decision": assessment.decision,
            "reason_code": assessment.reason_code,
        }
        for assessment in assessments
    ]
    return accepted, audit


def _evidence_snapshot(
    row: dict, *, identity_conflict: bool, location_conflict: bool,
) -> dict:
    website = _first(row, "webpage", "website", "website_url")
    domain = website if is_trustworthy_domain(website) else ""
    email, email_candidates = _email_evidence(row)
    return {
        "name": _first(row, "title", "company_name", "name"),
        "category": _first(row, "category", "categories"),
        "description": _first(
            row, "about_desc", "business_description", "description", "about",
        ),
        "website": website,
        "usable_website": bool(domain),
        "phone": _first(row, "phone_number", "phone"),
        "email": email,
        "email_candidates": email_candidates,
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
    strong_employment_category = _category_in(
        category, policy.employment_target_categories,
    )
    technical_description = _contains(
        description, policy.employment_strong_terms,
    )
    unrelated_organization = _category_in(
        category, policy.unrelated_organization_categories,
    )
    if unrelated_organization:
        employment_relevance = "NOISE"
        employment_reasons.append("unrelated_organization_category")
    elif has_business_identity and (
        strong_employment_category or technical_description
    ):
        employment_relevance = "TARGET"
        if strong_employment_category:
            employment_reasons.append("target_technical_business_category")
        else:
            employment_reasons.append("explicit_technical_services_description")
    elif _contains(descriptive, policy.employment_possible_terms) or _contains(
        name, policy.employment_strong_terms + policy.employment_possible_terms,
    ):
        employment_relevance = "POSSIBLE"
        employment_reasons.append("adjacent_or_name_only_technical_evidence")
    elif (
        _contains(all_text, policy.definite_noise_terms)
        or (category and name)
    ):
        employment_relevance = "NOISE"
        employment_reasons.append("no_software_or_it_business_evidence")
    else:
        employment_relevance = "UNKNOWN"
        employment_reasons.append("insufficient_business_evidence")

    if _contains(description, policy.employment_opportunity_terms):
        employment_reasons.append("explicit_hiring_opportunity_evidence")
    else:
        employment_reasons.append("current_hiring_not_established")

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
    strong_mission_category = _category_in(
        category, policy.mission_target_categories,
    )
    strong_mission_description = _contains(
        description, policy.mission_strong_terms,
    )
    strong_mission = has_business_identity and (
        strong_mission_category or strong_mission_description
    )
    possible_mission = has_business_identity and _contains(
        descriptive, policy.mission_possible_terms,
    )
    # Development capability is partnership/overflow fit, not a claim that the
    # company currently needs help.
    if unrelated_organization:
        mission_relevance = "NOISE"
        mission_reasons.append("unrelated_organization_category")
    elif strong_mission:
        mission_relevance = "TARGET"
        if strong_mission_category:
            mission_reasons.append("target_technical_service_category")
        else:
            mission_reasons.append("explicit_technical_services_description")
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

    if _contains(description, policy.mission_opportunity_terms):
        mission_reasons.append("explicit_mission_opportunity_evidence")
    else:
        mission_reasons.append("active_mission_not_established")

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
