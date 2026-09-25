'''*!*! Read reviewer-assigned project layers from PostGIS.'''

from __future__ import annotations

import math
from http import HTTPStatus
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.api.database import database_url


class FeatureStoreError(RuntimeError):
    '''*!*! Report a browser feature-edit failure with an HTTP status.'''

    def __init__(self, message: str, status: HTTPStatus):
        '''*!*! Preserve a safe client message and its response status.'''

        super().__init__(message)
        self.status = status


def read_project_features(
    project_id: str,
    reviewer_id: str,
) -> dict[str, Any]:
    '''*!*! Return assigned, non-deleted features from every project layer.'''

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        rows = connection.execute(
            '''
SELECT
    feature.layer_id,
    layer.name AS layer_name,
    layer.feature_id_field,
    layer.fields AS configured_fields,
    layer.h3_prefix,
    layer.editing,
    feature.id,
    ST_AsGeoJSON(feature.geometry)::jsonb AS geometry,
    feature.properties,
    feature.version,
    feature.updated_by,
    feature.updated_at,
    COALESCE(
        (
            SELECT jsonb_object_agg(cell.resolution::text, cell.h3_index)
            FROM feature_h3 AS cell
            WHERE cell.project_id = feature.project_id
              AND cell.layer_id = feature.layer_id
              AND cell.feature_id = feature.id
        ),
        '{}'::jsonb
    ) AS h3
FROM features AS feature
JOIN feature_layers AS layer
  ON layer.project_id = feature.project_id
 AND layer.id = feature.layer_id
WHERE feature.project_id = %(project_id)s
  AND feature.deleted_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM task_reviewers AS reviewer
      JOIN tasks AS task ON task.id = reviewer.task_id
      WHERE task.project_id = feature.project_id
        AND task.layer_id = feature.layer_id
        AND task.active
        AND reviewer.reviewer_id = %(reviewer_id)s
        AND (
            reviewer.all_features
            OR EXISTS (
                SELECT 1
                FROM task_h3_assignments AS assignment
                JOIN feature_h3 AS feature_cell
                  ON feature_cell.project_id = feature.project_id
                 AND feature_cell.layer_id = feature.layer_id
                 AND feature_cell.feature_id = feature.id
                 AND feature_cell.resolution = assignment.resolution
                 AND feature_cell.h3_index = assignment.h3_index
                WHERE assignment.task_id = reviewer.task_id
                  AND assignment.reviewer_id = reviewer.reviewer_id
            )
        )
  )
ORDER BY feature.layer_id, feature.id
''',
            {'project_id': project_id, 'reviewer_id': reviewer_id},
        ).fetchall()

    features = []
    for row in rows:
        configured_fields = row['configured_fields'] or {}
        retained_names = {
            configured_fields.get('feature_id'),
            configured_fields.get('predicted_class'),
            configured_fields.get('confidence'),
            *(configured_fields.get('display') or []),
        }
        properties = {
            name: row['properties'].get(name)
            for name in retained_names
            if name and name in row['properties']
        }
        properties.setdefault(row['feature_id_field'], row['id'])
        for resolution, index in row['h3'].items():
            properties[f'{row["h3_prefix"]}{resolution}'] = index
        features.append({
            'type': 'Feature',
            'id': row['id'],
            'layer_id': row['layer_id'],
            'layer_name': row['layer_name'],
            'editing': row['editing'],
            'h3': row['h3'],
            'geometry': row['geometry'],
            'properties': properties,
            'version': row['version'],
            'updated_by': row['updated_by'],
            'updated_at': row['updated_at'].isoformat(),
        })
    return {'type': 'FeatureCollection', 'features': features}


def _point_coordinates(geometry: object) -> tuple[float, float]:
    '''*!*! Validate and normalize one GeoJSON Point coordinate pair.'''

    if not isinstance(geometry, dict) or geometry.get('type') != 'Point':
        raise FeatureStoreError(
            'Only Point movement is supported by the browser',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    coordinates = geometry.get('coordinates')
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise FeatureStoreError(
            'Point coordinates are invalid',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        longitude = float(coordinates[0])
        latitude = float(coordinates[1])
    except (TypeError, ValueError) as error:
        raise FeatureStoreError(
            'Point coordinates are invalid',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        ) from error
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise FeatureStoreError(
            'Point coordinates are invalid',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise FeatureStoreError(
            'Point coordinates fall outside EPSG:4326',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return longitude, latitude


def update_project_geometry(
    project_id: str,
    reviewer_id: str,
    layer_id: str,
    feature_id: str,
    expected_version: int,
    geometry: object,
) -> dict[str, Any]:
    '''*!*! Update assigned point or polygon geometry with version checking.'''

    import h3

    if not isinstance(geometry, dict):
        raise FeatureStoreError(
            'Geometry must be a GeoJSON object',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    geometry_type = geometry.get('type')
    if geometry_type == 'Point':
        longitude, latitude = _point_coordinates(geometry)
        normalized_geometry = {
            'type': 'Point',
            'coordinates': [longitude, latitude],
        }
    elif geometry_type in {'Polygon', 'MultiPolygon'}:
        normalized_geometry = geometry
    else:
        raise FeatureStoreError(
            'Browser editing supports Point, Polygon, and MultiPolygon geometry',
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        assignment = connection.execute(
            '''
SELECT layer.geometry_types, layer.editing
FROM task_reviewers AS reviewer
JOIN tasks AS task ON task.id = reviewer.task_id
JOIN feature_layers AS layer
  ON layer.project_id = task.project_id
 AND layer.id = task.layer_id
JOIN features AS feature
  ON feature.project_id = task.project_id
 AND feature.layer_id = task.layer_id
 AND feature.id = %(feature_id)s
WHERE task.project_id = %(project_id)s
  AND task.layer_id = %(layer_id)s
  AND task.mode = 'editing'
  AND task.active
  AND reviewer.reviewer_id = %(reviewer_id)s
  AND feature.deleted_at IS NULL
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
FOR UPDATE OF feature
''',
            {
                'project_id': project_id,
                'reviewer_id': reviewer_id,
                'layer_id': layer_id,
                'feature_id': feature_id,
            },
        ).fetchone()
        if assignment is None:
            raise FeatureStoreError(
                'Feature is not assigned for editing',
                HTTPStatus.FORBIDDEN,
            )
        if geometry_type not in assignment['geometry_types']:
            raise FeatureStoreError(
                'Layer does not accept this geometry type',
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        editing = assignment['editing']
        if geometry_type == 'Point' and not editing.get('move'):
            raise FeatureStoreError(
                'Point movement is disabled for this layer',
                HTTPStatus.FORBIDDEN,
            )
        if (
            geometry_type in {'Polygon', 'MultiPolygon'}
            and not editing.get('move')
            and not editing.get('reshape')
        ):
            raise FeatureStoreError(
                'Polygon editing is disabled for this layer',
                HTTPStatus.FORBIDDEN,
            )
        valid = connection.execute(
            '''
SELECT ST_IsValid(requested) AND NOT ST_IsEmpty(requested) AS valid
FROM (
    SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326) AS requested
) AS geometry_check
''',
            [Jsonb(normalized_geometry)],
        ).fetchone()['valid']
        if not valid:
            raise FeatureStoreError(
                'Geometry is invalid',
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        updated = connection.execute(
            '''
UPDATE features
SET
    geometry = ST_SetSRID(ST_GeomFromGeoJSON(%(geometry)s::text), 4326),
    version = version + 1,
    updated_by = %(reviewer_id)s,
    updated_at = now()
WHERE project_id = %(project_id)s
  AND layer_id = %(layer_id)s
  AND id = %(feature_id)s
  AND version = %(expected_version)s
RETURNING
    layer_id,
    id,
    ST_AsGeoJSON(geometry)::jsonb AS geometry,
    properties,
    version,
    updated_by,
    updated_at
''',
            {
                'geometry': Jsonb(normalized_geometry),
                'reviewer_id': reviewer_id,
                'project_id': project_id,
                'layer_id': layer_id,
                'feature_id': feature_id,
                'expected_version': expected_version,
            },
        ).fetchone()
        if updated is None:
            raise FeatureStoreError(
                'Feature was changed by another reviewer; reload before editing it',
                HTTPStatus.CONFLICT,
            )

        center = connection.execute(
            '''
SELECT
    ST_Y(ST_PointOnSurface(geometry)) AS latitude,
    ST_X(ST_PointOnSurface(geometry)) AS longitude
FROM features
WHERE project_id = %s AND layer_id = %s AND id = %s
''',
            [project_id, layer_id, feature_id],
        ).fetchone()
        resolutions = connection.execute(
            '''
SELECT resolution
FROM feature_h3
WHERE project_id = %s AND layer_id = %s AND feature_id = %s
ORDER BY resolution
''',
            [project_id, layer_id, feature_id],
        ).fetchall()
        h3_cells = {
            str(row['resolution']): h3.latlng_to_cell(
                center['latitude'],
                center['longitude'],
                row['resolution'],
            )
            for row in resolutions
        }
        if h3_cells:
            with connection.cursor() as cursor:
                cursor.executemany(
                    '''
UPDATE feature_h3
SET h3_index = %s
WHERE project_id = %s
  AND layer_id = %s
  AND feature_id = %s
  AND resolution = %s
''',
                    [
                        [cell, project_id, layer_id, feature_id, int(resolution)]
                        for resolution, cell in h3_cells.items()
                    ],
                )
        connection.execute(
            '''
UPDATE projects
SET revision = revision + 1, updated_at = now()
WHERE id = %s
''',
            [project_id],
        )

    return {
        'type': 'Feature',
        'id': updated['id'],
        'layer_id': updated['layer_id'],
        'geometry': updated['geometry'],
        'properties': updated['properties'],
        'version': updated['version'],
        'updated_by': updated['updated_by'],
        'updated_at': updated['updated_at'].isoformat(),
        'h3': h3_cells,
    }
