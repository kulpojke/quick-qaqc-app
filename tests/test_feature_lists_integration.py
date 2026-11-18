'''*!*! Per-building lists against a real PostGIS: permissions, top-ups, re-share, upgrade.'''

from __future__ import annotations

from dataclasses import asdict
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb
import numpy
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import rasterio
from rasterio.transform import from_bounds
import yaml

from src.api.auto_assign import AutoAssignOptions, config_signature, saved_assignment_config
from src.api.bootstrap import bootstrap_project
from src.api.export import write_annotation_sequence
from src.api.expunge import expunge_reviewer
from src.api.feature_store import create_project_polygon, read_project_features
from src.api.review_store import ReviewStoreError, write_reviewer_annotation
from src.project_config import load_review_config

REVIEWERS = ('alice', 'bob', 'carol', 'dave')
# *!*! Three clusters of three buildings, far apart, so each is its own r5 hex.
CLUSTERS = {'a': (0.5, 0.5), 'b': (1.5, 1.5), 'c': (2.5, 2.5)}


@unittest.skipUnless(
    os.environ.get('TEST_DATABASE_URL'),
    'TEST_DATABASE_URL is required for PostGIS integration tests',
)
class FeatureListIntegrationTests(unittest.TestCase):
    '''*!*! Each test builds its own project and removes it afterwards.'''

    def setUp(self):
        self.database_url = os.environ['TEST_DATABASE_URL']
        self.project_id = f'lists-test-{uuid4()}'
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.write_buildings(directory / 'buildings.parquet')
        self.write_cog(directory / 'cog.tif')
        self.config_path = directory / 'project.yaml'
        self.write_config(self.config_path, directory)
        self.config = load_review_config(self.config_path)
        self.environment = patch.dict(os.environ, {'DATABASE_URL': self.database_url})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        with psycopg.connect(self.database_url) as connection:
            connection.execute('DELETE FROM feature_history WHERE project_id = %s', [self.project_id])
            connection.execute('DELETE FROM projects WHERE id = %s', [self.project_id])
        self.temp.cleanup()

    # *!*! Fixtures ---------------------------------------------------------

    def write_buildings(self, path):
        rows = []
        for cluster, (x, y) in CLUSTERS.items():
            for index in range(3):
                left = x + index * 0.001
                rows.append(
                    f"('{cluster}{index}', 'lidar', ST_GeomFromText('POLYGON (("
                    f"{left} {y}, {left} {y + 0.0005}, {left + 0.0005} {y + 0.0005}, "
                    f"{left + 0.0005} {y}, {left} {y}))'))"
                )
        connection = duckdb.connect()
        try:
            connection.execute('LOAD spatial')
            connection.execute(
                'CREATE TABLE source AS SELECT * FROM (VALUES '
                + ', '.join(rows) + ') AS features(id, origin, geometry)'
            )
            connection.execute('COPY source TO ? (FORMAT PARQUET)', [str(path)])
        finally:
            connection.close()

    def write_cog(self, path):
        with rasterio.open(path, 'w', driver='GTiff', width=4, height=4, count=1,
                           dtype='uint8', crs='EPSG:4326',
                           transform=from_bounds(0, 0, 4, 4, 4, 4)) as dataset:
            dataset.write(numpy.ones((1, 4, 4), dtype='uint8'))

    def write_config(self, path, directory, caps=None, reviewers=REVIEWERS):
        caps = caps or {}
        values = {
            'version': 3,
            'project': {'id': self.project_id, 'name': 'Feature list test'},
            'paths': {'imagery_cog': str(directory / 'cog.tif')},
            'layers': [{
                'id': 'buildings',
                'source': str(directory / 'buildings.parquet'),
                'geometry_types': ['Polygon'],
                'fields': {'feature_id': 'id', 'origin': 'origin'},
                'h3_resolutions': [5, 8],
                'modes': ['annotation', 'editing'],
                'editing': {'move': True, 'reshape': True, 'create': True},
            }],
            'annotation': {'labels': ['damaged', 'undamaged', 'unknown']},
            'workflow': {
                'modes': ['annotation', 'editing'],
                'reviewers': [{
                    'id': reviewer,
                    'assignments': [{
                        'layer': 'buildings',
                        'modes': ['annotation', 'editing'],
                        **({'max_features': caps[reviewer]} if reviewer in caps else {}),
                    }],
                } for reviewer in reviewers],
            },
        }
        path.write_text(yaml.safe_dump(values), encoding='utf-8')

    def config_with(self, caps=None, reviewers=REVIEWERS):
        path = Path(self.temp.name) / f'project-{uuid4()}.yaml'
        self.write_config(path, Path(self.temp.name), caps, reviewers)
        return load_review_config(path)

    # *!*! Helpers ----------------------------------------------------------

    def lists(self):
        '''*!*! feature -> {reviewer: reason} for the buildings annotation task.'''

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                '''
SELECT listed.feature_id, listed.reviewer_id, listed.reason
FROM task_feature_assignments AS listed
JOIN tasks AS task ON task.id = listed.task_id
WHERE task.project_id = %s AND task.mode = 'annotation'
''',
                [self.project_id],
            ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row['feature_id'], {})[row['reviewer_id']] = row['reason']
        return result

    def label(self, reviewer, feature_id, label='damaged'):
        return write_reviewer_annotation(self.project_id, reviewer, 'annotation', {
            'id': feature_id, 'layer_id': 'buildings', 'annotation_label': label,
            'feature_version_seen': 1,
        })

    def visible(self, reviewer):
        '''*!*! feature -> annotatable, as the browser receives it.'''

        return {feature['id']: feature['annotatable']
                for feature in read_project_features(self.project_id, reviewer)['features']}

    # *!*! Tests ------------------------------------------------------------

    def test_lists_permissions_unknown_top_up_drawing_and_restart(self):
        bootstrap_project(self.config, AutoAssignOptions(1, 2, resolution=5))
        lists = self.lists()
        self.assertEqual(len(lists), 9)
        self.assertTrue(all(len(holders) == 2 for holders in lists.values()))

        holders = sorted(lists['a0'])
        outsider = next(reviewer for reviewer in REVIEWERS if reviewer not in lists['a0'])
        self.assertEqual(self.visible(holders[0]).get('a1'), True)
        self.assertNotIn('a0', self.visible(outsider))

        # *!*! A first "unknown" brings a third reviewer at once, with the hex.
        self.label(holders[0], 'a0', 'unknown')
        lists = self.lists()
        self.assertEqual(len(lists['a0']), 3)
        third = next(reviewer for reviewer in lists['a0'] if reviewer not in holders)
        self.assertEqual(lists['a0'][third], 'topped_up')
        seen = self.visible(third)
        self.assertTrue(seen['a0'])
        # *!*! The rest of that hex is visible to them but not theirs to label.
        if 'a1' not in lists or third not in lists['a1']:
            self.assertFalse(seen['a1'])
            with self.assertRaises(ReviewStoreError) as refused:
                self.label(third, 'a1')
            self.assertEqual(refused.exception.status, 403)
        self.label(third, 'a0', 'damaged')

        # *!*! A drawn building goes to its drawer and one other reviewer.
        drawer = holders[1]
        created = create_project_polygon(self.project_id, drawer, 'buildings', {
            'type': 'Polygon', 'coordinates': [[
                [0.5021, 0.5], [0.5021, 0.5004], [0.5025, 0.5004], [0.5025, 0.5], [0.5021, 0.5],
            ]],
        })
        self.assertTrue(created['annotatable'])
        drawn = self.lists()[created['id']]
        self.assertEqual(drawn[drawer], 'drawn')
        self.assertEqual(len(drawn), 2)

        # *!*! A plain restart keeps the lists, top-ups included, and their hexes.
        before = self.lists()
        bootstrap_project(self.config)
        self.assertEqual(self.lists(), before)
        self.assertTrue(self.visible(third)['a0'])
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            effective = saved_assignment_config(connection, self.config)
        third_cells = effective.reviewer(third).assignment_for('buildings', 'annotation').h3_indexes
        self.assertTrue(third_cells)

    def test_reshare_after_a_reviewer_leaves_keeps_their_labels_counting(self):
        bootstrap_project(self.config, AutoAssignOptions(1, 2, resolution=5))
        leaver = sorted(self.lists()['b0'])[0]
        for feature_id in ('b0', 'b1', 'b2'):
            self.label(leaver, feature_id)
        remaining = tuple(reviewer for reviewer in REVIEWERS if reviewer != leaver)
        smaller = self.config_with(reviewers=remaining)
        with self.assertRaisesRegex(ValueError, 'eligibility changed'):
            bootstrap_project(smaller)
        bootstrap_project(smaller, AutoAssignOptions(1, 2, resolution=5))
        lists = self.lists()
        for feature_id in ('b0', 'b1', 'b2'):
            self.assertEqual(len(lists[feature_id]), 1)
            self.assertNotIn(leaver, lists[feature_id])
        for feature_id in ('a0', 'c0'):
            self.assertEqual(len(lists[feature_id]), 2)

    def test_caps_limit_a_list(self):
        capped = self.config_with(caps={'alice': 2})
        bootstrap_project(capped, AutoAssignOptions(1, 2, resolution=5))
        lists = self.lists()
        self.assertEqual(sum('alice' in holders for holders in lists.values()), 2)
        self.assertTrue(all(len(holders) == 2 for holders in lists.values()))

    def test_whole_hex_database_from_before_upgrades_without_losing_labels(self):
        '''*!*! What the droplet has now: a saved whole-hex allocation and labels.'''

        bootstrap_project(self.config, manual_assign=True)
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            cells = {row['feature_id']: row['h3_index'] for row in connection.execute(
                "SELECT feature_id, h3_index FROM feature_h3 WHERE project_id = %s AND resolution = 5",
                [self.project_id],
            ).fetchall()}
            old_options = AutoAssignOptions(1, 2, resolution=5)
            connection.execute(
                '''
INSERT INTO auto_hex_assignments (project_id, signature, options, allocations)
VALUES (%s, %s, %s, %s)
''',
                [self.project_id, config_signature(self.config), Jsonb(asdict(old_options)),
                 Jsonb({'buildings': {'alice': [cells['a0'], cells['b0']], 'bob': [cells['a0'], cells['c0']],
                                      'carol': [cells['b0']], 'dave': [cells['c0']]}})],
            )
        # *!*! A plain restart after the upgrade keeps whole-hex labelling.
        bootstrap_project(self.config)
        self.assertEqual(set(self.visible('alice').values()), {True})
        self.assertEqual(set(self.visible('alice')), {'a0', 'a1', 'a2', 'b0', 'b1', 'b2'})
        self.label('alice', 'a0')
        self.label('bob', 'a0')
        self.label('alice', 'b1')
        # *!*! The same flags now switch to lists and keep the labels.
        bootstrap_project(self.config, old_options)
        lists = self.lists()
        self.assertEqual(set(lists['a0']), {'alice', 'bob'})
        self.assertIn('alice', lists['b1'])
        self.assertEqual(len(lists['b1']), 2)
        self.assertTrue(all(len(holders) == 2 for holders in lists.values()))
        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                'SELECT count(*) FROM annotations WHERE project_id = %s', [self.project_id],
            ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_expunged_labels_stop_counting_and_leave_exports(self):
        bootstrap_project(self.config, AutoAssignOptions(1, 2, resolution=5))
        holders = sorted(self.lists()['c0'])
        self.label(holders[0], 'c0')
        self.assertEqual(expunge_reviewer(self.project_id, holders[0], 'test'), 1)
        with self.assertRaisesRegex(ValueError, 'still in the YAML'):
            bootstrap_project(self.config)
        remaining = tuple(reviewer for reviewer in REVIEWERS if reviewer != holders[0])
        smaller = self.config_with(reviewers=remaining)
        bootstrap_project(smaller, AutoAssignOptions(1, 2, resolution=5))
        self.assertEqual(len(self.lists()['c0']), 2)
        self.assertNotIn(holders[0], self.lists()['c0'])
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(write_annotation_sequence(connection, smaller, io.StringIO()), 0)
        expunge_reviewer(self.project_id, holders[0], restore=True)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(write_annotation_sequence(connection, smaller, io.StringIO()), 1)


if __name__ == '__main__':
    unittest.main()
