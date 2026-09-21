from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

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
            self.assertEqual(config.imagery_cog, str(directory / 'imagery/image.tif'))
            self.assertEqual(
                config.annotations_output,
                directory / 'output/annotations_alice.csv',
            )
            self.assertEqual(config.feature_id_field, 'building_id')
            self.assertEqual(config.h3_prefix, 'cell_')
            self.assertEqual(config.todo_h3_indexes, ('8828308281fffff',))

    def test_requires_output_for_each_enabled_mode(self):
        '''*!*! Every selected workflow mode requires its output path.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            values = self.base_config()
            values['workflow']['modes'] = ['annotation', 'qaqc']
            path = self.write_config(Path(temp_dir), values)

            with self.assertRaisesRegex(ConfigError, 'paths.qaqc_output'):
                load_review_config(path)

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
