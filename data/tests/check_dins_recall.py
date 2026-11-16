#!/usr/bin/env python
'''*!*! How many Camp Fire DINS points lie on or near a building of the merged layer.

CAL FIRE's DINS inspected the structures standing when the fire came, so the
share of its points with a building nearby measures how complete the building
layer is. Inside the COG bounds, per DAMAGE class and by whether the point has
pre-fire lidar, it prints the share of points inside a building and within 5,
10 and 20 m of one: of any building, and of a lidar building only. A report,
not a pass or fail: a DINS point can lie on the driveway, and one building can
hold several units.

Run from the repo root after the merge:
    python -s data/tests/check_dins_recall.py
'''

from __future__ import annotations

from pathlib import Path
import sys

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import merge_camp_lidar as merge  # noqa: E402


DISTANCES = [0, 5, 10, 20]  # m; 0 is inside a building


def read_dins(source: str, crs: str) -> gpd.GeoDataFrame:
    '''*!*! The Camp Fire's DINS points, with their DAMAGE class.'''
    connection = duckdb.connect()
    connection.execute('LOAD spatial')
    table = connection.execute(
        f"SELECT GLOBALID, DAMAGE, ST_AsWKB(geometry) AS wkb FROM read_parquet('{source}') "
        "WHERE INCIDENTNAME = 'Camp'"
    ).df()
    geometry = shapely.from_wkb([bytes(w) for w in table.pop('wkb')])
    return gpd.GeoDataFrame(table, geometry=geometry, crs=crs)


def nearest_distance(points: np.ndarray, buildings: np.ndarray) -> np.ndarray:
    '''*!*! Each point's distance to the nearest building, m.'''
    (ip, _), distance = shapely.STRtree(buildings).query_nearest(points, return_distance=True, all_matches=False)
    out = np.full(len(points), np.inf)
    out[ip] = distance
    return out


def table(dins: pd.DataFrame, column: str) -> pd.DataFrame:
    '''*!*! Per DAMAGE class: the number of points and the share within each distance.'''
    groups = dict(list(dins.groupby('DAMAGE'))) | {'all': dins}
    rows = {
        name: {'points': len(g)} | {f'{"in" if d == 0 else f"<= {d} m"}': f'{(g[column] <= d).mean():.0%}' for d in DISTANCES}
        for name, g in groups.items()
    }
    return pd.DataFrame(rows).T


def main() -> None:
    config = merge.load_review_config(merge.CONFIG)
    layer = next(layer for layer in config.layers if layer.id == 'dins')
    bounds_wgs84 = merge.cog_bounds_wgs84(config.imagery_cog)
    dins = read_dins(str(layer.source), layer.source_crs).to_crs('EPSG:4326')
    dins = dins[dins.within(box(*bounds_wgs84))].to_crs(merge.METRIC_CRS)
    buildings = gpd.read_file(merge.OUTPUT).to_crs(merge.METRIC_CRS)
    flown = gpd.read_file(merge.FLOWN).to_crs(merge.METRIC_CRS)
    before = flown[flown['period'].isin(['before', 'both'])].union_all()

    points = np.asarray(dins.geometry)
    dins['any'] = nearest_distance(points, np.asarray(buildings.geometry))
    dins['lidar'] = nearest_distance(points, np.asarray(buildings[buildings['origin'] == 'lidar'].geometry))
    dins['prefire'] = dins.within(before)
    print(f'{len(dins)} Camp Fire DINS points in the COG bounds, {dins["prefire"].sum()} where there is pre-fire lidar')
    with pd.option_context('display.width', 120):
        for prefire, name in ((True, 'with pre-fire lidar'), (False, 'without pre-fire lidar (western Paradise)')):
            part = dins[dins['prefire'] == prefire]
            print(f'\nPoints {name}: distance to any building')
            print(table(part, 'any'))
            if prefire:
                print(f'\nPoints {name}: distance to a lidar building')
                print(table(part, 'lidar'))


if __name__ == '__main__':
    main()
