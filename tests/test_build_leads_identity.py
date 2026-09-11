"""Adversarial regression coverage for final lead company identity."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from utils.build_leads import (
    MASTER_FIELDS,
    build_groups,
    build_lead_files,
    finalize_group,
)


STAMP_1 = "2026-09-01T10:30:00+01:00"
STAMP_2 = "2026-09-03T14:00:00+01:00"


def maps_url(name, token):
    return f"https://www.google.com/maps/place/{name}/data=!4m2!3m1!1s{token}!8m2?hl=en"


def row(name, website="", phone="", email="", place="", address="", **extra):
    value = {
        "title": name,
        "webpage": website,
        "phone_number": phone,
        "site_email": email,
        "map_link": place,
        "address": address,
        "_source": extra.pop("_source", "google_maps"),
        "_source_queries": extra.pop("_source_queries", ""),
    }
    value.update(extra)
    return value


def finalized(rows):
    groups, _ = build_groups(rows)
    return groups, [finalize_group(group) for group in groups]


class CompanyIdentityTests(TestCase):
    def test_same_maps_place_merges_despite_name_formatting(self):
        groups, output = finalized([
            row("ACME, Inc.", place=maps_url("Acme", "ChIJSame")),
            row("Acme Incorporated", place=maps_url("ACME-Inc", "ChIJSame")),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(output[0]["map_place_id"], "place_id:chijsame")

    def test_same_domain_with_compatible_evidence_merges(self):
        groups, _ = finalized([
            row("Acme", "https://www.acme.test", "+216 71 000 000"),
            row("Acme", "https://acme.test/about"),
        ])
        self.assertEqual(len(groups), 1)

    def test_name_and_company_email_domain_merge_case_insensitively(self):
        groups, _ = finalized([
            row("Acme Labs", email="INFO@ACME.TEST"),
            row("acme labs", email="sales@acme.test"),
        ])
        self.assertEqual(len(groups), 1)

    def test_bridge_attack_never_unions_existing_groups(self):
        groups, output = finalized([
            row("Alpha", "https://alpha.test", "+1 111 111 1111"),
            row("Beta", "https://beta.test", "+1 222 222 2222"),
            row("Beta", "https://alpha.test", "+1 222 222 2222"),
        ])
        self.assertEqual(len(groups), 3)
        self.assertTrue(all("identity bridge conflict" in item["review_reasons"] for item in output))

    def test_same_exact_name_with_conflicting_domains_stays_separate(self):
        _, output = finalized([
            row("Acme", "https://acme-one.test"),
            row("acme", "https://acme-two.test"),
        ])
        self.assertEqual(len(output), 2)
        self.assertTrue(all("conflicting strong identities" in item["review_reasons"] for item in output))

    def test_shared_domain_distinct_branches_stay_separate(self):
        _, output = finalized([
            row("Acme Downtown", "https://acme.test", "+1 111 111 1111",
                place=maps_url("Downtown", "ChIJBranchOne"), address="1 Main St"),
            row("Acme Airport", "https://acme.test", "+1 222 222 2222",
                place=maps_url("Airport", "ChIJBranchTwo"), address="2 Airport Rd"),
        ])
        self.assertEqual(len(output), 2)
        self.assertTrue(all("shared domain across distinct places" in item["review_reasons"] for item in output))

    def test_same_maps_place_changed_website_merges_and_reviews(self):
        _, output = finalized([
            row("Acme", "https://old-acme.test", place=maps_url("Acme", "ChIJSame")),
            row("Acme Ltd", "https://new-acme.test", place=maps_url("Acme-New", "ChIJSame")),
        ])
        self.assertEqual(len(output), 1)
        self.assertIn("multiple conflicting websites", output[0]["review_reasons"])

    def test_maps_and_search_same_domain_merge_without_place_conflict(self):
        groups, output = finalized([
            row("Acme Maps", "https://acme.test", place=maps_url("Acme", "ChIJAcme")),
            row("Acme Search", "https://www.acme.test/about", _source="google_search"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(output[0]["source"], "google_maps;google_search")

    def test_name_phone_fallback_merges_without_domain_or_place(self):
        groups, _ = finalized([
            row("Acme Labs", phone="+216 71 234 567"),
            row("acme labs", phone="+216 (71) 234-567"),
        ])
        self.assertEqual(len(groups), 1)

    def test_name_only_different_addresses_stays_separate(self):
        groups, output = finalized([
            row("Central Services", address="1 Main St, Tunis"),
            row("central services", address="9 High St, London"),
        ])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all("conflicting location evidence" in item["review_reasons"] for item in output))

    def test_name_only_different_explicit_countries_stays_separate(self):
        groups, _ = finalized([
            row("Central Services", country="Tunisia"),
            row("central services", country="France"),
        ])
        self.assertEqual(len(groups), 2)

    def test_earliest_added_at_survives_identity_merge(self):
        _, output = finalized([
            row("Acme", "https://acme.test", added_at=STAMP_2),
            row("Acme Ltd", "https://acme.test/about", added_at=STAMP_1),
        ])
        self.assertEqual(output[0]["added_at"], STAMP_1)

    def test_geography_conflict_behavior_is_unchanged(self):
        _, output = finalized([
            row("Acme", "https://acme.test", country="Tunisia", city="Tunis"),
            row("Acme", "https://acme.test", country="France", city="Paris"),
        ])
        self.assertEqual(len(output), 1)
        self.assertIn("conflicting countries", output[0]["review_reasons"])

    def test_email_ranking_is_unchanged(self):
        _, output = finalized([
            row("Acme", "https://acme.test", email="sales@other.test"),
            row("Acme", "https://acme.test", email="jobs@acme.test"),
        ])
        self.assertEqual(output[0]["email"], "jobs@acme.test")
        self.assertEqual(output[0]["alternate_emails"], "sales@other.test")

    def test_grouping_is_deterministic(self):
        rows = [
            row("Acme", "https://acme.test", place=maps_url("Acme", "ChIJAcme")),
            row("Acme Ltd", "https://acme.test/about"),
        ]
        self.assertEqual(finalized(rows)[1], finalized(rows)[1])

    def test_old_maps_and_master_schemas_do_not_crash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "google_maps_data.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = DictWriter(handle, fieldnames=("title", "webpage"))
                writer.writeheader()
                writer.writerow({"title": "Legacy", "webpage": "https://legacy.test"})
            with (root / "leads_master.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = DictWriter(handle, fieldnames=("name", "website", "added_at"))
                writer.writeheader()
                writer.writerow({"name": "Legacy", "website": "https://legacy.test", "added_at": STAMP_1})
            build_lead_files(root / "google_maps_data.csv", root)
            with (root / "leads_master.csv").open("r", newline="", encoding="utf-8-sig") as handle:
                output = list(DictReader(handle))
            self.assertEqual(tuple(output[0]), MASTER_FIELDS)
            self.assertEqual(output[0]["added_at"], STAMP_1)
