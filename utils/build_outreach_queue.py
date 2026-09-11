"""
Outreach Queue and Send History
===============================

Purpose:
    Build a send queue that cannot reintroduce companies already contacted or
    protected by response history, and record later send outcomes safely.

Pipeline:
    outreach_ready.csv -> build_outreach_queue.py -> outreach_queue.csv -> sender
                                 |                         |
                                 +<-- outreach_history.csv-+

Input:
    Outreach-ready rows, persistent history, and optional sent-result imports.

Output:
    A duplicate-safe queue preserving every outreach-ready column, including
    geography, plus updated send history. The external email sender normally
    consumes the queue and reports SENT/FAILED outcomes back here.
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from datetime import datetime, timezone
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

if __package__:
    from utils.build_outreach import (
        clean_value,
        flagged_local_record,
        is_valid_email,
        normalize_email,
        normalize_website_domain,
    )
else:  # Support direct execution from the utils directory.
    from build_outreach import (
        clean_value,
        flagged_local_record,
        is_valid_email,
        normalize_email,
        normalize_website_domain,
    )


DEFAULT_INPUT = Path("./CSV_FILES/outreach_ready.csv")
DEFAULT_HISTORY = Path("./CSV_FILES/outreach_history.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/outreach_queue.csv")

HISTORY_FIELDS = (
    "company_name",
    "email",
    "website",
    "normalized_domain",
    "send_status",
    "sent_at",
    "response_status",
    "response_at",
    "notes",
)
SEND_STATUSES = {"PENDING", "SENT", "FAILED", "SKIPPED"}
RESPONSE_STATUSES = {
    "",
    "REPLIED",
    "INTERESTED",
    "NOT_INTERESTED",
    "BOUNCED",
    "UNSUBSCRIBED",
}
# ---------------------------------------------------------------------------
# PERSISTENT EXCLUSION POLICY
# ---------------------------------------------------------------------------
# Domain exclusions prevent a company from returning under a newly discovered
# address after it was sent, skipped, bounced, or unsubscribed.

PROTECTED_RESPONSE_STATUSES = {"BOUNCED", "UNSUBSCRIBED"}


def explicitly_requires_review(row):
    """Defensively reject review-marked rows from a custom ready file."""
    return (
        clean_value(row.get("enrichment_status")).upper() == "PARTIAL"
        or not clean_value(row.get("description"))
        or flagged_local_record(row)
    )


def read_csv(path):
    if not path.exists():
        return [], []
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        reader = DictReader(file_handler)
        return list(reader.fieldnames or ()), list(reader)


def atomic_write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8-sig",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_handler:
            temporary_path = Path(file_handler.name)
            writer = DictWriter(
                file_handler,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)
        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def ensure_history(path):
    if not path.exists():
        atomic_write_csv(path, HISTORY_FIELDS, [])


def normalized_history_row(row):
    """Return a history row with canonical statuses and a normalized domain."""
    normalized = {field: clean_value(row.get(field)) for field in HISTORY_FIELDS}
    normalized["send_status"] = normalized["send_status"].upper()
    normalized["response_status"] = normalized["response_status"].upper()
    if normalized["send_status"] not in SEND_STATUSES:
        normalized["send_status"] = "PENDING"
    if normalized["response_status"] not in RESPONSE_STATUSES:
        normalized["response_status"] = ""
    if not normalized["normalized_domain"]:
        normalized["normalized_domain"] = normalize_website_domain(
            normalized["website"]
        )
    else:
        normalized["normalized_domain"] = normalize_website_domain(
            normalized["normalized_domain"]
        )
    return normalized


def history_identity(row):
    return (
        normalize_email(row.get("email")),
        normalize_website_domain(
            row.get("normalized_domain") or row.get("website")
        ),
    )


def exclusion_indexes(history_rows, exclude_failed=False):
    """Return email/domain sets that must not be placed in the next queue."""
    blocked_emails = set()
    blocked_domains = set()
    for source_row in history_rows:
        row = normalized_history_row(source_row)
        send_status = row["send_status"]
        response_status = row["response_status"]
        should_exclude = (
            send_status in {"SENT", "SKIPPED"}
            or response_status in PROTECTED_RESPONSE_STATUSES
            or (exclude_failed and send_status == "FAILED")
        )
        if not should_exclude:
            continue
        email, domain = history_identity(row)
        if email:
            blocked_emails.add(email)
        if domain:
            blocked_domains.add(domain)
    return blocked_emails, blocked_domains


def build_queue(input_path, history_path, output_path, exclude_failed=False):
    """Write unsent unique outreach rows after applying persistent exclusions."""
    input_path = Path(input_path)
    history_path = Path(history_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Outreach input not found: {input_path}")

    input_fields, outreach_rows = read_csv(input_path)
    ensure_history(history_path)
    _, history_rows = read_csv(history_path)
    blocked_emails, blocked_domains = exclusion_indexes(
        history_rows,
        exclude_failed=exclude_failed,
    )

    queue_rows = []
    seen_emails = set()
    seen_domains = set()
    for row in outreach_rows:
        if explicitly_requires_review(row):
            continue
        email = normalize_email(row.get("email"))
        domain = normalize_website_domain(row.get("website"))
        if not is_valid_email(email):
            continue
        if email in blocked_emails or (domain and domain in blocked_domains):
            continue
        if email in seen_emails or (domain and domain in seen_domains):
            continue
        seen_emails.add(email)
        if domain:
            seen_domains.add(domain)
        # Build from the input header so new personalization metadata such as
        # country/city/location passes through without changing queue identity.
        queue_rows.append({field: row.get(field, "") for field in input_fields})

    atomic_write_csv(output_path, input_fields, queue_rows)
    return {
        "outreach_ready": len(outreach_rows),
        "history": len(history_rows),
        "queue": len(queue_rows),
        "excluded": len(outreach_rows) - len(queue_rows),
    }


def find_outreach_record(email, input_path, queue_path):
    target = normalize_email(email)
    for path in (input_path, queue_path):
        _, rows = read_csv(path)
        for row in rows:
            if normalize_email(row.get("email")) == target:
                return row
    raise ValueError(f"Email not found in outreach-ready or queue data: {email}")


def matching_history_index(history_rows, email, domain):
    for index, row in enumerate(history_rows):
        existing_email, _ = history_identity(row)
        if email and existing_email == email:
            return index
    if domain:
        for index, row in enumerate(history_rows):
            _, existing_domain = history_identity(row)
            if existing_domain == domain:
                return index
    return None


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mark_history(history_path, outreach_row, send_status, timestamp=None):
    """Persist one SENT or FAILED outcome without downgrading a prior SENT record."""
    if send_status not in {"SENT", "FAILED"}:
        raise ValueError(f"Unsupported mark status: {send_status}")
    history_path = Path(history_path)
    ensure_history(history_path)
    _, source_rows = read_csv(history_path)
    history_rows = [normalized_history_row(row) for row in source_rows]
    email = normalize_email(outreach_row.get("email"))
    domain = normalize_website_domain(outreach_row.get("website"))
    index = matching_history_index(history_rows, email, domain)

    if index is None:
        record = {field: "" for field in HISTORY_FIELDS}
        history_rows.append(record)
        index = len(history_rows) - 1
    record = history_rows[index]
    source_values = {
        "company_name": clean_value(outreach_row.get("company_name")),
        "email": clean_value(outreach_row.get("email")),
        "website": clean_value(outreach_row.get("website")),
        "normalized_domain": domain,
    }
    for field, value in source_values.items():
        if value and not clean_value(record.get(field)):
            record[field] = value

    if send_status == "SENT":
        if record.get("send_status") != "SENT":
            record["send_status"] = "SENT"
            record["sent_at"] = timestamp or utc_timestamp()
    elif record.get("send_status") != "SENT":
        record["send_status"] = "FAILED"

    atomic_write_csv(history_path, HISTORY_FIELDS, history_rows)
    return record


def import_sent_rows(import_path, input_path, history_path):
    """Merge an external sender's sent-email CSV into persistent history."""
    import_path = Path(import_path)
    input_path = Path(input_path)
    history_path = Path(history_path)
    if not import_path.exists():
        raise FileNotFoundError(f"Sent import file not found: {import_path}")

    import_fields, imported_rows = read_csv(import_path)
    if "email" not in import_fields:
        raise ValueError("Sent import file must contain an email column")
    _, outreach_rows = read_csv(input_path)
    outreach_by_email = {}
    for row in outreach_rows:
        email = normalize_email(row.get("email"))
        if email:
            outreach_by_email.setdefault(email, row)

    ensure_history(history_path)
    _, source_history = read_csv(history_path)
    history_rows = [normalized_history_row(row) for row in source_history]
    matched = 0
    unknown = 0
    already_in_history = 0
    new_sent_records = 0

    for imported in imported_rows:
        imported_email = normalize_email(imported.get("email"))
        if not imported_email:
            continue
        outreach = outreach_by_email.get(imported_email)
        if outreach is None:
            unknown += 1
            outreach = {}
        else:
            matched += 1

        company_name = clean_value(
            outreach.get("company_name") or imported.get("company_name")
        )
        email = clean_value(outreach.get("email") or imported.get("email"))
        website = clean_value(outreach.get("website") or imported.get("website"))
        domain = normalize_website_domain(website)
        index = next(
            (
                row_index
                for row_index, history_row in enumerate(history_rows)
                if history_identity(history_row)[0] == imported_email
            ),
            None,
        )
        if index is None:
            record = {field: "" for field in HISTORY_FIELDS}
            history_rows.append(record)
            index = len(history_rows) - 1
            new_sent_records += 1
        else:
            already_in_history += 1
        record = history_rows[index]

        import_values = {
            "company_name": company_name,
            "email": email,
            "website": website,
            "normalized_domain": domain,
            "sent_at": clean_value(imported.get("sent_at")),
            "notes": clean_value(imported.get("notes")),
        }
        for field, value in import_values.items():
            if value and not clean_value(record.get(field)):
                record[field] = value
        record["send_status"] = "SENT"

    atomic_write_csv(history_path, HISTORY_FIELDS, history_rows)
    return {
        "imported": len(imported_rows),
        "matched": matched,
        "unknown": unknown,
        "already_in_history": already_in_history,
        "new_sent_records": new_sent_records,
    }


def import_sent_and_rebuild(
    import_path,
    input_path,
    history_path,
    output_path,
    exclude_failed=False,
):
    """Import sent outcomes, rebuild the queue, and report before/after counts."""
    before = build_queue(
        input_path,
        history_path,
        output_path,
        exclude_failed=exclude_failed,
    )
    imported = import_sent_rows(import_path, input_path, history_path)
    after = build_queue(
        input_path,
        history_path,
        output_path,
        exclude_failed=exclude_failed,
    )
    return {
        **imported,
        "queue_before": before["queue"],
        "queue_after": after["queue"],
    }


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--mark-sent", metavar="EMAIL")
    action.add_argument("--mark-failed", metavar="EMAIL")
    action.add_argument("--import-sent", type=Path, metavar="FILE")
    parser.add_argument(
        "--exclude-failed",
        action="store_true",
        help="Exclude FAILED history entries instead of leaving them retryable",
    )
    return parser.parse_args()


def print_summary(summary):
    print(f"Outreach-ready rows: {summary['outreach_ready']}")
    print(f"History rows: {summary['history']}")
    print(f"Excluded rows: {summary['excluded']}")
    print(f"Queue rows: {summary['queue']}")


def main():
    arguments = parse_arguments()
    if not arguments.input.exists():
        raise SystemExit(f"Outreach input not found: {arguments.input}")

    if arguments.import_sent:
        try:
            summary = import_sent_and_rebuild(
                arguments.import_sent,
                arguments.input,
                arguments.history,
                arguments.output,
                exclude_failed=arguments.exclude_failed,
            )
        except (FileNotFoundError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(f"Imported rows: {summary['imported']}")
        print(f"Matched outreach leads: {summary['matched']}")
        print(f"Unknown historical emails: {summary['unknown']}")
        print(f"Already in history: {summary['already_in_history']}")
        print(f"New SENT records: {summary['new_sent_records']}")
        print(f"Queue before: {summary['queue_before']}")
        print(f"Queue after: {summary['queue_after']}")
        return

    mark_status = "SENT" if arguments.mark_sent else "FAILED"
    mark_email = arguments.mark_sent or arguments.mark_failed
    if mark_email:
        try:
            outreach_row = find_outreach_record(
                mark_email,
                arguments.input,
                arguments.output,
            )
            record = mark_history(arguments.history, outreach_row, mark_status)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print(f"Recorded {record['send_status']}: {record['email']}")

    summary = build_queue(
        arguments.input,
        arguments.history,
        arguments.output,
        exclude_failed=arguments.exclude_failed,
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
