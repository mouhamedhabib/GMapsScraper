import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from company_registry.storage import initialize_registry
from local_release import (
    DEFAULT_BROWSER_HOME,
    DEFAULT_LOCAL_EXPORTS,
    DEFAULT_LOCAL_REGISTRY,
    LocalReleaseError,
    build_maps_command,
    build_parser,
    create_verified_backup,
    execute_release,
    validate_registry_path,
    verify_sqlite_backup,
)


class LocalReleaseTests(TestCase):
    def make_registry(self, root: Path) -> Path:
        database = root / "registry.db"
        initialize_registry(database)
        return database

    def release_args(self, root: Path, registry: Path, command: str = "run") -> Namespace:
        query_file = root / "queries.txt"
        query_file.write_text("software companies Tunis\n", encoding="utf-8")
        return Namespace(
            command=command,
            query_file=str(query_file),
            max_new=3,
            run_id="local-20261010",
            registry_path=str(registry),
            backup_directory=str(root / "backups"),
            export_directory=str(root / "exports"),
            browser_home=str(root / "browser-home"),
        )

    def test_parser_uses_permanent_local_defaults(self):
        args = build_parser().parse_args([
            "run", "--query-file", "queries.txt", "--max-new", "3",
            "--run-id", "daily-1",
        ])
        self.assertEqual(Path(args.registry_path), DEFAULT_LOCAL_REGISTRY)
        self.assertEqual(Path(args.export_directory), DEFAULT_LOCAL_EXPORTS)
        self.assertEqual(Path(args.browser_home), DEFAULT_BROWSER_HOME)

    def test_fresh_command_is_fixed_to_authoritative_local_options(self):
        command = build_maps_command(
            query_file="queries.txt", max_new=3, run_id="daily-1",
        )
        self.assertIn("authoritative", command)
        self.assertIn("--registry-run-id", command)
        self.assertNotIn("--resume-run-id", command)
        self.assertNotIn("--allow-production-registry", command)
        self.assertEqual(command[command.index("--limit") + 1], "3")

    def test_resume_command_uses_only_explicit_resume_id(self):
        command = build_maps_command(
            query_file="queries.txt", max_new=3, run_id="daily-1", resume=True,
        )
        self.assertIn("--resume-run-id", command)
        self.assertNotIn("--registry-run-id", command)
        self.assertEqual(command[command.index("--resume-run-id") + 1], "daily-1")

    def test_production_and_shadow_paths_are_rejected(self):
        for relative in ("data/company_registry.db", "data/company_registry_shadow.db"):
            with self.subTest(relative=relative), self.assertRaisesRegex(
                LocalReleaseError, "protected production/shadow"
            ):
                validate_registry_path(relative)

    def test_backup_is_online_verified_private_and_manifested(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            result = create_verified_backup(
                registry, root / "backups", label="pre-run",
                now=datetime(2026, 10, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(result["schema_version"], 6)
            self.assertEqual(result["integrity_check"], "ok")
            self.assertEqual(result["foreign_key_violations"], 0)
            self.assertEqual(result["path"].stat().st_mode & 0o777, 0o600)
            record = json.loads(result["manifest"].read_text(encoding="utf-8"))
            self.assertEqual(record["sha256"], result["sha256"])
            self.assertEqual(verify_sqlite_backup(result["path"])["sha256"], result["sha256"])

    def test_backup_never_overwrites_an_existing_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            instant = datetime(2026, 10, 10, tzinfo=timezone.utc)
            create_verified_backup(registry, root / "backups", now=instant)
            with self.assertRaisesRegex(LocalReleaseError, "Refusing to overwrite"):
                create_verified_backup(registry, root / "backups", now=instant)

    def test_invalid_backup_fails_verification(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "old.db"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA user_version=2")
            connection.close()
            with self.assertRaisesRegex(LocalReleaseError, "requires schema 6"):
                verify_sqlite_backup(database)

    def test_required_backup_failure_prevents_maps_launch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            args = self.release_args(root, registry)
            runner = Mock()
            with patch(
                "local_release.create_verified_backup",
                side_effect=LocalReleaseError("verification failed"),
            ), self.assertRaisesRegex(LocalReleaseError, "verification failed"):
                execute_release(args, runner=runner)
            runner.assert_not_called()

    def test_subprocess_failure_is_reported_and_home_is_isolated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            args = self.release_args(root, registry)
            runner = Mock(return_value=CompletedProcess([], 9))
            with self.assertRaisesRegex(LocalReleaseError, "exit code 9"):
                execute_release(args, runner=runner)
            kwargs = runner.call_args.kwargs
            self.assertEqual(kwargs["env"]["HOME"], str((root / "browser-home").resolve()))
            self.assertFalse(kwargs["check"])

    def test_success_verifies_exports_and_creates_post_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            args = self.release_args(root, registry)
            backups = [
                {"path": root / "pre.db"},
                {"path": root / "post.db"},
            ]
            exports = {
                name: {"csv": root / f"{name}.csv", "manifest": root / f"{name}.json"}
                for name in ("discovery", "employment", "mission")
            }
            with patch("local_release.create_verified_backup", side_effect=backups) as backup, patch(
                "local_release._run_status", return_value="SUCCESS"
            ), patch("local_release.verify_run_exports", return_value=exports) as verify:
                result = execute_release(
                    args, runner=Mock(return_value=CompletedProcess([], 0))
                )
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(backup.call_count, 2)
            verify.assert_called_once_with(
                registry.resolve(), args.run_id, (root / "exports").resolve()
            )

    def test_resume_requires_an_existing_run_before_launch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.make_registry(root)
            args = self.release_args(root, registry, command="resume")
            runner = Mock()
            with self.assertRaisesRegex(LocalReleaseError, "unknown run ID"):
                execute_release(args, runner=runner)
            runner.assert_not_called()

