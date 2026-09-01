"""Offline tests for deterministic geography extraction and propagation."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.build_leads import MASTER_FIELDS, READY_FIELDS, build_lead_files
from utils.build_outreach import OUTPUT_FIELDS as OUTREACH_FIELDS, build_outreach
from utils.build_outreach_queue import build_queue, read_csv as read_queue_csv
from utils.enrich_leads import prepare_records as prepare_enrichment_records
from utils.enrich_search_emails import prepare_records as prepare_email_records
from utils.geography import extract_address_geography, extract_query_geography
from utils.google_search_discovery import DISCOVERY_FIELDS, merge_discoveries


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


class QueryGeographyTests(TestCase):
    def test_supported_query_forms(self):
        cases = (
            ("software development company Tunis Tunisia", "Tunis", "Tunisia"),
            ("agence développement web Paris France", "Paris", "France"),
            ("agence développement web Bruxelles Belgique", "Bruxelles", "Belgium"),
            ("software development company London UK", "London", "United Kingdom"),
            ("agence développement web Montréal Canada", "Montréal", "Canada"),
        )
        for query, city, country in cases:
            with self.subTest(query=query):
                geography = extract_query_geography(query)
                self.assertEqual(geography["city"], city)
                self.assertEqual(geography["country"], country)
                self.assertEqual(geography["location"], f"{city}, {country}")

    def test_country_only_query_does_not_guess_city(self):
        self.assertEqual(
            extract_query_geography("software development company Tunisia"),
            {"country": "Tunisia", "city": "", "location": "Tunisia"},
        )

    def test_search_discovery_writes_normalized_geography(self):
        rows = merge_discoveries([], [{
            "company_name": "Acme", "website": "https://acme.test/about",
            "source": "google_search",
            "source_query": "agence développement web Bruxelles Belgique",
            "source_url": "https://acme.test/about",
        }])
        self.assertEqual(rows[0]["country"], "Belgium")
        self.assertEqual(rows[0]["city"], "Bruxelles")
        self.assertEqual(rows[0]["location"], "Bruxelles, Belgium")


class MapsAddressGeographyTests(TestCase):
    def test_tunisian_addresses_without_country(self):
        cases = (
            ("20 rue mahmoud GHAZNAOUI, Tunis 1004", "Tunis"),
            ("ENNASR 2, immeuble L'ALKAZAR, Appartement d5-2, Ariana 2037", "Ariana"),
            ("Rue du Lac Biwa, Tunis 1053", "Tunis"),
            ("Golden Tower, Centre Urbain Nord, Bloc B, 7ème Etage, Tunis 1082", "Tunis"),
        )
        for address, city in cases:
            with self.subTest(address=address):
                geography = extract_address_geography(address)
                self.assertEqual(geography["city"], city)
                self.assertEqual(geography["country"], "Tunisia")
                self.assertEqual(geography["location"], address)

    def test_explicit_international_country_addresses(self):
        cases = (
            ("Paris, France", "Paris", "France"),
            ("Bruxelles, Belgique", "Bruxelles", "Belgium"),
            ("London, UK", "London", "United Kingdom"),
            ("Montréal, QC, Canada", "Montréal", "Canada"),
        )
        for address, city, country in cases:
            with self.subTest(address=address):
                geography = extract_address_geography(address)
                self.assertEqual(geography["city"], city)
                self.assertEqual(geography["country"], country)
                self.assertEqual(geography["location"], address)

    def test_ambiguous_address_does_not_invent_country(self):
        address = "20 Example Street, Springfield 1234"
        self.assertEqual(
            extract_address_geography(address),
            {"country": "", "city": "", "location": address},
        )


class LeadGeographyTests(TestCase):
    def build(self, maps_rows, search_rows=()):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_csv(
            root / "google_maps_data.csv",
            (
                "title", "webpage", "phone_number", "site_email", "address",
                "source_query", "country", "city", "location",
            ),
            maps_rows,
        )
        write_csv(root / "google_search_companies.csv", DISCOVERY_FIELDS, search_rows)
        build_lead_files(root / "google_maps_data.csv", root)
        return root, read_rows(root / "leads_master.csv")

    def test_old_csv_without_geography_does_not_crash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "google_maps_data.csv",
                ("title", "webpage", "phone_number", "site_email"),
                [{
                    "title": "Legacy Co", "webpage": "https://legacy.test",
                    "phone_number": "", "site_email": "info@legacy.test",
                }],
            )
            build_lead_files(root / "google_maps_data.csv", root)
            master = read_rows(root / "leads_master.csv")
            ready = read_rows(root / "leads_ready.csv")
            self.assertEqual(tuple(master[0]), MASTER_FIELDS)
            self.assertEqual(tuple(ready[0]), READY_FIELDS)
            self.assertEqual(master[0]["country"], "")
            self.assertEqual(master[0]["city"], "")
            self.assertEqual(master[0]["location"], "")

    def test_deduplication_prefers_maps_address_and_fills_compatible_values(self):
        _, master = self.build(
            [{
                "title": "Acme", "webpage": "https://acme.test",
                "phone_number": "", "site_email": "info@acme.test",
                "address": "10 Avenue Habib Bourguiba, Paris, France",
            }],
            [{
                "company_name": "Acme Search", "website": "https://acme.test/about",
                "source": "google_search", "source_query": "software company France",
                "source_url": "https://acme.test/about", "country": "France",
                "city": "", "location": "France",
            }],
        )
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["city"], "Paris")
        self.assertEqual(master[0]["country"], "France")
        self.assertEqual(
            master[0]["location"],
            "10 Avenue Habib Bourguiba, Paris, France",
        )

    def test_existing_maps_location_backfills_blank_city_and_country(self):
        address = "20 rue mahmoud GHAZNAOUI, Tunis 1004"
        _, master = self.build([{
            "title": "Existing Maps Co", "webpage": "https://existing.test",
            "phone_number": "", "site_email": "info@existing.test",
            "address": "", "location": address, "country": "", "city": "",
        }])
        self.assertEqual(master[0]["country"], "Tunisia")
        self.assertEqual(master[0]["city"], "Tunis")
        self.assertEqual(master[0]["location"], address)

    def test_ambiguous_maps_address_keeps_location_and_uses_query_fallback(self):
        address = "20 Example Street"
        _, master = self.build([{
            "title": "Fallback Co", "webpage": "https://fallback.test",
            "phone_number": "", "site_email": "info@fallback.test",
            "address": address,
            "source_query": "software development company Tunis Tunisia",
        }])
        self.assertEqual(master[0]["country"], "Tunisia")
        self.assertEqual(master[0]["city"], "Tunis")
        self.assertEqual(master[0]["location"], address)

    def test_country_conflict_routes_deduplicated_company_to_review(self):
        root, master = self.build(
            [{
                "title": "Acme", "webpage": "https://acme.test",
                "phone_number": "", "site_email": "info@acme.test",
                "address": "10 Main Street, Paris, France",
            }],
            [{
                "company_name": "Acme", "website": "https://acme.test/about",
                "source": "google_search",
                "source_query": "agence web Bruxelles Belgique",
                "source_url": "https://acme.test/about", "country": "Belgium",
                "city": "Bruxelles", "location": "Bruxelles, Belgium",
            }],
        )
        review = read_rows(root / "leads_review.csv")
        self.assertEqual(len(master), 1)
        self.assertEqual(master[0]["review_status"], "REVIEW")
        self.assertIn("conflicting countries", master[0]["review_reasons"])
        self.assertEqual(len(review), 1)


class DownstreamGeographyTests(TestCase):
    def test_enrichment_stages_preserve_geography(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            master = {field: "" for field in MASTER_FIELDS}
            master.update({
                "name": "Acme", "email": "info@acme.test",
                "website": "https://acme.test", "source": "google_search",
                "country": "Tunisia", "city": "Tunis",
                "location": "Tunis, Tunisia",
            })
            write_csv(root / "leads_master.csv", MASTER_FIELDS, [master])
            email_records = prepare_email_records(
                root / "leads_master.csv", root / "search_email_enriched.csv",
            )
            self.assertEqual(email_records[0]["location"], "Tunis, Tunisia")

            ready = {field: master.get(field, "") for field in READY_FIELDS}
            write_csv(root / "leads_ready.csv", READY_FIELDS, [ready])
            enrichment_records, _ = prepare_enrichment_records(
                root / "leads_ready.csv", root / "leads_enriched.csv",
            )
            self.assertEqual(enrichment_records[0]["country"], "Tunisia")
            self.assertEqual(enrichment_records[0]["city"], "Tunis")
            self.assertEqual(enrichment_records[0]["location"], "Tunis, Tunisia")

    def test_outreach_and_queue_preserve_geography(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            enriched_fields = (*OUTREACH_FIELDS, "enrichment_attempts")
            enriched = {field: "" for field in enriched_fields}
            enriched.update({
                "company_name": "Acme", "email": "info@acme.test",
                "website": "https://acme.test", "description": "Software company",
                "industry": "Software", "services": "Development",
                "website_title": "Acme", "enrichment_status": "SUCCESS",
                "country": "Tunisia", "city": "Tunis",
                "location": "Tunis, Tunisia",
            })
            write_csv(root / "leads_enriched_final.csv", enriched_fields, [enriched])
            build_outreach(
                root / "leads_enriched_final.csv",
                root / "outreach_ready.csv",
                root / "outreach_review.csv",
            )
            outreach = read_rows(root / "outreach_ready.csv")
            self.assertEqual(outreach[0]["country"], "Tunisia")
            self.assertEqual(outreach[0]["city"], "Tunis")
            self.assertEqual(outreach[0]["location"], "Tunis, Tunisia")

            build_queue(
                root / "outreach_ready.csv",
                root / "outreach_history.csv",
                root / "outreach_queue.csv",
            )
            fields, queue = read_queue_csv(root / "outreach_queue.csv")
            self.assertEqual(fields, list(OUTREACH_FIELDS))
            self.assertEqual(queue[0]["country"], "Tunisia")
            self.assertEqual(queue[0]["city"], "Tunis")
            self.assertEqual(queue[0]["location"], "Tunis, Tunisia")

if __name__ == "__main__":
    main()
