"""
Database migration runner.

Applies all pending SQL migration files in order.

Usage:
  python db/migrate.py
  python db/migrate.py --dry-run

Environment variables required:
  DATABASE_URL  — PostgreSQL connection string
                  e.g. postgresql://user:pass@localhost:5432/trading
"""

import os
import sys
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def run_migrations(dry_run: bool = False) -> None:
    from db.connection import get_conn

    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    # Ensure tracking table exists (bootstrapping)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     VARCHAR(50)  PRIMARY KEY,
            applied_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            description TEXT
        )
    """)
    conn.commit()

    # Collect applied versions
    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    applied = {row[0] for row in cur.fetchall()}

    # Discover migration files (sorted)
    files = sorted(
        f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql")
    )

    if not files:
        logger.info("No migration files found in %s", MIGRATIONS_DIR)
        cur.close()
        conn.close()
        return

    pending = [f for f in files if f.split("_")[0] not in applied]

    if not pending:
        logger.info("All %d migration(s) already applied.", len(files))
        cur.close()
        conn.close()
        return

    logger.info(
        "%d migration(s) applied, %d pending.", len(applied), len(pending)
    )

    for filename in pending:
        version = filename.split("_")[0]
        filepath = os.path.join(MIGRATIONS_DIR, filename)

        with open(filepath, "r") as f:
            sql = f.read()

        logger.info("Applying migration %s: %s …", version, filename)

        if dry_run:
            logger.info("[DRY RUN] Would execute:\n%s", sql[:400])
            continue

        try:
            cur.execute(sql)
            conn.commit()
            logger.info("  Applied %s successfully.", version)
        except Exception as e:
            conn.rollback()
            logger.error("  FAILED %s: %s", version, e)
            cur.close()
            conn.close()
            raise

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run database migrations")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without applying them",
    )
    args = parser.parse_args()
    run_migrations(dry_run=args.dry_run)
