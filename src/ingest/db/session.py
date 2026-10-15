"""Engine/session handling. src/ingest/db/session.py"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ingest.config import settings
from ingest.db.models import Base


def get_engine(url: str | None = None):
    return create_engine(url or settings.database_url, future=True)


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(url: str | None = None) -> None:
    eng = get_engine(url) if url else engine
    # Import here so target-table helpers register before create.
    from ingest.db import models  # noqa: F401

    Base.metadata.create_all(bind=eng)
