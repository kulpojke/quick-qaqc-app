#!/usr/bin/env python
'''*!*! Check data/camp_buildings_lidar2018_overture2024 against its inputs.

Recomputes what data/merge_camp_lidar.py should have done, where it can by
other means (an STRtree instead of a spatial join, its own ID and H3 code), and
compares. Prints PASS or FAIL per check and exits with 1 if any fails.

Run from the repo root after the merge and the GeoParquet conversion:
    python -s data/tests/check_camp_lidar_merge.py
'''

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import duckdb
import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import merge_camp_lidar as merge  # noqa: E402


PARQUET = merge.OUTPUT.with_suffix('.parquet')
failures: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    '''*!*! Print one check's result and remember a failure.'''
    print(f'{"PASS" if ok else "FAIL"}  {name}{"" if ok else f": {detail}"}')
    if not ok:
        failures.append(name)


def expected_id(geometry, marker: str) -> str:
    '''*!*! The ID the layout in the merge script's docstring gives a geometry.'''
    point = geometry.representative_point()
    cell = h3.latlng_to_cell(point.y, point.x, 11)
    return cell.rjust(16, '0') + marker + hashlib.sha1(geometry.wkb).hexdigest()[:12]


def shared_pairs(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    '''*!*! Positions (in a, in b) of the pairs that share more than MIN_SHARED_AREA, and that area.'''
    ia, ib = shapely.STRtree(b).query(a, predicate='intersects')
    shared = shapely.area(shapely.intersection(a[ia], b[ib]))
    keep = shared > merge.MIN_SHARED_AREA
    return ia[keep], ib[keep], shared[keep]


def plain(values) -> list:
    '''*!*! Values with every kind of missing value as None, for comparing two readers.'''
    return [None if v is None or (not isinstance(v, str) and pd.isna(v)) else v for v in values]


def main() -> None:
    out = gpd.read_file(merge.OUTPUT)
    base = gpd.read_file(merge.BASE)
    source = gpd.read_file(merge.LIDAR, fid_as_index=True)
    bounds_wgs84 = merge.cog_bounds_wgs84(merge.load_review_config(merge.CONFIG).imagery_cog)
    lidar = out[out['origin'] == 'lidar']
    kept = out[out['origin'] != 'lidar']
    print(f'{len(out)} buildings: {len(lidar)} lidar, {len(kept)} Overture or added')

    # IDs and geometry.
    ids = out['id']
    check('IDs are unique', not ids.duplicated().any(), f'{ids.duplicated().sum()} duplicates')
    check('IDs are 32 hex characters', ids.str.fullmatch('[0-9a-f]{32}').all())
    check('the ID marker matches origin', (ids.str[16:20].map(merge.ORIGINS) == out['origin']).all())
    check('columns are as listed', list(out.columns) == merge.COLUMNS, str(list(out.columns)))
    geom = out.geometry
    bad = ~geom.is_valid | geom.is_empty | ~geom.geom_type.isin(['Polygon', 'MultiPolygon'])
    check('geometries are valid, non-empty polygons', not bad.any(), f'{bad.sum()} bad')
    ok = [expected_id(g, merge.LIDAR_MARKER) == i for g, i in zip(lidar.geometry, lidar['id'])]
    check('lidar IDs follow from their geometry', all(ok), f'{ok.count(False)} differ')
    points = geom.representative_point()
    for resolution in merge.H3_RESOLUTIONS:
        cells = [h3.latlng_to_cell(p.y, p.x, resolution) for p in points]
        check(f'h3_r{resolution} is the cell of the representative point', (out[f'h3_r{resolution}'] == cells).all())

    # The Overture and added buildings: kept unchanged, or replaced.
    by_id = base.set_index('id')
    unchanged = kept['id'].isin(by_id.index).all() and all(
        shapely.equals_exact(g, by_id.geometry[i], 0) for g, i in zip(kept.geometry, kept['id'])
    )
    check('kept Overture and added buildings are unchanged (ID and geometry)', unchanged)
    replaced = {i for r in lidar['replaces'].dropna() for i in r.split(',')}
    check('every Overture or added building is kept or replaced',
          replaced | set(kept['id']) == set(base['id']),
          f'{len(set(base["id"]) - replaced - set(kept["id"]))} missing, '
          f'{len(replaced - set(base["id"]))} unknown IDs in replaces')
    check('no building is both kept and replaced', not replaced & set(kept['id']))

    # The lidar buildings: all of those in the COG bounds, once.
    in_bounds = source.to_crs('EPSG:4326').intersects(box(*bounds_wgs84))
    check('every lidar building in the COG bounds is here, once',
          sorted(lidar['lidar_fid']) == sorted(source.index[in_bounds]))
    classes = source.loc[lidar['lidar_fid'], 'building'].map({1: 'building', 2: 'shed'})
    check('lidar_class matches the lidar building code', (classes.to_numpy() == lidar['lidar_class'].to_numpy()).all())

    # replaces and grey_zone, recomputed from the geometry.
    lidar_m = np.asarray(lidar.to_crs(merge.METRIC_CRS).geometry)
    base_m = np.asarray(base.to_crs(merge.METRIC_CRS).geometry)
    il, ib, shared = shared_pairs(lidar_m, base_m)
    expected = {}
    for l, b in zip(il, ib):
        expected.setdefault(l, set()).add(base['id'].iloc[b])
    actual = [set(r.split(',')) if isinstance(r, str) else set() for r in lidar['replaces']]
    wrong = [k for k in range(len(lidar)) if actual[k] != expected.get(k, set())]
    check('replaces lists exactly the buildings each lidar building shares area with', not wrong,
          f'{len(wrong)} lidar buildings differ')
    grey = (shared / shapely.area(base_m[ib]) < merge.GREY_SHARE) & (shared / shapely.area(lidar_m[il]) < merge.GREY_SHARE)
    flagged = np.zeros(len(lidar), bool)
    flagged[il[grey]] = True
    check('grey_zone is set exactly where a replacement shares under half of both',
          (lidar['grey_zone'].to_numpy(bool) == flagged).all() and not kept['grey_zone'].any(),
          f'{(lidar["grey_zone"].to_numpy(bool) != flagged).sum()} lidar buildings differ')
    kept_m = np.asarray(kept.to_crs(merge.METRIC_CRS).geometry)
    check('no lidar building shares area with a kept building', len(shared_pairs(lidar_m, kept_m)[0]) == 0)

    # prefire_lidar.
    flown = gpd.read_file(merge.FLOWN)
    before = flown[flown['period'].isin(['before', 'both'])].to_crs(merge.METRIC_CRS)
    inside = shapely.STRtree(np.asarray(before.geometry)).query(
        np.asarray(kept.to_crs(merge.METRIC_CRS).representative_point()), predicate='within'
    )[0]
    expect = np.isin(np.arange(len(kept)), inside)
    check('prefire_lidar: all lidar buildings, and the kept ones flown before the fire',
          lidar['prefire_lidar'].all() and (kept['prefire_lidar'].to_numpy(bool) == expect).all())

    # The GeoParquet holds the same as the GeoJSON.
    connection = duckdb.connect()
    connection.execute('LOAD spatial')
    table = connection.execute(
        f"SELECT * EXCLUDE (geometry), ST_AsWKB(geometry) AS wkb FROM read_parquet('{PARQUET}')"
    ).df()
    check('the GeoParquet has the same rows, in order', table['id'].tolist() == out['id'].tolist())
    columns = [c for c in merge.COLUMNS if c != 'geometry']
    check('the GeoParquet has the same values',
          all(plain(table[c]) == plain(out[c]) for c in columns),
          ', '.join(c for c in columns if plain(table[c]) != plain(out[c])))
    same = shapely.equals_exact(shapely.from_wkb([bytes(w) for w in table['wkb']]), np.asarray(geom), 0)
    check('the GeoParquet has the same geometry', same.all(), f'{(~same).sum()} differ')

    print(f'{len(failures)} checks failed' if failures else 'All checks passed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
