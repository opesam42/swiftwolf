import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)

ENV_FILE_PATH = os.path.join(ROOT_DIR, ".env")

class Settings(BaseSettings):
    DATABASE_URL: str
    SWIFTWOLF_API_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379/1"

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

    # 4. Feed the absolute path directly to Pydantic
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH, 
        env_file_encoding='utf-8', 
        extra='ignore'
    )

# Instantiate the settings object
settings = Settings()