'''*!*! Persist browser annotation records in PostGIS.'''

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.api.database import database_url


class ReviewStoreError(RuntimeError):
    '''*!*! Report a review request error with its HTTP response status.'''

    def __init__(self, message: str, status: HTTPStatus):
        '''*!*! Store the client-facing message and HTTP status.'''

        super().__init__(message)
        self.status = status


def annotation_key(layer_id: str, feature_id: str) -> str:
    '''*!*! Return the stable browser key for one layer feature.'''

    return json.dumps([layer_id, feature_id], separators=(',', ':'))


def reviewer_task(
    connection,
    project_id: str,
    reviewer_id: str,
    mode: str,
    layer_id: str,
) -> dict[str, Any]:
    '''*!*! Return the active task assigned for one layer and workflow mode.'''

    rows = connection.execute(
        '''
SELECT task.id, task.labels
FROM tasks AS task
JOIN task_reviewers AS reviewer ON reviewer.task_id = task.id
WHERE task.project_id = %s
  AND task.layer_id = %s
  AND task.mode = %s
  AND task.active
  AND reviewer.reviewer_id = %s
ORDER BY task.created_at, task.id
LIMIT 2
''',
        [project_id, layer_id, mode, reviewer_id],
    ).fetchall()
    if not rows:
        raise ReviewStoreError(
            f'No active {mode} task for layer {layer_id!r} is assigned to {reviewer_id}',
            HTTPStatus.FORBIDDEN,
        )
    if len(rows) > 1:
        raise ReviewStoreError(
            f'Multiple active {mode} tasks for layer {layer_id!r} are assigned '
            f'to {reviewer_id}',
            HTTPStatus.CONFLICT,
        )
    return rows[0]


def browser_annotation(row: dict[str, Any]) -> dict[str, Any]:
    '''*!*! Convert one database annotation to the browser review shape.'''

    return {
        'id': row['feature_id'],
        'layer_id': row['layer_id'],
        'annotation_label': row['label'],
        'qa_status': 'annotated',
        'qa_correct_class': row['label'],
        'qa_notes': row['notes'],
        'reviewer': row['reviewer_id'],
        'reviewed_at': row['updated_at'].isoformat(),
        'feature_version_seen': row['feature_version_seen'],
        'version': row['version'],
    }


def read_reviewer_annotations(
    project_id: str,
    reviewer_id: str,
    mode: str,
) -> dict[str, dict[str, Any]]:
    '''*!*! Read one reviewer's records across layers for a workflow mode.'''

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        task_rows = connection.execute(
            '''
SELECT task.id
FROM tasks AS task
JOIN task_reviewers AS reviewer ON reviewer.task_id = task.id
WHERE task.project_id = %s
  AND task.mode = %s
  AND task.active
  AND reviewer.reviewer_id = %s
''',
            [project_id, mode, reviewer_id],
        ).fetchall()
        if not task_rows:
            raise ReviewStoreError(
                f'No active {mode} tasks are assigned to {reviewer_id}',
                HTTPStatus.FORBIDDEN,
            )
        rows = connection.execute(
            '''
SELECT
    feature_id,
    layer_id,
    reviewer_id,
    label,
    notes,
    feature_version_seen,
    version,
    updated_at
FROM annotations
WHERE task_id = ANY(%s) AND reviewer_id = %s
ORDER BY layer_id, feature_id
''',
            [[row['id'] for row in task_rows], reviewer_id],
        ).fetchall()
    return {
        annotation_key(row['layer_id'], row['feature_id']): browser_annotation(row)
        for row in rows
    }


def write_reviewer_annotation(
    project_id: str,
    reviewer_id: str,
    mode: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    '''*!*! Create or update one assigned layer-specific review record.'''

    feature_id = str(payload.get('id', '')).strip()
    layer_id = str(payload.get('layer_id', '')).strip()
    label = str(
        payload.get('annotation_label')
        or payload.get('qa_correct_class')
        or ''
    ).strip()
    notes = str(payload.get('qa_notes') or '')
    feature_version_seen = payload.get('feature_version_seen')
    if not feature_id:
        raise ReviewStoreError('Missing feature id', HTTPStatus.BAD_REQUEST)
    if not layer_id:
        raise ReviewStoreError('Missing layer id', HTTPStatus.BAD_REQUEST)
    if not label:
        raise ReviewStoreError('Missing annotation label', HTTPStatus.BAD_REQUEST)
    if len(label) > 100:
        raise ReviewStoreError('Annotation label is too long', HTTPStatus.BAD_REQUEST)
    if len(notes) > 5000:
        raise ReviewStoreError('Annotation notes are too long', HTTPStatus.BAD_REQUEST)
    if not isinstance(feature_version_seen, int) or feature_version_seen < 1:
        raise ReviewStoreError(
            'Missing or invalid feature version',
            HTTPStatus.BAD_REQUEST,
        )

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        task = reviewer_task(connection, project_id, reviewer_id, mode, layer_id)
        if label not in task['labels']:
            raise ReviewStoreError('Invalid annotation label', HTTPStatus.BAD_REQUEST)
        assignment = connection.execute(
            '''
SELECT feature.version
FROM task_reviewers AS reviewer
JOIN tasks AS task ON task.id = reviewer.task_id
JOIN features AS feature
  ON feature.project_id = task.project_id
 AND feature.layer_id = task.layer_id
 AND feature.id = %(feature_id)s
WHERE reviewer.task_id = %(task_id)s
  AND reviewer.reviewer_id = %(reviewer_id)s
  AND feature.deleted_at IS NULL
  AND task.active
  AND (
      reviewer.all_features
      OR EXISTS (
          SELECT 1
          FROM task_h3_assignments AS assigned
          JOIN feature_h3 AS feature_cell
            ON feature_cell.project_id = feature.project_id
           AND feature_cell.layer_id = feature.layer_id
           AND feature_cell.feature_id = feature.id
           AND feature_cell.resolution = assigned.resolution
           AND feature_cell.h3_index = assigned.h3_index
          WHERE assigned.task_id = reviewer.task_id
            AND assigned.reviewer_id = reviewer.reviewer_id
      )
  )
FOR SHARE OF feature
''',
            {
                'task_id': task['id'],
                'reviewer_id': reviewer_id,
                'feature_id': feature_id,
            },
        ).fetchone()
        if assignment is None:
            raise ReviewStoreError(
                'Feature is not assigned to this reviewer',
                HTTPStatus.FORBIDDEN,
            )
        if feature_version_seen != assignment['version']:
            raise ReviewStoreError(
                'Feature changed after it was loaded; refresh before reviewing',
                HTTPStatus.CONFLICT,
            )

        annotation = connection.execute(
            '''
INSERT INTO annotations (
    task_id,
    project_id,
    layer_id,
    feature_id,
    reviewer_id,
    label,
    notes,
    feature_version_seen
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (task_id, layer_id, feature_id, reviewer_id) DO UPDATE SET
    label = EXCLUDED.label,
    notes = EXCLUDED.notes,
    feature_version_seen = EXCLUDED.feature_version_seen,
    version = annotations.version + 1,
    updated_at = now()
WHERE annotations.label IS DISTINCT FROM EXCLUDED.label
   OR annotations.notes IS DISTINCT FROM EXCLUDED.notes
   OR annotations.feature_version_seen IS DISTINCT FROM EXCLUDED.feature_version_seen
RETURNING
    feature_id,
    layer_id,
    reviewer_id,
    label,
    notes,
    feature_version_seen,
    version,
    updated_at
''',
            [
                task['id'],
                project_id,
                layer_id,
                feature_id,
                reviewer_id,
                label,
                notes,
                feature_version_seen,
            ],
        ).fetchone()
        if annotation is None:
            annotation = connection.execute(
                '''
SELECT
    feature_id,
    layer_id,
    reviewer_id,
    label,
    notes,
    feature_version_seen,
    version,
    updated_at
FROM annotations
WHERE task_id = %s
  AND layer_id = %s
  AND feature_id = %s
  AND reviewer_id = %s
''',
                [task['id'], layer_id, feature_id, reviewer_id],
            ).fetchone()
        else:
            connection.execute(
                '''
UPDATE projects
SET revision = revision + 1, updated_at = now()
WHERE id = %s
''',
                [project_id],
            )
    return browser_annotation(annotation)
