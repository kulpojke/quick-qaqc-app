from io import BytesIO
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import FRONTEND_DIR, QaqcStore, browser_url, make_handler
from src.project_config import ReviewerAssignmentConfig, ReviewerConfig


class FrontendTests(unittest.TestCase):
    '''*!*! Verify static frontend files and their Python server boundary.'''

    def review_config(self, layers=(), modes=('annotation',)):
        '''*!*! Build a reviewer-aware runtime configuration for handler tests.'''

        assignments = tuple(
            ReviewerAssignmentConfig(
                layer_id=layer.id,
                modes=tuple(mode for mode in layer.modes if mode in modes),
                h3_indexes=(),
            )
            for layer in layers
        )
        reviewer = ReviewerConfig(id='alice', assignments=assignments)
        config = SimpleNamespace(
            project_id='project-one',
            annotation_labels=('damaged', 'undamaged'),
            imagery_cog='',
            project_name='Test project',
            modes=modes,
            reviewers=(reviewer,),
            layers=layers,
        )
        config.reviewer = lambda reviewer_id: (
            reviewer if reviewer_id == reviewer.id else None
        )
        return config

    def handler_class(self, store, config):
        '''*!*! Bind a test store to one authenticated reviewer.'''

        return make_handler(
            config,
            store_factory=lambda project_id, reviewer_id: store,
            identity_resolver=lambda headers, **kwargs: 'alice',
        )

    def test_browser_url_uses_localhost_for_local_bindings(self):
        '''*!*! Startup URLs remain clickable when binding locally or in Docker.'''

        self.assertEqual(browser_url('127.0.0.1', 8501), 'http://localhost:8501')
        self.assertEqual(browser_url('0.0.0.0', 8501), 'http://localhost:8501')
        self.assertEqual(browser_url('::', 8501), 'http://localhost:8501')
        self.assertEqual(
            browser_url('review.example', 8501),
            'http://review.example:8501',
        )

    def test_frontend_is_split_into_static_files(self):
        '''*!*! HTML references separate CSS and JavaScript without template tokens.'''

        html = (FRONTEND_DIR / 'index.html').read_text(encoding='utf-8')
        javascript = (FRONTEND_DIR / 'app.js').read_text(encoding='utf-8')

        self.assertIn('href="/static/styles.css"', html)
        self.assertIn('src="/static/app.js"', html)
        self.assertIn("fetch('/api/config')", javascript)
        self.assertNotIn('__DEFAULT_', html + javascript)
        self.assertNotIn('settings-panel', html)
        self.assertNotIn('localStorage', javascript)
        self.assertIn('pointToLayer', javascript)
        self.assertIn('layer_id: selectedFeature.layer_id', javascript)
        self.assertIn('id="layer-list"', html)
        self.assertIn('function renderLayerPanel()', javascript)
        self.assertIn("fetch('/api/features'", javascript)
        self.assertIn('function indexFeatures()', javascript)
        self.assertIn("document.body.dataset.activeMode = activeTool.mode", javascript)
        self.assertIn("const sameLayer = activeTool.layerId === layerId", javascript)
        self.assertIn("selectedFeatureLayer.getLatLng()", javascript)
        self.assertIn("map.setView(layer.getLatLng()", javascript)
        self.assertIn("button.setAttribute(\n        'aria-pressed'", javascript)
        self.assertIn("mode: 'polygon-move'", javascript)
        self.assertIn("mode: 'polygon-reshape'", javascript)
        self.assertIn("mode: 'polygon-delete'", javascript)
        self.assertIn("mode: 'polygon-create'", javascript)
        self.assertIn("method: 'DELETE'", javascript)
        self.assertIn("map.on('pm:create'", javascript)
        self.assertIn('bubblingMouseEvents: false', javascript)
        self.assertIn('function enablePointDragging()', javascript)
        self.assertIn("selectedFeatureLayer.on('pm:dragend', capturePointDraft)", javascript)
        self.assertNotIn('stagePointPlacement', javascript)
        self.assertIn("fillColor: '#0ea5e9'", javascript)
        self.assertIn('leaflet-geoman', html)
        self.assertNotIn('workflow-mode-buttons', html)
        self.assertNotIn('id="project-name"', html)
        self.assertNotIn('id="workflow-summary"', html)
        self.assertNotIn('Select an H3 cell', html)
        self.assertNotIn('id="reviewer"', html)
        self.assertNotIn('reviewerInput', javascript)
        self.assertNotIn('configuredUser', javascript)
        self.assertIn('layer.assignments[candidateMode]', javascript)
        self.assertIn('featureIsAssigned(feature, assignmentMode)', javascript)
        self.assertNotIn("getElementById('project-name')", javascript)

    def test_handler_serves_frontend_assets_and_runtime_config(self):
        '''*!*! Handler routes expose static assets plus JSON runtime configuration.'''

        store = QaqcStore('project-one', 'alice')
        store.read_buildings = Mock(
            return_value={'type': 'FeatureCollection', 'features': []},
        )
        config = self.review_config(
            layers=(
                SimpleNamespace(
                    id='buildings',
                    name='Buildings',
                    geometry_types=('Polygon',),
                    modes=('annotation',),
                    feature_id_field='id',
                    predicted_class_field='predicted_class',
                    confidence_field='',
                    display_fields=(),
                    editing=SimpleNamespace(
                        move=False,
                        reshape=False,
                        create=False,
                        delete=False,
                    ),
                ),
            ),
            modes=('annotation',),
        )
        handler_class = self.handler_class(store, config)
        handler = handler_class.__new__(handler_class)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = BytesIO()
        handler.headers = {}

        # *!*! Exercise route dispatch without requiring a sandboxed network socket.
        handler.path = '/static/app.js'
        handler.do_GET()
        self.assertIn(b"fetch('/api/config')", handler.wfile.getvalue())
        handler.send_header.assert_any_call(
            'Content-Type',
            'text/javascript; charset=utf-8',
        )
        handler.send_header.assert_any_call('Cache-Control', 'no-store')

        handler.wfile = BytesIO()
        handler.path = '/api/config'
        handler.do_GET()
        config = json.loads(handler.wfile.getvalue())

        self.assertNotIn('defaultBuildingsPath', config)
        self.assertEqual(config['configuredProjectName'], 'Test project')
        self.assertNotIn('workflowModes', config)
        self.assertEqual(config['layers'][0]['id'], 'buildings')
        self.assertEqual(config['layers'][0]['assignments'], {'annotation': []})

        handler.wfile = BytesIO()
        handler.path = '/api/buildings'
        handler.do_GET()
        self.assertEqual(store.read_buildings.call_count, 1)

    def test_handler_accepts_point_move_requests(self):
        '''*!*! Browser point moves are delegated to the database feature store.'''

        store = QaqcStore('project-one', 'alice')
        store.read_buildings = Mock(
            return_value={'type': 'FeatureCollection', 'features': []},
        )
        store.update_geometry = Mock(return_value={
            'type': 'Feature',
            'id': 'point-one',
            'layer_id': 'points',
            'geometry': {'type': 'Point', 'coordinates': [-121.0, 39.0]},
            'properties': {},
            'version': 2,
            'h3': {},
        })
        config = self.review_config(modes=('annotation', 'editing'))
        handler_class = self.handler_class(store, config)
        handler = handler_class.__new__(handler_class)
        payload = json.dumps({
            'id': 'point-one',
            'layer_id': 'points',
            'expected_version': 1,
            'operation': 'move',
            'geometry': {'type': 'Point', 'coordinates': [-121.0, 39.0]},
        }).encode('utf-8')
        handler.path = '/api/features'
        handler.headers = {'Content-Length': str(len(payload))}
        handler.rfile = BytesIO(payload)
        handler.wfile = BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_PATCH()

        store.update_geometry.assert_called_once()
        response = json.loads(handler.wfile.getvalue())
        self.assertEqual(response['version'], 2)

    def test_handler_scopes_runtime_config_to_authenticated_reviewer(self):
        '''*!*! One shared URL exposes only the requester's assigned tools.'''

        layer = SimpleNamespace(
            id='buildings',
            name='Buildings',
            geometry_types=('Polygon',),
            modes=('annotation', 'editing'),
            feature_id_field='id',
            predicted_class_field='',
            confidence_field='',
            display_fields=(),
            editing=SimpleNamespace(
                move=True,
                reshape=False,
                create=False,
                delete=False,
            ),
        )
        alice = ReviewerConfig(
            id='alice',
            assignments=(ReviewerAssignmentConfig(
                layer_id='buildings',
                modes=('annotation',),
                h3_indexes=('8828308281fffff',),
            ),),
        )
        bob = ReviewerConfig(
            id='bob',
            assignments=(ReviewerAssignmentConfig(
                layer_id='buildings',
                modes=('editing',),
                h3_indexes=(),
            ),),
        )
        config = SimpleNamespace(
            project_id='project-one',
            annotation_labels=('damaged',),
            imagery_cog='',
            project_name='Test project',
            modes=('annotation', 'editing'),
            reviewers=(alice, bob),
            layers=(layer,),
        )
        config.reviewer = lambda reviewer_id: {
            'alice': alice,
            'bob': bob,
        }.get(reviewer_id)
        store = QaqcStore('project-one', 'bob')
        store_factory = Mock(return_value=store)
        handler_class = make_handler(
            config,
            store_factory=store_factory,
            identity_resolver=lambda headers, **kwargs: 'bob',
        )
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/config'
        handler.headers = {}
        handler.wfile = BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        runtime = json.loads(handler.wfile.getvalue())
        self.assertEqual(runtime['layers'][0]['modes'], ['editing'])
        self.assertEqual(runtime['layers'][0]['assignments'], {'editing': []})
        store_factory.assert_called_once_with('project-one', 'bob')

    def test_handler_accepts_polygon_create_and_delete_requests(self):
        '''*!*! Browser drawing and deletion delegate to the feature store.'''

        store = QaqcStore('project-one', 'alice')
        store.read_buildings = Mock(
            return_value={'type': 'FeatureCollection', 'features': []},
        )
        polygon = {
            'type': 'Polygon',
            'coordinates': [[[0, 0], [0, 1], [1, 1], [0, 0]]],
        }
        store.create_polygon = Mock(return_value={
            'id': 'new-polygon',
            'layer_id': 'buildings',
            'geometry': polygon,
            'version': 1,
        })
        store.delete_polygon = Mock(return_value={
            'id': 'old-polygon',
            'layer_id': 'buildings',
            'version': 2,
        })
        config = self.review_config(modes=('editing',))
        handler_class = self.handler_class(store, config)

        create_payload = json.dumps({
            'layer_id': 'buildings',
            'geometry': polygon,
            'properties': {},
        }).encode('utf-8')
        create_handler = handler_class.__new__(handler_class)
        create_handler.path = '/api/features'
        create_handler.headers = {'Content-Length': str(len(create_payload))}
        create_handler.rfile = BytesIO(create_payload)
        create_handler.wfile = BytesIO()
        create_handler.send_response = Mock()
        create_handler.send_header = Mock()
        create_handler.end_headers = Mock()

        create_handler.do_POST()

        store.create_polygon.assert_called_once()
        create_handler.send_response.assert_called_with(201)

        delete_payload = json.dumps({
            'id': 'old-polygon',
            'layer_id': 'buildings',
            'expected_version': 1,
        }).encode('utf-8')
        delete_handler = handler_class.__new__(handler_class)
        delete_handler.path = '/api/features'
        delete_handler.headers = {'Content-Length': str(len(delete_payload))}
        delete_handler.rfile = BytesIO(delete_payload)
        delete_handler.wfile = BytesIO()
        delete_handler.send_response = Mock()
        delete_handler.send_header = Mock()
        delete_handler.end_headers = Mock()

        delete_handler.do_DELETE()

        store.delete_polygon.assert_called_once()
        response = json.loads(delete_handler.wfile.getvalue())
        self.assertEqual(response['version'], 2)


if __name__ == '__main__':
    unittest.main()
