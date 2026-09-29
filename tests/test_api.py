from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.auth import (
    AuthenticationError,
    authenticated_reviewer,
    resolve_reviewer_identity,
)
from src.api.feature_store import FeatureStoreError, _point_coordinates
from src.api.main import geojson_feature
from src.api.migrate import migration_checksum
from src.api.models import AnnotationSubmission, FeaturePatch


class ApiModelTests(unittest.TestCase):
    '''*!*! Tests request validation and response shaping without a database.'''

    def test_feature_patch_requires_a_change(self):
        '''*!*! Version-only feature patches are rejected.'''

        with self.assertRaises(ValidationError):
            FeaturePatch(expected_version=1)

    def test_feature_patch_accepts_point_geometry(self):
        '''*!*! Point movement can submit a point geometry update.'''

        patch = FeaturePatch(
            expected_version=1,
            geometry={'type': 'Point', 'coordinates': [0, 0]},
        )

        self.assertEqual(patch.geometry['type'], 'Point')

    def test_feature_patch_rejects_unsupported_geometry_type(self):
        '''*!*! Feature edits reject geometry types outside configured layers.'''

        with self.assertRaises(ValidationError):
            FeaturePatch(
                expected_version=1,
                geometry={'type': 'LineString', 'coordinates': [[0, 0], [1, 1]]},
            )

    def test_browser_point_coordinates_require_valid_wgs84(self):
        '''*!*! Browser point placement accepts finite EPSG:4326 coordinates.'''

        self.assertEqual(
            _point_coordinates({'type': 'Point', 'coordinates': [-121, 39]}),
            (-121.0, 39.0),
        )
        with self.assertRaises(FeatureStoreError):
            _point_coordinates({'type': 'Point', 'coordinates': [-181, 39]})

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
            'layer_id': 'buildings',
            'geometry': {'type': 'Polygon', 'coordinates': []},
            'properties': {'h3_r8': '8828308281fffff'},
            'version': 4,
            'updated_by': 'alice',
            'updated_at': datetime(2026, 9, 22, tzinfo=timezone.utc),
        }

        feature = geojson_feature(row)

        self.assertEqual(feature['version'], 4)
        self.assertEqual(feature['layer_id'], 'buildings')
        self.assertNotIn('version', feature['properties'])


class AuthenticationTests(unittest.TestCase):
    '''*!*! Test development identity and Cloudflare Access validation.'''

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

    def test_accepts_configured_development_fallback(self):
        '''*!*! A local browser can use the configured development identity.'''

        with patch.dict(
            os.environ,
            {'AUTH_MODE': 'development', 'DEV_REVIEWER_ID': 'pazazu'},
            clear=True,
        ):
            reviewer_id = resolve_reviewer_identity({})

        self.assertEqual(reviewer_id, 'pazazu')

    def test_validates_cloudflare_identity_from_signed_claims(self):
        '''*!*! Cloudflare mode uses the validated JWT email as reviewer ID.'''

        environment = {
            'AUTH_MODE': 'cloudflare',
            'CF_ACCESS_TEAM_DOMAIN': 'https://team.cloudflareaccess.com',
            'CF_ACCESS_AUD': 'audience-tag',
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                'src.api.auth.decode_cloudflare_access_token',
                return_value={'email': 'alice+field@example.com'},
            ) as decode,
        ):
            reviewer_id = resolve_reviewer_identity({
                'Cf-Access-Jwt-Assertion': 'signed-token',
            })

        self.assertEqual(reviewer_id, 'alice+field@example.com')
        decode.assert_called_once_with(
            'signed-token',
            'https://team.cloudflareaccess.com',
            'audience-tag',
        )

    def test_cloudflare_mode_rejects_missing_configuration(self):
        '''*!*! Production auth fails closed without Access issuer settings.'''

        with patch.dict(os.environ, {'AUTH_MODE': 'cloudflare'}, clear=True):
            with self.assertRaises(AuthenticationError) as raised:
                resolve_reviewer_identity({'Cf-Access-Jwt-Assertion': 'token'})

        self.assertEqual(raised.exception.status, 503)

    def test_unknown_auth_mode_fails_closed(self):
        '''*!*! Unknown authentication modes cannot silently trust a header.'''

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
