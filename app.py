#!/usr/bin/env python
"""Local map-feature annotation app."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from src.api.feature_store import read_project_features
from src.fetch_overture_buildings import ensure_overture_buildings
from src.project_config import ConfigError, ReviewConfig, load_review_config


DEFAULT_BUILDINGS_PATH = Path("../damage-map-web-map/data/buildings_h3.geojson")
DEFAULT_ANNOTATIONS_PATH = Path("data/qaqc/annotations_local.csv")
DEFAULT_LABEL_FIELD = ""
DEFAULT_ANNOTATION_LABELS = "damaged,undamaged,unknown"
DEFAULT_COG_PATH = ""
DEFAULT_FEATURE_ID_FIELD = 'id'
DEFAULT_H3_PREFIX = 'h3_r'
TILE_SIZE = 256

ANNOTATION_FIELDS = [
    "id",
    "predicted_class",
    "annotation_label",
    "qa_status",
    "qa_correct_class",
    "qa_notes",
    "reviewer",
    "reviewed_at",
]

# *!*! Frontend files remain external assets for direct serving in the container.
FRONTEND_DIR = Path(__file__).resolve().parent / 'frontend'
FRONTEND_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/static/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
}


def empty_png_tile() -> bytes:
    ''' Creates empty tile to be used as a successful blank map tile response'''
    from PIL import Image

    image = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


CogSource = Path | str


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def is_parquet_source(source: Path | str) -> bool:
    '''*!*! Return whether a local path or URL names a Parquet feature source.'''

    source_path = (
        urlparse(str(source)).path
        if is_http_url(str(source))
        else str(source)
    )
    return Path(source_path).suffix.lower() in {'.parquet', '.geoparquet'}


def quote_sql_identifier(value: str) -> str:
    '''*!*! Quote a DuckDB identifier after escaping embedded quotes.'''

    quote = chr(34)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def quote_sql_string(value: str) -> str:
    '''*!*! Quote a DuckDB string literal after escaping apostrophes.'''

    quote = chr(39)
    return f'{quote}{value.replace(quote, quote * 2)}{quote}'


def load_duckdb_extension(connection, name: str) -> None:
    '''*!*! Load a DuckDB extension, installing it when not cached locally.'''

    import duckdb

    try:
        connection.execute(f'LOAD {name}')
    except duckdb.Error:
        connection.execute(f'INSTALL {name}')
        connection.execute(f'LOAD {name}')


def parse_cog_source(value: str) -> CogSource | None:
    '''Parses COG source string into http(s) url, Path, or None'''
    value = value.strip()
    if not value:
        return None
    if is_http_url(value):
        return value
    return Path(value)


def cog_source_exists(source: CogSource) -> bool:
    '''Returns True if source is a string or Path to an existing file'''
    return isinstance(source, str) or source.exists()


def render_cog_tile(cog_source: CogSource, z: int, x: int, y: int) -> bytes:
    '''Returns rendered tile'''
    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader

    try:
        # fetch the tile
        with Reader(str(cog_source)) as cog:
            # TODO: allow for different band orders?
            indexes = (1, 2, 3) if cog.dataset.count >= 3 else (1,)
            tile = cog.tile(x, y, z, indexes=indexes)
            return tile.render(img_format="PNG")
    except TileOutsideBounds:
        # return and empty tile
        return empty_png_tile()


def cog_info(cog_source: CogSource) -> dict[str, object]:
    '''Returns dict containing info about COG'''
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(str(cog_source)) as dataset:
        if dataset.crs is None:
            raise ValueError(f"COG has no CRS: {cog_source}")

        left, bottom, right, top = transform_bounds(
            dataset.crs,
            "EPSG:4326",
            *dataset.bounds,
            densify_pts=21,
        )
        return {
            "path": str(cog_source),
            "crs": dataset.crs.to_string(),
            "width": dataset.width,
            "height": dataset.height,
            "count": dataset.count,
            "bounds": [[bottom, left], [top, right]],
        }


class QaqcStore:
    '''*!*! Read project features and layer persisted review records.'''

    def __init__(
        self,
        buildings_path: Path | str,
        annotations_path: Path,
        annotations_input_path: Path | None = None,
        database_project_id: str | None = None,
        database_reviewer_id: str | None = None,
        feature_id_field: str = DEFAULT_FEATURE_ID_FIELD,
    ):
        '''*!*! Configure feature, database, and annotation sources.'''

        self.buildings_path = buildings_path
        self.annotations_path = annotations_path
        self.annotations_input_path = annotations_input_path
        self.database_project_id = database_project_id
        self.database_reviewer_id = database_reviewer_id
        self.feature_id_field = feature_id_field
        self._write_lock = threading.Lock()

    def _read_parquet_buildings(
        self,
        source: Path | str,
        h3_assignments: dict[str, set[str]],
    ) -> dict:
        '''*!*! Query local or remote GeoParquet and return a FeatureCollection.'''

        try:
            import duckdb
        except ImportError as error:
            raise RuntimeError(
                'DuckDB is required to read GeoParquet feature sources'
            ) from error

        connection = duckdb.connect()
        try:
            load_duckdb_extension(connection, 'spatial')
            if is_http_url(str(source)):
                load_duckdb_extension(connection, 'httpfs')

            description = connection.execute(
                'DESCRIBE SELECT * FROM read_parquet(?)',
                [str(source)],
            ).fetchall()
            geometry_columns = [
                name
                for name, data_type, *_ in description
                if str(data_type).startswith('GEOMETRY')
            ]
            if not geometry_columns:
                raise ValueError(f'GeoParquet has no geometry column: {source}')
            if len(geometry_columns) > 1:
                raise ValueError(
                    f'GeoParquet has multiple geometry columns: {geometry_columns}'
                )

            geometry_column = geometry_columns[0]
            property_columns = [
                name for name, *_ in description if name != geometry_column
            ]
            available_columns = {name for name, *_ in description}
            missing_columns = sorted(set(h3_assignments) - available_columns)
            if missing_columns:
                missing_text = ', '.join(missing_columns)
                raise ValueError(
                    f'GeoParquet is missing assigned H3 column(s): {missing_text}'
                )

            property_items = []
            for column in property_columns:
                property_items.extend(
                    [quote_sql_string(column), quote_sql_identifier(column)]
                )
            property_arguments = ', '.join(property_items)
            properties_sql = (
                f'json_object({property_arguments})'
                if property_items
                else 'json_object()'
            )

            parameters: list[object] = [str(source)]
            assignment_clauses = []
            for column, indexes in sorted(h3_assignments.items()):
                ordered_indexes = sorted(indexes)
                placeholders = ', '.join('?' for _ in ordered_indexes)
                assignment_clauses.append(
                    f'{quote_sql_identifier(column)} IN ({placeholders})'
                )
                parameters.extend(ordered_indexes)
            assignment_filter = ' OR '.join(assignment_clauses)
            where_sql = (
                f'WHERE {assignment_filter}'
                if assignment_clauses
                else ''
            )
            geometry_identifier = quote_sql_identifier(geometry_column)
            rows = connection.execute(
                f'''
SELECT json_object(
    'type', 'Feature',
    'geometry', ST_AsGeoJSON({geometry_identifier})::JSON,
    'properties', {properties_sql}
)
FROM read_parquet(?)
{where_sql}
''',
                parameters,
            ).fetchall()
            return {
                'type': 'FeatureCollection',
                'features': [json.loads(feature_json) for feature_json, in rows],
            }
        except duckdb.Error as error:
            raise RuntimeError(
                f'Could not query GeoParquet feature source {source}: {error}'
            ) from error
        finally:
            connection.close()

    def read_buildings(
        self,
        buildings_path: Path | str | None = None,
        *,
        h3_assignments: dict[str, set[str]] | None = None,
    ) -> dict:
        '''*!*! Load assigned PostGIS features or the configured feature file.'''
        if (
            buildings_path is None
            and self.database_project_id
            and self.database_reviewer_id
            and os.environ.get('DATABASE_URL')
        ):
            return read_project_features(
                self.database_project_id,
                self.database_reviewer_id,
                feature_id_field=self.feature_id_field,
            )

        source = self.buildings_path if buildings_path is None else buildings_path
        assignments = h3_assignments or {}
        if is_parquet_source(source):
            return self._read_parquet_buildings(source, assignments)
        if isinstance(source, str) and is_http_url(source):
            request = Request(
                source,
                headers={
                    'Accept': 'application/geo+json, application/json',
                    'User-Agent': 'damagemap-qaqc/1.0',
                },
            )
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        path = Path(source)
        with path.open(encoding='utf-8') as file:
            return json.load(file)

    @staticmethod
    def _read_annotation_file(path: Path | None) -> dict[str, dict[str, str]]:
        '''*!*! Read one annotation CSV and normalize merged wide rows.'''

        if path is None or not path.exists():
            return {}

        with path.open(newline='', encoding='utf-8') as file:
            annotations = {}
            for row in csv.DictReader(file):
                if not row.get('id'):
                    continue
                # Wide files from merge_qaqc_annotations.py have one label
                # column per reviewer. Treat them as complete without exposing
                # a previous reviewer's label in the blinded UI.
                reviewer_labels = [
                    value
                    for key, value in row.items()
                    if key.endswith('_annotation_label') and value
                ]
                if not row.get('annotation_label') and reviewer_labels:
                    row['qa_status'] = row.get('qa_status') or 'annotated'
                annotations[row['id']] = row
            return annotations

    def read_annotations(self) -> dict[str, dict[str, str]]:
        '''*!*! Overlay this user's output records on merged input records.'''
        # TODO: what does this do? Do we need it?
        annotations = self._read_annotation_file(self.annotations_input_path)
        annotations.update(self._read_annotation_file(self.annotations_path))
        return annotations

    def write_annotation(self, annotation: dict[str, str]) -> dict[str, str]:
        '''
        Atomically add or replace one record in the user output
        TODO: currently csv, this is where it should write to DB.
        '''

        with self._write_lock:
            # Only rewrite this user's output. Loaded merged input remains read-only.
            annotations = self._read_annotation_file(self.annotations_path)
            annotation = {field: annotation.get(field, '') for field in ANNOTATION_FIELDS}
            annotation['reviewed_at'] = datetime.now(timezone.utc).isoformat()
            annotations[annotation['id']] = annotation

            self.annotations_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    'w',
                    dir=self.annotations_path.parent,
                    newline='',
                    encoding='utf-8',
                    delete=False,
                ) as file:
                    temp_path = Path(file.name)
                    writer = csv.DictWriter(file, fieldnames=ANNOTATION_FIELDS)
                    writer.writeheader()
                    writer.writerows(annotations.values())
                os.replace(temp_path, self.annotations_path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        return annotation


def assignment_columns(
    review_config: ReviewConfig | None,
) -> dict[str, set[str]]:
    '''*!*! Group configured H3 assignments by their feature column.'''

    if review_config is None:
        return {}

    import h3

    assignments: dict[str, set[str]] = {}
    for index in review_config.todo_h3_indexes:
        column = f'{review_config.h3_prefix}{h3.get_resolution(index)}'
        assignments.setdefault(column, set()).add(index)
    return assignments


def filter_buildings_for_assignments(
    buildings: dict,
    review_config: ReviewConfig | None,
) -> dict:
    '''*!*! Return only features belonging to assigned H3 cells.'''

    if review_config is None or not review_config.todo_h3_indexes:
        return buildings

    assignments = assignment_columns(review_config)

    filtered = dict(buildings)
    filtered['features'] = [
        feature
        for feature in buildings.get('features', [])
        if any(
            str(feature.get('properties', {}).get(column, '')) in indexes
            for column, indexes in assignments.items()
        )
    ]
    return filtered


def prepare_configured_features(review_config: ReviewConfig) -> None:
    '''*!*! Fetch or reuse YAML-configured Overture buildings before serving.'''

    if review_config.overture is None:
        return
    result = ensure_overture_buildings(
        cog_source=review_config.imagery_cog,
        fire_date=review_config.overture.fire_date,
        output_path=review_config.features_path,
        h3_prefix=review_config.h3_prefix,
        release_selection=review_config.overture.release_selection,
        refresh=review_config.overture.refresh,
    )
    action = 'Reused cached' if result.from_cache else 'Fetched'
    selection_text = (
        f'before {review_config.overture.fire_date}'
        if review_config.overture.release_selection == 'before_fire'
        else 'oldest available fallback'
    )
    print(
        f'{action} {result.feature_count:,} Overture buildings '
        f'from {result.release} ({selection_text})'
    )


def make_handler(
    store: QaqcStore,
    review_config: ReviewConfig | None = None,
    mode_stores: dict[str, QaqcStore] | None = None,
):
    '''*!*! Build an HTTP handler bound to project config and mode stores.'''

    stores = mode_stores or {'annotation': store}
    configured_buildings = None
    assigned_feature_ids = None
    assignments = assignment_columns(review_config)
    if review_config is not None:
        configured_buildings = filter_buildings_for_assignments(
            store.read_buildings(h3_assignments=assignments),
            review_config,
        )
        assigned_feature_ids = {
            str(feature.get('properties', {}).get(review_config.feature_id_field))
            for feature in configured_buildings.get('features', [])
            if feature.get('properties', {}).get(review_config.feature_id_field) is not None
        }

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                return

        def frontend_config(self) -> dict[str, object]:
            '''*!*! Return browser runtime values without templating static files.'''

            # *!*! These defaults preserve the settings-menu workflow when YAML is absent.
            config = {
                'defaultBuildingsPath': str(store.buildings_path),
                'defaultLabelField': DEFAULT_LABEL_FIELD,
                'defaultAnnotationLabels': DEFAULT_ANNOTATION_LABELS,
                'defaultCogPath': DEFAULT_COG_PATH,
                'defaultConfidenceField': '',
                'yamlConfigured': False,
                'configuredProjectName': 'Feature Annotator',
                'configuredFeatureIdField': DEFAULT_FEATURE_ID_FIELD,
                'configuredH3Prefix': DEFAULT_H3_PREFIX,
                'configuredUser': '',
                'workflowModes': ['annotation'],
                'todoH3Indexes': [],
            }
            if review_config is None:
                return config

            # *!*! YAML mode makes server-approved project and assignment values authoritative.
            config.update({
                'defaultLabelField': review_config.predicted_class_field,
                'defaultAnnotationLabels': ','.join(review_config.annotation_labels),
                'defaultCogPath': review_config.imagery_cog,
                'defaultConfidenceField': review_config.confidence_field,
                'yamlConfigured': True,
                'configuredProjectName': review_config.project_name,
                'configuredFeatureIdField': review_config.feature_id_field,
                'configuredH3Prefix': review_config.h3_prefix,
                'configuredUser': review_config.user,
                'workflowModes': list(review_config.modes),
                'todoH3Indexes': list(review_config.todo_h3_indexes),
            })
            return config

        def requested_buildings_source(self) -> Path | str:
            '''*!*! Returns the configured local or remote feature source.'''

            if review_config is not None:
                return store.buildings_path
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            if not value:
                return store.buildings_path
            return value if is_http_url(value) else Path(value)

        def requested_cog_source(self) -> CogSource | None:
            if review_config is not None:
                return parse_cog_source(review_config.imagery_cog)
            query = parse_qs(urlparse(self.path).query)
            value = query.get("path", [""])[0].strip()
            return parse_cog_source(value)

        def requested_annotation_store(self) -> QaqcStore | None:
            '''*!*! Return the configured persistence store for a requested mode.'''

            query = parse_qs(urlparse(self.path).query)
            mode = query.get('mode', ['annotation'])[0].strip().lower()
            return stores.get(mode)

        def tile_coordinates(self) -> tuple[int, int, int]:
            path = urlparse(self.path).path
            tile_path = path.removeprefix("/api/cog/tile/").removesuffix(".png")
            z_text, x_text, y_text = tile_path.split("/")
            return int(z_text), int(x_text), int(y_text)

        def send_json(self, body: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def send_frontend_file(self, request_path: str) -> None:
            '''*!*! Serve one allowlisted frontend asset with its explicit MIME type.'''

            file_name, content_type = FRONTEND_FILES[request_path]
            asset_path = FRONTEND_DIR / file_name
            try:
                encoded = asset_path.read_bytes()
            except OSError as error:
                self.send_text(
                    f'Frontend asset unavailable: {error}',
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            # *!*! Disable caching while assets are unversioned and edited in place.
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                return

        def send_png(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in FRONTEND_FILES:
                self.send_frontend_file(path)
                return

            if path == '/api/config':
                self.send_json(self.frontend_config())
                return

            if path == "/api/buildings":
                if configured_buildings is not None:
                    self.send_json(configured_buildings)
                    return
                buildings_source = self.requested_buildings_source()
                if (
                    isinstance(buildings_source, Path)
                    and not buildings_source.exists()
                ):
                    self.send_text(
                        f"Feature file not found: {buildings_source}",
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                buildings = store.read_buildings(
                    buildings_source,
                    h3_assignments=assignments,
                )
                self.send_json(filter_buildings_for_assignments(buildings, review_config))
                return

            if path == "/api/annotations":
                annotation_store = self.requested_annotation_store()
                if annotation_store is None:
                    self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                    return
                self.send_json(annotation_store.read_annotations())
                return

            if path == "/api/cog/info":
                cog_source = self.requested_cog_source()
                if cog_source is None:
                    self.send_text("COG path or URL is required", HTTPStatus.BAD_REQUEST)
                    return
                if not cog_source_exists(cog_source):
                    self.send_text(f"COG file not found: {cog_source}", HTTPStatus.NOT_FOUND)
                    return
                try:
                    self.send_json(cog_info(cog_source))
                except Exception as error:
                    self.send_text(str(error), HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            if path.startswith("/api/cog/tile/") and path.endswith(".png"):
                cog_source = self.requested_cog_source()
                if cog_source is None:
                    self.send_text("COG path or URL is required", HTTPStatus.BAD_REQUEST)
                    return
                if not cog_source_exists(cog_source):
                    self.send_text(f"COG file not found: {cog_source}", HTTPStatus.NOT_FOUND)
                    return
                try:
                    z, x, y = self.tile_coordinates()
                    self.send_png(render_cog_tile(cog_source, z, x, y))
                except Exception as error:
                    self.send_text(str(error), HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            self.send_text("Not found", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/annotations":
                self.send_text("Not found", HTTPStatus.NOT_FOUND)
                return

            annotation_store = self.requested_annotation_store()
            if annotation_store is None:
                self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")

            if not payload.get("id"):
                self.send_text("Missing feature id", HTTPStatus.BAD_REQUEST)
                return

            if assigned_feature_ids is not None and str(payload['id']) not in assigned_feature_ids:
                self.send_text('Feature is not assigned to this user', HTTPStatus.FORBIDDEN)
                return

            if review_config is not None:
                if payload.get('annotation_label') not in review_config.annotation_labels:
                    self.send_text('Invalid annotation label', HTTPStatus.BAD_REQUEST)
                    return
                payload['reviewer'] = review_config.user

            self.send_json(annotation_store.write_annotation(payload))

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line arguments for fallback and YAML modes.'''

    parser = argparse.ArgumentParser(description="Run the local map-feature annotation app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        '--yaml',
        type=Path,
        help=(
            'Project YAML. Relative paths inside it are resolved from the YAML '
            'directory; when omitted, the in-app Settings panel is available.'
        ),
    )
    parser.add_argument(
        "--buildings",
        type=Path,
        default=DEFAULT_BUILDINGS_PATH,
        help=f"Feature GeoJSON path. Default: {DEFAULT_BUILDINGS_PATH}",
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=DEFAULT_ANNOTATIONS_PATH,
        help=f"Annotation CSV path. Default: {DEFAULT_ANNOTATIONS_PATH}",
    )
    return parser


def browser_url(host: str, port: int) -> str:
    '''*!*! Return a browser-friendly URL for a bound application server.'''

    # *!*! Wildcard and loopback bindings are reached through localhost in a browser.
    browser_host = (
        'localhost'
        if host in {'0.0.0.0', '127.0.0.1', '::', '::1', 'localhost'}
        else host
    )
    if ':' in browser_host and not browser_host.startswith('['):
        browser_host = f'[{browser_host}]'
    return f'http://{browser_host}:{port}'


def main() -> None:
    '''*!*! Load configuration and serve the annotation application.'''

    args = build_parser().parse_args()
    try:
        review_config = load_review_config(args.yaml) if args.yaml else None
    except ConfigError as error:
        raise SystemExit(f'Invalid project configuration: {error}') from error

    if review_config is not None:
        try:
            prepare_configured_features(review_config)
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f'Could not prepare Overture buildings: {error}') from error

    buildings_path = review_config.features_path if review_config else args.buildings
    annotations_input_path = None
    annotations_path = args.annotations
    # this reads modes from config
    mode_stores = None
    if review_config:
        mode_stores = {}
        if 'annotation' in review_config.modes:
            mode_stores['annotation'] = QaqcStore(
                buildings_path,
                review_config.annotations_output,
                review_config.annotations_input,
                review_config.project_id,
                review_config.user,
                review_config.feature_id_field,
            )
        if 'qaqc' in review_config.modes:
            mode_stores['qaqc'] = QaqcStore(
                buildings_path,
                review_config.qaqc_output,
                review_config.qaqc_input,
                review_config.project_id,
                review_config.user,
                review_config.feature_id_field,
            )
        if not mode_stores:
            raise SystemExit(
                'Editing-only projects are not supported yet; include annotation or qaqc mode'
            )
        first_mode = next(mode for mode in review_config.modes if mode in mode_stores)
        first_store = mode_stores[first_mode]
        annotations_input_path = first_store.annotations_input_path
        annotations_path = first_store.annotations_path

    if annotations_path is None:
        raise SystemExit('The active workflow has no annotation output path')

    store = QaqcStore(
        buildings_path,
        annotations_path,
        annotations_input_path,
        review_config.project_id if review_config else None,
        review_config.user if review_config else None,
        review_config.feature_id_field if review_config else DEFAULT_FEATURE_ID_FIELD,
    )
    try:
        handler = make_handler(store, review_config, mode_stores)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'Could not load project features: {error}') from error
    server = ThreadingHTTPServer((args.host, args.port), handler)
    bound_port = server.server_address[1]
    print(f'Annotation app: {browser_url(args.host, bound_port)}', flush=True)
    if review_config:
        modes_text = ', '.join(review_config.modes)
        print(f'Configuration: {review_config.source_path}')
        print(f'Project: {review_config.project_name}')
        print(f'User: {review_config.user}')
        print(f'Modes: {modes_text}')
        print(f'Assigned H3 cells: {len(review_config.todo_h3_indexes):,}')
    feature_source = (
        f'PostGIS project {review_config.project_id}'
        if review_config and os.environ.get('DATABASE_URL')
        else store.buildings_path
    )
    print(f'Features: {feature_source}')
    if mode_stores:
        for mode, mode_store in mode_stores.items():
            if mode_store.annotations_input_path:
                print(f'{mode.title()} input: {mode_store.annotations_input_path}')
            print(f'{mode.title()} output: {mode_store.annotations_path}')
    else:
        if store.annotations_input_path:
            print(f'Existing annotations: {store.annotations_input_path}')
        print(f'Annotations: {store.annotations_path}')
    server.serve_forever()


if __name__ == "__main__":
    main()
