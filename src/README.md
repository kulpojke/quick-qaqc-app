# Source Modules

This directory contains project configuration, geospatial preparation tools,
and the shared API package. The browser-facing server remains in
`../app.py`, while static browser files live in `../frontend/`.

## Files

| File | Purpose |
| --- | --- |
| `project_config.py` | Loads and validates project YAML, resolves paths, and produces the shared `ReviewConfig` used by the app and database bootstrap |
| `fetch_overture_buildings.py` | Finds an appropriate Fused Overture release, queries buildings inside COG bounds, clips them, adds H3 indexes, and maintains the local cache |
| `add_h3_indexes.py` | Adds H3 cell columns to polygon data and can combine or deduplicate input files |
| `geojson2parquet.py` | Converts GeoJSON into compressed GeoParquet using DuckDB Spatial |
| `build_cog.py` | Builds a VRT and Cloud Optimized GeoTIFF from source TIFF imagery |
| `merge_qaqc_annotations.py` | Historical conversion helper for old CSV exports; it is not used by the application |
| `api/` | Contains PostGIS migration, bootstrap, feature access, authentication, validation, and FastAPI code |

Each utility can be run directly with `python src/<file>.py --help`. The API
modules are normally run as modules, for example
`python -m src.api.bootstrap --yaml project.yaml`.

## Relationships

The primary preparation path is:

```text
project YAML
    |
    +--> project_config.py ------------------------+
    |                                             |
    +--> fetch_overture_buildings.py               |
            |                                      |
            +--> add_h3_indexes.py                 |
            |                                      |
            +--> cached GeoJSON                    |
                                                   v
existing GeoJSON --> geojson2parquet.py --> GeoParquet
                                                   |
                                                   v
                                            api/bootstrap.py
                                                   |
                                                   v
                                                PostGIS
```

`app.py` also loads `ReviewConfig`. It reads reviewer-assigned features
through `api/feature_store.py` and reviews through `api/review_store.py`.

## Data Responsibilities

- Source GeoJSON and GeoParquet use EPSG:4326 polygon or multipolygon
  geometries.
- H3 source fields use the configured prefix followed by a resolution, such as
  `h3_r8`.
- GeoParquet is immutable initialization or export data.
- PostGIS is the mutable shared source of truth.
- Annotation and QA/QC records are read from and written to PostGIS.

See [`api/README.md`](api/README.md) for the database lifecycle and service
relationships.
