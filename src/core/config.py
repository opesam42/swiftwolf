import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
ENV_DIR = os.path.dirname(ROOT_DIR)

ENV_FILE_PATH = os.path.join(ENV_DIR, ".env")

class Settings(BaseSettings):
    DATABASE_URL: str
    SWIFTWOLF_API_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379/1"

    # HMAC KEY FOR HASHING RECEIPIENT ID LIKE BENEFICIARY ACCOUNT NUMBER,
    SW_HMAC_KEY: str

    # Header key for /v1/internal JSON (scripts). Empty → SWIFTWOLF_API_KEY.
    ADMIN_API_KEY: str = ""

    # Browser dashboard login at /admin. Separate from SWIFTWOLF_API_KEY.
    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""
    ADMIN_SESSION_SECRET: str = "" 

    # AMOUNT CONTINUOUS SCORE CONFIG
    AMOUNT_SCORE_MAX_SCORE: float = Field(
        default=50,
        description="Maximum risk points awarded for extreme amount deviations."
    )
    AMOUNT_SCORE_STEEPNESS: float = Field(
        default=1.5,
        description="Growth rate of the Sigmoid curve. Higher values create a steeper score ramp."
    )
    AMOUNT_SCORE_MIDPOINT: float = Field(
        default=3.0,
        description="Z-score deviation midpoint where 50% of max points are awarded."
    )

    # --- Velocity Window Parameters ---
    VELOCITY_WINDOW_SECONDS: int = Field(
        default=600,
        description="Sliding time window duration in seconds (600s = 10 minutes) for tracking transaction velocity.",
    )

    VELOCITY_MAX_THRESHOLD: int = Field(
        default=5,
        description= "Maximum transaction count allowed within VELOCITY_WINDOW_SECONDS before raising a burst penalty.",
    )

    VELOCITY_SCORE_PENALTY: int = Field(
        default=50,
        description="Risk points added when transaction count in the window exceeds VELOCITY_MAX_THRESHOLD.",
    )

    # --- Cold Start ---
    COLD_START_MIN_SETTLED_TRANSACTIONS: int = Field(
        default=10,
        description="Settled transactions (across all categories) after which a customer leaves cold start.",
    )

    # --- Recent-habit amount baseline (EWMA) ---
    EWMA_ALPHA: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        description="Exponential decay for the recent-habit amount mean/variance. "
                    "0.05 ≈ 40-transaction memory; warmup uses max(alpha, 1/n).",
    )

    # --- Adaptive Cluster Baselines (typing / behavioural biometrics) ---
    TYPING_CLUSTER_MAX: int = Field(
        default=5,
        ge=1,
        description="Max EWMA clusters per telemetry field. Slots fill as new styles appear.",
    )
    TYPING_COLD_START_SAMPLES: int = Field(
        default=30,
        ge=1,
        description="Genuine settled biometric samples before typing can add risk points.",
    )
    TYPING_MATCH_Z: float = Field(
        default=2.5,
        gt=0.0,
        description="Max Z-score against the nearest cluster to count as a match (else spawn/flag).",
    )
    TYPING_DEFAULT_STD_MS: float = Field(
        default=15.0,
        gt=0.0,
        description="Stand-in σ (ms) for a brand-new duration cluster while ewma_std is still 0.",
    )
    TYPING_DEFAULT_STD_BACKSPACE: float = Field(
        default=1.0,
        gt=0.0,
        description="Stand-in σ for a brand-new backspace_count cluster while ewma_std is still 0.",
    )
    TYPING_SCORE_MAX: float = Field(
        default=30,
        ge=0.0,
        description="Cap on typing_deviation risk points (same sigmoid shape as amount).",
    )

    # 4. Feed the absolute path directly to Pydantic
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH, 
        env_file_encoding='utf-8', 
        extra='ignore'
    )

# Instantiate the settings object
settings = Settings()