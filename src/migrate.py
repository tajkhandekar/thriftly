"""
Minimal SQL migration runner.

Applies every migrations/NNN_*.sql file not yet recorded in schema_migrations,
in filename order, each in its own transaction.

  python src/migrate.py                    # apply pending migrations
  python src/migrate.py --status           # list applied / pending
  python src/migrate.py --baseline 002     # mark migrations up to and including
                                           # 002 as applied WITHOUT running them
                                           # (for a DB set up by hand). Later
                                           # migrations stay pending.
"""

import argparse
from pathlib import Path

from db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Arbitrary constant key for pg_advisory_lock, so concurrent runs serialize.
LOCK_KEY = 7_412_026

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def number(path):
    """'002_hybrid_search' -> '002'"""
    return path.stem.split("_", 1)[0]


def applied_versions(cur):
    cur.execute("SELECT version FROM schema_migrations;")
    return {row[0] for row in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show applied/pending")
    group.add_argument("--baseline", metavar="NNN",
                       help="record migrations up to and including NNN as applied "
                            "without running them")
    args = parser.parse_args()

    files = migration_files()
    if args.baseline and args.baseline not in {number(f) for f in files}:
        parser.error(f"no migration numbered {args.baseline!r} in {MIGRATIONS_DIR}")

    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Session-level lock: held until the connection closes, so a second
            # concurrent run waits here and then sees the first run's work.
            cur.execute("SELECT pg_advisory_lock(%s);", (LOCK_KEY,))
            cur.execute(CREATE_TRACKING_TABLE)
            done = applied_versions(cur)
        conn.autocommit = False

        pending = [f for f in files if f.stem not in done]

        if args.status:
            for f in files:
                print(f"  {'applied' if f.stem in done else 'pending'}  {f.name}")
            return

        if args.baseline:
            pending = [f for f in pending if number(f) <= args.baseline]

        if not pending:
            print("Nothing to do.")
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
        conn.close()  # also releases the advisory lock


if __name__ == "__main__":
    main()
