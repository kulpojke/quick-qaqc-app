import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from src.api.export import (
    AnnotationChangeToken,
    ExportChangeState,
    ExportResult,
    LayerChangeToken,
    annotation_snapshot_filename,
    edit_count_since,
    prune_old_snapshots,
    snapshot_filename,
    watch_project,
    write_sequence_geoparquet,
    write_sequence_parquet,
)
from src.project_config import EditingConfig, LayerConfig


class ExportTests(unittest.TestCase):
    '''*!*! Tests local PostGIS snapshot naming and GeoParquet writing.'''

    def layer(self) -> LayerConfig:
        '''*!*! Return a point layer with the generated columns under test.'''

        return LayerConfig(
            id='observations',
            name='Observations',
            source=Path('points.parquet'),
            source_crs='EPSG:4326',
            geometry_types=('Point',),
            feature_id_field='GLOBALID',
            predicted_class_field='',
            confidence_field='',
            display_fields=('DAMAGE',),
            h3_prefix='h3_r',
            h3_resolutions=(8,),
            modes=('annotation',),
            editing=EditingConfig(),
        )

    def test_snapshot_filename_is_revisioned_and_portable(self):
        '''*!*! Snapshot paths preserve identity without directory characters.'''

        self.assertEqual(
            snapshot_filename('Camp Fire / QAQC', 'observations', 17),
            'Camp-Fire-QAQC.observations.revision-17.parquet',
        )
        self.assertEqual(
            annotation_snapshot_filename('Camp Fire / QAQC', 17),
            'Camp-Fire-QAQC.annotations.revision-17.parquet',
        )

    def test_sequence_is_written_as_geoparquet(self):
        '''*!*! DuckDB retains attributes and emits spatial Parquet metadata.'''

        feature = {
            'type': 'Feature',
            'id': 'point-one',
            'geometry': {'type': 'Point', 'coordinates': [-121.2, 39.7]},
            'properties': {
                'GLOBALID': 'point-one',
                'DAMAGE': 'Destroyed',
                'h3_r8': '8828101235fffff',
                '_dm_project_id': 'camp',
                '_dm_layer_id': 'observations',
                '_dm_feature_id': 'point-one',
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            sequence_path = directory / 'features.geojsonl'
            output_path = directory / 'features.parquet'
            sequence_path.write_text(json.dumps(feature) + '\n', encoding='utf-8')

            write_sequence_geoparquet(
                sequence_path,
                output_path,
                self.layer(),
                expected_count=1,
            )

            connection = duckdb.connect()
            try:
                connection.execute('LOAD spatial')
                row = connection.execute(
                    '''
SELECT GLOBALID, DAMAGE, h3_r8, ST_AsText(geometry)
FROM read_parquet(?)
''',
                    [str(output_path)],
                ).fetchone()
                metadata = connection.execute(
                    'SELECT key FROM parquet_kv_metadata(?)',
                    [str(output_path)],
                ).fetchall()
            finally:
                connection.close()

        keys = {
            key.decode() if isinstance(key, bytes) else str(key)
            for key, in metadata
        }
        self.assertEqual(
            row,
            ('point-one', 'Destroyed', '8828101235fffff', 'POINT (-121.2 39.7)'),
        )
        self.assertIn('geo', keys)

    def test_empty_layer_still_produces_typed_geoparquet(self):
        '''*!*! Layers with no current features remain valid empty snapshots.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            sequence_path = directory / 'empty.geojsonl'
            output_path = directory / 'empty.parquet'
            sequence_path.touch()

            write_sequence_geoparquet(
                sequence_path,
                output_path,
                self.layer(),
                expected_count=0,
            )

            connection = duckdb.connect()
            try:
                connection.execute('LOAD spatial')
                count = connection.execute(
                    'SELECT count(*) FROM read_parquet(?)',
                    [str(output_path)],
                ).fetchone()[0]
                description = connection.execute(
                    'DESCRIBE SELECT * FROM read_parquet(?)',
                    [str(output_path)],
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(count, 0)
        self.assertEqual(
            [name for name, *_ in description],
            [
                '_dm_project_id',
                '_dm_layer_id',
                '_dm_feature_id',
                'GLOBALID',
                'h3_r8',
                'geometry',
            ],
        )

    def test_annotation_records_are_written_as_joinable_parquet(self):
        '''*!*! Annotation rows retain unique IDs and composite feature keys.'''

        annotation = {
            'annotation_id': 'annotation-one',
            '_dm_project_id': 'camp',
            '_dm_layer_id': 'observations',
            '_dm_feature_id': 'point-one',
            'task_id': 'task-one',
            'task_name': 'Annotate observations',
            'task_mode': 'annotation',
            'reviewer_id': 'alice',
            'label': 'damaged',
            'notes': '',
            'feature_version_seen': 2,
            'annotation_version': 3,
            'created_at': '2026-09-28T10:00:00+00:00',
            'updated_at': '2026-09-28T10:05:00+00:00',
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            sequence_path = directory / 'annotations.jsonl'
            output_path = directory / 'annotations.parquet'
            sequence_path.write_text(json.dumps(annotation) + '\n', encoding='utf-8')

            write_sequence_parquet(sequence_path, output_path, expected_count=1)

            connection = duckdb.connect()
            try:
                row = connection.execute(
                    '''
SELECT
    annotation_id,
    _dm_project_id,
    _dm_layer_id,
    _dm_feature_id,
    reviewer_id,
    label
FROM read_parquet(?)
''',
                    [str(output_path)],
                ).fetchone()
                columns = connection.execute(
                    'DESCRIBE SELECT * FROM read_parquet(?)',
                    [str(output_path)],
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(
            row,
            (
                'annotation-one',
                'camp',
                'observations',
                'point-one',
                'alice',
                'damaged',
            ),
        )
        self.assertNotIn('geometry', [name for name, *_ in columns])

    def test_pruning_keeps_two_revisions_and_protects_sources(self):
        '''*!*! Cleanup removes only old generated files, never source data.'''

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            paths = {
                revision: directory / f'camp.points.revision-{revision}.parquet'
                for revision in range(1, 5)
            }
            for path in paths.values():
                path.touch()
            unrelated = directory / 'original-points.parquet'
            unrelated.touch()

            deleted = prune_old_snapshots(
                directory,
                'camp',
                'points',
                keep_revisions=2,
                protected_paths={paths[1], unrelated},
            )

            self.assertEqual(deleted, (paths[2],))
            self.assertTrue(paths[1].exists())
            self.assertFalse(paths[2].exists())
            self.assertTrue(paths[3].exists())
            self.assertTrue(paths[4].exists())
            self.assertTrue(unrelated.exists())

    @patch('src.api.export.time.sleep')
    @patch('src.api.export.time.monotonic', side_effect=[0.0, 1.0, 2.0])
    @patch('src.api.export.export_project')
    @patch('src.api.export.read_export_change_state')
    def test_watch_exports_at_edit_threshold(
        self,
        read_state,
        export_project,
        _monotonic,
        _sleep,
    ):
        '''*!*! The worker exports as soon as fifty saved edits accumulate.'''

        layers = (
            LayerChangeToken('observations', 1, 0, 1, '2026-09-28T10:00:00'),
        )
        baseline = ExportChangeState(
            layers,
            AnnotationChangeToken(0, 0, None),
        )
        changed = ExportChangeState(
            layers,
            AnnotationChangeToken(50, 50, '2026-09-28T10:01:00'),
        )
        read_state.side_effect = [baseline, changed]
        export_project.return_value = (
            ExportResult('camp', 'observations', 2, 1, Path('/tmp/out.parquet')),
        )
        config = unittest.mock.Mock(project_id='camp')

        watch_project(
            config,
            poll_seconds=1,
            interval_seconds=300,
            edit_threshold=50,
            max_polls=1,
        )

        self.assertEqual(export_project.call_count, 2)
        self.assertEqual(export_project.call_args.kwargs['layer_ids'], set())
        self.assertTrue(export_project.call_args.kwargs['include_annotations'])

    def test_edit_count_uses_feature_and_annotation_versions(self):
        '''*!*! Feature mutations and annotation saves share the threshold.'''

        exported = ExportChangeState(
            (
                LayerChangeToken('buildings', 10, 0, 10, None),
                LayerChangeToken('points', 20, 0, 20, None),
            ),
            AnnotationChangeToken(4, 5, None),
        )
        current = ExportChangeState(
            (
                LayerChangeToken('buildings', 11, 0, 13, None),
                LayerChangeToken('points', 20, 1, 22, None),
            ),
            AnnotationChangeToken(5, 7, None),
        )

        self.assertEqual(edit_count_since(exported, current), 7)

    @patch('src.api.export.time.sleep')
    @patch('src.api.export.time.monotonic', side_effect=[0.0, 300.0, 301.0])
    @patch('src.api.export.export_project')
    @patch('src.api.export.read_export_change_state')
    def test_watch_exports_pending_edits_at_time_limit(
        self,
        read_state,
        export_project,
        _monotonic,
        _sleep,
    ):
        '''*!*! One pending edit exports when five minutes have elapsed.'''

        baseline = ExportChangeState(
            (LayerChangeToken(
                'observations', 1, 0, 1, '2026-09-28T10:00:00'
            ),),
            AnnotationChangeToken(0, 0, None),
        )
        changed = ExportChangeState(
            (LayerChangeToken(
                'observations', 1, 0, 2, '2026-09-28T10:01:00'
            ),),
            AnnotationChangeToken(0, 0, None),
        )
        read_state.side_effect = [baseline, changed]
        export_project.return_value = ()
        config = unittest.mock.Mock(project_id='camp')

        watch_project(
            config,
            poll_seconds=1,
            interval_seconds=300,
            edit_threshold=50,
            max_polls=1,
        )

        self.assertEqual(export_project.call_count, 2)
        self.assertEqual(
            export_project.call_args.kwargs['layer_ids'],
            {'observations'},
        )
        self.assertFalse(export_project.call_args.kwargs['include_annotations'])

    @patch('src.api.export.time.sleep')
    @patch('src.api.export.export_project')
    @patch('src.api.export.read_export_change_state')
    def test_watch_ignores_unchanged_database_state(
        self,
        read_state,
        export_project,
        _sleep,
    ):
        '''*!*! Unchanged feature and annotation counters do not export.'''

        baseline = ExportChangeState(
            (LayerChangeToken(
                'observations', 1, 0, 1, '2026-09-28T10:00:00'
            ),),
            AnnotationChangeToken(1, 1, '2026-09-28T10:00:00'),
        )
        read_state.side_effect = [baseline, baseline]
        export_project.return_value = ()
        config = unittest.mock.Mock(project_id='camp')

        watch_project(
            config,
            poll_seconds=1,
            interval_seconds=300,
            edit_threshold=50,
            max_polls=1,
        )

        export_project.assert_called_once()


if __name__ == '__main__':
    unittest.main()
