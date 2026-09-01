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
from os.path import isfile
import sys


class GMapsScraper:
    """Translate CLI settings into one configured, concurrent Maps scrape."""

    def __init__(self):
        self._args = None

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
                            help='Number of results to scrape (-1 for all results, default: -1)',
                            type=int, default=-1)
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
                            default=[])
        parser.add_argument('-wb', '--windowed-browser',
                            help='Disable headless mode', action='store_false',
                            default=True)
        parser.add_argument('-nv', '--disable-verbose', help='Disable verbose mode', action='store_true')
        parser.add_argument('-o', '--output-folder',
                            help='Output folder to store CSV details (default: ./CSV_FILES)',
                            type=str, default='./CSV_FILES')

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

        # Custom commands for additional help
        parser.add_argument('--help-query-file',
                            action='store_true', help='Get help for specifying the query file')
        parser.add_argument('--help-limit', action='store_true',
                            help='Get help for specifying the result limit')
        parser.add_argument('--help-driver-path', action='store_true',
                            help='Get help for specifying the driver path')

        self._args = parser.parse_args()
        if self._args.incremental and self._args.output_format != "CSV":
            parser.error("--incremental requires --output-format CSV for restart-safe state")

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
        print("Use this option to specify the maximum number of results to scrape.")
        print("Use '-1' to scrape all results.")
        sys.exit(0)

    def check_args(self):
        """Stop with a clear error when the required query file does not exist."""
        q = self._args.query_file
        if not isfile(q):
            print(f"[-] File not found at path: {q}")
            sys.exit(1)

    def scrape_maps_data(self):
        """Load queries, configure worker limits, and run the Maps scraper."""
        self.check_args()

        if self._args.help_query_file:
            self.print_query_file_help()

        if self._args.help_limit:
            self.print_limit_help()

        queries_list = FastSearchAlgo.load_query_file(file_name=self._args.query_file)
        threads_limit = min(self._args.threads, len(queries_list))
        if self._args.low_resource:
            threads_limit = min(threads_limit, 1)
        limit_results = None if self._args.limit == -1 else self._args.limit

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
        )

        algo_obj.fast_search_algorithm(queries_list)


if __name__ == '__main__':
    App = GMapsScraper()
    App.arg_parser()
    App.scrape_maps_data()
