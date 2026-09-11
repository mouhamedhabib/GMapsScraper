"""
Lead Builder
============

Purpose:
    Turn Maps and Search discoveries into stable, deduplicated lead records.

Pipeline:
    google_maps_data.csv -----------\
    google_search_companies.csv -----+-> build_leads.py -> leads_master.csv
    search_email_enriched.csv -------+                  -> leads_ready.csv
    missing_email_enriched.csv ------/                  -> leads_review.csv
    search_email_fallback.csv -------/

Input:
    Raw Maps rows, optional Search discoveries, and optional Search-email
    enrichment results, including source-agnostic missing-email recovery.

Output:
    A complete master export, a ready subset, and records requiring review,
    all preserving available country, city, and location metadata.

Previous / next:
    Maps/Search discovery runs before this file. Search-email enrichment may
    feed a second build; ``enrich_leads.py`` normally consumes leads_ready.csv.
"""

from argparse import ArgumentParser
from collections import defaultdict
from csv import DictReader, DictWriter
from datetime import datetime, timezone
from hashlib import sha256
from json import dump, load
import os
from pathlib import Path
from re import compile
from shutil import copyfile
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

try:
    from utils.discovery_timestamps import earliest_added_at
    from utils.geography import (
        extract_address_geography,
        extract_query_geography,
        format_location,
        normalize_country,
    )
    from utils.maps_identity import maps_identities_for
except ModuleNotFoundError:  # Support direct execution from the utils directory.
    from discovery_timestamps import earliest_added_at
    from geography import (
        extract_address_geography,
        extract_query_geography,
        format_location,
        normalize_country,
    )
    from maps_identity import maps_identities_for


# ---------------------------------------------------------------------------
# EMAIL QUALITY AND PRIORITY RULES
# ---------------------------------------------------------------------------
# Reject obvious examples/assets, then prefer website-domain and useful role
# addresses so the most outreach-relevant email becomes the primary address.

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
    "map_place_id", "maps_identity",
    "country", "city", "location",
    "added_at",
    "email_status", "review_status", "review_reasons", "source",
    "source_queries",
)
READY_FIELDS = (
    "name", "email", "phone", "website", "country", "city", "location",
    "added_at",
)
SNAPSHOT_MANIFEST = "leads_snapshot.json"
SNAPSHOT_PENDING = ".leads_snapshot.pending.json"


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
    """Return a lowercase website identity without scheme, path, or ``www``."""
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    domain = (parsed.hostname or "").casefold().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def is_valid_email(email):
    """Return whether an address is syntactically usable and not placeholder data."""
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
    """Classify an email as website-matching, reviewable, or lacking a website."""
    if not web_domain:
        return "NO_WEBSITE"
    domain = email.rsplit("@", 1)[1].casefold()
    return "MATCH" if email_matches_website(domain, web_domain) else "REVIEW"


def email_sort_key(email, web_domain):
    """Return the ranking key used to choose a group's primary outreach email."""
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
        "map_place_ids": set(),
        "map_place_urls": set(),
        "name_keys": set(),
        "phones": [],
        "phone_keys": set(),
        "emails": {},
        "review_reasons": set(),
        "sources": [],
        "source_queries": [],
        "geographies": [],
        "address_keys": set(),
        "added_at_values": [],
        "match_counts": defaultdict(int),
        "rows": 0,
    }


def add_unique(values, value):
    if value and value.casefold() not in {item.casefold() for item in values}:
        values.append(value)


def merge_groups(target, source):
    """Combine two records proven to represent the same company."""
    for value in source["names"]:
        add_unique(target["names"], value)
    for value in source["websites"]:
        add_unique(target["websites"], value)
    for value in source["phones"]:
        add_unique(target["phones"], value)
    target["website_domains"].update(source["website_domains"])
    target["map_place_ids"].update(source["map_place_ids"])
    target["map_place_urls"].update(source["map_place_urls"])
    target["name_keys"].update(source["name_keys"])
    target["phone_keys"].update(source["phone_keys"])
    target["emails"].update(source["emails"])
    target["review_reasons"].update(source["review_reasons"])
    for value in source["sources"]:
        add_unique(target["sources"], value)
    for value in source["source_queries"]:
        add_unique(target["source_queries"], value)
    target["geographies"].extend(source["geographies"])
    target["address_keys"].update(source["address_keys"])
    target["added_at_values"].extend(source["added_at_values"])
    for kind, count in source["match_counts"].items():
        target["match_counts"][kind] += count
    target["rows"] += source["rows"]
    target["first_index"] = min(target["first_index"], source["first_index"])


def compatible_name_fallback(group, web_domain, phone_key, email_domains):
    """Allow an exact-name merge only when available identifiers do not conflict."""
    if web_domain and group["website_domains"] and web_domain not in group["website_domains"]:
        return False
    if phone_key and group["phone_keys"] and phone_key not in group["phone_keys"]:
        return False
    group_email_domains = {email.rsplit("@", 1)[1].casefold() for email in group["emails"]}
    if email_domains and group_email_domains and email_domains.isdisjoint(group_email_domains):
        return False
    return True


def group_company_email_domains(group):
    return {
        email.rsplit("@", 1)[1].casefold() for email in group["emails"]
        if email.rsplit("@", 1)[1].casefold() not in FREE_EMAIL_DOMAINS
    }


def sets_conflict(first, second):
    return bool(first and second and first.isdisjoint(second))


def identity_conflicts(group, evidence, match_kind):
    """Return reasons that veto attaching ``evidence`` through ``match_kind``.

    A Maps match is authoritative for a place. Other identity kinds are vetoed
    when they would cross explicit stronger evidence. Domain matches also stop
    at distinct Maps places when a phone or address distinguishes branches.
    """
    group_maps = group["map_place_ids"] | group["map_place_urls"]
    row_maps = evidence["map_place_ids"] | evidence["map_place_urls"]
    distinct_maps = sets_conflict(group_maps, row_maps)
    domain_conflict = sets_conflict(
        group["website_domains"], evidence["website_domains"],
    )
    phone_conflict = sets_conflict(group["phone_keys"], evidence["phone_keys"])
    name_conflict = sets_conflict(group["name_keys"], evidence["name_keys"])
    email_conflict = sets_conflict(
        group_company_email_domains(group), evidence["email_domains"],
    )
    address_conflict = sets_conflict(group["address_keys"], evidence["address_keys"])

    if match_kind == "maps":
        return set()
    if match_kind == "domain":
        if distinct_maps:
            reasons = {"conflicting maps identities"}
            if phone_conflict or address_conflict:
                reasons.add("shared domain across distinct places")
            return reasons
        if name_conflict and phone_conflict:
            return {"conflicting strong identities"}
        return set()
    if match_kind in {"name_phone", "name_email"}:
        reasons = set()
        if distinct_maps:
            reasons.add("conflicting maps identities")
        if domain_conflict:
            reasons.add("conflicting strong identities")
        return reasons

    reasons = set()
    if distinct_maps:
        reasons.add("conflicting maps identities")
    if domain_conflict or phone_conflict or email_conflict:
        reasons.add("conflicting strong identities")
    if address_conflict:
        reasons.add("conflicting location evidence")
    return reasons


def row_geographies(row, source, row_index):
    """Return ranked geography evidence without collapsing country conflicts."""
    candidates = []

    def add_candidate(country, city, location, priority):
        country = normalize_country(country)
        city = clean_value(city)
        location = clean_value(location) or format_location(city, country)
        if country or city or location:
            candidates.append({
                "country": country,
                "city": city,
                "location": location,
                "priority": priority,
                "row_index": row_index,
            })

    # Maps address evidence is preferred because it describes the actual
    # result. Explicit Maps columns follow, then query-derived fallback data.
    if source == "google_maps":
        address_geo = extract_address_geography(row.get("address"))
        add_candidate(**address_geo, priority=0)
        location_geo = extract_address_geography(row.get("location"))
        add_candidate(**location_geo, priority=1)
        add_candidate(
            row.get("country"), row.get("city"), row.get("location"), 1,
        )
    else:
        location_geo = extract_address_geography(row.get("location"))
        add_candidate(**location_geo, priority=2)
        add_candidate(
            row.get("country"), row.get("city"), row.get("location"), 2,
        )

    for query in str(row.get("_source_queries") or "").split(";"):
        query_geo = extract_query_geography(query)
        add_candidate(**query_geo, priority=3)
    return candidates


def build_groups(rows):
    """Group raw rows by supported company identities and count rejected emails."""

    # -----------------------------------------------------------------------
    # COMPANY DEDUPLICATION
    # -----------------------------------------------------------------------
    # Precedence is Maps place, domain, name+phone, name+email-domain, then an
    # exact-name fallback. An incoming row never unions multiple established
    # groups: a unique highest-precedence compatible match wins; a tie becomes
    # a separate review group. This is the bridge-conflict veto.
    groups = []
    place_id_index = defaultdict(set)
    place_url_index = defaultdict(set)
    domain_index = defaultdict(set)
    name_phone_index = defaultdict(set)
    name_email_index = defaultdict(set)
    name_index = defaultdict(set)
    rejected_emails = 0

    for row_index, row in enumerate(rows):
        name = clean_value(row.get("title"))
        name_key = normalize_name(name)
        website = clean_value(row.get("webpage"))
        web_domain = website_domain(website)
        phone = clean_value(row.get("phone_number"))
        phone_key = normalize_phone(phone)
        maps_identities = maps_identities_for(row)
        map_place_id = maps_identities["place_id"]
        map_place_url = maps_identities["place_url"]
        address_key = normalize_name(
            row.get("address") or row.get("location") or format_location(
                clean_value(row.get("city")), normalize_country(row.get("country")),
            )
        )
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
        email_domains = {
            email.rsplit("@", 1)[1].casefold() for email in valid_emails
            if email.rsplit("@", 1)[1].casefold() not in FREE_EMAIL_DOMAINS
        }
        evidence = {
            "map_place_ids": {map_place_id} if map_place_id else set(),
            "map_place_urls": {map_place_url} if map_place_url else set(),
            "website_domains": {web_domain} if web_domain else set(),
            "name_keys": {name_key} if name_key else set(),
            "phone_keys": {phone_key} if phone_key else set(),
            "email_domains": email_domains,
            "address_keys": {address_key} if address_key else set(),
        }

        candidate_kinds = defaultdict(set)
        if map_place_id:
            for candidate in place_id_index[map_place_id]:
                candidate_kinds[candidate].add("maps")
        if map_place_url:
            for candidate in place_url_index[map_place_url]:
                candidate_kinds[candidate].add("maps")
        if web_domain:
            for candidate in domain_index[web_domain]:
                candidate_kinds[candidate].add("domain")
        if name_key and phone_key:
            for candidate in name_phone_index[(name_key, phone_key)]:
                candidate_kinds[candidate].add("name_phone")
        if name_key:
            for domain in email_domains:
                for candidate in name_email_index[(name_key, domain)]:
                    candidate_kinds[candidate].add("name_email")

        priority = {"maps": 0, "domain": 1, "name_phone": 2, "name_email": 3, "name": 4}
        compatible = []
        rejected = defaultdict(set)
        for candidate, kinds in candidate_kinds.items():
            best_kind = min(kinds, key=priority.get)
            reasons = identity_conflicts(groups[candidate], evidence, best_kind)
            if reasons:
                rejected[candidate].update(reasons)
            else:
                compatible.append((priority[best_kind], candidate, best_kind))

        ambiguous_name_matches = set()
        if not candidate_kinds and name_key:
            for candidate in name_index[name_key]:
                reasons = identity_conflicts(groups[candidate], evidence, "name")
                if reasons:
                    rejected[candidate].update(reasons)
                    ambiguous_name_matches.add(candidate)
                else:
                    compatible.append((priority["name"], candidate, "name"))

        group_id = None
        selected_kind = ""
        if compatible:
            best_priority = min(item[0] for item in compatible)
            best = [item for item in compatible if item[0] == best_priority]
            if len(best) == 1:
                group_id = best[0][1]
                selected_kind = best[0][2]
            else:
                ambiguous_name_matches.update(item[1] for item in best)

        bridge_candidates = set(candidate_kinds)
        if len(bridge_candidates) > 1:
            for candidate in bridge_candidates:
                groups[candidate]["review_reasons"].add("identity bridge conflict")
            if group_id is not None:
                groups[group_id]["review_reasons"].add("identity bridge conflict")

        if group_id is None:
            group_id = len(groups)
            groups.append(new_group(row_index))

        group = groups[group_id]
        if selected_kind:
            group["match_counts"][selected_kind] += 1
        for candidate, reasons in rejected.items():
            groups[candidate]["review_reasons"].update(reasons)
            group["review_reasons"].update(reasons)
        if len(bridge_candidates) > 1:
            group["review_reasons"].add("identity bridge conflict")
        group["rows"] += 1
        add_unique(group["names"], name)
        add_unique(group["websites"], website)
        add_unique(group["phones"], phone)
        if web_domain:
            group["website_domains"].add(web_domain)
        if map_place_id:
            group["map_place_ids"].add(map_place_id)
        if map_place_url:
            group["map_place_urls"].add(map_place_url)
        if name_key:
            group["name_keys"].add(name_key)
        if phone_key:
            group["phone_keys"].add(phone_key)
        if address_key:
            group["address_keys"].add(address_key)
        group["emails"].update(valid_emails)
        add_unique(group["sources"], source)
        for source_query in source_queries:
            add_unique(group["source_queries"], source_query)
        group["geographies"].extend(row_geographies(row, source, row_index))
        group["added_at_values"].append(row.get("added_at", ""))

        if ambiguous_name_matches:
            group["review_reasons"].add("ambiguous exact-name match")
            for ambiguous_id in ambiguous_name_matches:
                groups[ambiguous_id]["review_reasons"].add("ambiguous exact-name match")

        if map_place_id:
            place_id_index[map_place_id].add(group_id)
        if map_place_url:
            place_url_index[map_place_url].add(group_id)
        if web_domain:
            domain_index[web_domain].add(group_id)
        if name_key and phone_key:
            name_phone_index[(name_key, phone_key)].add(group_id)
        for domain in email_domains:
            if name_key:
                name_email_index[(name_key, domain)].add(group_id)
        if name_key:
            name_index[name_key].add(group_id)

    groups.sort(key=lambda group: group["first_index"])
    return groups, rejected_emails


def finalize_group(group):
    """Convert one merged company group into a master-row review decision."""
    name = group["names"][0] if group["names"] else ""
    website = group["websites"][0] if group["websites"] else ""
    web_domain = website_domain(website)
    phone = group["phones"][0] if group["phones"] else ""
    emails = sorted(group["emails"].values(), key=lambda email: email_sort_key(email, web_domain))
    primary_email = emails[0] if emails else ""
    status = email_status(primary_email, web_domain) if primary_email else ""
    reasons = set(group["review_reasons"])
    map_place_id = sorted(group["map_place_ids"])[0] if group["map_place_ids"] else ""
    maps_identity = (
        sorted(group["map_place_urls"])[0]
        if group["map_place_urls"] else map_place_id
    )

    countries = {
        geography["country"] for geography in group["geographies"]
        if geography["country"]
    }
    if len(countries) > 1:
        reasons.add("conflicting countries")
    ranked_geographies = sorted(
        group["geographies"],
        key=lambda geography: (
            geography["priority"],
            not bool(geography["country"]),
            not bool(geography["city"]),
            geography["row_index"],
        ),
    )
    selected = ranked_geographies[0] if ranked_geographies else {}
    country = next((
        geography["country"] for geography in ranked_geographies
        if geography["country"]
    ), "")
    city = next((
        geography["city"] for geography in ranked_geographies
        if geography["city"]
        and (not country or not geography["country"] or geography["country"] == country)
    ), "")
    # Maps locations may be complete street addresses. Keep that useful source
    # text; query-derived candidates already carry the compact city/country form.
    location = selected.get("location", "") or format_location(city, country)

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
        "map_place_id": map_place_id,
        "maps_identity": maps_identity,
        "country": country,
        "city": city,
        "location": location,
        "added_at": earliest_added_at(group["added_at_values"]),
        "email_status": status,
        "review_status": "REVIEW" if reasons else "READY",
        "review_reasons": "; ".join(sorted(reasons)),
        "source": ";".join(group["sources"]),
        "source_queries": ";".join(group["source_queries"]),
    }


def write_csv(path, fieldnames, rows):
    """Write and durably flush a complete CSV at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        file_handler.flush()
        os.fsync(file_handler.fileno())


def _fsync_directory(path):
    """Persist directory-entry changes on filesystems that support it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some non-POSIX filesystems do not support directory fsync. File data
        # is still fsynced and every publication rename remains atomic.
        pass
    finally:
        os.close(descriptor)


def _temporary_path(folder, prefix):
    with NamedTemporaryFile(dir=folder, prefix=prefix, delete=False) as handle:
        return Path(handle.name)


def _atomic_write_json(path, value):
    temporary = _temporary_path(path.parent, f".{path.name}.tmp-")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_path(folder, filename):
    return folder / f".{filename}.snapshot-backup"


def _copy_durable(source, destination):
    copyfile(source, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def _remove_transaction_files(folder, transaction):
    # Remove and persist the recovery instruction first. Backups are retained
    # until that point, so a crash during cleanup can never leave a journal
    # referring to already-deleted recovery data.
    (folder / SNAPSHOT_PENDING).unlink(missing_ok=True)
    _fsync_directory(folder)
    for details in transaction.get("files", {}).values():
        backup_name = details.get("backup")
        if backup_name:
            (folder / backup_name).unlink(missing_ok=True)
    _fsync_directory(folder)


def _rollback_snapshot(folder, transaction):
    """Idempotently restore every path recorded by a pending transaction."""
    restore_temps = []
    try:
        for filename, details in transaction["files"].items():
            final_path = folder / filename
            if details["existed"]:
                backup_path = folder / details["backup"]
                if not backup_path.exists():
                    raise RuntimeError(
                        f"Cannot recover lead snapshot: missing {backup_path.name}"
                    )
                restore_path = _temporary_path(folder, f".{filename}.restore-")
                restore_temps.append(restore_path)
                _copy_durable(backup_path, restore_path)
                os.replace(restore_path, final_path)
            else:
                final_path.unlink(missing_ok=True)
        _fsync_directory(folder)
    finally:
        for restore_path in restore_temps:
            restore_path.unlink(missing_ok=True)


def recover_interrupted_snapshot(output_folder):
    """Recover an interrupted prior publication before a normal build."""
    output_folder = Path(output_folder)
    pending_path = output_folder / SNAPSHOT_PENDING
    if not pending_path.exists():
        return False
    with pending_path.open("r", encoding="utf-8") as handle:
        transaction = load(handle)

    manifest_path = output_folder / SNAPSHOT_MANIFEST
    committed_generation = ""
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            committed_generation = load(handle).get("generation_id", "")
    if committed_generation != transaction.get("generation_id"):
        _rollback_snapshot(output_folder, transaction)
    _remove_transaction_files(output_folder, transaction)
    return True


def publish_lead_snapshot(output_folder, outputs):
    """Publish the three lead CSVs as one recoverable logical generation.

    Filesystems do not provide a multi-file atomic rename. The pending journal
    therefore precedes the rename window, while the committed manifest follows
    it. Handled failures roll back immediately; a killed process is recovered
    by ``recover_interrupted_snapshot`` on the next normal build.
    """
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    recover_interrupted_snapshot(output_folder)
    generation_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    staged = {}
    transaction = {
        "schema_version": 1,
        "generation_id": generation_id,
        "files": {},
    }
    pending_written = False
    commit_complete = False
    rollback_complete = False
    try:
        # Nothing under a final filename changes until every CSV is complete.
        for filename, fieldnames, rows in outputs:
            stage_path = _temporary_path(output_folder, f".{filename}.tmp-")
            staged[filename] = stage_path
            write_csv(stage_path, fieldnames, rows)

        manifest_files = {}
        for filename, _fieldnames, rows in outputs:
            final_path = output_folder / filename
            backup_path = _backup_path(output_folder, filename)
            backup_path.unlink(missing_ok=True)
            existed = final_path.exists()
            if existed:
                _copy_durable(final_path, backup_path)
            transaction["files"][filename] = {
                "backup": backup_path.name,
                "existed": existed,
            }
            manifest_files[filename] = {
                "rows": len(rows),
                "sha256": sha256(staged[filename].read_bytes()).hexdigest(),
            }

        _atomic_write_json(output_folder / SNAPSHOT_PENDING, transaction)
        pending_written = True
        for filename, _fieldnames, _rows in outputs:
            os.replace(staged[filename], output_folder / filename)
        _fsync_directory(output_folder)
        _atomic_write_json(output_folder / SNAPSHOT_MANIFEST, {
            "schema_version": 1,
            "generation_id": generation_id,
            "created_at": created_at,
            "files": manifest_files,
        })
        commit_complete = True
    except BaseException:
        if pending_written and not commit_complete:
            try:
                _rollback_snapshot(output_folder, transaction)
                rollback_complete = True
            except BaseException as recovery_error:
                raise RuntimeError(
                    "Lead snapshot publication failed and automatic rollback was "
                    "incomplete; rerun build_leads to recover from the pending journal"
                ) from recovery_error
        raise
    finally:
        for stage_path in staged.values():
            stage_path.unlink(missing_ok=True)
        # After a failed rollback, retain the journal/backups for retry.
        if commit_complete or rollback_complete or not pending_written:
            try:
                _remove_transaction_files(output_folder, transaction)
            except OSError:
                # The snapshot is already committed/restored. Any backup left
                # behind is deterministic and removed before the next commit.
                pass


def search_rows(search_input_path):
    """Adapt Search discovery rows to the raw schema expected by ``build_groups``."""
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
                "country": row.get("country", ""),
                "city": row.get("city", ""),
                "location": row.get("location", ""),
                "added_at": row.get("added_at", ""),
            }
            for row in DictReader(file_handler)
        ]


def enriched_email_rows(email_enrichment_input_path, default_source="google_search"):
    """Adapt validated FOUND enrichment results into raw lead-builder rows."""
    if not email_enrichment_input_path or not email_enrichment_input_path.exists():
        return []
    rows = []
    with email_enrichment_input_path.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file_handler:
        for row in DictReader(file_handler):
            enrichment_status = clean_value(
                row.get("email_enrichment_status") or row.get("status")
            ).upper()
            if enrichment_status != "FOUND":
                continue
            emails = [
                email
                for field in ("email", "alternate_emails")
                for email in extract_emails(row.get(field))
                if is_valid_email(email)
            ]
            if not emails:
                continue
            sources = {
                value.strip().casefold()
                for value in clean_value(row.get("source")).split(";")
                if value.strip()
            }
            source = (
                "google_maps" if "google_maps" in sources
                else "google_search" if "google_search" in sources
                else default_source
            )
            rows.append({
                "title": row.get("name") or row.get("company_name", ""),
                "webpage": row.get("website", ""),
                "phone_number": row.get("phone", ""),
                "map_place_id": row.get("map_place_id", ""),
                "maps_identity": row.get("maps_identity", ""),
                "site_email": ";".join(emails),
                "_source": source,
                "_source_queries": row.get("source_queries") or row.get("source_query", ""),
                "country": row.get("country", ""),
                "city": row.get("city", ""),
                "location": row.get("location", ""),
                "added_at": row.get("added_at", ""),
            })
    return rows


def enriched_search_rows(email_enrichment_input_path):
    """Backward-compatible adapter for historical Search enrichment state."""
    return enriched_email_rows(email_enrichment_input_path, "google_search")


def preserve_master_added_at(groups, master_path):
    """Merge prior master timestamps into matching current groups only."""
    if not master_path.exists():
        return
    domain_index = defaultdict(set)
    place_id_index = defaultdict(set)
    place_url_index = defaultdict(set)
    name_phone_index = defaultdict(set)
    name_email_index = defaultdict(set)
    name_index = defaultdict(set)
    for group_id, group in enumerate(groups):
        for place_id in group["map_place_ids"]:
            place_id_index[place_id].add(group_id)
        for place_url in group["map_place_urls"]:
            place_url_index[place_url].add(group_id)
        for domain in group["website_domains"]:
            domain_index[domain].add(group_id)
        for name in group["names"]:
            name_key = normalize_name(name)
            if not name_key:
                continue
            name_index[name_key].add(group_id)
            for phone_key in group["phone_keys"]:
                name_phone_index[(name_key, phone_key)].add(group_id)
            for email in group["emails"]:
                email_domain = email.rsplit("@", 1)[1]
                if email_domain not in FREE_EMAIL_DOMAINS:
                    name_email_index[(name_key, email_domain)].add(group_id)

    with master_path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        prior_rows = list(DictReader(file_handler))
    for row in prior_rows:
        timestamp = row.get("added_at", "")
        if not earliest_added_at((timestamp,)):
            continue
        candidates = set()
        maps_identities = maps_identities_for(row)
        if maps_identities["place_id"]:
            candidates.update(place_id_index[maps_identities["place_id"]])
        if maps_identities["place_url"]:
            candidates.update(place_url_index[maps_identities["place_url"]])
        domain = website_domain(row.get("website"))
        if domain:
            candidates.update(domain_index[domain])
        name_key = normalize_name(row.get("name"))
        phone_key = normalize_phone(row.get("phone"))
        if name_key and phone_key:
            candidates.update(name_phone_index[(name_key, phone_key)])
        if name_key:
            for email in extract_emails(
                ";".join((row.get("email", ""), row.get("alternate_emails", "")))
            ):
                if is_valid_email(email):
                    email_domain = email.rsplit("@", 1)[1].casefold()
                    if email_domain not in FREE_EMAIL_DOMAINS:
                        candidates.update(name_email_index[(name_key, email_domain)])
        if not candidates and name_key:
            compatible = {
                group_id for group_id in name_index[name_key]
                if compatible_name_fallback(
                    groups[group_id], domain, phone_key,
                    {email.rsplit("@", 1)[1].casefold()
                     for email in extract_emails(row.get("email", ""))
                     if is_valid_email(email)},
                )
            }
            if len(compatible) == 1:
                candidates = compatible
        if len(candidates) == 1:
            groups[next(iter(candidates))]["added_at_values"].append(timestamp)


def load_lead_input_rows(
    input_path,
    output_folder,
    search_input_path=None,
    email_enrichment_input_path=None,
    missing_email_enrichment_input_path=None,
    search_email_fallback_input_path=None,
):
    """Read and adapt all discovery inputs without changing any files."""
    input_path = Path(input_path)
    output_folder = Path(output_folder)
    search_input_path = Path(search_input_path) if search_input_path else None
    email_enrichment_input_path = (
        Path(email_enrichment_input_path) if email_enrichment_input_path else None
    )
    missing_email_enrichment_input_path = (
        Path(missing_email_enrichment_input_path)
        if missing_email_enrichment_input_path else None
    )
    search_email_fallback_input_path = (
        Path(search_email_fallback_input_path)
        if search_email_fallback_input_path else None
    )
    with input_path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        raw_rows = list(DictReader(file_handler))
    # Maps enters first so its richer phone and email fields remain preferred
    # when a later Search discovery resolves to the same company.
    for row in raw_rows:
        row["_source"] = "google_maps"
        row["_source_queries"] = row.get("source_query", "")

    if search_input_path is None:
        search_input_path = output_folder / "google_search_companies.csv"
    discovered_rows = search_rows(search_input_path)
    raw_rows.extend(discovered_rows)
    if email_enrichment_input_path is None:
        email_enrichment_input_path = output_folder / "search_email_enriched.csv"
    raw_rows.extend(enriched_search_rows(email_enrichment_input_path))
    if missing_email_enrichment_input_path is None:
        missing_email_enrichment_input_path = output_folder / "missing_email_enriched.csv"
    raw_rows.extend(enriched_email_rows(missing_email_enrichment_input_path, "google_maps"))
    if search_email_fallback_input_path is None:
        search_email_fallback_input_path = output_folder / "search_email_fallback.csv"
    # Fallback discovers contact evidence, not a new company-discovery source.
    # Existing Search rows already retain google_search provenance when present.
    raw_rows.extend(enriched_email_rows(search_email_fallback_input_path, "google_maps"))
    return raw_rows


def analyze_identity_changes(
    input_path,
    output_folder,
    search_input_path=None,
    email_enrichment_input_path=None,
    missing_email_enrichment_input_path=None,
    search_email_fallback_input_path=None,
):
    """Print a read-only identity analysis; never write production CSVs."""
    output_folder = Path(output_folder)
    raw_rows = load_lead_input_rows(
        input_path, output_folder, search_input_path, email_enrichment_input_path,
        missing_email_enrichment_input_path, search_email_fallback_input_path,
    )
    groups, _ = build_groups(raw_rows)
    master_path = output_folder / "leads_master.csv"
    if master_path.exists():
        with master_path.open("r", newline="", encoding="utf-8-sig") as handle:
            current_master_count = sum(1 for _ in DictReader(handle))
    else:
        current_master_count = 0
    bridge_groups = sum(
        "identity bridge conflict" in group["review_reasons"] for group in groups
    )
    conflict_reasons = {
        "conflicting strong identities", "conflicting maps identities",
        "shared domain across distinct places", "identity bridge conflict",
    }
    conflict_review_groups = sum(
        bool(group["review_reasons"] & conflict_reasons) for group in groups
    )
    same_place_merges = sum(group["match_counts"]["maps"] for group in groups)
    print("READ-ONLY identity comparison (no files written)")
    print(f"Current master companies: {current_master_count}")
    print(f"New unique companies from current inputs: {len(groups)}")
    print(f"Groups involved in prevented bridge merges: {bridge_groups}")
    print(f"New identity-conflict review groups: {conflict_review_groups}")
    print(f"Rows newly joined by same-place identity: {same_place_merges}")


def build_lead_files(
    input_path,
    output_folder,
    search_input_path=None,
    email_enrichment_input_path=None,
    missing_email_enrichment_input_path=None,
    search_email_fallback_input_path=None,
):
    """Merge all discovery sources and write master, ready, and review CSV files."""
    output_folder = Path(output_folder)
    recover_interrupted_snapshot(output_folder)
    raw_rows = load_lead_input_rows(
        input_path, output_folder, search_input_path, email_enrichment_input_path,
        missing_email_enrichment_input_path, search_email_fallback_input_path,
    )

    groups, rejected_emails = build_groups(raw_rows)
    # A rebuild may have fewer/different source rows, but an existing master is
    # authoritative timestamp evidence. Match it using the same strong company
    # identities without adding old companies back into the current dataset.
    preserve_master_added_at(groups, output_folder / "leads_master.csv")
    master_rows = [finalize_group(group) for group in groups]
    ready_rows = [
        {field: row[field] for field in READY_FIELDS}
        for row in master_rows
        if row["email"] and row["review_status"] == "READY"
    ]
    review_rows = [row for row in master_rows if row["review_status"] == "REVIEW"]

    publish_lead_snapshot(output_folder, (
        ("leads_master.csv", MASTER_FIELDS, master_rows),
        ("leads_ready.csv", READY_FIELDS, ready_rows),
        ("leads_review.csv", MASTER_FIELDS, review_rows),
    ))

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
    parser.add_argument(
        "--missing-email-enrichment-input",
        type=Path,
        help="Optional all-source missing-email enrichment CSV (defaults to the output folder)",
    )
    parser.add_argument(
        "--search-email-fallback-input",
        type=Path,
        help="Optional Google Search email fallback CSV (defaults to the output folder)",
    )
    parser.add_argument(
        "--analyze-identities",
        action="store_true",
        help="Compare identity grouping read-only; do not write lead CSVs",
    )
    args = parser.parse_args()
    operation = analyze_identity_changes if args.analyze_identities else build_lead_files
    operation(
        args.input,
        args.output_folder,
        args.search_input,
        args.email_enrichment_input,
        args.missing_email_enrichment_input,
        args.search_email_fallback_input,
    )


if __name__ == "__main__":
    main()
