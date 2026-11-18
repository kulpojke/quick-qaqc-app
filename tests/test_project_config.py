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
            'version': 3,
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
                        'hover': ['address', 'prediction'],
                        'origin': 'origin',
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
                'modes': ['annotation', 'editing'],
                'reviewers': [
                    {
                        'id': 'alice@example.com',
                        'assignments': [
                            {
                                'layer': 'buildings',
                                'modes': ['annotation'],
                                'h3_indexes': ['8828308281fffff'],
                            },
                            {
                                'layer': 'points',
                                'modes': ['annotation', 'editing'],
                                'h3_indexes': [],
                            },
                        ],
                    },
                ],
            },
        }

    def test_r2_destination_is_read_from_yaml(self):
        '''*!*! Each project controls its bucket and prefix independently of secrets.'''

        values = self.base_config()
        values['exports'] = {
            'bucket': 'source-bucket',
            'endpoint_url': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com/',
            'prefix': 'exports/camp/',
        }
        with tempfile.TemporaryDirectory() as directory:
            config = load_review_config(self.write_config(Path(directory), values))
        self.assertEqual(config.exports.bucket, 'source-bucket')
        self.assertEqual(config.exports.prefix, 'exports/camp')
        self.assertFalse(config.exports.endpoint_url.endswith('/'))

    def test_invalid_export_settings_are_rejected(self):
        '''*!*! Refuse bucket URLs, credential fields, unsafe prefixes, and public endpoints.'''

        valid = {
            'bucket': 'source-bucket',
            'endpoint_url': 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
        }
        for invalid in (
            {}, None, {'bucket': 'https://public.r2.dev'}, {'prefix': '../inputs'},
            {'prefix': ''}, {'endpoint_url': 'https://public.r2.dev'},
            {'secret_access_key': 'must-not-be-in-yaml'},
        ):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                values = self.base_config()
                values['exports'] = invalid if invalid in ({}, None) else {**valid, **invalid}
                with self.assertRaises(ConfigError):
                    load_review_config(self.write_config(Path(directory), values))

    def test_omitted_exports_preserves_local_snapshots(self):
        '''*!*! Existing projects retain their local export behavior.'''

        with tempfile.TemporaryDirectory() as directory:
            config = load_review_config(self.write_config(Path(directory), self.base_config()))
        self.assertIsNone(config.exports)

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
            self.assertEqual(config.layers[0].hover_fields, ('address', 'prediction'))
            # *!*! Layers without hover fall back to showing only the feature ID.
            self.assertEqual(config.layers[1].hover_fields, ())
            self.assertEqual(config.layers[0].origin_field, 'origin')
            self.assertEqual(config.layers[1].origin_field, '')
            self.assertEqual(config.layers[0].h3_prefix, 'building_h3_r')
            self.assertEqual(config.layers[1].geometry_types, ('Point',))
            self.assertEqual(config.layers[1].source_crs, 'EPSG:6414')
            self.assertTrue(config.layers[1].editing.move)
            reviewer = config.reviewer('alice@example.com')
            self.assertIsNotNone(reviewer)
            self.assertEqual(reviewer.modes, ('annotation', 'editing'))
            self.assertEqual(
                reviewer.assignment_for('buildings', 'annotation').h3_indexes,
                ('8828308281fffff',),
            )
            self.assertIsNone(reviewer.assignment_for('buildings', 'editing'))

    def test_imagery_list_keeps_first_entry_as_primary(self):
        '''*!*! Multiple COGs load in order; the first drives bootstrap bounds.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            values = self.base_config()
            del values['paths']
            values['imagery'] = [
                {'id': 'post', 'name': 'Post-fire', 'source': 'imagery/post.tif'},
                {'id': 'pre', 'source': 'https://example.com/pre.tif'},
            ]
            config = load_review_config(self.write_config(directory, values))

            self.assertEqual([image.id for image in config.imagery], ['post', 'pre'])
            self.assertEqual(config.imagery[0].name, 'Post-fire')
            # *!*! A missing name falls back to the id.
            self.assertEqual(config.imagery[1].name, 'pre')
            self.assertEqual(config.imagery_cog, str(directory / 'imagery/post.tif'))
            self.assertEqual(config.imagery[1].source, 'https://example.com/pre.tif')

    def test_single_imagery_cog_becomes_one_imagery_entry(self):
        '''*!*! Older configs with paths.imagery_cog keep working unchanged.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = load_review_config(self.write_config(directory, self.base_config()))

            self.assertEqual(len(config.imagery), 1)
            self.assertEqual(config.imagery[0].source, config.imagery_cog)

    def test_rejects_ambiguous_or_duplicate_imagery(self):
        '''*!*! One imagery form per config, with unique ids.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            values = self.base_config()
            values['imagery'] = [{'id': 'post', 'source': 'post.tif'}]
            with self.assertRaisesRegex(ConfigError, 'not both'):
                load_review_config(self.write_config(directory, values))

            del values['paths']
            values['imagery'].append({'id': 'post', 'source': 'other.tif'})
            with self.assertRaisesRegex(ConfigError, 'Duplicate imagery id'):
                load_review_config(self.write_config(directory, values))

    def test_rejects_version_two_configuration(self):
        '''*!*! The former single-reviewer schema fails with a clear version error.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['version'] = 2
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'expected 3'):
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
            values['workflow']['reviewers'][0]['assignments'][0]['h3_indexes'] = [
                'not-an-h3-cell'
            ]
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'not a valid H3 index'):
                load_review_config(path)

    def test_rejects_duplicate_reviewer_ids(self):
        '''*!*! Authenticated identities uniquely identify configured reviewers.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['reviewers'].append(
                values['workflow']['reviewers'][0].copy()
            )
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'Duplicate reviewer id'):
                load_review_config(path)

    def test_rejects_assignment_mode_not_enabled_for_layer(self):
        '''*!*! Reviewer tools remain a subset of each layer's enabled modes.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['reviewers'][0]['assignments'][0]['modes'] = [
                'editing'
            ]
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'not enabled for layer'):
                load_review_config(path)

    def test_max_features_is_an_optional_positive_cap(self):
        '''*!*! A reviewer can be limited to a few buildings for testing.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            config = load_review_config(self.write_config(Path(temp_dir), values))
            self.assertIsNone(
                config.reviewers[0].assignment_for('buildings', 'annotation').max_features
            )
            values['workflow']['reviewers'][0]['assignments'][0]['max_features'] = 50
            config = load_review_config(self.write_config(Path(temp_dir), values))
            self.assertEqual(
                config.reviewers[0].assignment_for('buildings', 'annotation').max_features, 50
            )
            for bad in (0, -1, True, '50', 2.5):
                with self.subTest(bad=bad):
                    values['workflow']['reviewers'][0]['assignments'][0]['max_features'] = bad
                    path = self.write_config(Path(temp_dir), values)
                    with self.assertRaisesRegex(ConfigError, 'max_features'):
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

    def test_delegates_polygon_creation_and_deletion(self):
        '''*!*! Geometry lifecycle requests retain project and reviewer identity.'''

        store = QaqcStore('project-one', 'alice')
        polygon = {
            'type': 'Polygon',
            'coordinates': [[[0, 0], [0, 1], [1, 1], [0, 0]]],
        }
        create_payload = {
            'layer_id': 'buildings',
            'geometry': polygon,
            'properties': {'source': 'drawn'},
        }
        delete_payload = {
            'layer_id': 'buildings',
            'id': 'one',
            'expected_version': 2,
        }
        with patch('app.create_project_polygon', return_value={'id': 'new'}) as create:
            self.assertEqual(store.create_polygon(create_payload), {'id': 'new'})
        create.assert_called_once_with(
            'project-one',
            'alice',
            'buildings',
            polygon,
            {'source': 'drawn'},
        )
        with patch('app.delete_project_polygon', return_value={'id': 'one'}) as delete:
            self.assertEqual(store.delete_polygon(delete_payload), {'id': 'one'})
        delete.assert_called_once_with(
            'project-one',
            'alice',
            'buildings',
            'one',
            2,
        )


if __name__ == '__main__':
    unittest.main()
