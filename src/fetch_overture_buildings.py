#!/usr/bin/env python
'''Fetches and caches Overture buildings covering a COG.'''

from __future__ import annotations

import argparse
import json
import os
import re
import site
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


user_site = site.getusersitepackages()
if isinstance(user_site, str):
    user_site = [user_site]
sys.path = [
    path for path in sys.path
    if path not in user_site and not path.startswith(f'{site.USER_BASE}/')
]

DEFAULT_H3_RESOLUTIONS = (5, 6, 7, 8, 9, 10)
FUSED_BASE = 's3://us-west-2.opendata.source.coop/fused/overture'
FUSED_RELEASE_INDEX = 'https://data.source.coop/fused/overture/?delimiter=/'
FUSED_PARTITION_CHANGE = date(2025, 1, 22)
CACHE_VERSION = 1
RELEASE_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2})(?:-[^/]+)+$')


@dataclass(frozen=True)
class FetchResult:
    '''Dataclass describing the local feature cache returned by a fetch operation.'''

    path: Path
    feature_count: int
    from_cache: bool
    bounds_wgs84: tuple[float, float, float, float]
    release: str


def cog_bounds_wgs84(cog_source: str) -> tuple[float, float, float, float]:
    '''Reads COG bounds and transform them to EPSG:4326.'''

    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(cog_source) as dataset:
        if dataset.crs is None:
            raise ValueError(f'COG has no CRS: {cog_source}')
        bounds = transform_bounds(
            dataset.crs,
            'EPSG:4326',
            *dataset.bounds,
            densify_pts=21,
        )
    return tuple(float(value) for value in bounds)


def release_date(release: str) -> date:
    '''Extracts the publication date from a Fused release name.'''

    match = RELEASE_PATTERN.fullmatch(release)
    if match is None:
        raise ValueError(f'Unrecognized Fused release name: {release}')
    return date.fromisoformat(match.group(1))


def discover_fused_releases() -> tuple[str, ...]:
    '''Returns sorted tuple of dated releases advertised by the Fused public bucket.'''

    request = Request(
        FUSED_RELEASE_INDEX,
        headers={
            'Accept': 'application/xml',
            'User-Agent': 'damagemap-qaqc/1.0',
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            root = ElementTree.fromstring(response.read())
    except (OSError, URLError, ElementTree.ParseError) as error:
        raise RuntimeError(f'Could not list Fused Overture releases: {error}') from error

    releases = []
    for element in root.findall('.//{*}CommonPrefixes/{*}Prefix'):
        prefix = element.text or ''
        parts = prefix.strip('/').split('/')
        if len(parts) != 2 or parts[0] != 'overture':
            continue
        try:
            release_date(parts[1])
        except ValueError:
            continue
        releases.append(parts[1])
    if not releases:
        raise RuntimeError('The Fused Overture release listing contained no dated releases')
    return tuple(sorted(set(releases), key=lambda value: (release_date(value), value)))


def select_release_before(fire_date: date, releases: tuple[str, ...]) -> str:
    '''Selects the most recent available Fused release before a fire date.'''

    candidates = [release for release in releases if release_date(release) < fire_date]
    if not candidates:
        earliest = min((release_date(release) for release in releases), default=None)
        suffix = f'; earliest available release is {earliest}' if earliest else ''
        raise ValueError(f'No Fused Overture release predates {fire_date}{suffix}')
    return max(candidates, key=lambda value: (release_date(value), value))


def select_fused_release(
    fire_date: date,
    releases: tuple[str, ...],
    release_selection: str,
) -> str:
    '''Selects a release using the pre-fire or oldest strategy given in config'''

    if release_selection == 'before_fire':
        return select_release_before(fire_date, releases)
    if release_selection == 'oldest':
        if not releases:
            raise ValueError('No Fused Overture releases are available')
        return min(releases, key=lambda value: (release_date(value), value))
    raise ValueError(f'Unsupported Fused release selection: {release_selection}')


def fused_building_paths(release: str) -> list[str]:
    '''Returns remote Parquet globs for one mirrored Overture release.'''

    base = f'{FUSED_BASE}/{release}/theme=buildings/type=building'
    partition_count = 6 if release_date(release) >= FUSED_PARTITION_CHANGE else 5
    return [f'{base}/part={part}/**/*.parquet' for part in range(partition_count)]


def open_fused_connection():
    '''Opens DuckDB with HTTP/S3 access configured for the Fused mirror.'''

    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            'DuckDB is required to fetch Overture buildings; update the Conda environment'
        ) from error

    connection = duckdb.connect()
    try:
        try:
            connection.execute('LOAD httpfs')
        except duckdb.Error:
            connection.execute('INSTALL httpfs')
            connection.execute('LOAD httpfs')
        connection.execute('SET s3_region = ?', ['us-west-2'])
        connection.execute('SET s3_endpoint = ?', ['s3.us-west-2.amazonaws.com'])
        connection.execute('SET s3_url_style = ?', ['path'])
    except duckdb.Error as error:
        connection.close()
        raise RuntimeError(f'Could not configure DuckDB for Fused: {error}') from error
    return connection


def bbox_field_names(connection, parquet_paths: list[str]) -> tuple[str, str, str, str]:
    '''*!*! Detects bbox field names used by an Overture Parquet schema.'''

    connection.execute(
        '''
SELECT bbox
FROM read_parquet(?, hive_partitioning = 1, union_by_name = true)
LIMIT 0
''',
        [parquet_paths],
    )
    bbox_type = str(connection.description[0][1]).lower()
    for fields in (
        ('xmin', 'xmax', 'ymin', 'ymax'),
        ('minx', 'maxx', 'miny', 'maxy'),
    ):
        if all(f'{field} ' in bbox_type for field in fields):
            return fields
    raise RuntimeError(f'Unsupported Overture bbox schema: {bbox_type}')


def query_fused_buildings(
    release: str,
    bounds_wgs84: tuple[float, float, float, float],
):
    '''
    Uses duckdb to Query building IDs and WKB geometries intersecting a WGS84
    bbox.
    '''

    import duckdb

    minx, miny, maxx, maxy = bounds_wgs84
    parquet_paths = fused_building_paths(release)
    connection = open_fused_connection()
    try:
        xmin_field, xmax_field, ymin_field, ymax_field = bbox_field_names(
            connection,
            parquet_paths,
        )
        sql = f'''
SELECT id, geometry
FROM read_parquet(?, hive_partitioning = 1, union_by_name = true)
WHERE bbox.{xmax_field} >= ? AND bbox.{xmin_field} <= ?
  AND bbox.{ymax_field} >= ? AND bbox.{ymin_field} <= ?
'''
        return connection.execute(
            sql,
            [parquet_paths, minx, maxx, miny, maxy],
        ).fetch_df()
    except duckdb.Error as error:
        raise RuntimeError(
            f'Could not query Fused Overture release {release}: {error}'
        ) from error
    finally:
        connection.close()


def building_geodataframe(
    rows,
    bounds_wgs84: tuple[float, float, float, float],
    *,
    h3_prefix: str,
    h3_resolutions: tuple[int, ...],
):
    '''Converts queried WKB rows to polygons with H3 indexes.'''

    import geopandas as gpd
    from shapely.geometry import box

    from src.add_h3_indexes import add_h3_columns

    if rows.empty:
        raise ValueError('No Overture buildings intersect the COG bounds')

    rows = rows.copy()
    wkb = rows.pop('geometry').map(
        lambda value: bytes(value) if isinstance(value, (bytearray, memoryview)) else value
    )
    geometry = gpd.GeoSeries.from_wkb(wkb, crs='EPSG:4326')
    buildings = gpd.GeoDataFrame(rows, geometry=geometry, crs='EPSG:4326')
    buildings = buildings[
        buildings.geometry.notna()
        & ~buildings.geometry.is_empty
        & buildings.geometry.intersects(box(*bounds_wgs84))
    ].reset_index(drop=True)
    if buildings.empty:
        raise ValueError('No valid Overture building geometries intersect the COG bounds')

    return add_h3_columns(
        buildings,
        list(h3_resolutions),
        prefix=h3_prefix,
    )


def cache_metadata_path(output_path: Path) -> Path:
    '''Returns the sidecar path used to validate a feature cache.'''

    return output_path.with_suffix(f'{output_path.suffix}.overture.json')


def cache_signature(
    *,
    cog_source: str,
    fire_date: date,
    release_selection: str,
    release: str,
    bounds_wgs84: tuple[float, float, float, float],
    h3_prefix: str,
    h3_resolutions: tuple[int, ...],
) -> dict:
    '''Builds dict of json ready inputs used to identify a matching cache.'''

    return {
        'version': CACHE_VERSION,
        'cog_source': cog_source,
        'fire_date': fire_date.isoformat(),
        'release_selection': release_selection,
        'release': release,
        'bounds_wgs84': [round(value, 12) for value in bounds_wgs84],
        'h3_prefix': h3_prefix,
        'h3_resolutions': list(h3_resolutions),
        'fused_base': FUSED_BASE,
    }


def read_matching_cache(
    output_path: Path,
    expected_inputs: dict,
) -> FetchResult | None:
    '''*!*! Return cache details when output and sidecar match the inputs.'''

    metadata_path = cache_metadata_path(output_path)
    if not output_path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    signature = metadata.get('signature')
    if not isinstance(signature, dict):
        return None
    release = signature.get('release')
    inputs = {key: value for key, value in signature.items() if key != 'release'}
    if inputs != expected_inputs or not isinstance(release, str):
        return None
    try:
        selected_date = release_date(release)
        fire_date = date.fromisoformat(expected_inputs['fire_date'])
        if (
            expected_inputs['release_selection'] == 'before_fire'
            and selected_date >= fire_date
        ):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return FetchResult(
        path=output_path,
        feature_count=int(metadata.get('feature_count', 0)),
        from_cache=True,
        bounds_wgs84=tuple(signature['bounds_wgs84']),
        release=release,
    )


def atomic_write_geojson(buildings, output_path: Path) -> None:
    '''*!*! Replace a GeoJSON only after its temporary output is complete.'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}.',
        suffix='.geojson',
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink(missing_ok=True)
    try:
        buildings.to_file(temporary_path, driver='GeoJSON')
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(value: dict, output_path: Path) -> None:
    '''*!*! Atomically write JSON metadata beside the feature cache.'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        'w',
        dir=output_path.parent,
        encoding='utf-8',
        delete=False,
    ) as file:
        temporary_path = Path(file.name)
        json.dump(value, file, indent=2)
        file.write('\n')
    try:
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def ensure_overture_buildings(
    *,
    cog_source: str,
    fire_date: date,
    output_path: Path,
    h3_prefix: str = 'h3_r',
    h3_resolutions: tuple[int, ...] = DEFAULT_H3_RESOLUTIONS,
    release_selection: str = 'before_fire',
    refresh: bool = False,
) -> FetchResult:
    '''*!*! Return a matching cache or fetch app-ready Overture buildings.'''

    if not cog_source:
        raise ValueError('An imagery COG is required to derive Overture query bounds')
    if release_selection not in {'before_fire', 'oldest'}:
        raise ValueError(f'Unsupported Fused release selection: {release_selection}')

    bounds_wgs84 = cog_bounds_wgs84(cog_source)
    expected_inputs = cache_signature(
        cog_source=cog_source,
        fire_date=fire_date,
        release_selection=release_selection,
        release='',
        bounds_wgs84=bounds_wgs84,
        h3_prefix=h3_prefix,
        h3_resolutions=h3_resolutions,
    )
    expected_inputs.pop('release')
    if not refresh:
        cached = read_matching_cache(output_path, expected_inputs)
        if cached is not None:
            return cached

    release = select_fused_release(
        fire_date,
        discover_fused_releases(),
        release_selection,
    )
    signature = {**expected_inputs, 'release': release}
    rows = query_fused_buildings(release, bounds_wgs84)
    buildings = building_geodataframe(
        rows,
        bounds_wgs84,
        h3_prefix=h3_prefix,
        h3_resolutions=h3_resolutions,
    )
    atomic_write_geojson(buildings, output_path)
    atomic_write_json(
        {
            'signature': signature,
            'feature_count': len(buildings),
            'fetched_at': datetime.now(timezone.utc).isoformat(),
        },
        cache_metadata_path(output_path),
    )
    return FetchResult(
        path=output_path,
        feature_count=len(buildings),
        from_cache=False,
        bounds_wgs84=bounds_wgs84,
        release=release,
    )


def parse_resolutions(value: str) -> tuple[int, ...]:
    '''*!*! Parse comma-separated H3 resolutions for the command line.'''

    resolutions = tuple(int(part.strip()) for part in value.split(',') if part.strip())
    if not resolutions or any(resolution < 0 or resolution > 15 for resolution in resolutions):
        raise argparse.ArgumentTypeError('H3 resolutions must be between 0 and 15')
    return resolutions


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line options for a standalone building fetch.'''

    parser = argparse.ArgumentParser(
        description='Fetch and cache Overture buildings covering a COG.',
    )
    parser.add_argument('--cog', required=True, help='Local COG path or HTTP(S) URL.')
    parser.add_argument(
        '--fire-date',
        required=True,
        type=date.fromisoformat,
        help='Fire date in YYYY-MM-DD format; the newest earlier release is selected.',
    )
    parser.add_argument('--output', required=True, type=Path, help='Output GeoJSON cache path.')
    parser.add_argument('--h3-prefix', default='h3_r')
    parser.add_argument(
        '--h3-resolutions',
        type=parse_resolutions,
        default=DEFAULT_H3_RESOLUTIONS,
    )
    parser.add_argument('--force', action='store_true', help='Ignore a matching cache.')
    return parser


def main() -> None:
    '''*!*! Fetch or reuse buildings and report the resulting local cache.'''

    args = build_parser().parse_args()
    result = ensure_overture_buildings(
        cog_source=args.cog,
        fire_date=args.fire_date,
        output_path=args.output.expanduser().resolve(),
        h3_prefix=args.h3_prefix,
        h3_resolutions=args.h3_resolutions,
        release_selection='before_fire',
        refresh=args.force,
    )
    action = 'Reused' if result.from_cache else 'Fetched'
    print(f'{action} {result.feature_count:,} buildings: {result.path}')
    print(f'Fused Overture release: {result.release}')
    print(f'COG bounds (EPSG:4326): {result.bounds_wgs84}')


if __name__ == '__main__':
    main()
