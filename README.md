

## Data requirements

The application requires one or more GeoParquet layers containing point or
polygon features, annotation labels, and a COG. Each YAML layer names its
source CRS and relevant columns. Bootstrap reprojects features to EPSG:4326,
keeps only geometries intersecting the COG bounds, and generates H3 membership.

## Helper scripts

Bootstrap generates H3 membership automatically. `src/add_h3_indexes.py`
remains available for standalone data preparation outside the database flow.

For building a COG of imagery , `src/build_cog.py` has been provided

## Usage

Build the conda environment:

```bash
conda env create -f environment.yml
conda activate damagemap-qaqc
```

If needed, build a COG from a directory of TIFF imagery:

```bash
python src/build_cog.py path/to/imagery_dir
```

For imagery served by the app, add `--web --compress jpeg`. This reprojects
to EPSG:3857 and aligns 256 px blocks with the web-map tile grid
(GoogleMapsCompatible), so each map tile is read from one block instead of
being reprojected on request. Without `--zoom-level`, it rounds up to the next
zoom at least as fine as the source so no detail is lost.

```bash
python src/build_cog.py path/to/imagery_dir --web --compress jpeg --zoom-level 22
```

Run the app against a migrated and bootstrapped database:

```bash
AUTH_MODE=development DEV_REVIEWER_ID=alice@example.com \
  python app.py --yaml project.yaml
```

The browser frontend is kept in `frontend/index.html`, `frontend/styles.css`,
and `frontend/app.js`. `app.py` serves those files directly and exposes
runtime project settings through `/api/config`; no frontend build step or
Python string templating is required. Keeping this directory at the repository
root also makes it an explicit part of the containerized server application.

### Containers

The development Compose stack runs the current app alongside a persistent
PostGIS database:

```bash
cp .env.example .env
# Set database credentials and authentication in .env before starting.
./run.sh camp_config.yaml
```

To update an existing deployment after pulling changes:

```bash
git pull --ff-only
./run.sh camp_config.yaml
```

`run.sh` accepts a YAML path, relative to your current directory or absolute,
followed by optional assignment flags described below.
It sets the Compose variables for that invocation without editing `.env`,
builds the shared image using Docker's cache, validates the YAML and configured
R2 bucket access, starts or
reuses PostGIS, applies migrations, and synchronizes reviewer assignments.
It then recreates the app/API/export containers and waits for health checks.
The database container is reused, its named volume is retained, local exports
remain in `tmp/`, and the separate cloudflared service is unaffected. The app
may be briefly unavailable during replacement. A failed prerequisite stops
the script before application replacement; this is not an automatic rollback
of any migrations or bootstrap changes already applied.

For YAML inside the checkout, the script mounts the repository directory so
relative sibling data paths work. For external YAML, it mounts the YAML's
parent directory; keep local inputs beneath that directory. It always uses
this checkout's `.env` and `compose.yaml`. Use the same Compose project name
as the existing deployment (default `damagemap-qaqc`) to reuse its volume.

### Automatic hex assignments

Select approximately 25% of buildings, in whole hexes, and have each one
labelled by two distinct reviewers (omit `2` for one reviewer):

```bash
./run.sh spokane_config.yaml --auto-assign-hex 0.25 2
```

Select every hex containing a building whose `confidence` is at or below `0.7`:

```bash
./run.sh spokane_config.yaml --auto-assign-hex 0.25 2 --conf confidence 0.7
```

`--conf COLUMN LIMIT` **overrides the fraction**. All buildings in a selected hex
are assigned, including neighbors above the threshold. Values are compared in
the column's original units; null values do not select a hex, and a missing
column or invalid non-null value stops assignment. The fraction must be between
0 and 1; without confidence selection, zero assigns no buildings.

Assignment runs separately for each polygon layer, using its current,
non-deleted, COG-filtered PostGIS buildings and generated H3 indexes. Point
layers follow the building hexes (see below). The default resolution is the coarsest
in the layer's `h3_resolutions`; add `--hex-resolution 8` to use a finer generated
resolution. Large hexes can make the requested fraction approximate and leave
some reviewers with no work.

Only reviewers with annotation on that layer in YAML participate, and their
annotation/editing permissions are retained. Each reviewer gets a **list of
buildings** to label:

- Every selected building needs `N` counted labels. The first `unknown` label
  on a building does not count, so such a building ends up with `N + 1` labels.
- Buildings a reviewer already labelled, or drew, stay on their list. Labels
  from reviewers who have left the YAML still count.
- The rest is shared out hex by hex to the reviewers with the least open work
  (YAML order breaks ties), skipping anyone who already labelled the building.
  Without labels, a whole hex goes to the same `N` people.
- An optional `max_features` on a reviewer's building assignment caps their
  list (labelled and drawn buildings count toward it). If caps leave buildings
  short of reviewers, startup prints a warning with the number of open slots.

Hexes stay the unit people see and edit: a reviewer gets every hex that holds
a building on their list and sees all buildings in it. In annotation mode,
buildings on other reviewers' lists are drawn as dashed grey outlines; they
cannot be selected or labelled, and counts and navigation skip them.

Point layers (such as DINS) follow the hexes: a reviewer sees and moves the
points in their building hexes, like everyone else with that hex, so each
point is usually seen by `N` reviewers. Two people moving the same point get
the usual version check. A point assignment with its own `h3_indexes` in the
YAML keeps that list. The point layer must generate the hex resolution
(`--hex-resolution`), or the run stops. A reviewer with no building hexes
sees no points.

While people work, lists are topped up at once: a newly drawn building goes
to its drawer (even over a cap) plus `N - 1` others, and a building's first
`unknown` label brings one more reviewer. Reviewers who already have the hex
are preferred. The new reviewer sees it, and the points in its hex, on their
next page load.

Startup prints selected building counts and each reviewer's hexes and list
size.

Automatic assignments override manual `h3_indexes` without rewriting YAML.
They persist in PostGIS and are reused on ordinary restarts, including when
the same flags are repeated. Changing selection options shares out again,
keeping labelled and drawn buildings where they are. Changes to reviewer
eligibility/order, caps, layer sources or resolutions make an ordinary restart
stop; rerun with the auto flags (to share out again) or restore manual mode:

```bash
./run.sh spokane_config.yaml --manual-assign-hex
```

Manual mode removes the saved override and the building lists and reapplies
YAML assignments. Feature edits and annotations are preserved in either mode.
Bootstrap stores the override, lists and task permissions in one transaction,
and the app reads the same hexes for browser filtering on each page load.
Reviewers assigned zero hexes receive no building access rather than the YAML
empty-list shortcut for all features.

A database whose saved allocation predates building lists keeps whole-hex
labelling on ordinary restarts; the next run with the auto flags switches it
to lists and keeps every existing label.

#### Expunging a reviewer's labels

To make one reviewer's labels stop counting (for example after bad work),
without deleting anything:

```bash
PROJECT_CONFIG=camp_config_v2.yaml docker compose run --rm --no-deps \
  bootstrap python -m src.api.expunge --yaml /project/camp_config_v2.yaml \
  --reviewer someone@example.com --note "why"
```

Their labels stay in PostGIS but no longer count toward `N`, trigger top-ups,
or appear in exports. Remove them from the YAML, then rerun `./run.sh` with
the auto flags so their buildings go to others. `--restore` undoes it.
Bootstrap refuses to run while an expunged reviewer is still in the YAML.
Geometry edits are not undone.

Set `PROJECT_CONFIG` in `.env` to the project YAML, or pass it when invoking
Compose directly instead of using the script:

```bash
PROJECT_CONFIG=camp_config.yaml docker compose up -d
```

Compose requires this parameter; it has no hardcoded project YAML. The app,
bootstrap, and export worker all receive the same selection. `PROJECT_CONFIG`
is relative to `PROJECT_CONFIG_DIR` (defaults to the repository directory),
which is mounted read-only at `/project`. Relative source and imagery paths
inside the YAML still resolve from its own directory. Keep referenced local
files within that mounted directory tree, or use HTTP(S) sources. For a project
stored outside the repository:

```bash
PROJECT_CONFIG_DIR=/opt/review-projects PROJECT_CONFIG=camp/project.yaml \
  docker compose up -d
```

Persist these values in the server `.env` so subsequent Compose commands use
the same project. Give each distinct project its own stable `project.id` in
YAML. This selects one project per running UI; it does not start multiple UIs.
After changing the selection or reviewer assignments, synchronize the database
and recreate the services:

```bash
docker compose run --rm bootstrap
docker compose up -d --no-deps --force-recreate app api export
```

No image rebuild is needed for a YAML-only change. Exports continue to use the
repository's `tmp/` mount, independent of the selected YAML directory.

Open `http://127.0.0.1:8501`. The app binds only to localhost by default, as
does PostGIS on port `5432`. Stop the stack with `docker compose down`; the
named `postgis_data` volume survives container replacement.

In attached mode, `docker compose up` waits for the UI and API health checks,
then prints their host-facing URLs using `APP_PORT` and `API_PORT` from `.env`.
Detached mode does not display service logs; use `docker compose logs startup`
to show the same links after `docker compose up -d`.

The shared FastAPI service runs at `http://127.0.0.1:8000`, with interactive
documentation at `/docs`. The one-shot `migrate` service applies ordered SQL
migrations. The `bootstrap` service then imports each configured GeoParquet
layer into PostGIS once, filters it to the COG, generates normalized H3 rows,
and synchronizes layer tasks and reviewer assignments before either server
starts.

The UI reads reviewer-assigned features and review records from PostGIS.
Annotation submissions are written directly to the `annotations` table and
preserve independent records for each reviewer.

Each `layers[].source` must point to local or HTTP(S) GeoParquet.

During development, browser requests use `DEV_REVIEWER_ID`; an explicit
`X-Reviewer-ID` header overrides it for API and multi-user tests. In production,
set `AUTH_MODE=cloudflare`, `CF_ACCESS_TEAM_DOMAIN`, and `CF_ACCESS_AUD`. The
server validates Cloudflare Access's `Cf-Access-Jwt-Assertion` and uses its
`email` claim as the reviewer ID. That identity must exactly match a reviewer
ID in the project YAML.

The initial API supports:

- assigned, paginated feature reads without exposing peer annotations;
- one current annotation per task, feature, and reviewer, with history;
- optimistic geometry/property edits using `expected_version`;
- overlapping H3 assignments for multiple reviewers; and
- project revisions that can drive periodic GeoParquet exports.

See [`migrations/README.md`](migrations/README.md) for the database tables,
relationships, history triggers, concurrency model, and export tracking.

PostGIS remains the mutable source of truth. GeoParquet in R2 will be an
immutable, versioned export rather than a file edited by reviewer clients.

The Compose stack runs an export worker automatically. It writes one
revisioned file per layer to project-root `tmp/` at startup and after feature
or annotation changes reach either five minutes or 50 saved edits, whichever
comes first. Soft-deleted features are excluded; current geometries, source
properties, feature IDs, and generated H3 columns are retained.

Current annotations are written to a separate ordinary Parquet file because
their geometry remains in the feature GeoParquet. Each row retains its unique
`annotation_id`, while `project_id`, `layer_id`, and `feature_id` form the
collision-safe join back to a feature. In the files these shared columns are
named `_dm_project_id`, `_dm_layer_id`, and `_dm_feature_id`; every feature
layer receives the same standard keys regardless of its source ID field.
Feature layers also get `_dm_edited`, which is true when a feature's geometry
or properties changed after import or drawing. A layer can name an origin
column with `fields.origin` (for example `origin: origin`): imported features
keep the value from their source, and features drawn in the app get the
editor's ID, so exports separate original features from added ones.
Multiple reviewers therefore remain separate rows. Annotation-only changes
update only this annotation snapshot, and an idle worker does not rewrite
unchanged data.

`EXPORT_POLL_SECONDS`, `EXPORT_INTERVAL_SECONDS`, and `EXPORT_EDIT_THRESHOLD`
in `.env` tune the worker. `EXPORT_REVISIONS_TO_KEEP` defaults to `2`, retaining
the current generated snapshot plus one rollback revision for each stream.
Cleanup runs only after a complete new batch validates, matches only
exporter-generated revision names, and explicitly protects configured local
source files. When R2 is configured, publication must succeed before local
retention runs or the worker marks changes as exported.

### R2 export destination

Set the destination in each project's YAML. Use a private bucket: the
exports hold reviewer IDs (email addresses) and notes.

```yaml
exports:
  bucket: <bucket>
  endpoint_url: https://<account id>.r2.cloudflarestorage.com
  prefix: exports
```

The endpoint is the bucket's **S3 API endpoint without the bucket path**.
Omit `exports` entirely for local-only development; an incomplete section is
an error. Credentials never belong in YAML. Create an R2 S3 access key with
**Object Read & Write** permission for the destination bucket, then set these
in the server `.env`:

```dotenv
R2_ACCESS_KEY_ID=your-access-key-id
R2_SECRET_ACCESS_KEY=your-secret-access-key
```

Compose passes these credentials only to the export worker. Run
`./run.sh camp_config.yaml` after pulling the code; the image rebuild installs
the new boto3 dependency. Preflight checks credentials and bucket access before
replacing the app services; actual uploads also require write permissions.
Cloudflare documents the credentials and endpoint in its
[R2 S3 setup guide](https://developers.cloudflare.com/r2/get-started/s3/).

The worker stages validated files in `tmp/`, uploads them, and streams them
back to verify their size and SHA-256 digest. It publishes a `latest.json`
manifest only after the entire selected batch verifies, at:

```text
s3://<bucket>/<prefix>/_damagemap_exports/<project id>/latest.json
```

The manifest's `streams` map contains newest-first histories for each feature
layer and `annotations`. Entries contain the object `key`, database `revision`,
`row_count`, byte `size`, `sha256`, and a UTC `timestamp`. Snapshot keys include
their revision and datetime, for example
`revision-17-20261012T183045.123456Z.parquet`. Checksums remain in the manifest
and object metadata for upload verification. Existing hash-named snapshots
remain supported and age out through normal retention.
Annotation-only batches retain unchanged feature references in the manifest.
After manifest verification, the worker records feature-layer publication in
`export_state`, then prunes older generated objects, keeping two revisions per
stream by default. Cleanup follows only validated manifest keys, never scans
or deletes other objects in the source bucket, and protects configured source
URLs. The `annotations` layer ID is reserved for the annotation stream.

A publication failure exits the worker without advancing its change token;
Compose restarts it and retries a complete snapshot. Pending deletion keys
are saved in the manifest for retry. An interrupted upload can leave an
unreferenced object; cleanup deliberately does not scan for these orphaned
objects. Run one worker per project/destination against the same PostGIS;
publication is serialized with a database advisory lock. An older snapshot
cannot replace a newer manifest.

Exports inherit the bucket's access settings. In the public source bucket,
export files can also be public, including all reviewers' labels and identities;
the app's blinded-review controls do not restrict direct bucket access.

For an assigned project, pass its YAML file and development identity to the app:

```bash
AUTH_MODE=development DEV_REVIEWER_ID=alice@example.com \
  python app.py --yaml project.yaml
```

Relative paths in the YAML are resolved from the YAML file's directory. YAML
is required; the former no-YAML Settings mode has been removed. One shared app
URL serves every reviewer. Authentication chooses a `workflow.reviewers[]`
entry, and each assignment limits that reviewer by layer, mode, and H3 cell.
An empty `h3_indexes` list assigns every COG-filtered feature for that layer
and mode.

A project configuration supplies only server-controlled runtime values:

```yaml
version: 3

project:
  id: 'example_fire'
  name: 'example_fire'

# One or more COGs. The first is the primary: it sets the starting view and
# the bounds features are filtered to. With two or more, a switcher appears at
# the top right of the map and B flips between the current and previous
# imagery. A single `paths: {imagery_cog: ...}` still works instead.
imagery:
  - id: post
    name: 'Post-fire'
    source: 'https://example.com/post-fire-imagery.tif'
  - id: pre
    name: 'Pre-fire'
    source: 'https://example.com/pre-fire-imagery.tif'

layers:
  - id: buildings
    source: 'https://example.com/buildings.parquet'
    crs: 'EPSG:4326'
    geometry_types: [Polygon, MultiPolygon]
    fields:
      feature_id: id
      predicted_class: predicted_class
      confidence: confidence
      # Source column naming where each feature came from; polygons drawn in
      # the app store the editor's ID here.
      origin: origin
    h3_resolutions: [5, 6, 7, 8, 9, 10]
    modes: [annotation, editing]
    editing:
      move: true
      reshape: true
      # Turn a polygon around its centre by dragging a corner handle.
      rotate: true

  - id: observations
    source: 'https://example.com/points.parquet'
    crs: 'EPSG:6414'
    geometry_types: [Point]
    fields:
      feature_id: GLOBALID
      predicted_class: DAMAGE
      display: [DAMAGE, STRUCTURETYPE, SITEADDRESS]
      # Shown on mouse hover, under the layer name. Without hover, the
      # tooltip shows the feature ID.
      hover: [DAMAGE, SITEADDRESS]
    h3_resolutions: [5, 6, 7, 8, 9, 10]
    modes: [annotation, editing]
    editing:
      move: true

annotation:
  labels: ['damaged', 'undamaged', 'unknown']

workflow:
  modes: ['annotation', 'editing']
  reviewers:
    - id: 'alice@example.com'
      assignments:
        - layer: buildings
          modes: [annotation, editing]
          h3_indexes:
            - '8828308281fffff'
            - '8828308283fffff'
        - layer: observations
          modes: [annotation]
          h3_indexes: []

    # Assigning the same cell to multiple reviewers preserves independent
    # annotation rows for each person.
    - id: 'bob@example.com'
      assignments:
        - layer: buildings
          modes: [annotation]
          h3_indexes:
            - '8828308281fffff'
          # Optional, used by --auto-assign-hex: at most 50 buildings on
          # bob's list (for example to try the app without a full share).
          max_features: 50
```

Configured sources must be local or public/signed HTTP(S) GeoParquet objects.
DuckDB reads and spatially filters each source once to populate PostGIS.

The standalone fetch utility can retrieve Overture footprints using COG bounds
and a fire date. Its GeoJSON output must be converted to GeoParquet before
database bootstrap:

```bash
python src/fetch_overture_buildings.py \
  --cog path/to/imagery.tif \
  --fire-date 2025-06-28 \
  --output data/features/buildings.geojson
```

Annotation and editing use independent tasks per layer; QA/QC is no longer a
separate configured mode. The layer panel provides annotation plus
capability-specific geometry modes for each eligible layer: point movement and
polygon movement, vertex editing, rotation, deletion, and drawing. The active layer is
interactive and rendered above visible context layers. Point layers remain
hidden until an H3 cell is selected. Geometry changes are saved with optimistic
version checking; only annotation saves automatically advance to the next
feature.

Open `http://127.0.0.1:8501` in a web browser. Project, imagery, labels, modes,
and assignments come from the YAML configuration; the authenticated identity
selects the reviewer entry.

The imagery should appear as well as hexagonal grid cells. Grid cells only appear where features are present. The grid will change scale when you zoom. Zoom to the desired level and select a grid cell by clicking it. Use escape to exit a selected grid cell.

Rendered COG tiles use a shared 2,048-entry process cache on the app server
(the Droplet in production), with 24-hour remote-source refresh intervals.
Set `TILE_CACHE_MAX_TILES` and `TILE_CACHE_TTL_SECONDS` in `.env` to tune these
positive integer limits. The cache fills on demand, can consume a few hundred
MB when full, and clears when the app process restarts. Changed local source
files invalidate their cached tiles. When replacing a remote COG at the same
URL, restart the app to clear its server cache or wait for the refresh interval;
a new source URL has separate cache entries.
At most four raster reads run concurrently, and duplicate requests share the
cached result. Browsers may cache imagery privately for 24 hours. When replacing
imagery at the same URL, use a browser hard refresh to bypass cached tiles;
restarting the server alone does not clear browser caches. Leaflet
requests imagery after zoom animations settle, avoiding intermediate zoom
levels. New uncached views still require remote COG reads. Reload the page
after deploying frontend changes.

The cell will appear as a pink outline. A green circle will show the location of the first feature to annotate.

To begin annotating features, look at the image within the circle, decide what the class is, and pick it from the available buttons under "Annotation Label". When you save the annotation, it will jump to the next feature.  Repeat this process until all features in the hexgrid are complete.  When you are done, the app will exit the hexgrid and zoom back out.

Hexgrid colors will change based on completion.

## Application flow

The detailed flow chart is in [flow_chart.md](flow_chart.md).
