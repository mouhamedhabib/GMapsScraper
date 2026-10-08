"""Deterministic contact-role selection for qualified jobs.

This module selects role categories only. It performs no person discovery,
enrichment, scraping, outreach, or network access.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


CONTACT_POLICY_VERSION = "contact-strategy-v1"
CONTACT_CONFIDENCES = ("HIGH", "MEDIUM", "LOW")

EARLY_CAREERS_TERMS = re.compile(
    r"\b(?:junior|graduate|new[ -]?grad|intern(?:ship)?|apprentice|entry[ -]?level|campus)\b",
    re.I,
)
SPECIALIZED_ENGINEERING_TERMS = re.compile(
    r"\b(?:back[ -]?end|front[ -]?end|full[ -]?stack)\b",
    re.I,
)
SOFTWARE_ENGINEERING_TERMS = re.compile(
    r"\b(?:software|engineer(?:ing)?|developer|programmer)\b", re.I,
)
EXPLICIT_CONTACT_PATTERNS = (
    re.compile(
        r"\b(?i:recruiter|hiring contact|talent contact)\s*:\s*"
        r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+){1,3}\b"
    ),
    re.compile(
        r"\b(?i:contact|reach out to|email)\s+"
        r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+){1,3}"
        r"(?:\s*[,–—-]\s*(?:technical |campus |early careers |talent )?recruiter)?\b"
    ),
    re.compile(
        r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’+-]+){1,3}"
        r"\s*[,–—-]\s*(?i:technical recruiter|campus recruiter|early careers recruiter|hiring manager)\b",
    ),
)


@dataclass(frozen=True)
class ContactStrategy:
    job_id: int
    primary_role: str
    secondary_roles: tuple[str, ...]
    avoid_roles: tuple[str, ...]
    confidence: str
    rationale: str
    reason_codes: tuple[str, ...]
    resolved_at: str
    input_fingerprint: str = ""
    reused: bool = False


def _value(row: Mapping, name: str) -> str:
    try:
        return str(row[name] or "").strip()
    except (KeyError, IndexError):
        return ""


def _role_family(title: str) -> str:
    if EARLY_CAREERS_TERMS.search(title):
        return "EARLY_CAREERS"
    if SPECIALIZED_ENGINEERING_TERMS.search(title):
        return "SPECIALIZED_ENGINEERING"
    if SOFTWARE_ENGINEERING_TERMS.search(title):
        return "SOFTWARE_ENGINEERING"
    return "OTHER"


def _company_size(text: str) -> str:
    normalized = " ".join((text or "").split())
    if re.search(r"\b(?:small|tiny|early[ -]?stage)\s+(?:tech\s+)?(?:startup|start-up)\b", normalized, re.I):
        return "SMALL"
    counts = []
    for match in re.finditer(
        r"\b([0-9][0-9 ,.]*)(?:\+)?\s+(?:employees|people|team members|collaborateurs?)\b",
        normalized, re.I,
    ):
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            counts.append(int(digits))
    if counts and max(counts) <= 50:
        return "SMALL"
    if counts and max(counts) >= 1000:
        return "LARGE"
    return "STANDARD"


def _has_explicit_contact(text: str) -> bool:
    normalized = " ".join((text or "").split())
    return any(pattern.search(normalized) for pattern in EXPLICIT_CONTACT_PATTERNS)


def evaluate_contact_strategy(
    row: Mapping, sources: Sequence[Mapping] = (), *, resolved_at: str | None = None,
) -> ContactStrategy:
    """Return a bounded role strategy using only stored job/company evidence."""
    timestamp = resolved_at or utc_now()
    title = _value(row, "title")
    role_signal = " ".join(filter(None, (title, _value(row, "role_family"))))
    description = _value(row, "description")
    company_text = "\n".join(filter(None, (
        _value(row, "company"), _value(row, "company_website"), description,
    )))
    family = _role_family(role_signal)
    size = _company_size(company_text)
    relationships = {_value(source, "employer_relationship").upper() for source in sources}
    recruiter_hosted = "RECRUITER" in relationships
    explicit_contact = _has_explicit_contact(description)

    reason_codes: list[str] = []
    if family == "EARLY_CAREERS":
        if re.search(r"\b(?:intern(?:ship)?|campus)\b", title, re.I):
            primary = "Campus Recruiter"
            secondary = ["Early Careers Recruiter", "Engineering Manager"]
        else:
            primary = "Early Careers Recruiter"
            secondary = ["Engineering Manager"]
        reason_codes.append("CONTACT_EARLY_CAREERS")
    elif size == "SMALL":
        primary = "CTO"
        secondary = ["VP Engineering", "Technical Founder"]
        reason_codes.append("CONTACT_SMALL_STARTUP_TECH_LEADERSHIP")
    elif size == "LARGE":
        primary = "Technical Recruiter"
        secondary = ["Engineering Manager", "Talent Acquisition Partner"]
        reason_codes.append("CONTACT_LARGE_COMPANY_TECH_RECRUITING")
    elif family == "SPECIALIZED_ENGINEERING":
        primary = "Engineering Manager"
        secondary = ["Head of Engineering", "Technical Recruiter"]
        reason_codes.append("CONTACT_SPECIALIZED_ENGINEERING")
    else:
        primary = "Engineering Manager"
        secondary = ["Technical Recruiter"]
        reason_codes.append(
            "CONTACT_STANDARD_SOFTWARE_ENGINEERING"
            if family == "SOFTWARE_ENGINEERING" else "CONTACT_TECHNICAL_FALLBACK"
        )

    if explicit_contact:
        candidates = [primary, *secondary]
        primary = "EXPLICIT_CONTACT"
        secondary = candidates[:2]
        reason_codes.insert(0, "CONTACT_EXPLICIT_POSTING")

    avoid = ["CEO", "Unrelated Executives", "Generic HR"]
    reason_codes.append("AVOID_NON_TECHNICAL_CONTACTS")
    if recruiter_hosted:
        avoid.append("Recruiter Platform Staff (unless explicitly named)")
        reason_codes.append("CONTACT_RECRUITER_HOSTED_DISTINCTION")

    # Preserve order while enforcing the public contract's hard limits.
    secondary = list(dict.fromkeys(role for role in secondary if role != primary))[:2]
    avoid = list(dict.fromkeys(avoid))
    confidence = "HIGH" if family != "OTHER" or explicit_contact or size in {"SMALL", "LARGE"} else "MEDIUM"

    family_label = family.lower().replace("_", " ")
    size_label = size.lower()
    rationale = (
        f"The {family_label} role and {size_label} company evidence favor direct "
        f"technical hiring responsibility; {primary} is the strongest role category."
    )
    if explicit_contact:
        rationale += " The posting explicitly identifies a hiring contact, so that contact has highest priority."
    if recruiter_hosted:
        rationale += " The recruiter-hosted source is kept distinct from the actual employer contact search."

    return ContactStrategy(
        int(row["job_id"]), primary, tuple(secondary), tuple(avoid), confidence,
        rationale, tuple(reason_codes), timestamp,
    )


def _select_rows(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None,
    run_id: str | None, include_review: bool,
) -> list[sqlite3.Row]:
    clauses, parameters = [], []
    if job_ids is not None:
        if not job_ids:
            return []
        clauses.append("j.job_id IN (" + ",".join("?" for _ in job_ids) + ")")
        parameters.extend(job_ids)
    if run_id:
        if not connection.execute("SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError(f"workflow run not found: {run_id}")
        clauses.append(
            "EXISTS (SELECT 1 FROM workflow_run_jobs w WHERE w.run_id=? AND w.job_id=j.job_id)"
        )
        parameters.append(run_id)
    statuses = ("QUALIFIED", "REVIEW") if include_review else ("QUALIFIED",)
    clauses.append("q.qualification_status IN (" + ",".join("?" for _ in statuses) + ")")
    parameters.extend(statuses)
    return connection.execute(
        """SELECT j.job_id,j.canonical_url,j.title,j.description,j.content_hash,j.updated_at,
                  c.canonical_name AS company,c.website_url AS company_website,
                  q.qualification_id,q.policy_version AS qualification_policy_version,
                  q.qualification_status,q.input_evidence_hash,q.updated_at AS qualification_updated_at
           FROM jobs j
           JOIN job_qualifications q ON q.qualification_id=(
               SELECT q2.qualification_id FROM job_qualifications q2
               WHERE q2.job_id=j.job_id ORDER BY q2.qualification_id DESC LIMIT 1
           )
           LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE """ + " AND ".join(clauses) + " ORDER BY j.job_id",
        parameters,
    ).fetchall()


def _sources(connection: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT job_source_id,provider,source_url,apply_url,source_type,
                  employer_relationship,last_fetched_at,raw_content_hash
           FROM job_sources WHERE job_id=? ORDER BY job_source_id""",
        (job_id,),
    ).fetchall()


def _fingerprint(row: Mapping, sources: Sequence[Mapping]) -> str:
    payload = {"job": dict(row), "sources": [dict(source) for source in sources]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def _from_stored(row: sqlite3.Row) -> ContactStrategy:
    return ContactStrategy(
        row["job_id"], row["primary_role"], tuple(json.loads(row["secondary_roles_json"])),
        tuple(json.loads(row["avoid_roles_json"])), row["confidence"], row["rationale"],
        tuple(json.loads(row["reason_codes_json"])), row["resolved_at"],
        row["input_fingerprint"], True,
    )


def _persist(connection: sqlite3.Connection, result: ContactStrategy) -> None:
    secondary = json.dumps(result.secondary_roles, ensure_ascii=False, separators=(",", ":"))
    avoid = json.dumps(result.avoid_roles, ensure_ascii=False, separators=(",", ":"))
    reasons = json.dumps(result.reason_codes, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """INSERT INTO job_contact_strategies
           (job_id,policy_version,primary_role,secondary_roles_json,avoid_roles_json,
            confidence,rationale,reason_codes_json,input_fingerprint,resolved_at,
            created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id,policy_version) DO UPDATE SET
             primary_role=excluded.primary_role,
             secondary_roles_json=excluded.secondary_roles_json,
             avoid_roles_json=excluded.avoid_roles_json,
             confidence=excluded.confidence,rationale=excluded.rationale,
             reason_codes_json=excluded.reason_codes_json,
             input_fingerprint=excluded.input_fingerprint,
             resolved_at=excluded.resolved_at,updated_at=excluded.updated_at""",
        (
            result.job_id, CONTACT_POLICY_VERSION, result.primary_role, secondary, avoid,
            result.confidence, result.rationale, reasons, result.input_fingerprint,
            result.resolved_at, result.resolved_at, result.resolved_at,
        ),
    )


def resolve_contact_strategies(
    connection: sqlite3.Connection, job_ids: Sequence[int] | None = None,
    run_id: str | None = None, include_review: bool = False,
    verbose: bool = False,
) -> list[ContactStrategy]:
    results = []
    for row in _select_rows(connection, job_ids, run_id, include_review):
        sources = _sources(connection, row["job_id"])
        fingerprint = _fingerprint(row, sources)
        existing = connection.execute(
            """SELECT * FROM job_contact_strategies
               WHERE job_id=? AND policy_version=? AND input_fingerprint=?""",
            (row["job_id"], CONTACT_POLICY_VERSION, fingerprint),
        ).fetchone()
        if existing:
            result = _from_stored(existing)
        else:
            evaluated = evaluate_contact_strategy(row, sources)
            result = ContactStrategy(
                evaluated.job_id, evaluated.primary_role, evaluated.secondary_roles,
                evaluated.avoid_roles, evaluated.confidence, evaluated.rationale,
                evaluated.reason_codes, evaluated.resolved_at, fingerprint,
            )
            with connection:
                _persist(connection, result)
        results.append(result)
        if verbose:
            state = "reused" if result.reused else "resolved"
            print(f"{result.job_id}: {state} {result.primary_role} ({result.confidence})")
    return results


def print_results(results: Sequence[ContactStrategy]) -> None:
    counts = Counter(result.confidence for result in results)
    print(f"Jobs selected: {len(results)}")
    for confidence in CONTACT_CONFIDENCES:
        print(f"{confidence}: {counts[confidence]}")
    for result in results:
        print(f"\njob_id: {result.job_id}")
        print(f"PRIMARY_CONTACT_ROLE: {result.primary_role}")
        print("SECONDARY_CONTACT_ROLE: " + (", ".join(result.secondary_roles) or "-"))
        print("AVOID_ROLES: " + (", ".join(result.avoid_roles) or "-"))
        print(f"confidence: {result.confidence}")
        print(f"rationale: {result.rationale}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic contact-role strategy for qualified jobs")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--job-id", type=int, action="append")
    parser.add_argument("--run-id")
    parser.add_argument("--include-review", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    connection = connect_database(args.database)
    try:
        try:
            results = resolve_contact_strategies(
                connection, args.job_id, args.run_id, args.include_review, args.verbose,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    finally:
        connection.close()
    print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
