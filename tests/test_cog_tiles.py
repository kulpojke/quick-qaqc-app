'''*!*! Exercise tile caching without reading remote imagery.'''

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import _render_cog_tile_cached, render_cog_tile


class CogTileTests(unittest.TestCase):
    '''*!*! Verify cache reuse, expiry, local replacement, and failed-render retry.'''

    def setUp(self):
        '''*!*! Clear process cache and replace the raster reader with a fake.'''

        _render_cog_tile_cached.cache_clear()
        self.addCleanup(_render_cog_tile_cached.cache_clear)
        patcher = patch('rio_tiler.io.Reader')
        self.reader = patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = self.reader.return_value.__enter__.return_value
        self.cog.dataset.count = 3
        self.cog.tile.return_value.render.return_value = b'PNG'

    @patch('app.time.monotonic', return_value=1)
    def test_concurrent_identical_tiles_render_once(self, clock):
        '''*!*! Repeated requests share a single rendered PNG.'''

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: render_cog_tile('https://example.com/cog.tif', 2, 1, 1),
                range(16),
            ))
        self.assertEqual(results, [b'PNG'] * 16)
        self.cog.tile.assert_called_once_with(1, 1, 2, indexes=(1, 2, 3))

    @patch('app.TILE_CACHE_TTL_SECONDS', 86400)
    @patch('app.time.monotonic', side_effect=[1, 301, 86401])
    def test_remote_tiles_expire(self, clock):
        '''*!*! Tiles survive five minutes and refresh at the configured daily boundary.'''

        for _ in range(3):
            render_cog_tile('https://example.com/cog.tif', 2, 1, 1)
        self.assertEqual(self.cog.tile.call_count, 2)

    @patch('app.time.monotonic', return_value=1)
    def test_tiles_survive_beyond_previous_cache_capacity(self, clock):
        '''*!*! Navigating more than 256 tiles retains earlier imagery for revisits.'''

        source = 'https://example.com/cog.tif'
        for x in range(300):
            render_cog_tile(source, 10, x, 1)
        render_cog_tile(source, 10, 0, 1)
        self.assertEqual(self.cog.tile.call_count, 300)

    def test_changed_local_file_invalidates_tile(self):
        '''*!*! Local source changes refresh tiles without a server restart.'''

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cog.tif'
            source.write_bytes(b'old')
            render_cog_tile(source, 2, 1, 1)
            render_cog_tile(source, 2, 1, 1)
            source.write_bytes(b'new version')
            render_cog_tile(source, 2, 1, 1)
        self.assertEqual(self.cog.tile.call_count, 2)

    def test_render_failure_is_not_cached(self):
        '''*!*! A transient raster error is retried on the next request.'''

        self.cog.tile.side_effect = [RuntimeError('network error'), self.cog.tile.return_value]
        with self.assertRaisesRegex(RuntimeError, 'network error'):
            render_cog_tile('https://example.com/cog.tif', 2, 1, 1)
        self.assertEqual(render_cog_tile('https://example.com/cog.tif', 2, 1, 1), b'PNG')

    def test_native_zoom_matches_raster_resolution(self):
        '''*!*! A z22 Web Mercator COG reports 22; 60 cm UTM imagery reports 18.'''

        from rasterio.io import MemoryFile
        from rasterio.transform import from_origin

        from app import WEB_MERCATOR_Z0_RESOLUTION, native_zoom

        cases = (
            ('EPSG:3857', -13537000.0, 4832000.0, WEB_MERCATOR_Z0_RESOLUTION / 2 ** 22, 22),
            ('EPSG:32610', 620000.0, 4400000.0, 0.6, 18),
        )
        for crs, left, top, resolution, expected in cases:
            profile = dict(driver='GTiff', width=64, height=64, count=1, dtype='uint8',
                           crs=crs, transform=from_origin(left, top, resolution, resolution))
            with MemoryFile() as memory, memory.open(**profile) as dataset:
                self.assertEqual(native_zoom(dataset), expected, crs)

    def test_invalid_coordinates_do_not_open_raster(self):
        '''*!*! Invalid zoom or tile indexes are rejected before raster work.'''

        for coordinates in ((-1, 0, 0), (24, 0, 0), (2, 4, 0), (2, 0, -1)):
            with self.assertRaises(ValueError):
                render_cog_tile('https://example.com/cog.tif', *coordinates)
        self.reader.assert_not_called()
