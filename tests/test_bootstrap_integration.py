from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb
import numpy
import psycopg
import rasterio
from rasterio.transform import from_bounds
import yaml

from src.api.bootstrap import bootstrap_project
from src.api.feature_store import read_project_features, update_project_geometry
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
    '''*!*! Exercise repeatable multi-layer initialization against PostGIS.'''

    def write_geoparquets(self, polygons: Path, points: Path) -> None:
        '''*!*! Write polygon and statewide-like point sources.'''

        connection = duckdb.connect()
        try:
            connection.execute('LOAD spatial')
            connection.execute(
                '''
CREATE TABLE polygon_source AS
SELECT *
FROM (
    VALUES
        ('one', 'first', ST_GeomFromText('POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))')),
        ('two', 'second', ST_GeomFromText('POLYGON ((2 2, 2 3, 3 3, 3 2, 2 2))'))
) AS features(id, source, geometry)
'''
            )
            connection.execute(
                '''
CREATE TABLE point_source AS
SELECT *
FROM (
    VALUES
        ('point-inside', 'inside', ST_Point(1.5, 1.5)),
        ('point-outside', 'outside', ST_Point(10, 10))
) AS features(id, source, geometry)
'''
            )
            connection.execute('COPY polygon_source TO ? (FORMAT PARQUET)', [str(polygons)])
            connection.execute('COPY point_source TO ? (FORMAT PARQUET)', [str(points)])
        finally:
            connection.close()

    def write_cog(self, path: Path) -> None:
        '''*!*! Write a small EPSG:4326 raster covering the test features.'''

        with rasterio.open(
            path,
            'w',
            driver='GTiff',
            width=4,
            height=4,
            count=1,
            dtype='uint8',
            crs='EPSG:4326',
            transform=from_bounds(0, 0, 4, 4, 4, 4),
        ) as dataset:
            dataset.write(numpy.ones((1, 4, 4), dtype='uint8'))

    def write_config(
        self,
        path: Path,
        project_id: str,
        polygons: Path,
        points: Path,
        cog: Path,
    ) -> None:
        '''*!*! Write a two-layer project configuration for bootstrap.'''

        values = {
            'version': 2,
            'project': {'id': project_id, 'name': 'Bootstrap integration test'},
            'paths': {'imagery_cog': str(cog)},
            'layers': [
                {
                    'id': 'buildings',
                    'source': str(polygons),
                    'geometry_types': ['Polygon'],
                    'modes': ['annotation', 'editing'],
                    'editing': {'move': True, 'reshape': True},
                },
                {
                    'id': 'points',
                    'source': str(points),
                    'geometry_types': ['Point'],
                    'modes': ['annotation', 'editing'],
                    'editing': {'move': True},
                },
            ],
            'annotation': {'labels': ['damaged', 'undamaged']},
            'workflow': {
                'user': 'alice',
                'modes': ['annotation', 'editing'],
                'todo': [],
            },
        }
        path.write_text(yaml.safe_dump(values), encoding='utf-8')

    def test_second_boot_preserves_filtered_multi_layer_features(self):
        '''*!*! Repeated startup retains edits and excludes out-of-COG points.'''

        database_url = os.environ['TEST_DATABASE_URL']
        project_id = f'bootstrap-test-{uuid4()}'
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            polygons = directory / 'polygons.parquet'
            points = directory / 'points.parquet'
            cog = directory / 'imagery.tif'
            config_path = directory / 'project.yaml'
            self.write_geoparquets(polygons, points)
            self.write_cog(cog)
            self.write_config(config_path, project_id, polygons, points, cog)
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
                            'id': 'point-inside',
                            'layer_id': 'points',
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
                    moved_point = update_project_geometry(
                        project_id,
                        'alice',
                        'points',
                        'point-inside',
                        1,
                        {'type': 'Point', 'coordinates': [1.75, 1.75]},
                    )
                    edited_polygon = update_project_geometry(
                        project_id,
                        'alice',
                        'buildings',
                        'one',
                        1,
                        {
                            'type': 'Polygon',
                            'coordinates': [[
                                [0.1, 0.1],
                                [0.1, 1.0],
                                [1.0, 1.0],
                                [1.0, 0.1],
                                [0.1, 0.1],
                            ]],
                        },
                    )

                    second = bootstrap_project(config)

                with psycopg.connect(database_url) as connection:
                    feature_rows = connection.execute(
                        '''
SELECT layer_id, count(*), max(version)
FROM features
WHERE project_id = %s
GROUP BY layer_id
ORDER BY layer_id
''',
                        [project_id],
                    ).fetchall()
                    h3_count = connection.execute(
                        'SELECT count(*) FROM feature_h3 WHERE project_id = %s',
                        [project_id],
                    ).fetchone()[0]

                self.assertTrue(first.imported)
                self.assertFalse(second.imported)
                self.assertEqual(first.feature_count, 3)
                self.assertEqual(len(assigned['features']), 3)
                self.assertEqual(
                    {feature['layer_id'] for feature in assigned['features']},
                    {'buildings', 'points'},
                )
                self.assertEqual(saved_review['layer_id'], 'points')
                self.assertEqual(moved_point['version'], 2)
                self.assertEqual(moved_point['geometry']['coordinates'], [1.75, 1.75])
                self.assertEqual(edited_polygon['version'], 2)
                self.assertEqual(
                    reviews['["points","point-inside"]']['qa_notes'],
                    'database review',
                )
                self.assertEqual(feature_rows, [('buildings', 2, 2), ('points', 1, 2)])
                self.assertEqual(h3_count, 18)
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
