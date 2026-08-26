from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # pydantic-settings v2 matches env vars case-insensitively by default,
    # so DATABASE_URL -> database_url and REDIS_URL -> redis_url need no
    # extra mapping. This replaces the old (deprecated, v1-style) Config
    # class that used to live here.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    database_url: str = "postgresql://ledger_user:ledger_pass@localhost:5432/ledger_db"
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
