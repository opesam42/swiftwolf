# Seed a customer baseline from a statement CSV

`seed-profile` is an offline CLI job. It reads a customer's historical debits, inserts them into `transactions`, then runs the **settlement** path so SwiftWolf learns that customer's normal destinations, banks, amounts, and hours.

It does **not** call `/v1/score`. Historical rows are not fraud decisions.

After the job, live scoring (`POST /v1/score`) uses the warmed baseline. Repeat payees should no longer look new.

---

## Prerequisites

- Same setup as the API: PostgreSQL, Redis, a filled `.env` (`DATABASE_URL`, `REDIS_URL`, `SW_HMAC_KEY`)
- Schema applied (`alembic upgrade head` or `python -m src.cli init-db`)
- Run from the repo root, with the virtualenv activated

`SW_HMAC_KEY` must be the **same key** the API uses. Destination keys are HMAC'd; a different key makes every seeded payee look new on the next live score.

---

## CSV format

Required columns (header names are case-insensitive):

| Column | Meaning |
|---|---|
| `date` | `YYYY-MM-DD` |
| `time` | `HH:MM:SS` or `HH:MM` |
| `amount_kobo` | Positive whole kobo (`300.00` naira → `30000`) |
| `transaction_id` | Statement reference, max 64 characters. The job stores a per-customer `seed-…` id, so the same file can warm more than one customer. |
| `recipient` | Phone, NUBAN, meter number, etc. Max 50 characters |
| `provider` | Network (`MTN`, `GLO`) or bank code (`000012`). Max 30 characters |

You also need **one** of:

- `transaction_type` — `transfer`, `airtime`, `data`, `electricity`, `cable_tv`, `betting`
- `detail` — narration the job maps, for example `Buy Data bundle`, `Top up Airtime`, `Send to …`

Optional: `medium` per row (`app`, `ussd`, or `statement`). If omitted, the CLI default is used (`statement`).

Put raw files in `jobs/files/raw_stmts/` (that folder is gitignored).

Example:

```csv
date,time,amount_kobo,transaction_id,recipient,provider,detail
2026-10-07,06:06:06,30000,63bxvksab01,09057339147,GLO,Buy Data bundle
2026-10-02,17:31:23,9200,133bphyx2800,09057339147,GLO,Top up Airtime
2026-09-29,15:09:24,300000,033bjrea6506,5767940304,000012,Send to A. Y. AMAMA VENTURES
```

Rows with a blank recipient or provider, a non-integer amount, or an unmapped `detail` are skipped. Credits do not belong in this file — the job only stores outgoing payments.

`detail` mapping (first match wins):

| Narration contains | Type |
|---|---|
| `buy data`, `data bundle`, `data purchase` | `data` |
| `top up airtime`, `airtime` | `airtime` |
| `send to`, `transfer`, `nip` | `transfer` |
| `electricity`, `prepaid`, `postpaid` | `electricity` |
| `dstv`, `gotv`, `startimes`, `cable` | `cable_tv` |
| `sporty`, `betting`, `bet9ja` | `betting` |

If you already have a clean `transaction_type` column, prefer that over `detail`.

---

## Run it

Always dry-run first:

```bash
python -m src.cli seed-profile \
  --customer-id cust_123 \
  --csv jobs/files/raw_stmts/stmt.csv \
  --dry-run
```

Check the skip list. Then write:

```bash
python -m src.cli seed-profile \
  --customer-id cust_123 \
  --csv jobs/files/raw_stmts/stmt.csv
```

Each run attributes every row to `--customer-id` (statements do not carry SwiftWolf's id). The same file can be run again for a different customer.

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--customer-id` | (required) | Owner of every row. 1–64 characters |
| `--csv` | (required) | Path to the statement file |
| `--medium` | `statement` | Channel stored when the CSV has no `medium` column |
| `--timezone` | `Africa/Lagos` | Applied to naive `date` + `time` |
| `--dry-run` | off | Parse and report only; no database writes |
| `--limit` | all rows | After sorting, process only the first N valid rows |

`medium=statement` means “imported from a statement; channel unknown.” Live `/v1/score` still only accepts `app` or `ussd`. Do not send `statement` on the score API.

Use `--medium app` only when you know every row was actually an app payment.

---

## What the job does

1. Parses the CSV and sorts **oldest → newest** (Welford and the hour histogram are incremental).
2. Creates the `customers` row if it does not exist.
3. Inserts each new `transactions` row with a per-customer `transaction_reference` (`seed-` plus a 16-char hash of `customer_id:csv_id`), `status=APPROVED`, `medium=statement`, and an HMAC `destination_key` (same helper live scoring uses). The CSV `transaction_id` is not written as-is: it is unique only on PalmPay's statement, not on SwiftWolf's global ledger.
4. Calls `SettleService` with `SUCCESS` so the baseline updates: known destinations, bank codes, per-category amount stats, typical hours, and `is_cold_start` (off after 10 settled rows).
5. Prints a report.

It does **not** write `risk_events` and does **not** touch the Redis velocity window.

### Reruns

Safe to run again with the same file:

- Already-settled rows for **this** `customer_id` are skipped (`skipped existing`). A second customer can reuse the same CSV.
- Category counts are not incremented again
- A row that was inserted but never settled (job died mid-loop) is settled on the next run

There is no `--reset`. Re-seeding does not wipe a customer.

### Report fields

```
Seed complete.
  parsed:            9
  inserted:          9
  settled:           9
  skipped existing:  0
  skipped invalid:   1
  is_cold_start:     True
  known destinations:4
  category counts:   airtime=1, data=5, transfer=3
  skipped rows:
    line 11 (63bikyi67006): missing recipient
```

`is_cold_start` stays `True` until that customer has at least 10 settled transactions (`COLD_START_MIN_SETTLED_TRANSACTIONS`).

---

## Check the result

With the API running and the same `X-SwiftWolf-Key`:

```bash
curl -s -H "X-SwiftWolf-Key: $SWIFTWOLF_API_KEY" \
  http://localhost:8000/v1/customers/cust_123/baseline
```

Or score a payee that was in the CSV. You should not see `new_beneficiary` for that destination.

---

## Common failures

| Symptom | Likely cause |
|---|---|
| `CSV is missing required columns` | Header names don't match (`amount` instead of `amount_kobo`, etc.) |
| `CSV must include transaction_type or detail` | No way to classify the row |
| Row skipped: `could not resolve transaction_type` | Narration didn't match; add a `transaction_type` column |
| Row skipped: `missing recipient` / `missing provider` | Incomplete statement line — expected |
| Live score still returns `new_beneficiary` after seed | Different `SW_HMAC_KEY`, different `customer_id`, or `provider`/`recipient` format doesn't match the live payload (`0803…` vs `+234803…`) |
| `is_cold_start: True` after a small file | Need 10+ settled rows for that customer |
