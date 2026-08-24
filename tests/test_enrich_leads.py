"""Fixture tests for resumable lead-enrichment status handling."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.enrich_leads import enrich_leads_batch


class FakeDriver:
    def quit(self):
        pass


def write_input(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(
            file_handler,
            fieldnames=("name", "email", "phone", "website"),
        )
        writer.writeheader()
        writer.writerows(rows)


def read_output(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def successful_enrichment(website):
    return {
        "description": f"Supported software context for {website}",
        "industry": "Software",
        "website_title": "Software Company",
    }


class PendingLeadTests(TestCase):
    def test_limit_and_rerun_resume_pending_without_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "leads_ready.csv"
            output_path = root / "leads_enriched.csv"
            write_input(input_path, [
                {
                    "name": f"Company {index}",
                    "email": f"info@company{index}.test",
                    "phone": str(index),
                    "website": f"https://company{index}.test",
                }
                for index in range(69)
            ])

            calls = []

            def enrich(driver, website, timeout, verbose):
                calls.append(website)
                return successful_enrichment(website)

            first_summary = enrich_leads_batch(
                input_path=input_path,
                output_path=output_path,
                limit=5,
                driver_factory=FakeDriver,
                enrichment_function=enrich,
            )
            first_rows = read_output(output_path)

            self.assertEqual(first_summary["Attempted this run"], 5)
            self.assertEqual(first_summary["Pending"], 64)
            self.assertEqual(len(first_rows), 69)
            self.assertEqual(
                sum(row["enrichment_status"] == "SUCCESS" for row in first_rows),
                5,
            )
            pending_rows = [
                row for row in first_rows
                if row["enrichment_status"] == "PENDING"
            ]
            self.assertEqual(len(pending_rows), 64)
            self.assertTrue(all(row["enrichment_attempts"] == "0" for row in pending_rows))

            successful_websites = {
                row["website"] for row in first_rows
                if row["enrichment_status"] == "SUCCESS"
            }
            second_summary = enrich_leads_batch(
                input_path=input_path,
                output_path=output_path,
                limit=5,
                driver_factory=FakeDriver,
                enrichment_function=enrich,
            )
            second_rows = read_output(output_path)

            self.assertEqual(second_summary["Already enriched"], 5)
            self.assertEqual(second_summary["Attempted this run"], 5)
            self.assertEqual(second_summary["Pending"], 59)
            self.assertEqual(len(second_rows), 69)
            self.assertEqual(
                len({row["website"] for row in second_rows}),
                69,
            )
            self.assertTrue(successful_websites.isdisjoint(calls[5:]))
            for row in second_rows[:5]:
                self.assertEqual(row["enrichment_status"], "SUCCESS")
                self.assertEqual(row["enrichment_attempts"], "1")

    def test_incomplete_requires_a_real_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "leads_ready.csv"
            output_path = root / "leads_enriched.csv"
            write_input(input_path, [
                {
                    "name": "Attempted Empty",
                    "email": "info@empty.test",
                    "phone": "1",
                    "website": "https://empty.test",
                },
                {
                    "name": "Untouched",
                    "email": "info@untouched.test",
                    "phone": "2",
                    "website": "https://untouched.test",
                },
            ])

            summary = enrich_leads_batch(
                input_path=input_path,
                output_path=output_path,
                limit=1,
                driver_factory=FakeDriver,
                enrichment_function=lambda *args, **kwargs: {},
            )
            rows = read_output(output_path)

            self.assertEqual(summary["Attempted this run"], 1)
            self.assertEqual(summary["Incomplete"], 1)
            self.assertEqual(summary["Pending"], 1)
            self.assertEqual(rows[0]["enrichment_status"], "INCOMPLETE")
            self.assertEqual(rows[0]["enrichment_attempts"], "1")
            self.assertEqual(rows[1]["enrichment_status"], "PENDING")
            self.assertEqual(rows[1]["enrichment_attempts"], "0")


if __name__ == "__main__":
    main()
