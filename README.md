# SwiftWolf

A real-time fraud detection middleware for bank transfers — sitting between a bank's app and NIBSS, scoring every transaction in milliseconds and learning each customer's behavior continuously in the background.

SwiftWolf never touches money, never talks to the customer, and never sees biometric data. It advises: `PROCEED`, step up verification, or `BLOCK` — the bank app decides what to actually do.

---

## 📌 Table of Contents

- [Overview](#-overview)
- [Architecture — Two Layers, Two Jobs](#️-architecture--two-layers-two-jobs)
- [The Reflex Layer](#the-reflex-layer--real-time-rule-engine)
- [The Vigilance Layer](#the-vigilance-layer--behavioral-learning)
- [Security Model](#-security-model)
- [Tech Stack](#-tech-stack)
- [Installation & Setup](#-installation--setup)
- [API Reference](#-api-reference)
- [Known Limitations](#-known-limitations--scoped-out)

---

## 📖 Overview

A bank app calls `POST /v1/score` *before* it calls NIBSS, on every outgoing transfer. SwiftWolf reads that customer's cached behavioral baseline, runs it through a deterministic scoring engine, and returns a decision in real time — fast enough that it never becomes the bottleneck in the transfer flow.

After the transfer settles, a second call (`POST /v1/transactions/settle`) feeds the outcome back in. That's where the slower work happens: updating the customer's behavioral baseline and running it through an online anomaly model — asynchronously, so it never touches the hot path.

Two layers, two different jobs, two different latency budgets. That split is the core architectural decision behind everything else in this repo.

---

## ⚙️ Architecture — Two Layers, Two Jobs

```
Bank App                                    SwiftWolf
────────                                    ─────────
                    POST /v1/score
   ──────────────────────────────────────▶  Reflex Layer
                                             (sync, <500ms, pure rules)
   ◀──────────────────────────────────────  { score, decision, reasons }

   (transfer proceeds / step-up happens)

              POST /v1/transactions/settle
   ──────────────────────────────────────▶  responds immediately
                                             │
                                             ▼
                                        Vigilance Layer
                                        (async, background task)
                                        learns from this transaction
```

---

### The Reflex Layer — real-time rule engine

**Job:** answer "does this look like this customer?" in under 500ms, with zero ML inference and zero database calls on the hot path.

It's pure, deterministic arithmetic against a Redis-cached behavioral baseline — additive point scoring across a fixed set of signals:

| Signal | Weight |
|---|---|
| New beneficiary (never paid before) | +30 |
| Unfamiliar bank | +15 |
| Amount deviates >2σ from this customer's category baseline | +25 |
| Unusual hour for this customer | +15 |
| Bot-speed timing (<2s from login to transfer) | +40 |
| Active call detected during transfer | +50 |
| Pasted beneficiary + genuinely new beneficiary | +10 |
| Dormant account suddenly active + amount deviation | +20 |
| Location far outside this customer's known area | +10 to +20 |
| Elevated risk tier (see Vigilance Layer) | +15 |

Additive scoring is then mapped to a decision:

| Score | Decision |
|---|---|
| ≤ 30 | `PROCEED` |
| 31–100 | `STEP_UP` (the bank app chooses the verification: OTP, BVN liveness, security question, etc.) |
| > 100 | `BLOCK` |

**One override sits above all of this:** a blacklisted destination account short-circuits straight to `BLOCK`, with no step-up offered at all — regardless of every other signal. This is deliberate. Step-up verification proves the *sender* is who they claim to be; it says nothing about whether the *destination* is legitimate. A blacklisted account is a confirmed-bad destination, and a real customer being socially engineered would pass their own liveness check perfectly while the money still lands in a known-bad account. That gap is closed by making the blacklist an unconditional override, not an additive weight.

A fresh customer with no real history yet gets a cold-start dampener rather than being treated as inherently suspicious just because their baseline is thin.

No ML model runs anywhere in this layer — that's a named architectural decision, not a gap. Running inference synchronously on every transaction would risk blowing the latency budget; it belongs entirely in the Vigilance Layer instead.

---

### The Vigilance Layer — behavioral learning

**Job:** turn every settled transaction into a slightly better understanding of what "normal" looks like for that customer — without ever touching the request the bank app is waiting on.

Runs as a background task after `POST /v1/transactions/settle` returns:

- **Behavioral baseline** — per-category running mean/variance of transaction amounts, an hour-of-day histogram, known beneficiaries, known banks, and known geographic areas. All built incrementally (Welford's algorithm), so it updates in O(1) per transaction with no need to replay history.
- **Online anomaly detection** — a population-level model (River's `HalfSpaceTrees`) scores each settled transaction against cross-customer patterns the per-customer rule engine can't see on its own. It self-filters: anything it already suspects as anomalous is scored, but deliberately *not* learned from — so a single outlier can't poison its own definition of normal.
- **Adaptive risk tier** — if a customer's step-up verification comes back *failed* (not abandoned — a rejected identity claim, specifically), they're flagged internally. Their next transaction gets scrutinized harder by the Reflex Layer until the tier resets. This is a deliberately simple, single-model stand-in for a full champion/challenger fraud-relearning pipeline — cheap to build, and it directly targets the real risk (a rejected identity claim should raise suspicion on what comes next) without needing a second model.
- **Only learns from transactions that actually completed.** A failed or abandoned transfer never moved real money — feeding it into the baseline would teach the model to expect money movement that never happened.

Everything here runs *after* the bank app has already moved on — this layer can afford to be thorough because nothing is waiting on it.

---

## 🔒 Security Model

- **Idempotency by design, not convention.** Every score and settle call is keyed on a `transaction_reference` the bank app generates *before* calling NIBSS. A retried call — network hiccup, timeout, accidental double-send — returns the exact same decision instead of re-scoring, and a settle retry is a safe no-op rather than double-counting a transaction into the customer's baseline.
- **Blacklist checks are destination-based, not identity-based.** SwiftWolf never performs a live BVN lookup against a beneficiary — that's not just a licensing gap, it's structurally not permitted under current CBN/NIBSS consent regulation, since a beneficiary can't grant real-time consent to a check on someone else's transfer. Blacklist checks run against beneficiary account numbers instead — a legitimate operational fraud-data check that doesn't require third-party consent.
- **No biometric data ever reaches SwiftWolf.** Liveness capture, face matching, and the pass/fail decision all happen entirely inside the bank app. SwiftWolf only ever sees the *outcome* (`passed`, `liveness_failed`, etc.) after the fact — never a photo, never a match score.
- **No direct line to the customer.** SwiftWolf has no customer-facing channel at all — no push notification, no SMS, no UI. It advises the bank app; the bank app acts.
- **Every request is authenticated.** A shared API key (`X-SwiftWolf-Key` header) is required and validated on every endpoint. An unconfigured key on the server fails *closed* — every request gets rejected — rather than silently accepting an unauthenticated call.

---

## 🛠 Tech Stack

- **API framework:** FastAPI (Python)
- **Online learning:** [River](https://riverml.xyz/) — incremental statistics (Welford's algorithm) for behavioral baselines, `HalfSpaceTrees` for anomaly detection
- **Database:** PostgreSQL (source of truth), SQLModel/SQLAlchemy, Alembic for migrations
- **Cache:** Redis — a pure hot-path cache in front of Postgres (never the source of truth for anything)
- **Validation:** Pydantic v2

---

## 🚀 Installation & Setup

### Prerequisites
- Python 3.12+
- PostgreSQL
- Redis

### Local Development

```bash
git clone <this-repo>
cd swiftwolf

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Configure environment variables:

```bash
cp .env.example .env
```

```
DATABASE_URL=postgresql://user:password@host:port/dbname
REDIS_URL=redis://host:port
SWIFTWOLF_API_KEY=<a shared secret with the bank app>
```

Run database migrations:

```bash
alembic upgrade head
```

Start the API:

```bash
uvicorn src.main:app --reload
```

---

## 🔌 API Reference

### `POST /v1/score`

Called synchronously, before the bank app calls NIBSS.

**Request:**
```json
{
  "transaction_reference": "txn_ref_a1b2c3",
  "customer_id": "cust_123",
  "beneficiary_account": "0123456789",
  "beneficiary_bank_code": "000013",
  "amount": 200000.00,
  "timestamp": "2026-07-11T02:17:00Z",
  "last_transaction_timestamp": "2026-07-10T02:17:00Z",
  "transaction_type": "transfer",
  "medium": "app",
  "geolocation": { "lat": 6.5244, "lng": 3.3792 },
  "session": {
    "login_to_transfer_seconds": 1.4,
    "pasted_beneficiary": true
  }
}
```

**Response:**
```json
{
  "transaction_reference": "txn_ref_a1b2c3",
  "score": 135,
  "decision": "BLOCK",
  "reasons": ["new_beneficiary", "new_bank", "bot_speed_timing", "pasted_new_beneficiary"]
}
```

`decision` is one of `PROCEED`, `STEP_UP` or `BLOCK`. On `STEP_UP`, SwiftWolf does not pick the verification method — the bank app chooses it for its channel (e.g. OTP, BVN liveness in the app, a security question on USSD).

### `POST /v1/transactions/settle`

Fire-and-forget, called after NIBSS confirms settlement and any step-up finishes.

```json
{
  "transaction_reference": "txn_ref_a1b2c3",
  "customer_id": "cust_123",
  "final_status": "completed",
  "verification_outcome": "passed",
  "amount": 200000.00,
  "beneficiary_account": "0123456789",
  "timestamp": "2026-07-11T02:19:00Z",
  "nibss_reference": "nip_ref_889271"
}
```

Returns immediately with `{ "status": "accepted" }` (or `"already_processed"` on a retry) — the actual behavioral update runs afterward, in the background.

**Auth:** every request on every endpoint requires an `X-SwiftWolf-Key` header.

---

## 📋 Known Limitations — Scoped Out

Stated explicitly rather than silently omitted:

- **Blacklist checks are account-level only**, per current CBN/NIBSS BVN consent regulation — not a scope limitation that resolves with a license; a structural one.
- **The anomaly model (`HalfSpaceTrees`) is fully online** — no retrain step exists, but it's explicitly weaker against anomalies clustered together in time than a batch-retrained model would be. A named, accepted tradeoff for the freshness it buys.
- **Risk tier never resets automatically** once elevated — a production version would reset it after a run of clean transactions; this version states the gap rather than pretending it's handled.
- **No champion/challenger model rebasing.** A full fraud-relearning pipeline (a clean model that never learns from confirmed fraud, periodically reconciled with the live model) was considered and scoped out in favor of the simpler risk-tier mechanism above.
- **A bloom filter pre-check for blacklist lookups** would matter at real bank scale (millions of accounts, every transaction checked); at current scale, a direct lookup already comfortably fits the latency budget.


