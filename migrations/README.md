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
| `features` | Current building geometries and properties |
| `feature_h3` | H3 cells associated with each feature and resolution |
| `tasks` | Annotation, QA/QC, or editing work units |
| `task_reviewers` | Reviewers assigned to tasks |
| `task_h3_assignments` | H3 cells assigned to individual reviewers |
| `annotations` | Each reviewer's current annotation for a feature |
| `annotation_history` | Previous versions of changed annotations |
| `feature_history` | Previous versions of edited features |
| `export_state` | Most recent project revision exported to GeoParquet |

The principal relationships are:

```text
projects
  +-- features
  |     +-- feature_h3
  |     +-- annotations
  +-- tasks
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

## Features

[`features`](001_initial.sql#L11-L23) stores the current version of each
building.

- `(project_id, id)` is the composite primary key, allowing different projects
  to use the same source feature identifier.
- `geometry` uses EPSG:4326.
- Geometry must be valid, nonempty, and either Polygon or MultiPolygon.
- `properties` holds flexible source attributes as JSONB.
- `version` supports optimistic concurrency checks.
- `updated_by` identifies the reviewer responsible for the latest edit.

The geometry column is declared as `geometry(Geometry, 4326)` so it can hold
both Polygon and MultiPolygon values. Check constraints restrict it to those
two building geometry types.

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

One feature can have one H3 index at each resolution:

```text
project_id | feature_id | resolution | h3_index
camp       | 123        | 8          | 8828308281fffff
camp       | 123        | 9          | 8928308280fffff
```

The lookup index supports queries that find all project features assigned to a
set of H3 cells.

## Tasks And Reviewer Assignments

[`tasks`](001_initial.sql#L40-L50) defines work units. A task has one mode:

- `annotation`
- `qaqc`
- `editing`

Tasks also store their permitted labels, active status, and intended blind
review policy.

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
task_id + feature_id + reviewer_id
```

Alice and Bob can therefore annotate the same feature without overwriting one
another. Annotation and QA/QC records can also coexist because they belong to
different tasks.

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

[`export_state`](001_initial.sql#L127-L132) records:

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

## What This Migration Does Not Do

The migration creates database structure only. It does not:

- import initial building features;
- create projects, tasks, reviewers, or assignments;
- authenticate users;
- export GeoParquet; or
- connect the legacy CSV-writing browser workflow to PostGIS.

Those operations belong to the API, bootstrap/import tooling, authentication
layer, and periodic export worker.
