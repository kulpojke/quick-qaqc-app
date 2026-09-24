from datetime import date
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from app import QaqcStore, filter_buildings_for_assignments
from src.project_config import ConfigError, load_review_config


class ReviewConfigTests(unittest.TestCase):
    '''*!*! Tests for project YAML parsing and assignment filtering.'''

    def write_config(self, directory: Path, values: dict) -> Path:
        '''*!*! Write one temporary YAML configuration for a test.'''

        path = directory / 'project.yaml'
        path.write_text(yaml.safe_dump(values), encoding='utf-8')
        return path

    def base_config(self) -> dict:
        '''*!*! Return a minimal valid annotation project configuration.'''

        return {
            'version': 1,
            'project': {'name': 'test-review'},
            'paths': {
                'features': 'features.geojson',
                'imagery_cog': 'imagery/image.tif',
            },
            'fields': {'feature_id': 'building_id', 'h3_prefix': 'cell_'},
            'annotation': {'labels': ['damaged', 'undamaged']},
            'workflow': {
                'user': 'alice',
                'modes': ['annotation'],
                'todo': [{'h3_index': '8828308281fffff'}],
            },
        }

    def test_resolves_paths_and_user_template_from_yaml_directory(self):
        '''*!*! Paths resolve from the YAML directory and expand user.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = load_review_config(self.write_config(directory, self.base_config()))

            self.assertEqual(config.features_path, directory / 'features.geojson')
            self.assertEqual(config.project_id, 'test-review')
            self.assertEqual(config.imagery_cog, str(directory / 'imagery/image.tif'))
            self.assertEqual(config.feature_id_field, 'building_id')
            self.assertEqual(config.h3_prefix, 'cell_')
            self.assertEqual(config.todo_h3_indexes, ('8828308281fffff',))

    def test_project_id_can_differ_from_display_name(self):
        '''*!*! A stable project ID is independent of its display name.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['project'] = {'id': 'stable-id', 'name': 'Display Name'}

            config = load_review_config(
                self.write_config(Path(temp_dir), values)
            )

            self.assertEqual(config.project_id, 'stable-id')
            self.assertEqual(config.project_name, 'Display Name')

    def test_preserves_remote_feature_url(self):
        '''*!*! HTTP feature sources remain URLs rather than local paths.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['paths']['features'] = 'https://example.com/buildings_h3.geojson'

            config = load_review_config(
                self.write_config(Path(temp_dir), values)
            )

            self.assertEqual(
                config.features_path,
                'https://example.com/buildings_h3.geojson',
            )
            self.assertIsNone(config.overture)

    def test_parses_overture_fire_date_and_refresh(self):
        '''*!*! Overture settings accept an ISO timestamp and strict boolean.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['paths']['features'] = 'None'
            values['project']['fire_date'] = '2025-06-28T11:12:56Z'
            values['overture'] = {'refresh': True}

            config = load_review_config(
                self.write_config(Path(temp_dir), values)
            )

            self.assertEqual(config.overture.fire_date, date(2025, 6, 28))
            self.assertEqual(config.overture.release_selection, 'before_fire')
            self.assertTrue(config.overture.refresh)
            self.assertEqual(
                config.features_path,
                Path(temp_dir) / 'data/project_overture_buildings.geojson',
            )

    def test_overture_requires_fire_date_and_cog(self):
        '''*!*! Overture startup retrieval requires its date and COG source.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            values = self.base_config()
            values['paths']['features'] = 'None'
            with self.assertRaisesRegex(ConfigError, 'project.fire_date'):
                load_review_config(self.write_config(directory, values))

            values['project']['fire_date'] = '2025-06-28'
            values['paths']['imagery_cog'] = ''
            with self.assertRaisesRegex(ConfigError, 'paths.imagery_cog'):
                load_review_config(self.write_config(directory, values))

    def test_oldest_feature_sentinel_selects_oldest_fused_release(self):
        '''*!*! The oldest sentinel enables the post-fire fallback strategy.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['paths']['features'] = 'oldest'
            values['project']['fire_date'] = '2018-11-08'

            config = load_review_config(
                self.write_config(Path(temp_dir), values)
            )

            self.assertEqual(config.overture.release_selection, 'oldest')
            self.assertEqual(config.overture.fire_date, date(2018, 11, 8))

    def test_rejects_unknown_mode(self):
        '''*!*! Unknown workflow modes fail configuration validation.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['modes'] = ['triage']
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'Unsupported workflow mode'):
                load_review_config(path)

    def test_rejects_invalid_h3_assignment(self):
        '''*!*! Invalid H3 indexes fail configuration validation.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['todo'] = [{'h3_index': 'not-an-h3-cell'}]
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'not a valid H3 index'):
                load_review_config(path)

    def test_filters_features_to_assigned_cells(self):
        '''*!*! Feature filtering retains only configured H3 assignments.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_review_config(
                self.write_config(Path(temp_dir), self.base_config())
            )
            buildings = {
                'type': 'FeatureCollection',
                'features': [
                    {'properties': {'cell_8': '8828308281fffff'}},
                    {'properties': {'cell_8': '8828308283fffff'}},
                ],
            }

            filtered = filter_buildings_for_assignments(buildings, config)

            self.assertEqual(len(filtered['features']), 1)
            self.assertEqual(len(buildings['features']), 2)


class QaqcStoreTests(unittest.TestCase):
    '''*!*! Tests for project feature loading and database delegation.'''

    def write_geoparquet(self, path: Path) -> None:
        '''*!*! Write two small GeoParquet features for source-loading tests.'''

        import duckdb

        connection = duckdb.connect()
        try:
            connection.execute('LOAD spatial')
            connection.execute(
                '''
CREATE TABLE test_features AS
SELECT *
FROM (
    VALUES
        ('one', '8828308281fffff', ST_GeomFromText('POINT (0 0)')),
        ('two', '8828308283fffff', ST_GeomFromText('POINT (1 1)'))
) AS features(id, h3_r8, geometry)
'''
            )
            connection.execute(
                'COPY test_features TO ? (FORMAT PARQUET)',
                [str(path)],
            )
        finally:
            connection.close()

    def test_reads_remote_feature_geojson(self):
        '''*!*! A public bucket URL is loaded as the feature collection.'''

        payload = b'{"type":"FeatureCollection","features":[]}'
        with tempfile.TemporaryDirectory() as temp_dir:
            source = 'https://example.com/buildings_h3.geojson'
            store = QaqcStore(source)
            with patch('app.urlopen', return_value=BytesIO(payload)) as open_url:
                buildings = store.read_buildings()

        self.assertEqual(buildings['type'], 'FeatureCollection')
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, source)
        self.assertEqual(request.get_header('User-agent'), 'damagemap-qaqc/1.0')

    def test_reads_configured_project_features_from_postgis(self):
        '''*!*! YAML mode uses PostGIS when a database URL is configured.'''

        expected = {'type': 'FeatureCollection', 'features': []}
        with tempfile.TemporaryDirectory() as temp_dir:
            store = QaqcStore(
                Path(temp_dir) / 'features.parquet',
                database_project_id='project-one',
                database_reviewer_id='alice',
                feature_id_field='building_id',
            )
            with (
                patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test'}),
                patch('app.read_project_features', return_value=expected) as read,
            ):
                buildings = store.read_buildings()

        self.assertIs(buildings, expected)
        read.assert_called_once_with(
            'project-one',
            'alice',
            feature_id_field='building_id',
        )

    def test_reads_reviews_from_postgis(self):
        '''*!*! Review state delegates to the configured PostGIS project.'''

        expected = {'one': {'annotation_label': 'damaged'}}
        store = QaqcStore(
            Path('features.parquet'),
            database_project_id='project-one',
            database_reviewer_id='alice',
        )
        with patch('app.read_reviewer_annotations', return_value=expected) as read:
            annotations = store.read_annotations('annotation')

        self.assertIs(annotations, expected)
        read.assert_called_once_with('project-one', 'alice', 'annotation')

    def test_writes_reviews_to_postgis(self):
        '''*!*! Review submissions delegate to database persistence.'''

        payload = {
            'id': 'one',
            'annotation_label': 'damaged',
            'feature_version_seen': 1,
        }
        expected = {'id': 'one', 'annotation_label': 'damaged'}
        store = QaqcStore(
            Path('features.parquet'),
            database_project_id='project-one',
            database_reviewer_id='alice',
        )
        with patch('app.write_reviewer_annotation', return_value=expected) as write:
            saved = store.write_annotation(payload, 'qaqc')

        self.assertIs(saved, expected)
        write.assert_called_once_with(
            'project-one',
            'alice',
            'qaqc',
            payload,
        )

    def test_reads_and_filters_local_geoparquet(self):
        '''*!*! GeoParquet queries return GeoJSON for assigned H3 cells only.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / 'buildings.parquet'
            self.write_geoparquet(source)
            store = QaqcStore(source)

            buildings = store.read_buildings(
                h3_assignments={'h3_r8': {'8828308281fffff'}},
            )

        self.assertEqual(buildings['type'], 'FeatureCollection')
        self.assertEqual(len(buildings['features']), 1)
        self.assertEqual(buildings['features'][0]['properties']['id'], 'one')
        self.assertEqual(buildings['features'][0]['geometry']['type'], 'Point')
        self.assertNotIn('geometry', buildings['features'][0]['properties'])

    def test_routes_remote_parquet_urls_to_duckdb(self):
        '''*!*! Bucket Parquet URLs use the range-query feature reader.'''

        source = 'https://example.com/buildings.parquet?signature=test'
        assignments = {'h3_r8': {'8828308281fffff'}}
        expected = {'type': 'FeatureCollection', 'features': []}
        store = QaqcStore(source)

        with patch.object(
            store,
            '_read_parquet_buildings',
            return_value=expected,
        ) as read_parquet:
            buildings = store.read_buildings(h3_assignments=assignments)

        self.assertIs(buildings, expected)
        read_parquet.assert_called_once_with(source, assignments)

if __name__ == '__main__':
    unittest.main()
