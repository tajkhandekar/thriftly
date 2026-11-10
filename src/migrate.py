"""
Minimal SQL migration runner.

Applies every migrations/NNN_*.sql file not yet recorded in schema_migrations,
in filename order, each in its own transaction.

  python src/migrate.py              # apply pending migrations
  python src/migrate.py --status     # list applied / pending
  python src/migrate.py --baseline   # mark ALL current files as applied without
                                     # running them (for a DB that was set up by
                                     # hand from the old schema.sql)
"""

import argparse
from pathlib import Path

from db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def applied_versions(cur):
    cur.execute("SELECT version FROM schema_migrations;")
    return {row[0] for row in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show applied/pending")
    group.add_argument("--baseline", action="store_true",
                       help="record all migrations as applied without running them")
    args = parser.parse_args()

    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CREATE_TRACKING_TABLE)
            done = applied_versions(cur)

        pending = [f for f in migration_files() if f.stem not in done]

        if args.status:
            for f in migration_files():
                print(f"  {'applied' if f.stem in done else 'pending'}  {f.name}")
            return

        if not pending:
            print("Database is up to date.")
            return

        for f in pending:
            with conn, conn.cursor() as cur:  # one transaction per migration
                if args.baseline:
                    print(f"Baselining {f.name} (not executed)")
                else:
                    print(f"Applying {f.name}")
                    cur.execute(f.read_text())
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s);", (f.stem,)
                )
        print(f"Done: {len(pending)} migration(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
