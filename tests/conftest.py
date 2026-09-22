import os

# Placeholder DSN so `Settings` can be constructed at import time. The real
# connection string is only known once PGlite has booted, at which point the
# `pglite_engine` fixture rebinds every module-level engine reference below.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
)
os.environ.setdefault("SWIFTWOLF_API_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")

from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, SQLModel

from py_pglite import PGliteConfig
from py_pglite.sqlalchemy import SQLAlchemyPGliteManager

import src.core.database as core_database
import src.main as main_module
from src.core.config import settings
from src.core.database import get_db_session
from src.core.redis import get_redis_client
from src.main import app
from src.profile.models import Customer

# PGlite ships as an npm package. Pinning a persistent work directory (instead
# of a fresh temp dir per run) means `npm install` happens once on the first
# run and every later run reuses the cached node_modules.
PGLITE_WORK_DIR = Path(__file__).resolve().parent.parent / ".pglite"


@pytest.fixture(scope="session")
def pglite_engine():
    """Boots one real PostgreSQL instance (PGlite) for the whole test session.

    Tests run against actual Postgres semantics — BIGSERIAL identities, JSONB,
    partial unique indexes, timezone-aware timestamps — instead of SQLite
    approximations, so the schema exercised here matches production.
    """
    config = PGliteConfig(work_dir=PGLITE_WORK_DIR)

    # py-pglite bakes the socket path into pglite_manager.js and only writes
    # that file when it is missing. Because the work directory persists, a
    # stale script would point at the previous run's socket and startup would
    # time out waiting for a socket nothing ever creates.
    stale_launcher = PGLITE_WORK_DIR / "pglite_manager.js"
    if stale_launcher.exists():
        stale_launcher.unlink()

    manager = SQLAlchemyPGliteManager(config)
    manager.start()

    # Build the engine before the readiness probe. py-pglite caches one shared
    # engine (PGlite serves a single connection at a time) and wait_for_ready()
    # would otherwise create it with its psycopg3 default; psycopg2 is the
    # driver this project already pins.
    engine = manager.get_engine(driver="psycopg2")

    if not manager.wait_for_ready():
        manager.stop()
        raise RuntimeError("PGlite failed to become ready")

    # Rebind every module-level engine reference. `src.main` imported `engine`
    # by value, so patching only `src.core.database.engine` would leave the
    # lifespan warm-up talking to the unreachable placeholder DSN.
    original_db_engine = core_database.engine
    original_main_engine = main_module.engine
    core_database.engine = engine
    main_module.engine = engine

    SQLModel.metadata.create_all(engine)

    try:
        yield engine
    finally:
        core_database.engine = original_db_engine
        main_module.engine = original_main_engine
        manager.stop()


@pytest.fixture(name="db_session")
def db_session_fixture(pglite_engine):
    """Gives each test a clean schema, then a session bound to PGlite.

    Truncating up front (rather than after) keeps the database inspectable when
    a test fails, and RESTART IDENTITY means autoincrement ids are predictable
    from one test to the next.
    """
    table_names = ", ".join(
        f'"{table.name}"' for table in SQLModel.metadata.sorted_tables
    )
    if table_names:
        with pglite_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

    with Session(pglite_engine) as session:
        yield session


@pytest.fixture(name="seed_customer")
def seed_customer_fixture(db_session: Session):
    """Creates a Customer row so transactions have a valid owner.

    Postgres enforces the transactions.customer_id -> customers.customer_id
    foreign key, so any test that settles or scores a transaction must seed its
    customer first.
    """

    def _seed(customer_id: str, **overrides) -> Customer:
        customer = Customer(customer_id=customer_id, **overrides)
        db_session.add(customer)
        db_session.commit()
        db_session.refresh(customer)
        return customer

    return _seed


@pytest.fixture(name="fake_redis")
def fake_redis_fixture():
    """Provides an isolated mock Redis client in memory."""
    redis_client = fakeredis.FakeRedis()
    yield redis_client
    redis_client.flushall()


@pytest.fixture(name="client")
def client_fixture(db_session: Session, fake_redis):
    """Overrides FastAPI's database and Redis dependencies with test doubles."""

    def get_db_session_override():
        yield db_session

    def get_redis_override():
        return fake_redis

    app.dependency_overrides[get_db_session] = get_db_session_override
    app.dependency_overrides[get_redis_client] = get_redis_override

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(name="auth_headers")
def auth_headers_fixture():
    """Valid headers for public/partner scoring and settlement endpoints."""
    return {"X-SwiftWolf-Key": settings.SWIFTWOLF_API_KEY}


@pytest.fixture(name="admin_auth_headers")
def admin_auth_headers_fixture():
    """Valid headers for administrative endpoints."""
    admin_key = getattr(settings, "ADMIN_API_KEY", settings.SWIFTWOLF_API_KEY)
    return {"X-SwiftWolf-Admin-Key": admin_key}
