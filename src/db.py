"""
Shared Postgres connection settings for Thriftly.

Every module imports DB / connect() from here instead of re-declaring them.
Values come from the environment (.env is loaded automatically).
"""

import os

import psycopg2
from dotenv import load_dotenv

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
