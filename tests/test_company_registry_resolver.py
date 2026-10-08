"""Phase 2A deterministic resolver regression tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.models import Classification, IdentityObservation, ResolutionAction
from company_registry.normalization import canonical_json, payload_hash
from company_registry.resolver import IdentityResolver
from company_registry.service import RegistryService
from company_registry.storage import connect_registry


STAMP = "2026-10-08T12:00:00+00:00"


class IdentityResolverTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "registry.db"
        connection = connect_registry(self.path)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES ('run-1', 'SCRAPE', 'RUNNING', ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        self.resolver = IdentityResolver(self.path)

    def observation(self, key, **changes):
        values = {
            "source_system": "GOOGLE_MAPS",
            "source_record_key": key,
            "observed_at": STAMP,
            "name": "Acme Consulting",
            "place_id": "ChIJ-Acme-Tunis",
            "website_url": "https://acme.example.tn/contact",
            "phone": "+216 71 123 456",
            "address": "1 Avenue de Tunis, Tunis",
        }
        values.update(changes)
        return IdentityObservation(**values)

    def counts(self):
        connection = connect_registry(self.path)
        try:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("companies", "branches", "company_identities",
                                       "branch_identities", "discovery_observations"))
        finally:
            connection.close()

    def test_exact_place_id_and_repeated_observation_are_idempotent(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        repeated = self.resolver.resolve("run-1", self.observation("one"))
        known = self.resolver.resolve("run-1", self.observation("two"))
        self.assertEqual(first.classification, Classification.NEW)
        self.assertEqual(repeated, first)
        self.assertEqual(known.classification, Classification.KNOWN)
        self.assertEqual(known.action, ResolutionAction.MATCH_ONLY)
        self.assertEqual((known.company_id, known.branch_id),
                         (first.company_id, first.branch_id))
        self.assertEqual(self.counts(), (1, 1, 2, 4, 2))

    def test_missing_place_id_matches_by_name_and_phone(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation("two", place_id="", website_url="", address=""),
        )
        self.assertEqual(result.classification, Classification.KNOWN)
        self.assertEqual(result.branch_id, first.branch_id)
        self.assertIn("exact name+phone", result.matched_evidence)

    def test_missing_place_id_matches_by_name_and_address(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation("two", place_id="", website_url="", phone=""),
        )
        self.assertEqual(result.classification, Classification.KNOWN)
        self.assertEqual(result.branch_id, first.branch_id)

    def test_changed_place_id_requires_strong_branch_alias_evidence(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation("two", place_id="ChIJ-Acme-Reissued"),
        )
        self.assertEqual(result.classification, Classification.UPDATED)
        self.assertEqual(result.action, ResolutionAction.ADD_PLACE_ALIAS)
        self.assertEqual(result.branch_id, first.branch_id)
        connection = connect_registry(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute(
            """SELECT count(*) FROM branch_identities
               WHERE branch_id=? AND identity_type='GOOGLE_MAPS_PLACE_ID'""",
            (first.branch_id,),
        ).fetchone()[0], 2)

    def test_changed_place_id_with_incomplete_evidence_is_ambiguous(self):
        self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation(
                "two", place_id="ChIJ-New", address="Different", website_url="",
            ),
        )
        self.assertEqual(result.classification, Classification.AMBIGUOUS)
        self.assertTrue(result.requires_review)

    def test_new_branch_of_existing_company_is_updated(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        second = self.resolver.resolve(
            "run-1", self.observation(
                "two", place_id="ChIJ-Acme-Sousse", phone="+216 73 999 999",
                address="2 Avenue Habib Bourguiba, Sousse",
            ),
        )
        self.assertEqual(second.classification, Classification.UPDATED)
        self.assertEqual(second.action, ResolutionAction.CREATE_BRANCH)
        self.assertEqual(second.company_id, first.company_id)
        self.assertNotEqual(second.branch_id, first.branch_id)
        self.assertEqual(self.counts()[0:2], (1, 2))

    def test_shared_domain_alone_never_merges_or_proves_new(self):
        first = self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation(
                "two", name="Different Legal Company", place_id="ChIJ-Different",
                phone="+216 70 000 000", address="20 Other Street",
            ),
        )
        self.assertEqual(result.classification, Classification.AMBIGUOUS)
        self.assertIsNone(result.company_id)
        self.assertIn(first.company_id, result.candidate_company_ids)
        self.assertEqual(self.counts()[0:2], (1, 1))

    def test_conflicting_name_and_domain_block_exact_place_acceptance(self):
        self.resolver.resolve("run-1", self.observation("one"))
        result = self.resolver.resolve(
            "run-1", self.observation(
                "two", name="Impostor SA", website_url="https://impostor.tn",
            ),
        )
        self.assertEqual(result.classification, Classification.AMBIGUOUS)
        self.assertIn("company name conflicts", result.conflicting_evidence)

    def test_company_only_search_match_and_restart(self):
        created = self.resolver.resolve("run-1", self.observation(
            "search-1", source_system="GOOGLE_SEARCH", place_id="", phone="", address="",
        ))
        self.assertEqual(created.classification, Classification.NEW)
        self.assertIsNone(created.branch_id)
        restarted = IdentityResolver(self.path)
        known = restarted.resolve("run-1", self.observation(
            "search-2", source_system="GOOGLE_SEARCH", place_id="", phone="", address="",
        ))
        self.assertEqual(known.classification, Classification.KNOWN)
        self.assertEqual(known.company_id, created.company_id)

    def insert_unresolved(self, status, raw, reason="manual review"):
        connection = connect_registry(self.path)
        raw_json = canonical_json(raw)
        digest = payload_hash(raw)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, finished_at, created_at)
               VALUES ('legacy', 'LEGACY_IMPORT', 'SUCCESS', ?, ?, ?)""",
            (STAMP, STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO historical_source_records
               (source_record_id, run_id, source_path, source_kind,
                source_row_number, source_content_hash, resolution_status,
                reason, raw_record_json, created_at)
               VALUES ('history-1', 'legacy', 'old.csv', 'OTHER', 2, ?, ?, ?, ?, ?)""",
            (digest, status, reason, raw_json, STAMP),
        )
        connection.commit()
        connection.close()

    def assert_exact_unresolved_is_preserved(self, status, expected):
        raw = {"name": "Old Unresolved", "phone": "+216 71 444 444"}
        self.insert_unresolved(status, raw)
        observation = IdentityObservation(
            "IMPORT_REPLAY", status, STAMP, name="Old Unresolved",
            phone="+216 71 444 444", raw_payload=raw,
        )
        result = self.resolver.resolve("run-1", observation)
        self.assertEqual(result.classification, expected)
        self.assertNotEqual(result.classification, Classification.NEW)
        self.assertEqual(self.counts()[0:2], (0, 0))

    def test_exact_historical_ambiguity_is_preserved(self):
        self.assert_exact_unresolved_is_preserved(
            "AMBIGUOUS", Classification.AMBIGUOUS,
        )

    def test_exact_historical_quarantine_is_preserved(self):
        self.assert_exact_unresolved_is_preserved(
            "QUARANTINED", Classification.QUARANTINED,
        )

    def test_historical_strong_overlap_blocks_new_but_domain_alone_does_not(self):
        raw = {"name": "Old Unresolved", "phone": "+216 71 444 444",
               "website": "https://shared.tn"}
        self.insert_unresolved("AMBIGUOUS", raw)
        strong = self.resolver.resolve("run-1", IdentityObservation(
            "SEARCH", "strong", STAMP, name="Old Unresolved",
            phone="+216 71 444 444",
        ))
        weak = self.resolver.resolve("run-1", IdentityObservation(
            "SEARCH", "weak", STAMP, name="Different Co",
            website_url="https://shared.tn",
        ))
        self.assertEqual(strong.classification, Classification.AMBIGUOUS)
        self.assertEqual(weak.classification, Classification.NEW)

    def test_insufficient_and_unmatched_nameless_records_are_quarantined(self):
        for key, changes in (
            ("no-evidence", {"place_id": "", "website_url": "", "phone": "", "address": ""}),
            ("nameless", {"name": "", "place_id": "ChIJ-Unknown"}),
        ):
            result = self.resolver.resolve("run-1", self.observation(key, **changes))
            self.assertEqual(result.classification, Classification.QUARANTINED)
            self.assertNotEqual(result.classification, Classification.NEW)

    def test_transaction_rolls_back_all_entity_changes(self):
        def fail(checkpoint):
            if checkpoint == "after_entity_changes":
                raise RuntimeError("injected failure")

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            RegistryService(self.path, failure_injector=fail).resolve(
                "run-1", self.observation("one"),
            )
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_concurrent_duplicate_submissions_create_one_entity(self):
        observation = self.observation("concurrent")
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(
                lambda _: IdentityResolver(self.path).resolve("run-1", observation),
                range(8),
            ))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.counts(), (1, 1, 2, 4, 1))

    def test_legacy_origin_and_null_first_seen_remain_immutable(self):
        connection = connect_registry(self.path)
        connection.execute(
            """INSERT INTO companies VALUES
               ('legacy-company', 'Legacy Co', 'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branches VALUES
               ('legacy-branch', 'legacy-company', 'Legacy Co', 'place_id:legacy',
                NULL, NULL, NULL, 'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO company_identities VALUES
               ('legacy-name', 'legacy-company', 'NAME', 'Legacy Co', 'legacy co',
                NULL, NULL, ?, ?)""", (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branch_identities VALUES
               ('legacy-place', 'legacy-branch', 'GOOGLE_MAPS_PLACE_ID',
                'place_id:legacy', 'place_id:legacy', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        result = self.resolver.resolve("run-1", self.observation(
            "legacy", name="Legacy Co", place_id="legacy", website_url="",
            phone="", address="",
        ))
        self.assertEqual(result.classification, Classification.UPDATED)
        connection = connect_registry(self.path)
        self.addCleanup(connection.close)
        company = connection.execute(
            "SELECT discovery_status, first_seen_at FROM companies WHERE company_id='legacy-company'"
        ).fetchone()
        branch = connection.execute(
            "SELECT discovery_status, first_seen_at FROM branches WHERE branch_id='legacy-branch'"
        ).fetchone()
        self.assertEqual(tuple(company), ("LEGACY_UNKNOWN", None))
        self.assertEqual(tuple(branch), ("LEGACY_UNKNOWN", None))
