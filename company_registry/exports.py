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

from company_registry.normalization import canonical_json
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


def verify_export_manifest(manifest_path: str | Path) -> dict:
    """Validate a published export's commit marker, hash, count, and decision list."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    export_format = manifest.get("format")
    if export_format not in {
        "company-registry-new-companies-v1",
        "company-registry-qualified-leads-v1",
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
    id_field = "qualification_assessment_id" if qualified else "registry_decision_id"
    manifest_id_field = "assessment_ids" if qualified else "decision_ids"
    exported_ids = [row.get(id_field, "") for row in exported_rows]
    if len(exported_rows) != manifest.get("row_count"):
        raise ValueError("Company-registry export row count does not match its manifest")
    if exported_ids != manifest.get(manifest_id_field):
        raise ValueError("Company-registry export IDs do not match its manifest")
    company_ids = [row.get("company_id", "") for row in exported_rows]
    manifest_company_ids = manifest.get("company_ids")
    if qualified or manifest_company_ids is not None:
        if company_ids != manifest_company_ids:
            raise ValueError("Company-registry export company IDs do not match its manifest")
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("Company-registry export contains duplicate companies")
    if qualified and any(row.get("eligibility") != "ELIGIBLE" for row in exported_rows):
        raise ValueError("Qualified export contains a non-eligible row")
    return {
        "valid": True,
        "csv": csv_path,
        "manifest": manifest_path,
        "rows": len(exported_rows),
        "sha256": actual_hash,
        manifest_id_field: exported_ids,
        "company_ids": company_ids,
    }


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
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
) -> dict[str, dict]:
    """Verify all required artifacts against fresh ordered SQLite projections."""
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
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
) -> dict:
    """Verify one named artifact against its exact SQLite projection."""
    if name not in {"discovery", "employment", "mission"}:
        raise ValueError(f"Unknown required run export: {name}")
    database = Path(database)
    output_directory = Path(output_directory)
    if name == "discovery":
        rows = _new_company_rows(database, run_id)[0]
        expected = {
            "decision_ids": [row["registry_decision_id"] for row in rows],
            "company_ids": [row["company_id"] for row in rows],
        }
    else:
        rows = _qualified_rows(database, run_id, name, policy_version)
        expected = {
            "assessment_ids": [row["qualification_assessment_id"] for row in rows],
            "company_ids": [row["company_id"] for row in rows],
        }
    manifest_path = _manifest_paths(output_directory, run_id)[name]
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
            verify_run_export(database, run_id, output_directory, name)
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
    verified = verify_run_exports(database, run_id, output_directory)
    return {"verified": verified, "rebuilt": rebuilt, "preserved": preserved}
