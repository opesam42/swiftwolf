import typer
from sqlmodel import Session

from src.admin.services import OnboardingService
from src.blacklist.models import BlacklistedAccount, BlacklistReason, BlacklistSource
from src.blacklist.services import BlacklistService
from src.core.database import db_init, engine
from src.core.redis import get_redis_client

app = typer.Typer(help="SwiftWolf 2.0 Administrative & Maintenance CLI")


@app.command()
def init_db():
    """Initializes database schema tables."""
    typer.echo("Initializing PostgreSQL tables...")
    db_init()
    typer.echo("Database tables created successfully.")


@app.command()
def seed_blacklist(
    account: str = typer.Option(..., help="Beneficiary account number"),
    bank_code: str = typer.Option(..., help="Beneficiary bank code"),
    reason: str = typer.Option("manual_flag", help="Blacklist reason"),
    added_by: str = typer.Option("cli_admin", help="Analyst or system identifier"),
):
    """Adds a new blacklisted account; the repository rebuilds the Redis set after commit."""
    with Session(engine) as db:
        entry = BlacklistedAccount(
            beneficiary_account=account,
            beneficiary_bank_code=bank_code,
            reason=BlacklistReason(reason),
            source=BlacklistSource.ANALYST,
            added_by=added_by,
        )
        BlacklistService(db, get_redis_client()).add(entry)

        typer.echo(f"Successfully blacklisted account {account}:{bank_code} and updated Redis cache.")


@app.command()
def sync_cache():
    """Forces an immediate rebuild of the Redis active blacklist cache."""
    with Session(engine) as db:
        redis_client = get_redis_client()
        keys = BlacklistService(db, redis_client).sync_to_redis()
        typer.echo(f"Redis cache refreshed. Total active blacklisted accounts: {len(keys)}")


if __name__ == "__main__":
    app()