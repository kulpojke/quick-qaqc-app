# Application Flow

The application is YAML-configured and PostGIS-backed. Independent point and
polygon GeoParquet layers initialize the database; reviewer clients never edit
object-storage files directly.

## Runtime Flow

```mermaid
flowchart TD
    yaml["Project YAML<br/>layers, columns, CRS, modes, user, H3 TODO"]
    parquet["GeoParquet layers<br/>points and polygons"]
    migrations["src/api/migrate.py<br/>apply numbered migrations"]
    bootstrap["src/api/bootstrap.py<br/>reproject, COG filter, H3, import"]
    postgis[("PostGIS<br/>layers, features, tasks, assignments,<br/>annotations and history")]
    app["app.py<br/>configured UI and COG tile server"]
    featureStore["src/api/feature_store.py<br/>assigned feature reads"]
    reviewStore["src/api/review_store.py<br/>review reads and writes"]
    browser["Browser UI<br/>Leaflet and H3 navigation"]
    cog["Local or HTTP(S) COG"]

    migrations --> postgis
    yaml --> bootstrap
    parquet --> bootstrap
    bootstrap --> postgis

    yaml --> app
    app --> featureStore
    app --> reviewStore
    featureStore --> postgis
    reviewStore --> postgis

    browser -->|GET /api/buildings| app
    browser -->|GET /api/annotations?mode=...| app
    browser -->|POST /api/annotations?mode=...| app
    app -->|configured features and own reviews| browser

    browser -->|XYZ tile requests| app
    app -->|range reads| cog
    cog -->|imagery| app
```

## Annotation Save Sequence

```mermaid
sequenceDiagram
    actor Reviewer
    participant Browser
    participant App as app.py
    participant Store as review_store.py
    participant DB as PostGIS

    Reviewer->>Browser: Select feature, label, and notes
    Browser->>App: POST layer_id, id, label, notes, feature_version_seen, mode
    App->>Store: write_reviewer_annotation(...)
    Store->>DB: Resolve active task and verify H3 assignment
    Store->>DB: Lock and compare feature version
    Store->>DB: Upsert reviewer annotation
    DB-->>Store: Current annotation row
    Store-->>App: Browser-shaped review record
    App-->>Browser: Saved review JSON
    Browser->>Browser: Update completion and advance
```

Annotation tasks are layer-specific. The database unique key also includes
reviewer identity, allowing multiple reviewers to annotate the same feature
without overwriting each other.

## COG Tile Sequence

```mermaid
sequenceDiagram
    participant Browser as Leaflet
    participant App as app.py
    participant RioTiler as rio-tiler
    participant COG as Local or HTTP(S) COG

    Browser->>App: GET /api/cog/tile/z/x/y.png
    App->>RioTiler: Reader.tile(x, y, z)
    RioTiler->>COG: Read required tile or overview ranges
    COG-->>RioTiler: Raster bytes
    RioTiler-->>App: Rendered PNG
    App-->>Browser: image/png
```

Geometry/property editing will use the existing versioned FastAPI endpoint,
but it is not yet exposed by the browser UI.
