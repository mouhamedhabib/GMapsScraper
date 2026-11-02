"""Read-only, non-production reporting for registry email coverage."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from company_registry.normalization import canonical_json
from company_registry.qualification import (
    DEFAULT_QUALIFICATION_POLICY,
    classify_email_fields,
)
from company_registry.storage import open_registry_readonly


DEFAULT_EMAIL_REPORT_DIRECTORY = Path("data/reports/email_coverage")
SOURCE_STATUSES = ("MISSING", "PLACEHOLDER", "AVAILABLE", "REVIEW", "REJECTED")
EMAIL_DECISION_VALUES = ("ACCEPT", "REVIEW", "REJECT")
IMMUTABLE_EXPORT_PATTERNS = (
    "new_companies_*.csv",
    "qualified_*_leads_*.csv",
    "review_*_queue_*.csv",
    "*.manifest.json",
)


def _safe_run_token(run_id: str) -> str:
    token = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in run_id
    )
    return token or "run"


def _require_separate_report_directory(output_directory: Path) -> None:
    """Refuse to place reports beside existing immutable export artifacts."""
    if not output_directory.is_dir():
        return
    if any(
        next(output_directory.glob(pattern), None) is not None
        for pattern in IMMUTABLE_EXPORT_PATTERNS
    ):
        raise ValueError(
            "Email coverage reports require a directory separate from "
            "immutable CSV exports and manifests"
        )


def _stored_candidates(evidence_json: object) -> list[dict]:
    if not evidence_json:
        return []
    try:
        evidence = json.loads(str(evidence_json))
    except (TypeError, ValueError):
        return []
    candidates = (
        evidence.get("email_candidates", [])
        if isinstance(evidence, dict)
        else []
    )
    return [item for item in candidates if isinstance(item, dict)]


def _summarize(records: list[dict]) -> dict:
    source_status_counts = {status: 0 for status in SOURCE_STATUSES}
    decision_counts = {decision: 0 for decision in EMAIL_DECISION_VALUES}
    records_with_decision = {decision: 0 for decision in EMAIL_DECISION_VALUES}
    available = 0
    assessed = 0

    for record in records:
        source = classify_email_fields(record["raw"])
        source_status_counts[source.status] += 1
        candidates = record["candidates"]
        if record["has_assessment"]:
            assessed += 1
        record_decisions = {
            item.get("decision") for item in candidates
            if item.get("decision") in EMAIL_DECISION_VALUES
        }
        for candidate in candidates:
            decision = candidate.get("decision")
            if decision in decision_counts:
                decision_counts[decision] += 1
        for decision in record_decisions:
            records_with_decision[decision] += 1

        # Stored policy evidence is authoritative when present. For an
        # unassessed record, the same deterministic classifier supplies only
        # the coverage result and does not fabricate historical assessment data.
        if candidates:
            is_available = bool(record_decisions & {"ACCEPT", "REVIEW"})
        else:
            is_available = source.available
        available += int(is_available)

    total = len(records)
    return {
        "records": total,
        "available": available,
        "unavailable": total - available,
        "assessed_records": assessed,
        "unassessed_records": total - assessed,
        "source_status_counts": source_status_counts,
        "decision_counts": decision_counts,
        "records_with_decision": records_with_decision,
    }


def _report_records(
    database: Path, run_id: str, policy_version: str,
) -> list[dict]:
    connection = open_registry_readonly(database)
    try:
        if connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone() is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        rows = connection.execute(
            """SELECT decision.company_id, decision.decision_id,
                      observation.raw_payload_json,
                      assessment.assessment_id, assessment.evidence_json,
                      assessment.employment_eligibility,
                      assessment.mission_eligibility
                 FROM discovery_run_decisions decision
                 JOIN discovery_observations observation
                   ON observation.observation_id=decision.observation_id
            LEFT JOIN qualification_assessments assessment
                   ON assessment.decision_id=decision.decision_id
                  AND assessment.policy_version=?
                WHERE decision.run_id=?
                  AND decision.classification='NEW'
                  AND decision.resolution_action='CREATE_COMPANY'
                  AND decision.requires_review=0
                ORDER BY decision.company_id, decision.decision_id""",
            (policy_version, run_id),
        ).fetchall()
    finally:
        connection.close()

    records = []
    seen_companies = set()
    for row in rows:
        if row["company_id"] in seen_companies:
            continue
        seen_companies.add(row["company_id"])
        try:
            raw = json.loads(row["raw_payload_json"])
        except (TypeError, ValueError):
            raw = {}
        records.append({
            "raw": raw if isinstance(raw, dict) else {},
            "candidates": _stored_candidates(row["evidence_json"]),
            "has_assessment": bool(row["assessment_id"]),
            "employment_eligibility": row["employment_eligibility"],
            "mission_eligibility": row["mission_eligibility"],
        })
    return records


def build_email_coverage_report(
    database: str | Path,
    run_id: str,
    output_directory: str | Path = DEFAULT_EMAIL_REPORT_DIRECTORY,
    *,
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
) -> dict:
    """Write a deterministic report without changing registry or export data."""
    database = Path(database)
    output_directory = Path(output_directory)
    _require_separate_report_directory(output_directory)
    records = _report_records(database, run_id, policy_version)
    report = {
        "format": "company-registry-email-coverage-v1",
        "run_id": run_id,
        "policy_version": policy_version,
        "semantics": {
            "available": "record has at least one ACCEPT or REVIEW candidate",
            "unavailable": "record is missing, placeholder-only, or REJECT-only",
            "decision_counts": "stored candidate decisions; not reassessments",
            "production_exports_modified": False,
        },
        "populations": {
            "discovery_new": _summarize(records),
            "employment_eligible": _summarize([
                item for item in records
                if item["employment_eligibility"] == "ELIGIBLE"
            ]),
            "mission_eligible": _summarize([
                item for item in records
                if item["mission_eligibility"] == "ELIGIBLE"
            ]),
        },
    }
    payload = (canonical_json(report) + "\n").encode("utf-8")
    digest = sha256(payload).hexdigest()
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"email_coverage_{_safe_run_token(run_id)}.json"
    if path.is_file() and path.read_bytes() == payload:
        return {"report": path, "sha256": digest, "reused": True, **report}

    temporary = None
    try:
        with NamedTemporaryFile(
            "wb", dir=output_directory, prefix=".email-coverage.",
            suffix=".tmp", delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"report": path, "sha256": digest, "reused": False, **report}
