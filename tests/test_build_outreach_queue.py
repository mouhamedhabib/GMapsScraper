"""Offline tests for persistent outreach history and queue protection."""

from csv import DictReader, DictWriter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from utils.build_outreach import OUTPUT_FIELDS
from utils.build_outreach_queue import (
    HISTORY_FIELDS,
    atomic_write_csv,
    build_queue,
    import_sent_and_rebuild,
    mark_history,
    read_csv,
)


def outreach_row(
    company="Acme",
    email="contact@acme.test",
    website="https://acme.test/",
    description="Personalized company description",
):
    row = {field: "" for field in OUTPUT_FIELDS}
    row.update({
        "company_name": company,
        "email": email,
        "website": website,
        "description": description,
        "industry": "Software",
        "services": "Development",
        "enrichment_status": "SUCCESS",
    })
    return row


def history_row(
    company="Acme",
    email="contact@acme.test",
    website="https://acme.test/",
    send_status="SENT",
    response_status="",
    sent_at="2026-08-25T10:00:00+00:00",
    notes="",
):
    return {
        "company_name": company,
        "email": email,
        "website": website,
        "normalized_domain": "acme.test",
        "send_status": send_status,
        "sent_at": sent_at,
        "response_status": response_status,
        "response_at": "",
        "notes": notes,
    }


class QueueFixture(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "outreach_ready.csv"
        self.history = self.root / "outreach_history.csv"
        self.output = self.root / "outreach_queue.csv"

    def write_outreach(self, rows):
        atomic_write_csv(self.input, OUTPUT_FIELDS, rows)

    def write_history(self, rows):
        atomic_write_csv(self.history, HISTORY_FIELDS, rows)

    def queue(self, exclude_failed=False):
        return build_queue(
            self.input,
            self.history,
            self.output,
            exclude_failed=exclude_failed,
        )

    def queued_rows(self):
        return read_csv(self.output)[1]


class OutreachQueueTests(QueueFixture):
    def test_new_company_enters_queue_and_history_is_initialized(self):
        self.write_outreach([outreach_row()])
        self.queue()
        self.assertTrue(self.history.exists())
        self.assertEqual(read_csv(self.history)[0], list(HISTORY_FIELDS))
        self.assertEqual(len(self.queued_rows()), 1)

    def test_sent_email_is_excluded_case_insensitively(self):
        self.write_outreach([outreach_row(email="CONTACT@ACME.TEST", website="")])
        self.write_history([history_row(website="")])
        self.queue()
        self.assertEqual(self.queued_rows(), [])

    def test_sent_domain_excludes_different_email_and_url_variants(self):
        variants = (
            "https://www.acme.test/",
            "https://acme.test/about",
            "http://acme.test",
        )
        for website in variants:
            with self.subTest(website=website):
                self.write_outreach([
                    outreach_row(email="new@acme.test", website=website)
                ])
                self.write_history([history_row(website="https://acme.test/")])
                self.queue()
                self.assertEqual(self.queued_rows(), [])

    def test_unsubscribed_and_bounced_are_excluded(self):
        for response_status in ("UNSUBSCRIBED", "BOUNCED"):
            with self.subTest(response_status=response_status):
                self.write_outreach([outreach_row()])
                self.write_history([history_row(
                    send_status="FAILED",
                    response_status=response_status,
                )])
                self.queue()
                self.assertEqual(self.queued_rows(), [])

    def test_failed_is_retryable_unless_explicitly_excluded(self):
        self.write_outreach([outreach_row()])
        self.write_history([history_row(send_status="FAILED")])
        self.queue()
        self.assertEqual(len(self.queued_rows()), 1)
        self.queue(exclude_failed=True)
        self.assertEqual(self.queued_rows(), [])

    def test_new_leads_enter_later_and_history_survives(self):
        original_history = history_row(notes="preserve this note")
        self.write_outreach([outreach_row()])
        self.write_history([original_history])
        self.queue()
        before = self.history.read_bytes()

        self.write_outreach([
            outreach_row(),
            outreach_row("New Co", "hello@new.test", "https://new.test/"),
        ])
        self.queue()

        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(
            [row["company_name"] for row in self.queued_rows()],
            ["New Co"],
        )

    def test_queue_is_deterministic_and_preserves_personalization_fields(self):
        self.write_outreach([outreach_row()])
        self.queue()
        first = self.output.read_bytes()
        self.queue()
        second = self.output.read_bytes()
        fields, rows = read_csv(self.output)

        self.assertEqual(first, second)
        self.assertEqual(fields, list(OUTPUT_FIELDS))
        self.assertEqual(rows[0]["description"], "Personalized company description")
        self.assertEqual(rows[0]["industry"], "Software")
        self.assertEqual(rows[0]["services"], "Development")


class MarkHistoryTests(QueueFixture):
    def test_mark_sent_updates_once_and_preserves_first_timestamp(self):
        row = outreach_row()
        first = mark_history(
            self.history,
            row,
            "SENT",
            timestamp="2026-08-25T10:00:00+00:00",
        )
        second = mark_history(
            self.history,
            row,
            "SENT",
            timestamp="2026-08-26T10:00:00+00:00",
        )
        history = read_csv(self.history)[1]

        self.assertEqual(first["send_status"], "SENT")
        self.assertEqual(second["sent_at"], "2026-08-25T10:00:00+00:00")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["normalized_domain"], "acme.test")

    def test_mark_failed_is_not_sent_and_remains_retryable(self):
        row = outreach_row()
        record = mark_history(self.history, row, "FAILED")
        self.write_outreach([row])
        self.queue()

        self.assertEqual(record["send_status"], "FAILED")
        self.assertEqual(record["sent_at"], "")
        self.assertEqual(len(self.queued_rows()), 1)

    def test_marking_never_replaces_populated_history_with_blanks(self):
        original = history_row(notes="keep", response_status="REPLIED")
        self.write_history([original])
        incomplete = outreach_row(company="", website="")
        mark_history(self.history, incomplete, "FAILED")
        saved = read_csv(self.history)[1][0]

        self.assertEqual(saved["company_name"], "Acme")
        self.assertEqual(saved["website"], "https://acme.test/")
        self.assertEqual(saved["notes"], "keep")
        self.assertEqual(saved["response_status"], "REPLIED")
        self.assertEqual(saved["send_status"], "SENT")


class ImportSentTests(QueueFixture):
    def write_import(self, rows, fields=("email",)):
        path = self.root / "already_sent.csv"
        atomic_write_csv(path, fields, rows)
        return path

    def import_rows(self, rows, fields=("email",)):
        return import_sent_and_rebuild(
            self.write_import(rows, fields),
            self.input,
            self.history,
            self.output,
        )

    def test_known_email_is_matched_case_insensitively_and_excluded(self):
        self.write_outreach([outreach_row(email="Contact@Acme.Test")])
        summary = self.import_rows([{"email": "CONTACT@ACME.TEST"}])
        history = read_csv(self.history)[1]

        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["unknown"], 0)
        self.assertEqual(summary["queue_before"], 1)
        self.assertEqual(summary["queue_after"], 0)
        self.assertEqual(history[0]["send_status"], "SENT")
        self.assertEqual(history[0]["company_name"], "Acme")
        self.assertEqual(history[0]["normalized_domain"], "acme.test")

    def test_imported_domain_excludes_alternate_company_email(self):
        self.write_outreach([
            outreach_row(email="contact@acme.test"),
            outreach_row(email="sales@acme.test", website="https://www.acme.test/about"),
        ])
        self.import_rows([{"email": "contact@acme.test"}])
        self.assertEqual(self.queued_rows(), [])

    def test_unknown_email_and_optional_fields_are_preserved(self):
        self.write_outreach([])
        summary = self.import_rows([{
            "email": "past@unknown.test",
            "company_name": "Unknown Historical Co",
            "website": "https://unknown.test/about",
            "sent_at": "2025-04-10T09:30:00+00:00",
            "notes": "Imported archive",
        }], fields=("email", "company_name", "website", "sent_at", "notes"))
        history = read_csv(self.history)[1]

        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(history[0]["email"], "past@unknown.test")
        self.assertEqual(history[0]["company_name"], "Unknown Historical Co")
        self.assertEqual(history[0]["normalized_domain"], "unknown.test")
        self.assertEqual(history[0]["sent_at"], "2025-04-10T09:30:00+00:00")
        self.assertEqual(history[0]["notes"], "Imported archive")

    def test_blank_sent_at_does_not_invent_historical_time(self):
        self.write_outreach([])
        self.import_rows([{"email": "past@unknown.test"}])
        self.assertEqual(read_csv(self.history)[1][0]["sent_at"], "")

    def test_existing_response_and_timestamp_are_preserved(self):
        self.write_outreach([outreach_row()])
        existing = history_row(
            response_status="REPLIED",
            sent_at="2025-01-02T03:04:05+00:00",
            notes="Original note",
        )
        self.write_history([existing])
        self.import_rows([{
            "email": "contact@acme.test",
            "sent_at": "2026-08-25T12:00:00+00:00",
        }], fields=("email", "sent_at"))
        saved = read_csv(self.history)[1][0]

        self.assertEqual(saved["response_status"], "REPLIED")
        self.assertEqual(saved["sent_at"], "2025-01-02T03:04:05+00:00")
        self.assertEqual(saved["notes"], "Original note")

    def test_duplicate_import_is_idempotent(self):
        self.write_outreach([outreach_row()])
        imported = [{"email": "contact@acme.test"}]
        first_summary = self.import_rows(imported)
        first_history = self.history.read_bytes()
        first_queue = self.output.read_bytes()
        second_summary = self.import_rows(imported)

        self.assertEqual(first_summary["new_sent_records"], 1)
        self.assertEqual(second_summary["new_sent_records"], 0)
        self.assertEqual(second_summary["already_in_history"], 1)
        self.assertEqual(len(read_csv(self.history)[1]), 1)
        self.assertEqual(self.history.read_bytes(), first_history)
        self.assertEqual(self.output.read_bytes(), first_queue)

    def test_distinct_imported_emails_on_same_domain_are_both_preserved(self):
        self.write_outreach([
            outreach_row(email="contact@acme.test"),
            outreach_row(email="sales@acme.test"),
        ])
        self.import_rows([
            {"email": "contact@acme.test"},
            {"email": "sales@acme.test"},
        ])
        saved_emails = {
            row["email"] for row in read_csv(self.history)[1]
        }
        self.assertEqual(
            saved_emails,
            {"contact@acme.test", "sales@acme.test"},
        )


if __name__ == "__main__":
    main()
