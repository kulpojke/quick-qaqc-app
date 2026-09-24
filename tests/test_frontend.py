from io import BytesIO
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

    def test_handler_serves_frontend_assets_and_runtime_config(self):
        '''*!*! Handler routes expose static assets plus JSON runtime configuration.'''

        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            buildings_path = directory / 'buildings.geojson'
            store = QaqcStore(
                buildings_path,
                directory / 'annotations.csv',
            )
            handler_class = make_handler(store)
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

            self.assertEqual(
                config['defaultBuildingsPath'],
                str(buildings_path),
            )
            self.assertFalse(config['yamlConfigured'])
            self.assertEqual(config['workflowModes'], ['annotation'])


if __name__ == '__main__':
    unittest.main()
