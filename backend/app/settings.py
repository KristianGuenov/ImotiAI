from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = "dev-key-change-me"
    database_url: str = "sqlite:///./data/extractions.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
