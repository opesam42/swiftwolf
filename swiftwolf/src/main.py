from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlmodel import Session, select
from src.routes import router
from src.database import db_init, engine, SessionDep
from src.redis import get_redis_client
from src.core.services import BlacklistService

import logging
from src.core.models import Transaction

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    # Eager cache warm-up — optional, since BlacklistService.get_active_keys()
    # already self-heals from Postgres on a cache miss. This just avoids that
    # one extra round-trip on the very first /v1/score call after a deploy.
    with Session(engine) as db:
        BlacklistService(db_session=db, redis_client=get_redis_client()).sync_to_redis()
    yield

app = FastAPI(
    lifespan=lifespan,
    title="SwiftWolf",
    description="SwiftWolf API",
    version="0.1.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router)

@app.get("/")
async def root():
    return {"message": "Welcome to SwiftWolf API"}

# @app.get("/health")
# async def health_check():

#     return {"status": "healthy"}


@app.get("/health")
async def health_check(db: SessionDep):
    recent_transactions = db.exec(
        select(Transaction)
        .order_by(Transaction.occurred_at.desc())
        .limit(5)
    ).all()

    logger.info(f"Health check — last {len(recent_transactions)} transactions:")
    for txn in recent_transactions:
        logger.info(
            f"  {txn.transaction_reference} | {txn.customer_id} | "
            f"₦{txn.amount} | {txn.transaction_type} | {txn.occurred_at}"
        )

    return {"status": "healthy"}