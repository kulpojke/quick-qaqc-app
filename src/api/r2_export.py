'''*!*! Publish verified R2 snapshots and a manifest before pruning old exports.'''

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlparse

import psycopg

from src.api.database import database_url
from src.project_config import ExportConfig, ReviewConfig

if TYPE_CHECKING:
    from src.api.export import ExportResult


def create_client(destination: ExportConfig):
    '''*!*! Use explicit R2 credentials, bounded retries, and the S3 API endpoint.'''

    import boto3
    from botocore.config import Config

    access_key = os.environ.get('R2_ACCESS_KEY_ID', '').strip()
    secret_key = os.environ.get('R2_SECRET_ACCESS_KEY', '').strip()
    if not access_key or not secret_key:
        raise RuntimeError('R2 exports require R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY')
    return boto3.client(
        's3',
        endpoint_url=destination.endpoint_url,
        region_name='auto',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={'mode': 'standard', 'max_attempts': 4},
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        ),
    )


def project_prefix(destination: ExportConfig, project_id: str) -> str:
    '''*!*! Keep generated objects in a project-specific namespace beneath the prefix.'''

    project = quote(project_id, safe='').replace('.', '%2E')
    return f'{destination.prefix}/_damagemap_exports/{project}/'


def check_destination(config: ReviewConfig) -> None:
    '''*!*! Fail deployment preflight if the configured R2 bucket is inaccessible.'''

    if config.exports is not None:
        client = create_client(config.exports)
        client.head_bucket(Bucket=config.exports.bucket)


def file_digest(path: Path) -> str:
    '''*!*! Hash staged bytes without loading an entire GeoParquet into memory.'''

    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_object(client, bucket: str, key: str, digest: str, size: int) -> None:
    '''*!*! Read back the object and verify actual bytes, not multipart ETags.'''

    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    checksum = hashlib.sha256()
    count = 0
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            checksum.update(chunk)
            count += len(chunk)
    finally:
        body.close()
    if count != size or checksum.hexdigest() != digest:
        raise RuntimeError(f'R2 verification failed for {key}')


def read_manifest(client, bucket: str, key: str, project_id: str, root: str) -> dict:
    '''*!*! Load publication history, accepting only this exporter's exact key format.'''

    from botocore.exceptions import ClientError

    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') == 'NoSuchKey':
            return {'version': 1, 'project_id': project_id, 'revision': -1, 'streams': {}, 'prune': []}
        raise
    body = response['Body']
    try:
        manifest = json.loads(body.read())
    finally:
        body.close()
    pattern = re.compile(
        rf'{re.escape(root)}[A-Za-z0-9_-]+/revision-[0-9]+-[a-f0-9]{{64}}\.parquet'
    )
    try:
        if (
            not isinstance(manifest, dict)
            or manifest['version'] != 1 or manifest['project_id'] != project_id
            or not isinstance(manifest['revision'], int)
            or not isinstance(manifest['streams'], dict)
            or not isinstance(manifest['prune'], list)
        ):
            raise ValueError('Invalid manifest header')
        for stream, entries in manifest['streams'].items():
            if (
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', stream)
                or not isinstance(entries, list) or not entries
            ):
                raise ValueError('Invalid stream')
            for entry in entries:
                expected = f'{root}{stream}/revision-{entry["revision"]}-{entry["sha256"]}.parquet'
                if (
                    entry['key'] != expected or not pattern.fullmatch(entry['key'])
                    or not isinstance(entry['revision'], int)
                    or not isinstance(entry['row_count'], int)
                    or not isinstance(entry['size'], int)
                ):
                    raise ValueError('Invalid snapshot')
        if any(not isinstance(key, str) or not pattern.fullmatch(key) for key in manifest['prune']):
            raise ValueError('Invalid prune key')
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('Invalid R2 export manifest; refusing to publish or prune') from error
    return manifest


def publish_batch(
    config: ReviewConfig,
    results: tuple[ExportResult, ...],
    *,
    keep_revisions: int = 2,
) -> None:
    '''*!*! Serialize publication against other workers sharing this database.'''

    if config.exports is None or not results:
        return
    if keep_revisions < 1:
        raise ValueError('keep_revisions must be at least one')
    destination = config.exports
    client = create_client(destination)
    root = project_prefix(destination, config.project_id)
    lock_name = f'r2-export:{destination.endpoint_url}:{destination.bucket}:{root}'
    with psycopg.connect(database_url(), autocommit=True) as connection:
        connection.execute('SELECT pg_advisory_lock(hashtextextended(%s, 0))', [lock_name])
        try:
            publish_locked(client, connection, config, results, keep_revisions=keep_revisions)
        finally:
            connection.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', [lock_name])


def publish_locked(
    client,
    connection,
    config: ReviewConfig,
    results: tuple[ExportResult, ...],
    *,
    keep_revisions: int,
) -> None:
    '''*!*! Upload, verify, publish, record feature state, then prune generated keys.'''

    destination = config.exports
    root = project_prefix(destination, config.project_id)
    manifest_key = f'{root}latest.json'
    manifest = read_manifest(client, destination.bucket, manifest_key, config.project_id, root)
    revision = results[0].revision
    if any(result.project_id != config.project_id or result.revision != revision for result in results):
        raise ValueError('An R2 batch must belong to one project snapshot')
    if revision < manifest['revision']:
        raise RuntimeError('A newer snapshot is already published; retry with a fresh database export')

    # *!*! Guard configured remote inputs even if someone selects a generated
    # *!*! snapshot as a source later. Never overwrite or prune those keys.
    source_paths = {
        unquote(urlparse(str(layer.source)).path).lstrip('/')
        for layer in config.layers if isinstance(layer.source, str)
    }
    def protected(key: str) -> bool:
        '''*!*! Match source object paths without relying on their public hostnames.'''

        return any(path == unquote(key) or path.endswith('/' + unquote(key)) for path in source_paths)
    pending = set(manifest['prune'])
    for result in results:
        digest = file_digest(result.path)
        size = result.path.stat().st_size
        key = f'{root}{result.stream_id}/revision-{revision}-{digest}.parquet'
        if protected(key):
            raise RuntimeError('R2 export key overlaps a configured source')
        client.upload_file(
            str(result.path), destination.bucket, key,
            ExtraArgs={'ContentType': 'application/vnd.apache.parquet', 'Metadata': {'sha256': digest}},
        )
        verify_object(client, destination.bucket, key, digest, size)
        entry = {'key': key, 'revision': revision, 'sha256': digest, 'size': size, 'row_count': result.row_count}
        history = manifest['streams'].get(result.stream_id, [])
        # *!*! A restart may regenerate equivalent bytes at the same database
        # *!*! revision. Keep one output per revision, with content-addressed keys.
        replaced = [item for item in history if item['revision'] == revision and item['key'] != key]
        history = [entry] + [item for item in history if item['revision'] != revision]
        history.sort(key=lambda item: item['revision'], reverse=True)
        pending.update(item['key'] for item in replaced + history[keep_revisions:])
        manifest['streams'][result.stream_id] = history[:keep_revisions]

    retained = {entry['key'] for history in manifest['streams'].values() for entry in history}
    manifest['revision'] = revision
    manifest['prune'] = sorted(key for key in pending - retained if not protected(key))
    payload = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    client.put_object(
        Bucket=destination.bucket, Key=manifest_key, Body=payload,
        ContentType='application/json', CacheControl='no-store',
    )
    verify_object(client, destination.bucket, manifest_key, hashlib.sha256(payload).hexdigest(), len(payload))

    # *!*! Only durable verified objects advance the existing feature-layer
    # *!*! publication table. Annotation publication lives in the manifest.
    with connection.transaction():
        for result in results:
            if result.stream_id == 'annotations':
                continue
            entry = manifest['streams'][result.stream_id][0]
            connection.execute(
                '''
INSERT INTO export_state (project_id, layer_id, exported_revision, object_key, exported_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (project_id, layer_id) DO UPDATE SET
    exported_revision = EXCLUDED.exported_revision,
    object_key = EXCLUDED.object_key,
    exported_at = EXCLUDED.exported_at
WHERE export_state.exported_revision <= EXCLUDED.exported_revision
''',
                [config.project_id, result.stream_id, revision, entry['key']],
            )
    # *!*! Keep the pending list in the manifest so interrupted deletion is
    # *!*! retried on the next publication. Deleting an absent key is harmless.
    for key in manifest['prune']:
        client.delete_object(Bucket=destination.bucket, Key=key)
    if manifest['prune']:
        manifest['prune'] = []
        payload = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        client.put_object(
            Bucket=destination.bucket, Key=manifest_key, Body=payload,
            ContentType='application/json', CacheControl='no-store',
        )
        verify_object(client, destination.bucket, manifest_key, hashlib.sha256(payload).hexdigest(), len(payload))
    print(f'Published revision {revision} to s3://{destination.bucket}/{manifest_key}', flush=True)
