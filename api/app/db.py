"""Postgres access: one small connection pool, raw parameterized SQL, no ORM."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool = None


def init_pool(database_url):
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection():
    """Yield a connection inside a transaction: commit on success, rollback on error."""
    with _pool.connection() as conn:
        yield conn
