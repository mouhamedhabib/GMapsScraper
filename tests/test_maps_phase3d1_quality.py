"""Phase 3D.1 offline Maps identity, relevance, and eligibility matrix."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.models import Classification, ResolutionAction
from company_registry.storage import initialize_registry, open_registry
from utils.maps_audit import NOISE, TARGET_COMPANY, classify_maps_company
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-10-09T12:00:00+00:00"


def maps_row(index: int, **changes) -> dict:
    row = {
        "title": f"Software Company {index}",
        "category": "Software company",
        "map_link": (
            "https://www.google.com/maps/place/company/"
            f"data=!4m1!1sChIJ-Phase3D1-{index}"
        ),
        "webpage": f"https://software-{index}.example/contact",
        "phone_number": f"+216 71 {index:06d}",
        "address": f"{index} Technology Road, Tunis, Tunisia",
    }
    row.update(changes)
    return row


def conservative_outreach_eligible(row: dict, outcome) -> bool:
    """Model the recommendation only; this does not alter outreach code."""
    resolution = outcome.decision.resolution
    return (
        outcome.outcome == "new"
        and resolution.classification is Classification.NEW
        and resolution.action is ResolutionAction.CREATE_COMPANY
        and not resolution.requires_review
        and classify_maps_company(row) == TARGET_COMPANY
        and bool(str(row.get("webpage") or "").strip())
    )


class MapsPhase3D1QualityTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry-v4.db"
        self.exports = self.root / "exports"
        initialize_registry(self.database)
        self.session = AuthoritativeMapsSession(
            self.database, self.exports, run_id="phase3d1-offline",
        )
        self.budget = RunAcceptanceBudget(20)

    def resolve(self, row: dict, query: str = "software development company Tunis"):
        observation = maps_observation(row, query=query, observed_at=STAMP)
        return self.session.resolve(observation, self.budget)

    def test_01_software_company_with_website(self):
        row = maps_row(1, category="Software development company")
        outcome = self.resolve(row)
        self.assertEqual(outcome.outcome, "new")
        self.assertEqual(classify_maps_company(row), TARGET_COMPANY)
        self.assertTrue(conservative_outreach_eligible(row, outcome))

    def test_02_software_company_without_website(self):
        row = maps_row(2, webpage="")
        outcome = self.resolve(row)
        self.assertEqual(outcome.outcome, "new")
        self.assertEqual(classify_maps_company(row), TARGET_COMPANY)
        self.assertFalse(conservative_outreach_eligible(row, outcome))

    def test_03_unrelated_travel_agency(self):
        row = maps_row(
            3, title="Tozeur Experience", category="Sightseeing tour agency",
            webpage="", phone_number="",
        )
        outcome = self.resolve(row)
        self.assertEqual(outcome.outcome, "new")
        self.assertEqual(classify_maps_company(row), NOISE)
        self.assertFalse(conservative_outreach_eligible(row, outcome))

    def test_04_unrelated_marketing_agency(self):
        row = maps_row(4, title="Campaign Studio", category="Marketing agency")
        outcome = self.resolve(row)
        self.assertEqual(outcome.outcome, "new")
        self.assertEqual(classify_maps_company(row), NOISE)
        self.assertFalse(conservative_outreach_eligible(row, outcome))

    def test_05_existing_company_with_new_branch(self):
        original = maps_row(5, title="Branchable Software")
        created = self.resolve(original)
        branch_row = maps_row(
            6, title="Branchable Software",
            webpage="https://software-5.example/sfax",
            address="6 Innovation Avenue, Sfax, Tunisia",
        )
        branch = self.resolve(branch_row, "software development company Sfax")
        self.assertEqual(created.outcome, "new")
        self.assertEqual(branch.outcome, "branch")
        self.assertEqual(branch.decision.resolution.classification, Classification.UPDATED)
        self.assertEqual(branch.decision.resolution.action, ResolutionAction.CREATE_BRANCH)
        self.assertEqual(
            created.decision.resolution.company_id,
            branch.decision.resolution.company_id,
        )
        self.assertFalse(conservative_outreach_eligible(branch_row, branch))

    def test_06_same_company_from_multiple_queries(self):
        row = maps_row(7)
        first = self.resolve(row, "software development company Tunis")
        second = self.resolve(row, "custom software developers Tunisia")
        self.assertEqual(first.outcome, "new")
        self.assertIn(second.outcome, {"known", "same_run"})
        self.assertEqual(
            first.decision.resolution.company_id,
            second.decision.resolution.company_id,
        )
        self.assertEqual(self.budget.snapshot()["committed"], 1)

    def test_07_shared_domain_is_ambiguous(self):
        first = self.resolve(maps_row(8, title="Tenant One", webpage="https://hub.example"))
        second_row = maps_row(
            9, title="Unrelated Tenant", webpage="https://hub.example/about",
        )
        second = self.resolve(second_row)
        self.assertEqual(first.outcome, "new")
        self.assertEqual(second.outcome, "ambiguous")
        self.assertTrue(second.decision.resolution.requires_review)
        self.assertFalse(conservative_outreach_eligible(second_row, second))

    def test_08_missing_phone_and_website(self):
        row = maps_row(10, webpage="", phone_number="")
        outcome = self.resolve(row)
        self.assertEqual(outcome.outcome, "new")
        self.assertEqual(classify_maps_company(row), TARGET_COMPANY)
        self.assertFalse(conservative_outreach_eligible(row, outcome))

    def test_09_conflicting_identity_evidence(self):
        original = maps_row(11, title="Identity One", webpage="https://one.example")
        self.resolve(original)
        conflict_row = maps_row(
            11, title="Different Legal Entity", webpage="https://different.example",
        )
        conflict = self.resolve(conflict_row)
        self.assertEqual(conflict.outcome, "ambiguous")
        self.assertTrue(conflict.decision.resolution.requires_review)
        self.assertFalse(conservative_outreach_eligible(conflict_row, conflict))

    def test_10_historical_company_is_not_recreated(self):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO companies VALUES
               ('legacy-company', 'Historical Software', 'LEGACY_UNKNOWN',
                NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branches VALUES
               ('legacy-branch', 'legacy-company', 'Historical Software',
                'place_id:chij-historical', NULL, NULL, NULL,
                'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branch_identities VALUES
               ('legacy-place', 'legacy-branch', 'GOOGLE_MAPS_PLACE_ID',
                'place_id:chij-historical', 'place_id:chij-historical',
                NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()

        row = maps_row(
            12, title="Historical Software",
            map_link=(
                "https://www.google.com/maps/place/company/"
                "data=!4m1!1sChIJ-Historical"
            ),
            webpage="", phone_number="", address="",
        )
        outcome = self.resolve(row)
        self.assertNotEqual(outcome.decision.resolution.classification, Classification.NEW)
        self.assertFalse(conservative_outreach_eligible(row, outcome))
        connection = open_registry(self.database)
        try:
            company = connection.execute(
                "SELECT discovery_status, first_seen_at FROM companies "
                "WHERE company_id='legacy-company'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(tuple(company), ("LEGACY_UNKNOWN", None))
