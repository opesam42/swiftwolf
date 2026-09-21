"""Extract bank statement into SwiftWolf and bank app seed CSVs."""
import csv
import hashlib
import re
from datetime import datetime
from pathlib import Path
from jobs.preprocess import preprocess_statement


NOISE_KEYWORDS = [
    "cashbox interest",
    "cashbox auto save",
    "auto deduct",
    "disbursement",
    "repayment",
    "stamp duty",
    "electronic money transfer levy",
    "ussd charge",
]

TELCO_AGGREGATOR_BANK_CODE = "TELCO_AGGREGATOR"
TELCO_AGGREGATOR_BANK_NAME = "Telco Aggregator"

NIGERIAN_BANKS = [
    ("000018", "Union Bank"), 
    ("000013", "GTBank Plc"), 
    ("000014", "Access Bank"),
    ("000015", "Zenith Bank Plc"), 
    ("000016", "First Bank of Nigeria"),
    ("000004", "United Bank for Africa"), 
    ("000017", "Wema Bank"),
    ("000010", "Ecobank Bank"), 
    ("000007", "Fidelity Bank"),
    ("000012", "StanbicIBTC Bank"), 
    ("100004", "Paycom(Opay)"),
    ("100033", "PalmPay Limited"), 
    ("090267", "Kuda Microfinance Bank"),
    ("000001", "Sterling Bank"),
]


def is_noise(detail: str) -> bool:
    """Check if transaction detail matches noise filtering rules."""
    d = detail.lower()
    return any(kw in d for kw in NOISE_KEYWORDS)


def infer_transaction_type(detail: str) -> str:
    """
    Infer transaction type from transaction detail text.
    
    Returns one of: 'data', 'airtime', 'transfer', 'uncategorized'
    """
    d = detail.lower()
    if "buy data bundle" in d:
        return "data"
    if "top up airtime" in d:
        return "airtime"
    if "pos transfer" in d:
        return "transfer"
    if "send to" in d or "received from" in d:
        return "transfer"
    if "interbank transfer" in d:
        return "transfer"
    return "uncategorized"


def extract_beneficiary_name(detail: str) -> str:
    """
    Extract beneficiary name from transaction detail.
    
    Handles patterns like:
    - "Send to X"
    - "Received from X"
    - "Send to POS Transfer X"
    """
    match = re.search(r"(?:Send to|Received from)\s+(.+)", detail, re.IGNORECASE)
    return match.group(1).strip() if match else detail.strip()


def synthetic_account_number(name: str) -> str:
    """
    Generate a deterministic synthetic account number from beneficiary name.
    
    Same name always produces the same account number, enabling
    recurring-detection logic even without real account numbers.
    """
    normalized = name.strip().lower()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    # Extract only digits, pad to 10 digits with zeros if needed
    return "".join(filter(str.isdigit, digest))[:10].ljust(10, "0")


def synthetic_bank(name: str, detail: str) -> tuple:
    """
    Determine bank code and name for beneficiary.
    
    First checks if transaction detail mentions a real Nigerian bank.
    Otherwise generates a synthetic bank code deterministically based on 
    beneficiary name hash, spreading across real Nigerian bank codes.
    Same name always gets the same bank every time.
    
    Args:
        name: Beneficiary name
        detail: Transaction detail text
    
    Returns:
        Tuple of (bank_code, bank_name)
    """
    detail_lower = detail.lower()
    # Check if detail mentions a real bank
    for code, bank_name in NIGERIAN_BANKS:
        if bank_name.split()[0].lower() in detail_lower:
            return code, bank_name
    
    # Otherwise generate deterministically per beneficiary name
    normalized = name.strip().lower()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    index = int(digest, 16) % len(NIGERIAN_BANKS)
    return NIGERIAN_BANKS[index]


def parse_statement_rows(
    raw_rows: list[dict],
    customer_id: str,
    customer_account_number: str = "",
) -> tuple[list[dict], list[dict]]:
    """
    Parse preprocessed statement rows into SwiftWolf and bank app CSVs.
    
    Args:
        raw_rows: List of dicts with keys: date_str, detail, money_in, money_out, txn_id
        customer_id: Customer identifier (e.g. 'cust_gbenga_demo')
        customer_account_number: Account number from statement header
    
    Returns:
        Tuple of (swiftwolf_rows, bankapp_rows)
    """
    swiftwolf_rows = []
    bankapp_rows = []

    for i, row in enumerate(raw_rows):
        detail = row["detail"]
        
        # Apply noise filtering
        if is_noise(detail):
            continue

        # Determine credit/debit
        is_credit = row["money_in"] not in (None, "", "0.00")
        amount = float(row["money_in"] if is_credit else row["money_out"])
        direction = "credit" if is_credit else "debit"

        # Check for Auto Save sweep pairs (noise credits)
        # NOTE: Statement format is newest-first, so Auto Save appears BEFORE
        # the credit in the array. Check i-1, not i+1.
        if is_credit:
            if i - 1 >= 0:
                prev_detail = raw_rows[i - 1]["detail"].lower()
                prev_out = raw_rows[i - 1].get("money_out")
                if (
                    "auto save" in prev_detail
                    and prev_out
                    and float(prev_out) == amount
                ):
                    continue  # Part of sweep pair, skip

        # Parse timestamp
        timestamp = datetime.strptime(
            row["date_str"], "%m/%d/%Y %I:%M:%S %p"
        ).isoformat()

        # Infer transaction type
        txn_type = infer_transaction_type(detail)

        # Extract beneficiary info
        beneficiary_name = extract_beneficiary_name(detail)
        if txn_type in ("data", "airtime", "electricity"):
            # Airtime/data purchases go to a telco billing aggregator, not a
            # real NUBAN at a bank — synthesizing a fake bank account here
            # would misleadingly imply this went to a real beneficiary
            # account (e.g. "this data purchase went to First Bank account
            # 7088979964"), which isn't how these transactions work at all.
            beneficiary_account = None
            beneficiary_bank_code = None
            beneficiary_name = None
        else:
            beneficiary_account = synthetic_account_number(beneficiary_name)
            bank_code, bank_name = synthetic_bank(beneficiary_name, detail)

        # SwiftWolf CSV row
        swiftwolf_rows.append(
            {
                "customer_id": customer_id,
                "timestamp": timestamp,
                "amount": amount,
                "direction": direction,
                "beneficiary_name": beneficiary_name,
                "beneficiary_account": beneficiary_account,
                "beneficiary_bank_code": bank_code,
                "transaction_type": txn_type,
            }
        )

        # Bank app CSV row
        bankapp_rows.append(
            {
                "customer_id": customer_id,
                "timestamp": timestamp,
                "amount": amount,
                "direction": direction,
                "beneficiary_account": beneficiary_account,
                "beneficiary_name": beneficiary_name,
                "beneficiary_bank_name": bank_name,
                "beneficiary_bank_code": bank_code,
                "narration": detail,
                "transaction_type": txn_type,
                "nibss_reference": row["txn_id"],
                "customer_account_number": customer_account_number,
                "account_balance_after": None,
            }
        )

    return swiftwolf_rows, bankapp_rows


def write_csv(rows: list[dict], path: str):
    """Write rows to CSV file."""
    if not rows:
        return
    
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def extract_from_text(
    raw_text: str,
    customer_id: str,
    statement_filename: str = "statement",
    customer_account_number: str = "",
    output_dir: str = "jobs/files/csv",
) -> dict:
    """
    Full extraction pipeline: raw text → preprocessed → parsed → CSVs.
    
    Args:
        raw_text: Raw statement text
        customer_id: Customer identifier
        statement_filename: Original statement filename (without extension) for prefixing output
        customer_account_number: Account number from header
        output_dir: Directory to write output CSVs
    
    Returns:
        Dict with extraction stats: {swiftwolf_count, bankapp_count, paths}
    """
    # Preprocess
    raw_rows = preprocess_statement(raw_text)
    if not raw_rows:
        raise ValueError("No valid transaction rows found in statement")

    # Parse
    swiftwolf_rows, bankapp_rows = parse_statement_rows(
        raw_rows, customer_id, customer_account_number
    )

    # Write CSVs
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    sw_path = output_path / f"{statement_filename}_swiftwolf_seed.csv"
    bank_path = output_path / f"{statement_filename}_bankapp_seed.csv"

    write_csv(swiftwolf_rows, str(sw_path))
    write_csv(bankapp_rows, str(bank_path))

    return {
        "swiftwolf_count": len(swiftwolf_rows),
        "bankapp_count": len(bankapp_rows),
        "swiftwolf_path": str(sw_path),
        "bankapp_path": str(bank_path),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python -m jobs.extract_statement <statement_file> "
            "[customer_id] [account_number]"
        )
        print("\nExample:")
        print("  python -m jobs.extract_statement jobs/files/raw_stmts/gbenga_palmpay_stmt.txt cust_gbenga_demo")
        sys.exit(1)

    statement_file = sys.argv[1]
    customer_id = sys.argv[2] if len(sys.argv) > 2 else "cust_demo"
    account_number = sys.argv[3] if len(sys.argv) > 3 else ""
    
    # Extract filename without extension for prefixing output files
    statement_filename = Path(statement_file).stem

    with open(statement_file, "r", encoding="utf-8") as f:
        raw_text = f.read()

    result = extract_from_text(raw_text, customer_id, statement_filename, account_number)
    print(f"✓ SwiftWolf rows: {result['swiftwolf_count']}")
    print(f"✓ Bank app rows: {result['bankapp_count']}")
    print(f"\nOutput files:")
    print(f"  - {result['swiftwolf_path']}")
    print(f"  - {result['bankapp_path']}")
