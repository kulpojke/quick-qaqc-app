#!/usr/bin/env python
'''*!*! Append hand-digitised Camp footprints to the Overture buildings.

New buildings get IDs in the same layout as the 2024-02-15-alpha-0 Overture
IDs, with 9999 in place of 0200 to mark them as additions:

    0 + H3 res-11 cell (15 hex) | 9999 | 12 hex from a hash of the geometry

Any new building that shares area with an Overture building is dropped, as
are the added footprints listed in DROP_IDS.

One-time script; run from the repo root:
    python -s data/add_camp_footprints.py
'''

from __future__ import annotations

import hashlib
from pathlib import Path

import geopandas as gpd
import h3
import pandas as pd


OVERTURE = Path('data/camp_overture_2024-02-15-alpha-0.geojson')
ADDED = [
    Path('data/camp_added_foot/CAMP_AddedFootprints_SF.shp'),
    Path('data/camp_added_foot/CAMP_AddedFootprints_SP.shp'),
]
OUTPUT = Path('data/camp_overture_2024-02-15-alpha-0_with_added.geojson')
H3_RESOLUTIONS = [5, 6, 7, 8, 9, 10]
ADDITION_MARKER = '9999'
# Added footprints rejected by eye.
DROP_IDS = [
    # Duplicates of 08b2814a03d30fff9999bed82f5e2daa, which is kept.
    '08b2814a03d30fff99996c8648ed52b4',
    '08b2814a03d30fff9999f910621f4876',
    # An overlapping pair of poor-quality duplicates; neither is kept.
    '08b2814a2e7aafff9999c05365c4d96b',
    '08b2814a2e7aefff9999aeb1782e91f7',
]


def read_added(paths: list[Path]) -> gpd.GeoDataFrame:
    '''*!*! Read the added footprints as valid EPSG:4326 polygons, geometry only.'''
    frames = [gpd.read_file(path)[['geometry']].to_crs('EPSG:4326') for path in paths]
    added = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs='EPSG:4326')
    # One SF polygon self-intersects; repair it so the overlap test is reliable.
    added['geometry'] = added.make_valid()
    return added


def drop_overlapping(added: gpd.GeoDataFrame, overture: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    '''*!*! Drop added buildings that share area with an Overture building.

    Touching along an edge does not count; only a non-zero intersection does.
    '''
    pairs = gpd.sjoin(added, overture[['geometry']], predicate='intersects')
    overlap_area = [
        added.geometry[i].intersection(overture.geometry[j]).area
        for i, j in zip(pairs.index, pairs['index_right'])
    ]
    overlapping = set(pairs.index[[area > 0 for area in overlap_area]])
    print(f'Dropping {len(overlapping)} added buildings that overlap Overture')
    return added.drop(index=list(overlapping))


def make_id(geometry) -> str:
    '''*!*! Build an Overture-style ID from the geometry's location and shape.'''
    point = geometry.representative_point()
    cell = h3.latlng_to_cell(point.y, point.x, 11)
    tail = hashlib.sha1(geometry.wkb).hexdigest()[:12]
    return f'{cell:0>16}{ADDITION_MARKER}{tail}'


def add_ids_and_h3(added: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    '''*!*! Fill the id and h3_r* columns the same way the Overture file has them.'''
    added = added.copy()
    points = added.geometry.representative_point()
    added['id'] = [make_id(geometry) for geometry in added.geometry]
    for resolution in H3_RESOLUTIONS:
        added[f'h3_r{resolution}'] = [
            h3.latlng_to_cell(point.y, point.x, resolution) for point in points
        ]
    return added


def main() -> None:
    overture = gpd.read_file(OVERTURE)
    added = read_added(ADDED)
    print(f'Read {len(overture)} Overture and {len(added)} added buildings')

    added = add_ids_and_h3(drop_overlapping(added, overture))
    rejected = added['id'].isin(DROP_IDS)
    if rejected.sum() != len(DROP_IDS):
        raise ValueError('Not every ID in DROP_IDS was found; did the inputs change?')
    print(f'Dropping {rejected.sum()} added buildings rejected by eye')
    added = added[~rejected]

    combined = pd.concat([overture, added[overture.columns]], ignore_index=True)

    if combined['id'].duplicated().any():
        raise ValueError('Duplicate IDs in the combined buildings')

    combined.to_file(OUTPUT, driver='GeoJSON')
    print(f'Wrote {len(combined)} buildings ({len(added)} added) to {OUTPUT}')


if __name__ == '__main__':
    main()
