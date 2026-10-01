'''*!*! Stage current PostGIS snapshots and publish configured R2 exports.'''

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, TextIO

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from src.api.bootstrap import (
    load_duckdb_extension,
    quote_identifier,
    quote_string,
)
from src.api.database import database_url
from src.api.r2_export import publish_batch
from src.geojson2parquet import geometry_column
from src.project_config import LayerConfig, ReviewConfig, load_review_config


DEFAULT_OUTPUT_DIRECTORY = Path('tmp')
EXPORT_BATCH_SIZE = 1_000
SAFE_FILENAME_PATTERN = re.compile(r'[^A-Za-z0-9._-]+')
JOIN_KEY_COLUMNS = ('_dm_project_id', '_dm_layer_id', '_dm_feature_id')


@dataclass(frozen=True)
class ExportResult:
    '''*!*! Describe one feature or annotation snapshot written locally.'''

    project_id: str
    stream_id: str
    revision: int
    row_count: int
    path: Path


@dataclass(frozen=True)
class LayerChangeToken:
    '''*!*! Summarize feature state used to detect layer changes cheaply.'''

    layer_id: str
    feature_count: int
    deleted_count: int
    version_total: int
    latest_update: str | None


@dataclass(frozen=True)
class AnnotationChangeToken:
    '''*!*! Summarize current annotation state for automatic exports.'''

    annotation_count: int
    version_total: int
    latest_update: str | None


@dataclass(frozen=True)
class ExportChangeState:
    '''*!*! Combine feature and annotation counters from one database read.'''

    layers: tuple[LayerChangeToken, ...]
    annotations: AnnotationChangeToken


def safe_filename_component(value: str) -> str:
    '''*!*! Convert a project or layer identifier into a portable filename part.'''

    normalized = SAFE_FILENAME_PATTERN.sub('-', value.strip()).strip('.-_')
    return normalized or 'project'


def snapshot_filename(project_id: str, layer_id: str, revision: int) -> str:
    '''*!*! Build the immutable-looking filename for one database revision.'''

    project = safe_filename_component(project_id)
    layer = safe_filename_component(layer_id)
    return f'{project}.{layer}.revision-{revision}.parquet'


def annotation_snapshot_filename(project_id: str, revision: int) -> str:
    '''*!*! Build the filename for current multi-reviewer annotations.'''

    project = safe_filename_component(project_id)
    return f'{project}.annotations.revision-{revision}.parquet'


def _json_object(value: object, field: str) -> dict[str, Any]:
    '''*!*! Normalize a JSON database value into a dictionary.'''

    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError(f'Database field {field!r} is not a JSON object')
    return value


def write_layer_sequence(
    connection: Connection,
    config: ReviewConfig,
    layer: LayerConfig,
    destination: TextIO,
) -> int:
    '''*!*! Stream one non-deleted PostGIS layer as temporary GeoJSON sequences.'''

    # *!*! A server-side cursor keeps statewide-derived layers from being copied
    # *!*! into Python memory before DuckDB gets a chance to write the snapshot.
    cursor_name = f'export_{safe_filename_component(layer.id)}'
    with connection.cursor(name=cursor_name, row_factory=dict_row) as cursor:
        cursor.itersize = EXPORT_BATCH_SIZE
        cursor.execute(
            '''
SELECT
    feature.id,
    ST_AsGeoJSON(feature.geometry)::jsonb AS geometry,
    feature.properties,
    COALESCE(
        (
            SELECT jsonb_object_agg(cell.resolution::text, cell.h3_index)
            FROM feature_h3 AS cell
            WHERE cell.project_id = feature.project_id
              AND cell.layer_id = feature.layer_id
              AND cell.feature_id = feature.id
        ),
        '{}'::jsonb
    ) AS h3
FROM features AS feature
WHERE feature.project_id = %s
  AND feature.layer_id = %s
  AND feature.deleted_at IS NULL
ORDER BY feature.id
''',
            [config.project_id, layer.id],
        )

        feature_count = 0
        for row in cursor:
            properties = {
                '_dm_project_id': config.project_id,
                '_dm_layer_id': layer.id,
                '_dm_feature_id': row['id'],
            }
            properties.update(_json_object(row['properties'], 'properties'))
            properties[layer.feature_id_field] = row['id']
            # *!*! Reserved join keys override any coincident source property.
            properties['_dm_project_id'] = config.project_id
            properties['_dm_layer_id'] = layer.id
            properties['_dm_feature_id'] = row['id']
            for resolution, h3_index in _json_object(row['h3'], 'h3').items():
                properties[f'{layer.h3_prefix}{resolution}'] = h3_index
            feature = {
                'type': 'Feature',
                'id': row['id'],
                'geometry': _json_object(row['geometry'], 'geometry'),
                'properties': properties,
            }
            destination.write(json.dumps(feature, separators=(',', ':')))
            destination.write('\n')
            feature_count += 1
    return feature_count


def write_annotation_sequence(
    connection: Connection,
    config: ReviewConfig,
    destination: TextIO,
) -> int:
    '''*!*! Stream current multi-reviewer annotations as newline-delimited JSON.'''

    with connection.cursor(name='export_annotations', row_factory=dict_row) as cursor:
        cursor.itersize = EXPORT_BATCH_SIZE
        cursor.execute(
            '''
SELECT
    annotation.id::text AS annotation_id,
    annotation.project_id AS _dm_project_id,
    annotation.layer_id AS _dm_layer_id,
    annotation.feature_id AS _dm_feature_id,
    annotation.task_id::text AS task_id,
    task.name AS task_name,
    task.mode AS task_mode,
    annotation.reviewer_id,
    annotation.label,
    annotation.notes,
    annotation.feature_version_seen,
    annotation.version AS annotation_version,
    annotation.created_at,
    annotation.updated_at
FROM annotations AS annotation
JOIN tasks AS task ON task.id = annotation.task_id
WHERE annotation.project_id = %s
ORDER BY annotation.layer_id, annotation.feature_id, annotation.reviewer_id
''',
            [config.project_id],
        )

        annotation_count = 0
        for row in cursor:
            record = dict(row)
            record['created_at'] = row['created_at'].isoformat()
            record['updated_at'] = row['updated_at'].isoformat()
            destination.write(json.dumps(record, separators=(',', ':')))
            destination.write('\n')
            annotation_count += 1
    return annotation_count


def write_sequence_parquet(
    sequence_path: Path,
    output_path: Path,
    expected_count: int,
) -> None:
    '''*!*! Convert annotation JSON records into atomic compressed Parquet.'''

    import duckdb

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}.',
        suffix='.parquet',
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()

    connection = duckdb.connect()
    try:
        # *!*! An explicit schema keeps empty exports and inferred timestamps
        # *!*! identical to populated annotation snapshots.
        connection.execute(
            '''
CREATE TEMP TABLE source_annotations (
    annotation_id VARCHAR,
    _dm_project_id VARCHAR,
    _dm_layer_id VARCHAR,
    _dm_feature_id VARCHAR,
    task_id VARCHAR,
    task_name VARCHAR,
    task_mode VARCHAR,
    reviewer_id VARCHAR,
    label VARCHAR,
    notes VARCHAR,
    feature_version_seen BIGINT,
    annotation_version BIGINT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
)
'''
        )
        if expected_count:
            connection.execute(
                '''
INSERT INTO source_annotations
SELECT
    annotation_id,
    _dm_project_id,
    _dm_layer_id,
    _dm_feature_id,
    task_id,
    task_name,
    task_mode,
    reviewer_id,
    label,
    notes,
    feature_version_seen,
    annotation_version,
    created_at,
    updated_at
FROM read_json_auto(?, format = 'newline_delimited')
''',
                [str(sequence_path)],
            )
        connection.execute(
            f'''
COPY source_annotations TO {quote_string(str(temporary_path))} (
    FORMAT PARQUET,
    COMPRESSION ZSTD
)
'''
        )
        output_count = connection.execute(
            'SELECT count(*) FROM read_parquet(?)',
            [str(temporary_path)],
        ).fetchone()[0]
        if output_count != expected_count:
            raise RuntimeError(
                f'Row-count mismatch: expected {expected_count}, wrote {output_count}'
            )
        os.replace(temporary_path, output_path)
    except duckdb.Error as error:
        raise RuntimeError(f'Could not write annotation Parquet: {error}') from error
    finally:
        connection.close()
        temporary_path.unlink(missing_ok=True)


def _empty_layer_table(connection, layer: LayerConfig) -> None:
    '''*!*! Create the minimum typed schema needed for an empty layer export.'''

    column_names = [
        *JOIN_KEY_COLUMNS,
        layer.feature_id_field,
        *(f'{layer.h3_prefix}{resolution}' for resolution in layer.h3_resolutions),
    ]
    if len(set(column_names)) != len(column_names):
        raise ValueError(
            f'Layer {layer.id!r} generates duplicate ID or H3 column names'
        )
    columns = ',\n    '.join(
        f'{quote_identifier(name)} VARCHAR' for name in column_names
    )
    connection.execute(
        f'''
CREATE TEMP TABLE source_features (
    {columns},
    geometry GEOMETRY('OGC:CRS84')
)
'''
    )


def write_sequence_geoparquet(
    sequence_path: Path,
    output_path: Path,
    layer: LayerConfig,
    expected_count: int,
) -> None:
    '''*!*! Convert a temporary GeoJSON sequence into atomic GeoParquet.'''

    import duckdb

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}.',
        suffix='.parquet',
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()

    connection = duckdb.connect()
    try:
        load_duckdb_extension(connection, 'spatial')
        if expected_count:
            connection.execute(
                'CREATE TEMP TABLE source_features AS SELECT * FROM ST_Read(?)',
                [str(sequence_path)],
            )
        else:
            _empty_layer_table(connection, layer)

        description = connection.execute(
            'SELECT * FROM source_features LIMIT 0'
        ).description
        source_geometry = geometry_column(description)
        source_columns = [name for name, *_ in description]
        required_columns = {
            *JOIN_KEY_COLUMNS,
            layer.feature_id_field,
            *(f'{layer.h3_prefix}{value}' for value in layer.h3_resolutions),
        }
        missing_columns = sorted(required_columns - set(source_columns))
        if missing_columns:
            raise RuntimeError(
                f'Layer {layer.id!r} export is missing generated column(s): '
                f'{", ".join(missing_columns)}'
            )

        property_columns = [
            name
            for name in source_columns
            if name != source_geometry and name.lower() != 'ogc_fid'
        ]
        selections = [quote_identifier(name) for name in property_columns]
        selections.append(
            f'{quote_identifier(source_geometry)} AS {quote_identifier("geometry")}'
        )
        copy_options = [
            'FORMAT PARQUET',
            'COMPRESSION ZSTD',
        ]
        if not expected_count:
            # *!*! DuckDB cannot infer GeoParquet metadata from zero geometry
            # *!*! values, so describe the otherwise valid empty WKB column.
            geo_metadata = json.dumps({
                'version': '1.0.0',
                'primary_column': 'geometry',
                'columns': {
                    'geometry': {
                        'encoding': 'WKB',
                        'geometry_types': list(layer.geometry_types),
                    },
                },
            }, separators=(',', ':'))
            copy_options.append(
                f'KV_METADATA {{geo: {quote_string(geo_metadata)}}}'
            )
        connection.execute(
            f'''
COPY (
    SELECT
        {',\n        '.join(selections)}
    FROM source_features
) TO {quote_string(str(temporary_path))} (
    {',\n    '.join(copy_options)}
)
'''
        )

        output_count = connection.execute(
            'SELECT count(*) FROM read_parquet(?)',
            [str(temporary_path)],
        ).fetchone()[0]
        if output_count != expected_count:
            raise RuntimeError(
                f'Row-count mismatch: expected {expected_count}, wrote {output_count}'
            )
        metadata = connection.execute(
            'SELECT key, value FROM parquet_kv_metadata(?)',
            [str(temporary_path)],
        ).fetchall()
        metadata_keys = {
            key.decode() if isinstance(key, bytes) else str(key)
            for key, _ in metadata
        }
        if 'geo' not in metadata_keys:
            raise RuntimeError('DuckDB output is Parquet but lacks GeoParquet metadata')
        os.replace(temporary_path, output_path)
    except duckdb.Error as error:
        raise RuntimeError(
            f'Could not write GeoParquet layer {layer.id!r}: {error}'
        ) from error
    finally:
        connection.close()
        temporary_path.unlink(missing_ok=True)


def _project_revision(connection: Connection, config: ReviewConfig) -> int:
    '''*!*! Return the snapshotted project revision after validating its layers.'''

    row = connection.execute(
        'SELECT revision FROM projects WHERE id = %s',
        [config.project_id],
    ).fetchone()
    if row is None:
        raise RuntimeError(f'Project {config.project_id!r} is not in PostGIS')

    database_layers = {
        result['id']
        for result in connection.execute(
            'SELECT id FROM feature_layers WHERE project_id = %s',
            [config.project_id],
        ).fetchall()
    }
    missing_layers = sorted({layer.id for layer in config.layers} - database_layers)
    if missing_layers:
        raise RuntimeError(
            f'Project {config.project_id!r} is missing database layer(s): '
            f'{", ".join(missing_layers)}'
        )
    return row['revision']


def _read_layer_change_tokens(
    connection: Connection,
    config: ReviewConfig,
) -> tuple[LayerChangeToken, ...]:
    '''*!*! Read compact per-layer state from an existing connection.'''

    rows = connection.execute(
        '''
SELECT
    layer.id AS layer_id,
    count(feature.id) AS feature_count,
    count(feature.id) FILTER (
        WHERE feature.deleted_at IS NOT NULL
    ) AS deleted_count,
    COALESCE(sum(feature.version), 0) AS version_total,
    max(feature.updated_at) AS latest_update
FROM feature_layers AS layer
LEFT JOIN features AS feature
  ON feature.project_id = layer.project_id
 AND feature.layer_id = layer.id
WHERE layer.project_id = %s
GROUP BY layer.id
ORDER BY layer.id
''',
        [config.project_id],
    ).fetchall()

    by_layer = {row['layer_id']: row for row in rows}
    missing_layers = sorted({layer.id for layer in config.layers} - set(by_layer))
    if missing_layers:
        raise RuntimeError(
            f'Project {config.project_id!r} is missing database layer(s): '
            f'{", ".join(missing_layers)}'
        )
    return tuple(
        LayerChangeToken(
            layer_id=layer.id,
            feature_count=int(by_layer[layer.id]['feature_count']),
            deleted_count=int(by_layer[layer.id]['deleted_count']),
            version_total=int(by_layer[layer.id]['version_total']),
            latest_update=(
                by_layer[layer.id]['latest_update'].isoformat()
                if by_layer[layer.id]['latest_update'] is not None
                else None
            ),
        )
        for layer in config.layers
    )


def read_export_change_state(config: ReviewConfig) -> ExportChangeState:
    '''*!*! Read feature and annotation counters used by the export worker.'''

    with psycopg.connect(
        database_url(),
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        layers = _read_layer_change_tokens(connection, config)
        row = connection.execute(
            '''
SELECT
    count(*) AS annotation_count,
    COALESCE(sum(version), 0) AS version_total,
    max(updated_at) AS latest_update
FROM annotations
WHERE project_id = %s
''',
            [config.project_id],
        ).fetchone()
    return ExportChangeState(
        layers=layers,
        annotations=AnnotationChangeToken(
            annotation_count=int(row['annotation_count']),
            version_total=int(row['version_total']),
            latest_update=(
                row['latest_update'].isoformat()
                if row['latest_update'] is not None
                else None
            ),
        ),
    )


def read_layer_change_tokens(
    config: ReviewConfig,
) -> tuple[LayerChangeToken, ...]:
    '''*!*! Preserve the feature-only state helper used by diagnostics.'''

    return read_export_change_state(config).layers


def prune_old_snapshots(
    output_directory: Path,
    project_id: str,
    stream_id: str,
    *,
    keep_revisions: int = 2,
    protected_paths: set[Path] | None = None,
) -> tuple[Path, ...]:
    '''*!*! Delete only old exporter-named files after a validated replacement.'''

    if keep_revisions < 1:
        raise ValueError('keep_revisions must be at least one')
    project = safe_filename_component(project_id)
    stream = safe_filename_component(stream_id)
    pattern = re.compile(
        rf'^{re.escape(project)}\.{re.escape(stream)}\.revision-(\d+)\.parquet$'
    )
    protected = {
        path.expanduser().resolve() for path in (protected_paths or set())
    }
    candidates = []
    for path in output_directory.glob(f'{project}.{stream}.revision-*.parquet'):
        match = pattern.fullmatch(path.name)
        if match is None or path.resolve() in protected:
            continue
        candidates.append((int(match.group(1)), path))
    candidates.sort(key=lambda item: item[0], reverse=True)

    deleted = []
    for _, path in candidates[keep_revisions:]:
        # *!*! `unlink` removes a matching symlink itself, never its target.
        path.unlink()
        deleted.append(path)
    return tuple(deleted)


def export_project(
    config: ReviewConfig,
    output_directory: Path | None = None,
    *,
    layer_ids: set[str] | None = None,
    include_annotations: bool = True,
    keep_revisions: int = 2,
) -> tuple[ExportResult, ...]:
    '''*!*! Write selected feature and annotation streams from one snapshot.'''

    output_directory = (
        output_directory
        if output_directory is not None
        else config.source_path.parent / DEFAULT_OUTPUT_DIRECTORY
    )
    output_directory = output_directory.expanduser().resolve()
    if keep_revisions < 1:
        raise ValueError('keep_revisions must be at least one')
    configured_layer_ids = {layer.id for layer in config.layers}
    selected_layer_ids = configured_layer_ids if layer_ids is None else layer_ids
    unknown_layer_ids = selected_layer_ids - configured_layer_ids
    if unknown_layer_ids:
        raise ValueError(
            f'Unknown export layer(s): {", ".join(sorted(unknown_layer_ids))}'
        )

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        # *!*! All layer queries see one database snapshot even if reviewers keep
        # *!*! editing while this longer-running export is in progress.
        connection.execute(
            'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
        )
        revision = _project_revision(connection, config)
        results = []
        with tempfile.TemporaryDirectory(prefix='damagemap-export-') as temp_dir:
            temporary_directory = Path(temp_dir)
            for layer in config.layers:
                if layer.id not in selected_layer_ids:
                    continue
                sequence_path = temporary_directory / f'{layer.id}.geojsonl'
                with sequence_path.open('w', encoding='utf-8') as sequence:
                    feature_count = write_layer_sequence(
                        connection,
                        config,
                        layer,
                        sequence,
                    )
                output_path = output_directory / snapshot_filename(
                    config.project_id,
                    layer.id,
                    revision,
                )
                write_sequence_geoparquet(
                    sequence_path,
                    output_path,
                    layer,
                    feature_count,
                )
                results.append(ExportResult(
                    project_id=config.project_id,
                    stream_id=layer.id,
                    revision=revision,
                    row_count=feature_count,
                    path=output_path,
                ))
            if include_annotations:
                sequence_path = temporary_directory / 'annotations.jsonl'
                with sequence_path.open('w', encoding='utf-8') as sequence:
                    annotation_count = write_annotation_sequence(
                        connection,
                        config,
                        sequence,
                    )
                output_path = output_directory / annotation_snapshot_filename(
                    config.project_id,
                    revision,
                )
                write_sequence_parquet(
                    sequence_path,
                    output_path,
                    annotation_count,
                )
                results.append(ExportResult(
                    project_id=config.project_id,
                    stream_id='annotations',
                    revision=revision,
                    row_count=annotation_count,
                    path=output_path,
                ))

    # *!*! R2 failures propagate before local retention or watch state advances.
    # *!*! Compose restarts a failed worker, which retries a complete snapshot.
    if config.exports is not None:
        publish_batch(config, tuple(results), keep_revisions=keep_revisions)

    # *!*! Prune only after every selected stream has been written and published.
    # *!*! Configured local sources remain protected even if unusually named
    # *!*! like an exporter-generated revision in the same directory.
    protected_sources = {
        layer.source.expanduser().resolve()
        for layer in config.layers
        if isinstance(layer.source, Path)
    }
    for result in results:
        for deleted_path in prune_old_snapshots(
            output_directory,
            config.project_id,
            result.stream_id,
            keep_revisions=keep_revisions,
            protected_paths=protected_sources,
        ):
            print(f'Deleted old generated snapshot {deleted_path}', flush=True)
    return tuple(results)


def print_results(results: tuple[ExportResult, ...]) -> None:
    '''*!*! Report completed layer snapshots immediately in container logs.'''

    for result in results:
        print(
            f'Wrote {result.row_count:,} rows from stream {result.stream_id!r} '
            f'at revision {result.revision} to {result.path}',
            flush=True,
        )


def edit_count_since(
    exported: ExportChangeState,
    current: ExportChangeState,
) -> int:
    '''*!*! Count feature and annotation mutations since the last export.'''

    exported_versions = {
        token.layer_id: token.version_total for token in exported.layers
    }
    feature_edits = sum(
        max(0, token.version_total - exported_versions.get(token.layer_id, 0))
        for token in current.layers
    )
    annotation_edits = max(
        0,
        current.annotations.version_total - exported.annotations.version_total,
    )
    return feature_edits + annotation_edits


def changed_layer_ids(
    exported: ExportChangeState,
    current: ExportChangeState,
) -> set[str]:
    '''*!*! Return feature layers whose current state differs from the export.'''

    exported_layers = {token.layer_id: token for token in exported.layers}
    return {
        token.layer_id
        for token in current.layers
        if token != exported_layers.get(token.layer_id)
    }


def watch_project(
    config: ReviewConfig,
    output_directory: Path | None = None,
    *,
    poll_seconds: float = 2.0,
    interval_seconds: float = 300.0,
    edit_threshold: int = 50,
    keep_revisions: int = 2,
    max_polls: int | None = None,
) -> None:
    '''*!*! Export pending feature or annotation changes at either threshold.'''

    if poll_seconds <= 0:
        raise ValueError('poll_seconds must be greater than zero')
    if interval_seconds <= 0:
        raise ValueError('interval_seconds must be greater than zero')
    if edit_threshold <= 0:
        raise ValueError('edit_threshold must be greater than zero')
    if keep_revisions < 1:
        raise ValueError('keep_revisions must be at least one')

    # *!*! Export once at startup so the mounted directory always reflects the
    # *!*! database even when files were removed while Compose was stopped.
    exported_state = read_export_change_state(config)
    print_results(export_project(
        config,
        output_directory,
        keep_revisions=keep_revisions,
    ))
    last_export_at = time.monotonic()
    print(
        f'Watching PostGIS every {poll_seconds:g}s; exporting every '
        f'{interval_seconds:g}s or after {edit_threshold} saved edits',
        flush=True,
    )

    changes_announced = False
    poll_count = 0
    while max_polls is None or poll_count < max_polls:
        time.sleep(poll_seconds)
        poll_count += 1
        current_state = read_export_change_state(config)
        if current_state == exported_state:
            changes_announced = False
            continue
        edit_count = edit_count_since(exported_state, current_state)
        elapsed = time.monotonic() - last_export_at
        if not changes_announced:
            print(
                f'Saved changes pending; next export occurs after '
                f'{interval_seconds:g}s or {edit_threshold} saved edits',
                flush=True,
            )
            changes_announced = True
        if edit_count >= edit_threshold or elapsed >= interval_seconds:
            layers = changed_layer_ids(exported_state, current_state)
            annotations_changed = (
                current_state.annotations != exported_state.annotations
            )
            print_results(export_project(
                config,
                output_directory,
                layer_ids=layers,
                include_annotations=annotations_changed,
                keep_revisions=keep_revisions,
            ))
            # *!*! Keep the pre-export token. An edit racing with the snapshot
            # *!*! will therefore differ on the next poll and cannot be missed.
            exported_state = current_state
            last_export_at = time.monotonic()
            changes_announced = False


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line options for local layer export.'''

    parser = argparse.ArgumentParser(
        description='Export PostGIS snapshots and publish to the R2 destination in YAML.',
    )
    parser.add_argument(
        '--yaml',
        required=True,
        type=Path,
        help='Layer-aware project YAML used to identify the database project.',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        help='Snapshot directory. Defaults to tmp beside the project YAML.',
    )
    parser.add_argument(
        '--watch',
        action='store_true',
        help='Keep running and export automatically after saved changes.',
    )
    parser.add_argument(
        '--poll-seconds',
        type=float,
        default=float(os.environ.get('EXPORT_POLL_SECONDS', '2')),
        help='Seconds between database checks in watch mode. Default: 2.',
    )
    parser.add_argument(
        '--interval-seconds',
        type=float,
        default=float(os.environ.get('EXPORT_INTERVAL_SECONDS', '300')),
        help='Maximum seconds between exports with pending edits. Default: 300.',
    )
    parser.add_argument(
        '--edit-threshold',
        type=int,
        default=int(os.environ.get('EXPORT_EDIT_THRESHOLD', '50')),
        help='Saved edits that trigger an export before the timer. Default: 50.',
    )
    parser.add_argument(
        '--keep-revisions',
        type=int,
        default=int(os.environ.get('EXPORT_REVISIONS_TO_KEEP', '2')),
        help='Generated revisions retained per stream. Default: 2.',
    )
    return parser


def main() -> None:
    '''*!*! Export the configured project and print every local snapshot path.'''

    args = build_parser().parse_args()
    config = load_review_config(args.yaml)
    if args.watch:
        watch_project(
            config,
            args.output_dir,
            poll_seconds=args.poll_seconds,
            interval_seconds=args.interval_seconds,
            edit_threshold=args.edit_threshold,
            keep_revisions=args.keep_revisions,
        )
        return
    print_results(export_project(
        config,
        args.output_dir,
        keep_revisions=args.keep_revisions,
    ))


if __name__ == '__main__':
    main()
