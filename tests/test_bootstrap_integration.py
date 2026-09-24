from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb
import psycopg
import yaml

from src.api.bootstrap import bootstrap_project
from src.api.feature_store import read_project_features
from src.api.review_store import (
    read_reviewer_annotations,
    write_reviewer_annotation,
)
from src.project_config import load_review_config


@unittest.skipUnless(
    os.environ.get('TEST_DATABASE_URL'),
    'TEST_DATABASE_URL is required for PostGIS integration tests',
)
class BootstrapIntegrationTests(unittest.TestCase):
    '''*!*! Exercise repeatable GeoParquet initialization against PostGIS.'''

    def write_geoparquet(self, path: Path) -> None:
        '''*!*! Write two polygon features with distinct H3 assignments.'''

        connection = duckdb.connect()
        try:
            connection.execute('LOAD spatial')
            connection.execute(
                '''
CREATE TABLE bootstrap_source AS
SELECT *
FROM (
    VALUES
        (
            'one',
            '8828308281fffff',
            'first',
            ST_GeomFromText('POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))')
        ),
        (
            'two',
            '8828308283fffff',
            'second',
            ST_GeomFromText('POLYGON ((2 2, 2 3, 3 3, 3 2, 2 2))')
        )
) AS features(id, h3_r8, source, geometry)
'''
            )
            connection.execute(
                'COPY bootstrap_source TO ? (FORMAT PARQUET)',
                [str(path)],
            )
        finally:
            connection.close()

    def write_config(self, path: Path, project_id: str, source: Path) -> None:
        '''*!*! Write a complete project configuration for bootstrap.'''

        values = {
            'version': 1,
            'project': {'id': project_id, 'name': 'Bootstrap integration test'},
            'paths': {
                'features': str(source),
            },
            'fields': {'feature_id': 'id', 'h3_prefix': 'h3_r'},
            'annotation': {'labels': ['damaged', 'undamaged']},
            'workflow': {
                'user': 'alice',
                'modes': ['annotation'],
                'todo': [{'h3_index': '8828308281fffff'}],
            },
        }
        path.write_text(yaml.safe_dump(values), encoding='utf-8')

    def test_second_boot_preserves_features_and_refreshes_assignments(self):
        '''*!*! Repeated startup never overwrites a mutable database feature.'''

        database_url = os.environ['TEST_DATABASE_URL']
        project_id = f'bootstrap-test-{uuid4()}'
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            source = directory / 'features.parquet'
            config_path = directory / 'project.yaml'
            self.write_geoparquet(source)
            self.write_config(config_path, project_id, source)
            config = load_review_config(config_path)

            try:
                with patch.dict(os.environ, {'DATABASE_URL': database_url}):
                    first = bootstrap_project(config)
                    assigned = read_project_features(project_id, 'alice')
                    saved_review = write_reviewer_annotation(
                        project_id,
                        'alice',
                        'annotation',
                        {
                            'id': 'one',
                            'annotation_label': 'damaged',
                            'qa_notes': 'database review',
                            'feature_version_seen': 1,
                        },
                    )
                    reviews = read_reviewer_annotations(
                        project_id,
                        'alice',
                        'annotation',
                    )

                    with psycopg.connect(database_url) as connection:
                        connection.execute(
                            '''
UPDATE features
SET
    properties = properties || '{"edited": true}'::jsonb,
    version = version + 1,
    updated_by = 'alice'
WHERE project_id = %s AND id = 'one'
''',
                            [project_id],
                        )

                    second = bootstrap_project(config)

                with psycopg.connect(database_url) as connection:
                    feature_row = connection.execute(
                        '''
SELECT count(*), max(version), bool_or(properties @> '{"edited": true}'::jsonb)
FROM features
WHERE project_id = %s
''',
                        [project_id],
                    ).fetchone()
                    h3_count = connection.execute(
                        'SELECT count(*) FROM feature_h3 WHERE project_id = %s',
                        [project_id],
                    ).fetchone()[0]

                self.assertTrue(first.imported)
                self.assertFalse(second.imported)
                self.assertEqual(first.feature_count, 2)
                self.assertEqual(len(assigned['features']), 1)
                self.assertEqual(assigned['features'][0]['id'], 'one')
                self.assertEqual(saved_review['annotation_label'], 'damaged')
                self.assertEqual(reviews['one']['qa_notes'], 'database review')
                self.assertEqual(feature_row, (2, 2, True))
                self.assertEqual(h3_count, 2)
            finally:
                with psycopg.connect(database_url) as connection:
                    connection.execute(
                        'DELETE FROM feature_history WHERE project_id = %s',
                        [project_id],
                    )
                    connection.execute(
                        'DELETE FROM projects WHERE id = %s',
                        [project_id],
                    )


if __name__ == '__main__':
    unittest.main()
