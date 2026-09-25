

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

Run the app against a migrated and bootstrapped database:

```bash
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
migrations. The `bootstrap` service then imports each configured GeoParquet
layer into PostGIS once, filters it to the COG, generates normalized H3 rows,
and synchronizes layer tasks and reviewer assignments before either server
starts.

The UI reads reviewer-assigned features and review records from PostGIS.
Annotation submissions are written directly to the `annotations` table and
preserve independent records for each reviewer.

Each `layers[].source` must point to local or HTTP(S) GeoParquet.

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

Relative paths in the YAML are resolved from the YAML file's directory. YAML
is required; the former no-YAML Settings mode has been removed. The technician
name is fixed by `workflow.user`, and only features inside the H3 cells listed
under `workflow.todo` are loaded into the work queue. An empty TODO list makes
every feature available.

A project configuration supplies only server-controlled runtime values:

```yaml
version: 2

project:
  id: 'example_fire'
  name: 'example_fire'

paths:
  imagery_cog: 'https://example.com/post-fire-imagery.tif'

layers:
  - id: buildings
    source: 'https://example.com/buildings.parquet'
    crs: 'EPSG:4326'
    geometry_types: [Polygon, MultiPolygon]
    fields:
      feature_id: id
      predicted_class: predicted_class
      confidence: confidence
    h3_resolutions: [5, 6, 7, 8, 9, 10]
    modes: [annotation, editing]
    editing:
      move: true
      reshape: true

  - id: observations
    source: 'https://example.com/points.parquet'
    crs: 'EPSG:6414'
    geometry_types: [Point]
    fields:
      feature_id: GLOBALID
      predicted_class: DAMAGE
      display: [DAMAGE, STRUCTURETYPE, SITEADDRESS]
    h3_resolutions: [5, 6, 7, 8, 9, 10]
    modes: [annotation, editing]
    editing:
      move: true

annotation:
  labels: ['damaged', 'undamaged', 'unknown']

workflow:
  user: 'alice'
  modes: ['annotation', 'editing']
  todo: []
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
separate configured mode. The layer panel provides an annotation or
geometry-editing mode for each eligible layer. The active layer is interactive
and rendered above visible context layers. Point layers remain hidden until an
H3 cell is selected. Point placement and Leaflet-Geoman polygon changes are
saved with optimistic version checking.

Open `http://127.0.0.1:8501` in a web browser. Project, imagery, labels,
reviewer, modes, and assignments come from the YAML configuration.

The imagery should appear as well as hexagonal grid cells. Grid cells only appear where features are present. The grid will change scale when you zoom. Zoom to the desired level and select a grid cell by clicking it. Use escape to exit a selected grid cell.

The cell will appear as a pink outline. A green circle will show the location of the first feature to annotate.

To begin annotating features, look at the image within the circle, decide what the class is, and pick it from the available buttons under "Annotation Label". When you save the annotation, it will jump to the next feature.  Repeat this process until all features in the hexgrid are complete.  When you are done, the app will exit the hexgrid and zoom back out.

Hexgrid colors will change based on completion.

## Application flow

The detailed flow chart is in [flow_chart.md](flow_chart.md).
