from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from app import QaqcStore
from src.project_config import ConfigError, load_review_config


class ReviewConfigTests(unittest.TestCase):
    '''*!*! Tests for YAML-only project configuration.'''

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
                'features': 'features.parquet',
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

    def test_resolves_paths_from_yaml_directory(self):
        '''*!*! Feature and imagery paths resolve from the YAML directory.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = load_review_config(self.write_config(directory, self.base_config()))

            self.assertEqual(config.features_path, directory / 'features.parquet')
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
            values['paths']['features'] = 'https://example.com/buildings.parquet'

            config = load_review_config(
                self.write_config(Path(temp_dir), values)
            )

            self.assertEqual(
                config.features_path,
                'https://example.com/buildings.parquet',
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
        '''*!*! Overture retrieval configuration requires its date and COG.'''

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


class QaqcStoreTests(unittest.TestCase):
    '''*!*! Tests for database feature and review delegation.'''

    def test_reads_features_from_postgis(self):
        '''*!*! Feature state delegates to the configured PostGIS project.'''

        expected = {'type': 'FeatureCollection', 'features': []}
        store = QaqcStore('project-one', 'alice', 'building_id')
        with patch('app.read_project_features', return_value=expected) as read:
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
        store = QaqcStore('project-one', 'alice')
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
        store = QaqcStore('project-one', 'alice')
        with patch('app.write_reviewer_annotation', return_value=expected) as write:
            saved = store.write_annotation(payload, 'qaqc')

        self.assertIs(saved, expected)
        write.assert_called_once_with(
            'project-one',
            'alice',
            'qaqc',
            payload,
        )


if __name__ == '__main__':
    unittest.main()
