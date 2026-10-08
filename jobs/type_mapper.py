from src.settlement.models import TransactionType

# First matching needle wins. Needles are lowercase substrings of `detail`.
_DETAIL_RULES: tuple[tuple[tuple[str, ...], TransactionType], ...] = (
    (("buy data", "data bundle", "data purchase"), TransactionType.DATA),
    (("top up airtime", "airtime"), TransactionType.AIRTIME),
    (("send to", "transfer", "nip"), TransactionType.TRANSFER),
    (("electricity", "prepaid", "postpaid"), TransactionType.ELECTRICITY),
    (("dstv", "gotv", "startimes", "cable"), TransactionType.CABLE_TV),
    (("sporty", "betting", "bet9ja"), TransactionType.BETTING),
)


def resolve_transaction_type(
    *,
    transaction_type: str | None,
    detail: str | None,
) -> TransactionType | None:
    """Resolve a CSV row to a TransactionType, or None if it cannot be mapped.

    An explicit `transaction_type` column wins. Otherwise `detail` is matched
    with conservative substring rules so a wrong category cannot poison Welford.
    """
    if transaction_type and transaction_type.strip():
        try:
            return TransactionType(transaction_type.strip().lower())
        except ValueError:
            return None

    if not detail or not detail.strip():
        return None

    text = " ".join(detail.strip().lower().split())
    for needles, mapped in _DETAIL_RULES:
        if any(needle in text for needle in needles):
            return mapped
    return None
