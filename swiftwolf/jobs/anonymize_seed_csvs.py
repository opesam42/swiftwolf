"""
jobs/anonymize_seed_csvs.py — anonymizes beneficiary_name IN PLACE across a
dataset's swiftwolf_seed.csv and bankapp_seed.csv, once, at rest — replacing
the two runtime call sites (seed_customer_profile.py, OnboardingService
.get_bankapp_seed_rows) that used to compute this on every read.

Why here instead of at read time: both CSVs already share the same
(beneficiary_account, beneficiary_bank_code) columns for the same
beneficiary, so anonymize_name() being a pure function of those two columns
already guarantees the two files agree — running it once here and writing
the result back means every future consumer of these CSVs gets already-clean
data for free, with no risk of a new call site forgetting to anonymize.

Usage:
    python jobs/anonymize_seed_csvs.py jobs/files/csv/gbenga_palmpay_stmt_swiftwolf_seed.csv jobs/files/csv/gbenga_palmpay_stmt_bankapp_seed.csv
"""
import csv
import sys
from pathlib import Path

# Allow running this script directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.anonymize import anonymize_name


def anonymize_csv_in_place(csv_path: str) -> int:
    path = Path(csv_path)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    changed = 0
    for row in rows:
        new_name = anonymize_name(row["beneficiary_account"], row["beneficiary_bank_code"])
        if row.get("beneficiary_name") != new_name:
            changed += 1
        row["beneficiary_name"] = new_name

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return changed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python jobs/anonymize_seed_csvs.py <csv_path> [<csv_path> ...]")
        sys.exit(1)

    for csv_path in sys.argv[1:]:
        changed = anonymize_csv_in_place(csv_path)
        print(f"{csv_path}: {changed} rows anonymized")
