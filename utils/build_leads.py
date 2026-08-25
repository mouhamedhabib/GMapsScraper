"""Build stable, deduplicated lead exports from the raw scraper CSV."""

from argparse import ArgumentParser
from collections import defaultdict
from csv import DictReader, DictWriter
from pathlib import Path
from re import compile
from urllib.parse import urlsplit


EMAIL_PATTERN = compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "company.com",
    "domain.com", "yourcompany.com", "email.com",
}
PLACEHOLDER_LOCAL_PARTS = {
    "test", "user", "name", "email", "foulen", "firstname",
    "lastname", "firstname.lastname", "yourname",
}
ASSET_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "svg", "webp", "css", "js", "pdf"}
FREE_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "proton.me", "protonmail.com", "aol.com",
}
EMAIL_LOCAL_PRIORITY = {
    local_part: priority for priority, local_part in enumerate((
        "jobs", "careers", "hr", "recruitment", "recruiting", "contact",
        "info", "hello", "support", "commercial", "sales",
    ))
}
UNAVAILABLE_VALUES = {"", "not available", "n/a", "none", "null"}
MASTER_FIELDS = (
    "name", "email", "alternate_emails", "phone", "website",
    "email_status", "review_status", "review_reasons", "source",
    "source_queries",
)
READY_FIELDS = ("name", "email", "phone", "website")


def clean_value(value):
    value = " ".join((value or "").split()).strip()
    return "" if value.casefold() in UNAVAILABLE_VALUES else value


def normalize_name(value):
    return clean_value(value).casefold()


def normalize_phone(value):
    value = clean_value(value)
    digits = "".join(character for character in value if character.isdigit())
    return digits if len(digits) >= 6 else ""


def website_domain(value):
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    domain = (parsed.hostname or "").casefold().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def is_valid_email(email):
    email = email.strip()
    if not EMAIL_PATTERN.fullmatch(email):
        return False
    local_part, domain = email.rsplit("@", 1)
    local_part = local_part.casefold()
    domain = domain.casefold().rstrip(".")
    if domain in PLACEHOLDER_DOMAINS or local_part in PLACEHOLDER_LOCAL_PARTS:
        return False
    return domain.rsplit(".", 1)[-1] not in ASSET_EXTENSIONS


def extract_emails(value):
    return EMAIL_PATTERN.findall(value or "")


def email_matches_website(email_domain, web_domain):
    if not web_domain:
        return False
    return (email_domain == web_domain or email_domain.endswith("." + web_domain)
            or web_domain.endswith("." + email_domain))


def email_status(email, web_domain):
    if not web_domain:
        return "NO_WEBSITE"
    domain = email.rsplit("@", 1)[1].casefold()
    return "MATCH" if email_matches_website(domain, web_domain) else "REVIEW"


def email_sort_key(email, web_domain):
    local_part, domain = email.casefold().rsplit("@", 1)
    status_order = {"MATCH": 0, "NO_WEBSITE": 1, "REVIEW": 2}[email_status(email, web_domain)]
    if domain in FREE_EMAIL_DOMAINS:
        address_priority = 12
    else:
        address_priority = EMAIL_LOCAL_PRIORITY.get(local_part, 11)
    return status_order, address_priority, email.casefold()


def new_group(index):
    return {
        "first_index": index,
        "names": [],
        "websites": [],
        "website_domains": set(),
        "phones": [],
        "phone_keys": set(),
        "emails": {},
        "review_reasons": set(),
        "sources": [],
        "source_queries": [],
        "rows": 0,
    }


def add_unique(values, value):
    if value and value.casefold() not in {item.casefold() for item in values}:
        values.append(value)


def merge_groups(target, source):
    for value in source["names"]:
        add_unique(target["names"], value)
    for value in source["websites"]:
        add_unique(target["websites"], value)
    for value in source["phones"]:
        add_unique(target["phones"], value)
    target["website_domains"].update(source["website_domains"])
    target["phone_keys"].update(source["phone_keys"])
    target["emails"].update(source["emails"])
    target["review_reasons"].update(source["review_reasons"])
    for value in source["sources"]:
        add_unique(target["sources"], value)
    for value in source["source_queries"]:
        add_unique(target["source_queries"], value)
    target["rows"] += source["rows"]
    target["first_index"] = min(target["first_index"], source["first_index"])


def compatible_name_fallback(group, web_domain, phone_key, email_domains):
    if web_domain and group["website_domains"] and web_domain not in group["website_domains"]:
        return False
    if phone_key and group["phone_keys"] and phone_key not in group["phone_keys"]:
        return False
    group_email_domains = {email.rsplit("@", 1)[1].casefold() for email in group["emails"]}
    if email_domains and group_email_domains and email_domains.isdisjoint(group_email_domains):
        return False
    return True


def build_groups(rows):
    groups = []
    parent = []
    domain_index = defaultdict(set)
    name_phone_index = defaultdict(set)
    name_email_index = defaultdict(set)
    name_index = defaultdict(set)
    rejected_emails = 0

    def find(group_id):
        while parent[group_id] != group_id:
            parent[group_id] = parent[parent[group_id]]
            group_id = parent[group_id]
        return group_id

    def active_ids(group_ids):
        return {find(group_id) for group_id in group_ids}

    for row_index, row in enumerate(rows):
        name = clean_value(row.get("title"))
        name_key = normalize_name(name)
        website = clean_value(row.get("webpage"))
        web_domain = website_domain(website)
        phone = clean_value(row.get("phone_number"))
        phone_key = normalize_phone(phone)
        source = clean_value(row.get("_source")) or "google_maps"
        source_queries = [
            clean_value(value) for value in (row.get("_source_queries") or "").split(";")
            if clean_value(value)
        ]

        valid_emails = {}
        for email in extract_emails(row.get("site_email")):
            if is_valid_email(email):
                valid_emails.setdefault(email.casefold(), email)
            else:
                rejected_emails += 1
        email_domains = {email.rsplit("@", 1)[1].casefold() for email in valid_emails}

        strong_matches = set()
        if web_domain:
            strong_matches.update(active_ids(domain_index[web_domain]))
        if name_key and phone_key:
            strong_matches.update(active_ids(name_phone_index[(name_key, phone_key)]))
        if name_key:
            for domain in email_domains:
                strong_matches.update(active_ids(name_email_index[(name_key, domain)]))

        ambiguous_name_matches = set()
        if not strong_matches and name_key:
            compatible_name_matches = set()
            for group_id in active_ids(name_index[name_key]):
                if compatible_name_fallback(groups[group_id], web_domain, phone_key, email_domains):
                    compatible_name_matches.add(group_id)
                else:
                    ambiguous_name_matches.add(group_id)
            if len(compatible_name_matches) == 1:
                strong_matches.update(compatible_name_matches)
            elif len(compatible_name_matches) > 1:
                ambiguous_name_matches.update(compatible_name_matches)

        if strong_matches:
            group_id = min(strong_matches, key=lambda candidate: groups[candidate]["first_index"])
            for other_id in sorted(strong_matches):
                other_id = find(other_id)
                if other_id != group_id:
                    merge_groups(groups[group_id], groups[other_id])
                    parent[other_id] = group_id
        else:
            group_id = len(groups)
            groups.append(new_group(row_index))
            parent.append(group_id)

        group = groups[group_id]
        group["rows"] += 1
        add_unique(group["names"], name)
        add_unique(group["websites"], website)
        add_unique(group["phones"], phone)
        if web_domain:
            group["website_domains"].add(web_domain)
        if phone_key:
            group["phone_keys"].add(phone_key)
        group["emails"].update(valid_emails)
        add_unique(group["sources"], source)
        for source_query in source_queries:
            add_unique(group["source_queries"], source_query)

        if ambiguous_name_matches:
            group["review_reasons"].add("ambiguous exact-name match")
            for ambiguous_id in ambiguous_name_matches:
                groups[find(ambiguous_id)]["review_reasons"].add("ambiguous exact-name match")

        if web_domain:
            domain_index[web_domain].add(group_id)
        if name_key and phone_key:
            name_phone_index[(name_key, phone_key)].add(group_id)
        for domain in email_domains:
            if name_key:
                name_email_index[(name_key, domain)].add(group_id)
        if name_key:
            name_index[name_key].add(group_id)

    active_groups = [group for group_id, group in enumerate(groups) if find(group_id) == group_id]
    active_groups.sort(key=lambda group: group["first_index"])
    return active_groups, rejected_emails


def finalize_group(group):
    name = group["names"][0] if group["names"] else ""
    website = group["websites"][0] if group["websites"] else ""
    web_domain = website_domain(website)
    phone = group["phones"][0] if group["phones"] else ""
    emails = sorted(group["emails"].values(), key=lambda email: email_sort_key(email, web_domain))
    primary_email = emails[0] if emails else ""
    status = email_status(primary_email, web_domain) if primary_email else ""
    reasons = set(group["review_reasons"])

    if len(group["website_domains"]) > 1:
        reasons.add("multiple conflicting websites")
    email_domains = {email.rsplit("@", 1)[1].casefold() for email in emails}
    unrelated_email_domains = any(
        not (first == second or first.endswith("." + second) or second.endswith("." + first))
        for first in email_domains for second in email_domains if first < second
    )
    if unrelated_email_domains:
        reasons.add("multiple email domains")
    if status == "REVIEW":
        reasons.add("email domain differs from website")

    return {
        "name": name,
        "email": primary_email,
        "alternate_emails": ";".join(emails[1:]),
        "phone": phone,
        "website": website,
        "email_status": status,
        "review_status": "REVIEW" if reasons else "READY",
        "review_reasons": "; ".join(sorted(reasons)),
        "source": ";".join(group["sources"]),
        "source_queries": ";".join(group["source_queries"]),
    }


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def search_rows(search_input_path):
    if not search_input_path or not search_input_path.exists():
        return []
    with search_input_path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return [
            {
                "title": row.get("company_name", ""),
                "webpage": row.get("website", ""),
                "phone_number": "",
                "site_email": "",
                "_source": "google_search",
                "_source_queries": row.get("source_query", ""),
            }
            for row in DictReader(file_handler)
        ]


def enriched_search_rows(email_enrichment_input_path):
    if not email_enrichment_input_path or not email_enrichment_input_path.exists():
        return []
    rows = []
    with email_enrichment_input_path.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file_handler:
        for row in DictReader(file_handler):
            if clean_value(row.get("email_enrichment_status")).upper() != "FOUND":
                continue
            emails = [
                email
                for field in ("email", "alternate_emails")
                for email in extract_emails(row.get(field))
                if is_valid_email(email)
            ]
            if not emails:
                continue
            rows.append({
                "title": row.get("name", ""),
                "webpage": row.get("website", ""),
                "phone_number": row.get("phone", ""),
                "site_email": ";".join(emails),
                "_source": "google_search",
                "_source_queries": row.get("source_queries", ""),
            })
    return rows


def build_lead_files(
    input_path,
    output_folder,
    search_input_path=None,
    email_enrichment_input_path=None,
):
    input_path = Path(input_path)
    output_folder = Path(output_folder)
    search_input_path = Path(search_input_path) if search_input_path else None
    email_enrichment_input_path = (
        Path(email_enrichment_input_path) if email_enrichment_input_path else None
    )
    with input_path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        raw_rows = list(DictReader(file_handler))
    for row in raw_rows:
        row["_source"] = "google_maps"

    if search_input_path is None:
        search_input_path = output_folder / "google_search_companies.csv"
    discovered_rows = search_rows(search_input_path)
    raw_rows.extend(discovered_rows)
    if email_enrichment_input_path is None:
        email_enrichment_input_path = output_folder / "search_email_enriched.csv"
    raw_rows.extend(enriched_search_rows(email_enrichment_input_path))

    groups, rejected_emails = build_groups(raw_rows)
    master_rows = [finalize_group(group) for group in groups]
    ready_rows = [
        {field: row[field] for field in READY_FIELDS}
        for row in master_rows
        if row["email"] and row["review_status"] == "READY"
    ]
    review_rows = [row for row in master_rows if row["review_status"] == "REVIEW"]

    write_csv(output_folder / "leads_master.csv", MASTER_FIELDS, master_rows)
    write_csv(output_folder / "leads_ready.csv", READY_FIELDS, ready_rows)
    write_csv(output_folder / "leads_review.csv", MASTER_FIELDS, review_rows)

    domain_matches = sum(row["email_status"] == "MATCH" for row in master_rows)
    missing_email = sum(not row["email"] for row in master_rows)
    search_only_missing_email = sum(
        not row["email"] and row["source"] == "google_search"
        for row in master_rows
    )
    print(f"Raw rows: {len(raw_rows)}")
    print(f"Unique companies: {len(master_rows)}")
    print(f"Duplicates merged: {len(raw_rows) - len(master_rows)}")
    print(f"Usable leads: {len(ready_rows)}")
    print(f"Domain matches: {domain_matches}")
    print(f"Needs review: {len(review_rows)}")
    print(f"Rejected emails: {rejected_emails}")
    print(f"Missing email: {missing_email}")
    print(f"Google Search-only without email: {search_only_missing_email}")


def main():
    parser = ArgumentParser(description="Build clean lead exports from GMapsScraper CSV output")
    parser.add_argument("--input", type=Path, default=Path("./CSV_FILES/google_maps_data.csv"))
    parser.add_argument("--output-folder", type=Path, default=Path("./CSV_FILES"))
    parser.add_argument(
        "--search-input",
        type=Path,
        help="Optional Google Search discovery CSV (defaults to the output folder)",
    )
    parser.add_argument(
        "--email-enrichment-input",
        type=Path,
        help="Optional Search email enrichment CSV (defaults to the output folder)",
    )
    args = parser.parse_args()
    build_lead_files(
        args.input,
        args.output_folder,
        args.search_input,
        args.email_enrichment_input,
    )


if __name__ == "__main__":
    main()
