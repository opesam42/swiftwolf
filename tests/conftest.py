import os

# Placeholder DSN so `Settings` can be constructed at import time. The real
# connection string is only known once pgserver has booted, at which point the
# `pg_engine` fixture rebinds every module-level engine reference below.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
)
# Forced, not setdefault: tests must never reach the REDIS_URL in .env (a real,
# shared Redis). Every test uses fakeredis; this unreachable local URL is a
# safety net so any code path that still builds a real client fails fast
# instead of touching real data.
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ.setdefault("SWIFTWOLF_API_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")

import fakeredis
import pgserver
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

import src.core.database as core_database
import src.main as main_module
from src.core.config import settings
from src.core.database import get_db_session
from src.core.redis import get_redis_client
from src.main import app
from src.profile.models import Customer

@pytest.fixture(scope="session")
def pg_engine(tmp_path_factory):
    """Boots one real PostgreSQL server (pgserver) for the whole test session.

    pgserver runs the native Postgres binaries shipped in its pip wheel — no
    Docker, no system install. Tests get actual Postgres semantics (BIGSERIAL
    identities, JSON columns, partial unique indexes, SELECT ... FOR UPDATE,
    timezone-aware timestamps) and, unlike the old PGlite setup, any number of
    concurrent connections.

    The data directory is a fresh temp dir per session, so every run starts
    from an empty cluster; cleanup_mode="stop" shuts the server down at exit.
    """
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    # get_uri() is a libpq URI over a Unix socket; point SQLAlchemy at psycopg2,
    # the driver this project already pins.
    engine = create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg2://", 1))

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
        engine.dispose()
        server.cleanup()


@pytest.fixture(name="db_session")
def db_session_fixture(pg_engine):
    """Gives each test a clean schema, then a session bound to the test server.

    Truncating up front (rather than after) keeps the database inspectable when
    a test fails, and RESTART IDENTITY means autoincrement ids are predictable
    from one test to the next.
    """
    table_names = ", ".join(
        f'"{table.name}"' for table in SQLModel.metadata.sorted_tables
    )
    if table_names:
        with pg_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

    with Session(pg_engine) as session:
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
def client_fixture(db_session: Session, fake_redis, monkeypatch):
    """Overrides FastAPI's database and Redis dependencies with test doubles."""

    def get_db_session_override():
        yield db_session

    def get_redis_override():
        return fake_redis

    app.dependency_overrides[get_db_session] = get_db_session_override
    app.dependency_overrides[get_redis_client] = get_redis_override
    # dependency_overrides only covers request handlers. The lifespan startup calls
    # get_redis_client() directly (to warm the blacklist cache), via the name
    # src.main imported — patch that too, or startup talks to the real REDIS_URL.
    monkeypatch.setattr(main_module, "get_redis_client", get_redis_override)

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
