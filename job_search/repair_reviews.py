"""Deterministically complete missing or demonstrably invalid REVIEW job data.

The command fetches only each selected job's stored canonical URL.  It never
crawls links, changes job/source identity, or overwrites a nonblank value.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from job_search.filtering import (
    DEFAULT_POLICY_VERSION,
    evaluate_job,
    extract_experience,
    persist_result,
)
from job_search.providers import (
    ParsedJob, classify_source_context, fetch_job, is_safe_company_name,
    infer_trusted_company, is_safe_location_value, is_valid_job_title,
    parse_job_html,
)
from job_search.storage import DEFAULT_DATABASE, connect_database


REPAIR_FIELDS = (
    "title", "location_text", "country", "city", "region", "description", "published_at",
    "employment_type",
)
DISPLAY_FIELDS = {
    "title": "title",
    "company_name": "company",
    "location_text": "location",
    "description": "description",
    "published_at": "published_at",
    "country": "country",
    "city": "city",
    "region": "region",
    "employment_type": "employment_type",
    "source_type": "source_type",
    "employer_relationship": "employer_relationship",
}
SOURCE_PRIORITY = (
    "provider_title_field", "provider_company_field", "provider_location_field",
    "provider_description_field", "provider_date_field", "provider_employment_field",
    "jsonld_title", "jsonld_hiringOrganization", "jsonld_jobLocation", "jsonld_description",
    "jsonld_datePosted", "jsonld_employmentType", "microdata_hiringOrganization",
    "microdata_jobLocation", "microdata_description", "microdata_datePosted",
    "microdata_employmentType", "job_title_meta", "job_description_meta",
    "job_company_meta", "job_location_meta", "html_job_heading",
    "open_graph_site_name", "PAGE_METADATA", "PAGE_CONTENT",
    "direct_domain_company", "validated_hosted_tenant_title",
    "title_structured_location", "url_structured_location",
    "deterministic_geography", "url_structured_title",
    "JSON_LD", "MICRODATA", "PROVIDER_STRUCTURED", "TITLE_PATTERN",
    "URL_PATTERN", "UNKNOWN",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _valid_date(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _selected_reviews(connection, policy_version, job_ids=None, limit=None, include_finalized=False):
    parameters: list[object] = [policy_version]
    query = """SELECT j.*, c.canonical_name AS company_name,
                      (SELECT s.provider FROM job_sources s
                       WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1)
                      AS source_provider,
                      (SELECT s.job_source_id FROM job_sources s
                       WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1)
                      AS job_source_id,
                      (SELECT s.source_type FROM job_sources s
                       WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1)
                      AS stored_source_type,
                      (SELECT s.employer_relationship FROM job_sources s
                       WHERE s.job_id=j.job_id ORDER BY s.job_source_id LIMIT 1)
                      AS stored_employer_relationship
               FROM job_filter_results f
               JOIN jobs j ON j.job_id=f.job_id
               LEFT JOIN companies c ON c.company_id=j.company_id
               WHERE f.policy_version=? AND f.status='REVIEW'"""
    if not include_finalized:
        query += """ AND NOT EXISTS (
                       SELECT 1 FROM job_repair_results final
                       WHERE final.job_id=j.job_id AND final.final_pass=1
                   )"""
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        query += f" AND j.job_id IN ({placeholders})"
        parameters.extend(job_ids)
    query += " ORDER BY j.job_id"
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    return connection.execute(query, parameters).fetchall()


def _method(source: str) -> str:
    if source == "SOURCE_CLASSIFICATION":
        return "SOURCE_CLASSIFICATION"
    if source.startswith("provider_"):
        return "PROVIDER_STRUCTURED"
    if source.startswith("jsonld_") or source == "JSON_LD":
        return "JSON_LD"
    if source.startswith("microdata_") or source == "MICRODATA":
        return "MICRODATA"
    if source.startswith("job_"):
        return "JOB_META"
    if source == "direct_domain_company":
        return "DIRECT_DOMAIN"
    if source == "validated_hosted_tenant_title":
        return "HOSTED_TENANT_TITLE"
    if source in {"html_job_heading", "PAGE_CONTENT"}:
        return "HTML_LABEL"
    if source in {"title_structured_location", "deterministic_geography", "TITLE_PATTERN"}:
        return "TITLE_INFERENCE"
    if source in {"url_structured_location", "url_structured_title", "URL_PATTERN"}:
        return "URL_INFERENCE"
    return "UNKNOWN"


def _change(old_value, new_value, source):
    return {
        "old_value": "" if old_value is None else str(old_value),
        "new_value": "" if new_value is None else str(new_value),
        "source": source or "UNKNOWN",
        "method": _method(source or "UNKNOWN"),
    }


def _candidate_fields(row, parsed: ParsedJob) -> dict[str, dict[str, str]]:
    """Choose blank fills and demonstrably invalid repairs without weak overwrites."""
    fields = {}
    company_source = parsed.evidence_sources.get("company_name", "UNKNOWN")
    old_company = row["company_name"]
    new_company = str(parsed.company_name or "").strip()
    valid_new_company = bool(new_company and is_safe_company_name(new_company, company_source))
    invalid_old_company = not _blank(old_company) and not is_safe_company_name(old_company)
    if (_blank(old_company) or invalid_old_company) and valid_new_company and str(old_company or "").strip() != new_company:
        fields["company_name"] = _change(old_company, new_company, company_source)
    elif invalid_old_company:
        fields["company_name"] = _change(old_company, "", "DETERMINISTIC_VALIDATION")
    for field_name in REPAIR_FIELDS:
        value = getattr(parsed, field_name, "")
        value = str(value or "").strip()
        source = parsed.evidence_sources.get(field_name, "UNKNOWN")
        valid = (
            is_valid_job_title(value, parsed.canonical_url) if field_name == "title"
            else is_safe_location_value(value, source, city=field_name == "city")
            if field_name in {"location_text", "city", "region"}
            else field_name != "published_at" or _valid_date(value)
        )
        old = row[field_name]
        invalid_old = (
            field_name == "title" and not _blank(old)
            and not is_valid_job_title(old, row["canonical_url"])
        ) or (
            field_name in {"location_text", "city", "region"} and not _blank(old)
            and not is_safe_location_value(old, city=field_name == "city")
        ) or (
            field_name == "description" and not _blank(old)
            and len(str(old).strip()) < 80 and len(value) >= 200
            and _method(source) in {"PROVIDER_STRUCTURED", "JSON_LD", "MICRODATA", "HTML_LABEL"}
        )
        if (_blank(old) or invalid_old) and value and valid and str(old or "").strip() != value:
            fields[field_name] = _change(old, value, source)
        elif invalid_old and field_name in {"location_text", "city", "region"}:
            fields[field_name] = _change(old, "", "DETERMINISTIC_VALIDATION")
    return fields


def _source_type(parsed: ParsedJob, fields: dict[str, str]) -> str:
    sources = {
        parsed.evidence_sources.get(field_name, "UNKNOWN") for field_name in fields
    }
    ordered = [source for source in SOURCE_PRIORITY if source in sources]
    return ",".join(ordered) or "UNKNOWN"


def _is_protection_failure(parsed: ParsedJob) -> bool:
    error = (parsed.fetch_error or "").casefold()
    return any(marker in error for marker in (
        "403", "401", "429", "forbidden", "captcha", "challenge", "cloudflare",
    ))


def _browser_fetch(url, timeout, driver):
    """Load exactly one URL in the existing browser session."""
    try:
        from selenium.webdriver.support.ui import WebDriverWait

        driver.set_page_load_timeout(timeout)
        driver.get(url)
        WebDriverWait(driver, timeout).until(
            lambda current: bool(current.page_source)
        )
        html = driver.page_source or ""
        folded = html.casefold()
        if not html.strip() or "cf-chl-" in folded or "just a moment" in folded:
            return None
        return parse_job_html(url, html)
    except Exception:
        return None


def _company_id(connection, row, name, now):
    """Return a company id while preserving every existing nonblank value."""
    name = name.strip()
    if row["company_id"] is not None:
        connection.execute(
            """UPDATE companies SET canonical_name=?, updated_at=?
               WHERE company_id=? AND (canonical_name IS NULL OR trim(canonical_name)='')""",
            (name, now, row["company_id"]),
        )
        return row["company_id"]
    existing = connection.execute(
        """SELECT company_id FROM companies
           WHERE canonical_name=? COLLATE NOCASE ORDER BY company_id LIMIT 1""",
        (name,),
    ).fetchone()
    if existing:
        return existing["company_id"]
    cursor = connection.execute(
        """INSERT INTO companies
           (canonical_name, first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (name, now, now, now, now),
    )
    return cursor.lastrowid


def _record_result(
    connection, job_id, policy_version, attempted_at, status, fields,
    source_type="", failure_reason="", final_pass=False,
    completion_status=None, experience_evidence=None,
    employer_relationship="UNKNOWN",
):
    legacy_fields = {
        name: details.get("source", "UNKNOWN")
        for name, details in fields.items() if isinstance(details, dict)
    }
    payload = json.dumps(legacy_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    changes_payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    experience_payload = json.dumps(
        experience_evidence or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    connection.execute(
        """INSERT INTO job_repair_results
           (job_id, policy_version, attempted_at, status, fields_filled_json,
            source_type, failure_reason, created_at, updated_at, final_pass,
            completion_status, field_changes_json, experience_evidence_json,
            employer_relationship)
           VALUES (?, ?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id, policy_version, attempted_at) DO UPDATE SET
             status=excluded.status, fields_filled_json=excluded.fields_filled_json,
             source_type=excluded.source_type, failure_reason=excluded.failure_reason,
             final_pass=excluded.final_pass,
             completion_status=excluded.completion_status,
             field_changes_json=excluded.field_changes_json,
             experience_evidence_json=excluded.experience_evidence_json,
             employer_relationship=excluded.employer_relationship,
             updated_at=excluded.updated_at""",
        (job_id, policy_version, attempted_at, status, payload, source_type,
         failure_reason[:1000], attempted_at, attempted_at, int(final_pass),
         completion_status, changes_payload, experience_payload,
         employer_relationship),
    )


def _experience_evidence(text, source):
    return [
        {
            "minimum": item.minimum, "maximum": item.maximum,
            "preferred": item.preferred, "text": item.text,
            "source": source,
        }
        for item in extract_experience(text or "")
    ]


def _row_for_filter(connection, job_id):
    return connection.execute(
        """SELECT j.*, c.canonical_name AS company_name,
                  (SELECT s.provider FROM job_sources s WHERE s.job_id=j.job_id
                   ORDER BY s.job_source_id LIMIT 1) AS source_provider
           FROM jobs j LEFT JOIN companies c ON c.company_id=j.company_id
           WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()


def _repair_provenance(connection, job_id, policy_version):
    """Return the newest recorded repair source for every field."""
    provenance = {}
    rows = connection.execute(
        """SELECT fields_filled_json FROM job_repair_results
           WHERE job_id=? AND policy_version=? AND status='REPAIRED'
           ORDER BY attempted_at DESC, repair_id DESC""",
        (job_id, policy_version),
    ).fetchall()
    for row in rows:
        try:
            fields = json.loads(row["fields_filled_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(fields, list):
            fields = {name: "UNKNOWN" for name in fields}
        if not isinstance(fields, dict):
            continue
        for name, source in fields.items():
            provenance.setdefault(name, str(source or "UNKNOWN"))
    return provenance


def cleanup_invalid_repairs(
    connection, job_ids, policy_version=DEFAULT_POLICY_VERSION,
    refilter=False, verbose=False,
):
    """Clear only invalid values whose fields were previously repair-filled."""
    summary = {"selected": 0, "cleaned": 0, "no_change": 0, "fields": Counter(), "refilter": Counter()}
    for job_id in dict.fromkeys(job_ids or ()):
        row = _row_for_filter(connection, job_id)
        if row is None:
            continue
        summary["selected"] += 1
        provenance = _repair_provenance(connection, job_id, policy_version)
        cleared = {}
        company_source = provenance.get("company_name") or provenance.get("company")
        if company_source and not _blank(row["company_name"]) and not is_safe_company_name(row["company_name"], company_source):
            cleared["company_name"] = {
                "value": row["company_name"], "source": company_source,
            }
        for name in ("location_text", "city", "region"):
            source = provenance.get(name) or (provenance.get("location") if name == "location_text" else None)
            if source and not _blank(row[name]) and not is_safe_location_value(row[name], source, city=name == "city"):
                cleared[name] = {"value": row[name], "source": source}
        if not cleared:
            summary["no_change"] += 1
            if verbose:
                print(f"{job_id}: no invalid repair-derived values")
            continue
        cleaned_at = _now()
        with connection:
            assignments = []
            if "company_name" in cleared:
                assignments.append("company_id=NULL")
            assignments.extend(
                f"{name}=NULL" for name in ("location_text", "city", "region")
                if name in cleared
            )
            assignments.append("updated_at=?")
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?",
                (cleaned_at, job_id),
            )
            connection.execute(
                """INSERT INTO job_repair_cleanups
                   (job_id, policy_version, cleaned_at, fields_cleared_json,
                    reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, policy_version, cleaned_at,
                 json.dumps(cleared, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 "Current deterministic validation rejected repair-derived value",
                 cleaned_at, cleaned_at),
            )
            if refilter:
                current = _row_for_filter(connection, job_id)
                decision = evaluate_job(current)
                persist_result(connection, job_id, policy_version, decision, cleaned_at)
                summary["refilter"][decision.status] += 1
        summary["cleaned"] += 1
        for name in cleared:
            summary["fields"][name] += 1
        if verbose:
            print(f"{job_id}: cleared {', '.join(cleared)}")
    return summary


def print_cleanup_summary(summary):
    print(f"Cleanup jobs selected: {summary['selected']}")
    print(f"Cleaned: {summary['cleaned']}")
    print(f"No change: {summary['no_change']}")
    print("Fields cleared:")
    for name in ("company_name", "location_text", "city", "region"):
        print(f"  {name}: {summary['fields'][name]}")
    if summary["refilter"]:
        print("After re-filter:")
        for status in ("PASS", "REVIEW", "REJECT"):
            print(f"{status}: {summary['refilter'][status]}")


def _apply_repair(
    connection, row, parsed, fields, policy_version, attempted_at, refilter,
    final_pass, completion_status, experience_evidence, employer_relationship,
    legacy_status="REPAIRED",
):
    updates = {}
    now = attempted_at
    if "company_name" in fields:
        company_name = fields["company_name"]["new_value"]
        updates["company_id"] = (
            _company_id(connection, row, company_name, now) if company_name else None
        )
    for field_name in REPAIR_FIELDS:
        if field_name in fields:
            updates[field_name] = fields[field_name]["new_value"] or None
    if updates:
        assignments = ", ".join(f"{name}=?" for name in updates)
        connection.execute(
            f"UPDATE jobs SET {assignments}, updated_at=? WHERE job_id=?",
            (*updates.values(), now, row["job_id"]),
        )
    if row["job_source_id"] is not None:
        connection.execute(
            """UPDATE job_sources SET source_type=?, employer_relationship=?
               WHERE job_source_id=?""",
            (
                fields.get("source_type", {}).get("new_value") or row["stored_source_type"] or "UNKNOWN",
                fields.get("employer_relationship", {}).get("new_value")
                or row["stored_employer_relationship"] or "UNKNOWN",
                row["job_source_id"],
            ),
        )
    source_type = _source_type(parsed, fields)
    _record_result(
        connection, row["job_id"], policy_version, attempted_at, legacy_status,
        fields, source_type, final_pass=final_pass,
        completion_status=completion_status,
        experience_evidence=experience_evidence,
        employer_relationship=employer_relationship,
    )
    decision = None
    if refilter:
        current = _row_for_filter(connection, row["job_id"])
        decision = evaluate_job(current)
        persist_result(connection, row["job_id"], policy_version, decision, attempted_at)
    return decision


def repair_review_jobs(
    connection: sqlite3.Connection, policy_version=DEFAULT_POLICY_VERSION,
    limit=None, job_ids=None, timeout=15, browser_fallback=False,
    windowed=False, verbose=False, refilter=False, fetcher=fetch_job,
    driver_factory=None, browser_fetcher=_browser_fetch, final_pass=False,
    include_finalized=False,
):
    """Repair selected REVIEW jobs and return counters for the CLI/tests."""
    rows = _selected_reviews(
        connection, policy_version, job_ids, limit, include_finalized,
    )
    summary = {
        "selected": len(rows), "attempted": 0, "repaired": 0,
        "no_change": 0, "failed": 0, "blocked": 0,
        "fields": Counter(), "refilter": Counter(), "experience_evidence": 0,
    }
    driver = None
    try:
        for row in rows:
            summary["attempted"] += 1
            attempted_at = _now()
            try:
                parsed = fetcher(row["canonical_url"], timeout=timeout)
            except Exception as error:
                parsed = ParsedJob(row["canonical_url"], row["source_provider"] or "generic")
                parsed.fetch_status = "FAILED"
                parsed.fetch_error = f"{type(error).__name__}: {error}"

            if not parsed.company_name:
                company, source = infer_trusted_company(
                    row["canonical_url"], row["title"] or parsed.title,
                )
                if company:
                    parsed.company_name = company
                    parsed.evidence_sources["company_name"] = source

            fields = _candidate_fields(row, parsed)
            needs_browser = parsed.fetch_status == "FAILED" or not fields
            if needs_browser and browser_fallback:
                try:
                    if driver is None:
                        if driver_factory is None:
                            from utils.google_search_discovery import create_chrome_driver
                            driver_factory = create_chrome_driver
                        driver = driver_factory(windowed=windowed)
                    browser_result = browser_fetcher(row["canonical_url"], timeout, driver)
                except Exception:
                    browser_result = None
                if browser_result is not None:
                    parsed = browser_result
                    fields = _candidate_fields(row, parsed)

            relationship_company = parsed.company_name or row["company_name"] or ""
            context = classify_source_context(
                row["canonical_url"], row["source_provider"] or parsed.provider,
                relationship_company,
            )
            if (row["stored_source_type"] or "") != context.source_type:
                fields["source_type"] = _change(
                    row["stored_source_type"], context.source_type,
                    "SOURCE_CLASSIFICATION",
                )
            if (row["stored_employer_relationship"] or "") != context.employer_relationship:
                fields["employer_relationship"] = _change(
                    row["stored_employer_relationship"], context.employer_relationship,
                    "SOURCE_CLASSIFICATION",
                )
            evidence_text = parsed.description or row["description"] or ""
            evidence_source = (
                parsed.evidence_sources.get("description", "UNKNOWN")
                if parsed.description else "EXISTING_STORED_VALUE"
            )
            experience_evidence = _experience_evidence(evidence_text, evidence_source)
            if experience_evidence:
                summary["experience_evidence"] += 1

            relationship_only = bool(fields) and set(fields) <= {
                "source_type", "employer_relationship",
            }
            if relationship_only and parsed.fetch_status == "FAILED":
                completion_status = "BLOCKED" if _is_protection_failure(parsed) else "FAILED"
                try:
                    with connection:
                        _apply_repair(
                            connection, row, parsed, fields, policy_version,
                            attempted_at, False, final_pass, completion_status,
                            experience_evidence, context.employer_relationship,
                            legacy_status=completion_status,
                        )
                except Exception as error:
                    with connection:
                        _record_result(
                            connection, row["job_id"], policy_version, attempted_at,
                            "FAILED", {}, failure_reason=f"{type(error).__name__}: {error}",
                            final_pass=final_pass, completion_status="FAILED",
                            experience_evidence=experience_evidence,
                            employer_relationship=context.employer_relationship,
                        )
                    summary["failed"] += 1
                    continue
                summary["blocked" if completion_status == "BLOCKED" else "failed"] += 1
                for name in fields:
                    summary["fields"][DISPLAY_FIELDS[name]] += 1
                if verbose:
                    print(f'{row["job_id"]}: {completion_status.lower()} - {parsed.fetch_error}')
                continue

            if fields:
                projected = lambda name: fields.get(name, {}).get("new_value", row[name])
                core_missing = any((
                    _blank(projected("title")),
                    _blank(projected("company_name")),
                    not any(not _blank(projected(name)) for name in ("location_text", "city", "region", "country")),
                    _blank(projected("description")),
                ))
                completion_status = (
                    "PARTIAL" if parsed.fetch_status == "FAILED" or core_missing else "SUCCESS"
                )
                try:
                    with connection:
                        decision = _apply_repair(
                            connection, row, parsed, fields, policy_version,
                            attempted_at, refilter, final_pass, completion_status,
                            experience_evidence, context.employer_relationship,
                        )
                except Exception as error:
                    # The failed transaction has rolled back all job/company/filter
                    # writes. Persist only the audit failure in a fresh transaction.
                    with connection:
                        _record_result(
                            connection, row["job_id"], policy_version, attempted_at,
                            "FAILED", {}, failure_reason=f"{type(error).__name__}: {error}",
                            final_pass=final_pass, completion_status="FAILED",
                            experience_evidence=experience_evidence,
                            employer_relationship=context.employer_relationship,
                        )
                    summary["failed"] += 1
                    if verbose:
                        print(f'{row["job_id"]}: failed; row left unchanged')
                    continue
                summary["repaired"] += 1
                for name in fields:
                    summary["fields"][DISPLAY_FIELDS[name]] += 1
                if decision is not None:
                    summary["refilter"][decision.status] += 1
                if verbose:
                    print(f'job_id: {row["job_id"]}')
                    for name, value in fields.items():
                        label = DISPLAY_FIELDS[name]
                        shown = "filled" if name == "description" else value["new_value"]
                        print(f'{label}: {value["old_value"]!r} -> {shown}')
                continue

            protected = _is_protection_failure(parsed)
            if parsed.fetch_status == "FAILED":
                status = "BLOCKED" if protected else "FAILED"
                reason = parsed.fetch_error or "HTTP fetch failed"
            elif needs_browser and browser_fallback:
                status = "BLOCKED"
                reason = "Browser fallback returned no usable job fields"
            else:
                status = "NO_CHANGE"
                reason = "No valid missing fields found"
            with connection:
                _record_result(
                    connection, row["job_id"], policy_version, attempted_at,
                    status, {}, failure_reason=reason,
                    final_pass=final_pass,
                    completion_status=status,
                    experience_evidence=experience_evidence,
                    employer_relationship=context.employer_relationship,
                )
            key = {"NO_CHANGE": "no_change", "FAILED": "failed", "BLOCKED": "blocked"}[status]
            summary[key] += 1
            if verbose:
                print(f'{row["job_id"]}: {status.lower()} - {reason}')
    finally:
        if driver is not None:
            driver.quit()
    return summary


def print_summary(summary):
    print(f"Review jobs selected: {summary['selected']}")
    print(f"Attempted: {summary['attempted']}")
    print(f"Repaired: {summary['repaired']}")
    print(f"No change: {summary['no_change']}")
    print(f"Failed: {summary['failed']}")
    print(f"Blocked: {summary['blocked']}")
    print("Fields filled:")
    for field_name in ("title", "company", "location", "description", "published_at", "country", "city", "region", "employment_type", "source_type", "employer_relationship"):
        print(f"  {field_name}: {summary['fields'][field_name]}")
    print(f"Jobs with explicit experience evidence: {summary['experience_evidence']}")
    if summary["refilter"]:
        print("After re-filter:")
        for status in ("PASS", "REVIEW", "REJECT"):
            print(f"{status}: {summary['refilter'][status]}")


def build_parser():
    parser = argparse.ArgumentParser(description="Complete deterministic job data on REVIEW jobs only")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--job-id", type=int, action="append", dest="job_ids")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--browser-fallback", action="store_true")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--refilter", action="store_true")
    parser.add_argument(
        "--include-finalized", action="store_true",
        help="Include REVIEW rows previously marked as a legacy final repair pass",
    )
    parser.add_argument(
        "--final-pass", action="store_true",
        help="Attempt explicit REVIEW job ids once and exclude them from later repairs",
    )
    parser.add_argument(
        "--cleanup-invalid", action="store_true",
        help="Clear invalid repair-derived values for explicit --job-id targets",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    if args.windowed and not args.browser_fallback:
        raise SystemExit("--windowed requires --browser-fallback")
    if args.cleanup_invalid and not args.job_ids:
        raise SystemExit("--cleanup-invalid requires at least one --job-id")
    if args.final_pass and not args.job_ids:
        raise SystemExit("--final-pass requires at least one --job-id")
    if args.final_pass and args.cleanup_invalid:
        raise SystemExit("--final-pass cannot be combined with --cleanup-invalid")
    connection = connect_database(args.database)
    try:
        if args.cleanup_invalid:
            summary = cleanup_invalid_repairs(
                connection, args.job_ids, args.policy_version,
                args.refilter, args.verbose,
            )
        else:
            summary = repair_review_jobs(
                connection, args.policy_version, args.limit, args.job_ids,
                args.timeout, args.browser_fallback, args.windowed, args.verbose,
                args.refilter or args.final_pass, final_pass=args.final_pass,
                include_finalized=args.include_finalized,
            )
        decisions = []
        if args.final_pass:
            for job_id in dict.fromkeys(args.job_ids):
                row = connection.execute(
                    """SELECT status, reasons_json FROM job_filter_results
                       WHERE job_id=? AND policy_version=?""",
                    (job_id, args.policy_version),
                ).fetchone()
                decisions.append((job_id, row))
    finally:
        connection.close()
    if args.cleanup_invalid:
        print_cleanup_summary(summary)
    else:
        print_summary(summary)
        if args.final_pass:
            print("Final decisions:")
            for job_id, row in decisions:
                if row is None:
                    print(f"job {job_id} -> NOT_FOUND")
                    continue
                reasons = json.loads(row["reasons_json"] or "[]")
                labels = "; ".join(
                    item.get("code", "UNKNOWN") for item in reasons
                    if isinstance(item, dict)
                )
                print(f"job {job_id} -> {row['status']}: {labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
