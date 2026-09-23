'''*!*! FastAPI service for concurrent feature review and editing.'''

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.api.auth import authenticated_reviewer
from src.api.database import create_pool, get_connection
from src.api.models import AnnotationSubmission, FeaturePatch


ConnectionDependency = Annotated[Connection, Depends(get_connection)]
ReviewerDependency = Annotated[str, Depends(authenticated_reviewer)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    '''*!*! Open and close the database pool with the API process.'''

    pool = create_pool()
    pool.open(wait=True)
    app.state.database_pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title='Damage Map Review API',
    version='0.1.0',
    lifespan=lifespan,
)


def geojson_feature(row: dict[str, Any]) -> dict[str, Any]:
    '''*!*! Convert a database feature row to a versioned GeoJSON feature.'''

    return {
        'type': 'Feature',
        'id': row['id'],
        'geometry': row['geometry'],
        'properties': row['properties'],
        'version': row['version'],
        'updated_by': row['updated_by'],
        'updated_at': row['updated_at'].isoformat(),
    }


def select_feature(
    connection: Connection,
    project_id: str,
    feature_id: str,
) -> dict[str, Any] | None:
    '''*!*! Read one feature with GeoJSON geometry from PostGIS.'''

    return connection.execute(
        '''
SELECT
    id,
    ST_AsGeoJSON(geometry)::jsonb AS geometry,
    properties,
    version,
    updated_by,
    updated_at
FROM features
WHERE project_id = %s AND id = %s
''',
        [project_id, feature_id],
    ).fetchone()


def assigned_task_feature(
    connection: Connection,
    task_id: UUID,
    feature_id: str,
    reviewer_id: str,
    *,
    lock: bool = False,
) -> dict[str, Any] | None:
    '''*!*! Return a task feature only when it is assigned to the reviewer.'''

    lock_sql = 'FOR SHARE OF feature' if lock else ''
    return connection.execute(
        f'''
SELECT
    task.project_id,
    task.mode,
    task.labels,
    feature.version AS feature_version
FROM task_reviewers AS reviewer
JOIN tasks AS task ON task.id = reviewer.task_id
JOIN features AS feature
  ON feature.project_id = task.project_id
 AND feature.id = %(feature_id)s
WHERE reviewer.task_id = %(task_id)s
  AND reviewer.reviewer_id = %(reviewer_id)s
  AND task.active
  AND (
      reviewer.all_features
      OR EXISTS (
          SELECT 1
          FROM task_h3_assignments AS assignment
          JOIN feature_h3 AS feature_cell
            ON feature_cell.project_id = feature.project_id
           AND feature_cell.feature_id = feature.id
           AND feature_cell.resolution = assignment.resolution
           AND feature_cell.h3_index = assignment.h3_index
          WHERE assignment.task_id = reviewer.task_id
            AND assignment.reviewer_id = reviewer.reviewer_id
      )
  )
{lock_sql}
''',
        {
            'task_id': task_id,
            'feature_id': feature_id,
            'reviewer_id': reviewer_id,
        },
    ).fetchone()


@app.get('/health')
def health(connection: ConnectionDependency) -> dict[str, str]:
    '''*!*! Confirm API, PostgreSQL, and PostGIS availability.'''

    row = connection.execute(
        'SELECT current_database() AS database, PostGIS_Version() AS postgis'
    ).fetchone()
    return {'status': 'ok', 'database': row['database'], 'postgis': row['postgis']}


@app.get('/api/tasks/{task_id}/features/{feature_id}')
def get_feature(
    task_id: UUID,
    feature_id: str,
    connection: ConnectionDependency,
    reviewer_id: ReviewerDependency,
) -> dict[str, Any]:
    '''*!*! Return an assigned feature without exposing peer annotations.'''

    assignment = assigned_task_feature(
        connection,
        task_id,
        feature_id,
        reviewer_id,
    )
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Assigned feature not found')
    row = select_feature(connection, assignment['project_id'], feature_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Feature not found')
    return geojson_feature(row)


@app.patch('/api/tasks/{task_id}/features/{feature_id}')
def patch_feature(
    task_id: UUID,
    feature_id: str,
    patch: FeaturePatch,
    connection: ConnectionDependency,
    reviewer_id: ReviewerDependency,
) -> dict[str, Any]:
    '''*!*! Apply an optimistic feature edit or return a version conflict.'''

    assignment = assigned_task_feature(
        connection,
        task_id,
        feature_id,
        reviewer_id,
        lock=True,
    )
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Assigned feature not found')
    if assignment['mode'] != 'editing':
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            'Task does not permit feature editing',
        )
    project_id = assignment['project_id']
    geometry = Jsonb(patch.geometry) if patch.geometry is not None else None
    properties = Jsonb(patch.properties) if patch.properties is not None else None
    if geometry is not None:
        valid = connection.execute(
            '''
SELECT ST_IsValid(requested) AND NOT ST_IsEmpty(requested) AS valid
FROM (
    SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326) AS requested
) AS geometry_check
''',
            [geometry],
        ).fetchone()['valid']
        if not valid:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                'Geometry is invalid',
            )

    row = connection.execute(
        '''
UPDATE features
SET
    geometry = CASE
        WHEN %(geometry)s::jsonb IS NULL THEN geometry
        ELSE ST_SetSRID(ST_GeomFromGeoJSON(%(geometry)s::text), 4326)
    END,
    properties = COALESCE(%(properties)s::jsonb, properties),
    version = version + 1,
    updated_by = %(reviewer_id)s,
    updated_at = now()
WHERE project_id = %(project_id)s
  AND id = %(feature_id)s
  AND version = %(expected_version)s
RETURNING
    id,
    ST_AsGeoJSON(geometry)::jsonb AS geometry,
    properties,
    version,
    updated_by,
    updated_at
''',
        {
            'geometry': geometry,
            'properties': properties,
            'reviewer_id': reviewer_id,
            'project_id': project_id,
            'feature_id': feature_id,
            'expected_version': patch.expected_version,
        },
    ).fetchone()
    if row is None:
        current = select_feature(connection, project_id, feature_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'Feature not found')
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                'message': 'Feature was changed by another reviewer',
                'current': geojson_feature(current),
            },
        )
    connection.execute(
        '''
UPDATE projects
SET revision = revision + 1, updated_at = now()
WHERE id = %s
''',
        [project_id],
    )
    return geojson_feature(row)


@app.get('/api/tasks/{task_id}/features')
def assigned_features(
    task_id: UUID,
    connection: ConnectionDependency,
    reviewer_id: ReviewerDependency,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 250,
) -> dict[str, Any]:
    '''*!*! Return one reviewer's assigned features and only their own labels.'''

    rows = connection.execute(
        '''
SELECT
    feature.id,
    ST_AsGeoJSON(feature.geometry)::jsonb AS geometry,
    feature.properties,
    feature.version,
    feature.updated_by,
    feature.updated_at,
    annotation.label AS annotation_label,
    annotation.notes AS annotation_notes,
    annotation.version AS annotation_version
FROM task_reviewers AS reviewer
JOIN tasks AS task ON task.id = reviewer.task_id
JOIN features AS feature ON feature.project_id = task.project_id
LEFT JOIN annotations AS annotation
    ON annotation.task_id = task.id
   AND annotation.feature_id = feature.id
   AND annotation.reviewer_id = reviewer.reviewer_id
WHERE reviewer.task_id = %(task_id)s
  AND reviewer.reviewer_id = %(reviewer_id)s
  AND task.active
  AND (%(after)s IS NULL OR feature.id > %(after)s)
  AND (
      reviewer.all_features
      OR EXISTS (
          SELECT 1
          FROM task_h3_assignments AS assignment
          JOIN feature_h3 AS feature_cell
            ON feature_cell.project_id = feature.project_id
           AND feature_cell.feature_id = feature.id
           AND feature_cell.resolution = assignment.resolution
           AND feature_cell.h3_index = assignment.h3_index
          WHERE assignment.task_id = reviewer.task_id
            AND assignment.reviewer_id = reviewer.reviewer_id
      )
  )
ORDER BY feature.id
LIMIT %(limit)s
''',
        {
            'task_id': task_id,
            'reviewer_id': reviewer_id,
            'after': after,
            'limit': limit,
        },
    ).fetchall()
    features = []
    for row in rows:
        feature = geojson_feature(row)
        feature['review'] = {
            'label': row['annotation_label'],
            'notes': row['annotation_notes'],
            'version': row['annotation_version'],
        }
        features.append(feature)
    return {
        'type': 'FeatureCollection',
        'features': features,
        'next_after': features[-1]['id'] if len(features) == limit else None,
    }


@app.put('/api/tasks/{task_id}/features/{feature_id}/annotation')
def submit_annotation(
    task_id: UUID,
    feature_id: str,
    submission: AnnotationSubmission,
    connection: ConnectionDependency,
    reviewer_id: ReviewerDependency,
) -> dict[str, Any]:
    '''*!*! Create or revise one reviewer's annotation without overwriting peers.'''

    assignment = assigned_task_feature(
        connection,
        task_id,
        feature_id,
        reviewer_id,
        lock=True,
    )
    if assignment is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            'Active assigned task feature not found',
        )
    if assignment['mode'] not in {'annotation', 'qaqc'}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            'Task does not accept annotation submissions',
        )
    if submission.label not in assignment['labels']:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Invalid label')
    if submission.feature_version_seen != assignment['feature_version']:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                'message': 'Feature changed after it was loaded',
                'current_version': assignment['feature_version'],
            },
        )

    annotation = connection.execute(
        '''
INSERT INTO annotations (
    task_id,
    project_id,
    feature_id,
    reviewer_id,
    label,
    notes,
    feature_version_seen
) VALUES (
    %(task_id)s,
    %(project_id)s,
    %(feature_id)s,
    %(reviewer_id)s,
    %(label)s,
    %(notes)s,
    %(feature_version_seen)s
)
ON CONFLICT (task_id, feature_id, reviewer_id) DO UPDATE
SET
    label = EXCLUDED.label,
    notes = EXCLUDED.notes,
    feature_version_seen = EXCLUDED.feature_version_seen,
    version = annotations.version + 1,
    updated_at = now()
WHERE annotations.label IS DISTINCT FROM EXCLUDED.label
   OR annotations.notes IS DISTINCT FROM EXCLUDED.notes
   OR annotations.feature_version_seen IS DISTINCT FROM EXCLUDED.feature_version_seen
RETURNING id, label, notes, feature_version_seen, version, created_at, updated_at
''',
        {
            'task_id': task_id,
            'project_id': assignment['project_id'],
            'feature_id': feature_id,
            'reviewer_id': reviewer_id,
            'label': submission.label,
            'notes': submission.notes,
            'feature_version_seen': submission.feature_version_seen,
        },
    ).fetchone()
    changed = annotation is not None
    if annotation is None:
        annotation = connection.execute(
            '''
SELECT id, label, notes, feature_version_seen, version, created_at, updated_at
FROM annotations
WHERE task_id = %s AND feature_id = %s AND reviewer_id = %s
''',
            [task_id, feature_id, reviewer_id],
        ).fetchone()
    if changed:
        connection.execute(
            '''
UPDATE projects
SET revision = revision + 1, updated_at = now()
WHERE id = %s
''',
            [assignment['project_id']],
        )
    return {
        'id': str(annotation['id']),
        'feature_id': feature_id,
        'reviewer_id': reviewer_id,
        'label': annotation['label'],
        'notes': annotation['notes'],
        'feature_version_seen': annotation['feature_version_seen'],
        'version': annotation['version'],
        'created_at': annotation['created_at'].isoformat(),
        'updated_at': annotation['updated_at'].isoformat(),
        'changed': changed,
    }
