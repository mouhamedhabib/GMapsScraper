"""
Missing-Email Lead Enrichment
=============================

Find public website emails for any master lead that lacks a valid email,
regardless of discovery source. Progress is checkpointed after every attempt.
"""

from argparse import ArgumentParser
from pathlib import Path
from random import Random
from urllib.parse import urlsplit

if __package__:
    from utils.build_leads import website_domain
    from utils.enrich_search_emails import (
        DEFAULT_INPUT,
        MAX_LIFETIME_ATTEMPTS,
        existing_index,
        parse_attempts,
        prepare_records,
        read_csv,
        row_identity,
        run_email_enrichment,
        valid_row_emails,
    )
else:  # Support direct execution from the utils directory.
    from build_leads import website_domain
    from enrich_search_emails import (
        DEFAULT_INPUT,
        MAX_LIFETIME_ATTEMPTS,
        existing_index,
        parse_attempts,
        prepare_records,
        read_csv,
        row_identity,
        run_email_enrichment,
        valid_row_emails,
    )


DEFAULT_OUTPUT = Path("./CSV_FILES/missing_email_enriched.csv")


def is_missing_email(row):
    """Return whether neither primary nor alternate fields contain a valid email."""
    return not valid_row_emails(row)


def has_usable_website(row):
    """Accept a normalized web domain with either HTTP(S) or no explicit scheme."""
    value = str(row.get("website") or "").strip()
    if not website_domain(value):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme or parsed.scheme.casefold() in {"http", "https"}


def summarize_missing_email_run(input_rows, counters, prior_found):
    missing_rows = [row for row in input_rows if is_missing_email(row)]
    eligible = sum(has_usable_website(row) for row in missing_rows)
    summary = {
        "Total leads": len(input_rows),
        "Missing-email leads": len(missing_rows),
        "Eligible with website": eligible,
        "Without website": len(missing_rows) - eligible,
        "Attempted this run": counters["attempted"],
        "Found": counters["found"],
        "Not found": counters["not_found"],
        "Failed": counters["failed"],
        "Skipped existing result": counters["skipped_existing_result"] + prior_found,
        "Skipped max attempts": counters["skipped_max_attempts"],
    }
    for label, value in summary.items():
        print(f"{label}: {value}")
    rate = (
        counters["found"] / counters["attempted"] * 100
        if counters["attempted"] else 0.0
    )
    summary["Recovery rate"] = rate
    print(f"Recovery rate: {rate:.1f}%")
    return summary


def enrich_missing_emails(
    input_path=DEFAULT_INPUT,
    output_path=DEFAULT_OUTPUT,
    limit=None,
    timeout=15,
    verbose=False,
    retry_failed=False,
    driver_factory=None,
    scraper_factory=None,
    random_sample=False,
    seed=None,
):
    """Enrich every missing-email master lead that has a usable website."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    input_rows = read_csv(input_path)
    previous = existing_index(read_csv(output_path))
    prior_found = 0
    for row_index, row in enumerate(input_rows):
        prior = previous.get(row_identity(row, row_index), {})
        if (
            is_missing_email(row)
            and str(prior.get("email_enrichment_status") or "").strip().upper() == "FOUND"
            and valid_row_emails(prior)
        ):
            prior_found += 1

    records = prepare_records(
        input_path,
        output_path,
        target_predicate=is_missing_email,
        website_predicate=has_usable_website,
    )
    target_predicate = is_missing_email
    excluded_max_attempts = 0
    excluded_existing_result = 0
    if random_sample:
        candidates = []
        for record in records:
            if not is_missing_email(record):
                continue
            status = record["email_enrichment_status"]
            if status in {"FOUND", "NO_WEBSITE"}:
                continue
            if status == "FAILED" and not retry_failed:
                excluded_existing_result += 1
                continue
            if parse_attempts(record["email_enrichment_attempts"]) >= MAX_LIFETIME_ATTEMPTS:
                excluded_max_attempts += 1
                continue
            candidates.append(record)

        sample_size = len(candidates) if limit is None else min(limit, len(candidates))
        selected_ids = {
            id(record) for record in Random(seed).sample(candidates, sample_size)
        }
        target_predicate = lambda record: id(record) in selected_ids

    counters = run_email_enrichment(
        records=records,
        output_path=output_path,
        target_predicate=target_predicate,
        limit=None if random_sample else limit,
        timeout=timeout,
        verbose=verbose,
        retry_failed=retry_failed,
        driver_factory=driver_factory,
        scraper_factory=scraper_factory,
    )
    counters["skipped_max_attempts"] += excluded_max_attempts
    counters["skipped_existing_result"] += excluded_existing_result
    return summarize_missing_email_run(input_rows, counters, prior_found)


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--random-sample",
        action="store_true",
        help="Select --limit attemptable leads randomly instead of by CSV order",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Optional deterministic seed for --random-sample",
    )
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 0:
        parser.error("--limit must be zero or greater")
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return arguments


def main():
    arguments = parse_arguments()
    enrich_missing_emails(
        input_path=arguments.input,
        output_path=arguments.output,
        limit=arguments.limit,
        timeout=arguments.timeout,
        verbose=arguments.verbose,
        retry_failed=arguments.retry_failed,
        random_sample=arguments.random_sample,
        seed=arguments.seed,
    )


if __name__ == "__main__":
    main()
