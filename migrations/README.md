# Database Schema

The migrations in this directory define the shared PostGIS database used by
the annotation and QA/QC system. PostGIS is the mutable source of truth for
collaborative work. Versioned GeoParquet files in object storage are periodic
exports, not files edited directly by reviewer clients.

Migration files are numbered in execution order. `001_initial.sql` creates the
initial schema; `src/api/migrate.py` records applied migrations and their
checksums so each migration is applied once and cannot be silently changed
afterward.

## Schema Overview

| Table | Purpose |
| --- | --- |
| `projects` | Top-level projects and their current database revision |
| `feature_layers` | Independent point or polygon sources, column roles, and editing capabilities |
| `features` | Current layer-aware point or polygon geometries and properties |
| `feature_h3` | H3 cells associated with each feature and resolution |
| `tasks` | Annotation, QA/QC, or editing work units |
| `task_reviewers` | Reviewers assigned to tasks |
| `task_h3_assignments` | H3 cells assigned to individual reviewers |
| `annotations` | Each reviewer's current annotation for a feature |
| `annotation_history` | Previous versions of changed annotations |
| `feature_history` | Previous versions of edited features |
| `export_state` | Most recent project revision exported for each layer |
| `feature_imports` | Immutable GeoParquet source and COG bounds used for each layer |

The principal relationships are:

```text
projects
  +-- feature_layers
  |     +-- features
  |     +-- feature_h3
  |     +-- annotations
  +----- tasks
        +-- task_reviewers
        |     +-- task_h3_assignments
        +-- annotations
```

## PostGIS

[`001_initial.sql`](001_initial.sql#L1) enables PostGIS. This provides spatial
types, indexes, validation, and functions such as `ST_IsValid`.

## Projects

[`projects`](001_initial.sql#L3-L9) contains one row per review project.

- `id` is the stable project identifier.
- `name` is the display name.
- `revision` represents the overall mutable state of the project.
- `created_at` and `updated_at` use timezone-aware timestamps.

The API increments `revision` when a feature or annotation changes. This is
application behavior, not a database trigger. Export workers can compare this
revision with `export_state.exported_revision` to determine whether a new
GeoParquet snapshot is needed.

## Feature Layers

[`feature_layers`](003_feature_layers.sql) separates independently sourced
datasets within one project. It records source CRS, accepted geometry types,
semantic column roles, H3 prefix, and permitted editing operations. Tasks,
features, imports, annotations, histories, and exports retain this layer
identity.

Bootstrap filters every layer by intersection with the imagery COG bounds.
Future exports should write one versioned GeoParquet per layer from this
relevant PostGIS subset, rather than copying the full original source such as
a statewide point dataset.

## Features

[`features`](001_initial.sql#L11-L23), extended by
[`003_feature_layers.sql`](003_feature_layers.sql), stores each layer's current
point or polygon features.

- `(project_id, layer_id, id)` is the composite primary key, allowing layers
  and projects to reuse source feature identifiers safely.
- `geometry` uses EPSG:4326.
- Geometry must be valid, nonempty, and Point, MultiPoint, Polygon, or
  MultiPolygon. Each layer further declares its accepted geometry types.
- `properties` holds flexible source attributes as JSONB.
- `version` supports optimistic concurrency checks.
- `updated_by` identifies the reviewer responsible for the latest edit.

All database geometries use EPSG:4326. Bootstrap transforms each source from
its configured CRS before filtering and insertion.

The GiST index at [`features_geometry_gix`](001_initial.sql#L25) supports fast
spatial intersection and bounding-box queries.

### Optimistic feature editing

Clients submit the feature version they originally loaded. An update succeeds
only if that expected version still matches the current row. A successful edit
increments the version. If another reviewer has already changed the feature,
the update affects no row and the API returns a conflict instead of
overwriting newer work.

## H3 Indexes

[`feature_h3`](001_initial.sql#L27-L38) normalizes H3 membership rather than
adding columns such as `h3_r8` directly to `features`.

One layer feature can have one H3 index at each resolution:

```text
project_id | layer_id | feature_id | resolution | h3_index
camp       | points   | 123        | 8          | 8828308281fffff
camp       | points   | 123        | 9          | 8928308280fffff
```

The lookup index supports queries that find all project features assigned to a
set of H3 cells.

## Tasks And Reviewer Assignments

[`tasks`](001_initial.sql#L40-L50) defines layer-specific work units. A task
has one database mode:

- `annotation`
- `qaqc`
- `editing`

Tasks also store their permitted labels, active status, and intended blind
review policy.

`qaqc` remains accepted by the original database constraint for historical
projects, but current YAML validation does not create QA/QC tasks. The browser
uses only `annotation` and `editing` tasks.

[`task_reviewers`](001_initial.sql#L52-L58) assigns users to tasks.
`all_features = true` grants the reviewer the entire task. Otherwise,
[`task_h3_assignments`](001_initial.sql#L60-L72) defines the reviewer's work
area.

The assignment primary key includes both task and reviewer. Consequently, the
same H3 cell can be assigned to multiple reviewers independently.

## Annotations

[`annotations`](001_initial.sql#L74-L96) stores current review results. The
unique constraint covers:

```text
task_id + layer_id + feature_id + reviewer_id
```

Alice and Bob can therefore annotate the same feature without overwriting one
another. Historical QA/QC records may coexist because they belong to different
tasks, although the current workflow no longer creates those tasks.

- `label` and `notes` contain the current review.
- `feature_version_seen` records which geometry version the reviewer observed.
- `version` tracks revisions to that reviewer's annotation.
- `created_at` records the original annotation time.
- `updated_at` records its most recent change.

Foreign keys ensure that the task and feature belong to the stated project and
that the reviewer is assigned to the task.

## History

The current tables contain only the latest state. Trigger-maintained history
tables preserve the states they replace.

### Feature history

[`archive_feature_update`](001_initial.sql#L134-L167) runs before a feature's
geometry or properties change. It copies the old geometry, properties, and
version into `feature_history` and records the user causing the new update.

Changing only timestamps or bookkeeping columns does not create a feature
history row.

### Annotation history

[`archive_annotation_update`](001_initial.sql#L169-L211) runs before an
annotation's label, notes, or observed feature version changes. It copies the
old annotation into `annotation_history` before the current row is replaced.

The history tables deliberately contain snapshots rather than current-row
foreign keys. Deleting current project data therefore does not automatically
erase its recorded history.

## Export State

[`export_state`](001_initial.sql#L127-L132) records per layer:

- the most recently exported project revision;
- the versioned object-storage key; and
- the export timestamp.

An exporter can check:

```text
projects.revision > export_state.exported_revision
```

When true, PostGIS contains changes that have not yet been written to a
GeoParquet snapshot.

## Deletion Behavior

Most current-state relationships use `ON DELETE CASCADE`. Deleting a project,
task, feature, or reviewer assignment removes dependent current rows that can
no longer be valid. History rows are independent snapshots and are not part of
those cascades.

## Initial Feature Import

[`002_feature_imports.sql`](002_feature_imports.sql), extended by migration 003,
records each layer source, retained feature count, and COG bounds. Bootstrap
uses these rows to make startup idempotent: unchanged filtered sources are
reused, while changed sources or bounds are rejected instead of replacing
database edits.

The import record is written in the same transaction as the project features
and normalized H3 rows. A failed or interrupted import therefore leaves no
partially initialized project.

## What This Migration Does Not Do

The migrations create database structure only. They do not:

- execute the initial building import;
- execute project, task, reviewer, or assignment setup;
- authenticate users;
- export GeoParquet; or
- connect the legacy CSV-writing browser workflow to PostGIS.

`src/api/bootstrap.py` performs the first two operations at container startup.
The remaining operations belong to the API, authentication layer, and
periodic export worker.
