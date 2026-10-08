"""Offline regression coverage for deterministic Maps audit decisions."""

from unittest import TestCase

from utils.maps_audit import (
    NOISE, POSSIBLE_TARGET, TARGET_COMPANY,
    classify_maps_company, validate_maps_geography,
)
from utils.geography import resolve_maps_geography


class MapsQualityAuditTests(TestCase):
    def test_maps_geography_normalizer_recognizes_malta(self):
        self.assertEqual(
            resolve_maps_geography("San Ġwann SGN 3000, Malta", "software company Malta"),
            {
                "country": "Malta", "city": "San Ġwann",
                "location": "San Ġwann SGN 3000, Malta",
            },
        )
        self.assertEqual(
            resolve_maps_geography("", "software company Malta")["country"],
            "Malta",
        )

    def test_target_possible_and_noise_classification(self):
        cases = (
            ({"title": "Neural AI", "category": "Software company"}, TARGET_COMPANY),
            ({"title": "DevXperts", "category": "Computer support and services"}, POSSIBLE_TARGET),
            ({"title": "Harbour Hotel", "category": "Hotel", "source_query": "IT company Malta"}, NOISE),
            ({"title": "Not Available", "category": "Not Available"}, NOISE),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(classify_maps_company(row), expected)

    def test_sfax_geography_match_mismatch_and_unknown(self):
        query = "software company Sfax Tunisia"
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "Route El Ain, Sfax, Tunisia",
        }), "MATCH")
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "Avenue Habib Bourguiba, Tunis, Tunisia",
        }), "MISMATCH")
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "Not Available",
        }), "UNKNOWN")

    def test_malta_geography_match_mismatch_and_unknown(self):
        query = "software company Malta"
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "San Gwann SGN 3000, Malta",
        }), "MATCH")
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "Sfax, Tunisia",
        }), "MISMATCH")
        self.assertEqual(validate_maps_geography({
            "source_query": query, "address": "Not Available",
        }), "UNKNOWN")
