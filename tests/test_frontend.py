from io import BytesIO
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import FRONTEND_DIR, QaqcStore, browser_url, make_handler


class FrontendTests(unittest.TestCase):
    '''*!*! Verify static frontend files and their Python server boundary.'''

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
        self.assertIn('leaflet-geoman', html)
        self.assertNotIn('workflow-mode-buttons', html)

    def test_handler_serves_frontend_assets_and_runtime_config(self):
        '''*!*! Handler routes expose static assets plus JSON runtime configuration.'''

        store = QaqcStore('project-one', 'alice')
        store.read_buildings = Mock(
            return_value={'type': 'FeatureCollection', 'features': []},
        )
        config = SimpleNamespace(
            annotation_labels=('damaged', 'undamaged'),
            imagery_cog='',
            project_name='Test project',
            user='alice',
            modes=('annotation',),
            todo_h3_indexes=(),
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
        )
        handler_class = make_handler(store, config)
        handler = handler_class.__new__(handler_class)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = BytesIO()

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
        self.assertEqual(config['workflowModes'], ['annotation'])
        self.assertEqual(config['layers'][0]['id'], 'buildings')

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
        config = SimpleNamespace(
            annotation_labels=('damaged',),
            imagery_cog='',
            project_name='Test project',
            user='alice',
            modes=('annotation', 'editing'),
            todo_h3_indexes=(),
            layers=(),
        )
        handler_class = make_handler(store, config)
        handler = handler_class.__new__(handler_class)
        payload = json.dumps({
            'id': 'point-one',
            'layer_id': 'points',
            'expected_version': 1,
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


if __name__ == '__main__':
    unittest.main()
