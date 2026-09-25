from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import UUID

import duckdb

from src.api.bootstrap import (
    canonical_source,
    read_geoparquet_rows,
    sync_tasks,
    task_id_for,
)
from src.project_config import EditingConfig, LayerConfig


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

        annotation_id = task_id_for('camp', 'buildings', 'annotation')

        self.assertIsInstance(annotation_id, UUID)
        self.assertEqual(
            annotation_id,
            task_id_for('camp', 'buildings', 'annotation'),
        )
        self.assertNotEqual(
            annotation_id,
            task_id_for('camp', 'points', 'annotation'),
        )
        self.assertNotEqual(
            annotation_id,
            task_id_for('camp', 'buildings', 'editing'),
        )

    def layer(self, modes: tuple[str, ...] = ('annotation',)) -> LayerConfig:
        '''*!*! Return a minimal layer for task and import tests.'''

        return LayerConfig(
            id='points',
            name='Points',
            source=Path('points.parquet'),
            source_crs='EPSG:4326',
            geometry_types=('Point',),
            feature_id_field='id',
            predicted_class_field='',
            confidence_field='',
            display_fields=(),
            h3_prefix='h3_r',
            h3_resolutions=(8,),
            modes=modes,
            editing=EditingConfig(move='editing' in modes),
        )

    def test_sync_tasks_skips_empty_h3_batch(self):
        '''*!*! All-feature assignments do not execute an empty H3 batch.'''

        connection = MagicMock(spec=['execute', 'cursor'])
        config = SimpleNamespace(
            project_id='camp',
            project_name='Camp',
            annotation_labels=('damaged', 'undamaged'),
            modes=('annotation',),
            layers=(self.layer(),),
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
            layers=(self.layer(),),
            user='alice',
            todo_h3_indexes=('8828308281fffff',),
        )

        sync_tasks(connection, config)

        cursor.executemany.assert_called_once()

    def test_filters_layers_to_cog_bounds_and_generates_h3(self):
        '''*!*! Point imports retain only COG-intersecting source features.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'points.parquet'
            connection = duckdb.connect()
            try:
                connection.execute('LOAD spatial')
                connection.execute(
                    '''
CREATE TABLE points AS
SELECT *
FROM (
    VALUES
        ('inside', ST_Point(0.5, 0.5)),
        ('outside', ST_Point(10, 10))
) AS source(id, geometry)
'''
                )
                connection.execute('COPY points TO ? (FORMAT PARQUET)', [str(source)])
            finally:
                connection.close()

            layer = self.layer()
            layer = LayerConfig(**{**vars(layer), 'source': source})
            rows = read_geoparquet_rows(layer, (0, 0, 1, 1))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'inside')
        self.assertIn('"8":', rows[0][3])


if __name__ == '__main__':
    unittest.main()
