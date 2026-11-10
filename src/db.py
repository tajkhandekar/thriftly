"""
Shared Postgres connection settings for Thriftly.

Every module imports DB / connect() from here instead of re-declaring them.
Values come from the environment (.env is loaded automatically).

connect() opens a fresh connection (fine for scripts and long jobs).
pooled() lends one from a shared pool: use it for short request-sized work
in the web app, where opening a connection to RDS (~0.7 s with TLS) would
dominate every request.
"""

import os
import threading
from contextlib import contextmanager

import psycopg2
from dotenv import load_dotenv
from psycopg2.pool import ThreadedConnectionPool

load_dotenv()

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    dbname=os.getenv("DB_NAME", "thriftly"),
    user=os.getenv("DB_USER", os.getenv("USER")),
    password=os.getenv("DB_PASSWORD", ""),
    port=os.getenv("DB_PORT", "5432"),
)


def connect():
    """Open a new psycopg2 connection using DB settings."""
    return psycopg2.connect(**DB)


POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
# TCP keepalives so idle pooled connections dropped by a NAT/firewall are
# noticed instead of hanging the next request.
_KEEPALIVE = dict(keepalives=1, keepalives_idle=60, keepalives_interval=10,
                  keepalives_count=3)

_pool = None
_pool_init_lock = threading.Lock()
_pool_slots = threading.BoundedSemaphore(POOL_MAX)


@contextmanager
def pooled(autocommit=True):
    """Borrow a pooled connection. Waits when all POOL_MAX are in use
    (ThreadedConnectionPool itself would raise). Connections that broke are
    discarded rather than reused.

    autocommit=True (default) runs each statement on its own: one round trip
    instead of BEGIN + query + COMMIT, ~3x faster against RDS. Don't wrap it
    in `with conn:` -- psycopg2 opens a transaction there even in autocommit
    mode. Pass autocommit=False when several statements must be atomic and
    commit with `with conn:`; anything left uncommitted is rolled back."""
    global _pool
    with _pool_init_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(1, POOL_MAX, **DB, **_KEEPALIVE)
    with _pool_slots:
        conn = _pool.getconn()
        if conn.closed:
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
        conn.autocommit = autocommit
        broken = False
        try:
            yield conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise
        finally:
            if not broken and not conn.closed and not conn.autocommit:
                try:
                    conn.rollback()
                except psycopg2.Error:
                    broken = True
            _pool.putconn(conn, close=broken or bool(conn.closed))
