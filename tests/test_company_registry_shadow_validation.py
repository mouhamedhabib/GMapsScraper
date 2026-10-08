"""Unit coverage for the offline Phase 2B.1 evaluator."""

from pathlib import Path
from unittest import TestCase

from company_registry.shadow_validation import (
    HOLDOUT_PATH,
    classifications_agree,
    observation_from_row,
    pre_cutoff_sources,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ShadowValidationTests(TestCase):
    def test_agreement_treats_updated_as_known_but_not_unresolved(self):
        self.assertTrue(classifications_agree("KNOWN", "KNOWN"))
        self.assertTrue(classifications_agree("KNOWN", "UPDATED"))
        self.assertTrue(classifications_agree("NEW", "NEW"))
        self.assertFalse(classifications_agree("NEW", "UPDATED"))
        self.assertFalse(classifications_agree("NEW", "AMBIGUOUS"))

    def test_cutoff_sources_exclude_cumulative_and_holdout_data(self):
        paths = [source.relative_path for source in pre_cutoff_sources(PROJECT_ROOT)]
        self.assertIn("CSV_FILES/leads_master.csv", paths)
        self.assertIn("CSV_FILES/google_search_companies.csv", paths)
        self.assertNotIn("CSV_FILES/google_maps_data.csv", paths)
        self.assertNotIn(HOLDOUT_PATH, paths)
        self.assertTrue(all("2026-09-15" not in path for path in paths))

    def test_observation_preserves_source_provenance_and_raw_payload(self):
        raw = {
            "title": "Example Co", "map_link": "ChIJ-example",
            "webpage": "https://example.tn", "phone_number": "71 123 456",
            "address": "Tunis",
        }
        observation = observation_from_row(
            "CSV_FILES/example.csv", 42, raw, "GOOGLE_MAPS",
        )
        self.assertEqual(observation.source_record_key, "CSV_FILES/example.csv:42")
        self.assertEqual(observation.name, "Example Co")
        self.assertEqual(dict(observation.raw_payload), raw)
