from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGEST_")

    database_url: str = "sqlite:///./data/ingest.db"
    chunk_size: int = 10_000
    upload_dir: str = "data/uploads"
    contracts_dir: str = "contracts"


settings = Settings()
