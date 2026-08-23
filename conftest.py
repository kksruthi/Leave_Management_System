"""Test bootstrap.

Two jobs, both done before any test module is imported.

1. **Make the project importable** when running bare `pytest`. `python -m`
   puts the working directory on `sys.path`; plain `pytest` does not.

2. **Point the suite at its own database.** The tests assert on exact
   balances, so they need a ledger nobody else has written to. Sharing a
   database with the demo seed meant `seed_demo.py` broke a dozen accrual
   assertions — and the append-only trigger (finding 16) means those rows
   cannot simply be deleted afterwards.

   So `pytest` uses `leave_engine_test`, created and migrated automatically on
   first run. Point `TEST_DATABASE_URL` elsewhere to override, or set
   `LEAVE_ENGINE_USE_DEV_DB=1` to run against whatever `DATABASE_URL` says
   (useful for debugging a real dataset, but expect balance assertions to
   fail).

   This must happen before `app.db` is imported, because that module builds
   its engine at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DEV_URL = "postgresql+psycopg2://leave:leave@127.0.0.1:5432/leave_engine"
TEST_DB_NAME = "leave_engine_test"


def _derive_test_url(dev_url: str) -> str:
    parts = urlsplit(dev_url)
    return urlunsplit(parts._replace(path=f"/{TEST_DB_NAME}"))


def _ensure_database(url: str) -> None:
    """Create the test database if it does not exist, then migrate and seed."""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    parts = urlsplit(url)
    admin = urlunsplit(parts._replace(path="/postgres"))
    dsn = admin.replace("postgresql+psycopg2://", "postgresql://")

    connection = psycopg2.connect(dsn)
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
            if cursor.fetchone() is None:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME))
                )
                print(f"\n[conftest] created {TEST_DB_NAME}")
    finally:
        connection.close()

    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "seed_users.py"],
        cwd=ROOT, env=env, check=True, capture_output=True,
    )


if not os.environ.get("LEAVE_ENGINE_USE_DEV_DB"):
    _url = os.environ.get("TEST_DATABASE_URL") or _derive_test_url(
        os.environ.get("DATABASE_URL", DEFAULT_DEV_URL)
    )
    _ensure_database(_url)
    os.environ["DATABASE_URL"] = _url

# Tokens must verify across the whole run; without this the dev fallback
# generates a fresh random secret per process.
os.environ.setdefault("JWT_SECRET", "pytest-secret-key-at-least-32-characters")
