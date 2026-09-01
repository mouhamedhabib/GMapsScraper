"""Offline tests for Google Search discovery and lead integration."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.build_leads import MASTER_FIELDS, READY_FIELDS, build_lead_files, website_domain
from utils.google_search_discovery import (
    DISCOVERY_FIELDS,
    atomic_write_discoveries,
    is_suitable_company_url,
    is_suitable_company_result,
    merge_discoveries,
    read_discoveries,
)


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def maps_row(name="Maps Company", website="https://maps.test", email="info@maps.test"):
    return {
        "title": name,
        "webpage": website,
        "phone_number": "+216 70 000 000",
        "site_email": email,
    }


def search_row(name="Search Company", website="https://search.test/about", query="software"):
    return {
        "company_name": name,
        "website": website,
        "source": "google_search",
        "source_query": query,
        "source_url": website,
    }


class NormalizationAndFilteringTests(TestCase):
    def test_domain_normalization(self):
        self.assertEqual(website_domain("HTTPS://Example.COM./path?q=1#x"), "example.com")

    def test_www_and_path_normalization(self):
        values = (
            "https://example.com",
            "http://example.com/",
            "https://www.example.com/about",
            "https://example.com/services",
        )
        self.assertEqual({website_domain(value) for value in values}, {"example.com"})

    def test_blocked_domain(self):
        self.assertFalse(is_suitable_company_url("https://jobs.linkedin.com/view/1"))
        self.assertFalse(is_suitable_company_url("https://www.google.tn/search?q=x"))

    def test_pdf_filtering(self):
        self.assertFalse(is_suitable_company_url("https://company.test/brochure.PDF?download=1"))

    def test_ranked_startup_article_is_discovery_noise(self):
        self.assertFalse(is_suitable_company_result(
            "163 Top startups in Tunisia for August 2026",
            "https://directory.test/blog/top-startups-tunisia",
        ))


class DiscoveryMergeTests(TestCase):
    def test_duplicate_same_domain(self):
        rows = merge_discoveries([], [
            search_row(website="https://www.acme.test/about"),
            search_row(website="http://acme.test/services"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["website"], "https://acme.test/")

    def test_duplicate_across_queries_preserves_queries(self):
        rows = merge_discoveries([], [
            search_row(website="https://acme.test/a", query="software Tunisia"),
            search_row(website="https://acme.test/b", query="SaaS Tunisia"),
        ])
        self.assertEqual(rows[0]["source_query"], "software Tunisia;SaaS Tunisia")

    def test_empty_results(self):
        self.assertEqual(merge_discoveries([], []), [])

    def test_repeated_output_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "discoveries.csv"
            row = search_row(website="https://acme.test/about")
            first = merge_discoveries(read_discoveries(path), [row])
            atomic_write_discoveries(path, first)
            second = merge_discoveries(read_discoveries(path), [row])
            atomic_write_discoveries(path, second)
            self.assertEqual(len(read_discoveries(path)), 1)
            self.assertEqual(tuple(read_csv(path)[0]), DISCOVERY_FIELDS)


class BuildLeadIntegrationTests(TestCase):
    def build(self, maps_rows, search_rows=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        maps_path = root / "google_maps_data.csv"
        write_csv(
            maps_path,
            ("title", "webpage", "phone_number", "site_email"),
            maps_rows,
        )
        if search_rows is not None:
            write_csv(root / "google_search_companies.csv", DISCOVERY_FIELDS, search_rows)
        build_lead_files(maps_path, root)
        return read_csv(root / "leads_master.csv"), read_csv(root / "leads_ready.csv")

    def test_maps_and_search_same_domain_merge_with_maps_values(self):
        master, ready = self.build(
            [maps_row("Lexa Technologies", "https://www.lexa.tn/", "info@lexa.tn")],
            [search_row("Lexa ERP", "https://lexa.tn/solutions", "ERP Tunisia")],
        )
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["name"], "Lexa Technologies")
        self.assertEqual(master[0]["email"], "info@lexa.tn")
        self.assertEqual(master[0]["phone"], "+216 70 000 000")
        self.assertEqual(len(ready), 1)

    def test_search_only_is_preserved_but_not_ready(self):
        master, ready = self.build([], [search_row()])
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["name"], "Search Company")
        self.assertEqual(master[0]["source"], "google_search")
        self.assertEqual(ready, [])

    def test_maps_only_behavior_and_ready_contract(self):
        master, ready = self.build([maps_row()], None)
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["source"], "google_maps")
        self.assertEqual(tuple(master[0]), MASTER_FIELDS)
        self.assertEqual(tuple(ready[0]), READY_FIELDS)
        self.assertEqual(ready[0]["email"], "info@maps.test")

    def test_source_provenance_merge(self):
        master, _ = self.build(
            [maps_row("Acme Maps", "https://acme.test")],
            [search_row("Acme Search", "https://www.acme.test/about", "query one;query two")],
        )
        self.assertEqual(master[0]["source"], "google_maps;google_search")
        self.assertEqual(master[0]["source_queries"], "query one;query two")


if __name__ == "__main__":
    main()
