#!/usr/bin/env python
'''*!*! Converts GeoJSON feature data to GeoParquet with DuckDB.'''

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


DATA_DIRECTORY = Path(__file__).resolve().parent.parent / 'data'
DEFAULT_INPUT = DATA_DIRECTORY / 'camp_overture_2024-02-15-alpha-0.geojson'


def quote_identifier(value: str) -> str:
    '''*!*! Quotes a SQL identifier after escaping embedded quote characters.'''

    quote = chr(34)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def quote_string(value: str | Path) -> str:
    '''*!*! Quotes a SQL string after escaping embedded apostrophes.'''

    quote = chr(39)
    text = str(value)
    return f'{quote}{text.replace(quote, quote * 2)}{quote}'


def open_spatial_connection():
    '''*!*! Opens DuckDB with its spatial extension loaded.'''

    import duckdb

    connection = duckdb.connect()
    try:
        connection.execute('LOAD spatial')
    except duckdb.Error:
        connection.execute('INSTALL spatial')
        connection.execute('LOAD spatial')
    return connection


def geometry_column(description) -> str:
    '''*!*! Returns the single geometry column in a DuckDB result schema.'''

    columns = [
        name
        for name, data_type, *_ in description
        if str(data_type).startswith('GEOMETRY')
    ]
    if not columns:
        raise ValueError('The input GeoJSON has no geometry column')
    if len(columns) > 1:
        raise ValueError(f'The input GeoJSON has multiple geometry columns: {columns}')
    return columns[0]


def convert_geojson_to_geoparquet(
    input_path: Path,
    output_path: Path,
    *,
    overwrite: bool = False,
) -> int:
    '''*!*! Converts one GeoJSON to an atomic, Zstandard-compressed GeoParquet.'''

    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f'Input GeoJSON not found: {input_path}')
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f'Output already exists: {output_path}; pass --overwrite to replace it'
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}.',
        suffix='.parquet',
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()

    connection = open_spatial_connection()
    try:
        connection.execute(
            'CREATE TEMP TABLE source_features AS SELECT * FROM ST_Read(?)',
            [str(input_path)],
        )
        description = connection.execute(
            'SELECT * FROM source_features LIMIT 0'
        ).description
        source_geometry = geometry_column(description)
        property_columns = [
            name
            for name, *_ in description
            if name != source_geometry and name.lower() != 'ogc_fid'
        ]
        selections = [quote_identifier(name) for name in property_columns]
        geometry_alias = quote_identifier('geometry')
        selections.append(
            f'{quote_identifier(source_geometry)} AS {geometry_alias}'
        )
        select_sql = ',\n    '.join(selections)
        connection.execute(
            f'''
COPY (
    SELECT
        {select_sql}
    FROM source_features
) TO {quote_string(temporary_path)} (
    FORMAT PARQUET,
    COMPRESSION ZSTD
)
'''
        )
        source_count = connection.execute(
            'SELECT count(*) FROM source_features'
        ).fetchone()[0]
        output_count = connection.execute(
            'SELECT count(*) FROM read_parquet(?)',
            [str(temporary_path)],
        ).fetchone()[0]
        if output_count != source_count:
            raise RuntimeError(
                f'Row-count mismatch: source has {source_count}, output has {output_count}'
            )
        os.replace(temporary_path, output_path)
        return output_count
    finally:
        connection.close()
        temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Builds command-line options for GeoJSON conversion.'''

    parser = argparse.ArgumentParser(
        description='Convert a GeoJSON file to Zstandard-compressed GeoParquet.',
    )
    parser.add_argument(
        'input',
        nargs='?',
        type=Path,
        default=DEFAULT_INPUT,
        help=f'Input GeoJSON. Default: {DEFAULT_INPUT}',
    )
    parser.add_argument(
        '--output',
        type=Path,
        help='Output GeoParquet. Defaults to the input path with a .parquet suffix.',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Replace the output when it already exists.',
    )
    return parser


def main() -> None:
    '''*!*! Converts the requested file and reports the resulting snapshot.'''

    args = build_parser().parse_args()
    output_path = args.output or args.input.with_suffix('.parquet')
    count = convert_geojson_to_geoparquet(
        args.input,
        output_path,
        overwrite=args.overwrite,
    )
    print(f'Wrote {count:,} features to {output_path.expanduser().resolve()}')


if __name__ == '__main__':
    main()
