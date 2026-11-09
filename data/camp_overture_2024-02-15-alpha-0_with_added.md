# camp_overture_2024-02-15-alpha-0_with_added.geojson

Camp Fire building footprints: the Overture buildings from
`camp_overture_2024-02-15-alpha-0.geojson` plus hand-digitised footprints of
buildings Overture is missing.

| | |
|---|---|
| Files | `data/camp_overture_2024-02-15-alpha-0_with_added.geojson` and `.parquet` (same features) |
| Created | 2026-11-04 |
| Made by | `data/add_camp_footprints.py` (run once from the repo root: `python -s data/add_camp_footprints.py`), then `python -s src/geojson2parquet.py data/camp_overture_2024-02-15-alpha-0_with_added.geojson` |
| CRS | EPSG:4326 |
| Features | 3,146 (2,523 Overture + 623 added) |
| Geometry | Polygon / MultiPolygon |
| Bounds | -121.6946, 39.7239, -121.5347, 39.8529 |

## Inputs

| Input | Features | Notes |
|---|---|---|
| `data/camp_overture_2024-02-15-alpha-0.geojson` | 2,523 | Overture buildings, release 2024-02-15-alpha-0, with `h3_r5`–`h3_r10` already added. Copied through unchanged. |
| `data/camp_added_foot/CAMP_AddedFootprints_SF.shp` | 649 | Added footprints, EPSG:3857. Received as `CAMP_AddedFootprints_SF.zip`. |
| `data/camp_added_foot/CAMP_AddedFootprints_SP.shp` | 1 | Added footprint, EPSG:3857. Received as `CAMP_AddedFootprints_SP.zip`. |

## Columns

Same columns as the Overture input; attributes from the shapefiles
(`Shape_Leng`, `Shape_Area`, `GlobalID_2`, etc.) were not kept.

| Column | Description |
|---|---|
| `id` | 32-hex-character building ID (see below). |
| `h3_r5` … `h3_r10` | H3 cell of the footprint's `representative_point()` at resolutions 5–10. Same method as `src/add_h3_indexes.py`, which produced the Overture values. |
| `geometry` | Footprint polygon. |

## IDs

Overture IDs in this release are laid out as:

```
08b2814a25319fff  0200  eb877f5897d2
└ H3 res-11 cell  └ constant  └ unique tail
  (zero-padded)
```

Added buildings use the same layout, with `9999` in place of `0200` to mark
them as additions:

- **Characters 1–16:** the H3 resolution-11 cell of the footprint's
  `representative_point()`, left-padded with `0` to 16 characters.
- **Characters 17–20:** `9999`.
- **Characters 21–32:** first 12 hex characters of the SHA-1 hash of the
  footprint's WKB geometry (EPSG:4326, after `make_valid`). This makes the IDs
  repeatable: rerunning the script on the same inputs gives the same IDs.

To select only the added buildings: `id.str[16:20] == '9999'`.

## Processing steps

1. Read both shapefiles, keep geometry only, reproject EPSG:3857 → EPSG:4326.
2. Repair invalid geometry with `make_valid()` (one SF polygon had a ring
   self-intersection).
3. **Drop added buildings that overlap Overture.** An added building is dropped
   if its intersection with any Overture building has non-zero area (touching
   along an edge does not count). 23 were dropped; each overlapped by 22–96% of
   its own area, so these were duplicates of buildings Overture already had.
4. Assign `id` and `h3_r5`–`h3_r10` as described above.
5. **Drop overlapping added footprints, checked by eye** (`DROP_IDS` in the
   script). After step 3, three pairs of added footprints still overlapped
   each other:
   - `08b2814a03d30fff99996c8648ed52b4` and `08b2814a03d30fff9999f910621f4876`
     duplicated `08b2814a03d30fff9999bed82f5e2daa`; the two were dropped and
     `…bed82f5e2daa` kept.
   - `08b2814a2e7aafff9999c05365c4d96b` and `08b2814a2e7aefff9999aeb1782e91f7`
     duplicated each other and were both poor footprints; both were dropped.
6. Append the 623 remaining added buildings to the Overture buildings and check
   that all IDs are unique. No added footprints overlap each other or an
   Overture building.
7. Convert to GeoParquet with `src/geojson2parquet.py`: GeoParquet 1.0.0
   metadata, Zstandard compression, same columns, IDs and geometry as the
   GeoJSON (verified).

## Known issues

- Bootstrap will not swap this in under the existing Camp project: once a
  layer is imported, `src/api/bootstrap.py` refuses a different `source` for
  the same `project.id` and layer. Using it needs a new project ID, a new layer
  ID, or an authorised database reset. Annotations already made on the live
  database are tied to Overture IDs that are unchanged in this file.
