## Decision: Continuous Risk Scoring via Sigmoid Curve

**Date:** September 2026
**Status:** Implemented in `src/scoring/services.py`

---

## 1. What was the problem?

Legacy scoring used rigid binary checks:

```python
if z_score > 2:
    score += 25
```

This created "score cliffs" — treating a minor budget stretch the same as a complete account drain, since any deviation past the threshold received the identical flat penalty regardless of how extreme it actually was.

---

## 2. What is the solution?

We replaced binary checks with a smooth Sigmoid Transfer Function:

$$R(Z) = \frac{R_{\text{max}}}{1 + e^{-k(Z - Z_0)}}$$

Risk points now scale continuously with the severity of the deviation, instead of jumping abruptly at a single fixed threshold.

---

## 3. What do the configuration parameters mean?

| Parameter | Default | Meaning |
|---|---|---|
| `AMOUNT_SCORE_MAX` | `50` | The absolute maximum risk points allowed for amount anomalies. |
| `AMOUNT_SCORE_MIDPOINT` | `3.0` | The Z-score deviation at which 50% of max points (25 pts) are awarded. |
| `AMOUNT_SCORE_STEEPNESS` | `1.5` | How aggressively the score ramps up around the midpoint. |

---

## 4. How do I test or tweak this?

Modify `AMOUNT_SCORE_STEEPNESS` or `AMOUNT_SCORE_MIDPOINT` in `.env` to adjust sensitivity without editing Python code.

---

## 2. Decision: Atomic Redis Sliding-Window Velocity Counter

**Status:** Implemented in `VelocityWindow` (`src/scoring/services.py`)
**Affected Modules:** `VelocityWindow`, `ScoreService`, Redis

### Problem

Exponential decay models handle slow, permanent lifestyle drift, but fail against rapid account-takeover (ATO) burst attacks where 5+ transfers occur within 2 minutes.

### Solution

We implemented a 10-minute sliding-window rate limiter using a **Redis Sorted Set (ZSET)** keyed as `velocity:{customer_id}`, where transaction timestamps act as scores and transaction references act as members.

### Execution Pipeline (<1ms Overhead)

A single atomic Redis pipeline executes four operations per request:

1. `ZREMRANGEBYSCORE velocity:{id} -inf (now - window_seconds)` — prune expired timestamps
2. `ZADD velocity:{id} now tx_reference` — record the current transaction
3. `ZCARD velocity:{id}` — count active transactions in the sliding window
4. `EXPIRE velocity:{id} (window_seconds + 60)` — auto-clean up the key via TTL

### Parameter Definitions

| Parameter | Default | Meaning |
|---|---|---|
| `VELOCITY_WINDOW_SECONDS` | `600` | 10-minute sliding window. |
| `VELOCITY_MAX_THRESHOLD` | `5` | Transaction count in the window at which an anomaly is triggered. |
| `VELOCITY_SCORE_PENALTY` | `50` | Risk points added when the velocity threshold is reached. |

### Trade-offs

- **Idempotency:** Retried transactions with the same `tx_reference` update their timestamp score in the ZSET rather than inflating `tx_count`.
- **Fail-open:** If Redis is unavailable (`None` or a `RedisError`), the check returns a neutral result (`tx_count=1`, no penalty) so scoring still completes.

---

## 4. Decision: Multi-Category Composite Destination Keys (`known_destinations`)

**Status:** Implemented in `CustomerProfileService` (`src/profile/services.py`)
**Affected Modules:** `CustomerProfile`, `RuleEngine`, `ScoreService`, `Settlement`

### Problem

Legacy scoring tracked customer recipients using flat lists of 10-digit NUBAN account numbers (`known_beneficiaries`). This created two major flaws:

1. **Single-Category Bias:** Assumed all banking transactions were NUBAN transfers, breaking on utility bills (meter numbers), betting top-ups (account IDs), and airtime (phone numbers).
2. **Ambiguity & Collisions:** An airtime phone number (`08031234567`) could falsely match a NUBAN account number (`08031234567`).

### Solution

We replaced flat lists with a unified, category-aware **Composite String Key Set** (`known_destinations: set[str]`):

- **Transfers:** `transfer:{bank_code}:{account_number}` (e.g., `transfer:058:0123456789`)
- **Airtime / Data:** `airtime:{network}:{phone_number}` (e.g., `airtime:MTN:08031234567`)
- **Electricity / Bills:** `electricity:{disco}:{meter_number}` (e.g., `electricity:EKEDC:44001234567`)
- **Betting:** `betting:{platform}:{account_ref}` (e.g., `betting:sportybet:99887766`)

### Performance & Architectural Rationale

- **$O(1)$ Hash Set Speed:** On the hot path (<50ms SLA), Python evaluates `target_key in customer_profile.known_destinations` in **~0.001ms** ($O(1)$ set lookup). This avoids $O(N)$ linear scans over nested JSON objects (*Designing Data-Intensive Applications*, Ch. 2).
- **Deep Module Abstraction:** The key-formatting rules live exclusively on `CustomerProfileService.build_destination_key()`. Neither `RuleEngine` nor `Settlement` needs to know how the composite string is structured (*A Philosophy of Software Design*, Ch. 5).
