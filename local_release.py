"""Safe local-release entry point for authoritative Google Maps discovery."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Callable, Sequence

from company_registry.exports import verify_run_exports
from company_registry.schema import SCHEMA_VERSION
from company_registry.shadow import DEFAULT_SHADOW_DATABASE
from company_registry.storage import DEFAULT_DATABASE
from utils.display_environment import validate_wslg_display


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_LOCAL_REGISTRY = PROJECT_ROOT / "data/local_registry/company_registry.db"
DEFAULT_LOCAL_EXPORTS = PROJECT_ROOT / "data/local_registry/exports"
DEFAULT_BROWSER_HOME = PROJECT_ROOT / "data/local_registry/browser_home"


class LocalReleaseError(RuntimeError):
    """A local release safety or verification check failed."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _protected_registries() -> set[Path]:
    return {
        (PROJECT_ROOT / DEFAULT_DATABASE).resolve(),
        (PROJECT_ROOT / DEFAULT_SHADOW_DATABASE).resolve(),
    }


def validate_registry_path(path: str | Path) -> Path:
    """Require an existing local registry that is not production or shadow."""
    registry = _resolved(path)
    if registry in _protected_registries():
        raise LocalReleaseError(
            f"Local release refuses the protected production/shadow registry: {registry}"
        )
    if not registry.is_file():
        raise LocalReleaseError(f"Local schema-v6 registry does not exist: {registry}")
    return registry


def verify_sqlite_backup(path: str | Path) -> dict[str, object]:
    """Reopen a backup read-only and verify schema, integrity, and foreign keys."""
    backup = _resolved(path)
    if not backup.is_file():
        raise LocalReleaseError(f"SQLite backup does not exist: {backup}")
    connection = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if version != SCHEMA_VERSION:
        raise LocalReleaseError(
            f"Backup schema is {version}; local release requires schema {SCHEMA_VERSION}"
        )
    if integrity != ["ok"]:
        raise LocalReleaseError(f"Backup integrity verification failed: {integrity}")
    if foreign_keys:
        raise LocalReleaseError(
            f"Backup contains {len(foreign_keys)} foreign-key violation(s)"
        )
    return {
        "path": backup,
        "schema_version": version,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "sha256": sha256(backup.read_bytes()).hexdigest(),
        "size_bytes": backup.stat().st_size,
    }


def create_verified_backup(
    registry_path: str | Path,
    backup_directory: str | Path | None = None,
    *,
    label: str = "manual",
    now: datetime | None = None,
) -> dict[str, object]:
    """Create a unique SQLite online backup and a verification manifest."""
    registry = validate_registry_path(registry_path)
    destination_directory = _resolved(
        backup_directory or registry.parent / "backups"
    )
    destination_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination_directory, 0o700)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-.") or "backup"
    destination = destination_directory / f"registry_{safe_label}_{timestamp}.db"
    manifest = destination.with_suffix(".verification.json")
    if destination.exists() or manifest.exists():
        raise LocalReleaseError(f"Refusing to overwrite existing backup: {destination}")

    source = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    target = None
    try:
        target = sqlite3.connect(destination)
        source.backup(target)
        target.close()
        target = None
    except BaseException:
        if target is not None:
            target.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    os.chmod(destination, 0o600)
    try:
        result = verify_sqlite_backup(destination)
        record = {
            **result,
            "path": str(result["path"]),
            "source": str(registry),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(manifest, 0o600)
    except BaseException:
        destination.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise
    return {**result, "manifest": manifest}


def build_maps_command(
    *,
    query_file: str | Path,
    max_new: int,
    run_id: str,
    registry_path: str | Path = DEFAULT_LOCAL_REGISTRY,
    export_directory: str | Path = DEFAULT_LOCAL_EXPORTS,
    resume: bool = False,
    show_browser: bool = False,
    diagnostic_stack_interval_seconds: float | None = None,
) -> list[str]:
    """Construct the fixed authoritative Maps command for a fresh or resumed run."""
    if max_new < 1:
        raise LocalReleaseError("--max-new must be >= 1")
    if not run_id.strip():
        raise LocalReleaseError("--run-id must not be empty")
    if diagnostic_stack_interval_seconds is not None:
        raise LocalReleaseError(
            "--diagnostic-stack-interval-seconds is disabled because the "
            "CPython faulthandler watchdog is unsafe"
        )
    identifier_option = "--resume-run-id" if resume else "--registry-run-id"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "maps.py"),
        "--query-file", str(_resolved(query_file)),
        "--limit", str(max_new),
        "--low-resource",
        "--discovery-mode", "authoritative",
        "--registry-database", str(_resolved(registry_path)),
        identifier_option, run_id,
        "--authoritative-export-dir", str(_resolved(export_directory)),
    ]
    if show_browser:
        command.append("--show-browser")
    return command


def _validate_run_selection(registry: Path, run_id: str, resume: bool) -> None:
    connection = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status FROM discovery_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if resume and row is None:
        raise LocalReleaseError(f"Cannot resume unknown run ID: {run_id}")
    if not resume and row is not None:
        raise LocalReleaseError(f"Fresh run ID already exists: {run_id}")


def _run_status(registry: Path, run_id: str) -> str | None:
    connection = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status FROM discovery_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def _lease_diagnostic_path(registry: Path) -> Path:
    return registry.parent / "logs/writer_lease.jsonl"


def execute_release(
    args: Namespace,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, object]:
    """Back up, launch Maps, verify SUCCESS outputs, and back up again."""
    registry = validate_registry_path(args.registry_path)
    query_file = _resolved(args.query_file)
    if not query_file.is_file():
        raise LocalReleaseError(f"Query file does not exist: {query_file}")
    if not any(line.strip() for line in query_file.read_text(encoding="utf-8").splitlines()):
        raise LocalReleaseError(f"Query file has no usable queries: {query_file}")
    stack_interval = getattr(args, "diagnostic_stack_interval_seconds", None)
    if stack_interval is not None:
        raise LocalReleaseError(
            "--diagnostic-stack-interval-seconds is disabled because the "
            "CPython faulthandler watchdog is unsafe"
        )
    show_browser = getattr(args, "show_browser", False)
    if show_browser:
        try:
            validate_wslg_display()
        except ValueError as error:
            raise LocalReleaseError(str(error)) from error
    _validate_run_selection(registry, args.run_id, args.command == "resume")
    pre_backup = create_verified_backup(
        registry, args.backup_directory, label=f"pre-{args.run_id}"
    )

    browser_home = _resolved(args.browser_home)
    browser_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(browser_home, 0o700)
    environment = os.environ.copy()
    environment["HOME"] = str(browser_home)
    command = build_maps_command(
        query_file=query_file,
        max_new=args.max_new,
        run_id=args.run_id,
        registry_path=registry,
        export_directory=args.export_directory,
        resume=args.command == "resume",
        show_browser=show_browser,
        diagnostic_stack_interval_seconds=stack_interval,
    )
    completed = runner(command, cwd=PROJECT_ROOT, env=environment, check=False)
    if completed.returncode != 0:
        raise LocalReleaseError(
            f"Maps process failed with exit code {completed.returncode}; "
            f"verified pre-run backup: {pre_backup['path']}; "
            f"writer-lease diagnostics: {_lease_diagnostic_path(registry)}"
        )
    status = _run_status(registry, args.run_id)
    if status != "SUCCESS":
        raise LocalReleaseError(
            f"Run {args.run_id} ended with status {status or 'MISSING'}, not SUCCESS; "
            f"verified pre-run backup: {pre_backup['path']}"
        )
    exports = verify_run_exports(registry, args.run_id, _resolved(args.export_directory))
    post_backup = create_verified_backup(
        registry, args.backup_directory, label=f"post-{args.run_id}"
    )
    return {
        "status": status,
        "command": command,
        "pre_backup": pre_backup,
        "post_backup": post_backup,
        "exports": exports,
        "lease_diagnostics": _lease_diagnostic_path(registry),
    }


def _add_registry_options(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--registry-path", default=str(DEFAULT_LOCAL_REGISTRY),
        help="Persistent schema-v6 local registry",
    )
    parser.add_argument(
        "--backup-directory", default=None,
        help="Backup directory (default: <registry-directory>/backups)",
    )


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Safe local Google Maps release workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="Create and verify an online SQLite backup")
    _add_registry_options(backup)

    for name in ("run", "resume"):
        operation = subparsers.add_parser(name, help=f"{name.title()} a local Maps run")
        operation.add_argument("--query-file", required=True)
        operation.add_argument("--max-new", required=True, type=int)
        operation.add_argument("--run-id", required=True)
        _add_registry_options(operation)
        operation.add_argument(
            "--export-directory", default=str(DEFAULT_LOCAL_EXPORTS),
            help="Directory for the three verified CSV/manifest pairs",
        )
        operation.add_argument(
            "--browser-home", default=str(DEFAULT_BROWSER_HOME),
            help="Isolated writable HOME for Chrome/ChromeDriver caches",
        )
        operation.add_argument(
            "--show-browser", action="store_true",
            help="Show Chrome in a visible WSLg window",
        )
        operation.add_argument(
            "--diagnostic-stack-interval-seconds", type=float,
            help="Disabled: periodic faulthandler stack snapshots are unsafe",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_verified_backup(args.registry_path, args.backup_directory)
            print(f"Verified backup: {result['path']}")
            print(f"Verification: {result['manifest']}")
            print(f"SHA-256: {result['sha256']}")
            return 0
        result = execute_release(args)
        print(f"Run status: {result['status']}")
        for name, export in result["exports"].items():
            print(f"{name.title()} CSV: {export['csv']}")
            print(f"{name.title()} manifest: {export['manifest']}")
        print(f"Post-run backup: {result['post_backup']['path']}")
        print(f"Writer-lease diagnostics: {result['lease_diagnostics']}")
        return 0
    except (LocalReleaseError, OSError, sqlite3.Error, UnicodeError, ValueError) as error:
        print(f"Local release failed safely: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
