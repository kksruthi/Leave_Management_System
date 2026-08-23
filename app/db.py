"""Database engine / session wiring for the Leave Engine data layer."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

DEFAULT_URL = "postgresql+psycopg2://leave:leave@localhost:5432/leave_engine"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


engine = create_engine(get_database_url(), future=True, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
