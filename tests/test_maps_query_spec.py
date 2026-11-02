"""Offline tests for Batch 03 Maps query specifications."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from utils.geography import (
    MapsQuerySpec,
    QuerySpecificationError,
    load_maps_query_specs,
    parse_maps_query_spec,
)
from utils.threading_controller import FastSearchAlgo


class MapsQuerySpecTests(TestCase):
    def test_explicit_spec_accepts_arbitrary_country_and_preserves_search(self):
        search = "AI research labs  in  São Paulo"
        spec = parse_maps_query_spec(f"{search}\tSão Paulo\tBrasil")

        self.assertEqual(spec.search_text, search)
        self.assertEqual(spec.target_city, "São Paulo")
        self.assertEqual(spec.target_country, "Brasil")
        self.assertEqual(spec.target_location, "São Paulo, Brasil")
        self.assertEqual(spec.geography_source, "EXPLICIT")
        self.assertEqual(spec.canonical_text, f"{search}\tSão Paulo\tBrasil")

    def test_explicit_spec_accepts_multiword_country_and_country_wide_query(self):
        city = parse_maps_query_spec(
            "software Sarajevo\tSarajevo\tBosnia and Herzegovina"
        )
        country_wide = parse_maps_query_spec(
            "renewable energy New Zealand\t\tNew Zealand"
        )

        self.assertEqual(city.target_country, "Bosnia and Herzegovina")
        self.assertEqual(country_wide.target_city, "")
        self.assertEqual(country_wide.target_location, "New Zealand")

    def test_explicit_metadata_is_unicode_normalized_without_changing_search(self):
        decomposed_city = "Sa\u0303o Paulo"
        search = f"companies {decomposed_city}"
        spec = parse_maps_query_spec(f"{search}\t{decomposed_city}\tBrasil")

        self.assertEqual(spec.search_text, search)
        self.assertEqual(spec.target_city, "São Paulo")

    def test_legacy_queries_remain_supported_and_conservative(self):
        inferred = parse_maps_query_spec("  software company Tunis Tunisia  ")
        unknown = parse_maps_query_spec("software company Tokyo Japan")

        self.assertEqual(inferred.search_text, "software company Tunis Tunisia")
        self.assertEqual(inferred.target_city, "Tunis")
        self.assertEqual(inferred.target_country, "Tunisia")
        self.assertEqual(inferred.geography_source, "LEGACY_INFERRED")
        self.assertEqual(unknown.target_city, "")
        self.assertEqual(unknown.target_country, "")
        self.assertEqual(unknown.geography_source, "UNDECLARED")

    def test_malformed_explicit_specs_are_rejected(self):
        invalid = (
            "search\tcountry",
            "search\tcity\tcountry\textra",
            "\tcity\tcountry",
            "search\tcity\t",
            " search\tcity\tcountry",
            "search\tcity\tcountry ",
            "search\x00text",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(QuerySpecificationError):
                parse_maps_query_spec(value)

    def test_loader_rejects_unicode_and_whitespace_equivalent_duplicates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "queries.txt"
            path.write_text(
                "Companies São Paulo\tSão Paulo\tBrasil\n"
                "companies  Sa\u0303o Paulo\tSa\u0303o Paulo\tBRASIL\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(QuerySpecificationError, "Line 2.*line 1"):
                load_maps_query_specs(path)

    def test_loader_ignores_blanks_and_retains_legacy_canonical_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "queries.txt"
            path.write_text(
                "\n legacy query \nsoftware Porto\tPorto\tPortugal\n",
                encoding="utf-8",
            )

            specs = load_maps_query_specs(path)

        self.assertEqual(
            [spec.canonical_text for spec in specs],
            ["legacy query", "software Porto\tPorto\tPortugal"],
        )
        self.assertTrue(all(isinstance(spec, MapsQuerySpec) for spec in specs))


class QueryDispatchTests(TestCase):
    def test_only_exact_search_text_is_passed_to_maps_worker(self):
        received = []

        class Worker:
            def __init__(self, **kwargs):
                pass

            def start_scrapper(self, query):
                received.append(query)
                return "COMPLETED"

            def quit_driver(self):
                pass

            def resource_metrics(self):
                return {}

            def query_states(self):
                return []

        coordinator = FastSearchAlgo(workers=1, result_range=1, verbose=False)
        with patch("utils.threading_controller.GoogleMaps", Worker):
            coordinator.fast_search_algorithm([
                "software firms  in  Tokyo\tTokyo\tJapan"
            ])

        self.assertEqual(received, ["software firms  in  Tokyo"])
        self.assertEqual(
            coordinator._query_list[0].canonical_text,
            "software firms  in  Tokyo\tTokyo\tJapan",
        )
