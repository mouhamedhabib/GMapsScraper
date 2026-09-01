"""
Outreach Export Builder
=======================

Purpose:
    Convert enriched leads into a deduplicated outreach dataset while retaining
    incomplete or locally flagged records in a companion review export.

Pipeline:
    leads_enriched_final.csv -> build_outreach.py -> outreach_ready.csv
                                                -> outreach_review.csv

Input:
    Enriched lead rows plus nearby master, review, and raw Maps files used to
    recover trustworthy geography and preserve earlier review decisions.

Output:
    Outreach-ready rows and a review subset. ``build_outreach_queue.py`` normally
    consumes ``outreach_ready.csv`` next.
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from os import replace
from pathlib import Path
from re import IGNORECASE, compile
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

try:
    from utils.discovery_timestamps import earliest_added_at
except ModuleNotFoundError:
    from discovery_timestamps import earliest_added_at


DEFAULT_INPUT = Path("./CSV_FILES/leads_enriched_final.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/outreach_ready.csv")
DEFAULT_REVIEW_OUTPUT = Path("./CSV_FILES/outreach_review.csv")

OUTPUT_FIELDS = (
    "company_name",
    "email",
    "website",
    "added_at",
    "country",
    "city",
    "location",
    "description",
    "industry",
    "services",
    "website_title",
    "website_meta_description",
    "about_text",
    "linkedin_url",
    "phone",
    "contact_name",
    "contact_role",
    "recent_project_news",
    "enrichment_status",
)

# ---------------------------------------------------------------------------
# OUTREACH ELIGIBILITY
# ---------------------------------------------------------------------------
# Only leads with useful enrichment and a non-placeholder email may enter the
# outreach export; partial or previously flagged rows remain visible for review.

ELIGIBLE_STATUSES = {"SUCCESS", "PARTIAL"}
UNAVAILABLE_VALUES = {
    "",
    "n/a",
    "none",
    "not available",
    "null",
    "unknown",
}
EMAIL_PATTERN = compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    flags=IGNORECASE,
)
PLACEHOLDER_DOMAINS = {
    "company.com",
    "domain.com",
    "email.com",
    "example.com",
    "example.net",
    "example.org",
    "yourcompany.com",
}
PLACEHOLDER_LOCAL_PARTS = {
    "email",
    "firstname",
    "firstname.lastname",
    "foulen",
    "lastname",
    "name",
    "test",
    "user",
    "yourname",
}
ASSET_EXTENSIONS = {"css", "gif", "jpeg", "jpg", "js", "pdf", "png", "svg", "webp"}


def clean_value(value):
    """Trim source text without rewriting its internal content."""
    cleaned = str(value or "").strip()
    return "" if cleaned.casefold() in UNAVAILABLE_VALUES else cleaned


def normalize_company_name(value):
    return "".join(
        character
        for character in clean_value(value).casefold()
        if character.isalnum()
    )


def normalize_website_domain(value):
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    domain = (parsed.hostname or "").casefold().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def normalize_email(value):
    return clean_value(value).casefold()


def is_valid_email(value):
    email = clean_value(value)
    if not EMAIL_PATTERN.fullmatch(email):
        return False
    local_part, domain = email.casefold().rsplit("@", 1)
    if domain in PLACEHOLDER_DOMAINS or local_part in PLACEHOLDER_LOCAL_PARTS:
        return False
    return domain.rsplit(".", 1)[-1] not in ASSET_EXTENSIONS


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def atomic_write_csv(path, rows):
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
            writer = DictWriter(file_handler, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


GEOGRAPHY_FIELDS = ("country", "city", "location")


def add_index_value(index, key, value):
    if key and value:
        index.setdefault(key, set()).add(value)


def local_csv_paths(input_path):
    return (
        input_path.parent / "leads_master.csv",
        input_path.parent / "google_maps_data.csv",
    )


def load_geography_indexes(input_path):
    """Index geography from nearby lead and Maps files for conservative recovery."""
    domain_values = {field: {} for field in GEOGRAPHY_FIELDS}
    name_values = {field: {} for field in GEOGRAPHY_FIELDS}
    for source_path in local_csv_paths(input_path):
        if source_path == input_path or not source_path.exists():
            continue
        for row in read_csv(source_path):
            website = row.get("website") or row.get("webpage")
            name = row.get("company_name") or row.get("name") or row.get("title")
            for field in GEOGRAPHY_FIELDS:
                value = clean_value(row.get(field))
                if field == "location" and not value:
                    value = clean_value(row.get("address"))
                add_index_value(
                    domain_values[field], normalize_website_domain(website), value,
                )
                add_index_value(
                    name_values[field], normalize_company_name(name), value,
                )
    return domain_values, name_values


def unique_index_value(index, key):
    values = index.get(key, set())
    return next(iter(values)) if len(values) == 1 else ""


def recover_geography(field, row, domain_values, name_values):
    value = clean_value(row.get(field))
    if value:
        return value
    domain = normalize_website_domain(row.get("website"))
    value = unique_index_value(domain_values[field], domain) if domain else ""
    if value:
        return value
    name = normalize_company_name(row.get("company_name"))
    return unique_index_value(name_values[field], name) if name else ""


def flagged_local_record(row):
    review_status = clean_value(row.get("review_status")).casefold()
    email_status = clean_value(row.get("email_status")).casefold()
    return (
        review_status == "review"
        or email_status == "review"
        or bool(clean_value(row.get("review_reasons")))
    )


def load_review_indexes(input_path):
    """Return domain/name indexes for records flagged earlier in lead building."""
    flagged_domains = set()
    flagged_names = set()
    name_identities = {}
    source_paths = (
        input_path.parent / "leads_master.csv",
        input_path.parent / "leads_review.csv",
    )
    for source_path in source_paths:
        if source_path == input_path or not source_path.exists():
            continue
        for row in read_csv(source_path):
            domain = normalize_website_domain(row.get("website") or row.get("webpage"))
            name = normalize_company_name(
                row.get("company_name") or row.get("name") or row.get("title")
            )
            identity = domain or normalize_email(row.get("email")) or name
            if name and identity:
                name_identities.setdefault(name, set()).add(identity)
            if not flagged_local_record(row):
                continue
            if domain:
                flagged_domains.add(domain)
            if name:
                flagged_names.add(name)
    return flagged_domains, flagged_names, name_identities


def is_locally_flagged(row, flagged_domains, flagged_names, name_identities):
    domain = normalize_website_domain(row.get("website"))
    if domain:
        return domain in flagged_domains
    name = normalize_company_name(row.get("company_name"))
    return (
        bool(name)
        and name in flagged_names
        and len(name_identities.get(name, set())) == 1
    )


def row_identity(row, row_index):
    domain = normalize_website_domain(row.get("website"))
    if domain:
        return "domain", domain
    name = normalize_company_name(row.get("company_name"))
    if name:
        return "name", name
    email = normalize_email(row.get("email"))
    return ("email", email) if email else ("row", str(row_index))


def preference_key(row):
    return (
        clean_value(row.get("enrichment_status")).upper() == "SUCCESS",
        bool(clean_value(row.get("description"))),
        bool(clean_value(row.get("industry"))),
        bool(clean_value(row.get("services"))),
    )


def merge_duplicate_rows(rows):
    """Combine duplicate identities, preferring the most complete enrichment row."""
    ranked_rows = sorted(rows, key=preference_key, reverse=True)
    merged = {field: "" for field in OUTPUT_FIELDS}
    for row in ranked_rows:
        for field in OUTPUT_FIELDS:
            if field in {"contact_name", "contact_role", "recent_project_news"}:
                continue
            value = clean_value(row.get(field))
            if value and not merged[field]:
                merged[field] = value
    merged["enrichment_status"] = clean_value(
        ranked_rows[0].get("enrichment_status")
    ).upper()
    merged["added_at"] = earliest_added_at(row.get("added_at") for row in rows)
    return merged


def build_outreach(input_path, output_path, review_output_path):
    """Write deduplicated outreach and review CSVs and return row-count metrics."""
    input_rows = read_csv(input_path)
    status_counts = {"SUCCESS": 0, "PARTIAL": 0}
    excluded_statuses = 0
    eligible_rows = []

    for row in input_rows:
        status = clean_value(row.get("enrichment_status")).upper()
        if status in status_counts:
            status_counts[status] += 1
        else:
            excluded_statuses += 1
        if status in ELIGIBLE_STATUSES and is_valid_email(row.get("email")):
            eligible_rows.append(row)

    groups = {}
    group_order = []
    for row_index, row in enumerate(eligible_rows):
        identity = row_identity(row, row_index)
        if identity not in groups:
            groups[identity] = []
            group_order.append(identity)
        groups[identity].append(row)

    domain_geography, name_geography = load_geography_indexes(input_path)
    flagged_domains, flagged_names, name_identities = load_review_indexes(input_path)
    outreach_rows = []
    review_rows = []
    for identity in group_order:
        output_row = merge_duplicate_rows(groups[identity])
        for field in GEOGRAPHY_FIELDS:
            output_row[field] = recover_geography(
                field, output_row, domain_geography, name_geography,
            )
        outreach_rows.append(output_row)
        if (
            output_row["enrichment_status"] == "PARTIAL"
            or not output_row["description"]
            or is_locally_flagged(
                output_row,
                flagged_domains,
                flagged_names,
                name_identities,
            )
        ):
            review_rows.append(output_row.copy())

    atomic_write_csv(output_path, outreach_rows)
    atomic_write_csv(review_output_path, review_rows)
    return {
        "input": len(input_rows),
        "success": status_counts["SUCCESS"],
        "partial": status_counts["PARTIAL"],
        "excluded": excluded_statuses,
        "outreach": len(outreach_rows),
        "review": len(review_rows),
    }


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW_OUTPUT)
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    if not arguments.input.exists():
        raise SystemExit(f"Input CSV not found: {arguments.input}")
    summary = build_outreach(
        arguments.input,
        arguments.output,
        arguments.review_output,
    )
    print(f"Input enriched leads: {summary['input']}")
    print(f"SUCCESS: {summary['success']}")
    print(f"PARTIAL: {summary['partial']}")
    print(f"Excluded incomplete/failed: {summary['excluded']}")
    print(f"Final outreach rows: {summary['outreach']}")
    print(f"Review rows: {summary['review']}")


if __name__ == "__main__":
    main()
