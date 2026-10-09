import newrelic.agent

# Vercel has no start command for FastAPI; the agent reads NEW_RELIC_LICENSE_KEY
# from the process environment. Must run before FastAPI is imported.
newrelic.agent.initialize()

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from src.core.database import db_init, engine
from src.core.redis import get_redis_client

# Domain Services & Routers
from src.admin.router import pages_router as admin_pages_router
from src.admin.router import router as admin_router
from src.blacklist.services import BlacklistService
from src.profile.router import router as profile_router
from src.scoring.router import router as scoring_router
from src.settlement.router import router as settlement_router

ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI Lifespan Context Manager.
    Handles startup infrastructure initialization and graceful shutdown.
    """
    # 1. Ensure PostgreSQL database schema is initialized
    db_init()

    # 2. Warm up active blacklist Redis cache on application start
    try:
        redis_client = get_redis_client()
        with Session(engine) as db:
            synced_keys = BlacklistService(db, redis_client).sync_to_redis()
            print(f"[Lifespan Startup] Active blacklist cache warmed. Synced {len(synced_keys)} entries.")
    except Exception as e:
        print(f"[Lifespan Startup Warning] Could not warm Redis cache on boot: {e}")

    yield  # Application runs and handles HTTP requests

    # Shutdown cleanup (if applicable)
    print("[Lifespan Shutdown] SwiftWolf 2.0 application shutting down cleanly.")


# Instantiate FastAPI Application
app = FastAPI(
    title="SwiftWolf 2.0 Fraud Detection API",
    description="High-performance real-time fraud scoring, behavioral baselining, and transaction settlement.",
    version="2.0.0",
    lifespan=lifespan,
)

# Global CORS Middleware Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Prevents unhandled low-level exceptions from leaking system internals."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred. Please contact SwiftWolf engineering."},
    )


# Register Domain Routers
app.include_router(scoring_router, tags=["Real-time Scoring"])
app.include_router(settlement_router, tags=["Transaction Settlement"])
app.include_router(profile_router, tags=["Customer Insights & Baselines"])
app.include_router(admin_router, tags=["Internal Administration"])
app.include_router(admin_pages_router, tags=["Admin Dashboard"])
app.mount("/admin/static", StaticFiles(directory=str(ADMIN_STATIC_DIR)), name="admin-static")


# Health Check: ping the base URL. No auth, no dependencies — a 200 means the process is up.
@app.get("/", tags=["Infrastructure"])
async def health_check():
    """Liveness ping for load balancers, uptime monitors and integrators."""
    return {"status": "ok", "service": "SwiftWolf"}