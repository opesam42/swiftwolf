
"""Preprocess raw bank statement text into clean transaction rows."""
import re
import json
from pathlib import Path

DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\s+[AP]M")
AMOUNT_ID_PATTERN = re.compile(
    r"^(.*?)\s*([+-])\s*([\d,]+\.\d{2})\s+(\S+)\s*$", re.DOTALL
)
PAGE_NUMBER_LINE = re.compile(r"^\d{1,3}$")

def preprocess_statement(raw_text: str) -> list[dict]:
    """
    Convert raw bank statement text into clean transaction rows.
    
    Handles:
    - Removing header lines before transaction table starts
    - Removing stray page-number lines
    - Reconstructing names that wrap across multiple lines
    - Extracting date, detail, money_in, money_out, and transaction ID
    
    Args:
        raw_text: Raw statement text (pasted or from file)
    
    Returns:
        List of transaction dicts with keys: date_str, detail, money_in, money_out, txn_id
    """
    lines = raw_text.splitlines()

    # Drop header lines before the transaction table starts, and any stray
    # lone-digit page-number lines anywhere in the body.
    body_lines = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if DATE_PATTERN.match(stripped):
                started = True
            else:
                continue
        if PAGE_NUMBER_LINE.match(stripped):
            continue
        if stripped:
            body_lines.append(stripped)

    # Group lines into blocks: each starts at a line matching DATE_PATTERN and
    # continues until the next date line — this handles names that wrap across
    # two lines (e.g. "Send to UMAR BULBULI" / "MUHAMMAD-600.00 ...").
    blocks = []
    current = []
    for line in body_lines:
        if DATE_PATTERN.match(line) and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    rows = []
    for block in blocks:
        full_text = " ".join(block)
        date_match = DATE_PATTERN.match(full_text)
        if not date_match:
            continue
        
        date_str = date_match.group(0)
        rest = full_text[len(date_str):].strip()

        amount_match = AMOUNT_ID_PATTERN.match(rest)
        if not amount_match:
            # Log in production for debugging
            continue

        detail, sign, amount_str, txn_id = amount_match.groups()
        amount = float(amount_str.replace(",", ""))
        is_credit = sign == "+"

        rows.append({
            "date_str": date_str,
            "detail": detail.strip(),
            "money_in": amount if is_credit else None,
            "money_out": None if is_credit else amount,
            "txn_id": txn_id,
        })

    return rows


if __name__ == "__main__":
    
# How to run this file 
# python -m jobs.preprocess jobs/files/raw_stmts/gbenga_palmpay_stmt.txt

    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m jobs.preprocess <statement_file>")
        sys.exit(1)

    statement_file = sys.argv[1]
    filename_without_ext = Path(statement_file).stem

    try:
        with open(statement_file, "r", encoding="utf-8") as f:
            raw_text = f.read()
        
        rows = preprocess_statement(raw_text)
        
        # Prepare output directory
        output_dir = Path("jobs/files/processed_stmt")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Save to JSON
        output_file = output_dir / f"{filename_without_ext}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        
        # Print JSON to stdout
        # print(json.dumps(rows, indent=2))
        print(f"\n✓ Saved to: {output_file}")
    
    except FileNotFoundError:
        print(f"✗ Error: File not found: {statement_file}")
        sys.exit(1)
