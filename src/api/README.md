# Shared API

This package provides the PostGIS-backed portion of the annotation and QA/QC
system. It owns database setup, initial project loading, assigned feature
reads, and concurrent API updates.

## Files

| File | Purpose |
| --- | --- |
| `__init__.py` | Marks and describes the shared API package |
| `database.py` | Reads `DATABASE_URL`, creates the Psycopg connection pool, and provides request-scoped connections |
| `migrate.py` | Applies numbered SQL files from `../../migrations/` once and verifies their checksums |
| `bootstrap.py` | Imports configured GeoParquet into PostGIS once, normalizes H3 rows, and synchronizes YAML-managed tasks and reviewer assignments |
| `feature_store.py` | Returns the PostGIS features assigned to a project reviewer as a GeoJSON feature collection |
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
        |
        v
bootstrap.py <---- project_config.py <---- camp_config.yaml
        |
        +---- DuckDB reads local or remote GeoParquet
        +---- features and feature_h3
        +---- tasks, task_reviewers, task_h3_assignments
        +---- feature_imports ledger
        |
        +------------------+
        v                  v
 FastAPI main.py       app.py
```

The import and its ledger row are committed in one transaction. Later starts
with the same source synchronize task assignments but do not replace feature
rows. A changed source or an inconsistent feature count stops startup so
database edits cannot be silently overwritten.

## Read And Write Paths

`app.py` currently calls `feature_store.read_project_features()` on the
server side and sends the resulting GeoJSON to the browser. That query applies
the active task and reviewer H3 assignments stored in PostGIS.

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
feature, and reviewer, so several reviewers can independently annotate the
same feature. The current browser workflow still writes annotations to CSV;
moving those writes to these endpoints is a separate integration step.

## Commands

Apply migrations:

```bash
python -m src.api.migrate
```

Initialize or reuse a configured project:

```bash
python -m src.api.bootstrap --yaml camp_config.yaml
```

Run the API directly:

```bash
uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

All commands require `DATABASE_URL`. Development API requests additionally
require the `X-Reviewer-ID` header. See
[`../../migrations/README.md`](../../migrations/README.md) for the schema.
