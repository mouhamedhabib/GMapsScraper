"""Recoverable deterministic exports derived from committed registry decisions."""

from __future__ import annotations

from csv import DictReader, DictWriter
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable

from company_registry.normalization import canonical_json, clean_text
from company_registry.qualification import DEFAULT_QUALIFICATION_POLICY
from company_registry.schema import SCHEMA_VERSION
from company_registry.storage import open_registry


FailureInjector = Callable[[str], None]
REGISTRY_EXPORT_FIELDS = (
    "registry_decision_id", "registry_run_id", "company_id", "branch_id",
    "source_system", "source_record_key",
    "classification", "resolution_action", "observed_at", "resolution_reason",
    "resolver_version",
)
QUALIFIED_EXPORT_FIELDS = (
    "qualification_assessment_id", "qualification_policy_version", "purpose",
    "company_id", "registry_decision_id", "registry_run_id", "branch_id",
    "source_system", "source_record_key", "relevance", "eligibility",
    "reasons_json", "evidence_hash", "assessed_at", "name", "category",
    "description", "website", "phone", "email", "address", "country", "city",
)
REVIEW_EXPORT_FIELDS = (
    "qualification_assessment_id", "qualification_policy_version", "purpose",
    "company_id", "registry_decision_id", "source_run_id", "branch_id",
    "source_system", "source_record_key", "review_priority", "relevance",
    "eligibility", "classification_reason_codes", "missing_evidence",
    "evidence_hash", "assessed_at", "company_name", "country", "address",
    "website", "accepted_contact_email", "phone", "google_maps_url",
    "business_category",
)
REVIEW_PRIORITIES = ("HIGH", "MEDIUM", "LOW")
_UNAVAILABLE_EXPORT_VALUES = {
    "", "-", "n/a", "na", "none", "not available", "null", "unknown",
}


def _safe_run_token(run_id: str) -> str:
    token = "".join(character if character.isalnum() or character in "-_" else "_"
                    for character in run_id)
    return token or "run"


def _new_company_rows(database: Path, run_id: str) -> tuple[list[dict], list[str]]:
    connection = open_registry(database)
    try:
        if connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone() is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        records = connection.execute(
            """SELECT decision.*, observation.source_system,
                      observation.source_record_key, observation.raw_payload_json
                 FROM discovery_run_decisions decision
                 JOIN discovery_observations observation
                   ON observation.observation_id=decision.observation_id
                WHERE decision.run_id=? AND decision.classification='NEW'
                  AND decision.resolution_action='CREATE_COMPANY'
                  AND decision.requires_review=0
                ORDER BY decision.decision_id""",
            (run_id,),
        ).fetchall()
    finally:
        connection.close()

    rows = []
    raw_fields = set()
    for record in records:
        raw = json.loads(record["raw_payload_json"])
        raw = raw if isinstance(raw, dict) else {"raw_payload": raw}
        raw_fields.update(str(key) for key in raw)
        row = {
            "registry_decision_id": record["decision_id"],
            "registry_run_id": record["run_id"],
            "company_id": record["company_id"] or "",
            "branch_id": record["branch_id"] or "",
            "source_system": record["source_system"],
            "source_record_key": record["source_record_key"],
            "classification": record["classification"],
            "resolution_action": record["resolution_action"],
            "observed_at": record["observed_at"],
            "resolution_reason": record["resolution_reason"],
            "resolver_version": record["resolver_version"],
        }
        row.update({str(key): value for key, value in raw.items()
                    if str(key) not in REGISTRY_EXPORT_FIELDS})
        rows.append(row)
    return rows, sorted(raw_fields - set(REGISTRY_EXPORT_FIELDS))


def _stage_csv(
    directory: Path, fieldnames: list[str], rows: list[dict], *, prefix: str = ".new-companies.",
) -> Path:
    with NamedTemporaryFile(
        "w", newline="", encoding="utf-8-sig", dir=directory,
        prefix=prefix, suffix=".tmp", delete=False,
    ) as handle:
        writer = DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _stage_manifest(
    directory: Path, manifest: dict, *, prefix: str = ".new-companies-manifest.",
) -> Path:
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory,
        prefix=prefix, suffix=".tmp", delete=False,
    ) as handle:
        handle.write(canonical_json(manifest) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _fsync_directory(directory: Path) -> None:
    """Make a completed rename durable where the host filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(descriptor)


def export_new_companies(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    *,
    failure_injector: FailureInjector | None = None,
    verified_maps_publication: bool = False,
) -> dict:
    """Atomically publish a rebuildable NEW-company CSV and commit manifest."""
    database = Path(database)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    rows, raw_fields = _new_company_rows(database, run_id)
    fields = [*REGISTRY_EXPORT_FIELDS, *raw_fields]
    token = _safe_run_token(run_id)
    csv_path = output_directory / f"new_companies_{token}.csv"
    manifest_path = output_directory / f"new_companies_{token}.manifest.json"
    staged_csv = staged_manifest = None
    try:
        staged_csv = _stage_csv(output_directory, fields, rows)
        csv_hash = sha256(staged_csv.read_bytes()).hexdigest()
        manifest = {
            "format": "company-registry-new-companies-v1",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "row_count": len(rows),
            "decision_ids": [row["registry_decision_id"] for row in rows],
        }
        if verified_maps_publication:
            manifest["company_ids"] = [row["company_id"] for row in rows]
        staged_manifest = _stage_manifest(output_directory, manifest)
        if failure_injector:
            failure_injector("before_csv_publish")
        os.replace(staged_csv, csv_path)
        staged_csv = None
        if verified_maps_publication:
            _fsync_directory(output_directory)
        if failure_injector:
            failure_injector("before_manifest_publish")
        # The manifest is the publication commit marker. A missing or mismatched
        # manifest makes a partially published CSV invalid and safe to rebuild.
        os.replace(staged_manifest, manifest_path)
        staged_manifest = None
        if verified_maps_publication:
            _fsync_directory(output_directory)
        return {
            "csv": csv_path,
            "manifest": manifest_path,
            "rows": len(rows),
            "sha256": csv_hash,
            "decision_ids": manifest["decision_ids"],
            "company_ids": [row["company_id"] for row in rows],
        }
    finally:
        for temporary in (staged_csv, staged_manifest):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _qualified_rows(
    database: Path, run_id: str, purpose: str, policy_version: str,
) -> list[dict]:
    if purpose not in {"employment", "mission"}:
        raise ValueError("Qualified export purpose must be 'employment' or 'mission'")
    relevance_column = f"{purpose}_relevance"
    eligibility_column = f"{purpose}_eligibility"
    reasons_column = f"{purpose}_reasons_json"
    connection = open_registry(database)
    try:
        if connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone() is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        records = connection.execute(
            f"""SELECT assessment.*, decision.run_id, decision.branch_id,
                       observation.source_system, observation.source_record_key
                  FROM qualification_assessments assessment
                  JOIN discovery_run_decisions decision
                    ON decision.decision_id=assessment.decision_id
                  JOIN discovery_observations observation
                    ON observation.observation_id=decision.observation_id
                 WHERE decision.run_id=? AND assessment.policy_version=?
                   AND decision.classification='NEW'
                   AND decision.resolution_action='CREATE_COMPANY'
                   AND decision.requires_review=0
                   AND assessment.{eligibility_column}='ELIGIBLE'
                 ORDER BY assessment.company_id, assessment.assessment_id""",
            (run_id, policy_version),
        ).fetchall()
    finally:
        connection.close()

    rows = []
    seen_companies = set()
    for record in records:
        if record["company_id"] in seen_companies:
            continue
        seen_companies.add(record["company_id"])
        evidence = json.loads(record["evidence_json"])
        rows.append({
            "qualification_assessment_id": record["assessment_id"],
            "qualification_policy_version": record["policy_version"],
            "purpose": purpose,
            "company_id": record["company_id"],
            "registry_decision_id": record["decision_id"],
            "registry_run_id": record["run_id"],
            "branch_id": record["branch_id"] or "",
            "source_system": record["source_system"],
            "source_record_key": record["source_record_key"],
            "relevance": record[relevance_column],
            "eligibility": record[eligibility_column],
            "reasons_json": record[reasons_column],
            "evidence_hash": record["evidence_hash"],
            "assessed_at": record["assessed_at"],
            **{field: evidence.get(field, "") for field in (
                "name", "category", "description", "website", "phone", "email",
                "address", "country", "city",
            )},
        })
    return rows


def _available_text(value: object) -> str:
    text = clean_text(value)
    if text.casefold() in _UNAVAILABLE_EXPORT_VALUES:
        return ""
    return text


def _review_priority(relevance: str) -> str:
    """Map stored relevance to a deterministic human-review priority."""
    if relevance == "TARGET":
        return "HIGH"
    if relevance == "POSSIBLE":
        return "MEDIUM"
    return "LOW"


def _review_missing_evidence(
    purpose: str, relevance: str, evidence: dict,
) -> tuple[str, ...]:
    """Describe evidence gaps without changing the qualification result."""
    missing: list[str] = []
    if relevance != "TARGET":
        missing.append("target_relevance_not_established")
    if not evidence.get("usable_website"):
        missing.append("usable_website_not_available")
    if purpose == "mission" and not (
        _available_text(evidence.get("phone"))
        or _available_text(evidence.get("email"))
    ):
        missing.append("direct_contact_not_available")
    if not _available_text(evidence.get("address")):
        missing.append("location_evidence_not_available")
    if evidence.get("identity_conflict"):
        missing.append("identity_conflict_requires_resolution")
    if evidence.get("location_conflict"):
        missing.append("location_conflict_requires_resolution")
    return tuple(missing or ("additional_verification_required",))


def _review_rows(
    database: Path, run_id: str, purpose: str, policy_version: str,
) -> list[dict]:
    if purpose not in {"employment", "mission"}:
        raise ValueError("Review export purpose must be 'employment' or 'mission'")
    relevance_column = f"{purpose}_relevance"
    eligibility_column = f"{purpose}_eligibility"
    reasons_column = f"{purpose}_reasons_json"
    connection = open_registry(database)
    try:
        if connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,),
        ).fetchone() is None:
            raise LookupError(f"Unknown discovery run: {run_id}")
        records = connection.execute(
            f"""SELECT assessment.*, decision.run_id, decision.branch_id,
                       observation.source_system, observation.source_record_key,
                       observation.raw_payload_json
                  FROM qualification_assessments assessment
                  JOIN discovery_run_decisions decision
                    ON decision.decision_id=assessment.decision_id
                  JOIN discovery_observations observation
                    ON observation.observation_id=decision.observation_id
                 WHERE decision.run_id=? AND assessment.policy_version=?
                   AND decision.classification='NEW'
                   AND decision.resolution_action='CREATE_COMPANY'
                   AND decision.requires_review=0
                   AND assessment.{eligibility_column}='REVIEW'""",
            (run_id, policy_version),
        ).fetchall()
    finally:
        connection.close()

    rows = []
    for record in records:
        evidence = json.loads(record["evidence_json"])
        evidence = evidence if isinstance(evidence, dict) else {}
        raw = json.loads(record["raw_payload_json"])
        raw = raw if isinstance(raw, dict) else {}
        reasons = json.loads(record[reasons_column])
        reasons = reasons if isinstance(reasons, list) else []
        relevance = record[relevance_column]
        google_maps_url = next(
            (
                value for value in (
                    _available_text(raw.get("map_link")),
                    _available_text(raw.get("google_maps_url")),
                    _available_text(raw.get("maps_url")),
                ) if value
            ),
            "",
        )
        rows.append({
            "qualification_assessment_id": record["assessment_id"],
            "qualification_policy_version": record["policy_version"],
            "purpose": purpose,
            "company_id": record["company_id"],
            "registry_decision_id": record["decision_id"],
            "source_run_id": record["run_id"],
            "branch_id": record["branch_id"] or "",
            "source_system": record["source_system"],
            "source_record_key": record["source_record_key"],
            "review_priority": _review_priority(relevance),
            "relevance": relevance,
            "eligibility": record[eligibility_column],
            "classification_reason_codes": canonical_json(reasons),
            "missing_evidence": canonical_json(_review_missing_evidence(
                purpose, relevance, evidence,
            )),
            "evidence_hash": record["evidence_hash"],
            "assessed_at": record["assessed_at"],
            "company_name": _available_text(evidence.get("name")),
            "country": _available_text(evidence.get("country")),
            "address": _available_text(evidence.get("address")),
            "website": _available_text(evidence.get("website")),
            "accepted_contact_email": _available_text(evidence.get("email")),
            "phone": _available_text(evidence.get("phone")),
            "google_maps_url": google_maps_url,
            "business_category": _available_text(evidence.get("category")),
        })

    priority_order = {priority: index for index, priority in enumerate(
        REVIEW_PRIORITIES
    )}
    rows.sort(key=lambda row: (
        priority_order[row["review_priority"]],
        row["company_name"].casefold(),
        row["company_id"],
        row["qualification_assessment_id"],
    ))
    unique_rows = []
    seen_companies = set()
    for row in rows:
        if row["company_id"] in seen_companies:
            continue
        seen_companies.add(row["company_id"])
        unique_rows.append(row)
    return unique_rows


def export_qualified_leads(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    *,
    purpose: str,
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
    failure_injector: FailureInjector | None = None,
) -> dict:
    """Publish a company-deduplicated ELIGIBLE-only purpose export."""
    database = Path(database)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    rows = _qualified_rows(database, run_id, purpose, policy_version)
    token = _safe_run_token(run_id)
    csv_path = output_directory / f"qualified_{purpose}_leads_{token}.csv"
    manifest_path = output_directory / f"qualified_{purpose}_leads_{token}.manifest.json"
    staged_csv = staged_manifest = None
    try:
        staged_csv = _stage_csv(
            output_directory, list(QUALIFIED_EXPORT_FIELDS), rows,
            prefix=f".qualified-{purpose}.",
        )
        csv_hash = sha256(staged_csv.read_bytes()).hexdigest()
        manifest = {
            "format": "company-registry-qualified-leads-v1",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "purpose": purpose,
            "policy_version": policy_version,
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "row_count": len(rows),
            "assessment_ids": [row["qualification_assessment_id"] for row in rows],
            "company_ids": [row["company_id"] for row in rows],
        }
        staged_manifest = _stage_manifest(
            output_directory, manifest,
            prefix=f".qualified-{purpose}-manifest.",
        )
        if failure_injector:
            failure_injector("before_csv_publish")
        os.replace(staged_csv, csv_path)
        staged_csv = None
        _fsync_directory(output_directory)
        if failure_injector:
            failure_injector("before_manifest_publish")
        os.replace(staged_manifest, manifest_path)
        staged_manifest = None
        _fsync_directory(output_directory)
        return {
            "csv": csv_path,
            "manifest": manifest_path,
            "rows": len(rows),
            "sha256": csv_hash,
            "assessment_ids": manifest["assessment_ids"],
            "company_ids": manifest["company_ids"],
        }
    finally:
        for temporary in (staged_csv, staged_manifest):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def export_review_queue(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    *,
    purpose: str,
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
    failure_injector: FailureInjector | None = None,
) -> dict:
    """Publish one deterministic, company-deduplicated REVIEW queue."""
    database = Path(database)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    rows = _review_rows(database, run_id, purpose, policy_version)
    token = _safe_run_token(run_id)
    csv_path = output_directory / f"review_{purpose}_queue_{token}.csv"
    manifest_path = (
        output_directory / f"review_{purpose}_queue_{token}.manifest.json"
    )
    priority_counts = {
        priority: sum(row["review_priority"] == priority for row in rows)
        for priority in REVIEW_PRIORITIES
    }
    staged_csv = staged_manifest = None
    try:
        staged_csv = _stage_csv(
            output_directory, list(REVIEW_EXPORT_FIELDS), rows,
            prefix=f".review-{purpose}.",
        )
        csv_hash = sha256(staged_csv.read_bytes()).hexdigest()
        manifest = {
            "format": "company-registry-review-queue-v1",
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "purpose": purpose,
            "policy_version": policy_version,
            "csv_file": csv_path.name,
            "csv_sha256": csv_hash,
            "row_count": len(rows),
            "assessment_ids": [
                row["qualification_assessment_id"] for row in rows
            ],
            "company_ids": [row["company_id"] for row in rows],
            "priority_counts": priority_counts,
        }
        staged_manifest = _stage_manifest(
            output_directory, manifest,
            prefix=f".review-{purpose}-manifest.",
        )
        if failure_injector:
            failure_injector("before_csv_publish")
        os.replace(staged_csv, csv_path)
        staged_csv = None
        _fsync_directory(output_directory)
        if failure_injector:
            failure_injector("before_manifest_publish")
        os.replace(staged_manifest, manifest_path)
        staged_manifest = None
        _fsync_directory(output_directory)
        return {
            "csv": csv_path,
            "manifest": manifest_path,
            "rows": len(rows),
            "sha256": csv_hash,
            "assessment_ids": manifest["assessment_ids"],
            "company_ids": manifest["company_ids"],
            "priority_counts": priority_counts,
        }
    finally:
        for temporary in (staged_csv, staged_manifest):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def verify_export_manifest(manifest_path: str | Path) -> dict:
    """Validate a published export's commit marker, hash, count, and decision list."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    export_format = manifest.get("format")
    if export_format not in {
        "company-registry-new-companies-v1",
        "company-registry-qualified-leads-v1",
        "company-registry-review-queue-v1",
    }:
        raise ValueError("Unsupported company-registry export manifest format")
    csv_path = manifest_path.parent / str(manifest.get("csv_file") or "")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Manifest CSV does not exist: {csv_path}")
    actual_hash = sha256(csv_path.read_bytes()).hexdigest()
    if actual_hash != manifest.get("csv_sha256"):
        raise ValueError("Company-registry export CSV hash does not match its manifest")
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        exported_rows = list(DictReader(handle))
    qualified = export_format == "company-registry-qualified-leads-v1"
    review = export_format == "company-registry-review-queue-v1"
    assessment_backed = qualified or review
    id_field = (
        "qualification_assessment_id"
        if assessment_backed else "registry_decision_id"
    )
    manifest_id_field = "assessment_ids" if assessment_backed else "decision_ids"
    exported_ids = [row.get(id_field, "") for row in exported_rows]
    if len(exported_rows) != manifest.get("row_count"):
        raise ValueError("Company-registry export row count does not match its manifest")
    if exported_ids != manifest.get(manifest_id_field):
        raise ValueError("Company-registry export IDs do not match its manifest")
    company_ids = [row.get("company_id", "") for row in exported_rows]
    manifest_company_ids = manifest.get("company_ids")
    if assessment_backed or manifest_company_ids is not None:
        if company_ids != manifest_company_ids:
            raise ValueError("Company-registry export company IDs do not match its manifest")
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("Company-registry export contains duplicate companies")
    if qualified and any(row.get("eligibility") != "ELIGIBLE" for row in exported_rows):
        raise ValueError("Qualified export contains a non-eligible row")
    if review and any(row.get("eligibility") != "REVIEW" for row in exported_rows):
        raise ValueError("Review export contains a non-review row")
    if review and any(
        row.get("review_priority") not in REVIEW_PRIORITIES
        for row in exported_rows
    ):
        raise ValueError("Review export contains an invalid priority")
    if review:
        priority_counts = {
            priority: sum(
                row.get("review_priority") == priority for row in exported_rows
            )
            for priority in REVIEW_PRIORITIES
        }
        if priority_counts != manifest.get("priority_counts"):
            raise ValueError("Review export priority counts do not match its manifest")
    return {
        "valid": True,
        "csv": csv_path,
        "manifest": manifest_path,
        "rows": len(exported_rows),
        "sha256": actual_hash,
        manifest_id_field: exported_ids,
        "company_ids": company_ids,
    }


def verify_review_export(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    purpose: str,
    *,
    policy_version: str | None = None,
) -> dict:
    """Verify one review queue against its exact SQLite projection."""
    if purpose not in {"employment", "mission"}:
        raise ValueError("Review export purpose must be 'employment' or 'mission'")
    database = Path(database)
    output_directory = Path(output_directory)
    token = _safe_run_token(run_id)
    manifest_path = (
        output_directory / f"review_{purpose}_queue_{token}.manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "company-registry-review-queue-v1":
        raise ValueError("Review export manifest has the wrong format")
    if policy_version is None:
        policy_version = clean_text(manifest.get("policy_version"))
        if not policy_version:
            raise ValueError("Review export manifest lacks a policy version")
    rows = _review_rows(database, run_id, purpose, policy_version)
    result = verify_export_manifest(manifest_path)
    if (
        manifest.get("run_id") != run_id
        or manifest.get("purpose") != purpose
        or manifest.get("policy_version") != policy_version
    ):
        raise ValueError("Review export provenance or policy metadata does not match")
    expected = {
        "assessment_ids": [
            row["qualification_assessment_id"] for row in rows
        ],
        "company_ids": [row["company_id"] for row in rows],
    }
    for field, values in expected.items():
        if result.get(field) != values:
            raise ValueError(
                f"Review export {field} do not match committed SQLite data"
            )
    return result


def _manifest_paths(output_directory: Path, run_id: str) -> dict[str, Path]:
    token = _safe_run_token(run_id)
    return {
        "discovery": output_directory / f"new_companies_{token}.manifest.json",
        "employment": output_directory / f"qualified_employment_leads_{token}.manifest.json",
        "mission": output_directory / f"qualified_mission_leads_{token}.manifest.json",
    }


def verify_run_exports(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    *,
    policy_version: str | None = None,
) -> dict[str, dict]:
    """Verify artifacts, defaulting to their recorded historical policy."""
    return {
        name: verify_run_export(
            database, run_id, output_directory, name,
            policy_version=policy_version,
        )
        for name in ("discovery", "employment", "mission")
    }


def verify_run_export(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    name: str,
    *,
    policy_version: str | None = None,
) -> dict:
    """Verify one named artifact against its exact SQLite projection."""
    if name not in {"discovery", "employment", "mission"}:
        raise ValueError(f"Unknown required run export: {name}")
    database = Path(database)
    output_directory = Path(output_directory)
    manifest_path = _manifest_paths(output_directory, run_id)[name]
    if name == "discovery":
        rows = _new_company_rows(database, run_id)[0]
        expected = {
            "decision_ids": [row["registry_decision_id"] for row in rows],
            "company_ids": [row["company_id"] for row in rows],
        }
    else:
        if policy_version is None:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            policy_version = clean_text(manifest.get("policy_version"))
            if not policy_version:
                raise ValueError(f"{name} export manifest lacks a policy version")
        rows = _qualified_rows(database, run_id, name, policy_version)
        expected = {
            "assessment_ids": [row["qualification_assessment_id"] for row in rows],
            "company_ids": [row["company_id"] for row in rows],
        }
    result = verify_export_manifest(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id:
        raise ValueError(f"{name} export run ID does not match SQLite run")
    if "company_ids" not in manifest:
        raise ValueError(f"{name} export manifest lacks committed company IDs")
    if name != "discovery" and (
        manifest.get("purpose") != name
        or manifest.get("policy_version") != policy_version
    ):
        raise ValueError(f"{name} export policy metadata does not match")
    for field, values in expected.items():
        if result.get(field) != values:
            raise ValueError(f"{name} export {field} do not match committed SQLite data")
    return result


def recover_run_exports(
    database: str | Path,
    run_id: str,
    output_directory: str | Path,
    *,
    failure_injector: FailureInjector | None = None,
) -> dict:
    """Preserve valid artifacts and deterministically replace invalid ones."""
    output_directory = Path(output_directory)
    rebuilt: list[str] = []
    preserved: list[str] = []

    def inject(artifact: str) -> FailureInjector | None:
        if failure_injector is None:
            return None
        return lambda point: failure_injector(f"{artifact}:{point}")

    def valid(name: str) -> bool:
        try:
            verify_run_export(
                database, run_id, output_directory, name,
                policy_version=DEFAULT_QUALIFICATION_POLICY.version,
            )
            return True
        except Exception:
            return False

    if valid("discovery"):
        preserved.append("discovery")
    else:
        export_new_companies(
            database, run_id, output_directory,
            failure_injector=inject("discovery"),
            verified_maps_publication=True,
        )
        rebuilt.append("discovery")
    for purpose in ("employment", "mission"):
        if valid(purpose):
            preserved.append(purpose)
        else:
            export_qualified_leads(
                database, run_id, output_directory, purpose=purpose,
                failure_injector=inject(purpose),
            )
            rebuilt.append(purpose)
    verified = verify_run_exports(
        database, run_id, output_directory,
        policy_version=DEFAULT_QUALIFICATION_POLICY.version,
    )
    return {"verified": verified, "rebuilt": rebuilt, "preserved": preserved}
