from pathlib import Path

import typer
from sqlmodel import Session

from jobs.seed_customer_profile import seed_profile
from src.blacklist.models import BlacklistedAccount
from src.blacklist.services import BlacklistService
from src.core.database import db_init, engine
from src.core.redis import get_redis_client
from src.settlement.models import TransactionChannel

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
            reason=reason,
            source="analyst",
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


@app.command("seed-profile")
def seed_profile_cmd(
    customer_id: str = typer.Option(..., help="Bank customer id to attribute every CSV row to"),
    csv_path: Path = typer.Option(
        ...,
        "--csv",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Statement CSV (date, time, amount_kobo, transaction_id, recipient, provider, and type or detail)",
    ),
    medium: str = typer.Option(
        TransactionChannel.STATEMENT.value,
        help="Default channel when the CSV has no medium column",
    ),
    timezone_name: str = typer.Option(
        "Africa/Lagos",
        "--timezone",
        help="IANA timezone for naive date+time columns",
    ),
    dry_run: bool = typer.Option(False, help="Parse and report skips without writing"),
    limit: int | None = typer.Option(None, min=1, help="Only process the first N valid rows after sort"),
):
    """Warm a customer baseline from historical statement rows. Does not score."""
    with Session(engine) as db:
        report = seed_profile(
            csv_path,
            customer_id,
            db=db,
            redis_client=get_redis_client(),
            default_medium=medium,
            timezone_name=timezone_name,
            dry_run=dry_run,
            limit=limit,
        )
    for line in report.summary_lines():
        typer.echo(line)


if __name__ == "__main__":
    app()