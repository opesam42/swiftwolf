import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobs.type_mapper import resolve_transaction_type
from src.settlement.models import TransactionChannel, TransactionType

REQUIRED_COLUMNS = (
    "date",
    "time",
    "amount_kobo",
    "transaction_id",
    "recipient",
    "provider",
)
TYPE_COLUMNS = ("transaction_type", "detail")
MAX_TRANSACTION_ID = 64
MAX_RECIPIENT = 50
MAX_PROVIDER = 30


@dataclass(frozen=True, slots=True)
class NormalizedRow:
    transaction_id: str
    amount_kobo: int
    occurred_at: datetime
    recipient: str
    provider: str
    transaction_type: TransactionType
    medium: str
    source_line: int


@dataclass(frozen=True, slots=True)
class SkippedRow:
    source_line: int
    reason: str
    transaction_id: str | None


@dataclass(frozen=True, slots=True)
class ParseResult:
    rows: list[NormalizedRow]
    skipped: list[SkippedRow]


def parse_statement_csv(
    csv_path: str | Path,
    *,
    default_medium: str = TransactionChannel.STATEMENT.value,
    timezone_name: str = "Africa/Lagos",
) -> ParseResult:
    """Turn a statement CSV into normalized debit rows, skipping bad lines."""
    _validate_medium(default_medium)
    tz = _load_timezone(timezone_name)
    path = Path(csv_path)

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")

        header_map = _header_map(reader.fieldnames)
        missing = [col for col in REQUIRED_COLUMNS if col not in header_map]
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
        if not any(col in header_map for col in TYPE_COLUMNS):
            raise ValueError("CSV must include transaction_type or detail")

        rows: list[NormalizedRow] = []
        skipped: list[SkippedRow] = []

        for raw in reader:
            source_line = reader.line_num
            transaction_id = _cell(raw, header_map, "transaction_id")
            try:
                rows.append(
                    _normalize_row(
                        raw,
                        header_map,
                        default_medium=default_medium,
                        tz=tz,
                        source_line=source_line,
                    )
                )
            except ValueError as exc:
                skipped.append(
                    SkippedRow(
                        source_line=source_line,
                        reason=str(exc),
                        transaction_id=transaction_id or None,
                    )
                )

    rows.sort(key=lambda row: (row.occurred_at, row.transaction_id))
    return ParseResult(rows=rows, skipped=skipped)


def _normalize_row(
    raw: dict[str, str | None],
    header_map: dict[str, str],
    *,
    default_medium: str,
    tz: ZoneInfo,
    source_line: int,
) -> NormalizedRow:
    transaction_id = _require(raw, header_map, "transaction_id")
    if len(transaction_id) > MAX_TRANSACTION_ID:
        raise ValueError(f"transaction_id longer than {MAX_TRANSACTION_ID} characters")

    recipient = _require(raw, header_map, "recipient")
    if len(recipient) > MAX_RECIPIENT:
        raise ValueError(f"recipient longer than {MAX_RECIPIENT} characters")

    provider = _require(raw, header_map, "provider")
    if len(provider) > MAX_PROVIDER:
        raise ValueError(f"provider longer than {MAX_PROVIDER} characters")

    amount_kobo = _parse_amount_kobo(_require(raw, header_map, "amount_kobo"))
    occurred_at = _parse_occurred_at(
        _require(raw, header_map, "date"),
        _require(raw, header_map, "time"),
        tz,
    )
    transaction_type = resolve_transaction_type(
        transaction_type=_cell(raw, header_map, "transaction_type"),
        detail=_cell(raw, header_map, "detail"),
    )
    if transaction_type is None:
        raise ValueError("could not resolve transaction_type from transaction_type or detail")

    medium = _cell(raw, header_map, "medium") or default_medium
    _validate_medium(medium)

    return NormalizedRow(
        transaction_id=transaction_id,
        amount_kobo=amount_kobo,
        occurred_at=occurred_at,
        recipient=recipient,
        provider=provider,
        transaction_type=transaction_type,
        medium=medium,
        source_line=source_line,
    )


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name in fieldnames:
        if name is None or not name.strip():
            continue
        mapping[name.strip().lower()] = name
    return mapping


def _cell(raw: dict[str, str | None], header_map: dict[str, str], column: str) -> str:
    key = header_map.get(column)
    if key is None:
        return ""
    value = raw.get(key)
    return value.strip() if value else ""


def _require(raw: dict[str, str | None], header_map: dict[str, str], column: str) -> str:
    value = _cell(raw, header_map, column)
    if not value:
        raise ValueError(f"missing {column}")
    return value


def _parse_amount_kobo(raw: str) -> int:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("amount_kobo is not a number") from exc
    if value <= 0 or value != value.to_integral_value():
        raise ValueError("amount_kobo must be a positive whole number of kobo")
    return int(value)


def _parse_occurred_at(date: str, time: str, tz: ZoneInfo) -> datetime:
    combined = f"{date} {time}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(combined, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError("date/time must be YYYY-MM-DD and HH:MM[:SS]")


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc


def _validate_medium(medium: str) -> None:
    try:
        TransactionChannel(medium)
    except ValueError as exc:
        allowed = ", ".join(channel.value for channel in TransactionChannel)
        raise ValueError(f"medium must be one of: {allowed}") from exc
