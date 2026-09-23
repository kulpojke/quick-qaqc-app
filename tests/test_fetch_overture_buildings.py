from datetime import date
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.fetch_overture_buildings import (
    bbox_field_names,
    building_geodataframe,
    cache_metadata_path,
    cache_signature,
    cog_bounds_wgs84,
    discover_fused_releases,
    ensure_overture_buildings,
    fused_building_paths,
    select_fused_release,
    select_release_before,
)


class FetchOvertureBuildingsTests(unittest.TestCase):
    '''*!*! Tests for release selection and startup feature caching.'''

    def test_discovers_dated_releases_from_bucket_prefixes(self):
        '''*!*! Release discovery ignores non-release bucket directories.'''

        listing = b'''<?xml version='1.0' encoding='UTF-8'?>
<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>
  <CommonPrefixes><Prefix>overture/2025-06-25-0/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>overture/docs/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>overture/2025-05-21-0/</Prefix></CommonPrefixes>
</ListBucketResult>'''

        with patch(
            'src.fetch_overture_buildings.urlopen',
            return_value=BytesIO(listing),
        ) as open_url:
            releases = discover_fused_releases()

        self.assertEqual(releases, ('2025-05-21-0', '2025-06-25-0'))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'damagemap-qaqc/1.0')
        self.assertEqual(request.get_header('Accept'), 'application/xml')

    def test_reads_cog_bounds_in_wgs84(self):
        '''*!*! COG bounds are exposed in the query coordinate system.'''

        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        with tempfile.TemporaryDirectory() as temp_dir:
            cog_path = Path(temp_dir) / 'imagery.tif'
            with rasterio.open(
                cog_path,
                'w',
                driver='GTiff',
                width=10,
                height=10,
                count=1,
                dtype='uint8',
                crs='EPSG:4326',
                transform=from_bounds(1.0, 2.0, 3.0, 4.0, 10, 10),
            ) as dataset:
                dataset.write(np.zeros((1, 10, 10), dtype='uint8'))

            bounds = cog_bounds_wgs84(str(cog_path))

        self.assertEqual(bounds, (1.0, 2.0, 3.0, 4.0))

    def test_builds_and_clips_app_ready_geometries(self):
        '''*!*! WKB rows are clipped and receive configured H3 columns.'''

        import pandas as pd
        from shapely.geometry import box

        rows = pd.DataFrame(
            {
                'id': ['inside', 'outside'],
                'geometry': [
                    box(-0.5, -0.5, 0.5, 0.5).wkb,
                    box(10, 10, 11, 11).wkb,
                ],
            }
        )

        buildings = building_geodataframe(
            rows,
            (-1.0, -1.0, 1.0, 1.0),
            h3_prefix='cell_',
            h3_resolutions=(8,),
        )

        self.assertEqual(buildings['id'].tolist(), ['inside'])
        self.assertTrue(buildings.loc[0, 'cell_8'])

    def test_selects_latest_release_strictly_before_fire_date(self):
        '''*!*! A same-day release is excluded from pre-fire buildings.'''

        releases = ('2025-05-21-0', '2025-06-25-0', '2025-07-23-0')

        self.assertEqual(
            select_release_before(date(2025, 6, 25), releases),
            '2025-05-21-0',
        )

    def test_reports_when_no_release_predates_fire(self):
        '''*!*! Fires older than the mirror fail with its earliest date.'''

        with self.assertRaisesRegex(ValueError, 'earliest available release is 2024-02-15'):
            select_release_before(date(2018, 11, 8), ('2024-02-15-alpha-0',))

    def test_oldest_strategy_selects_earliest_mirror_release(self):
        '''*!*! The explicit fallback can select the oldest Fused snapshot.'''

        releases = ('2025-05-21-0', '2024-02-15-alpha-0', '2025-06-25-0')

        self.assertEqual(
            select_fused_release(date(2018, 11, 8), releases, 'oldest'),
            '2024-02-15-alpha-0',
        )

    def test_uses_partition_count_for_release_generation(self):
        '''*!*! Paths account for the Fused building repartitioning change.'''

        self.assertEqual(len(fused_building_paths('2024-12-18-0')), 5)
        self.assertEqual(len(fused_building_paths('2025-01-22-0')), 6)

    def test_detects_historical_and_current_bbox_field_names(self):
        '''*!*! Bbox detection supports both mirrored Overture schemas.'''

        schemas = {
            'STRUCT(minx DOUBLE, maxx DOUBLE, miny DOUBLE, maxy DOUBLE)': (
                'minx',
                'maxx',
                'miny',
                'maxy',
            ),
            'STRUCT(xmin FLOAT, xmax FLOAT, ymin FLOAT, ymax FLOAT)': (
                'xmin',
                'xmax',
                'ymin',
                'ymax',
            ),
        }
        for schema, expected in schemas.items():
            with self.subTest(schema=schema):
                connection = Mock()
                connection.description = [('bbox', schema)]

                fields = bbox_field_names(connection, ['buildings.parquet'])

                self.assertEqual(fields, expected)

    def test_matching_cache_skips_release_listing_and_query(self):
        '''*!*! A valid sidecar permits offline reuse of materialized features.'''

        bounds = (-1.0, 2.0, 3.0, 4.0)
        fire_date = date(2025, 6, 28)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / 'buildings.geojson'
            output_path.write_text('{}', encoding='utf-8')
            signature = cache_signature(
                cog_source='imagery.tif',
                fire_date=fire_date,
                release_selection='before_fire',
                release='2025-06-25-0',
                bounds_wgs84=bounds,
                h3_prefix='h3_r',
                h3_resolutions=(5, 6, 7, 8, 9, 10),
            )
            cache_metadata_path(output_path).write_text(
                json.dumps({'signature': signature, 'feature_count': 42}),
                encoding='utf-8',
            )

            with (
                patch(
                    'src.fetch_overture_buildings.cog_bounds_wgs84',
                    return_value=bounds,
                ),
                patch('src.fetch_overture_buildings.discover_fused_releases') as discover,
                patch('src.fetch_overture_buildings.query_fused_buildings') as query,
            ):
                result = ensure_overture_buildings(
                    cog_source='imagery.tif',
                    fire_date=fire_date,
                    output_path=output_path,
                )

            self.assertTrue(result.from_cache)
            self.assertEqual(result.release, '2025-06-25-0')
            self.assertEqual(result.feature_count, 42)
            discover.assert_not_called()
            query.assert_not_called()


if __name__ == '__main__':
    unittest.main()
