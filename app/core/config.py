import os
from pathlib import Path
from typing import Optional, Literal
from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the project root dynamically based on this file's location:
# app/core/config.py -> parent(core) -> parent(app) -> parent(gallant-shannon)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    APP_NAME: str = "Multi-Agent AI Boardroom"
    APP_ENV: str = "development"
    DEBUG: bool = True
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # Database
    DATABASE_URL: str = "sqlite:///./boardroom.db"

    # LLM Provider: 'openai' or 'mock'
    LLM_PROVIDER: Literal["openai", "mock"] = "mock"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: Optional[str] = None

    @field_validator("OPENAI_BASE_URL", mode="before")
    def clean_base_url(cls, v: Optional[str]) -> Optional[str]:
        if not v or v.strip() == "":
            return None
        return v.strip()

    # Guardrails and Limits
    DEFAULT_MAX_ROUNDS: int = 2
    MAX_ALLOWED_ROUNDS: int = 5
    MAX_TOPIC_LENGTH: int = 500
    MAX_AGENT_TOKENS: int = 600
    LLM_REQUEST_TIMEOUT_SECONDS: float = 30.0
    LLM_MAX_RETRIES: int = 3
    LLM_BACKOFF_FACTOR: float = 1.5

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
