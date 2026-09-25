from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from app import QaqcStore
from src.project_config import ConfigError, load_review_config


class ReviewConfigTests(unittest.TestCase):
    '''*!*! Tests for layer-aware YAML project configuration.'''

    def write_config(self, directory: Path, values: dict) -> Path:
        '''*!*! Write one temporary YAML configuration for a test.'''

        path = directory / 'project.yaml'
        path.write_text(yaml.safe_dump(values), encoding='utf-8')
        return path

    def base_config(self) -> dict:
        '''*!*! Return a minimal project with point and polygon layers.'''

        return {
            'version': 2,
            'project': {'id': 'test-review', 'name': 'Test review'},
            'paths': {'imagery_cog': 'imagery/image.tif'},
            'fields': {'predicted_class': 'prediction'},
            'layers': [
                {
                    'id': 'buildings',
                    'name': 'Buildings',
                    'source': 'features/buildings.parquet',
                    'geometry_types': ['Polygon', 'MultiPolygon'],
                    'fields': {
                        'feature_id': 'building_id',
                        'predicted_class': 'prediction',
                        'confidence': 'score',
                        'display': ['address'],
                    },
                    'h3_prefix': 'building_h3_r',
                    'modes': ['annotation'],
                },
                {
                    'id': 'points',
                    'name': 'Points',
                    'source': 'https://example.com/california-points.parquet',
                    'crs': 'EPSG:6414',
                    'geometry_types': ['Point'],
                    'fields': {'feature_id': 'point_id'},
                    'modes': ['annotation', 'editing'],
                    'editing': {'move': True},
                },
            ],
            'annotation': {'labels': ['damaged', 'undamaged']},
            'workflow': {
                'user': 'alice',
                'modes': ['annotation', 'editing'],
                'todo': [{'h3_index': '8828308281fffff'}],
            },
        }

    def test_resolves_layer_and_imagery_paths(self):
        '''*!*! Local sources resolve while bucket URLs remain unchanged.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = load_review_config(self.write_config(directory, self.base_config()))

            self.assertEqual(config.project_id, 'test-review')
            self.assertEqual(config.imagery_cog, str(directory / 'imagery/image.tif'))
            self.assertEqual(
                config.layers[0].source,
                directory / 'features/buildings.parquet',
            )
            self.assertEqual(
                config.layers[1].source,
                'https://example.com/california-points.parquet',
            )
            self.assertEqual(config.layers[0].feature_id_field, 'building_id')
            self.assertEqual(config.layers[0].predicted_class_field, 'prediction')
            self.assertEqual(config.layers[0].display_fields, ('address',))
            self.assertEqual(config.layers[0].h3_prefix, 'building_h3_r')
            self.assertEqual(config.layers[1].geometry_types, ('Point',))
            self.assertEqual(config.layers[1].source_crs, 'EPSG:6414')
            self.assertTrue(config.layers[1].editing.move)
            self.assertEqual(config.todo_h3_indexes, ('8828308281fffff',))

    def test_rejects_version_one_configuration(self):
        '''*!*! The old single-feature-source schema fails with a clear version error.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['version'] = 1
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'expected 2'):
                load_review_config(path)

    def test_rejects_duplicate_layer_ids(self):
        '''*!*! Layer IDs must uniquely identify import and export streams.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['layers'][1]['id'] = 'buildings'
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'Duplicate layer id'):
                load_review_config(path)

    def test_rejects_unsupported_geometry_type(self):
        '''*!*! Layers accept only supported point and polygon geometry types.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['layers'][0]['geometry_types'] = ['LineString']
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'unsupported geometry type'):
                load_review_config(path)

    def test_editing_mode_requires_a_capability(self):
        '''*!*! Editable layers explicitly state which operations are allowed.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['layers'][1].pop('editing')
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'no editing capabilities'):
                load_review_config(path)

    def test_rejects_unknown_mode(self):
        '''*!*! Unknown workflow modes fail configuration validation.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['modes'] = ['triage']
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'Unsupported workflow mode'):
                load_review_config(path)

    def test_rejects_removed_qaqc_mode(self):
        '''*!*! QA/QC is no longer configured as a separate task mode.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['modes'] = ['annotation', 'qaqc']
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
        store = QaqcStore('project-one', 'alice')
        with patch('app.read_project_features', return_value=expected) as read:
            features = store.read_buildings()

        self.assertIs(features, expected)
        read.assert_called_once_with('project-one', 'alice')

    def test_reads_reviews_from_postgis(self):
        '''*!*! Review state delegates to the configured PostGIS project.'''

        expected = {'["points","one"]': {'annotation_label': 'damaged'}}
        store = QaqcStore('project-one', 'alice')
        with patch('app.read_reviewer_annotations', return_value=expected) as read:
            annotations = store.read_annotations('annotation')

        self.assertIs(annotations, expected)
        read.assert_called_once_with('project-one', 'alice', 'annotation')

    def test_writes_reviews_to_postgis(self):
        '''*!*! Review submissions retain their layer identity.'''

        payload = {
            'id': 'one',
            'layer_id': 'points',
            'annotation_label': 'damaged',
            'feature_version_seen': 1,
        }
        expected = {
            'id': 'one',
            'layer_id': 'points',
            'annotation_label': 'damaged',
        }
        store = QaqcStore('project-one', 'alice')
        with patch('app.write_reviewer_annotation', return_value=expected) as write:
            saved = store.write_annotation(payload, 'annotation')

        self.assertIs(saved, expected)
        write.assert_called_once_with(
            'project-one',
            'alice',
            'annotation',
            payload,
        )


if __name__ == '__main__':
    unittest.main()
