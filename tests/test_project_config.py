from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from app import ANNOTATION_FIELDS, QaqcStore, filter_buildings_for_assignments
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
                'annotations_input': 'input/merged.csv',
                'annotations_output': 'output/annotations_{user}.csv',
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
            self.assertEqual(
                config.annotations_output,
                directory / 'output/annotations_alice.csv',
            )
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

    def test_requires_output_for_each_enabled_mode(self):
        '''*!*! Every selected workflow mode requires its output path.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['modes'] = ['annotation', 'qaqc']
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'paths.qaqc_output'):
                load_review_config(path)

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
    '''*!*! Tests for layered, concurrent annotation CSV persistence.'''

    def write_rows(self, path: Path, rows: list[dict[str, str]]) -> None:
        '''*!*! Write canonical annotation rows for a persistence test.'''

        import csv

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=ANNOTATION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

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

    def row(self, feature_id: str, label: str, reviewer: str) -> dict[str, str]:
        '''*!*! Build one canonical annotation row.'''

        row = {field: '' for field in ANNOTATION_FIELDS}
        row.update({'id': feature_id, 'annotation_label': label, 'reviewer': reviewer})
        return row

    def test_output_overlays_input_without_copying_merged_rows_on_write(self):
        '''*!*! User output overlays input without copying merged records.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            input_path = directory / 'merged.csv'
            output_path = directory / 'annotations_alice.csv'
            self.write_rows(input_path, [self.row('one', 'damaged', 'bob')])
            self.write_rows(output_path, [self.row('two', 'undamaged', 'alice')])
            store = QaqcStore(directory / 'features.geojson', output_path, input_path)

            self.assertEqual(set(store.read_annotations()), {'one', 'two'})
            store.write_annotation(self.row('three', 'unknown', 'alice'))

            self.assertEqual(set(store.read_annotations()), {'one', 'two', 'three'})
            self.assertEqual(set(store._read_annotation_file(output_path)), {'two', 'three'})

    def test_reads_remote_feature_geojson(self):
        '''*!*! A public bucket URL is loaded as the feature collection.'''

        payload = b'{"type":"FeatureCollection","features":[]}'
        with tempfile.TemporaryDirectory() as temp_dir:
            source = 'https://example.com/buildings_h3.geojson'
            store = QaqcStore(
                source,
                Path(temp_dir) / 'annotations.csv',
            )
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
                Path(temp_dir) / 'annotations.csv',
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

    def test_reads_and_filters_local_geoparquet(self):
        '''*!*! GeoParquet queries return GeoJSON for assigned H3 cells only.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / 'buildings.parquet'
            self.write_geoparquet(source)
            store = QaqcStore(source, directory / 'annotations.csv')

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
        store = QaqcStore(source, Path('annotations.csv'))

        with patch.object(
            store,
            '_read_parquet_buildings',
            return_value=expected,
        ) as read_parquet:
            buildings = store.read_buildings(h3_assignments=assignments)

        self.assertIs(buildings, expected)
        read_parquet.assert_called_once_with(source, assignments)

    def test_concurrent_writes_do_not_drop_annotations(self):
        '''*!*! Concurrent writes retain every annotation.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            output_path = directory / 'annotations_alice.csv'
            store = QaqcStore(directory / 'features.geojson', output_path)
            rows = [self.row(str(index), 'unknown', 'alice') for index in range(20)]

            with ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(store.write_annotation, rows))

            self.assertEqual(set(store.read_annotations()), {str(i) for i in range(20)})

    def test_merged_wide_input_is_complete_without_exposing_prior_label(self):
        '''*!*! Merged work counts complete without revealing its label.'''

        import csv

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            input_path = directory / 'annotations_merged.csv'
            with input_path.open('w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=['id', 'bob_annotation_label'])
                writer.writeheader()
                writer.writerow({'id': 'one', 'bob_annotation_label': 'damaged'})
            store = QaqcStore(
                directory / 'features.geojson',
                directory / 'annotations_alice.csv',
                input_path,
            )

            annotation = store.read_annotations()['one']
            self.assertEqual(annotation['qa_status'], 'annotated')
            self.assertNotIn('annotation_label', annotation)


if __name__ == '__main__':
    unittest.main()
