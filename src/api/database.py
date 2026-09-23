'''*!*! PostgreSQL connection management for the shared API.'''

from __future__ import annotations

import os
from collections.abc import Generator

from fastapi import Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def database_url() -> str:
    '''*!*! Return the configured PostgreSQL connection string.'''

    value = os.environ.get('DATABASE_URL', '').strip()
    if not value:
        raise RuntimeError('DATABASE_URL is required')
    return value


def create_pool() -> ConnectionPool:
    '''*!*! Build the process-wide PostgreSQL connection pool.'''

    return ConnectionPool(
        conninfo=database_url(),
        min_size=int(os.environ.get('DATABASE_POOL_MIN_SIZE', '1')),
        max_size=int(os.environ.get('DATABASE_POOL_MAX_SIZE', '10')),
        open=False,
        kwargs={'row_factory': dict_row},
        check=ConnectionPool.check_connection,
    )


def get_connection(request: Request) -> Generator[Connection, None, None]:
    '''*!*! Yield one pooled connection for a request transaction.'''

    with request.app.state.database_pool.connection() as connection:
        yield connection
