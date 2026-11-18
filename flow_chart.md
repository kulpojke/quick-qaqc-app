# Application Flow

The application is YAML-configured and PostGIS-backed. Independent point and
polygon GeoParquet layers initialize the database; reviewer clients never edit
object-storage files directly.

## Runtime Flow

```mermaid
flowchart TD
    yaml["Project YAML<br/>imagery, layers, labels,<br/>reviewers, modes, max_features"]
    parquet["GeoParquet layers on R2<br/>points and polygons"]
    cogs["COGs on R2<br/>post- and pre-fire imagery"]
    runsh["run.sh project.yaml<br/>[--auto-assign-hex FRACTION N]"]
    migrations["src/api/migrate.py<br/>apply numbered migrations"]
    bootstrap["src/api/bootstrap.py<br/>reproject, filter to imagery,<br/>H3, import once"]
    assign["src/api/auto_assign.py<br/>hexes and per-building lists"]
    postgis[("PostGIS<br/>features, history, tasks, hexes,<br/>building lists, annotations")]
    access["Cloudflare Tunnel and Access<br/>one-time PIN, tile cache"]
    app["app.py<br/>UI, COG tiles, Access JWT check"]
    featureStore["src/api/feature_store.py<br/>reads and geometry edits"]
    reviewStore["src/api/review_store.py<br/>label reads and writes"]
    browser["Browser UI<br/>Leaflet and H3 navigation"]
    exportWorker["src/api/export.py --watch<br/>revisioned snapshots"]
    exports["GeoParquet exports on R2<br/>features and annotations"]

    runsh --> migrations
    runsh --> bootstrap
    migrations --> postgis
    yaml --> bootstrap
    parquet --> bootstrap
    cogs -->|bounds| bootstrap
    bootstrap --> assign
    assign --> postgis
    bootstrap --> postgis

    browser <-->|HTTPS| access
    access <--> app
    yaml --> app
    app --> featureStore
    app --> reviewStore
    featureStore --> postgis
    reviewStore --> postgis
    featureStore -->|drawn building| assign
    reviewStore -->|first unknown| assign

    app -->|range reads| cogs

    postgis --> exportWorker
    exportWorker --> exports
```

`run.sh` runs the left-hand side once per deploy; the rest keeps running.
Without `--auto-assign-hex`, bootstrap reuses the saved assignments.

## Browser Requests

| Request | Purpose |
|---|---|
| `GET /api/config` | Reviewer, layers, modes and hexes (read fresh each page load) |
| `GET /api/buildings` | Features in the reviewer's hexes (or a whole layer, like DINS); each says if it is `annotatable` |
| `GET /api/annotations?mode=annotation` | The reviewer's own labels |
| `POST /api/annotations?mode=annotation` | Save a label |
| `POST /api/features` | Draw a building |
| `PATCH /api/features` | Move or reshape a feature |
| `DELETE /api/features` | Delete a polygon |
| `GET /api/cog/info`, `GET /api/cog/tile/z/x/y.png` | Imagery |

## Who Sees and Who Labels

```mermaid
flowchart LR
    hexes["task_h3_assignments<br/>hexes per reviewer"]
    lists["task_feature_assignments<br/>buildings per reviewer"]
    see["See and edit<br/>everything in the hex"]
    label["Label<br/>only own list"]
    grey["Other buildings in the hex<br/>dashed grey, not selectable"]

    hexes --> see
    see --> label
    lists --> label
    see --> grey
```

With `--auto-assign-hex FRACTION N`, every building gets N counted labels;
the first "unknown" on a building does not count. Labelled and drawn
buildings stay on their reviewer's list, the rest is shared out hex by hex,
and `max_features` caps a list. `reviewer_may_label()` in migration 006 is
the one rule the label checks use. Tasks without lists (`feature_lists =
false`) label everything in the hex, as before.

## Annotation Save Sequence

```mermaid
sequenceDiagram
    actor Reviewer
    participant Browser
    participant App as app.py
    participant Store as review_store.py
    participant Assign as auto_assign.py
    participant DB as PostGIS

    Reviewer->>Browser: Select own building, label, and notes
    Browser->>App: POST layer_id, id, label, notes, feature_version_seen, mode
    App->>Store: write_reviewer_annotation(...)
    Store->>DB: Active task, hex, and reviewer_may_label()
    Store->>DB: Lock and compare feature version
    Store->>DB: Upsert reviewer annotation
    opt Label changed to "unknown"
        Store->>Assign: top_up_feature(...)
        Assign->>DB: Add one more reviewer if still needed
    end
    DB-->>Store: Current annotation row
    Store-->>App: Browser-shaped review record
    App-->>Browser: Saved review JSON
    Browser->>Browser: Update completion and advance
```

Annotation tasks are layer-specific. The database unique key also includes
reviewer identity, allowing multiple reviewers to annotate the same feature
without overwriting each other. Drawing a building works the same way:
`feature_store.py` inserts it, then `top_up_feature` puts it on the
drawer's list and on N - 1 other lists.

## COG Tile Sequence

```mermaid
sequenceDiagram
    participant Browser as Leaflet
    participant CF as Cloudflare
    participant App as app.py
    participant RioTiler as rio-tiler
    participant COG as COG on R2

    Browser->>CF: GET /api/cog/tile/z/x/y.png?imagery=...&v=...
    alt Tile in Cloudflare cache
        CF-->>Browser: Cached tile
    else Not cached
        CF->>App: Same request
        App->>RioTiler: Reader.tile(x, y, z)
        RioTiler->>COG: Read required tile or overview ranges
        COG-->>RioTiler: Raster bytes
        RioTiler-->>App: JPEG, or PNG where nodata
        App-->>CF: Cache-Control public, max-age=86400
        CF-->>Browser: Tile
    end
```

The `v=` parameter carries the COG source, so new imagery gets new cache keys.
