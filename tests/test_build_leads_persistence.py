"""Crash-safety coverage for publishing the three build_leads outputs."""

from contextlib import redirect_stdout
from csv import DictReader, DictWriter
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from utils import build_leads
from utils.build_leads import (
    MASTER_FIELDS,
    READY_FIELDS,
    SNAPSHOT_MANIFEST,
    SNAPSHOT_PENDING,
    build_lead_files,
    main,
    publish_lead_snapshot,
    recover_interrupted_snapshot,
)


OUTPUT_NAMES = ("leads_master.csv", "leads_ready.csv", "leads_review.csv")


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_header(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return tuple(DictReader(handle).fieldnames)


def master_row(name="New"):
    row = {field: "" for field in MASTER_FIELDS}
    row.update({
        "name": name,
        "email": f"info@{name.casefold()}.test",
        "website": f"https://{name.casefold()}.test",
        "email_status": "MATCH",
        "review_status": "READY",
    })
    return row


def snapshot_outputs():
    master = master_row()
    ready = {field: master[field] for field in READY_FIELDS}
    review = master_row("Review")
    review["review_status"] = "REVIEW"
    return (
        ("leads_master.csv", MASTER_FIELDS, [master, review]),
        ("leads_ready.csv", READY_FIELDS, [ready]),
        ("leads_review.csv", MASTER_FIELDS, [review]),
    )


class SnapshotPublicationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def seed_old_outputs(self):
        for name in OUTPUT_NAMES:
            (self.root / name).write_bytes(f"old:{name}\n".encode())
        return {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}

    def assert_no_transaction_debris(self):
        debris = [
            path.name for path in self.root.iterdir()
            if ".tmp-" in path.name
            or ".restore-" in path.name
            or path.name.endswith(".snapshot-backup")
            or path.name == SNAPSHOT_PENDING
        ]
        self.assertEqual(debris, [])

    def test_successful_publication_replaces_all_outputs_and_writes_manifest(self):
        old = self.seed_old_outputs()
        publish_lead_snapshot(self.root, snapshot_outputs())

        for name in OUTPUT_NAMES:
            self.assertNotEqual((self.root / name).read_bytes(), old[name])
        manifest = json.loads((self.root / SNAPSHOT_MANIFEST).read_text())
        self.assertEqual(set(manifest["files"]), set(OUTPUT_NAMES))
        self.assertEqual(
            {name: manifest["files"][name]["rows"] for name in OUTPUT_NAMES},
            {"leads_master.csv": 2, "leads_ready.csv": 1, "leads_review.csv": 1},
        )
        self.assert_no_transaction_debris()

    def test_each_staging_failure_leaves_old_snapshot_untouched_and_cleans_temps(self):
        real_write_csv = build_leads.write_csv
        for failed_name in OUTPUT_NAMES:
            with self.subTest(failed_name=failed_name):
                old = self.seed_old_outputs()

                def fail_selected_stage(path, fieldnames, rows):
                    if f".{failed_name}.tmp-" in path.name:
                        raise OSError("simulated staging failure")
                    return real_write_csv(path, fieldnames, rows)

                with patch("utils.build_leads.write_csv", side_effect=fail_selected_stage):
                    with self.assertRaises(OSError):
                        publish_lead_snapshot(self.root, snapshot_outputs())

                self.assertEqual(
                    {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}, old,
                )
                self.assert_no_transaction_debris()

    def test_replace_failure_rolls_back_and_does_not_publish_new_manifest(self):
        old = self.seed_old_outputs()
        previous_manifest = {"schema_version": 1, "generation_id": "previous"}
        (self.root / SNAPSHOT_MANIFEST).write_text(json.dumps(previous_manifest))
        real_replace = os.replace
        failed = False

        def fail_ready_once(source, destination):
            nonlocal failed
            if Path(destination).name == "leads_ready.csv" and not failed:
                failed = True
                self.assertTrue((self.root / SNAPSHOT_PENDING).exists())
                self.assertEqual(
                    json.loads((self.root / SNAPSHOT_MANIFEST).read_text()),
                    previous_manifest,
                )
                raise OSError("simulated replace failure")
            return real_replace(source, destination)

        with patch("utils.build_leads.os.replace", side_effect=fail_ready_once):
            with self.assertRaises(OSError):
                publish_lead_snapshot(self.root, snapshot_outputs())

        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}, old,
        )
        self.assertEqual(
            json.loads((self.root / SNAPSHOT_MANIFEST).read_text()), previous_manifest,
        )
        self.assert_no_transaction_debris()

    def test_next_build_recovery_restores_an_interrupted_previous_generation(self):
        old = self.seed_old_outputs()
        transaction = {"schema_version": 1, "generation_id": "interrupted", "files": {}}
        for name in OUTPUT_NAMES:
            backup = self.root / f".{name}.snapshot-backup"
            backup.write_bytes(old[name])
            transaction["files"][name] = {"backup": backup.name, "existed": True}
            (self.root / name).write_bytes(f"mixed:{name}\n".encode())
        (self.root / SNAPSHOT_PENDING).write_text(json.dumps(transaction))

        self.assertTrue(recover_interrupted_snapshot(self.root))

        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}, old,
        )
        self.assert_no_transaction_debris()


class BuildPersistenceIntegrationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input_path = self.root / "google_maps_data.csv"
        write_csv(self.input_path, ("title", "webpage", "site_email"), [{
            "title": "Acme",
            "webpage": "https://acme.test",
            "site_email": "info@acme.test",
        }])

    def test_successful_build_preserves_schemas_and_is_csv_deterministic(self):
        with redirect_stdout(StringIO()):
            build_lead_files(self.input_path, self.root)
        first = {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}

        self.assertEqual(read_header(self.root / "leads_master.csv"), MASTER_FIELDS)
        self.assertEqual(read_header(self.root / "leads_ready.csv"), READY_FIELDS)
        self.assertEqual(read_header(self.root / "leads_review.csv"), MASTER_FIELDS)

        with redirect_stdout(StringIO()):
            build_lead_files(self.input_path, self.root)
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in OUTPUT_NAMES}, first,
        )

    def test_analyze_identities_cli_causes_zero_filesystem_mutation(self):
        marker = self.root / "marker.txt"
        marker.write_text("unchanged", encoding="utf-8")
        before = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.iterdir()
        }

        argv = [
            "build_leads.py", "--analyze-identities",
            "--input", str(self.input_path), "--output-folder", str(self.root),
        ]
        with patch("sys.argv", argv), redirect_stdout(StringIO()):
            main()

        after = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.iterdir()
        }
        self.assertEqual(after, before)
