'''*!*! Initialize a PostGIS review project from configured GeoParquet.'''

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from src.api.database import database_url
from src.project_config import ALLOWED_MODES, ReviewConfig, load_review_config


@dataclass(frozen=True)
class BootstrapResult:
    '''*!*! Describe the database state established by one bootstrap run.'''

    project_id: str
    feature_count: int
    imported: bool
    task_ids: dict[str, UUID]


def quote_identifier(value: str) -> str:
    '''*!*! Quote a DuckDB identifier after escaping embedded quotes.'''

    quote = chr(34)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def quote_string(value: str) -> str:
    '''*!*! Quote a DuckDB string literal after escaping apostrophes.'''

    quote = chr(39)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def load_duckdb_extension(connection, name: str) -> None:
    '''*!*! Load a DuckDB extension, installing it when it is not cached.'''

    import duckdb

    try:
        connection.execute(f'LOAD {name}')
    except duckdb.Error:
        connection.execute(f'INSTALL {name}')
        connection.execute(f'LOAD {name}')


def canonical_source(source: Path | str) -> str:
    '''*!*! Return a stable source identity without URL credentials or queries.'''

    value = str(source)
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {'http', 'https'} and parsed.netloc:
        hostname = parsed.hostname or ''
        port = f':{parsed.port}' if parsed.port else ''
        return urlunsplit((parsed.scheme.lower(), f'{hostname}{port}', parsed.path, '', ''))
    return str(Path(value).expanduser().resolve())


def task_id_for(project_id: str, mode: str) -> UUID:
    '''*!*! Return the deterministic identifier for a YAML-managed task.'''

    return uuid5(NAMESPACE_URL, f'damagemap-qaqc:{project_id}:{mode}')


def read_geoparquet_rows(
    config: ReviewConfig,
) -> list[tuple[str, str, str, str]]:
    '''*!*! Read and validate source features for transactional database loading.'''

    import duckdb
    import h3

    source = str(config.features_path)
    parsed_path = urlsplit(source).path if urlsplit(source).scheme else source
    if Path(parsed_path).suffix.lower() not in {'.parquet', '.geoparquet'}:
        raise ValueError('Database bootstrap requires a GeoParquet feature source')

    connection = duckdb.connect()
    try:
        load_duckdb_extension(connection, 'spatial')
        if urlsplit(source).scheme.lower() in {'http', 'https'}:
            load_duckdb_extension(connection, 'httpfs')
        description = connection.execute(
            'DESCRIBE SELECT * FROM read_parquet(?)',
            [source],
        ).fetchall()
        columns = [name for name, *_ in description]
        geometry_columns = [
            name
            for name, data_type, *_ in description
            if str(data_type).startswith('GEOMETRY')
        ]
        if len(geometry_columns) != 1:
            raise ValueError(
                'GeoParquet must contain exactly one geometry column; '
                f'found {geometry_columns}'
            )
        if config.feature_id_field not in columns:
            raise ValueError(
                f'GeoParquet is missing feature ID field {config.feature_id_field!r}'
            )

        h3_pattern = re.compile(rf'^{re.escape(config.h3_prefix)}(\d+)$')
        h3_columns = []
        for column in columns:
            match = h3_pattern.fullmatch(column)
            if match:
                resolution = int(match.group(1))
                if not 0 <= resolution <= 15:
                    raise ValueError(f'Invalid H3 resolution in column {column!r}')
                h3_columns.append((column, resolution))
        if not h3_columns:
            raise ValueError(
                f'GeoParquet has no H3 columns beginning with {config.h3_prefix!r}'
            )

        geometry_column = geometry_columns[0]
        property_columns = [column for column in columns if column != geometry_column]
        property_items = []
        for column in property_columns:
            property_items.extend([quote_string(column), quote_identifier(column)])
        h3_items = []
        for column, resolution in h3_columns:
            h3_items.extend([quote_string(str(resolution)), quote_identifier(column)])

        query = f'''
SELECT
    CAST({quote_identifier(config.feature_id_field)} AS VARCHAR),
    ST_AsGeoJSON({quote_identifier(geometry_column)}),
    json_object({', '.join(property_items)}),
    json_object({', '.join(h3_items)})
FROM read_parquet(?)
'''
        rows = connection.execute(query, [source]).fetchall()
    except duckdb.Error as error:
        raise RuntimeError(f'Could not read GeoParquet source {source}: {error}') from error
    finally:
        connection.close()

    seen_ids = set()
    validated_rows = []
    for feature_id, geometry_json, properties_json, h3_json in rows:
        feature_id = str(feature_id or '').strip()
        if not feature_id:
            raise ValueError('GeoParquet contains an empty feature ID')
        if feature_id in seen_ids:
            raise ValueError(f'GeoParquet contains duplicate feature ID {feature_id!r}')
        seen_ids.add(feature_id)

        indexes = json.loads(h3_json)
        for resolution_text, index in indexes.items():
            index = str(index or '').strip()
            resolution = int(resolution_text)
            if not h3.is_valid_cell(index):
                raise ValueError(f'Feature {feature_id!r} has invalid H3 index {index!r}')
            if h3.get_resolution(index) != resolution:
                raise ValueError(
                    f'Feature {feature_id!r} H3 index {index!r} does not match '
                    f'resolution {resolution}'
                )
            indexes[resolution_text] = index
        validated_rows.append(
            (feature_id, geometry_json, properties_json, json.dumps(indexes))
        )
    return validated_rows


def sync_tasks(connection, config: ReviewConfig) -> dict[str, UUID]:
    '''*!*! Synchronize YAML-managed tasks and this reviewer's assignments.'''

    import h3

    managed_ids = [task_id_for(config.project_id, mode) for mode in ALLOWED_MODES]
    connection.execute(
        'UPDATE tasks SET active = false WHERE id = ANY(%s)',
        [managed_ids],
    )

    task_ids = {}
    all_features = not config.todo_h3_indexes
    for mode in config.modes:
        task_id = task_id_for(config.project_id, mode)
        labels = list(config.annotation_labels) if mode in {'annotation', 'qaqc'} else []
        connection.execute(
            '''
INSERT INTO tasks (id, project_id, name, mode, labels, blind, active)
VALUES (%s, %s, %s, %s, %s, true, true)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    labels = EXCLUDED.labels,
    blind = EXCLUDED.blind,
    active = true
WHERE tasks.project_id = EXCLUDED.project_id
  AND tasks.mode = EXCLUDED.mode
''',
            [
                task_id,
                config.project_id,
                f'{config.project_name} {mode}',
                mode,
                labels,
            ],
        )
        connection.execute(
            '''
INSERT INTO task_reviewers (task_id, reviewer_id, all_features)
VALUES (%s, %s, %s)
ON CONFLICT (task_id, reviewer_id) DO UPDATE SET
    all_features = EXCLUDED.all_features
''',
            [task_id, config.user, all_features],
        )
        connection.execute(
            '''
DELETE FROM task_h3_assignments
WHERE task_id = %s AND reviewer_id = %s
''',
            [task_id, config.user],
        )
        assignment_rows = [
            [task_id, config.user, h3.get_resolution(index), index]
            for index in config.todo_h3_indexes
        ]
        if assignment_rows:
            with connection.cursor() as cursor:
                cursor.executemany(
                    '''
INSERT INTO task_h3_assignments (
    task_id,
    reviewer_id,
    resolution,
    h3_index
) VALUES (%s, %s, %s, %s)
''',
                    assignment_rows,
                )
        task_ids[mode] = task_id
    return task_ids


def bootstrap_project(config: ReviewConfig) -> BootstrapResult:
    '''*!*! Import a project once and refresh its YAML-managed task assignments.'''

    source = canonical_source(config.features_path)
    rows = None
    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        connection.execute(
            'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            [config.project_id],
        )
        connection.execute(
            '''
INSERT INTO projects (id, name)
VALUES (%s, %s)
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
''',
            [config.project_id, config.project_name],
        )
        imported_row = connection.execute(
            '''
SELECT source, feature_count
FROM feature_imports
WHERE project_id = %s
''',
            [config.project_id],
        ).fetchone()

        imported = False
        if imported_row is not None:
            if imported_row['source'] != source:
                raise RuntimeError(
                    f'Project {config.project_id!r} was initialized from '
                    f'{imported_row["source"]!r}, not {source!r}'
                )
            feature_count = imported_row['feature_count']
            current_count = connection.execute(
                'SELECT count(*) AS count FROM features WHERE project_id = %s',
                [config.project_id],
            ).fetchone()['count']
            if current_count != feature_count:
                raise RuntimeError(
                    f'Project {config.project_id!r} import record expects '
                    f'{feature_count:,} features but PostGIS contains '
                    f'{current_count:,}'
                )
        else:
            existing_count = connection.execute(
                'SELECT count(*) AS count FROM features WHERE project_id = %s',
                [config.project_id],
            ).fetchone()['count']
            if existing_count:
                raise RuntimeError(
                    f'Project {config.project_id!r} has features but no import record; '
                    'refusing to overwrite them'
                )

            rows = read_geoparquet_rows(config)
            connection.execute(
                '''
CREATE TEMP TABLE bootstrap_features (
    id text PRIMARY KEY,
    geometry_json text NOT NULL,
    properties_json text NOT NULL,
    h3_json text NOT NULL
) ON COMMIT DROP
'''
            )
            with connection.cursor().copy(
                '''
COPY bootstrap_features (id, geometry_json, properties_json, h3_json)
FROM STDIN
'''
            ) as copy:
                for row in rows:
                    copy.write_row(row)
            connection.execute(
                '''
INSERT INTO features (project_id, id, geometry, properties)
SELECT
    %s,
    id,
    ST_SetSRID(ST_GeomFromGeoJSON(geometry_json), 4326),
    properties_json::jsonb
FROM bootstrap_features
''',
                [config.project_id],
            )
            connection.execute(
                '''
INSERT INTO feature_h3 (project_id, feature_id, resolution, h3_index)
SELECT
    %s,
    feature.id,
    cell.key::smallint,
    cell.value
FROM bootstrap_features AS feature
CROSS JOIN LATERAL jsonb_each_text(feature.h3_json::jsonb) AS cell
''',
                [config.project_id],
            )
            feature_count = len(rows)
            connection.execute(
                '''
INSERT INTO feature_imports (project_id, source, feature_count)
VALUES (%s, %s, %s)
''',
                [config.project_id, source, feature_count],
            )
            imported = True

        task_ids = sync_tasks(connection, config)
    return BootstrapResult(
        project_id=config.project_id,
        feature_count=feature_count,
        imported=imported,
        task_ids=task_ids,
    )


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line arguments for database bootstrap.'''

    parser = argparse.ArgumentParser(
        description='Initialize a PostGIS review project from GeoParquet.',
    )
    parser.add_argument('--yaml', type=Path, required=True)
    return parser


def main() -> None:
    '''*!*! Bootstrap the configured project and report the resulting state.'''

    config = load_review_config(build_parser().parse_args().yaml)
    result = bootstrap_project(config)
    action = 'Imported' if result.imported else 'Reused'
    tasks = ', '.join(f'{mode}={task_id}' for mode, task_id in result.task_ids.items())
    print(
        f'{action} {result.feature_count:,} PostGIS features for '
        f'{result.project_id}; tasks: {tasks}',
        flush=True,
    )


if __name__ == '__main__':
    main()
