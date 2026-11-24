#!/usr/bin/env python3
'''*!*! Back up one QAQC instance's PostGIS database.

Runs on the droplet itself, with the host's Python (not in a container),
from cron. Dumps to a temporary file, checks that the dump reads to the end,
gives it its final name, then uploads it to R2 and reads it back to check.
After a good upload, only the newest few dumps are kept on the droplet;
R2 deletes its copies itself, after 30 days (a lifecycle rule on the bucket).
'''

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import subprocess
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


# *!*! The settings file holds these four KEY=VALUE lines (see read_settings).
REQUIRED_SETTINGS = ('R2_ENDPOINT', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')


def log(message: str) -> None:
    '''*!*! Print one line with the UTC time, for the cron log.'''

    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'{stamp}  {message}', flush=True)


def find_db_container(compose_project: str) -> str:
    '''*!*! Return the ID of the one running db container of a Compose project.'''

    result = subprocess.run(
        [
            'docker', 'ps', '--quiet',
            '--filter', f'label=com.docker.compose.project={compose_project}',
            '--filter', 'label=com.docker.compose.service=db',
        ],
        capture_output=True, text=True, check=True,
    )
    ids = result.stdout.split()
    if len(ids) != 1:
        raise RuntimeError(
            f'expected 1 running db container in {compose_project!r}, found {len(ids)}'
        )
    return ids[0]


def dump_database(container: str, path: Path) -> None:
    '''*!*! Write a custom-format pg_dump of the container's database to path.'''

    # *!*! Single quotes: $POSTGRES_USER and $POSTGRES_DB are filled in by the
    # *!*! container's own shell, from its own settings. No password is needed
    # *!*! over the container's local socket. No --tty: it can damage binary output.
    command = [
        'docker', 'exec', container, 'sh', '-c',
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
    ]
    with path.open('wb') as output:
        subprocess.run(command, stdout=output, check=True)


def check_dump(container: str, path: Path) -> None:
    '''*!*! Read the whole dump with pg_restore; a cut-off dump fails here.'''

    # *!*! pg_restore turns the dump back into SQL and throws that away
    # *!*! (/dev/null). It has to read every byte to do so, so a file that
    # *!*! ends early gives "could not read from input file: end of file".
    with path.open('rb') as dump:
        subprocess.run(
            ['docker', 'exec', '--interactive', container, 'pg_restore', '--file=/dev/null'],
            stdin=dump, check=True,
        )


def read_settings(path: Path) -> dict[str, str]:
    '''*!*! Read KEY=VALUE lines from a settings file that only its owner can read.'''

    # *!*! Like ssh with its keys: refuse a secrets file others could read.
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f'{path} must be readable by its owner only: chmod 600 {path}')
    settings = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, _, value = line.partition('=')
        settings[name.strip()] = value.strip()
    missing = [name for name in REQUIRED_SETTINGS if not settings.get(name)]
    if missing:
        raise RuntimeError(f'{path} is missing ' + ', '.join(missing))
    return settings


def r2_client(settings: dict[str, str]):
    '''*!*! Connect to R2's S3 API the way src/api/r2_export.py does.'''

    options = {
        'connect_timeout': 10,
        'read_timeout': 60,
        'retries': {'mode': 'standard', 'max_attempts': 4},
    }
    try:
        # *!*! boto3 1.36 and newer add checksums by default that R2 can reject,
        # *!*! so the export turns them off. The droplet's own boto3 (1.34) is
        # *!*! older: it doesn't add them and doesn't know these two settings.
        config = Config(
            **options,
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        )
    except TypeError:
        config = Config(**options)
    return boto3.client(
        's3',
        endpoint_url=settings['R2_ENDPOINT'],
        region_name='auto',
        aws_access_key_id=settings['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=settings['R2_SECRET_ACCESS_KEY'],
        config=config,
    )


def file_sha256(path: Path) -> str:
    '''*!*! Return the SHA-256 of a file's bytes, as hex.'''

    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def upload_and_verify(client, bucket: str, path: Path, key: str) -> None:
    '''*!*! Upload a file, then download it again and compare every byte's checksum.'''

    digest = file_sha256(path)
    client.upload_file(str(path), bucket, key)
    # *!*! Read back the real bytes, as the export does: R2's ETag for a large
    # *!*! (multipart) upload is not the file's checksum, so it can't be compared.
    body = client.get_object(Bucket=bucket, Key=key)['Body']
    checksum = hashlib.sha256()
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            checksum.update(chunk)
    finally:
        body.close()
    if checksum.hexdigest() != digest:
        raise RuntimeError(f'R2 copy of {key} differs from {path}')


def prune_local(folder: Path, instance: str, keep: int) -> list[Path]:
    '''*!*! Delete all but the newest `keep` dumps, and leftover .partial files.'''

    # *!*! The names hold the UTC time in a fixed width, so sorting by name
    # *!*! sorts by age. Each instance has its own folder, so nothing else matches.
    dumps = sorted(folder.glob(f'{instance}_*.dump'))
    old = dumps[:-keep]
    # *!*! This run's .partial already has its final name, so any .partial left
    # *!*! is from an earlier run that broke off. (cron runs this under flock,
    # *!*! so two runs never overlap.)
    leftovers = sorted(folder.glob(f'{instance}_*.dump.partial'))
    for path in old + leftovers:
        path.unlink()
    return old + leftovers


def main() -> int:
    '''*!*! Dump, check and name one backup; return the exit code for cron.'''

    parser = argparse.ArgumentParser(description='Back up one QAQC instance database.')
    parser.add_argument(
        '--instance', required=True,
        help='short name for this instance, used in file names and the R2 path, e.g. camp',
    )
    parser.add_argument(
        '--compose-project', default='damagemap-qaqc',
        help='Compose project whose db container is dumped (default: %(default)s)',
    )
    parser.add_argument(
        '--backup-dir', type=Path, default=Path('/var/backups/damagemap'),
        help='local folder; each instance gets a subfolder (default: %(default)s)',
    )
    parser.add_argument(
        '--settings', type=Path, default=Path('/etc/damagemap-backup.env'),
        help='root-only file with the R2 endpoint, bucket and key (default: %(default)s)',
    )
    parser.add_argument(
        '--keep-local', type=int, default=3,
        help='dumps to keep on the droplet after a good upload (default: %(default)s)',
    )
    args = parser.parse_args()
    if args.keep_local < 1:
        parser.error('--keep-local must be at least 1: the newest dump always stays')

    # *!*! Read the settings before dumping, so a missing key fails at once.
    try:
        settings = read_settings(args.settings)
    except (RuntimeError, OSError) as error:
        log(f'FAILED: {error}')
        return 1

    folder = args.backup_dir / args.instance
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = folder / f'{args.instance}_{stamp}.dump'
    temporary = final.with_name(final.name + '.partial')

    try:
        container = find_db_container(args.compose_project)
        log(f'dumping {args.compose_project} (container {container[:12]}) to {temporary}')
        dump_database(container, temporary)
        check_dump(container, temporary)
        # *!*! Only a checked dump gets the final name, so a .dump file is
        # *!*! always a whole one. rename is atomic within one folder.
        temporary.rename(final)
    except (subprocess.CalledProcessError, RuntimeError, OSError) as error:
        log(f'FAILED: {error}')
        temporary.unlink(missing_ok=True)
        return 1

    log(f'dump ok: {final} ({final.stat().st_size / 1e6:.1f} MB)')

    # *!*! A failed upload keeps the local dump: it is whole, only not off the droplet.
    bucket = settings['R2_BUCKET']
    key = f'{args.instance}/{final.name}'
    try:
        upload_and_verify(r2_client(settings), bucket, final, key)
    except (BotoCoreError, ClientError, RuntimeError, OSError) as error:
        log(f'FAILED upload to r2://{bucket}/{key}: {error}')
        return 1

    log(f'upload ok: r2://{bucket}/{key}')

    # *!*! Only after a good upload: on a failure nothing is deleted.
    try:
        removed = prune_local(folder, args.instance, args.keep_local)
    except OSError as error:
        log(f'FAILED to remove old local dumps: {error}')
        return 1
    if removed:
        log('removed local: ' + ', '.join(path.name for path in removed))
    return 0


if __name__ == '__main__':
    sys.exit(main())
