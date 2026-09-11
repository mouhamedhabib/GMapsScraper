"""Validate and atomically publish the canonical final enrichment dataset.

Pipeline:
    leads_enriched.csv -> finalize_enrichment.py -> leads_enriched_final.csv
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter, Error as CsvError
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    from utils.enrich_leads import input_identity
    from utils.enrichment_schema import (
        CANONICAL_ENRICHMENT_FIELDS,
        normalize_enrichment_row,
    )
except ModuleNotFoundError:
    from enrich_leads import input_identity
    from enrichment_schema import (
        CANONICAL_ENRICHMENT_FIELDS,
        normalize_enrichment_row,
    )


DEFAULT_INPUT = Path("./CSV_FILES/leads_enriched.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/leads_enriched_final.csv")
OUTPUT_FIELDS = CANONICAL_ENRICHMENT_FIELDS


class FinalizationError(ValueError):
    """Raised when an enrichment CSV is structurally unsafe to publish."""


def _read_and_normalize(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = DictReader(handle, strict=True)
            header = reader.fieldnames
            if header is None:
                raise FinalizationError(f"Input CSV has no header: {path}")
            if any(not field for field in header) or len(header) != len(set(header)):
                raise FinalizationError(f"Input CSV has an invalid or duplicate header: {path}")
            rows = list(reader)
    except (OSError, UnicodeError, CsvError) as error:
        raise FinalizationError(f"Could not read enrichment CSV {path}: {error}") from error

    if any(None in row for row in rows):
        raise FinalizationError(f"Input CSV rows do not match its header: {path}")

    normalized = [normalize_enrichment_row(row) for row in rows]
    if any(tuple(row) != OUTPUT_FIELDS for row in normalized):
        raise FinalizationError("Canonical enrichment schema normalization failed")
    seen = {}
    for row_index, row in enumerate(normalized):
        identity = input_identity(row, row_index)
        if identity in seen:
            first_row = seen[identity] + 2
            current_row = row_index + 2
            raise FinalizationError(
                f"Duplicate enrichment identity {identity!r} in rows "
                f"{first_row} and {current_row}"
            )
        seen[identity] = row_index
    return normalized


def _write_atomic(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8-sig",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        with temporary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            published_reader = DictReader(handle, strict=True)
            if tuple(published_reader.fieldnames or ()) != OUTPUT_FIELDS:
                raise FinalizationError("Final enrichment schema verification failed")
            if sum(1 for _ in published_reader) != len(rows):
                raise FinalizationError("Final enrichment row-count verification failed")

        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def finalize_enrichment(input_path=DEFAULT_INPUT, output_path=DEFAULT_OUTPUT):
    """Normalize, validate, and atomically publish an enrichment CSV."""
    rows = _read_and_normalize(input_path)
    _write_atomic(output_path, rows)
    return {"input": len(rows), "published": len(rows)}


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    try:
        summary = finalize_enrichment(arguments.input, arguments.output)
    except (FileNotFoundError, FinalizationError) as error:
        raise SystemExit(str(error)) from error
    print(f"Validated enrichment rows: {summary['input']}")
    print(f"Published final enrichment rows: {summary['published']}")
    print(f"Final enrichment file: {arguments.output}")


if __name__ == "__main__":
    main()
