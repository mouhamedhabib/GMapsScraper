"""Regression tests for mutually exclusive outreach review gating."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.build_outreach import OUTPUT_FIELDS, build_outreach
from utils.build_outreach_queue import build_queue, read_csv as read_queue_csv


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def enriched_row(index, status="SUCCESS", description="Company description"):
    row = {field: "" for field in OUTPUT_FIELDS}
    row.update({
        "company_name": f"Company {index}",
        "email": f"contact{index}@company{index}.test",
        "website": f"https://company{index}.test",
        "description": description,
        "industry": "Software",
        "services": "Development",
        "enrichment_status": status,
    })
    return row


class OutreachReviewGatingTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "leads_enriched_final.csv"
        self.ready = self.root / "outreach_ready.csv"
        self.review = self.root / "outreach_review.csv"
        self.history = self.root / "outreach_history.csv"
        self.queue = self.root / "outreach_queue.csv"

    def build(self, rows):
        write_csv(self.input, OUTPUT_FIELDS, rows)
        return build_outreach(self.input, self.ready, self.review)

    def test_success_without_review_condition_goes_to_ready_only_and_queue(self):
        self.build([enriched_row(1)])

        self.assertEqual([row["company_name"] for row in read_rows(self.ready)], ["Company 1"])
        self.assertEqual(read_rows(self.review), [])

        build_queue(self.ready, self.history, self.queue)
        self.assertEqual([row["company_name"] for row in read_queue_csv(self.queue)[1]], ["Company 1"])

    def test_partial_goes_to_review_only_and_not_queue(self):
        self.build([enriched_row(1, status="PARTIAL")])

        self.assertEqual(read_rows(self.ready), [])
        self.assertEqual([row["company_name"] for row in read_rows(self.review)], ["Company 1"])

        build_queue(self.ready, self.history, self.queue)
        self.assertEqual(read_queue_csv(self.queue)[1], [])

    def test_missing_description_goes_to_review_only(self):
        self.build([enriched_row(1, description="")])

        self.assertEqual(read_rows(self.ready), [])
        self.assertEqual([row["company_name"] for row in read_rows(self.review)], ["Company 1"])

    def test_existing_local_review_flag_goes_to_review_only(self):
        flagged = enriched_row(1)
        flagged.update({"review_status": "REVIEW", "review_reasons": "manual check"})
        write_csv(
            self.root / "leads_master.csv",
            (*OUTPUT_FIELDS, "review_status", "review_reasons"),
            [flagged],
        )

        self.build([enriched_row(1)])

        self.assertEqual(read_rows(self.ready), [])
        self.assertEqual([row["company_name"] for row in read_rows(self.review)], ["Company 1"])

    def test_queue_defensively_rejects_explicit_review_marker(self):
        fields = (*OUTPUT_FIELDS, "review_status", "review_reasons")
        safe = enriched_row(1)
        safe.update({"review_status": "", "review_reasons": ""})
        flagged = enriched_row(2)
        flagged.update({"review_status": "REVIEW", "review_reasons": "manual check"})
        write_csv(self.ready, fields, [safe, flagged])

        build_queue(self.ready, self.history, self.queue)

        self.assertEqual([row["company_name"] for row in read_queue_csv(self.queue)[1]], ["Company 1"])

    def test_347_safe_and_95_review_rows_produce_347_queue_rows(self):
        rows = [enriched_row(index) for index in range(347)]
        rows.extend(enriched_row(index, status="PARTIAL") for index in range(347, 442))

        summary = self.build(rows)
        first_ready = self.ready.read_bytes()
        first_review = self.review.read_bytes()
        queue_summary = build_queue(self.ready, self.history, self.queue)
        first_queue = self.queue.read_bytes()

        self.assertEqual(summary["eligible"], 442)
        self.assertEqual(summary["ready"], 347)
        self.assertEqual(summary["review"], 95)
        self.assertEqual(queue_summary["queue"], 347)

        self.build(rows)
        build_queue(self.ready, self.history, self.queue)
        self.assertEqual(self.ready.read_bytes(), first_ready)
        self.assertEqual(self.review.read_bytes(), first_review)
        self.assertEqual(self.queue.read_bytes(), first_queue)


if __name__ == "__main__":
    main()
