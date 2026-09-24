

## Data requirements

The application requires three inputs:

1. a GeoJSON (in EPSG:4326) that  has:
    + polygon or multipolygon geometries
    + an id field
    + H3 columns h3_r5 through h3_r10


2. An annotation label list entered in the app, or an optional label field in the GeoJSON whose unique values can populate the annotation buttons.

3. A COG of imagery for the annotator to use as comparison.

## Helper scripts

Chances are you do not have H3 indexes  attached to your polygons.  You can use `src/add_h3_indexes.py` to attach them.

For building a COG of imagery , `src/build_cog.py` has been provided

The file `src/merge_qaqc_annotations.py` combines per-annotator CSVs into one wide CSV keyed by feature id, preserving each annotator’s latest annotation label, notes, and timestamp in annotator-specific columns.



## Usage

Build the conda environment:

```bash
conda env create -f environment.yml
conda activate damagemap-qaqc
```

If your polygons do not already have H3 columns, add them first:

```bash
python src/add_h3_indexes.py --input path/to/features.geojson --output path/to/features_h3.geojson
```

If needed, build a COG from a directory of TIFF imagery:

```bash
python src/build_cog.py path/to/imagery_dir
```

Run the app:

```bash
python app.py
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
docker compose build
docker compose up -d
```

Open `http://127.0.0.1:8501`. The app binds only to localhost by default, as
does PostGIS on port `5432`. Stop the stack with `docker compose down`; the
named `postgis_data` volume survives container replacement.

In attached mode, `docker compose up` waits for the UI and API health checks,
then prints their host-facing URLs using `APP_PORT` and `API_PORT` from `.env`.
Detached mode does not display service logs; use `docker compose logs startup`
to show the same links after `docker compose up -d`.

The shared FastAPI service runs at `http://127.0.0.1:8000`, with interactive
documentation at `/docs`. The one-shot `migrate` service applies ordered SQL
migrations. The `bootstrap` service then imports the configured GeoParquet
into PostGIS once, normalizes its H3 columns, and synchronizes configured tasks
and reviewer assignments before either server starts.

In Compose, the UI reads reviewer-assigned features from PostGIS. It still
writes review CSVs beneath `data/`; moving those writes to the shared API is
the next database integration phase.

Database bootstrap requires `paths.features` to point to local or HTTP(S)
GeoParquet. File-based app runs outside Compose continue to support GeoJSON.

During development, API requests require an `X-Reviewer-ID` header. This mode
is intentionally disabled when `AUTH_MODE` is anything other than
`development`; production OIDC authentication will replace it.

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

For an assigned project, pass its YAML file to the app:

```bash
python app.py --yaml project.yaml
```

Relative paths in the YAML are resolved from the YAML file's directory, and
`{user}` in output paths is replaced with `workflow.user`. In YAML mode the
in-app Settings panel is hidden, the technician name is locked, and only
features inside the H3 cells listed under `workflow.todo` are loaded into the
work queue. An empty TODO list makes every feature available.

YAML projects can fetch pre-fire Overture building footprints from the Fused
Source Cooperative mirror when they start:

```yaml
project:
  id: 'example_fire'
  name: 'example_fire'
  fire_date: '2025-06-28T11:12:56Z'

paths:
  features: None
  imagery_cog: 'https://example.com/post-fire-imagery.tif'

overture:
  refresh: false
```

Set `paths.features` to `None` to select the newest Fused Overture release
whose release date is before `project.fire_date`. Set it to `oldest` to
explicitly use the oldest mirrored release, including when no release predates
the fire. Any other value is treated as an existing feature source and
disables the Fused fetch. This may be a local path or a public/signed HTTP(S)
URL to a GeoJSON or GeoParquet object in S3, R2, or another object store.

Remote GeoJSON is fetched by the Python server once during startup. Remote
GeoParquet is queried through DuckDB using HTTP range requests, with configured
H3 assignments included in the query. Selected features are converted to
GeoJSON and retained in memory; they are not progressively streamed
feature-by-feature to the browser.

For automatic fetching, the app derives an EPSG:4326 bounding box from the COG.
It queries only Parquet row groups that overlap that box, clips the resulting
building geometries to it, adds the configured H3 columns, and writes the
GeoJSON directly under `data/` beside the project YAML.

The GeoJSON and its `.overture.json` sidecar are a startup cache. A matching
cache is reused without listing releases or querying building data. Set
`overture.refresh: true` for one deliberate rebuild, then return it to
`false`. The Fused mirror currently has no release before February 2024, so
older fire dates cannot use this source for genuinely pre-fire footprints.

The same fetch can be run separately:

```bash
python src/fetch_overture_buildings.py \
  --cog path/to/imagery.tif \
  --fire-date 2025-06-28 \
  --output data/features/buildings.geojson
```

`annotation` and `qaqc` can be enabled together and use independent input and
output CSVs. Existing input files are treated as read-only; the current user's
work is written only to the configured `{user}` output. The `editing` mode and
output path are accepted by the configuration format, but geometry editing is
not implemented yet, so an editing-only project cannot be started.

Open `http://127.0.0.1:8501` in a web browser, set the feature GeoJSON, annotation labels, optional local COG path or COG URL, and annotator name. The label field is optional; when supplied, its unique values are used as annotation button options. If class probability is available, you can select that field from the GeoJSON and filter features by model confidence.

The imagery should appear as well as hexagonal grid cells. Grid cells only appear where features are present. The grid will change scale when you zoom. Zoom to the desired level and select a grid cell by clicking it. Use escape to exit a selected grid cell.

The cell will appear as a pink outline. A green circle will show the location of the first feature to annotate.

To begin annotating features, look at the image within the circle, decide what the class is, and pick it from the available buttons under "Annotation Label". When you save the annotation, it will jump to the next feature.  Repeat this process until all features in the hexgrid are complete.  When you are done, the app will exit the hexgrid and zoom back out.

Hexgrid colors will change based on completion.

## Application flow

The detailed flow chart is in [flow_chart.md](flow_chart.md). A rendered vector
PDF is available at [flow_chart.pdf](flow_chart.pdf) for zooming and scrolling.
