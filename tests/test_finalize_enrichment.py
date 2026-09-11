"""Tests for the structural enrichment publication boundary."""

from csv import DictReader, DictWriter
from os import replace as os_replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from utils import build_outreach, enrich_leads, finalize_enrichment
from utils.enrichment_schema import CANONICAL_ENRICHMENT_FIELDS


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = DictReader(handle)
        return tuple(reader.fieldnames or ()), list(reader)


def row(index, status="SUCCESS"):
    return {
        "company_name": f"Company {index}",
        "email": f"info{index}@company{index}.test",
        "website": f"https://company{index}.test",
        "added_at": "2026-09-01T10:30:00+01:00",
        "description": "Supported software context",
        "industry": "Software",
        "services": "Development",
        "website_title": f"Company {index}",
        "website_meta_description": "Business software services",
        "hero_text": "Software for growing teams",
        "about_text": "We build business software.",
        "country": "Tunisia",
        "city": "Tunis",
        "location": "Tunis, Tunisia",
        "linkedin_url": f"https://linkedin.com/company/company-{index}",
        "phone": f"+216 70 000 00{index}",
        "source": "google_maps;google_search",
        "source_queries": "software Tunisia",
        "enrichment_status": status,
        "enrichment_attempts": str(index),
    }


class FinalizeEnrichmentTests(TestCase):
    def test_canonical_schema_is_shared_at_the_boundary(self):
        self.assertIs(enrich_leads.OUTPUT_FIELDS, CANONICAL_ENRICHMENT_FIELDS)
        self.assertIs(
            finalize_enrichment.OUTPUT_FIELDS, CANONICAL_ENRICHMENT_FIELDS
        )
        self.assertIs(
            build_outreach.ENRICHMENT_INPUT_FIELDS,
            CANONICAL_ENRICHMENT_FIELDS,
        )
        self.assertEqual(
            build_outreach.DEFAULT_INPUT, finalize_enrichment.DEFAULT_OUTPUT
        )

    def test_old_schema_normalizes_missing_columns_without_inventing_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old.csv"
            final = root / "final.csv"
            fields = (
                "company_name", "email", "website",
                "enrichment_status", "enrichment_attempts",
            )
            write_csv(source, fields, [{
                "company_name": "Old Company",
                "email": "old@example.test",
                "website": "https://old.example.test",
                "enrichment_status": "PARTIAL",
                "enrichment_attempts": "2",
            }])

            finalize_enrichment.finalize_enrichment(source, final)
            header, rows = read_csv(final)

        self.assertEqual(header, CANONICAL_ENRICHMENT_FIELDS)
        self.assertEqual(rows[0]["enrichment_status"], "PARTIAL")
        self.assertEqual(rows[0]["enrichment_attempts"], "2")
        for field in (
            "added_at", "country", "city", "location", "description",
            "industry", "services", "source", "source_queries",
        ):
            self.assertEqual(rows[0][field], "")

    def test_values_and_statuses_are_preserved_exactly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.csv"
            final = root / "final.csv"
            expected = [
                row(1, "SUCCESS"), row(2, "PARTIAL"), row(3, "FAILED")
            ]
            write_csv(source, CANONICAL_ENRICHMENT_FIELDS, expected)

            finalize_enrichment.finalize_enrichment(source, final)
            _, actual = read_csv(final)

        self.assertEqual(actual, expected)
        self.assertEqual(actual[0]["added_at"], expected[0]["added_at"])
        self.assertEqual(
            (actual[0]["country"], actual[0]["city"], actual[0]["location"]),
            ("Tunisia", "Tunis", "Tunis, Tunisia"),
        )
        self.assertEqual(actual[2]["enrichment_attempts"], "3")

    def test_publication_uses_same_directory_atomic_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.csv"
            final = root / "final.csv"
            write_csv(source, CANONICAL_ENRICHMENT_FIELDS, [row(1)])
            final.write_text("previous valid final\n", encoding="utf-8")
            observations = []

            def observe_replace(temporary, destination):
                temporary = Path(temporary)
                destination = Path(destination)
                observations.append({
                    "same_parent": temporary.parent == destination.parent,
                    "temporary_exists": temporary.exists(),
                    "old_still_valid": destination.read_text(encoding="utf-8")
                    == "previous valid final\n",
                })
                os_replace(temporary, destination)

            with patch.object(
                finalize_enrichment, "replace", side_effect=observe_replace
            ):
                finalize_enrichment.finalize_enrichment(source, final)

            _, rows = read_csv(final)

        self.assertEqual(observations, [{
            "same_parent": True,
            "temporary_exists": True,
            "old_still_valid": True,
        }])
        self.assertEqual(rows[0]["company_name"], "Company 1")

    def test_rerun_is_byte_for_byte_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.csv"
            final = root / "final.csv"
            write_csv(source, CANONICAL_ENRICHMENT_FIELDS, [row(1), row(2)])

            finalize_enrichment.finalize_enrichment(source, final)
            first = final.read_bytes()
            finalize_enrichment.finalize_enrichment(source, final)

            self.assertEqual(final.read_bytes(), first)

    def test_duplicate_identity_does_not_replace_valid_final(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.csv"
            final = root / "final.csv"
            duplicate = row(2)
            duplicate["website"] = row(1)["website"]
            write_csv(
                source, CANONICAL_ENRICHMENT_FIELDS, [row(1), duplicate]
            )
            final.write_bytes(b"previous valid final\n")

            with self.assertRaises(finalize_enrichment.FinalizationError):
                finalize_enrichment.finalize_enrichment(source, final)

            self.assertEqual(final.read_bytes(), b"previous valid final\n")
