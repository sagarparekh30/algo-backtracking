"""
PostgreSQL connection helpers.

Provides:
  get_conn()     — return a new psycopg2 connection (caller must close)
  get_engine()   — return a shared SQLAlchemy engine (for pd.read_sql)

Usage:
  from db.connection import get_conn, get_engine

  # raw cursor (inserts, updates)
  conn = get_conn()
  try:
      cur = conn.cursor()
      cur.execute("INSERT INTO ... VALUES (%s, %s)", (a, b))
      conn.commit()
  finally:
      conn.close()

  # pandas read
  import pandas as pd
  df = pd.read_sql("SELECT * FROM ...", get_engine())
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

_engine = None


def get_conn():
    """Return a new psycopg2 connection. Caller is responsible for closing it."""
    import psycopg2
    from config.settings import DATABASE_URL
    return psycopg2.connect(DATABASE_URL)


def get_engine():
    """
    Return a shared SQLAlchemy engine for the PostgreSQL database.
    Suitable for pd.read_sql() calls.
    """
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine
        from config.settings import DATABASE_URL
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _engine
