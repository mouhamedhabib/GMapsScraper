"""Resumable batch company enrichment for the curated leads-ready CSV."""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from os import replace
from pathlib import Path
from platform import system as platform_system
from re import search
from subprocess import DEVNULL, check_output
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit


DEFAULT_INPUT = Path("./CSV_FILES/leads_ready.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/leads_enriched.csv")
MAX_LIFETIME_ATTEMPTS = 3

OUTPUT_FIELDS = (
    "company_name",
    "email",
    "website",
    "description",
    "industry",
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
    "location",
    "linkedin_url",
    "phone",
    "enrichment_status",
    "enrichment_attempts",
)

ENRICHMENT_FIELDS = (
    "description",
    "industry",
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
    "linkedin_url",
)

SOURCE_CONTEXT_FIELDS = (
    "services",
    "website_title",
    "website_meta_description",
    "hero_text",
    "about_text",
)

UNAVAILABLE_VALUES = {
    "",
    "n/a",
    "none",
    "not available",
    "null",
    "unknown",
}

DRIVER_FAILURE_NAMES = {
    "BrowserUnavailableError",
    "InvalidSessionIdException",
    "NoSuchWindowException",
    "SessionNotCreatedException",
    "WebDriverException",
}

DRIVER_FAILURE_MARKERS = (
    "chrome not reachable",
    "disconnected",
    "invalid session id",
    "no such window",
    "session deleted",
    "target window already closed",
)


def clean_value(value):
    cleaned = " ".join(str(value or "").split()).strip()
    return "" if cleaned.casefold() in UNAVAILABLE_VALUES else cleaned


def normalize_company_name(value):
    return "".join(
        character for character in clean_value(value).casefold()
        if character.isalnum()
    )


def normalize_website_domain(value):
    value = clean_value(value)
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else "//" + value)
    domain = (parsed.hostname or "").casefold().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def parse_attempts(value):
    try:
        return max(0, int(clean_value(value) or "0"))
    except ValueError:
        return 0


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


def add_location(index, key, location):
    if key and location:
        index.setdefault(key, set()).add(location)


def load_location_indexes(input_path):
    domain_locations = {}
    name_locations = {}
    local_sources = (
        input_path.parent / "leads_master.csv",
        input_path.parent / "google_maps_data.csv",
    )
    for source_path in local_sources:
        if source_path == input_path or not source_path.exists():
            continue
        for row in read_csv(source_path):
            location = clean_value(row.get("location") or row.get("address"))
            if not location:
                continue
            name = row.get("company_name") or row.get("name") or row.get("title")
            website = row.get("website") or row.get("webpage")
            add_location(domain_locations, normalize_website_domain(website), location)
            add_location(name_locations, normalize_company_name(name), location)
    return domain_locations, name_locations


def unique_location(index, key):
    values = index.get(key, set())
    return next(iter(values)) if len(values) == 1 else ""


def recover_location(row, domain_locations, name_locations):
    direct = clean_value(row.get("location"))
    if direct:
        return direct
    domain = normalize_website_domain(row.get("website"))
    location = unique_location(domain_locations, domain) if domain else ""
    if location:
        return location
    name = normalize_company_name(row.get("name") or row.get("company_name"))
    return unique_location(name_locations, name) if name else ""


def input_identity(row, row_index):
    domain = normalize_website_domain(row.get("website"))
    if domain:
        return "domain", domain
    name = normalize_company_name(row.get("name") or row.get("company_name"))
    return ("name", name) if name else ("row", str(row_index))


def deduplicate_input_rows(rows):
    deduplicated = []
    identity_indexes = {}
    for row_index, row in enumerate(rows):
        identity = input_identity(row, row_index)
        existing_index = identity_indexes.get(identity)
        if existing_index is None:
            identity_indexes[identity] = len(deduplicated)
            deduplicated.append(dict(row))
            continue
        existing = deduplicated[existing_index]
        for field in ("name", "email", "website", "phone", "location"):
            if not clean_value(existing.get(field)) and clean_value(row.get(field)):
                existing[field] = row[field]
    return deduplicated


def build_existing_indexes(rows):
    domain_index = {}
    name_index = {}
    for row in rows:
        domain = normalize_website_domain(row.get("website"))
        name = normalize_company_name(row.get("company_name"))
        if domain:
            domain_index.setdefault(domain, []).append(row)
        if name:
            name_index.setdefault(name, []).append(row)
    return domain_index, name_index


def existing_matches(row, domain_index, name_index):
    domain = normalize_website_domain(row.get("website"))
    if domain and domain in domain_index:
        return domain_index[domain]
    name = normalize_company_name(row.get("name") or row.get("company_name"))
    matches = name_index.get(name, []) if name else []
    return matches if len(matches) == 1 else []


def blank_output_record():
    return {field: "" for field in OUTPUT_FIELDS}


def merge_existing_records(records):
    merged = blank_output_record()
    attempts = 0
    for record in records:
        for field in OUTPUT_FIELDS:
            value = clean_value(record.get(field))
            if value and not clean_value(merged.get(field)):
                merged[field] = value
        attempts = max(attempts, parse_attempts(record.get("enrichment_attempts")))
    merged["enrichment_attempts"] = str(attempts)
    return merged


def has_useful_enrichment(record):
    return any(clean_value(record.get(field)) for field in ENRICHMENT_FIELDS)


def enrichment_status(record, unexpected_failure=False):
    if not normalize_website_domain(record.get("website")):
        return "NO_WEBSITE"
    has_source = any(
        clean_value(record.get(field)) for field in SOURCE_CONTEXT_FIELDS
    )
    if (clean_value(record.get("description"))
            and clean_value(record.get("industry"))
            and has_source):
        return "SUCCESS"
    if has_useful_enrichment(record):
        return "PARTIAL"
    if unexpected_failure:
        return "FAILED"
    if parse_attempts(record.get("enrichment_attempts")) == 0:
        return "PENDING"
    return "INCOMPLETE"


def prepare_records(input_path, output_path):
    input_rows = deduplicate_input_rows(read_csv(input_path))
    existing_rows = read_csv(output_path)
    domain_index, name_index = build_existing_indexes(existing_rows)
    domain_locations, name_locations = load_location_indexes(input_path)
    records = []
    already_enriched = 0

    for input_row in input_rows:
        existing = merge_existing_records(
            existing_matches(input_row, domain_index, name_index)
        )
        record = blank_output_record()
        record.update(existing)

        source_values = {
            "company_name": input_row.get("name") or input_row.get("company_name"),
            "email": input_row.get("email"),
            "website": input_row.get("website"),
            "phone": input_row.get("phone"),
        }
        for field, value in source_values.items():
            cleaned = clean_value(value)
            if cleaned:
                record[field] = cleaned

        if not clean_value(record.get("location")):
            record["location"] = recover_location(
                input_row,
                domain_locations,
                name_locations,
            )

        attempts = parse_attempts(record.get("enrichment_attempts"))
        record["enrichment_attempts"] = str(attempts)
        prior_failure = clean_value(existing.get("enrichment_status")) == "FAILED"
        record["enrichment_status"] = enrichment_status(
            record,
            unexpected_failure=prior_failure,
        )
        if record["enrichment_status"] == "SUCCESS":
            already_enriched += 1
        records.append(record)

    return records, already_enriched


def merge_enrichment(record, enrichment):
    for field in ENRICHMENT_FIELDS:
        value = clean_value((enrichment or {}).get(field))
        if value:
            record[field] = value


def load_enrichment_function():
    if __package__:
        from utils.company_enrichment import enrich_company
    else:
        from company_enrichment import enrich_company
    return enrich_company


def detect_chrome_major_version(uc_module):
    try:
        chrome_path = uc_module.find_chrome_executable()
        if not chrome_path:
            return 0
        if platform_system().casefold() == "windows":
            command = [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Item '{chrome_path}').VersionInfo.ProductVersion",
            ]
        else:
            command = [chrome_path, "--version"]
        output = check_output(command, stderr=DEVNULL).decode("utf-8", "ignore")
        match = search(r"(\d+)\.", output)
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def create_chrome_driver():
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--disable-popup-blocking")
    chrome_version = detect_chrome_major_version(uc)
    return uc.Chrome(
        options=options,
        headless=True,
        use_subprocess=False,
        version_main=chrome_version or None,
    )


def is_driver_failure(error):
    if type(error).__name__ in DRIVER_FAILURE_NAMES:
        return True
    message = str(error).casefold()
    return any(marker in message for marker in DRIVER_FAILURE_MARKERS)


def close_driver(driver):
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


class BrowserSession:
    """Reuse one driver and permit at most one replacement session."""

    def __init__(self, driver_factory):
        self.driver_factory = driver_factory
        self.driver = None
        self.starts = 0

    def get(self):
        last_error = None
        while self.driver is None and self.starts < 2:
            self.starts += 1
            try:
                self.driver = self.driver_factory()
            except Exception as error:
                last_error = error
        if self.driver is None:
            error = BrowserUnavailableError("Chrome driver is unavailable")
            if last_error:
                raise error from last_error
            raise error
        return self.driver

    def invalidate(self):
        close_driver(self.driver)
        self.driver = None

    def can_recreate(self):
        return self.driver is not None or self.starts < 2

    def close(self):
        close_driver(self.driver)
        self.driver = None


class BrowserUnavailableError(RuntimeError):
    pass


def summarize(records, already_enriched, attempted, skipped_max_attempts):
    statuses = {
        status: sum(record["enrichment_status"] == status for record in records)
        for status in (
            "PENDING",
            "SUCCESS",
            "PARTIAL",
            "INCOMPLETE",
            "NO_WEBSITE",
            "FAILED",
        )
    }
    summary = {
        "Total leads": len(records),
        "Pending": statuses["PENDING"],
        "Already enriched": already_enriched,
        "Attempted this run": attempted,
        "Success": statuses["SUCCESS"],
        "Partial": statuses["PARTIAL"],
        "Incomplete": statuses["INCOMPLETE"],
        "No website": statuses["NO_WEBSITE"],
        "Failed": statuses["FAILED"],
        "Skipped max attempts": skipped_max_attempts,
    }
    for label, value in summary.items():
        print(f"{label}: {value}")
    return summary


def enrich_leads_batch(
    input_path=DEFAULT_INPUT,
    output_path=DEFAULT_OUTPUT,
    timeout=15,
    limit=None,
    verbose=False,
    retry_failed=False,
    driver_factory=None,
    enrichment_function=None,
):
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    records, already_enriched = prepare_records(input_path, output_path)
    atomic_write_csv(output_path, records)

    driver_factory = driver_factory or create_chrome_driver
    browser = BrowserSession(driver_factory)
    attempted = 0
    skipped_max_attempts = 0
    browser_exhausted = False

    try:
        for record in records:
            status = record["enrichment_status"]
            if status in {"SUCCESS", "NO_WEBSITE"}:
                continue
            attempts = parse_attempts(record.get("enrichment_attempts"))
            if attempts >= MAX_LIFETIME_ATTEMPTS and not retry_failed:
                skipped_max_attempts += 1
                continue
            if limit is not None and attempted >= limit:
                continue

            attempted += 1
            attempts += 1
            record["enrichment_attempts"] = str(attempts)
            unexpected_failure = False

            try:
                driver = browser.get()
                if enrichment_function is None:
                    enrichment_function = load_enrichment_function()
                enrichment = enrichment_function(
                    driver,
                    record["website"],
                    timeout=timeout,
                    verbose=verbose,
                )
                merge_enrichment(record, enrichment)
            except Exception as error:
                unexpected_failure = True
                if verbose:
                    print(
                        f"[-] Enrichment failed for {record['company_name'] or record['website']}: "
                        f"{type(error).__name__}: {error}"
                    )
                if is_driver_failure(error):
                    browser.invalidate()
                    if not browser.can_recreate():
                        browser_exhausted = True

            record["enrichment_status"] = enrichment_status(
                record,
                unexpected_failure=unexpected_failure,
            )
            atomic_write_csv(output_path, records)

            if verbose:
                print(
                    f"[{record['enrichment_status']}] "
                    f"{record['company_name'] or record['website']}"
                )
            if browser_exhausted:
                break
    finally:
        browser.close()
        atomic_write_csv(output_path, records)

    return summarize(records, already_enriched, attempted, skipped_max_attempts)


def main():
    parser = ArgumentParser(
        description="Enrich curated leads with existing company website extraction"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be zero or greater")

    enrich_leads_batch(
        input_path=args.input,
        output_path=args.output,
        timeout=args.timeout,
        limit=args.limit,
        verbose=args.verbose,
        retry_failed=args.retry_failed,
    )


if __name__ == "__main__":
    main()
