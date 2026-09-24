'''*!*! Read reviewer-assigned project features from PostGIS.'''

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.api.database import database_url


def read_project_features(
    project_id: str,
    reviewer_id: str,
    *,
    feature_id_field: str = 'id',
) -> dict[str, Any]:
    '''*!*! Return a GeoJSON collection assigned to one project reviewer.'''

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        rows = connection.execute(
            '''
SELECT
    feature.id,
    ST_AsGeoJSON(feature.geometry)::jsonb AS geometry,
    feature.properties,
    feature.version,
    feature.updated_by,
    feature.updated_at
FROM features AS feature
WHERE feature.project_id = %(project_id)s
  AND EXISTS (
      SELECT 1
      FROM task_reviewers AS reviewer
      JOIN tasks AS task ON task.id = reviewer.task_id
      WHERE task.project_id = feature.project_id
        AND task.active
        AND reviewer.reviewer_id = %(reviewer_id)s
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
  )
ORDER BY feature.id
''',
            {'project_id': project_id, 'reviewer_id': reviewer_id},
        ).fetchall()

    features = []
    for row in rows:
        properties = dict(row['properties'])
        properties.setdefault(feature_id_field, row['id'])
        features.append({
            'type': 'Feature',
            'id': row['id'],
            'geometry': row['geometry'],
            'properties': properties,
            'version': row['version'],
            'updated_by': row['updated_by'],
            'updated_at': row['updated_at'].isoformat(),
        })
    return {'type': 'FeatureCollection', 'features': features}
