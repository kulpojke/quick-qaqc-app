from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.auth import authenticated_reviewer
from src.api.main import geojson_feature
from src.api.migrate import migration_checksum
from src.api.models import AnnotationSubmission, FeaturePatch


class ApiModelTests(unittest.TestCase):
    '''*!*! Tests request validation and response shaping without a database.'''

    def test_feature_patch_requires_a_change(self):
        '''*!*! Version-only feature patches are rejected.'''

        with self.assertRaises(ValidationError):
            FeaturePatch(expected_version=1)

    def test_feature_patch_restricts_geometry_type(self):
        '''*!*! Building edits accept polygonal GeoJSON only.'''

        with self.assertRaises(ValidationError):
            FeaturePatch(
                expected_version=1,
                geometry={'type': 'Point', 'coordinates': [0, 0]},
            )

    def test_annotation_submission_normalizes_defaults(self):
        '''*!*! Annotation requests carry a feature version and optional notes.'''

        submission = AnnotationSubmission(
            label='damaged',
            feature_version_seen=3,
        )

        self.assertEqual(submission.notes, '')
        self.assertEqual(submission.feature_version_seen, 3)

    def test_geojson_feature_keeps_version_outside_properties(self):
        '''*!*! API feature metadata does not alter source properties.'''

        row = {
            'id': 'building-one',
            'geometry': {'type': 'Polygon', 'coordinates': []},
            'properties': {'h3_r8': '8828308281fffff'},
            'version': 4,
            'updated_by': 'alice',
            'updated_at': datetime(2026, 9, 22, tzinfo=timezone.utc),
        }

        feature = geojson_feature(row)

        self.assertEqual(feature['version'], 4)
        self.assertNotIn('version', feature['properties'])


class DevelopmentAuthTests(unittest.TestCase):
    '''*!*! Tests temporary header authentication used by the local stack.'''

    def test_accepts_a_valid_development_reviewer(self):
        '''*!*! Development auth derives identity from its dedicated header.'''

        with patch.dict(os.environ, {'AUTH_MODE': 'development'}):
            reviewer_id = authenticated_reviewer(None, 'alice@example.com')

        self.assertEqual(reviewer_id, 'alice@example.com')

    def test_rejects_missing_reviewer(self):
        '''*!*! Anonymous development requests are rejected.'''

        with patch.dict(os.environ, {'AUTH_MODE': 'development'}):
            with self.assertRaises(HTTPException) as raised:
                authenticated_reviewer(None, None)

        self.assertEqual(raised.exception.status_code, 401)

    def test_non_development_auth_fails_closed(self):
        '''*!*! Unimplemented production auth cannot silently trust a header.'''

        with patch.dict(os.environ, {'AUTH_MODE': 'oidc'}):
            with self.assertRaises(HTTPException) as raised:
                authenticated_reviewer(None, 'alice')

        self.assertEqual(raised.exception.status_code, 503)


class MigrationTests(unittest.TestCase):
    '''*!*! Tests migration history integrity helpers.'''

    def test_migration_checksum_is_stable(self):
        '''*!*! Identical migration text produces an identical checksum.'''

        self.assertEqual(migration_checksum('SELECT 1;'), migration_checksum('SELECT 1;'))
        self.assertNotEqual(migration_checksum('SELECT 1;'), migration_checksum('SELECT 2;'))


if __name__ == '__main__':
    unittest.main()
