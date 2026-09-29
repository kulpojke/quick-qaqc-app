# Shared API

This package provides the PostGIS-backed portion of the annotation and editing
system. It owns database setup, initial project loading, assigned feature
reads, and concurrent API updates.

## Files

| File | Purpose |
| --- | --- |
| `__init__.py` | Marks and describes the shared API package |
| `database.py` | Reads `DATABASE_URL`, creates the Psycopg connection pool, and provides request-scoped connections |
| `migrate.py` | Applies numbered SQL files from `../../migrations/` once and verifies their checksums |
| `bootstrap.py` | Reprojects and COG-filters configured GeoParquet layers, generates H3 rows, and synchronizes layer tasks and reviewer assignments |
| `export.py` | Streams current feature layers and multi-reviewer annotations through DuckDB into revisioned local snapshots |
| `feature_store.py` | Returns compact assigned features and applies browser point/polygon edits with optimistic version checking |
| `review_store.py` | Reads and writes reviewer annotations with task, assignment, and feature-version checks |
| `auth.py` | Derives development reviewer identity from `X-Reviewer-ID` and fails closed for unimplemented production authentication |
| `models.py` | Defines and validates annotation and feature-edit request bodies |
| `main.py` | Creates the FastAPI application and implements health, feature, annotation, and editing endpoints |

## Startup Lifecycle

Compose starts services in this order:

```text
PostGIS health check
        |
        v
migrate.py
  001_initial.sql
  002_feature_imports.sql
  003_feature_layers.sql
        |
        v
bootstrap.py <---- project_config.py <---- camp_config.yaml
        |
        +---- DuckDB reads each local or remote GeoParquet layer
        +---- CRS transform and COG-intersection filter
        +---- layer-aware features and generated feature_h3
        +---- per-layer tasks and feature_imports ledger
        |
        +------------------+
        v                  v
 FastAPI main.py       app.py
```

The import and its ledger row are committed in one transaction. Later starts
with the same sources and COG bounds synchronize task assignments but do not
replace feature rows. A changed source, bounds, or inconsistent feature count
stops startup so database edits cannot be silently overwritten.

## Read And Write Paths

`app.py` calls `feature_store.read_project_features()` and point-edit helper, plus the read/write
functions in `review_store.py`. These queries apply the active task and
reviewer assignments stored in PostGIS.

The shared FastAPI endpoints in `main.py` provide the future collaborative
write path:

```text
request
  |
  +--> auth.py establishes reviewer identity
  +--> models.py validates the body
  +--> database.py supplies a transaction
  +--> main.py verifies task assignment
          |
          +--> annotation upsert and history
          +--> expected-version feature update and history
```

Feature edits use optimistic version checks. An annotation is unique by task,
layer, feature, and reviewer, so several reviewers can independently annotate
the same feature. The browser exposes point dragging plus separate polygon
movement, vertex editing, soft-deletion, and drawing modes. Newly drawn
polygons receive a UUID from the server. Property editing remains a backend
foundation.

## Commands

Apply migrations:

```bash
python -m src.api.migrate
```

Initialize or reuse a configured project:

```bash
python -m src.api.bootstrap --yaml camp_config.yaml
```

Run one export directly for diagnostics:

```bash
python -m src.api.export --yaml camp_config.yaml
```

Compose normally runs the module with `--watch`. It checks compact per-layer
feature and annotation tokens every two seconds and exports pending changes
after five minutes or 50 saved edits, whichever comes first. The exporter uses
a repeatable-read PostGIS transaction so files written in one batch share one
project revision. It omits soft-deleted features and performs atomic DuckDB
writes. Feature layers are GeoParquet; annotations are ordinary Parquet keyed
by `annotation_id` and the composite `project_id`, `layer_id`, `feature_id`
feature reference, represented in both outputs as `_dm_project_id`,
`_dm_layer_id`, and `_dm_feature_id`. Annotation-only batches do not rewrite
feature files. Local staging does not update `export_state`; that record is
reserved for the later bucket-publish step.

After a complete export batch validates, the worker retains the newest two
generated revisions for each changed stream and deletes older generated files.
The filename matcher is project-and-stream specific, and configured local
source Parquets are protected from cleanup. `EXPORT_REVISIONS_TO_KEEP` can
raise the retention count but must be at least one.

Run the API directly:

```bash
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

All commands require `DATABASE_URL`. Development API requests additionally
require the `X-Reviewer-ID` header. See
[`../../migrations/README.md`](../../migrations/README.md) for the schema.
