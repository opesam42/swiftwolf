import os
from pydantic_settings import BaseSettings, SettingsConfigDict

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)

ENV_FILE_PATH = os.path.join(ROOT_DIR, ".env")

class Settings(BaseSettings):
    DATABASE_URL: str
    SWIFTWOLF_API_KEY: str = ""
    REDIS_URL: str = "redis://localhost:6379/1"

    # 4. Feed the absolute path directly to Pydantic
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH, 
        env_file_encoding='utf-8', 
        extra='ignore'
    )

# Instantiate the settings object
settings = Settings()