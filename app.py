#!/usr/bin/env python
"""Local map-feature annotation app."""

from __future__ import annotations

import argparse
import io
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.api.feature_store import (
    FeatureStoreError,
    read_project_features,
    update_project_geometry,
)
from src.api.review_store import (
    ReviewStoreError,
    annotation_key,
    read_reviewer_annotations,
    write_reviewer_annotation,
)
from src.project_config import ConfigError, ReviewConfig, load_review_config


TILE_SIZE = 256

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
        database_project_id: str,
        database_reviewer_id: str,
    ):
        '''*!*! Configure the PostGIS project and reviewer identity.'''

        self.database_project_id = database_project_id
        self.database_reviewer_id = database_reviewer_id

    def read_buildings(self) -> dict:
        '''*!*! Load this reviewer's assigned features from PostGIS.'''

        return read_project_features(
            self.database_project_id,
            self.database_reviewer_id,
        )

    def read_annotations(self, mode: str) -> dict[str, dict[str, object]]:
        '''*!*! Read this reviewer's current PostGIS records for one mode.'''

        if not self.database_project_id or not self.database_reviewer_id:
            raise RuntimeError('Database project and reviewer are required')
        return read_reviewer_annotations(
            self.database_project_id,
            self.database_reviewer_id,
            mode,
        )

    def write_annotation(
        self,
        annotation: dict[str, object],
        mode: str,
    ) -> dict[str, object]:
        '''*!*! Persist one annotation record in PostGIS.'''

        if not self.database_project_id or not self.database_reviewer_id:
            raise RuntimeError('Database project and reviewer are required')
        return write_reviewer_annotation(
            self.database_project_id,
            self.database_reviewer_id,
            mode,
            annotation,
        )

    def update_geometry(self, edit: dict[str, object]) -> dict[str, object]:
        '''*!*! Persist one assigned point or polygon geometry edit.'''

        return update_project_geometry(
            self.database_project_id,
            self.database_reviewer_id,
            str(edit['layer_id']),
            str(edit['id']),
            int(edit['expected_version']),
            edit['geometry'],
        )


def make_handler(
    store: QaqcStore,
    review_config: ReviewConfig,
):
    '''*!*! Build an HTTP handler bound to one database-backed project.'''

    configured_buildings = store.read_buildings()
    assigned_feature_keys = {
        annotation_key(str(feature.get('layer_id', '')), str(feature.get('id', '')))
        for feature in configured_buildings.get('features', [])
        if feature.get('layer_id') is not None and feature.get('id') is not None
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                return

        def frontend_config(self) -> dict[str, object]:
            '''*!*! Return browser runtime values without templating static files.'''

            return {
                'defaultAnnotationLabels': ','.join(review_config.annotation_labels),
                'defaultCogPath': review_config.imagery_cog,
                'configuredProjectName': review_config.project_name,
                'configuredUser': review_config.user,
                'workflowModes': list(review_config.modes),
                'todoH3Indexes': list(review_config.todo_h3_indexes),
                'layers': [
                    {
                        'id': layer.id,
                        'name': layer.name,
                        'geometryTypes': list(layer.geometry_types),
                        'modes': list(layer.modes),
                        'fields': {
                            'featureId': layer.feature_id_field,
                            'predictedClass': layer.predicted_class_field,
                            'confidence': layer.confidence_field,
                            'display': list(layer.display_fields),
                        },
                        'editing': vars(layer.editing),
                    }
                    for layer in review_config.layers
                ],
            }

        def requested_cog_source(self) -> CogSource | None:
            return parse_cog_source(review_config.imagery_cog)

        def requested_review_mode(self) -> str | None:
            '''*!*! Return an enabled database review mode from the request.'''

            query = parse_qs(urlparse(self.path).query)
            mode = query.get('mode', ['annotation'])[0].strip().lower()
            if mode != 'annotation' or mode not in review_config.modes:
                return None
            return mode

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
                self.send_json(configured_buildings)
                return

            if path == "/api/annotations":
                mode = self.requested_review_mode()
                if mode is None:
                    self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                    return
                try:
                    self.send_json(store.read_annotations(mode))
                except ReviewStoreError as error:
                    self.send_text(str(error), error.status)
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

            mode = self.requested_review_mode()
            if mode is None:
                self.send_text('Workflow mode is not enabled', HTTPStatus.BAD_REQUEST)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length) or b"{}")

            if not payload.get("id"):
                self.send_text("Missing feature id", HTTPStatus.BAD_REQUEST)
                return

            if not payload.get('layer_id'):
                self.send_text('Missing layer id', HTTPStatus.BAD_REQUEST)
                return

            feature_key = annotation_key(
                str(payload['layer_id']),
                str(payload['id']),
            )
            if feature_key not in assigned_feature_keys:
                self.send_text('Feature is not assigned to this user', HTTPStatus.FORBIDDEN)
                return

            if payload.get('annotation_label') not in review_config.annotation_labels:
                self.send_text('Invalid annotation label', HTTPStatus.BAD_REQUEST)
                return
            try:
                self.send_json(store.write_annotation(payload, mode))
            except ReviewStoreError as error:
                self.send_text(str(error), error.status)

        def do_PATCH(self) -> None:
            '''*!*! Accept same-origin point and polygon edits from the browser map.'''

            path = urlparse(self.path).path
            if path != '/api/features':
                self.send_text('Not found', HTTPStatus.NOT_FOUND)
                return

            content_length = int(self.headers.get('Content-Length', '0'))
            try:
                payload = json.loads(self.rfile.read(content_length) or b'{}')
            except json.JSONDecodeError:
                self.send_text('Invalid JSON', HTTPStatus.BAD_REQUEST)
                return
            required = {'id', 'layer_id', 'expected_version', 'geometry'}
            if not required.issubset(payload):
                self.send_text('Incomplete feature edit', HTTPStatus.BAD_REQUEST)
                return
            try:
                self.send_json(store.update_geometry(payload))
            except (TypeError, ValueError):
                self.send_text('Invalid feature version', HTTPStatus.BAD_REQUEST)
            except FeatureStoreError as error:
                self.send_text(str(error), error.status)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    '''*!*! Build command-line arguments for YAML-configured database mode.'''

    parser = argparse.ArgumentParser(description="Run the local map-feature annotation app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        '--yaml',
        type=Path,
        required=True,
        help='Project YAML; relative paths resolve from its directory.',
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
        review_config = load_review_config(args.yaml)
    except ConfigError as error:
        raise SystemExit(f'Invalid project configuration: {error}') from error

    if not os.environ.get('DATABASE_URL'):
        raise SystemExit('DATABASE_URL is required')
    if 'annotation' not in review_config.modes:
        raise SystemExit(
            'Editing-only projects are not supported yet; include annotation mode'
        )

    store = QaqcStore(
        review_config.project_id,
        review_config.user,
    )
    try:
        handler = make_handler(store, review_config)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f'Could not load project features: {error}') from error
    server = ThreadingHTTPServer((args.host, args.port), handler)
    bound_port = server.server_address[1]
    print(f'Annotation app: {browser_url(args.host, bound_port)}', flush=True)
    modes_text = ', '.join(review_config.modes)
    print(f'Configuration: {review_config.source_path}')
    print(f'Project: {review_config.project_name}')
    print(f'User: {review_config.user}')
    print(f'Modes: {modes_text}')
    print(f'Layers: {len(review_config.layers):,}')
    print(f'Assigned H3 cells per layer: {len(review_config.todo_h3_indexes):,}')
    print(f'Features and reviews: PostGIS project {review_config.project_id}')
    server.serve_forever()


if __name__ == "__main__":
    main()
