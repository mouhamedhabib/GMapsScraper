"""Diagnose imported historical records that replay as unresolved."""

from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from company_registry.resolver import ResolutionPolicy, normalize_observation
from company_registry.repository import RegistryRepository
from company_registry.shadow_validation import observation_from_row
from company_registry.storage import connect_registry


def token(value: str) -> str:
    return sha256(value.encode()).hexdigest()[:16]


ROOT_CAUSES = (
    "SHARED_BRANCH_EVIDENCE_WITHIN_COMPANY_POTENTIAL_OVER_CONSERVATISM",
    "SHARED_BRANCH_EVIDENCE_ACROSS_COMPANIES",
    "CONFLICTING_PLACE_IDS",
    "COMPANY_BRANCH_IDENTITY_MISMATCH",
    "MISSING_EVIDENCE",
    "HISTORICAL_UNRESOLVED_OVERLAP",
    "POTENTIAL_RESOLVER_OVER_CONSERVATISM",
)


def root_cause(reason: str, candidate_company_count: int) -> str:
    lowered = reason.casefold()
    if "supporting evidence matches multiple branches" in lowered:
        if candidate_company_count == 1:
            return "SHARED_BRANCH_EVIDENCE_WITHIN_COMPANY_POTENTIAL_OVER_CONSERVATISM"
        return "SHARED_BRANCH_EVIDENCE_ACROSS_COMPANIES"
    if "place id" in lowered:
        return "CONFLICTING_PLACE_IDS"
    if "partial identity overlap" in lowered or "ownership evidence" in lowered:
        return "COMPANY_BRANCH_IDENTITY_MISMATCH"
    if "missing" in lowered or "insufficient identity" in lowered:
        return "MISSING_EVIDENCE"
    if "historical" in lowered:
        return "HISTORICAL_UNRESOLVED_OVERLAP"
    return "POTENTIAL_RESOLVER_OVER_CONSERVATISM"


def diagnose(production: Path, output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="phase-2b2-diagnostic-") as directory:
        copy = Path(directory) / "registry.db"
        shutil.copy2(production, copy)
        connection = connect_registry(copy)
        policy = ResolutionPolicy(RegistryRepository(connection))
        cases = []
        for source in connection.execute(
            """SELECT source_record_id, source_path, source_kind,
                      source_row_number, company_id, branch_id, observed_at,
                      raw_record_json
                 FROM historical_source_records
                WHERE resolution_status='IMPORTED'
                ORDER BY source_path, source_row_number, source_record_id"""
        ):
            raw = json.loads(source["raw_record_json"])
            observation = observation_from_row(
                source["source_path"], source["source_row_number"], raw,
                source["source_kind"], source["observed_at"],
            )
            result = policy.evaluate(normalize_observation(observation))
            if result.classification.value not in {"AMBIGUOUS", "QUARANTINED"}:
                continue
            cases.append({
                "record_token": token(source["source_record_id"]),
                "source_path": source["source_path"],
                "source_row_number": source["source_row_number"],
                "source_kind": source["source_kind"],
                "replay_classification": result.classification.value,
                "root_cause": root_cause(
                    result.reason, len(result.candidate_company_ids),
                ),
                "resolution_reason": result.reason,
                "original_company_token": token(source["company_id"]),
                "original_branch_token": token(source["branch_id"]),
                "candidate_company_count": len(result.candidate_company_ids),
                "candidate_branch_count": len(result.candidate_branch_ids),
                "conflicting_evidence": " | ".join(result.conflicting_evidence),
                "company_id": source["company_id"],
                "branch_id": source["branch_id"],
            })
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.close()

    groups = {}
    for cause in ROOT_CAUSES:
        selected = [case for case in cases if case["root_cause"] == cause]
        groups[cause] = {
            "records": len(selected),
            "unique_companies": len({case["company_id"] for case in selected}),
            "unique_branches": len({case["branch_id"] for case in selected}),
            "classifications": dict(sorted(Counter(
                case["replay_classification"] for case in selected
            ).items())),
        }
    summary = {
        "records": len(cases),
        "unique_companies_affected": len({case["company_id"] for case in cases}),
        "unique_branches_affected": len({case["branch_id"] for case in cases}),
        "root_causes": groups,
        "source_kinds": dict(sorted(Counter(
            case["source_kind"] for case in cases
        ).items())),
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "automatic_reclassifications_performed": 0,
    }
    json_path = output / "historical_replay_ambiguity_summary.json"
    csv_path = output / "historical_replay_ambiguity_cases.csv"
    markdown_path = output / "historical_replay_ambiguity_findings.md"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    columns = (
        "record_token", "source_path", "source_row_number", "source_kind",
        "replay_classification", "root_cause", "resolution_reason",
        "original_company_token", "original_branch_token",
        "candidate_company_count", "candidate_branch_count", "conflicting_evidence",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: case[key] for key in columns} for case in cases)
    lines = [
        "# Historical imported-to-unresolved diagnostic", "",
        f"Records: {summary['records']}",
        f"Unique companies affected: {summary['unique_companies_affected']}",
        f"Unique branches affected: {summary['unique_branches_affected']}",
        "", "| Root cause | Records | Companies | Branches |", "|---|---:|---:|---:|",
    ]
    for cause, values in groups.items():
        lines.append(
            f"| {cause} | {values['records']} | "
            f"{values['unique_companies']} | {values['unique_branches']} |"
        )
    lines.extend([
        "", "No records were automatically resolved, merged, or reclassified.",
        "Company and branch tokens in the case report are anonymized deterministic hashes.", "",
    ])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--production", type=Path, default=Path("data/company_registry.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/reports/phase_2b"))
    arguments = parser.parse_args()
    print(json.dumps(
        diagnose(arguments.production, arguments.output_dir),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
