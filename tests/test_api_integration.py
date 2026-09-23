from __future__ import annotations

import os
import unittest
from uuid import uuid4

import psycopg

from src.api.main import app


@unittest.skipUnless(
    os.environ.get('TEST_DATABASE_URL'),
    'TEST_DATABASE_URL is required for PostGIS integration tests',
)
class ApiIntegrationTests(unittest.TestCase):
    '''*!*! Exercises shared review behavior against an migrated PostGIS.'''

    @classmethod
    def setUpClass(cls) -> None:
        '''*!*! Insert an isolated project, feature, tasks, and reviewers.'''

        cls.connection = psycopg.connect(
            os.environ['TEST_DATABASE_URL'],
            autocommit=True,
        )
        cls.project_id = f'api-test-{uuid4()}'
        cls.feature_id = 'building-one'
        cls.annotation_task = uuid4()
        cls.editing_task = uuid4()
        with cls.connection.transaction():
            cls.connection.execute(
                'INSERT INTO projects (id, name) VALUES (%s, %s)',
                [cls.project_id, 'API integration test'],
            )
            cls.connection.execute(
                '''
INSERT INTO features (project_id, id, geometry, properties)
VALUES (
    %s,
    %s,
    ST_GeomFromText('POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))', 4326),
    jsonb_build_object('source', 'test')
)
''',
                [cls.project_id, cls.feature_id],
            )
            cls.connection.execute(
                '''
INSERT INTO tasks (id, project_id, name, mode, labels)
VALUES
    (%s, %s, 'Annotations', 'annotation', ARRAY['damaged', 'undamaged']),
    (%s, %s, 'Edits', 'editing', ARRAY[]::text[])
''',
                [
                    cls.annotation_task,
                    cls.project_id,
                    cls.editing_task,
                    cls.project_id,
                ],
            )
            cls.connection.execute(
                '''
INSERT INTO task_reviewers (task_id, reviewer_id, all_features)
VALUES
    (%s, 'alice', true),
    (%s, 'bob', true),
    (%s, 'alice', true)
''',
                [cls.annotation_task, cls.annotation_task, cls.editing_task],
            )

    @classmethod
    def tearDownClass(cls) -> None:
        '''*!*! Remove integration records, including detached history rows.'''

        with cls.connection.transaction():
            cls.connection.execute(
                'DELETE FROM annotation_history WHERE project_id = %s',
                [cls.project_id],
            )
            cls.connection.execute(
                'DELETE FROM feature_history WHERE project_id = %s',
                [cls.project_id],
            )
            cls.connection.execute(
                'DELETE FROM projects WHERE id = %s',
                [cls.project_id],
            )
        cls.connection.close()

    def test_independent_annotations_and_optimistic_edits(self):
        '''*!*! Reviewers coexist and stale edits cannot overwrite features.'''

        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            alice = {'X-Reviewer-ID': 'alice'}
            bob = {'X-Reviewer-ID': 'bob'}
            annotation_url = (
                f'/api/tasks/{self.annotation_task}/features/'
                f'{self.feature_id}/annotation'
            )
            alice_response = client.put(
                annotation_url,
                headers=alice,
                json={'label': 'damaged', 'feature_version_seen': 1},
            )
            bob_response = client.put(
                annotation_url,
                headers=bob,
                json={'label': 'undamaged', 'feature_version_seen': 1},
            )

            self.assertEqual(alice_response.status_code, 200)
            self.assertEqual(bob_response.status_code, 200)
            revised_response = client.put(
                annotation_url,
                headers=alice,
                json={
                    'label': 'undamaged',
                    'notes': 'reconsidered',
                    'feature_version_seen': 1,
                },
            )
            self.assertEqual(revised_response.status_code, 200)
            self.assertEqual(revised_response.json()['version'], 2)
            count = self.connection.execute(
                'SELECT count(*) FROM annotations WHERE project_id = %s',
                [self.project_id],
            ).fetchone()[0]
            self.assertEqual(count, 2)
            history_count = self.connection.execute(
                'SELECT count(*) FROM annotation_history WHERE project_id = %s',
                [self.project_id],
            ).fetchone()[0]
            self.assertEqual(history_count, 1)

            edit_url = (
                f'/api/tasks/{self.editing_task}/features/{self.feature_id}'
            )
            edit_response = client.patch(
                edit_url,
                headers=alice,
                json={
                    'expected_version': 1,
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [
                            [[0, 0], [0, 2], [2, 2], [2, 0], [0, 0]],
                        ],
                    },
                    'properties': {'source': 'edited'},
                },
            )
            stale_response = client.patch(
                edit_url,
                headers=alice,
                json={
                    'expected_version': 1,
                    'properties': {'source': 'stale'},
                },
            )

            self.assertEqual(edit_response.status_code, 200)
            self.assertEqual(edit_response.json()['version'], 2)
            self.assertEqual(stale_response.status_code, 409)
            self.assertEqual(
                stale_response.json()['detail']['current']['version'],
                2,
            )
