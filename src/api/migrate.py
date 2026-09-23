'''*!*! Apply ordered SQL migrations to the shared PostGIS database.'''

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from src.api.database import database_url


MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[2] / 'migrations'


def migration_checksum(sql: str) -> str:
    '''*!*! Return the stable checksum recorded for one migration.'''

    return hashlib.sha256(sql.encode('utf-8')).hexdigest()


def apply_migrations() -> list[str]:
    '''*!*! Apply unapplied migrations and reject changed migration history.'''

    applied = []
    with psycopg.connect(database_url(), autocommit=True) as connection:
        connection.execute(
            '''
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
'''
        )
        for path in sorted(MIGRATIONS_DIRECTORY.glob('*.sql')):
            sql = path.read_text(encoding='utf-8')
            checksum = migration_checksum(sql)
            row = connection.execute(
                'SELECT checksum FROM schema_migrations WHERE version = %s',
                [path.name],
            ).fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise RuntimeError(
                        f'Applied migration changed after installation: {path.name}'
                    )
                continue
            with connection.transaction():
                connection.execute(sql)
                connection.execute(
                    'INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)',
                    [path.name, checksum],
                )
            applied.append(path.name)
    return applied


def main() -> None:
    '''*!*! Apply migrations and report database state.'''

    applied = apply_migrations()
    if applied:
        migration_names = ', '.join(applied)
        print(f'Applied migrations: {migration_names}')
    else:
        print('Database migrations are current')


if __name__ == '__main__':
    main()
