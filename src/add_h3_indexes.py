#!/usr/bin/env python
'''Add H3 index columns to polygon feature data.

Example:
    python src/add_h3_indexes.py --input path/to/features.geojson --output path/to/features_h3.geojson

By default, this uses the current damage-map building files from the sibling
damage-map-web-map project.
'''

from __future__ import annotations

import argparse
from pathlib import Path
import site
import sys

user_site = site.getusersitepackages()
if isinstance(user_site, str):
    user_site = [user_site]
sys.path = [
    path for path in sys.path
    if path not in user_site and not path.startswith(f'{site.USER_BASE}/')
]

DEFAULT_INPUT = Path('../damage-map-web-map/data/building_with_inference.geojson')
DEFAULT_OUTPUT = Path('../damage-map-web-map/data/buildings_h3.geojson')
DEFAULT_RESOLUTIONS = '5,6,7,8,9,10'


def parse_resolutions(value: str) -> list[int]:
    '''Parse a comma-separated H3 resolution list.'''
    resolutions = [int(part.strip()) for part in value.split(',') if part.strip()]
    invalid = [resolution for resolution in resolutions if resolution < 0 or resolution > 15]
    if invalid:
        raise argparse.ArgumentTypeError(
            f'H3 resolutions must be between 0 and 15: {invalid}'
        )
    return resolutions


def read_features(paths: list[Path]) -> gpd.GeoDataFrame:
    '''Read and combine one or more vector files as EPSG:4326 features.'''
    import geopandas as gpd
    import pandas as pd

    frames = []
    for path in paths:
        frame = gpd.read_file(path)
        if frame.crs is None:
            frame = frame.set_crs('EPSG:4326')
        frames.append(frame.to_crs('EPSG:4326'))

    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs='EPSG:4326')


def add_h3_columns(
    features: gpd.GeoDataFrame,
    resolutions: list[int],
) -> gpd.GeoDataFrame:
    '''Add one H3 cell column for each requested resolution.'''
    import h3

    features = features.copy()
    points = features.geometry.representative_point()
    valid_points = ~(points.is_empty | points.isna())

    for resolution in resolutions:
        column = f'h3_r{resolution}'
        features[column] = None
        features.loc[valid_points, column] = [
            h3.latlng_to_cell(point.y, point.x, resolution)
            for point in points.loc[valid_points]
        ]

    return features


def dedupe_features(
    features: gpd.GeoDataFrame,
    dedupe_field: str | None,
) -> gpd.GeoDataFrame:
    '''Drop duplicate rows, keeping the last copy of each feature.'''
    if not dedupe_field:
        return features

    if dedupe_field not in features.columns:
        raise ValueError(f'Cannot dedupe: field does not exist: {dedupe_field}')

    return features.drop_duplicates(subset=dedupe_field, keep='last')


def write_geojson(features: gpd.GeoDataFrame, output_path: Path) -> None:
    '''Write processed features as GeoJSON.'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    features.to_file(output_path, driver='GeoJSON')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Add H3 index columns to polygon feature data.'
    )
    parser.add_argument(
        '--input',
        nargs='+',
        type=Path,
        default=[DEFAULT_INPUT],
        help=f'Input vector file(s). Default: {DEFAULT_INPUT}',
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f'Output GeoJSON file. Default: {DEFAULT_OUTPUT}',
    )
    parser.add_argument(
        '--resolutions',
        type=parse_resolutions,
        default=parse_resolutions(DEFAULT_RESOLUTIONS),
        help=f'Comma-separated H3 resolutions. Default: {DEFAULT_RESOLUTIONS}',
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite the output instead of appending to it when it exists.',
    )
    parser.add_argument(
        '--dedupe-field',
        default='id',
        help='Field used to dedupe appended data. Use an empty string to disable.',
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    input_paths = list(args.input)
    if args.output.exists() and not args.overwrite:
        input_paths = [args.output, *input_paths]

    features = read_features(input_paths)
    features = dedupe_features(features, args.dedupe_field or None)
    features = add_h3_columns(features, args.resolutions)
    write_geojson(features, args.output)

    print(
        f'Wrote {len(features):,} features with '
        f'{len(args.resolutions)} H3 index columns to {args.output}'
    )


if __name__ == '__main__':
    main()
