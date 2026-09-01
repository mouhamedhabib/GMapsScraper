"""Export leads first added on one Africa/Tunis calendar date."""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

try:
    from utils.discovery_timestamps import LOCAL_TIMEZONE, parse_added_at
except ModuleNotFoundError:
    from discovery_timestamps import LOCAL_TIMEZONE, parse_added_at


DEFAULT_INPUT = Path("./CSV_FILES/leads_master.csv")
DEFAULT_OUTPUT_FOLDER = Path("./CSV_FILES/daily_exports")


def read_leads(path):
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        reader = DictReader(file_handler)
        return list(reader.fieldnames or ()), list(reader)


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as file_handler:
        writer = DictWriter(file_handler, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(path, fieldnames, rows):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Leads"
    worksheet.append(list(fieldnames))
    for row in rows:
        worksheet.append([row.get(field, "") for field in fieldnames])
    workbook.save(path)


def export_daily_leads(input_path, requested_date, output_folder=DEFAULT_OUTPUT_FOLDER):
    input_path = Path(input_path)
    output_folder = Path(output_folder)
    fieldnames, rows = read_leads(input_path)
    selected = []
    blank = 0
    invalid = 0
    for row in rows:
        value = str(row.get("added_at") or "").strip()
        if not value:
            blank += 1
            continue
        timestamp = parse_added_at(value)
        if timestamp is None:
            invalid += 1
            continue
        if timestamp.astimezone(LOCAL_TIMEZONE).date() == requested_date:
            selected.append(row)

    output_folder.mkdir(parents=True, exist_ok=True)
    suffix = requested_date.strftime("%d-%m-%Y")
    csv_path = output_folder / f"leads_{suffix}.csv"
    xlsx_path = output_folder / f"leads_{suffix}.xlsx"
    write_csv(csv_path, fieldnames, selected)
    write_xlsx(xlsx_path, fieldnames, selected)
    return {
        "date": requested_date,
        "input": len(rows),
        "exported": len(selected),
        "blank": blank,
        "invalid": invalid,
        "csv": csv_path,
        "xlsx": xlsx_path,
    }


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-folder", type=Path, default=DEFAULT_OUTPUT_FOLDER)
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    requested_date = arguments.date or datetime.now(LOCAL_TIMEZONE).date()
    if not arguments.input.is_file():
        raise SystemExit(f"Input CSV not found: {arguments.input}")
    summary = export_daily_leads(
        arguments.input, requested_date, arguments.output_folder,
    )
    print(f"Export date: {summary['date'].strftime('%d/%m/%Y')}")
    print(f"Input leads: {summary['input']}")
    print(f"Leads added that day: {summary['exported']}")
    print(f"Blank added_at skipped: {summary['blank']}")
    print(f"Invalid added_at skipped: {summary['invalid']}")
    print(f"CSV: {summary['csv']}")
    print(f"Excel: {summary['xlsx']}")


if __name__ == "__main__":
    main()
