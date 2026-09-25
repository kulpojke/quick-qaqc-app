'''*!*! Initialize layer-aware PostGIS projects from configured GeoParquet.'''

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.api.database import database_url
from src.fetch_overture_buildings import cog_bounds_wgs84
from src.project_config import LayerConfig, ReviewConfig, load_review_config


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


def task_id_for(project_id: str, layer_id: str, mode: str) -> UUID:
    '''*!*! Return the deterministic identifier for one layer workflow task.'''

    return uuid5(NAMESPACE_URL, f'damagemap-qaqc:{project_id}:{layer_id}:{mode}')


def bounds_polygon_wkt(bounds_wgs84: tuple[float, float, float, float]) -> str:
    '''*!*! Build a WKT envelope used to filter source geometries.'''

    minx, miny, maxx, maxy = bounds_wgs84
    return (
        f'POLYGON (({minx} {miny}, {minx} {maxy}, {maxx} {maxy}, '
        f'{maxx} {miny}, {minx} {miny}))'
    )


def read_geoparquet_rows(
    layer: LayerConfig,
    bounds_wgs84: tuple[float, float, float, float],
) -> list[tuple[str, str, str, str]]:
    '''*!*! Read one layer, filter it to the COG, and generate H3 membership.'''

    import duckdb
    import h3

    source = str(layer.source)
    parsed_path = urlsplit(source).path if urlsplit(source).scheme else source
    if Path(parsed_path).suffix.lower() not in {'.parquet', '.geoparquet'}:
        raise ValueError(f'Layer {layer.id!r} requires a GeoParquet source')

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
                f'Layer {layer.id!r} must contain exactly one geometry column; '
                f'found {geometry_columns}'
            )
        if layer.feature_id_field not in columns:
            raise ValueError(
                f'Layer {layer.id!r} is missing feature ID field '
                f'{layer.feature_id_field!r}'
            )

        geometry_column = geometry_columns[0]
        property_columns = [column for column in columns if column != geometry_column]
        property_items = []
        for column in property_columns:
            property_items.extend([quote_string(column), quote_identifier(column)])
        geometry_identifier = quote_identifier(geometry_column)
        geometry_wgs84 = (
            geometry_identifier
            if layer.source_crs.upper() == 'EPSG:4326'
            else (
                f'ST_Transform({geometry_identifier}, '
                f'{quote_string(layer.source_crs)}, '
                f'{quote_string("EPSG:4326")}, always_xy := true)'
            )
        )
        query = f'''
SELECT
    CAST({quote_identifier(layer.feature_id_field)} AS VARCHAR),
    ST_AsGeoJSON({geometry_wgs84}),
    json_object({', '.join(property_items)}),
    ST_Y(ST_PointOnSurface({geometry_wgs84})),
    ST_X(ST_PointOnSurface({geometry_wgs84}))
FROM read_parquet(?)
WHERE ST_Intersects({geometry_wgs84}, ST_GeomFromText(?))
'''
        rows = connection.execute(
            query,
            [source, bounds_polygon_wkt(bounds_wgs84)],
        ).fetchall()
    except duckdb.Error as error:
        raise RuntimeError(
            f'Could not read GeoParquet layer {layer.id!r} from {source}: {error}'
        ) from error
    finally:
        connection.close()

    seen_ids = set()
    validated_rows = []
    for feature_id, geometry_json, properties_json, latitude, longitude in rows:
        feature_id = str(feature_id or '').strip()
        if not feature_id:
            raise ValueError(f'Layer {layer.id!r} contains an empty feature ID')
        if feature_id in seen_ids:
            raise ValueError(
                f'Layer {layer.id!r} contains duplicate feature ID {feature_id!r}'
            )
        seen_ids.add(feature_id)

        geometry = json.loads(geometry_json)
        if geometry.get('type') not in layer.geometry_types:
            raise ValueError(
                f'Layer {layer.id!r} feature {feature_id!r} has geometry type '
                f'{geometry.get("type")!r}; expected {layer.geometry_types}'
            )
        indexes = {
            str(resolution): h3.latlng_to_cell(latitude, longitude, resolution)
            for resolution in layer.h3_resolutions
        }
        properties = json.loads(properties_json)
        for resolution_text, index in indexes.items():
            properties[f'{layer.h3_prefix}{resolution_text}'] = index
        validated_rows.append(
            (
                feature_id,
                geometry_json,
                json.dumps(properties),
                json.dumps(indexes),
            )
        )
    return validated_rows


def sync_tasks(connection, config: ReviewConfig) -> dict[str, UUID]:
    '''*!*! Synchronize layer tasks and this reviewer's H3 assignments.'''

    import h3

    # *!*! The YAML is authoritative for project tasks during bootstrap.
    connection.execute(
        'UPDATE tasks SET active = false WHERE project_id = %s',
        [config.project_id],
    )

    task_ids = {}
    all_features = not config.todo_h3_indexes
    for layer in config.layers:
        for mode in layer.modes:
            task_id = task_id_for(config.project_id, layer.id, mode)
            labels = (
                list(config.annotation_labels)
                if mode == 'annotation'
                else []
            )
            connection.execute(
                '''
INSERT INTO tasks (
    id,
    project_id,
    layer_id,
    name,
    mode,
    labels,
    blind,
    active,
    managed
)
VALUES (%s, %s, %s, %s, %s, %s, true, true, true)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    labels = EXCLUDED.labels,
    blind = EXCLUDED.blind,
    active = true,
    managed = true
WHERE tasks.project_id = EXCLUDED.project_id
  AND tasks.layer_id = EXCLUDED.layer_id
  AND tasks.mode = EXCLUDED.mode
''',
                [
                    task_id,
                    config.project_id,
                    layer.id,
                    f'{config.project_name} {layer.name} {mode}',
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
            task_ids[f'{layer.id}:{mode}'] = task_id
    return task_ids


def sync_layer(connection, config: ReviewConfig, layer: LayerConfig) -> None:
    '''*!*! Create or update one configured layer definition.'''

    connection.execute(
        '''
INSERT INTO feature_layers (
    project_id,
    id,
    name,
    source_crs,
    geometry_types,
    feature_id_field,
    fields,
    h3_prefix,
    editing
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (project_id, id) DO UPDATE SET
    name = EXCLUDED.name,
    source_crs = EXCLUDED.source_crs,
    geometry_types = EXCLUDED.geometry_types,
    feature_id_field = EXCLUDED.feature_id_field,
    fields = EXCLUDED.fields,
    h3_prefix = EXCLUDED.h3_prefix,
    editing = EXCLUDED.editing,
    updated_at = now()
''',
        [
            config.project_id,
            layer.id,
            layer.name,
            layer.source_crs,
            list(layer.geometry_types),
            layer.feature_id_field,
            Jsonb({
                'feature_id': layer.feature_id_field,
                'predicted_class': layer.predicted_class_field,
                'confidence': layer.confidence_field,
                'display': list(layer.display_fields),
            }),
            layer.h3_prefix,
            Jsonb(vars(layer.editing)),
        ],
    )


def import_layer(
    connection,
    config: ReviewConfig,
    layer: LayerConfig,
    bounds_wgs84: tuple[float, float, float, float],
) -> tuple[int, bool]:
    '''*!*! Import one filtered layer once without replacing mutable features.'''

    source = canonical_source(layer.source)
    imported_row = connection.execute(
        '''
SELECT source, feature_count, bounds_wgs84
FROM feature_imports
WHERE project_id = %s AND layer_id = %s
''',
        [config.project_id, layer.id],
    ).fetchone()
    if imported_row is not None:
        if imported_row['source'] != source:
            raise RuntimeError(
                f'Layer {layer.id!r} was initialized from '
                f'{imported_row["source"]!r}, not {source!r}'
            )
        stored_bounds = imported_row['bounds_wgs84']
        if stored_bounds is not None and tuple(stored_bounds) != bounds_wgs84:
            raise RuntimeError(
                f'Layer {layer.id!r} was filtered with different COG bounds; '
                'use a new project or explicitly rebuild the development database'
            )
        if stored_bounds is None:
            connection.execute(
                '''
UPDATE feature_imports
SET bounds_wgs84 = %s
WHERE project_id = %s AND layer_id = %s
''',
                [list(bounds_wgs84), config.project_id, layer.id],
            )
        feature_count = imported_row['feature_count']
        current_count = connection.execute(
            '''
SELECT count(*) AS count
FROM features
WHERE project_id = %s AND layer_id = %s
''',
            [config.project_id, layer.id],
        ).fetchone()['count']
        if current_count != feature_count:
            raise RuntimeError(
                f'Layer {layer.id!r} import expects {feature_count:,} features '
                f'but PostGIS contains {current_count:,}'
            )
        return feature_count, False

    existing_count = connection.execute(
        '''
SELECT count(*) AS count
FROM features
WHERE project_id = %s AND layer_id = %s
''',
        [config.project_id, layer.id],
    ).fetchone()['count']
    if existing_count:
        raise RuntimeError(
            f'Layer {layer.id!r} has features but no import record; '
            'refusing to overwrite them'
        )

    rows = read_geoparquet_rows(layer, bounds_wgs84)
    connection.execute('DROP TABLE IF EXISTS bootstrap_features')
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
INSERT INTO features (project_id, layer_id, id, geometry, properties)
SELECT
    %s,
    %s,
    id,
    ST_SetSRID(ST_GeomFromGeoJSON(geometry_json), 4326),
    properties_json::jsonb
FROM bootstrap_features
''',
        [config.project_id, layer.id],
    )
    connection.execute(
        '''
INSERT INTO feature_h3 (project_id, layer_id, feature_id, resolution, h3_index)
SELECT
    %s,
    %s,
    feature.id,
    cell.key::smallint,
    cell.value
FROM bootstrap_features AS feature
CROSS JOIN LATERAL jsonb_each_text(feature.h3_json::jsonb) AS cell
''',
        [config.project_id, layer.id],
    )
    feature_count = len(rows)
    connection.execute(
        '''
INSERT INTO feature_imports (
    project_id,
    layer_id,
    source,
    feature_count,
    bounds_wgs84
)
VALUES (%s, %s, %s, %s, %s)
''',
        [
            config.project_id,
            layer.id,
            source,
            feature_count,
            list(bounds_wgs84),
        ],
    )
    return feature_count, True


def bootstrap_project(config: ReviewConfig) -> BootstrapResult:
    '''*!*! Import all configured layers and refresh task assignments.'''

    bounds_wgs84 = cog_bounds_wgs84(config.imagery_cog)
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

        feature_count = 0
        imported = False
        for layer in config.layers:
            sync_layer(connection, config, layer)
            layer_count, layer_imported = import_layer(
                connection,
                config,
                layer,
                bounds_wgs84,
            )
            feature_count += layer_count
            imported = imported or layer_imported
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
        description='Initialize a PostGIS review project from GeoParquet layers.',
    )
    parser.add_argument('--yaml', type=Path, required=True)
    return parser


def main() -> None:
    '''*!*! Bootstrap configured layers and report the resulting database state.'''

    config = load_review_config(build_parser().parse_args().yaml)
    result = bootstrap_project(config)
    action = 'Imported' if result.imported else 'Reused'
    tasks = ', '.join(f'{key}={task_id}' for key, task_id in result.task_ids.items())
    print(
        f'{action} {result.feature_count:,} PostGIS features for '
        f'{result.project_id}; tasks: {tasks}',
        flush=True,
    )


if __name__ == '__main__':
    main()
