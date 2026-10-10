"""
Google Maps Scraper CLI
=======================

Purpose:
    Parse command-line options and start concurrent Google Maps scraping.

Pipeline:
    queries.txt -> maps.py -> google_maps_scraper.py -> google_maps_data.*

Input:
    A text file containing one Maps search query per line.

Output:
    Google Maps business records in CSV, JSON, or Excel format. The CSV output
    is normally consumed next by ``utils/build_leads.py``.

Previous / next:
    This is the pipeline entry point; ``utils/build_leads.py`` normally follows.
"""

from utils.threading_controller import FastSearchAlgo
from argparse import ArgumentParser
from os import R_OK, access
from os.path import isfile
from pathlib import Path
import sys

from job_search.csv_exports import DEFAULT_EXPORT_DIRECTORY, export_maps_rows
from company_registry.shadow import (
    DEFAULT_REPORT_DIRECTORY,
    DEFAULT_SHADOW_DATABASE,
    open_shadow_observer,
)
from company_registry.discovery_adapter import DiscoveryMode, parse_discovery_mode
from company_registry.maps_authoritative import DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY


def _finalize_shadow(observer, observability=None):
    """Best-effort passive reporting that cannot affect scraper results."""
    if observer is None:
        return
    if observability is not None:
        try:
            observer.set_run_observability(observability)
        except BaseException as exception:
            print(
                "[shadow] observability error ignored: "
                f"{type(exception).__name__}: {exception}"
            )
    try:
        observer.close()
    except BaseException as exception:
        print(
            "[shadow] finalization error ignored: "
            f"{type(exception).__name__}: {exception}"
        )


def run_maps_discovery(
    query_file="./queries.txt", limit=1, threads=1, output_folder="./CSV_FILES",
    browser_wait=15, scroll_minutes=1, windowed=False, low_resource=False,
    verbose=True,
    export_directory=DEFAULT_EXPORT_DIRECTORY,
    company_registry_shadow=False,
    shadow_database=DEFAULT_SHADOW_DATABASE,
    shadow_report_directory=DEFAULT_REPORT_DIRECTORY,
    known_companies_directory="./CSV_FILES",
    allow_empty_known_companies=False,
    discovery_mode="legacy",
    registry_database=None,
    registry_run_id=None,
    resume_run_id=None,
    authoritative_export_directory=DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY,
    allow_production_registry=False,
):
    """Run the existing Maps pipeline incrementally and return its counters."""
    queries = FastSearchAlgo.load_query_file(file_name=str(query_file))
    if not queries:
        raise ValueError(f"No usable search queries found in {query_file}")
    mode = parse_discovery_mode(discovery_mode)
    if company_registry_shadow and mode not in {DiscoveryMode.LEGACY, DiscoveryMode.SHADOW}:
        raise ValueError("passive shadow and authoritative modes are mutually exclusive")
    if company_registry_shadow:
        mode = DiscoveryMode.SHADOW
    shadow = open_shadow_observer(
        mode is DiscoveryMode.SHADOW, source_system="GOOGLE_MAPS",
        database=Path(shadow_database), report_directory=Path(shadow_report_directory),
    )
    try:
        algo = FastSearchAlgo(
            headless=not windowed, wait_time=browser_wait, output_path=str(output_folder),
            workers=min(threads, len(queries)), result_range=limit, scroll_minutes=scroll_minutes,
            verbose=verbose, output_format="CSV", incremental=True,
            low_resource=low_resource, shadow_observer=shadow,
            known_companies_dir=str(known_companies_directory),
            allow_empty_known_companies=allow_empty_known_companies,
            query_file_path=query_file,
            discovery_mode=mode.value,
            registry_database=registry_database,
            registry_run_id=registry_run_id,
            resume_run_id=resume_run_id,
            authoritative_export_directory=authoritative_export_directory,
            allow_production_registry=allow_production_registry,
        )
        stats = algo.fast_search_algorithm(queries)
    finally:
        observability = None
        if 'algo' in locals():
            try:
                observability = algo.run_observability()
            except BaseException:
                pass
        _finalize_shadow(shadow, observability)
    if not mode.is_authoritative:
        exported = export_maps_rows(
            getattr(algo, "run_records", lambda: [])(),
            export_directory=export_directory,
        )
        stats.update({
            "csv_path": str(exported["timestamped"]),
            "csv_latest": str(exported["latest"]),
            "csv_rows": exported["rows"],
        })
    return stats


class GMapsScraper:
    """Translate CLI settings into one configured, concurrent Maps scrape."""

    def __init__(self):
        self._args = None
        self._parser = None
        self._run_records = []
        self._effective_mode = DiscoveryMode.LEGACY
        self._stats = None

    def arg_parser(self):
        """Parse scraper options and store the resulting command-line namespace."""
        parser = ArgumentParser(description='Command Line Google Map Scraper by Abdul Moez')

        # Input options
        parser.add_argument('-q', '--query-file',
                            help='Path to query file (default: ./queries.txt)', type=str,
                            default="./queries.txt")
        parser.add_argument('-w', '--threads',
                            help='Number of threads to use (default: 1)', type=int, default=1)
        parser.add_argument('-l', '--limit',
                            help='Maximum newly persisted companies across the entire run (default: 1)',
                            type=int, default=1)
        parser.add_argument(
            '--incremental', action='store_true',
            help='Make --limit count only new companies and skip known identities early',
        )
        parser.add_argument('-u', '--unavailable-text',
                            help='Replacement text for unavailable information (default: "Not Available")', type=str,
                            default="Not Available")
        parser.add_argument('-bw', '--browser-wait',
                            help='Browser waiting time in seconds (default: 15)', type=int,
                            default=15)
        parser.add_argument('-se', '--suggested-ext',
                            help='Suggested URL extensions to try (can be specified multiple times)', action='append',
                            default=None)
        parser.add_argument('-wb', '--windowed-browser',
                            help='Disable headless mode', action='store_false',
                            default=True)
        parser.add_argument('-nv', '--disable-verbose', help='Disable verbose mode', action='store_true')
        parser.add_argument('-o', '--output-folder',
                            help='Output folder to store CSV details (default: ./CSV_FILES)',
                            type=str, default='./CSV_FILES')
        parser.add_argument(
            '--known-companies-dir', type=str, default='./CSV_FILES',
            help='Historical CSV directory used by incremental identity checks',
        )
        parser.add_argument(
            '--allow-empty-known-companies', action='store_true',
            help=(
                'Deliberately allow incremental discovery with a missing or empty '
                'historical identity baseline'
            ),
        )
        parser.add_argument(
            '--export-dir', type=str, default=str(DEFAULT_EXPORT_DIRECTORY),
            help='Timestamped CSV export directory',
        )

        parser.add_argument('-of', '--output-format',
                            help='Output format to store scraped data. '
                                 'Available formats [CSV, EXCEL, JSON] (default: CSV)',
                            type=str, default='CSV', choices=["CSV", "EXCEL", "JSON"])
        parser.add_argument('-sm', '--scroll-minutes',
                            help='Maximum minutes to wait for end of results the waiting time in minutes (default: 1)',
                            type=int,
                            default=1)
        parser.add_argument(
            '--low-resource', action='store_true',
            help=(
                'Use one worker and resource-conscious Chrome settings; '
                'legacy -w behavior is unchanged without this flag'
            ),
        )
        parser.add_argument(
            '--company-registry-shadow', action='store_true',
            help='Record passive registry comparisons; legacy decisions remain authoritative',
        )
        parser.add_argument(
            '--discovery-mode',
            choices=[mode.value for mode in DiscoveryMode],
            default=None,
            help=(
                'Identity authority mode: legacy (default), shadow, '
                'authoritative-canary, or authoritative'
            ),
        )
        parser.add_argument(
            '--registry-database', type=str,
            help=(
                'Explicit schema-v6 SQLite registry for authoritative modes; '
                'the canonical production path remains denied without its '
                'separate opt-in'
            ),
        )
        parser.add_argument(
            '--registry-run-id', type=str,
            help='New durable run ID; must not already exist',
        )
        parser.add_argument(
            '--resume-run-id', type=str,
            help='Explicitly resume an eligible schema-v6 Maps run',
        )
        parser.add_argument(
            '--authoritative-export-dir', type=str,
            default=str(DEFAULT_AUTHORITATIVE_EXPORT_DIRECTORY),
            help='Isolated directory for SQLite-derived authoritative exports',
        )
        parser.add_argument(
            '--allow-production-registry', action='store_true',
            help=(
                'Explicitly permit a validated schema-v6 canonical production '
                'registry in authoritative mode; denied by default'
            ),
        )
        parser.add_argument(
            '--shadow-database', type=str, default=str(DEFAULT_SHADOW_DATABASE),
            help='Initialized isolated company-registry shadow database',
        )
        parser.add_argument(
            '--shadow-report-dir', type=str, default=str(DEFAULT_REPORT_DIRECTORY),
            help='Directory for passive shadow comparison reports',
        )

        # Custom commands for additional help
        parser.add_argument('--help-query-file',
                            action='store_true', help='Get help for specifying the query file')
        parser.add_argument('--help-limit', action='store_true',
                            help='Get help for specifying the result limit')
        parser.add_argument('--help-driver-path', action='store_true',
                            help='Get help for specifying the driver path')

        self._parser = parser
        self._args = parser.parse_args()
        if self._args.limit < 1:
            parser.error("--limit must be >= 1")
        if self._args.threads < 1:
            parser.error("--threads must be >= 1")
        if self._args.browser_wait < 1:
            parser.error("--browser-wait must be >= 1")
        if self._args.scroll_minutes < 1:
            parser.error("--scroll-minutes must be >= 1")
        if self._args.incremental and self._args.output_format != "CSV":
            parser.error("--incremental requires --output-format CSV for restart-safe state")
        selected = self._args.discovery_mode or "legacy"
        if self._args.company_registry_shadow:
            if self._args.discovery_mode not in (None, "shadow"):
                parser.error(
                    "--company-registry-shadow cannot be combined with a non-shadow "
                    "--discovery-mode"
                )
            selected = "shadow"
        self._effective_mode = parse_discovery_mode(selected)
        if self._effective_mode.is_authoritative and not self._args.registry_database:
            parser.error("authoritative modes require --registry-database")
        if self._args.resume_run_id and self._args.registry_run_id:
            parser.error("--resume-run-id and --registry-run-id are mutually exclusive")
        if self._args.resume_run_id and not self._effective_mode.is_authoritative:
            parser.error("--resume-run-id requires an authoritative discovery mode")
        if self._args.allow_production_registry and (
            self._effective_mode is not DiscoveryMode.AUTHORITATIVE
        ):
            parser.error(
                "--allow-production-registry requires --discovery-mode authoritative"
            )

    @staticmethod
    def print_query_file_help():
        print("The query file should contain a list of search queries, each query on a separate line.")
        print("For example:")
        print("Pizza restaurants")
        print("Coffee shops")
        print("...")
        sys.exit(0)

    @staticmethod
    def print_limit_help():
        print("Use this option to cap newly persisted companies across the entire run.")
        print("The limit must be at least 1.")
        sys.exit(0)

    def check_args(self):
        """Validate and return normalized queries before scraper initialization."""
        q = self._args.query_file
        if not isfile(q):
            self._parser.error(f"Query file does not exist or is not a regular file: {q}")
        if not access(q, R_OK):
            self._parser.error(f"Query file is not readable: {q}")
        try:
            queries = FastSearchAlgo.load_query_file(file_name=q)
        except (OSError, UnicodeError) as exc:
            self._parser.error(f"Could not read query file {q}: {exc}")
        if not queries:
            self._parser.error(f"No usable search queries found in {q}")
        return queries

    def scrape_maps_data(self):
        """Load queries, configure worker limits, and run the Maps scraper."""
        if self._args.help_query_file:
            self.print_query_file_help()

        if self._args.help_limit:
            self.print_limit_help()

        queries_list = self.check_args()
        threads_limit = min(self._args.threads, len(queries_list))
        if self._args.low_resource:
            threads_limit = min(threads_limit, 1)
        limit_results = self._args.limit

        shadow = open_shadow_observer(
            self._effective_mode is DiscoveryMode.SHADOW,
            source_system="GOOGLE_MAPS",
            database=Path(self._args.shadow_database),
            report_directory=Path(self._args.shadow_report_dir),
        )
        try:
            algo_obj = FastSearchAlgo(
                unavailable_text=self._args.unavailable_text,
                headless=self._args.windowed_browser,
                wait_time=self._args.browser_wait,
                suggested_ext=self._args.suggested_ext,
                output_path=self._args.output_folder,
                workers=threads_limit,
                result_range=limit_results,
                scroll_minutes=self._args.scroll_minutes,
                verbose=False if self._args.disable_verbose else True,
                output_format=self._args.output_format,
                incremental=self._args.incremental,
                low_resource=self._args.low_resource,
                shadow_observer=shadow,
                known_companies_dir=self._args.known_companies_dir,
                allow_empty_known_companies=self._args.allow_empty_known_companies,
                query_file_path=self._args.query_file,
                discovery_mode=self._effective_mode.value,
                registry_database=self._args.registry_database,
                registry_run_id=self._args.registry_run_id,
                resume_run_id=self._args.resume_run_id,
                authoritative_export_directory=self._args.authoritative_export_dir,
                allow_production_registry=self._args.allow_production_registry,
            )
            stats = algo_obj.fast_search_algorithm(queries_list)
        except (FileNotFoundError, ValueError) as exception:
            self._parser.error(str(exception))
        finally:
            observability = None
            if 'algo_obj' in locals():
                try:
                    observability = algo_obj.run_observability()
                except BaseException:
                    pass
            _finalize_shadow(shadow, observability)
        self._run_records = getattr(algo_obj, "run_records", lambda: [])()
        self._stats = stats
        return stats

    def export_maps_csv(self):
        if self._effective_mode.is_authoritative:
            export = self._stats.get("authoritative_export")
            termination = self._stats.get("run_observability", {}).get(
                "termination_reason"
            )
            if export is None and termination == "INTERRUPTED":
                return None
            if export is None:
                raise RuntimeError(
                    "Authoritative Maps run completed without export metadata"
                )
            return export
        return export_maps_rows(
            self._run_records, export_directory=self._args.export_dir,
        )


def main():
    App = GMapsScraper()
    App.arg_parser()
    App.scrape_maps_data()
    exported = App.export_maps_csv()
    if exported is None:
        print("Authoritative Maps run interrupted; no export finalized.")
        return
    if "csv" in exported and "manifest" in exported:
        print(f"Authoritative Maps CSV: {exported['csv']}")
        print(f"Manifest: {exported['manifest']}")
    else:
        print(f"Maps CSV: {exported['timestamped']}")
        print(f"Latest: {exported['latest']}")


if __name__ == '__main__':
    main()
