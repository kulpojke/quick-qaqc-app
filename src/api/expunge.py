'''*!*! Make one reviewer's labels stop counting, or count again.

Nothing is deleted: the labels stay in PostGIS, but building counts,
top-ups and the R2 exports skip them. On the server:

    PROJECT_CONFIG=camp_config_v2.yaml docker compose run --rm --no-deps \\
        bootstrap python -m src.api.expunge --yaml /project/camp_config_v2.yaml \\
        --reviewer someone@example.com --note "why"

Add --restore to undo. After expunging, remove the reviewer from the YAML and
run ./run.sh with --auto-assign-hex so their buildings go to others.
'''

from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from src.api.database import database_url
from src.project_config import load_review_config


def expunge_reviewer(project_id: str, reviewer_id: str, note: str = '',
                     restore: bool = False) -> int:
    '''*!*! Record or remove the expunge; return how many labels it affects.'''

    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        labels = connection.execute(
            'SELECT count(*) AS count FROM annotations WHERE project_id = %s AND reviewer_id = %s',
            [project_id, reviewer_id],
        ).fetchone()['count']
        if restore:
            connection.execute(
                'DELETE FROM expunged_reviewers WHERE project_id = %s AND reviewer_id = %s',
                [project_id, reviewer_id],
            )
        else:
            connection.execute(
                '''
INSERT INTO expunged_reviewers (project_id, reviewer_id, note)
VALUES (%s, %s, %s)
ON CONFLICT (project_id, reviewer_id) DO UPDATE SET note = EXCLUDED.note
''',
                [project_id, reviewer_id, note],
            )
        # *!*! The exported annotations change, so publish a new revision.
        connection.execute(
            'UPDATE projects SET revision = revision + 1, updated_at = now() WHERE id = %s',
            [project_id],
        )
    return labels


def main() -> None:
    '''*!*! Command-line entry point.'''

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0].strip("'*! "))
    parser.add_argument('--yaml', required=True, type=Path, help='Project YAML (for the project ID)')
    parser.add_argument('--reviewer', required=True, help='Reviewer ID (email)')
    parser.add_argument('--note', default='', help='Why, for the record')
    parser.add_argument('--restore', action='store_true', help='Make their labels count again')
    args = parser.parse_args()
    config = load_review_config(args.yaml)
    labels = expunge_reviewer(config.project_id, args.reviewer, args.note, args.restore)
    if args.restore:
        print(f'{labels} label(s) by {args.reviewer} count again in {config.project_id}.')
    else:
        print(f'{labels} label(s) by {args.reviewer} no longer count in {config.project_id}.')
        print('Next: remove them from the YAML, then ./run.sh ... --auto-assign-hex ...')


if __name__ == '__main__':
    main()
