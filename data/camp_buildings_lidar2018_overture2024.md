# camp_buildings_lidar2018_overture2024.geojson

Camp Fire building footprints: the buildings found in the pre-fire lidar
(3DEP, flown July 2018), plus the Overture and hand-digitised buildings of
`camp_overture_2024-02-15-alpha-0_with_added.geojson` that no lidar building
covers. Where both have a building, the lidar's is kept.

| | |
|---|---|
| Files | `data/camp_buildings_lidar2018_overture2024.geojson` and `.parquet` (same features); `data/camp_buildings_lidar2018_overture2024_greyzone.gpkg` (to check by eye) |
| Created | 2026-11-16 |
| Made by | `data/merge_camp_lidar.py` (run from the repo root: `python -s data/merge_camp_lidar.py`), then `python -s src/geojson2parquet.py data/camp_buildings_lidar2018_overture2024.geojson` |
| Checked by | `python -s data/tests/check_camp_lidar_merge.py` (all checks pass) and `python -s data/tests/check_dins_recall.py` (a report) |
| CRS | EPSG:4326 |
| Features | 16,677: 15,517 lidar, 780 Overture, 380 added |
| Geometry | Polygon / MultiPolygon |
| Bounds | -121.6946, 39.7158, -121.5347, 39.8529 |

## Inputs

| Input | Features | Notes |
|---|---|---|
| `data/camp_overture_2024-02-15-alpha-0_with_added.geojson` | 3,146 | Overture 2024-02-15-alpha-0 (2,523) and hand-digitised (623) buildings; see its `.md`. |
| `../detection/runs2/camp_fire_prefire/buildings.gpkg` | 23,192 | Buildings from the 3DEP lidar flown before the fire (sets `USGS_LPC_CA_NoCAL_Wildfires_B4_2018` and `..._PlumasNF_B2_2018`), from <https://github.com/kulpojke/detection>, commit cf911e8; the run's `README.md` says how. EPSG:6339. The 15,517 that intersect the COG bounds are used. |
| `../detection/runs2/camp_fire_prefire/flown.gpkg` | 529 | Where the lidar was flown before the fire (`period` 'before' or 'both') or only after it ('after'). |
| `camp_config.yaml` | | `imagery_cog`: its bounds (-121.6516, 39.7156, -121.5527, 39.8085), as bootstrap uses them. |

## Columns

| Column | Description |
|---|---|
| `id` | 32-hex-character building ID (see below). Overture and added buildings keep theirs. |
| `origin` | `lidar`, `overture` or `added` (hand-digitised). For `fields.origin` in the config. |
| `prefire_lidar` | True where the lidar was flown before the fire: all lidar buildings, and kept buildings whose representative point lies there. False for western Paradise, which 3DEP flew only after the fire. |
| `replaces` | Lidar buildings only: the IDs of the Overture and added buildings it replaced, comma-separated, sorted; null if none. |
| `grey_zone` | True for a lidar building that replaced a building with which it shares less than half of either one's area: the two may not be the same building. False otherwise. The pairs are in the grey-zone GeoPackage. |
| `lidar_class` | Lidar only: `building`, or `shed` (under 40 m²: a shed, container or RV). |
| `lidar_height` | Lidar only: the building's height above the ground, m. |
| `lidar_area` | Lidar only: the footprint's area, m² (in EPSG:6339). |
| `lidar_tree_notch` | Lidar only: true where a tree hides part of the roof, so the footprint may be too small there. |
| `lidar_naip_green` | Lidar only: true where NAIP 2018 shows at least 4 m² of green on the roof: perhaps a tree taken as roof (NAIP leans tall trees, so many are fine). |
| `lidar_fid` | Lidar only: the building's `fid` in `buildings.gpkg`. |
| `h3_r5` … `h3_r10` | H3 cell of the footprint's `representative_point()` at resolutions 5–10, as in `src/add_h3_indexes.py`. |
| `geometry` | Footprint polygon. |

## IDs

Lidar buildings get IDs in the layout of the Overture IDs (see
`camp_overture_2024-02-15-alpha-0_with_added.md`), with `11da` as the marker:

| Characters 17–20 | Origin |
|---|---|
| `0200` | Overture |
| `9999` | added by hand |
| `11da` | lidar |

Characters 1–16 are the H3 resolution-11 cell of the footprint's
`representative_point()`, left-padded with `0`; characters 21–32 are the
first 12 hex characters of the SHA-1 hash of the footprint's WKB (EPSG:4326).
Lidar footprints are rounded to 1e-7° (about 1 cm) first, so the GeoJSON
holds them exactly and every ID can be recomputed from the file
(`check_camp_lidar_merge.py` does).

## Processing steps

1. Read the lidar buildings that intersect the COG bounds (bootstrap's own
   test), reproject EPSG:6339 → EPSG:4326, round to 1e-7°, `make_valid()`.
2. **Lidar takes precedence.** Every Overture or added building that shares
   area (more than 0.01 m², in EPSG:6339) with a lidar building is dropped,
   as `data/add_camp_footprints.py` dropped added buildings that overlapped
   Overture. 1,986 were dropped (1,743 Overture, 243 added), replaced by
   2,029 lidar buildings. One lidar building can replace several (93 do),
   and one building can be replaced by several lidar buildings (139 are).
3. **Grey zone:** 155 replacements, by 150 lidar buildings, share less than
   half of either one's area. Their lidar buildings get `grey_zone` true, and
   the pairs go to the GeoPackage: layer `replaced` (the dropped building,
   with `lidar_id`, `share_of_replaced`, `share_of_lidar`, `shared_m2`) and
   layer `lidar`.
4. Keep the other 1,160 Overture and added buildings unchanged. Inside the
   COG bounds (858): 658 lie where there is no pre-fire lidar
   (`prefire_lidar` false; 655 Overture, 3 added), and 200 where there is
   (125 Overture, 75 added): buildings the lidar missed, or built after
   2018. The other 302 are added buildings outside the COG bounds.
5. Add IDs and H3 to the lidar buildings, check that all IDs are unique, write
   GeoJSON, then GeoParquet with `src/geojson2parquet.py`.

## Checks

`data/tests/check_camp_lidar_merge.py` recomputes the merge by other means and
compares: unique, well-formed IDs whose marker matches `origin`; valid
polygons; lidar IDs and every row's H3 cells recomputed from the geometry in
the file; kept buildings unchanged; every input building either kept or listed
in `replaces`, never both; every lidar building in the COG bounds present,
once; `replaces` equal to the buildings each lidar building shares area with;
`grey_zone` where it should be; no lidar building sharing area with a kept
one; `prefire_lidar`; and the GeoParquet equal to the GeoJSON (rows, values,
geometry). All pass.

`data/tests/check_dins_recall.py` measures completeness against the 18,088
Camp Fire DINS points in the COG bounds (share with a building within 5 m):

| DINS points | Points | Inside a building | ≤ 5 m | ≤ 10 m | ≤ 20 m |
|---|---|---|---|---|---|
| With pre-fire lidar, all | 15,223 | 62% | 79% | 85% | 94% |
| … Destroyed | 13,307 | 60% | 77% | 84% | 93% |
| … No Damage | 1,442 | 71% | 88% | 92% | 96% |
| Without pre-fire lidar, all | 2,865 | 14% | 19% | 20% | 26% |
| … Destroyed | 2,372 | 5% | 7% | 9% | 15% |
| … No Damage | 378 | 55% | 70% | 74% | 78% |

A DINS point can lie beside its structure (on the driveway, at the address),
so even a complete layer would not reach 100% inside.

## Known issues

- **Western Paradise has almost no footprints of destroyed buildings.** 3DEP
  flew it only after the fire, and Overture 2024 no longer has most of what
  burned: of its 2,372 DINS points marked Destroyed, 7% have a building within
  5 m. Another pre-fire source is needed there.
- 302 added buildings lie outside the COG bounds. They are kept as they were
  (the lidar was read only inside the bounds), and bootstrap drops them.
- Bootstrap will not swap this in under the existing Camp project (see the
  `_with_added` `.md`): it needs a new layer ID or project ID. The 22
  annotations made so far are not carried over.
- The lidar's own limits (`../detection/runs2/camp_fire_prefire/README.md`):
  a building under a tree's canopy can be missed or cut short
  (`lidar_tree_notch`), and crinkly outlines became their main rectangle, so
  an L-shaped building can be one rectangle.
