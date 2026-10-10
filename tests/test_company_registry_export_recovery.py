"""Focused crash-safe Maps export finalization and recovery tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.maps_authoritative import (
    AuthoritativeMapsSession,
    maps_observation,
    recover_finalizing_run,
    verify_finalized_run,
)
from company_registry.run_lease import ActiveRunLeaseError, FinalizationRecoveryError
from company_registry.storage import initialize_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


QUERIES = ["software Tunis", "technology Sousse"]
STAMP = "2026-10-10T16:00:00+00:00"


class InjectedCrash(RuntimeError):
    pass


class MapsExportRecoveryTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "exports"
        initialize_registry(self.database)

    def session(self, run_id="run"):
        session = AuthoritativeMapsSession(
            self.database,
            self.exports,
            run_id=run_id,
            new_company_limit=2,
        )
        session.configure_queries(QUERIES)
        session.resolve(
            maps_observation(
                {
                    "title": "Recovery Company",
                    "map_link": "https://google.test/maps/data=!4m1!1sChIJ-Recover",
                    "webpage": "https://recovery.test",
                    "phone_number": "+216 71 111 222",
                    "address": "1 Recovery Street, Tunis",
                    "category": "Software company",
                    "description": "Technology services and employment",
                },
                query=QUERIES[0],
                observed_at=STAMP,
            ),
            RunAcceptanceBudget(None),
        )
        return session

    def status(self, run_id="run"):
        connection = open_registry(self.database)
        try:
            return connection.execute(
                "SELECT status FROM discovery_runs WHERE run_id=?", (run_id,),
            ).fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    def crash_at(target):
        def inject(point):
            if point == target:
                raise InjectedCrash(point)
        return inject

    def recover(self, run_id="run", **kwargs):
        return recover_finalizing_run(
            self.database,
            self.exports,
            run_id,
            QUERIES,
            new_company_limit=2,
            **kwargs,
        )

    def test_crashes_before_csv_and_after_manifest_never_publish_success(self):
        session = self.session()
        with self.assertRaises(InjectedCrash):
            session.complete(
                failure_injector=self.crash_at("discovery:before_csv_publish")
            )
        self.assertEqual(self.status(), "FINALIZING")

        with self.assertRaises(InjectedCrash):
            self.recover(
                failure_injector=self.crash_at(
                    "discovery:before_manifest_publish"
                )
            )
        self.assertEqual(self.status(), "FINALIZING")

        with self.assertRaises(InjectedCrash):
            self.recover(
                failure_injector=self.crash_at(
                    "after_exports_verified_before_success"
                )
            )
        self.assertEqual(self.status(), "FINALIZING")
        recovered = self.recover()
        self.assertEqual(recovered["status"], "SUCCESS")
        self.assertEqual(set(recovered["preserved"]), {
            "discovery", "employment", "mission",
        })

    def test_corrupt_csv_and_manifest_are_rebuilt_from_sqlite(self):
        session = self.session()
        with self.assertRaises(InjectedCrash):
            session.complete(
                failure_injector=self.crash_at(
                    "after_exports_verified_before_success"
                )
            )
        token = "run"
        (self.exports / f"new_companies_{token}.csv").write_text(
            "corrupt\n", encoding="utf-8"
        )
        (self.exports / f"qualified_employment_leads_{token}.manifest.json").write_text(
            "{bad json", encoding="utf-8"
        )

        recovered = self.recover()
        self.assertEqual(set(recovered["rebuilt"]), {"discovery", "employment"})
        self.assertEqual(recovered["preserved"], ["mission"])
        verified = verify_finalized_run(self.database, self.exports, "run")
        self.assertEqual(verified["discovery"]["rows"], 1)
        self.assertEqual(len(verified["discovery"]["company_ids"]), 1)

    def test_competing_recovery_has_one_writer_and_success_is_read_only(self):
        session = self.session()
        with self.assertRaises(InjectedCrash):
            session.complete(
                failure_injector=self.crash_at(
                    "after_exports_verified_before_success"
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.recover, lease_owner=f"writer-{index}")
                       for index in range(2)]
        self.assertEqual(sum(f.exception() is None for f in futures), 1)
        loser = next(f.exception() for f in futures if f.exception() is not None)
        self.assertIsInstance(loser, (ActiveRunLeaseError, FinalizationRecoveryError))
        with self.assertRaises(FinalizationRecoveryError):
            self.recover(lease_owner="late-writer")
        self.assertTrue(verify_finalized_run(self.database, self.exports, "run"))

    def test_stale_finalizer_lease_can_be_taken_over(self):
        session = self.session()
        with self.assertRaises(InjectedCrash):
            session.complete(
                failure_injector=self.crash_at(
                    "after_exports_verified_before_success"
                )
            )
        connection = open_registry(self.database)
        connection.execute(
            """UPDATE discovery_runs
                  SET lease_owner='stale', lease_expires_at=?, heartbeat_at=?
                WHERE run_id='run'""",
            (
                (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
                (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
            ),
        )
        connection.commit()
        connection.close()

        self.assertEqual(self.recover(lease_owner="takeover")["status"], "SUCCESS")
        connection = open_registry(self.database)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_active_finalizer_and_configuration_mismatch_fail_closed(self):
        session = self.session()
        with self.assertRaises(InjectedCrash):
            session.complete(
                failure_injector=self.crash_at(
                    "after_exports_verified_before_success"
                )
            )
        connection = open_registry(self.database)
        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        connection.execute(
            """UPDATE discovery_runs SET lease_owner='active', lease_expires_at=?
                 WHERE run_id='run'""",
            (future,),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(ActiveRunLeaseError):
            self.recover()

        connection = open_registry(self.database)
        connection.execute(
            "UPDATE discovery_runs SET lease_owner=NULL, lease_expires_at=NULL WHERE run_id='run'"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(FinalizationRecoveryError, "configuration hash"):
            recover_finalizing_run(
                self.database, self.exports, "run", ["different query"],
                new_company_limit=2,
            )
        self.assertEqual(self.status(), "FINALIZING")
