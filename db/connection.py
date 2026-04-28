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
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

_engine = None
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    """Return a shared SimpleConnectionPool (lazily created, thread-safe)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                import psycopg2.pool
                from config.settings import DATABASE_URL
                _pool = psycopg2.pool.SimpleConnectionPool(
                    minconn=2, maxconn=20, dsn=DATABASE_URL
                )
    return _pool


class _PooledConn:
    """
    Thin wrapper around a psycopg2 connection from the pool.
    Overrides close() to return the connection to the pool instead of destroying it.
    All other attributes/methods delegate to the underlying connection.
    """
    __slots__ = ('_conn', '_pool')

    def __init__(self, conn, pool):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_pool', pool)

    def close(self):
        conn = object.__getattribute__(self, '_conn')
        pool = object.__getattribute__(self, '_pool')
        try:
            if not conn.closed:
                conn.rollback()
            pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def cursor(self, *args, **kwargs):
        return object.__getattribute__(self, '_conn').cursor(*args, **kwargs)

    def commit(self):
        return object.__getattribute__(self, '_conn').commit()

    def rollback(self):
        return object.__getattribute__(self, '_conn').rollback()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, '_conn'), name, value)


def get_conn():
    """
    Return a pooled psycopg2 connection wrapped so conn.close() returns it
    to the pool. All existing call sites work unchanged.
    """
    pool = _get_pool()
    return _PooledConn(pool.getconn(), pool)


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
