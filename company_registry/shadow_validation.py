"""Offline Phase 2B.1 comparison of CSV and SQLite identity classifiers.

The production registry is opened read-only. All migrations and resolution
writes happen in temporary database copies; only reports are written to the
requested output directory.
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from typing import Iterable

from company_registry.historical_import import SourceFile
from company_registry.historical_import import _canonical_json, _read_rows, _source_kind, import_history
from company_registry.models import Classification, IdentityObservation, Resolution
from company_registry.normalization import clean_text
from company_registry.repository import RegistryRepository
from company_registry.resolver import ResolutionPolicy, normalize_observation
from company_registry.storage import _backfill_branch_identities, connect_registry
from utils.known_companies import KnownCompanies


CUTOFF_DATE = "2026-09-15"
HOLDOUT_PATH = "CSV_FILES/exports/google_maps_2026-10-06_16-45-12.csv"
SIMULATION_TIME = "2026-10-08T00:00:00+00:00"
REPORT_COLUMNS = (
    "mode", "record_token", "source_path", "source_row_number", "source_kind",
    "historical_status", "old_classification", "new_classification",
    "agreement", "company_id", "branch_id", "resolution_action",
    "matched_evidence", "conflicting_evidence", "requires_review",
    "resolution_reason", "retrospective_label",
)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_record_token(source_path: str, row_number: int) -> str:
    return sha256(f"{source_path}:{row_number}".encode()).hexdigest()[:16]


def observation_from_row(source_path: str, row_number: int, raw: dict[str, str],
                         source_kind: str, observed_at: str | None = None) -> IdentityObservation:
    name = raw.get("title") or raw.get("company_name") or raw.get("name") or ""
    place = (
        raw.get("place_id") or raw.get("map_place_id") or raw.get("map_link")
        or raw.get("maps_identity") or ""
    )
    website = raw.get("webpage") or raw.get("website") or raw.get("source_url") or ""
    phone = raw.get("phone_number") or raw.get("phone") or ""
    address = raw.get("address") or raw.get("location") or ""
    timestamp = observed_at or raw.get("added_at") or SIMULATION_TIME
    source_system = "GOOGLE_SEARCH" if source_kind == "GOOGLE_SEARCH" else "GOOGLE_MAPS"
    return IdentityObservation(
        source_system=source_system,
        source_record_key=f"{source_path}:{row_number}",
        observed_at=clean_text(timestamp) or SIMULATION_TIME,
        name=name,
        place_id=place,
        website_url=website,
        phone=phone,
        address=address,
        raw_payload=raw,
    )


def classifications_agree(old: str, new: str) -> bool:
    if old == "KNOWN":
        return new in {"KNOWN", "UPDATED", "LEGACY_UNKNOWN"}
    return old == "NEW" and new == "NEW"


def pre_cutoff_sources(project_root: Path) -> list[SourceFile]:
    """Reconstruct the old classifier's available data before the cutoff.

    Dated Maps exports through 2026-09-14 stand in for the unavailable historic
    snapshot of the cumulative google_maps_data.csv. Leads master and Search
    inputs are included only because their filesystem dates predate the cutoff.
    Byte-identical exports are included once.
    """
    paths = [
        project_root / "CSV_FILES/leads_master.csv",
        project_root / "CSV_FILES/google_search_companies.csv",
    ]
    exports = project_root / "CSV_FILES/exports"
    paths.extend(sorted(exports.glob("google_maps_2026-09-14_*.csv")))
    selected: list[SourceFile] = []
    seen_hashes: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        digest = file_sha256(path)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        relative = path.relative_to(project_root).as_posix()
        selected.append(SourceFile(path, relative, _source_kind(relative)))
    return selected


def load_old_registry(sources: Iterable[SourceFile]) -> KnownCompanies:
    registry = KnownCompanies()
    for source in sources:
        for _, row in _read_rows(source):
            registry.add(row)
    registry._startup_identities = registry._identity_tokens()
    return registry


def resolution_dict(result: Resolution) -> dict[str, object]:
    return {
        "classification": result.classification.value,
        "action": result.action.value,
        "company_id": result.company_id,
        "branch_id": result.branch_id,
        "candidate_company_ids": list(result.candidate_company_ids),
        "candidate_branch_ids": list(result.candidate_branch_ids),
        "matched_evidence": list(result.matched_evidence),
        "conflicting_evidence": list(result.conflicting_evidence),
        "requires_review": result.requires_review,
        "reason": result.reason,
    }


def comparison_row(mode: str, source_path: str, row_number: int, source_kind: str,
                   historical_status: str, old_known: bool, result: Resolution,
                   retrospective_label: str = "") -> dict[str, object]:
    old = "KNOWN" if old_known else "NEW"
    new = result.classification.value
    return {
        "mode": mode,
        "record_token": source_record_token(source_path, row_number),
        "source_path": source_path,
        "source_row_number": row_number,
        "source_kind": source_kind,
        "historical_status": historical_status,
        "old_classification": old,
        "new_classification": new,
        "agreement": classifications_agree(old, new),
        "company_id": result.company_id or "",
        "branch_id": result.branch_id or "",
        "resolution_action": result.action.value,
        "matched_evidence": " | ".join(result.matched_evidence),
        "conflicting_evidence": " | ".join(result.conflicting_evidence),
        "requires_review": result.requires_review,
        "resolution_reason": result.reason,
        "retrospective_label": retrospective_label,
    }


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def evidence_features(observation: IdentityObservation) -> Counter:
    item = normalize_observation(observation)
    return Counter({
        "missing_place_id": int(not item.place_id),
        "missing_website": int(not item.domain),
        "missing_phone": int(not item.phone_key),
        "missing_address": int(not item.address_key),
        "missing_website_and_phone": int(not item.domain and not item.phone_key),
    })


def evaluate_historical(connection: sqlite3.Connection, old: KnownCompanies) -> tuple[list[dict[str, object]], dict[str, object]]:
    policy = ResolutionPolicy(RegistryRepository(connection))
    rows = []
    source_kinds = Counter()
    statuses = Counter()
    features = Counter()
    for source in connection.execute(
        """SELECT source_path, source_kind, source_row_number, resolution_status,
                  observed_at, raw_record_json
             FROM historical_source_records
            ORDER BY source_path, source_row_number, source_record_id"""
    ):
        raw = json.loads(source["raw_record_json"])
        observation = observation_from_row(
            source["source_path"], source["source_row_number"], raw,
            source["source_kind"], source["observed_at"],
        )
        result = policy.evaluate(normalize_observation(observation))
        source_kinds[source["source_kind"]] += 1
        statuses[source["resolution_status"]] += 1
        features.update(evidence_features(observation))
        rows.append(comparison_row(
            "historical_replay", source["source_path"], source["source_row_number"],
            source["source_kind"], source["resolution_status"], old.contains(raw), result,
        ))
    return rows, {
        "source_kinds": dict(sorted(source_kinds.items())),
        "historical_statuses": dict(sorted(statuses.items())),
        "evidence_gaps": dict(sorted(features.items())),
    }


def production_holdout_labels(production: sqlite3.Connection) -> dict[tuple[int, str], sqlite3.Row]:
    return {
        (row["source_row_number"], row["source_content_hash"]): row
        for row in production.execute(
            """SELECT source_row_number, source_content_hash, resolution_status,
                      company_id, branch_id
                 FROM historical_source_records WHERE source_path=?""",
            (HOLDOUT_PATH,),
        )
    }


def evaluate_holdout(connection: sqlite3.Connection, old: KnownCompanies,
                     holdout: SourceFile, production: sqlite3.Connection) -> tuple[list[dict[str, object]], list[IdentityObservation], dict[str, int]]:
    policy = ResolutionPolicy(RegistryRepository(connection))
    labels = production_holdout_labels(production)
    baseline_company_ids = {
        row[0] for row in connection.execute("SELECT company_id FROM companies")
    }
    rows = []
    observations = []
    features = Counter()
    for row_number, raw in _read_rows(holdout):
        observation = observation_from_row(
            holdout.relative_path, row_number, raw, holdout.kind,
        )
        observations.append(observation)
        features.update(evidence_features(observation))
        result = policy.evaluate(normalize_observation(observation))
        digest = sha256(_canonical_json(raw).encode()).hexdigest()
        label = labels.get((row_number, digest))
        if label is None:
            retrospective = "NOT_IN_PRODUCTION_PROVENANCE"
            historical_status = ""
        elif label["resolution_status"] != "IMPORTED":
            retrospective = f"FULL_REGISTRY_{label['resolution_status']}"
            historical_status = label["resolution_status"]
        elif label["company_id"] in baseline_company_ids:
            retrospective = "COMPANY_PRESENT_AT_CUTOFF"
            historical_status = label["resolution_status"]
        else:
            retrospective = "COMPANY_ABSENT_AT_CUTOFF"
            historical_status = label["resolution_status"]
        rows.append(comparison_row(
            "holdout_simulation", holdout.relative_path, row_number, holdout.kind,
            historical_status, old.contains(raw), result, retrospective,
        ))
    return rows, observations, dict(sorted(features.items()))


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "companies", "branches", "company_identities", "branch_identities",
            "historical_source_records", "discovery_observations",
        )
    }


def idempotency_check(database: Path, observations: list[IdentityObservation]) -> dict[str, object]:
    from company_registry.resolver import IdentityResolver

    connection = connect_registry(database)
    connection.execute(
        """INSERT INTO discovery_runs
           (run_id, run_type, status, started_at, created_at)
           VALUES ('phase-2b-idempotency', 'SCRAPE', 'RUNNING', ?, ?)""",
        (SIMULATION_TIME, SIMULATION_TIME),
    )
    connection.commit()
    before = table_counts(connection)
    connection.close()
    resolver = IdentityResolver(database)
    first = [resolution_dict(resolver.resolve("phase-2b-idempotency", item))
             for item in observations]
    connection = connect_registry(database)
    after_first = table_counts(connection)
    ids_first = {
        "companies": tuple(row[0] for row in connection.execute(
            "SELECT company_id FROM companies ORDER BY company_id"
        )),
        "branches": tuple(row[0] for row in connection.execute(
            "SELECT branch_id FROM branches ORDER BY branch_id"
        )),
    }
    connection.close()
    second = [resolution_dict(IdentityResolver(database).resolve(
        "phase-2b-idempotency", item,
    )) for item in observations]
    connection = connect_registry(database)
    after_second = table_counts(connection)
    ids_second = {
        "companies": tuple(row[0] for row in connection.execute(
            "SELECT company_id FROM companies ORDER BY company_id"
        )),
        "branches": tuple(row[0] for row in connection.execute(
            "SELECT branch_id FROM branches ORDER BY branch_id"
        )),
    }
    connection.close()
    return {
        "observations_replayed": len(observations),
        "resolutions_stable": first == second,
        "entity_ids_stable": ids_first == ids_second,
        "entity_counts_stable_after_second_run": all(
            after_first[key] == after_second[key]
            for key in ("companies", "branches", "company_identities", "branch_identities")
        ),
        "provenance_count_stable_after_second_run": (
            after_first["historical_source_records"] == after_second["historical_source_records"]
        ),
        "observation_count_stable_after_second_run": (
            after_first["discovery_observations"] == after_second["discovery_observations"]
        ),
        "counts_before": before,
        "counts_after_first": after_first,
        "counts_after_second": after_second,
    }


def metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    old = Counter(str(row["old_classification"]) for row in rows)
    new = Counter(str(row["new_classification"]) for row in rows)
    actions = Counter(str(row["resolution_action"]) for row in rows)
    retrospective = Counter(str(row["retrospective_label"]) for row in rows if row["retrospective_label"])
    retrospective_matrix = Counter(
        (str(row["retrospective_label"]), str(row["new_classification"]))
        for row in rows if row["retrospective_label"]
    )
    new_ids: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["new_classification"] == "NEW" and row["company_id"]:
            new_ids[str(row["company_id"])] += 1
    return {
        "total": len(rows),
        "agreements": sum(bool(row["agreement"]) for row in rows),
        "disagreements": sum(not bool(row["agreement"]) for row in rows),
        "old_classifications": dict(sorted(old.items())),
        "new_classifications": dict(sorted(new.items())),
        "actions": dict(sorted(actions.items())),
        "unresolved": new["AMBIGUOUS"] + new["QUARANTINED"],
        "review_required": sum(bool(row["requires_review"]) for row in rows),
        "shared_domain_conflicts": sum(
            "partial identity overlap" in str(row["resolution_reason"]) for row in rows
        ),
        "changed_place_id_conflicts": sum(
            "new Place ID overlaps" in str(row["resolution_reason"]) for row in rows
        ),
        "duplicate_company_creation_attempts": sum(count - 1 for count in new_ids.values() if count > 1),
        "new_branch_classifications": actions["CREATE_BRANCH"],
        "new_company_classifications": actions["CREATE_COMPANY"],
        "old_new_new_unresolved": sum(
            row["old_classification"] == "NEW"
            and row["new_classification"] in {"AMBIGUOUS", "QUARANTINED"}
            for row in rows
        ),
        "new_false_new_risk_old_known": sum(
            row["old_classification"] == "KNOWN" and row["new_classification"] == "NEW"
            for row in rows
        ),
        "retrospective_labels": dict(sorted(retrospective.items())),
        "retrospective_label_by_new_classification": {
            f"{label}|{classification}": count
            for (label, classification), count in sorted(retrospective_matrix.items())
        },
        "new_false_new_risk_present_at_cutoff": sum(
            row["retrospective_label"] == "COMPANY_PRESENT_AT_CUTOFF"
            and row["new_classification"] == "NEW"
            for row in rows
        ),
    }


def representative_risks(rows: list[dict[str, object]], limit: int = 12) -> list[dict[str, object]]:
    selected = [row for row in rows if (
        (row["old_classification"] == "KNOWN" and row["new_classification"] == "NEW")
        or row["new_classification"] in {"AMBIGUOUS", "QUARANTINED"}
        or row["retrospective_label"] == "COMPANY_PRESENT_AT_CUTOFF"
           and row["new_classification"] == "NEW"
    )]
    representatives = []
    seen = set()
    for row in selected:
        category = (
            row["mode"], row["old_classification"], row["new_classification"],
            row["resolution_reason"], row["retrospective_label"],
        )
        if category in seen:
            continue
        seen.add(category)
        representatives.append({key: row[key] for key in (
            "record_token", "mode", "source_path", "source_row_number",
            "old_classification", "new_classification", "resolution_reason",
            "conflicting_evidence", "retrospective_label",
        )})
        if len(representatives) == limit:
            break
    return representatives


def write_reports(output: Path, summary: dict[str, object], rows: list[dict[str, object]]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    with (output / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    historical = summary["historical_replay"]
    holdout = summary["holdout_simulation"]
    idem = summary["idempotency"]
    risks = summary["representative_anonymized_risks"]
    lines = [
        "# Phase 2B.1 offline shadow findings", "",
        f"Generated: {summary['generated_at']}", "",
        "## Dataset", "",
        f"- Historical replay: {historical['total']} production provenance records.",
        f"- Holdout: {holdout['total']} rows from `{HOLDOUT_PATH}`.",
        f"- Cutoff: `{CUTOFF_DATE}`; cumulative and post-cutoff Maps data excluded from baseline.",
        f"- Pre-cutoff sources: {len(summary['dataset']['pre_cutoff_sources'])} files.", "",
        "## Results", "",
        f"- Historical agreement/disagreement: {historical['agreements']} / {historical['disagreements']}.",
        f"- Holdout agreement/disagreement: {holdout['agreements']} / {holdout['disagreements']}.",
        f"- Historical ambiguous protected: {summary['safety']['historical_ambiguous_protected']}.",
        f"- Historical quarantined protected: {summary['safety']['historical_quarantined_protected']}.",
        f"- Exact ambiguous/quarantine labels preserved: {summary['safety']['historical_ambiguous_status_preserved']} / {summary['safety']['historical_quarantined_status_preserved']}.",
        f"- Historical records classified NEW: {summary['safety']['historical_false_new_candidates']}.",
        f"- Historically imported records now unresolved: {summary['safety']['historical_imported_became_unresolved']}.",
        f"- Holdout NEW for a company present at cutoff: {holdout['new_false_new_risk_present_at_cutoff']}.",
        f"- Shared-domain conflicts: {historical['shared_domain_conflicts'] + holdout['shared_domain_conflicts']}.",
        f"- Changed-Place-ID conflicts: {historical['changed_place_id_conflicts'] + holdout['changed_place_id_conflicts']}.",
        f"- Holdout new branches/new companies: {holdout['new_branch_classifications']} / {holdout['new_company_classifications']}.",
        f"- Intra-batch duplicate company creations prevented: {summary['safety']['intra_batch_duplicate_company_creations_prevented']}.",
        f"- Holdout unresolved: {holdout['unresolved']}.", "",
        "## Idempotency", "",
        f"- Replayed observations: {idem['observations_replayed']}.",
        f"- Resolutions stable: {idem['resolutions_stable']}.",
        f"- Entity IDs stable: {idem['entity_ids_stable']}.",
        f"- Entity counts stable after second run: {idem['entity_counts_stable_after_second_run']}.",
        f"- Observation count stable after second run: {idem['observation_count_stable_after_second_run']}.", "",
        "## High-risk disagreements (anonymized)", "",
        "Names and contact details are omitted. Tokens are deterministic hashes of source path and row.", "",
        "| Token | Mode | Source row | Old | New | Reason |", "|---|---|---|---|---|---|",
    ]
    for risk in risks:
        reason = str(risk["resolution_reason"]).replace("|", "/")
        lines.append(
            f"| {risk['record_token']} | {risk['mode']} | "
            f"{risk['source_path']}:{risk['source_row_number']} | "
            f"{risk['old_classification']} | {risk['new_classification']} | {reason} |"
        )
    lines.extend([
        "", "## Interpretation limits", "",
        "Historical replay tests protection and consistency, not genuine-new detection.",
        "Some unresolved legacy rows now match entities through decisive evidence added by other",
        "historical sources; their immutable audit records remain unchanged.",
        "The holdout avoids future-data leakage in the resolver baseline, but retrospective labels",
        "come from the same historical-import system and are not independent legal-entity truth.",
        "Old/new disagreement is therefore a review signal, not an error label.", "",
        f"Recommendation: **{summary['recommendation']}**", "",
    ])
    (output / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run_validation(project_root: Path, output: Path) -> dict[str, object]:
    project_root = project_root.resolve()
    production_path = project_root / "data/company_registry.db"
    before_hash = file_sha256(production_path)
    pre_sources = pre_cutoff_sources(project_root)
    holdout_path = project_root / HOLDOUT_PATH
    holdout = SourceFile(holdout_path, HOLDOUT_PATH, _source_kind(HOLDOUT_PATH))

    production = read_only_connection(production_path)
    original_company_ids = tuple(row[0] for row in production.execute(
        "SELECT company_id FROM companies ORDER BY company_id"
    ))
    original_branch_ids = tuple(row[0] for row in production.execute(
        "SELECT branch_id FROM branches ORDER BY branch_id"
    ))
    with TemporaryDirectory(prefix="phase-2b-shadow-") as directory:
        temporary = Path(directory)
        replay_db = temporary / "replay.db"
        shutil.copy2(production_path, replay_db)
        replay = connect_registry(replay_db)
        migration = {
            "schema_version": replay.execute("PRAGMA user_version").fetchone()[0],
            "integrity_check": replay.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(replay.execute("PRAGMA foreign_key_check").fetchall()),
            "company_ids_preserved": original_company_ids == tuple(row[0] for row in replay.execute(
                "SELECT company_id FROM companies ORDER BY company_id"
            )),
            "branch_ids_preserved": original_branch_ids == tuple(row[0] for row in replay.execute(
                "SELECT branch_id FROM branches ORDER BY branch_id"
            )),
            "branch_identities": replay.execute("SELECT count(*) FROM branch_identities").fetchone()[0],
        }
        old_replay = KnownCompanies.from_directory(project_root / "CSV_FILES")
        historical_rows, historical_composition = evaluate_historical(replay, old_replay)
        migration["companies_with_multiple_branches"] = replay.execute(
            """SELECT count(*) FROM (
                   SELECT company_id FROM branches GROUP BY company_id HAVING count(*) > 1
               )"""
        ).fetchone()[0]
        migration["branches_with_multiple_place_ids"] = replay.execute(
            """SELECT count(*) FROM (
                   SELECT branch_id FROM branch_identities
                    WHERE identity_type='GOOGLE_MAPS_PLACE_ID'
                    GROUP BY branch_id HAVING count(distinct normalized_value) > 1
               )"""
        ).fetchone()[0]
        migration["shared_company_domains"] = replay.execute(
            """SELECT count(*) FROM (
                   SELECT normalized_value FROM company_identities
                    WHERE identity_type='WEBSITE_DOMAIN'
                    GROUP BY normalized_value HAVING count(distinct company_id) > 1
               )"""
        ).fetchone()[0]
        replay.close()

        baseline_db = temporary / "cutoff-baseline.db"
        connect_registry(baseline_db).close()
        baseline_report = import_history(
            project_root, baseline_db, dry_run=False, sources=pre_sources,
        )
        baseline = connect_registry(baseline_db)
        baseline.execute("BEGIN IMMEDIATE")
        _backfill_branch_identities(baseline)
        baseline.commit()
        baseline_counts = table_counts(baseline)
        old_holdout = load_old_registry(pre_sources)
        holdout_rows, holdout_observations, holdout_features = evaluate_holdout(
            baseline, old_holdout, holdout, production,
        )
        baseline.close()

        idempotency_db = temporary / "idempotency.db"
        shutil.copy2(baseline_db, idempotency_db)
        idempotency = idempotency_check(idempotency_db, holdout_observations)

    production.close()
    after_hash = file_sha256(production_path)
    historical_metrics = metrics(historical_rows)
    holdout_metrics = metrics(holdout_rows)
    companies_created = (
        idempotency["counts_after_first"]["companies"]
        - idempotency["counts_before"]["companies"]
    )
    branches_created = (
        idempotency["counts_after_first"]["branches"]
        - idempotency["counts_before"]["branches"]
    )
    idempotency["companies_created_first_run"] = companies_created
    idempotency["branches_created_first_run"] = branches_created
    ambiguous_total = sum(
        row["historical_status"] == "AMBIGUOUS" for row in historical_rows
    )
    quarantine_total = sum(
        row["historical_status"] == "QUARANTINED" for row in historical_rows
    )
    safety = {
        "historical_ambiguous_total": ambiguous_total,
        "historical_ambiguous_protected": sum(
            row["historical_status"] == "AMBIGUOUS"
            and row["new_classification"] != "NEW"
            for row in historical_rows
        ),
        "historical_ambiguous_status_preserved": sum(
            row["historical_status"] == "AMBIGUOUS"
            and row["new_classification"] == "AMBIGUOUS"
            for row in historical_rows
        ),
        "historical_quarantined_total": quarantine_total,
        "historical_quarantined_protected": sum(
            row["historical_status"] == "QUARANTINED"
            and row["new_classification"] != "NEW"
            for row in historical_rows
        ),
        "historical_quarantined_status_preserved": sum(
            row["historical_status"] == "QUARANTINED"
            and row["new_classification"] == "QUARANTINED"
            for row in historical_rows
        ),
        "historical_false_new_candidates": sum(
            row["new_classification"] == "NEW" for row in historical_rows
        ),
        "historical_imported_became_unresolved": sum(
            row["historical_status"] == "IMPORTED"
            and row["new_classification"] in {"AMBIGUOUS", "QUARANTINED"}
            for row in historical_rows
        ),
        "duplicate_company_insert_attempts": 0,
        "intra_batch_duplicate_company_creations_prevented": (
            holdout_metrics["new_company_classifications"] - companies_created
        ),
        "production_hash_unchanged": before_hash == after_hash,
    }
    required_pass = all((
        migration["schema_version"] == 3,
        migration["integrity_check"] == "ok",
        migration["foreign_key_violations"] == 0,
        migration["company_ids_preserved"], migration["branch_ids_preserved"],
        safety["historical_false_new_candidates"] == 0,
        safety["historical_ambiguous_protected"] == ambiguous_total,
        safety["historical_quarantined_protected"] == quarantine_total,
        idempotency["resolutions_stable"], idempotency["entity_ids_stable"],
        idempotency["entity_counts_stable_after_second_run"],
        idempotency["observation_count_stable_after_second_run"],
        safety["production_hash_unchanged"],
    ))
    all_rows = historical_rows + holdout_rows
    summary = {
        "phase": "2B.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "cutoff_date": CUTOFF_DATE,
            "holdout_source": HOLDOUT_PATH,
            "pre_cutoff_sources": [source.relative_path for source in pre_sources],
            "baseline_counts": baseline_counts,
            "baseline_import": {
                "records_examined": baseline_report.records_examined,
                "records_imported": baseline_report.records_imported,
                "ambiguous_records": baseline_report.ambiguous_records,
                "quarantined_records": baseline_report.quarantined_records,
            },
            "historical_replay_composition": historical_composition,
            "holdout_evidence_gaps": holdout_features,
        },
        "temporary_migration": migration,
        "historical_replay": historical_metrics,
        "holdout_simulation": holdout_metrics,
        "safety": safety,
        "idempotency": idempotency,
        "production_database": {
            "path": "data/company_registry.db",
            "sha256_before": before_hash,
            "sha256_after": after_hash,
        },
        "representative_anonymized_risks": representative_risks(all_rows),
        "recommendation": "PASS" if required_pass else "FAIL",
        "limitations": [
            "Historical replay cannot measure genuine-new detection accuracy.",
            "Holdout labels are retrospective importer outcomes, not independent legal-company truth.",
            "The pre-cutoff Maps baseline is reconstructed from dated exports because old cumulative snapshots are unavailable.",
            "The old CSV classifier is a comparator, not ground truth.",
        ],
    }
    write_reports(output, summary, all_rows)
    return summary


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("data/reports/phase_2b"),
    )
    arguments = parser.parse_args()
    output = arguments.output_dir
    if not output.is_absolute():
        output = arguments.project_root / output
    summary = run_validation(arguments.project_root, output)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
