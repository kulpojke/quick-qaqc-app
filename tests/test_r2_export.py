'''*!*! Exercise publication failures and retention with an in-memory S3 service.'''

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from src.api.export import ExportResult, export_project
from src.api.r2_export import check_destination, create_client, project_prefix, publish_batch, publish_locked
from src.project_config import ExportConfig


class MemoryS3:
    '''*!*! Store actual uploaded bytes and inject upload or readback failures.'''

    def __init__(self, events):
        '''*!*! Initialize isolated objects and a shared order-of-operations log.'''

        self.objects = {}
        self.events = events
        self.fail_upload = ''
        self.corrupt = ''
        self.fail_delete = False

    def upload_file(self, filename, bucket, key, **kwargs):
        '''*!*! Simulate uploading the staged file to a named object.'''

        self.events.append(('upload', key))
        if self.fail_upload and self.fail_upload in key:
            raise RuntimeError('Upload failed')
        self.objects[key] = Path(filename).read_bytes()

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        '''*!*! Publish a manifest atomically in the in-memory service.'''

        self.events.append(('manifest', Key))
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        '''*!*! Return a real streaming body so readback checks exercise bytes.'''

        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        self.events.append(('read', Key))
        data = self.objects[Key]
        if self.corrupt and self.corrupt in Key:
            data += b'corruption'
        return {'Body': StreamingBody(io.BytesIO(data), len(data))}

    def delete_object(self, *, Bucket, Key):
        '''*!*! Delete idempotently, or simulate an interrupted retention pass.'''

        self.events.append(('delete', Key))
        if self.fail_delete:
            raise RuntimeError('Delete failed')
        self.objects.pop(Key, None)


class MemoryDatabase:
    '''*!*! Record durable publication updates and transaction ordering.'''

    def __init__(self, events):
        '''*!*! Share the object service's event log for sequencing assertions.'''

        self.events = events

    @contextmanager
    def transaction(self):
        '''*!*! Record transaction completion before any object deletion.'''

        yield
        self.events.append(('commit', None))

    def execute(self, sql, params):
        '''*!*! Capture publication rows for feature-only bookkeeping assertions.'''

        self.events.append(('database', params))


class R2ExportTests(unittest.TestCase):
    '''*!*! Verify that failures cannot publish unverified snapshots or prune inputs.'''

    def setUp(self):
        '''*!*! Create a project destination, staging files, and fake services.'''

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = SimpleNamespace(
            project_id='camp', layers=(),
            exports=ExportConfig('source-bucket', 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com'),
        )
        self.events = []
        self.client = MemoryS3(self.events)
        self.database = MemoryDatabase(self.events)
        self.root = project_prefix(self.config.exports, 'camp')
        self.manifest_key = self.root + 'latest.json'

    def result(self, revision, stream='buildings'):
        '''*!*! Create distinct snapshot contents for one revision and stream.'''

        path = self.directory / f'{stream}-{revision}.parquet'
        path.write_bytes(f'{stream} revision {revision}'.encode())
        return ExportResult('camp', stream, revision, 4, path)

    def publish(self, *results):
        '''*!*! Run the production publication path with the lock already held.'''

        publish_locked(self.client, self.database, self.config, results, keep_revisions=2)

    def manifest(self):
        '''*!*! Inspect the consumer-visible manifest after publication.'''

        return json.loads(self.client.objects[self.manifest_key])

    def test_publish_then_prune_retains_two_revisions_and_source_objects(self):
        '''*!*! Retention follows readback and committed state and ignores source data.'''

        self.client.objects['DINS.parquet'] = b'original'
        for revision in range(1, 4):
            self.publish(self.result(revision))
        entries = self.manifest()['streams']['buildings']
        self.assertEqual([entry['revision'] for entry in entries], [3, 2])
        self.assertEqual(self.client.objects['DINS.parquet'], b'original')
        self.assertEqual(len([key for key in self.client.objects if key.endswith('.parquet')]), 3)
        first_delete = next(i for i, event in enumerate(self.events) if event[0] == 'delete')
        self.assertEqual(self.events[first_delete - 1][0], 'commit')
        for entry in entries:
            self.assertEqual(hashlib.sha256(self.client.objects[entry['key']]).hexdigest(), entry['sha256'])

    def test_annotation_only_batch_preserves_feature_references(self):
        '''*!*! An annotation batch carries forward the last feature snapshot.'''

        self.publish(self.result(1), self.result(1, 'annotations'))
        features = self.manifest()['streams']['buildings']
        self.events.clear()
        self.publish(self.result(2, 'annotations'))
        self.assertEqual(self.manifest()['streams']['buildings'], features)
        self.assertFalse(any(event[0] == 'database' for event in self.events))
        self.assertEqual(len([event for event in self.events if event[0] == 'upload']), 1)

    def test_timestamp_names_keep_checksums_and_replace_same_revision(self):
        '''*!*! Readable UTC names preserve verification and same-revision cleanup.'''

        self.publish(self.result(1), self.result(1, 'annotations'))
        streams = self.manifest()['streams']
        original = streams['buildings'][0]
        self.assertRegex(
            original['key'],
            r'/revision-1-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.parquet$',
        )
        self.assertNotIn(original['sha256'], original['key'])
        self.assertEqual(original['timestamp'], streams['annotations'][0]['timestamp'])
        replacement = self.result(1)
        replacement.path.write_bytes(b'updated snapshot bytes')
        self.publish(replacement)
        current = self.manifest()['streams']['buildings'][0]
        self.assertNotEqual(current['key'], original['key'])
        self.assertEqual(current['sha256'], hashlib.sha256(b'updated snapshot bytes').hexdigest())
        self.assertNotIn(original['key'], self.client.objects)

    def test_legacy_hash_names_remain_readable_and_prunable(self):
        '''*!*! Existing hash-named exports migrate through normal retention.'''

        self.publish(self.result(1))
        manifest = self.manifest()
        entry = manifest['streams']['buildings'][0]
        original_key = entry['key']
        entry.pop('timestamp')
        legacy_key = self.root + f'buildings/revision-1-{entry["sha256"]}.parquet'
        entry['key'] = legacy_key
        self.client.objects[legacy_key] = self.client.objects.pop(original_key)
        self.client.objects[self.manifest_key] = json.dumps(manifest).encode()
        self.publish(self.result(2))
        self.assertIn(legacy_key, self.client.objects)
        self.publish(self.result(3))
        self.assertNotIn(legacy_key, self.client.objects)

    def test_failed_upload_keeps_previous_manifest_and_retention(self):
        '''*!*! A partial batch never changes the consumer-visible publication.'''

        self.publish(self.result(1))
        previous = self.client.objects[self.manifest_key]
        self.events.clear()
        self.client.fail_upload = '/annotations/'
        with self.assertRaisesRegex(RuntimeError, 'Upload failed'):
            self.publish(self.result(2), self.result(2, 'annotations'))
        self.assertEqual(self.client.objects[self.manifest_key], previous)
        self.assertFalse(any(event[0] in {'database', 'delete', 'manifest'} for event in self.events))
        self.client.fail_upload = ''
        self.publish(self.result(2), self.result(2, 'annotations'))
        self.assertEqual(self.manifest()['revision'], 2)

    def test_corrupt_readback_cannot_publish_or_prune(self):
        '''*!*! Verification checks remote bytes rather than trusting uploaded metadata.'''

        self.client.corrupt = '.parquet'
        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
            self.publish(self.result(1))
        self.assertNotIn(self.manifest_key, self.client.objects)
        self.assertFalse(any(event[0] in {'database', 'delete'} for event in self.events))

    def test_interrupted_prune_is_retried(self):
        '''*!*! Pending deletions survive a failed cleanup and disappear after retry.'''

        self.publish(self.result(1))
        oldest = self.manifest()['streams']['buildings'][0]['key']
        self.publish(self.result(2))
        self.client.fail_delete = True
        with self.assertRaisesRegex(RuntimeError, 'Delete failed'):
            self.publish(self.result(3))
        self.assertIn(oldest, self.manifest()['prune'])
        self.client.fail_delete = False
        self.publish(self.result(3))
        self.assertNotIn(oldest, self.client.objects)
        self.assertEqual(self.manifest()['prune'], [])

    def test_stale_worker_cannot_replace_newer_publication(self):
        '''*!*! A late older snapshot cannot move the manifest revision backwards.'''

        self.publish(self.result(3))
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, 'newer snapshot'):
            self.publish(self.result(2))
        self.assertFalse(any(event[0] in {'upload', 'manifest', 'delete'} for event in self.events))

    def test_manifest_cannot_request_deletion_outside_generated_namespace(self):
        '''*!*! A malformed manifest cannot cause source-object deletion.'''

        self.publish(self.result(1))
        manifest = self.manifest()
        manifest['prune'] = ['DINS.parquet']
        self.client.objects[self.manifest_key] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(RuntimeError, 'Invalid R2 export manifest'):
            self.publish(self.result(2))
        self.assertFalse(any(event[0] == 'delete' for event in self.events))

    def test_generated_snapshot_used_as_source_is_not_pruned(self):
        '''*!*! A generated file later configured as an immutable source stays intact.'''

        self.publish(self.result(1))
        source = self.manifest()['streams']['buildings'][0]['key']
        self.config.layers = (SimpleNamespace(source=f'https://public.r2.dev/{source}'),)
        self.publish(self.result(2))
        self.publish(self.result(3))
        self.assertIn(source, self.client.objects)

    @patch.dict('os.environ', {}, clear=True)
    def test_missing_credentials_fail_clearly_without_network(self):
        '''*!*! R2 configuration cannot silently fall back to local-only export.'''

        with self.assertRaisesRegex(RuntimeError, 'R2_ACCESS_KEY_ID'):
            create_client(self.config.exports)

    @patch('src.api.r2_export.create_client')
    def test_local_configuration_does_not_create_r2_client(self, client):
        '''*!*! Existing YAML without exports remains local-only.'''

        self.config.exports = None
        publish_batch(self.config, (self.result(1),))
        client.assert_not_called()

    @patch.dict('os.environ', {'R2_ACCESS_KEY_ID': 'a-remote-name', 'R2_SECRET_ACCESS_KEY': 'secret'})
    def test_access_key_name_is_rejected_before_network(self):
        '''*!*! Diagnose a pasted token name without leaking either credential.'''

        with self.assertRaisesRegex(RuntimeError, '32-character S3 access key ID'):
            create_client(self.config.exports)

    @patch('src.api.r2_export.create_client')
    def test_preflight_checks_yaml_bucket(self, client):
        '''*!*! Preflight uses the configured bucket rather than a global destination.'''

        check_destination(self.config)
        client.return_value.head_bucket.assert_called_once_with(Bucket='source-bucket')

    @patch('src.api.r2_export.create_client')
    @patch('src.api.r2_export.database_url', return_value='test')
    @patch('src.api.r2_export.psycopg.connect')
    @patch('src.api.r2_export.publish_locked', side_effect=RuntimeError('Upload failed'))
    def test_failed_publication_releases_database_lock(self, publish, connect, url, client):
        '''*!*! A failing uploader does not leave another worker blocked on its lock.'''

        connection = connect.return_value.__enter__.return_value
        with self.assertRaisesRegex(RuntimeError, 'Upload failed'):
            publish_batch(self.config, (self.result(1),))
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn('pg_advisory_lock', statements[0])
        self.assertIn('pg_advisory_unlock', statements[-1])

    @patch('src.api.export.database_url', return_value='test')
    @patch('src.api.export.psycopg.connect')
    @patch('src.api.export._project_revision', return_value=1)
    @patch('src.api.export.write_layer_sequence', return_value=4)
    @patch('src.api.export.write_sequence_geoparquet')
    @patch('src.api.export.prune_old_snapshots')
    @patch('src.api.export.publish_batch', side_effect=RuntimeError('Upload failed'))
    def test_failed_publication_prevents_local_retention(self, publish, prune, *unused):
        '''*!*! Export integration retains staging history when R2 publication fails.'''

        self.config.layers = (SimpleNamespace(id='buildings', source=Path('/source.parquet')),)
        with self.assertRaisesRegex(RuntimeError, 'Upload failed'):
            export_project(self.config, self.directory, include_annotations=False)
        prune.assert_not_called()
        publish.assert_called_once()
