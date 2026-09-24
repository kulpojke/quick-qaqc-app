from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import UUID

from src.api.bootstrap import canonical_source, sync_tasks, task_id_for


class BootstrapTests(unittest.TestCase):
    '''*!*! Tests for stable project bootstrap identities.'''

    def test_canonical_source_removes_url_credentials_and_query(self):
        '''*!*! Import identity excludes temporary URL authentication values.'''

        source = 'https://user:secret@example.com/buildings.parquet?token=temporary'

        self.assertEqual(
            canonical_source(source),
            'https://example.com/buildings.parquet',
        )

    def test_canonical_source_resolves_local_paths(self):
        '''*!*! Local import identities are absolute normalized paths.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / '..' / Path(temp_dir).name / 'buildings.parquet'

            self.assertEqual(canonical_source(source), str(source.resolve()))

    def test_task_ids_are_deterministic_and_mode_specific(self):
        '''*!*! Managed task IDs remain stable without colliding across modes.'''

        annotation_id = task_id_for('camp', 'annotation')

        self.assertIsInstance(annotation_id, UUID)
        self.assertEqual(annotation_id, task_id_for('camp', 'annotation'))
        self.assertNotEqual(annotation_id, task_id_for('camp', 'qaqc'))

    def test_sync_tasks_skips_empty_h3_batch(self):
        '''*!*! All-feature assignments do not execute an empty H3 batch.'''

        connection = MagicMock(spec=['execute', 'cursor'])
        config = SimpleNamespace(
            project_id='camp',
            project_name='Camp',
            annotation_labels=('damaged', 'undamaged'),
            modes=('annotation',),
            user='alice',
            todo_h3_indexes=(),
        )

        sync_tasks(connection, config)

        connection.cursor.assert_not_called()

    def test_sync_tasks_batches_h3_rows_through_a_cursor(self):
        '''*!*! Explicit H3 assignments use Psycopg's cursor batch API.'''

        connection = MagicMock(spec=['execute', 'cursor'])
        cursor = connection.cursor.return_value.__enter__.return_value
        config = SimpleNamespace(
            project_id='camp',
            project_name='Camp',
            annotation_labels=('damaged', 'undamaged'),
            modes=('annotation',),
            user='alice',
            todo_h3_indexes=('8828308281fffff',),
        )

        sync_tasks(connection, config)

        cursor.executemany.assert_called_once()


if __name__ == '__main__':
    unittest.main()
