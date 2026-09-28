# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SwiftWolf is a FastAPI fraud-scoring middleware that sits between a bank app and NIBSS. It never moves money. It advises `PROCEED` / `STEP_UP_LIGHT` / `STEP_UP_LIVENESS` / `BLOCK`, and the bank app decides what to do. There are two calls per transaction:

1. `POST /v1/score`: synchronous, on the hot path (<500ms SLA). Pure rules against a Redis-cached baseline.
2. `POST /v1/transactions/settle`: after the transfer settles. Updates the customer's behavioral baseline.

README.md describes the intended design, including River `HalfSpaceTrees`, background tasks and adaptive risk tier. Some of it is not implemented in the current `src/`. Trust the code over the README. `docs/designNotes.md` records the reasons behind recent decisions: sigmoid amount scoring, the Redis velocity window and composite destination keys.

## Commands

```bash
source venv/bin/activate              # Python 3.12, deps in requirements.txt
uvicorn src.main:app --reload         # run the API (needs DATABASE_URL, REDIS_URL, SWIFTWOLF_API_KEY in .env)
python -m src.cli init-db             # create tables (SQLModel.metadata.create_all)
python -m src.cli seed-blacklist --account 0123456789 --bank-code 058
python -m src.cli sync-cache          # rebuild Redis blacklist set from Postgres

pytest                                # full suite
pytest tests/test_scoring.py          # one file
pytest tests/test_scoring.py::test_score_idempotency   # one test
```

Tests need **Node/npm** on PATH. `py-pglite` boots a real in-process Postgres (PGlite) and caches its npm install in `.pglite/`. Redis is `fakeredis`. No external Postgres or Redis is needed for tests. The first run is slow while npm installs. If a full `pytest` run seems to hang, run the files one at a time to find the culprit.

`run.py` points at `app.main:app`, which is wrong. Use `uvicorn src.main:app` instead.

## Architecture

Each domain package is laid out as `models.py`, `schemas.py`, `repository.py`, `services.py` and `router.py`. `src/main.py` wires the routers together. On startup, `lifespan` runs `db_init()` and warms the Redis blacklist cache.

| Package | Role |
|---|---|
| `src/scoring` | `ScoreService` orchestrates scoring. `RuleEngine` holds the additive rules, sigmoid amount score and decision thresholds. `VelocityWindow` is a Redis ZSET sliding window. `RiskEvent` model. |
| `src/settlement` | `SettleService`, `TransactionRepository`, `Transaction` model and `TransactionType`/`TransactionStatus` enums. |
| `src/profile` | `Customer` model (the behavioral baseline lives in JSON columns), `CustomerRepository` (Postgres + Redis baseline cache), `CustomerProfileService` (Welford baseline updates, destination keys), `cache_sync.py` (session hooks). |
| `src/blacklist` | `BlacklistedAccount`, `BlacklistRepository` (Postgres + Redis set), thin `BlacklistService`. |
| `src/admin` | `/v1/internal/*` routes behind the `X-SwiftWolf-Admin-Key` header. |
| `src/core` | `config.py` (pydantic-settings; all tunables such as sigmoid, velocity and cold-start live here), `database.py` (engine, `SessionDep`, imports every model so metadata is complete), `redis.py` (`RedisDep`, a cached single client), `auth.py` (`X-SwiftWolf-Key`, fails closed if unset), `errors.py` (domain exceptions). |

### Score flow (`ScoreService.score`)
1. Idempotency: an existing `RiskEvent` for the `transaction_reference` is returned as-is.
2. The baseline comes from Redis `baseline:{customer_id}`, falling back to Postgres. A missing customer row is created, which the FKs require.
3. Velocity check (`velocity:{customer_id}` ZSET), then the blacklist keys are loaded, then `RuleEngine.evaluate`.
4. A blacklisted transfer destination short-circuits to `BLOCK` (score 999) before any other rule runs.
5. Inserts a `Transaction` (status taken from `DECISION_TO_STATUS`) plus a `RiskEvent`. An `IntegrityError` race falls back to the stored decision.

### Settle flow (`SettleService.settle`)
It takes a row lock (`get_for_update`) on the transaction and then branches:
- Unknown reference: `TransactionNotFoundError`, returned as 404.
- Already `is_settled`: no-op.
- `status == "FAILED"`: the row is marked FAILED and the baseline is left alone.
- Otherwise the transaction is marked settled and `CustomerProfileService.update_baseline_from_settled_transaction` runs under a customer row lock. That call updates destinations, bank codes, the hour histogram, location cells and Welford stats per category, and handles the cold-start exit. All of it commits in one transaction.

## Conventions and invariants

- **Money is integer kobo** in the API and the DB (`amount: int`, strict, >0). Baseline stats (`avg_amount`, `std_amount`) are kept in **naira**, so divide by 100 before comparing or updating.
- **Repositories own all DB and Redis access.** Services call repository methods and don't touch SQLModel or Redis directly. The repo's `save()` commits.
- **Baseline cache invalidation is automatic.** The `cache_sync.py` listeners on the SQLAlchemy `Session` class collect changed `Customer` ids at flush time and delete their `baseline:{id}` keys after commit. So:
  - Never write the baseline to Redis after a Postgres write. Only the read path fills the cache (`_fill_cache`).
  - Invalidation only happens if a `CustomerRepository` built with a redis client was attached to that session (`session.info["redis_client"]`). Bulk or raw SQL updates bypass it, and the 24h TTL bounds any staleness.
  - `src/core/database.py` must keep `import src.profile.cache_sync`.
- **The blacklist cache is rebuilt in full** after every `BlacklistRepository.add()`. `blacklist:last_synced` tells an empty blacklist apart from an unsynced cache. Blacklist keys are `"{account}:{bank_code}"` and apply to transfers only.
- **Destination keys** have the form `{category}:{provider}:{recipient}`, for example `transfer:058:0123456789`. AIRTIME and DATA both map to `airtime:`. Always build them with `CustomerProfileService.destination_key_for()` and never format them by hand. The request fields are `provider` (bank code, network or DISCO) and `recipient` (NUBAN, phone or meter number).
- **If Redis fails, keep going.** Velocity comes back neutral, the baseline and blacklist are read from Postgres, and a failed cache invalidation is logged while the Postgres commit stands. Postgres is always the source of truth.
- `TransactionType` enum values are uppercase (`"TRANSFER"`), and so are the keys of `category_baselines`.
- JSON columns only hold plain JSON. Read and write them through the helper methods on `Customer` (`get_/set_category_stats`, `get_/set_hour_histogram`), which reassign the whole value so SQLAlchemy sees the change.

## Testing notes

- `tests/conftest.py` sets placeholder env vars before importing `src`. The `pglite_engine` fixture then rebinds `src.core.database.engine` and `src.main.engine`. Any new module-level `engine` import needs the same rebind.
- `db_session` runs TRUNCATE ... RESTART IDENTITY CASCADE on every table before each test.
- Tests that score or settle through the API need the customer to exist. `/v1/score` creates it, and direct DB tests use the `seed_customer` fixture.
- Useful fixtures: `client`, `auth_headers`, `admin_auth_headers`, `fake_redis`.

## Known stale or broken code

- `src/routes.py` is a leftover from before the split into domain packages. It imports `src.core.schemas` and `src.core.services`, which no longer exist, and nothing includes it. Don't extend it.
- `alembic/env.py` imports `src.core.models`, which doesn't exist, so Alembic is broken until that line is changed to import the domain models, for example via `src.core.database`. The two existing migrations come from the old schema. Right now the app creates its tables with `create_all`.
- The unusual-hour rule in `RuleEngine.evaluate` is commented out (TODO: rewrite it against the hour histogram). `CustomerProfileService.parse_destination_key` is incomplete: it has no `self`/`@staticmethod` and no return value.
- `ADMIN_API_KEY` is not a field on `Settings` (`extra='ignore'`), so admin auth silently falls back to `SWIFTWOLF_API_KEY`.
- The `profile` router's endpoints are all commented out.
