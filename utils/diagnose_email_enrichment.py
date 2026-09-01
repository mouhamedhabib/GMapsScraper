"""Manual, network-dependent diagnostic for the production email scraper path."""

from argparse import ArgumentParser

if __package__:
    from utils.enrich_leads import create_chrome_driver
    from utils.enrich_search_emails import (
        candidate_urls,
        extract_public_emails,
        set_record_emails,
    )
    from utils.web_site_scraper import PatternScrapper
else:
    from enrich_leads import create_chrome_driver
    from enrich_search_emails import candidate_urls, extract_public_emails, set_record_emails
    from web_site_scraper import PatternScrapper


CONTROL_WEBSITES = ("https://lexa.tn", "https://swiver.io", "https://mintit.io")


def _scraper(timeout):
    return PatternScrapper(
        wait_time=timeout,
        overall_timeout=timeout,
        verbose=False,
    )


def diagnose_website(driver, website, timeout):
    """Print direct extraction and shared enrichment-path results for one site."""
    direct = _scraper(timeout)
    urls = candidate_urls(website, direct.create_urls)
    sources = direct.get_source_code(driver, urls)
    raw = direct.get_raw_pattern_data(sources)["site_email"]
    validated = direct.get_pattern_data(sources)["site_email"]
    direct_record = {"website": website}
    set_record_emails(direct_record, validated)

    integrated = _scraper(timeout)
    integrated_record = {"website": website}
    try:
        integrated_emails = extract_public_emails(integrated, driver, website)
        set_record_emails(integrated_record, integrated_emails)
        integrated_status = "FOUND" if integrated_record.get("email") else "NOT_FOUND"
    except Exception as error:
        integrated_emails = []
        integrated_status = f"FAILED ({type(error).__name__}: {error})"

    print(f"Website: {website}")
    for url in direct.last_attempted_urls:
        print(f"Candidate: {url}")
    print(f"Raw emails: {raw}")
    print(f"Valid emails: {validated}")
    print(f"Final: {direct_record.get('email', '')}")
    if direct_record.get("email"):
        direct_status = "FOUND"
    elif direct.last_failure_kind:
        direct_status = f"FAILED_{direct.last_failure_kind.upper()}"
    else:
        direct_status = "NOT_FOUND"
    print(f"Direct status: {direct_status}")
    print(f"Enrichment emails: {integrated_emails}")
    print(f"Enrichment final: {integrated_record.get('email', '')}")
    print(f"Enrichment status: {integrated_status}")
    print()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=15)
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    driver = create_chrome_driver()
    try:
        for website in CONTROL_WEBSITES:
            diagnose_website(driver, website, arguments.timeout)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
