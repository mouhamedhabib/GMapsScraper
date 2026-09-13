"""Resolve job-source relationships from exact stored ATS tenant evidence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sqlite3
from typing import Sequence
import unicodedata
from urllib.parse import unquote, urlsplit

from job_search.providers import ATS_PROVIDERS, detect_provider
from job_search.repair_reviews import _record_result
from job_search.storage import DEFAULT_DATABASE, connect_database


RELATIONSHIP_POLICY_VERSION = "source-relationship-v1"
RELATIONSHIPS = ("DIRECT", "RECRUITER", "AGGREGATOR", "UNKNOWN")
LEGAL_SUFFIXES = (
    "incorporated", "corporation", "limited", "company", "gmbh", "sarl",
    "corp", "llc", "ltd", "inc", "plc", "sas", "ag", "nv", "bv",
)
FUSED_LEGAL_SUFFIXES = (
    "incorporated", "corporation", "limited", "gmbh", "sarl", "corp",
    "llc", "ltd", "inc", "plc",
)


@dataclass(frozen=True)
class RelationshipResolution:
    job_id: int
    job_source_id: int
    provider: str
    source_type: str
    old_relationship: str
    relationship: str
    method: str
    tenant: str
    company: str
    changed: bool
    evidence: dict


def normalize_company_identity(value: str) -> str:
    """Return a conservative exact-comparison key; never perform fuzzy matching."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()
    tokens = re.findall(r"[a-z0-9]+", ascii_value)
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    compact = "".join(tokens)
    # ATS tenants commonly fuse a legal suffix into a single slug.
    for suffix in FUSED_LEGAL_SUFFIXES:
        if compact.endswith(suffix) and len(compact) - len(suffix) >= 3:
            compact = compact[:-len(suffix)]
            break
    return compact


def extract_ats_tenant(url: str, provider: str = "") -> str:
    provider = (provider or detect_provider(url)).casefold()
    if provider not in {"greenhouse", "lever", "ashby"}:
        return ""
    parts = [unquote(part).strip() for part in urlsplit(url).path.split("/") if part]
    return parts[0] if len(parts) >= 2 else ""


def resolve_source(row: sqlite3.Row) -> RelationshipResolution:
    url = row["source_url"] or ""
    detected_provider = detect_provider(url)
    provider = (row["provider"] or detected_provider or "generic").casefold()
    if provider == "generic" and detected_provider != "generic":
        provider = detected_provider
    old_relationship = (row["employer_relationship"] or "UNKNOWN").upper()
    old_source_type = (row["source_type"] or "UNKNOWN").upper()
    company = (row["company"] or "").strip()
    tenant = extract_ats_tenant(url, provider)
    normalized_company = normalize_company_identity(company)
    normalized_tenant = normalize_company_identity(tenant)
    is_ats = provider in ATS_PROVIDERS and bool(tenant)
    source_type = "ATS" if is_ats else old_source_type

    evidence = {
        "source_url": url,
        "stored_provider": row["provider"] or "",
        "detected_provider": detected_provider,
        "provider": provider,
        "tenant": tenant,
        "normalized_tenant": normalized_tenant,
        "company": company,
        "normalized_company": normalized_company,
        "old_source_type": old_source_type,
        "resolved_source_type": source_type,
        "old_relationship": old_relationship,
    }
    if old_relationship in {"DIRECT", "RECRUITER", "AGGREGATOR"}:
        relationship = old_relationship
        method = "PRESERVED_KNOWN_RELATIONSHIP"
    elif not is_ats:
        relationship = "UNKNOWN"
        method = "INSUFFICIENT_ATS_EVIDENCE"
    elif not normalized_company:
        relationship = "UNKNOWN"
        method = "COMPANY_MISSING"
    elif normalized_company == normalized_tenant:
        relationship = "DIRECT"
        method = "ATS_TENANT_COMPANY_MATCH"
    else:
        relationship = "UNKNOWN"
        method = "ATS_TENANT_COMPANY_CONFLICT"
    evidence.update({"relationship": relationship, "method": method})
    return RelationshipResolution(
        job_id=int(row["job_id"]), job_source_id=int(row["job_source_id"]),
        provider=provider, source_type=source_type,
        old_relationship=old_relationship, relationship=relationship,
        method=method, tenant=tenant, company=company,
        changed=(source_type != old_source_type or relationship != old_relationship),
        evidence=evidence,
    )


def _selected_sources(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None,
    run_id: str | None,
) -> list[sqlite3.Row]:
    if job_ids is None and run_id is None:
        raise ValueError("provide at least one --job-id or --run-id")
    clauses = []
    parameters: list[object] = []
    if job_ids is not None:
        if not job_ids:
            return []
        clauses.append("j.job_id IN (" + ",".join("?" for _ in job_ids) + ")")
        parameters.extend(job_ids)
    if run_id:
        if not connection.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone():
            raise ValueError(f"workflow run not found: {run_id}")
        clauses.append(
            "EXISTS (SELECT 1 FROM workflow_run_jobs wrj "
            "WHERE wrj.run_id=? AND wrj.job_id=j.job_id)"
        )
        parameters.append(run_id)
    return connection.execute(
        """SELECT j.job_id, COALESCE(c.canonical_name, '') AS company,
                  s.job_source_id, s.provider, s.source_url,
                  s.source_type, s.employer_relationship
           FROM jobs j
           LEFT JOIN companies c ON c.company_id=j.company_id
           JOIN job_sources s ON s.job_id=j.job_id
           WHERE """ + " AND ".join(clauses)
        + " ORDER BY j.job_id, s.job_source_id",
        parameters,
    ).fetchall()


def resolve_relationships(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, verbose: bool = False,
) -> list[RelationshipResolution]:
    """Resolve selected stored sources without any network dependency."""
    rows = _selected_sources(connection, job_ids, run_id)
    resolutions = [resolve_source(row) for row in rows]
    changed_by_job: dict[int, list[RelationshipResolution]] = {}
    for resolution in resolutions:
        if resolution.changed:
            changed_by_job.setdefault(resolution.job_id, []).append(resolution)
    for job_id, changes in changed_by_job.items():
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        field_changes = {}
        with connection:
            for item in changes:
                original_source_type = item.evidence["old_source_type"]
                connection.execute(
                    """UPDATE job_sources SET source_type=?, employer_relationship=?
                       WHERE job_source_id=?""",
                    (item.source_type, item.relationship, item.job_source_id),
                )
                if item.source_type != original_source_type:
                    field_changes[f"source[{item.job_source_id}].source_type"] = {
                        "old_value": original_source_type,
                        "new_value": item.source_type,
                        "source": item.method,
                        "method": item.method,
                        "evidence": item.evidence,
                    }
                if item.relationship != item.old_relationship:
                    field_changes[f"source[{item.job_source_id}].employer_relationship"] = {
                        "old_value": item.old_relationship,
                        "new_value": item.relationship,
                        "source": item.method,
                        "method": item.method,
                        "evidence": item.evidence,
                    }
            primary = changes[0]
            _record_result(
                connection, job_id, RELATIONSHIP_POLICY_VERSION, attempted_at,
                "REPAIRED", field_changes, source_type=primary.source_type,
                completion_status="SUCCESS",
                employer_relationship=primary.relationship,
            )
    if verbose:
        for item in resolutions:
            action = "changed" if item.changed else "unchanged"
            print(
                f"job_id={item.job_id} source_id={item.job_source_id} "
                f"{item.old_relationship}->{item.relationship} "
                f"source_type={item.source_type} method={item.method} {action}"
            )
    return resolutions


def print_summary(results: Sequence[RelationshipResolution]) -> None:
    print(f"Sources selected: {len(results)}")
    print(f"Sources changed: {sum(item.changed for item in results)}")
    for relationship in RELATIONSHIPS:
        print(f"{relationship}: {sum(item.relationship == relationship for item in results)}")
    for item in results:
        print()
        print(f"job_id: {item.job_id}")
        print(f"job_source_id: {item.job_source_id}")
        print(f"provider: {item.provider}")
        print(f"tenant: {item.tenant or '-'}")
        print(f"company: {item.company or '-'}")
        print(f"source_type: {item.source_type}")
        print(f"old_relationship: {item.old_relationship}")
        print(f"relationship: {item.relationship}")
        print(f"method: {item.method}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve source relationships from stored exact ATS evidence"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_id is None and args.run_id is None:
        parser.error("provide at least one --job-id or --run-id")
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_relationships(
                connection, args.job_id, args.run_id, args.verbose
            )
        except ValueError as error:
            parser.error(str(error))
    finally:
        connection.close()
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
