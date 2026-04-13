"""
Database setup for Agency Hunter — applies every migration file in
`migrations/` (sorted by filename) against the self-hosted Supabase
Postgres instance shared with Job-search-automation.

Requires DATABASE_URL env var pointing to Postgres directly:
    postgresql://postgres:<password>@<host>:5432/postgres

Usage:
    python tools/setup_db.py

Called automatically at bot startup when DATABASE_URL is set (same pattern
as the sibling project). Individual migrations are expected to be
idempotent — use `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT
EXISTS`, etc.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def ensure_tables() -> None:
    """Run every migration in `migrations/` in filename order. Requires
    DATABASE_URL. No-ops (with a warning) when the env var is missing —
    in that case, apply the migrations manually in the Supabase SQL
    editor."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL not set — skipping auto table creation")
        return

    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        logger.error("No migration files found in %s", MIGRATIONS_DIR)
        return

    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        for path in migrations:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
            logger.info("Applied migration %s", path.name)
        conn.close()
    except Exception as e:
        logger.error(f"Failed to apply migrations: {e}")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_tables()
    print("Done.")
