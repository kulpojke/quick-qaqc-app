#!/usr/bin/env python
'''*!*! Merge the pre-fire lidar buildings into the Camp Overture buildings.

The lidar buildings come from the detection repository's run
runs2/camp_fire_prefire (3DEP lidar flown in July 2018, before the fire). They
take precedence: an Overture or hand-added building that shares area with a
lidar building is dropped, and the lidar building lists its ID in `replaces`.
The other Overture and added buildings are kept unchanged, with their IDs;
among them all of western Paradise, which has no pre-fire lidar.

Lidar buildings get IDs in the same layout as the Overture IDs, with 11da in
place of 0200 (Overture) or 9999 (added by hand):

    0 + H3 res-11 cell (15 hex) | 11da | 12 hex from a hash of the geometry

Replacements where the two share less than half of either one's area are in
the grey zone: those lidar buildings get `grey_zone` true, and the pairs are
written to a separate GeoPackage, to check by eye.

One-time script; run from the repo root:
    python -s data/merge_camp_lidar.py
then convert with
    python -s src/geojson2parquet.py data/camp_buildings_lidar2018_overture2024.geojson
'''

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.fetch_overture_buildings import cog_bounds_wgs84  # noqa: E402
from src.project_config import load_review_config  # noqa: E402


BASE = Path('data/camp_overture_2024-02-15-alpha-0_with_added.geojson')
RUN = Path('../detection/runs2/camp_fire_prefire')
LIDAR = RUN / 'buildings.gpkg'
FLOWN = RUN / 'flown.gpkg'
CONFIG = Path('camp_config.yaml')
OUTPUT = Path('data/camp_buildings_lidar2018_overture2024.geojson')
GREY_ZONE = Path('data/camp_buildings_lidar2018_overture2024_greyzone.gpkg')
# The lidar's CRS, NAD83(2011) / UTM zone 10N: areas in m².
METRIC_CRS = 'EPSG:6339'
H3_RESOLUTIONS = [5, 6, 7, 8, 9, 10]
LIDAR_MARKER = '11da'
ORIGINS = {'0200': 'overture', '9999': 'added', LIDAR_MARKER: 'lidar'}
# Lidar footprints are rounded to this many degrees (about 1 cm), so the
# GeoJSON holds them exactly and their IDs can be checked from the file.
PRECISION = 1e-7
# m²: less than this is two footprints touching (after reprojection), not
# sharing area.
MIN_SHARED_AREA = 0.01
# A replacement is in the grey zone when the two share less than this of
# each one's area.
GREY_SHARE = 0.5
COLUMNS = [
    'id', 'origin', 'prefire_lidar', 'replaces', 'grey_zone',
    'lidar_class', 'lidar_height', 'lidar_area', 'lidar_tree_notch',
    'lidar_naip_green', 'lidar_fid',
    *[f'h3_r{resolution}' for resolution in H3_RESOLUTIONS],
    'geometry',
]


def read_base(path: Path) -> gpd.GeoDataFrame:
    '''*!*! Read the Overture and added buildings, with their origin from the ID.'''
    base = gpd.read_file(path)
    base['origin'] = base['id'].str[16:20].map(ORIGINS)
    if base['origin'].isna().any() or (base['origin'] == 'lidar').any():
        raise ValueError(f'Unexpected ID markers in {path}')
    return base


def read_lidar(path: Path, bounds_wgs84: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    '''*!*! Read the lidar buildings that intersect the COG bounds, in EPSG:4326.

    Bootstrap keeps the features that intersect the COG bounds, so the same
    test is used here. Geometry is rounded to PRECISION and made valid.
    '''
    lidar = gpd.read_file(path, fid_as_index=True)
    metric = lidar.geometry.area
    lidar = lidar.to_crs('EPSG:4326')
    lidar = lidar[lidar.intersects(box(*bounds_wgs84))].copy()
    rounded = lidar.geometry.set_precision(PRECISION).make_valid()
    # set_precision leaves its grid on the geometry, and the representative
    # point (ID, H3) would be snapped to it too; the file won't hold the grid,
    # so it is dropped here, through WKB.
    lidar['geometry'] = gpd.GeoSeries.from_wkb(rounded.to_wkb(), index=lidar.index, crs=lidar.crs)
    lidar['lidar_fid'] = lidar.index.astype('Int64')
    lidar['lidar_class'] = lidar['building'].map({1: 'building', 2: 'shed'}).astype('string')
    lidar['lidar_height'] = lidar['height'].round(1).astype('Float64')
    lidar['lidar_area'] = metric[lidar.index].round(1).astype('Float64')
    lidar['lidar_tree_notch'] = (lidar['review'] == 1).astype('boolean')
    lidar['lidar_naip_green'] = (lidar['naip_flag'] == 1).astype('boolean')
    lidar['origin'] = 'lidar'
    lidar['prefire_lidar'] = True
    return lidar.reset_index(drop=True)


def shared_areas(base: gpd.GeoDataFrame, lidar: gpd.GeoDataFrame) -> pd.DataFrame:
    '''*!*! Every (base, lidar) pair that shares area: the area in m² and each one's share of it.'''
    base_m = base.to_crs(METRIC_CRS).geometry
    lidar_m = lidar.to_crs(METRIC_CRS).geometry
    pairs = gpd.sjoin(
        gpd.GeoDataFrame(geometry=base_m), gpd.GeoDataFrame(geometry=lidar_m), predicate='intersects'
    )
    pairs = pd.DataFrame({'base': pairs.index.to_numpy(), 'lidar': pairs['index_right'].to_numpy()})
    shared = shapely.intersection(np.asarray(base_m)[pairs['base']], np.asarray(lidar_m)[pairs['lidar']])
    pairs['shared'] = shapely.area(shared)
    pairs = pairs[pairs['shared'] > MIN_SHARED_AREA].reset_index(drop=True)
    pairs['of_base'] = pairs['shared'] / base_m.area.to_numpy()[pairs['base']]
    pairs['of_lidar'] = pairs['shared'] / lidar_m.area.to_numpy()[pairs['lidar']]
    return pairs


def make_id(geometry) -> str:
    '''*!*! Build an Overture-style ID from the geometry's location and shape.'''
    point = geometry.representative_point()
    cell = h3.latlng_to_cell(point.y, point.x, 11)
    tail = hashlib.sha1(geometry.wkb).hexdigest()[:12]
    return f'{cell:0>16}{LIDAR_MARKER}{tail}'


def add_ids_and_h3(lidar: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    '''*!*! Fill the id and h3_r* columns the same way the Overture file has them.'''
    lidar = lidar.copy()
    points = lidar.geometry.representative_point()
    lidar['id'] = [make_id(geometry) for geometry in lidar.geometry]
    for resolution in H3_RESOLUTIONS:
        lidar[f'h3_r{resolution}'] = [
            h3.latlng_to_cell(point.y, point.x, resolution) for point in points
        ]
    return lidar


def prefire_lidar(features: gpd.GeoDataFrame, flown_path: Path) -> pd.Series:
    '''*!*! Whether each feature's representative point was flown before the fire.

    flown.gpkg's `period` is 'before', 'after' or 'both' (along the edge
    between two flights); 'both' counts as before.
    '''
    flown = gpd.read_file(flown_path)
    before = flown[flown['period'].isin(['before', 'both'])].to_crs('EPSG:4326').union_all()
    return pd.Series(features.representative_point().within(before), index=features.index).astype('boolean')


def grey_zone(pairs: pd.DataFrame) -> pd.DataFrame:
    '''*!*! The replacements where the two share less than GREY_SHARE of each one's area.'''
    return pairs[(pairs['of_base'] < GREY_SHARE) & (pairs['of_lidar'] < GREY_SHARE)]


def write_grey_zone(path: Path, base: gpd.GeoDataFrame, lidar: gpd.GeoDataFrame, grey: pd.DataFrame) -> None:
    '''*!*! Write the grey-zone replacements to check by eye.

    Layer `replaced`: the dropped Overture or added building, with the lidar
    building that replaced it and both shares. Layer `lidar`: those lidar
    buildings.
    '''
    replaced = base.loc[grey['base'], ['id', 'origin', 'geometry']].reset_index(drop=True)
    replaced['lidar_id'] = lidar['id'].to_numpy()[grey['lidar']]
    replaced['share_of_replaced'] = grey['of_base'].round(3).to_numpy()
    replaced['share_of_lidar'] = grey['of_lidar'].round(3).to_numpy()
    replaced['shared_m2'] = grey['shared'].round(1).to_numpy()
    path.unlink(missing_ok=True)
    replaced.to_file(path, layer='replaced', driver='GPKG')
    lidar.loc[np.unique(grey['lidar']), ['id', 'replaces', 'lidar_class', 'geometry']].to_file(
        path, layer='lidar', driver='GPKG'
    )


def main() -> None:
    bounds_wgs84 = cog_bounds_wgs84(load_review_config(CONFIG).imagery_cog)
    base = read_base(BASE)
    lidar = add_ids_and_h3(read_lidar(LIDAR, bounds_wgs84))
    print(f'Read {len(base)} Overture and added buildings and {len(lidar)} lidar buildings in the COG bounds')

    pairs = shared_areas(base, lidar)
    replaced_ids = base['id'].to_numpy()[pairs['base']]
    replaces = pd.Series(replaced_ids).groupby(pairs['lidar'].to_numpy()).agg(lambda ids: ','.join(sorted(set(ids))))
    lidar['replaces'] = replaces.reindex(lidar.index).astype('string')
    replaced = np.unique(pairs['base'])
    print(f'{len(replaced)} Overture and added buildings share area with {lidar["replaces"].notna().sum()} '
          f'lidar buildings, which replace them')
    grey = grey_zone(pairs)
    lidar['grey_zone'] = lidar.index.isin(grey['lidar'])
    write_grey_zone(GREY_ZONE, base, lidar, grey)
    print(f'{len(grey)} replacements in the grey zone, by {lidar["grey_zone"].sum()} lidar buildings: {GREY_ZONE}')

    kept = base.drop(index=replaced)
    kept['prefire_lidar'] = prefire_lidar(kept, FLOWN)
    kept['grey_zone'] = False
    for column in COLUMNS:
        if column not in kept:
            kept[column] = None
    combined = gpd.GeoDataFrame(pd.concat([kept[COLUMNS], lidar[COLUMNS]], ignore_index=True), crs='EPSG:4326')
    combined = combined.astype({
        'replaces': 'string', 'lidar_class': 'string', 'lidar_height': 'Float64', 'lidar_area': 'Float64',
        'lidar_tree_notch': 'boolean', 'lidar_naip_green': 'boolean', 'lidar_fid': 'Int64',
        'prefire_lidar': 'boolean', 'grey_zone': 'boolean',
    })

    if combined['id'].duplicated().any():
        raise ValueError('Duplicate IDs in the combined buildings')

    combined.to_file(OUTPUT, driver='GeoJSON')
    counts = combined['origin'].value_counts()
    print(f'Wrote {len(combined)} buildings ({", ".join(f"{n} {o}" for o, n in counts.items())}) to {OUTPUT}')


if __name__ == '__main__':
    main()
