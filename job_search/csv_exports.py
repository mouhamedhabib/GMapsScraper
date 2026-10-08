"""Deterministic, run-scoped CSV exports for Jobs and Maps."""

from __future__ import annotations

from csv import DictWriter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from job_search.filtering import DEFAULT_POLICY_VERSION
from job_search.review_priority import load_review_priorities


LOCAL_TIMEZONE = ZoneInfo("Africa/Tunis")
DEFAULT_EXPORT_DIRECTORY = Path("CSV_FILES/exports")

JOB_FIELDS = (
    "run_id", "run_status", "job_id", "observation_status", "title",
    "company", "location", "city", "region", "country", "provider",
    "source_type", "employer_relationship", "published_at",
    "filter_decision", "filter_reasons", "review_priority",
    "priority_reasons", "qualification_status", "application_channel",
    "application_url", "job_url",
)

MAPS_FALLBACK_FIELDS = (
    "title", "map_link", "cover_image", "rating", "privacy_price",
    "category", "address", "source_query", "country", "city", "location",
    "working_hours", "menu_link", "webpage", "phone_number",
    "related_images", "latitude", "longitude", "site_email", "added_at",
)


def _timestamp(value=None) -> datetime:
    if value is None:
        return datetime.now(LOCAL_TIMEZONE)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _atomic_csv(path: Path, fieldnames, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            "w", newline="", encoding="utf-8-sig", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = DictWriter(
                handle, fieldnames=fieldnames, extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _update_latest(timestamped_path: Path, latest_path: Path) -> None:
    """Atomically replace latest only after the timestamped export exists."""
    temporary = None
    try:
        with timestamped_path.open("rb") as source, NamedTemporaryFile(
            "wb", dir=latest_path.parent, prefix=f".{latest_path.name}.",
            suffix=".tmp", delete=False,
        ) as target:
            temporary = Path(target.name)
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        os.replace(temporary, latest_path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _reason_codes(value) -> str:
    try:
        payload = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    return ";".join(
        str(item.get("code") or "").strip()
        for item in payload
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    )


def job_export_rows(connection, run_id: str) -> tuple[list[dict], str]:
    run = connection.execute(
        "SELECT status, started_at, finished_at FROM workflow_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"workflow run not found: {run_id}")
    priorities = {
        item.job_id: item
        for item in load_review_priorities(
            connection, DEFAULT_POLICY_VERSION, run_id=run_id,
        )
    }
    records = connection.execute(
        """SELECT w.run_id, w.discovery_state, j.job_id, j.title,
                  COALESCE(c.canonical_name, '') AS company,
                  j.location_text, j.city, j.region, j.country, j.published_at,
                  j.canonical_url,
                  COALESCE(s.provider, '') AS provider,
                  COALESCE(s.source_type, '') AS source_type,
                  COALESCE(s.employer_relationship, '') AS employer_relationship,
                  COALESCE(f.status, '') AS filter_decision,
                  COALESCE(f.reasons_json, '') AS filter_reasons_json,
                  COALESCE(q.qualification_status, '') AS qualification_status,
                  COALESCE(q.application_channel, ad.application_channel, '')
                      AS application_channel,
                  COALESCE(q.application_url, ad.application_url, '')
                      AS application_url
           FROM workflow_run_jobs w
           JOIN jobs j ON j.job_id=w.job_id
           LEFT JOIN companies c ON c.company_id=j.company_id
           LEFT JOIN job_filter_results f
             ON f.job_id=j.job_id AND f.policy_version=?
           LEFT JOIN job_qualifications q
             ON q.job_id=j.job_id AND q.policy_version=?
           LEFT JOIN job_application_destinations ad
             ON ad.job_id=j.job_id AND ad.policy_version=?
           LEFT JOIN job_sources s ON s.job_source_id=(
               SELECT candidate.job_source_id FROM job_sources candidate
               WHERE candidate.job_id=j.job_id
               ORDER BY candidate.job_source_id LIMIT 1
           )
           WHERE w.run_id=? ORDER BY j.job_id""",
        (DEFAULT_POLICY_VERSION, DEFAULT_POLICY_VERSION,
         DEFAULT_POLICY_VERSION, run_id),
    ).fetchall()
    rows = []
    for record in records:
        priority = priorities.get(record["job_id"])
        rows.append({
            "run_id": record["run_id"],
            "run_status": run["status"],
            "job_id": record["job_id"],
            "observation_status": record["discovery_state"],
            "title": record["title"] or "",
            "company": record["company"],
            "location": record["location_text"] or "",
            "city": record["city"] or "",
            "region": record["region"] or "",
            "country": record["country"] or "",
            "provider": record["provider"],
            "source_type": record["source_type"],
            "employer_relationship": record["employer_relationship"],
            "published_at": record["published_at"] or "",
            "filter_decision": record["filter_decision"],
            "filter_reasons": _reason_codes(record["filter_reasons_json"]),
            "review_priority": priority.priority if priority else "",
            "priority_reasons": (
                ";".join(priority.priority_reasons) if priority else ""
            ),
            "qualification_status": record["qualification_status"],
            "application_channel": record["application_channel"],
            "application_url": record["application_url"],
            "job_url": record["canonical_url"] or "",
        })
    return rows, (run["finished_at"] or run["started_at"])


def export_jobs_run(
    connection, run_id: str, export_directory=DEFAULT_EXPORT_DIRECTORY,
) -> dict:
    """Export exactly one workflow run in deterministic job-id order."""
    rows, run_time = job_export_rows(connection, run_id)
    export_directory = Path(export_directory)
    token = _timestamp(run_time).strftime("%Y-%m-%d_%H-%M-%S")
    timestamped = export_directory / f"google_jobs_{token}.csv"
    latest = export_directory / "google_jobs_latest.csv"
    _atomic_csv(timestamped, JOB_FIELDS, rows)
    _update_latest(timestamped, latest)
    return {"timestamped": timestamped, "latest": latest, "rows": len(rows)}


def export_maps_rows(
    rows, fieldnames=None, export_directory=DEFAULT_EXPORT_DIRECTORY, run_time=None,
) -> dict:
    """Export the stable rows appended by one Maps invocation."""
    rows = list(rows)
    fields = list(fieldnames or ())
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = list(MAPS_FALLBACK_FIELDS)
    export_directory = Path(export_directory)
    token = _timestamp(run_time).strftime("%Y-%m-%d_%H-%M-%S")
    timestamped = export_directory / f"google_maps_{token}.csv"
    latest = export_directory / "google_maps_latest.csv"
    _atomic_csv(timestamped, fields, rows)
    _update_latest(timestamped, latest)
    return {"timestamped": timestamped, "latest": latest, "rows": len(rows)}
