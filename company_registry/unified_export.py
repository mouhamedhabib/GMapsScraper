"""Deterministic read-only unified lead export for completed registry runs."""

from __future__ import annotations

from argparse import ArgumentParser
from csv import DictReader, DictWriter
import errno
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable

from company_registry.normalization import canonical_json, clean_text
from company_registry.qualification import (
    DEFAULT_QUALIFICATION_POLICY,
    EMAIL_SOURCE_FIELDS,
    classify_email_availability,
    classify_email_fields,
)
from company_registry.schema import SCHEMA_VERSION
from company_registry.storage import open_registry_readonly


UNIFIED_EXPORT_FORMAT = "company-registry-unified-leads-v1"
UNIFIED_EXPORT_SCHEMA_VERSION = 1
UNIFIED_EXPORT_DIRECTORY = "unified-leads-v1"
UNIFIED_EXPORT_FIELDS = (
    "company_id",
    "source_run_ids",
    "first_source_run_id",
    "latest_source_run_id",
    "selected_observation_run_id",
    "qualification_run_id",
    "qualification_assessment_id",
    "qualification_policy_version",
    "employment_eligible",
    "mission_eligible",
    "employment_eligibility",
    "mission_eligibility",
    "employment_relevance",
    "mission_relevance",
    "company_name",
    "country",
    "city",
    "website",
    "phone",
    "email",
    "email_classification",
    "email_status",
    "email_reason_code",
    "google_maps_link",
    "source_query",
)
FailureInjector = Callable[[str], None]


def _safe_token(value: str) -> str:
    token = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    )
    return token or "run"


def _canonical_runs(connection, run_ids: Iterable[str]) -> list[dict]:
    requested = {clean_text(run_id) for run_id in run_ids}
    requested.discard("")
    if not requested:
        raise ValueError("At least one source run ID is required")
    placeholders = ",".join("?" for _ in requested)
    records = connection.execute(
        f"""SELECT run_id, status, started_at, finished_at
              FROM discovery_runs
             WHERE run_id IN ({placeholders})""",
        tuple(sorted(requested)),
    ).fetchall()
    found = {record["run_id"] for record in records}
    missing = requested - found
    if missing:
        raise LookupError("Unknown discovery runs: " + ", ".join(sorted(missing)))
    incomplete = {
        record["run_id"]: record["status"]
        for record in records
        if record["status"] != "SUCCESS"
    }
    if incomplete:
        details = ", ".join(
            f"{run_id}={status}" for run_id, status in sorted(incomplete.items())
        )
        raise ValueError(f"Unified export requires SUCCESS runs; got {details}")
    return [
        dict(record)
        for record in sorted(
            records,
            key=lambda item: (item["started_at"], item["run_id"]),
        )
    ]


def _load_json(value: object) -> dict:
    try:
        loaded = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first(mapping: dict, *fields: str) -> str:
    for field in fields:
        value = clean_text(mapping.get(field))
        if value:
            return value
    return ""


def _candidate_email(evidence: dict, raw: dict) -> dict[str, str]:
    source = classify_email_fields(raw)
    candidates = evidence.get("email_candidates", [])
    candidates = candidates if isinstance(candidates, list) else []
    candidates = [item for item in candidates if isinstance(item, dict)]
    for decision in ("ACCEPT", "REVIEW", "REJECT"):
        selected = next(
            (item for item in candidates if item.get("decision") == decision),
            None,
        )
        if selected is not None:
            value = clean_text(
                selected.get("normalized") or selected.get("original")
            )
            if decision == "REJECT" and not value:
                value = " ".join(str(selected.get("original") or "").split())
            return {
                "email": value,
                "email_classification": decision,
                "email_status": source.status,
                "email_reason_code": clean_text(selected.get("reason_code")),
            }

    if source.decision:
        value = ""
        for field in EMAIL_SOURCE_FIELDS:
            classification = classify_email_availability(raw.get(field), field)
            if classification.status != source.status:
                continue
            raw_value = raw.get(field)
            if isinstance(raw_value, (list, tuple, set)):
                raw_value = next(iter(raw_value), "")
            value = " ".join(str(raw_value or "").split()).strip()
            break
        return {
            "email": value,
            "email_classification": source.decision,
            "email_status": source.status,
            "email_reason_code": source.reason_code,
        }
    return {
        "email": "",
        "email_classification": "UNAVAILABLE",
        "email_status": source.status,
        "email_reason_code": source.reason_code,
    }


def _eligibility_flag(value: object) -> str:
    normalized = clean_text(value).upper()
    if not normalized:
        return ""
    return "TRUE" if normalized == "ELIGIBLE" else "FALSE"


def _projection(
    database: Path,
    run_ids: Iterable[str],
    policy_version: str,
) -> tuple[list[dict], list[dict]]:
    connection = open_registry_readonly(database)
    try:
        runs = _canonical_runs(connection, run_ids)
        ordered_run_ids = [run["run_id"] for run in runs]
        run_rank = {
            run_id: index for index, run_id in enumerate(ordered_run_ids)
        }
        placeholders = ",".join("?" for _ in ordered_run_ids)
        records = connection.execute(
            f"""SELECT decision.decision_id, decision.run_id,
                       decision.company_id, decision.classification,
                       decision.resolution_action, decision.observed_at,
                       observation.raw_payload_json,
                       assessment.assessment_id, assessment.policy_version,
                       assessment.evidence_json,
                       assessment.employment_relevance,
                       assessment.mission_relevance,
                       assessment.employment_eligibility,
                       assessment.mission_eligibility,
                       assessment.assessed_at
                  FROM discovery_run_decisions decision
                  JOIN discovery_observations observation
                    ON observation.observation_id=decision.observation_id
             LEFT JOIN qualification_assessments assessment
                    ON assessment.decision_id=decision.decision_id
                   AND assessment.policy_version=?
                 WHERE decision.run_id IN ({placeholders})
                   AND decision.company_id IS NOT NULL
                   AND decision.requires_review=0""",
            (policy_version, *ordered_run_ids),
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, list] = {}
    for record in records:
        grouped.setdefault(record["company_id"], []).append(record)
    cohort = {
        company_id: company_records
        for company_id, company_records in grouped.items()
        if any(
            item["classification"] == "NEW"
            and item["resolution_action"] == "CREATE_COMPANY"
            for item in company_records
        )
    }

    rows: list[dict] = []
    for company_id, company_records in cohort.items():
        company_records.sort(key=lambda item: (
            item["observed_at"], run_rank[item["run_id"]], item["decision_id"],
        ))
        discovery_records = [
            item for item in company_records
            if item["classification"] == "NEW"
            and item["resolution_action"] == "CREATE_COMPANY"
        ]
        representative = discovery_records[-1]
        assessments = [
            item for item in discovery_records if item["assessment_id"]
        ]
        assessments.sort(key=lambda item: (
            item["assessed_at"], item["observed_at"],
            run_rank[item["run_id"]], item["assessment_id"],
        ))
        selected = assessments[-1] if assessments else None
        contact_record = selected or representative
        raw = _load_json(contact_record["raw_payload_json"])
        evidence = _load_json(selected["evidence_json"]) if selected else {}
        source_run_ids = [
            run_id for run_id in ordered_run_ids
            if any(item["run_id"] == run_id for item in company_records)
        ]
        email = _candidate_email(evidence, raw)
        employment_eligibility = (
            clean_text(selected["employment_eligibility"]) if selected else ""
        )
        mission_eligibility = (
            clean_text(selected["mission_eligibility"]) if selected else ""
        )
        rows.append({
            "company_id": company_id,
            "source_run_ids": canonical_json(source_run_ids),
            "first_source_run_id": source_run_ids[0],
            "latest_source_run_id": source_run_ids[-1],
            "selected_observation_run_id": representative["run_id"],
            "qualification_run_id": selected["run_id"] if selected else "",
            "qualification_assessment_id": (
                selected["assessment_id"] if selected else ""
            ),
            "qualification_policy_version": (
                selected["policy_version"] if selected else ""
            ),
            "employment_eligible": _eligibility_flag(employment_eligibility),
            "mission_eligible": _eligibility_flag(mission_eligibility),
            "employment_eligibility": employment_eligibility,
            "mission_eligibility": mission_eligibility,
            "employment_relevance": (
                selected["employment_relevance"] if selected else ""
            ),
            "mission_relevance": (
                selected["mission_relevance"] if selected else ""
            ),
            "company_name": _first(
                evidence, "name",
            ) or _first(raw, "title", "company_name", "name"),
            "country": _first(evidence, "country") or _first(raw, "country"),
            "city": _first(evidence, "city") or _first(raw, "city"),
            "website": _first(evidence, "website") or _first(
                raw, "webpage", "website", "website_url",
            ),
            "phone": _first(evidence, "phone") or _first(
                raw, "phone_number", "phone",
            ),
            **email,
            "google_maps_link": _first(
                raw, "map_link", "google_maps_url", "maps_url",
            ),
            "source_query": _first(raw, "source_query", "query"),
        })
    rows.sort(key=lambda row: row["company_id"])
    return rows, runs


def _csv_bytes(rows: list[dict]) -> bytes:
    text = StringIO(newline="")
    writer = DictWriter(
        text, fieldnames=list(UNIFIED_EXPORT_FIELDS), extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + text.getvalue().encode("utf-8")


def _fsync_directory(directory: Path) -> None:
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
            if error.errno not in {
                errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
            }:
                raise
    finally:
        os.close(descriptor)


def _write_temporary(directory: Path, payload: bytes, prefix: str) -> Path:
    with NamedTemporaryFile(
        "wb", dir=directory, prefix=prefix, suffix=".tmp", delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _dataset_token(run_ids: list[str], policy_version: str) -> str:
    if len(run_ids) == 1:
        return _safe_token(run_ids[0])
    digest = sha256(canonical_json({
        "run_ids": run_ids,
        "policy_version": policy_version,
    }).encode("utf-8")).hexdigest()[:16]
    return f"combined-{digest}"


def verify_unified_export(manifest_path: str | Path) -> dict:
    """Verify one unified CSV against its manifest and schema contract."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != UNIFIED_EXPORT_FORMAT:
        raise ValueError("Unified export manifest has an unsupported format")
    if manifest.get("export_schema_version") != UNIFIED_EXPORT_SCHEMA_VERSION:
        raise ValueError("Unified export manifest has an unsupported schema")
    if manifest.get("registry_schema_version") != SCHEMA_VERSION:
        raise ValueError("Unified export registry schema version does not match")
    if manifest.get("csv_file") != "unified_leads.csv":
        raise ValueError("Unified export manifest has an invalid CSV path")
    csv_path = manifest_path.parent / manifest["csv_file"]
    if not csv_path.is_file():
        raise FileNotFoundError(f"Unified export CSV does not exist: {csv_path}")
    digest = sha256(csv_path.read_bytes()).hexdigest()
    if digest != manifest.get("csv_sha256"):
        raise ValueError("Unified export CSV hash does not match its manifest")
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = DictReader(handle)
        if tuple(reader.fieldnames or ()) != UNIFIED_EXPORT_FIELDS:
            raise ValueError("Unified export CSV schema does not match")
        rows = list(reader)
    company_ids = [row["company_id"] for row in rows]
    if len(rows) != manifest.get("row_count"):
        raise ValueError("Unified export row count does not match its manifest")
    if company_ids != manifest.get("company_ids"):
        raise ValueError("Unified export company IDs do not match its manifest")
    if company_ids != sorted(company_ids) or len(company_ids) != len(set(company_ids)):
        raise ValueError("Unified export company ordering or uniqueness is invalid")
    source_run_ids = manifest.get("source_run_ids")
    if not isinstance(source_run_ids, list) or not source_run_ids:
        raise ValueError("Unified export manifest lacks source run IDs")
    for row in rows:
        try:
            row_runs = json.loads(row["source_run_ids"])
        except ValueError as error:
            raise ValueError("Unified export row has invalid source run IDs") from error
        if not row_runs or any(run_id not in source_run_ids for run_id in row_runs):
            raise ValueError("Unified export row source provenance is invalid")
        if (
            row["first_source_run_id"] != row_runs[0]
            or row["latest_source_run_id"] != row_runs[-1]
        ):
            raise ValueError("Unified export row source run bounds are invalid")
        if row["qualification_policy_version"] not in {
            "", manifest.get("qualification_policy_version"),
        }:
            raise ValueError("Unified export row qualification policy is invalid")
        if row["employment_eligible"] not in {"", "TRUE", "FALSE"}:
            raise ValueError("Unified export has invalid employment eligibility")
        if row["mission_eligible"] not in {"", "TRUE", "FALSE"}:
            raise ValueError("Unified export has invalid mission eligibility")
        if row["email_classification"] not in {
            "ACCEPT", "REVIEW", "REJECT", "UNAVAILABLE",
        }:
            raise ValueError("Unified export has invalid email classification")
    counts = {
        "employment_eligible_count": sum(
            row["employment_eligible"] == "TRUE" for row in rows
        ),
        "mission_eligible_count": sum(
            row["mission_eligible"] == "TRUE" for row in rows
        ),
        "employment_mission_overlap_count": sum(
            row["employment_eligible"] == "TRUE"
            and row["mission_eligible"] == "TRUE"
            for row in rows
        ),
        "qualified_company_count": sum(
            row["employment_eligible"] == "TRUE"
            or row["mission_eligible"] == "TRUE"
            for row in rows
        ),
        "missing_qualification_count": sum(
            not row["qualification_assessment_id"] for row in rows
        ),
    }
    if any(manifest.get(key) != value for key, value in counts.items()):
        raise ValueError("Unified export qualification counts do not match")
    return {
        "valid": True,
        "csv": csv_path,
        "manifest": manifest_path,
        "rows": len(rows),
        "sha256": digest,
        "company_ids": company_ids,
        **counts,
    }


def export_unified_leads(
    database: str | Path,
    run_ids: Iterable[str],
    output_directory: str | Path,
    *,
    policy_version: str = DEFAULT_QUALIFICATION_POLICY.version,
    failure_injector: FailureInjector | None = None,
) -> dict:
    """Publish one immutable, manifest-last unified lead dataset."""
    database = Path(database)
    output_root = Path(output_directory)
    if output_root.exists() and not output_root.is_dir():
        raise ValueError("Unified export destination must be a directory")
    rows, runs = _projection(database, run_ids, policy_version)
    canonical_run_ids = [run["run_id"] for run in runs]
    destination = (
        output_root / UNIFIED_EXPORT_DIRECTORY
        / _dataset_token(canonical_run_ids, policy_version)
    )
    csv_path = destination / "unified_leads.csv"
    manifest_path = destination / "unified_leads.manifest.json"
    database_resolved = database.resolve()
    if database_resolved in {csv_path.resolve(), manifest_path.resolve()}:
        raise ValueError("Unified export path collides with the source database")
    destination.mkdir(parents=True, exist_ok=True)

    csv_payload = _csv_bytes(rows)
    csv_hash = sha256(csv_payload).hexdigest()
    counts = {
        "employment_eligible_count": sum(
            row["employment_eligible"] == "TRUE" for row in rows
        ),
        "mission_eligible_count": sum(
            row["mission_eligible"] == "TRUE" for row in rows
        ),
        "employment_mission_overlap_count": sum(
            row["employment_eligible"] == "TRUE"
            and row["mission_eligible"] == "TRUE"
            for row in rows
        ),
        "qualified_company_count": sum(
            row["employment_eligible"] == "TRUE"
            or row["mission_eligible"] == "TRUE"
            for row in rows
        ),
        "missing_qualification_count": sum(
            not row["qualification_assessment_id"] for row in rows
        ),
    }
    manifest = {
        "format": UNIFIED_EXPORT_FORMAT,
        "export_schema_version": UNIFIED_EXPORT_SCHEMA_VERSION,
        "registry_schema_version": SCHEMA_VERSION,
        "source_run_ids": canonical_run_ids,
        "source_run_statuses": {
            run["run_id"]: run["status"] for run in runs
        },
        "qualification_policy_version": policy_version,
        "csv_file": csv_path.name,
        "csv_sha256": csv_hash,
        "row_count": len(rows),
        "company_ids": [row["company_id"] for row in rows],
        **counts,
    }
    manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")

    if manifest_path.exists():
        verified = verify_unified_export(manifest_path)
        if (
            csv_path.read_bytes() == csv_payload
            and manifest_path.read_bytes() == manifest_payload
        ):
            return {
                **verified,
                "destination": destination,
                "source_run_ids": canonical_run_ids,
                "reused": True,
            }
        raise FileExistsError(
            "Unified export destination already contains a different committed export"
        )
    if csv_path.exists() and csv_path.read_bytes() != csv_payload:
        raise FileExistsError(
            "Unified export CSV path collides with an existing source or artifact"
        )

    staged_csv = staged_manifest = None
    try:
        if not csv_path.exists():
            staged_csv = _write_temporary(
                destination, csv_payload, ".unified-leads.",
            )
            if failure_injector:
                failure_injector("before_csv_publish")
            os.replace(staged_csv, csv_path)
            staged_csv = None
            _fsync_directory(destination)
        staged_manifest = _write_temporary(
            destination, manifest_payload, ".unified-leads-manifest.",
        )
        if failure_injector:
            failure_injector("before_manifest_publish")
        os.replace(staged_manifest, manifest_path)
        staged_manifest = None
        _fsync_directory(destination)
    finally:
        for temporary in (staged_csv, staged_manifest):
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    verified = verify_unified_export(manifest_path)
    return {
        **verified,
        "destination": destination,
        "source_run_ids": canonical_run_ids,
        "reused": False,
    }


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, required=True,
        help="Existing schema-v6 company registry opened read-only",
    )
    parser.add_argument(
        "--run-id", action="append", required=True, dest="run_ids",
        help="Completed SUCCESS run to include; repeat for a combined export",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Root for the separate unified-leads-v1 destination",
    )
    parser.add_argument(
        "--policy-version", default=DEFAULT_QUALIFICATION_POLICY.version,
        help="Existing qualification policy version to project",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    result = export_unified_leads(
        arguments.database,
        arguments.run_ids,
        arguments.output_dir,
        policy_version=arguments.policy_version,
    )
    print(canonical_json({
        "csv": str(result["csv"]),
        "manifest": str(result["manifest"]),
        "rows": result["rows"],
        "sha256": result["sha256"],
        "source_run_ids": result["source_run_ids"],
        "reused": result["reused"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
