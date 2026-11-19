'''*!*! Share buildings out to reviewers, and top them up while people work.

How it works, in short:

- Every building needs `N` counted labels (`--auto-assign-hex FRACTION N`).
  The first "unknown" label on a building does not count, so such a building
  ends up with N + 1 labels.
- A reviewer's list holds the buildings they may label: the ones they already
  labelled, the ones they drew, and the ones shared out to them.
- Hexes stay the unit people see and edit: a reviewer gets every hex that
  holds a building on their list, and sees all buildings in it.
- `--auto-assign-hex` shares out again: labelled and drawn buildings stay
  where they are; the rest is shared out hex by hex to the reviewers with the
  least open work, skipping anyone who already labelled the building and
  anyone at their `max_features` cap.
- While people work, a building that is drawn, or gets its first "unknown",
  is topped up at once (`top_up_feature`).
- Point layers (such as DINS) follow the hexes: a reviewer sees and moves
  the points in their building hexes, as everyone else with that hex does.
  A point assignment with its own `h3` list in the YAML keeps that list.
'''

from dataclasses import dataclass, asdict, replace
import hashlib
import json
import math

from psycopg.types.json import Jsonb


# *!*! The label that does not count the first time a building gets it.
UNKNOWN_LABEL = 'unknown'


@dataclass(frozen=True)
class AutoAssignOptions:
    '''*!*! Describe deterministic whole-hex selection and reviewers per building.'''

    fraction: float
    reviewers_per_hex: int = 1
    confidence_column: str | None = None
    confidence_limit: float | None = None
    resolution: int | None = None

    def __post_init__(self):
        '''*!*! Reject invalid selection options before reading or changing data.'''

        if not math.isfinite(self.fraction) or not 0 <= self.fraction <= 1:
            raise ValueError('Auto-assignment fraction must be between 0 and 1')
        if self.reviewers_per_hex < 1:
            raise ValueError('Reviewer overlap must be at least 1')
        if (self.confidence_column is None) != (self.confidence_limit is None):
            raise ValueError('Confidence selection requires a column and threshold')
        if self.confidence_limit is not None and not math.isfinite(self.confidence_limit):
            raise ValueError('Confidence threshold must be finite')
        if self.confidence_column is not None and not self.confidence_column.strip():
            raise ValueError('Confidence column must not be empty')
        if self.resolution is not None and not 0 <= self.resolution <= 15:
            raise ValueError('Hex resolution must be between 0 and 15')


def add_assignment_arguments(parser):
    '''*!*! Share bootstrap flags with the deployment preflight validator.'''

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--auto-assign-hex', nargs='+', metavar='VALUE',
                       help='Fraction of buildings and optional reviewers per building (default 1)')
    group.add_argument('--manual-assign-hex', action='store_true',
                       help='Remove saved auto assignments and restore YAML assignments')
    parser.add_argument('--conf', nargs=2, metavar=('COLUMN', 'LIMIT'))
    parser.add_argument('--hex-resolution', type=int)


def assignment_options(args):
    '''*!*! Parse the optional overlap and confidence override consistently.'''

    if args.auto_assign_hex is None:
        if args.conf is not None or args.hex_resolution is not None:
            raise ValueError('--conf and --hex-resolution require --auto-assign-hex')
        return None
    if len(args.auto_assign_hex) not in (1, 2):
        raise ValueError('--auto-assign-hex requires FRACTION and optionally N')
    return AutoAssignOptions(
        fraction=float(args.auto_assign_hex[0]),
        reviewers_per_hex=int(args.auto_assign_hex[1]) if len(args.auto_assign_hex) == 2 else 1,
        confidence_column=args.conf[0] if args.conf else None,
        confidence_limit=float(args.conf[1]) if args.conf else None,
        resolution=args.hex_resolution,
    )


def building_layers(config):
    '''*!*! Leave point context layers and their manual assignments unchanged.'''

    return [layer for layer in config.layers
            if set(layer.geometry_types) <= {'Polygon', 'MultiPolygon'}]


def annotators(config, layer):
    '''*!*! Reviewers who label this layer, in YAML order, with their assignment.'''

    return [(reviewer.id, reviewer.assignment_for(layer.id, 'annotation'))
            for reviewer in config.reviewers
            if reviewer.assignment_for(layer.id, 'annotation') is not None]


def layer_modes(config, reviewer_id, layer):
    '''*!*! Every mode one reviewer has on one layer, across YAML entries.'''

    reviewer = config.reviewer(reviewer_id)
    return [mode for mode in layer.modes
            if reviewer is not None and reviewer.assignment_for(layer.id, mode) is not None]


def validate_options(config, options):
    '''*!*! Fail unsupported layers or overlap before importing a project.'''

    layers = building_layers(config)
    if not layers:
        raise ValueError('Auto assignment requires a polygon building layer')
    for layer in layers:
        if 'annotation' not in layer.modes:
            raise ValueError(f'Layer {layer.id}: auto assignment requires annotation mode')
        if options.reviewers_per_hex > len(annotators(config, layer)):
            raise ValueError(f'Layer {layer.id}: overlap exceeds eligible YAML reviewers')
        if options.resolution is not None and options.resolution not in layer.h3_resolutions:
            raise ValueError(f'Layer {layer.id}: hex resolution is not generated by this layer')
        resolution = options.resolution if options.resolution is not None else min(layer.h3_resolutions)
        for points in following_layers(config):
            if resolution not in points.h3_resolutions:
                raise ValueError(f'Layer {points.id}: its points follow the building hexes, '
                                 f'so it needs H3 r{resolution} too')


def config_signature(config, expunged=()):
    '''*!*! Detect changed eligibility without storing source URLs or credentials.'''

    identity = []
    for layer in building_layers(config):
        reviewers = []
        for reviewer in config.reviewers:
            assignments = [assignment for assignment in reviewer.assignments
                           if assignment.layer_id == layer.id]
            entry = [reviewer.id, [assignment.modes for assignment in assignments]]
            # *!*! Added only when set, so configs without caps keep the
            # *!*! signature saved by earlier versions.
            caps = [assignment.max_features for assignment in assignments
                    if assignment.max_features is not None]
            if caps:
                entry.append(caps)
            reviewers.append(entry)
        identity.append((layer.id, str(layer.source), layer.h3_resolutions, reviewers))
    if expunged:
        identity.append(['expunged', sorted(expunged)])
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def select_cells(rows, options, seed):
    '''*!*! Pick the whole hexes to share out, by fraction or by confidence.

    Returns building counts per hex and the set of selected hexes.
    '''

    counts = {}
    qualifying = set()
    column_found = False
    for row in rows:
        cell = row['h3_index']
        if not cell:
            raise ValueError('A building is missing its assignment-resolution H3 index')
        counts[cell] = counts.get(cell, 0) + 1
        if options.confidence_column is not None:
            properties = row['properties']
            column_found |= options.confidence_column in properties
            value = properties.get(options.confidence_column)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f'Confidence column {options.confidence_column!r} must be numeric') from error
            if isinstance(value, bool) or not math.isfinite(number):
                raise ValueError(f'Confidence column {options.confidence_column!r} must be finite numeric data')
            if number <= options.confidence_limit:
                qualifying.add(cell)
    if options.confidence_column is not None:
        if counts and not column_found:
            raise ValueError(f'Confidence column {options.confidence_column!r} is missing')
        return counts, qualifying
    target = sum(counts.values()) * options.fraction
    selected = set()
    total = 0
    ordered = sorted(counts, key=lambda cell: hashlib.sha256(f'{seed}:{cell}'.encode()).digest())
    for cell in ordered:
        if total >= target:
            break
        candidate = total + counts[cell]
        if candidate <= target or abs(candidate - target) <= abs(total - target):
            selected.add(cell)
            total = candidate
    if target > 0 and not selected and counts:
        selected.add(min(ordered, key=lambda cell: abs(counts[cell] - target)))
    return counts, selected


def counted_labels(labels):
    '''*!*! Count a building's labels toward N; the first "unknown" is free.

    `labels` maps reviewer ID to label, for reviewers whose work counts.
    '''

    count = len(labels)
    if UNKNOWN_LABEL in labels.values():
        count -= 1
    return count


def reviewers_still_needed(labels, holders, reviewers_needed):
    '''*!*! How many more reviewers a building needs on top of its current list.

    Holders who have not labelled it yet are already on their way, so they
    count as a label to come.
    '''

    waiting = sum(1 for reviewer in holders if reviewer not in labels)
    return max(0, reviewers_needed - counted_labels(labels) - waiting)


def allocate_feature_lists(buildings, reviewers, options, seed, max_features=None):
    '''*!*! Give every selected building the reviewers it still needs, hex by hex.

    `buildings` are dicts with `id`, `h3_index`, `properties`, `labels`
    (reviewer -> label, counted reviewers only) and `drawn_by`. `reviewers`
    are the eligible annotators in YAML order; `max_features` maps some of
    them to a cap. Returns (lists, short, selected, total): `lists` maps each
    reviewer to {feature_id: reason}; `short` counts reviewer slots no one
    could take, because of caps or too few reviewers.
    '''

    max_features = max_features or {}
    _, selected_cells = select_cells(buildings, options, seed)
    order = {reviewer: position for position, reviewer in enumerate(reviewers)}
    lists = {reviewer: {} for reviewer in reviewers}

    # *!*! 1. Work already done stays with its reviewer: what they labelled
    # *!*! (to see and fix it) and what they drew. Both count toward a cap.
    for building in buildings:
        for reviewer in building['labels']:
            if reviewer in lists:
                lists[reviewer][building['id']] = 'allocated'
        if building.get('drawn_by') in lists:
            lists[building['drawn_by']].setdefault(building['id'], 'drawn')
    labelled_by = {}
    for building in buildings:
        for reviewer in building['labels']:
            labelled_by.setdefault(reviewer, set()).add(building['id'])
    # *!*! Open work: buildings on a list that the reviewer has not labelled.
    open_work = {reviewer: len(set(lists[reviewer]) - labelled_by.get(reviewer, set()))
                 for reviewer in reviewers}

    def full(reviewer):
        cap = max_features.get(reviewer)
        return cap is not None and len(lists[reviewer]) >= cap

    # *!*! 2. Share out what is still needed, biggest hex first, so each
    # *!*! reviewer gets compact areas.
    needs = {}
    by_cell = {}
    for building in buildings:
        if building['h3_index'] not in selected_cells:
            continue
        holders = {reviewer for reviewer in reviewers if building['id'] in lists[reviewer]}
        needs[building['id']] = reviewers_still_needed(
            building['labels'], holders, options.reviewers_per_hex,
        )
        by_cell.setdefault(building['h3_index'], []).append(building)
    cell_need = {cell: sum(needs[building['id']] for building in members)
                 for cell, members in by_cell.items()}
    short = 0
    for cell in sorted(by_cell, key=lambda cell: (-cell_need[cell], cell)):
        # *!*! One ranking per hex keeps a hex with the same people.
        ranking = sorted(reviewers, key=lambda reviewer: (open_work[reviewer], order[reviewer]))
        for building in sorted(by_cell[cell], key=lambda building: building['id']):
            still_needed = needs[building['id']]
            for reviewer in ranking:
                if not still_needed:
                    break
                if (reviewer in building['labels'] or building['id'] in lists[reviewer]
                        or full(reviewer)):
                    continue
                lists[reviewer][building['id']] = 'allocated'
                open_work[reviewer] += 1
                still_needed -= 1
            short += still_needed
    selected = sum(len(members) for members in by_cell.values())
    return lists, short, selected, len(buildings)


def following_layers(config):
    '''*!*! Point layers whose hexes come from the building hexes.'''

    buildings = {layer.id for layer in building_layers(config)}
    return [layer for layer in config.layers if layer.id not in buildings]


def point_layer_cells(config, allocations):
    '''*!*! Each reviewer's building hexes, for every point layer.

    A reviewer with hexes on several building layers gets all of them. Hexes
    at a resolution the point layer does not generate are left out
    (`validate_options` refuses such a run).
    '''

    import h3

    buildings = {layer.id for layer in building_layers(config)}
    hexes = {}
    for layer_id, layer_allocation in allocations.items():
        if layer_id in buildings:
            for reviewer_id, cells in layer_allocation.items():
                hexes.setdefault(reviewer_id, set()).update(cells)
    return {
        layer.id: {
            reviewer_id: sorted(cell for cell in cells
                                if h3.is_valid_cell(cell)
                                and h3.get_resolution(cell) in layer.h3_resolutions)
            for reviewer_id, cells in hexes.items()
        }
        for layer in following_layers(config)
    }


def apply_assignments(config, allocations):
    '''*!*! Put allocated hexes into the config the tasks and browser use.

    Reviewers missing from a layer's allocation keep their YAML assignment
    (for example editors who do not label). An empty list removes the
    assignment, because an empty H3 list would otherwise grant every feature.
    Point layers get each reviewer's building hexes, unless the YAML gives
    the point assignment its own H3 list.
    '''

    followed = point_layer_cells(config, allocations)
    allocations = {**followed, **allocations}
    reviewers = []
    for reviewer in config.reviewers:
        assignments = []
        for assignment in reviewer.assignments:
            layer_allocation = allocations.get(assignment.layer_id)
            if (layer_allocation is None or reviewer.id not in layer_allocation
                    or (assignment.layer_id in followed and assignment.h3_indexes)):
                assignments.append(assignment)
                continue
            cells = layer_allocation[reviewer.id]
            if cells:
                assignments.append(replace(assignment, h3_indexes=tuple(cells)))
        reviewers.append(replace(reviewer, assignments=tuple(assignments)))
    return replace(config, reviewers=tuple(reviewers))


def read_expunged(connection, project_id):
    '''*!*! Reviewers whose labels no longer count in this project.'''

    rows = connection.execute(
        'SELECT reviewer_id FROM expunged_reviewers WHERE project_id = %s ORDER BY reviewer_id',
        [project_id],
    ).fetchall()
    return [row['reviewer_id'] for row in rows]


def listed_cells(connection, project_id, feature_lists):
    '''*!*! Each annotator's hexes: the hexes that hold a building on their list.'''

    allocations = {}
    for layer_id, settings in feature_lists.items():
        cells = {entry['id']: [] for entry in settings['reviewers']}
        rows = connection.execute(
            '''
SELECT DISTINCT listed.reviewer_id, cell.h3_index
FROM task_feature_assignments AS listed
JOIN tasks AS task ON task.id = listed.task_id
JOIN features AS feature
  ON feature.project_id = listed.project_id
 AND feature.layer_id = listed.layer_id
 AND feature.id = listed.feature_id
JOIN feature_h3 AS cell
  ON cell.project_id = listed.project_id
 AND cell.layer_id = listed.layer_id
 AND cell.feature_id = listed.feature_id
 AND cell.resolution = %s
WHERE task.project_id = %s
  AND task.layer_id = %s
  AND task.mode = 'annotation'
  AND feature.deleted_at IS NULL
ORDER BY listed.reviewer_id, cell.h3_index
''',
            [settings['resolution'], project_id, layer_id],
        ).fetchall()
        for row in rows:
            if row['reviewer_id'] in cells:
                cells[row['reviewer_id']].append(row['h3_index'])
        allocations[layer_id] = cells
    return allocations


def saved_assignment_config(connection, config):
    '''*!*! Give the browser the same persisted hex permissions as database tasks.'''

    saved = connection.execute(
        'SELECT signature, allocations, feature_lists FROM auto_hex_assignments WHERE project_id = %s',
        [config.project_id],
    ).fetchone()
    if saved is None:
        return config
    expunged = read_expunged(connection, config.project_id)
    if saved['signature'] != config_signature(config, expunged):
        raise ValueError('Auto-assignment eligibility changed; rerun --auto-assign-hex or --manual-assign-hex')
    if saved['feature_lists'] is None:
        # *!*! Saved by an earlier version: whole hexes, labelled as a whole.
        return apply_assignments(config, saved['allocations'])
    return apply_assignments(config, listed_cells(connection, config.project_id, saved['feature_lists']))


def read_layer_buildings(connection, config, layer, resolution, expunged):
    '''*!*! Read current buildings with their hex, counted labels, and drawer.'''

    rows = connection.execute(
        '''
SELECT
    feature.id,
    cell.h3_index,
    feature.properties,
    COALESCE((
        SELECT jsonb_object_agg(annotation.reviewer_id, annotation.label)
        FROM annotations AS annotation
        JOIN tasks AS task ON task.id = annotation.task_id
        WHERE task.project_id = feature.project_id
          AND task.layer_id = feature.layer_id
          AND task.mode = 'annotation'
          AND annotation.feature_id = feature.id
          AND annotation.reviewer_id <> ALL(%(expunged)s::text[])
    ), '{}'::jsonb) AS labels
FROM features AS feature
LEFT JOIN feature_h3 AS cell
  ON cell.project_id = feature.project_id AND cell.layer_id = feature.layer_id
 AND cell.feature_id = feature.id AND cell.resolution = %(resolution)s
WHERE feature.project_id = %(project_id)s
  AND feature.layer_id = %(layer_id)s
  AND feature.deleted_at IS NULL
ORDER BY feature.id
''',
        {'expunged': list(expunged), 'resolution': resolution,
         'project_id': config.project_id, 'layer_id': layer.id},
    ).fetchall()
    return [{
        'id': row['id'],
        'h3_index': row['h3_index'],
        'properties': row['properties'],
        'labels': row['labels'],
        # *!*! Drawn buildings carry the drawer's ID in the origin field.
        'drawn_by': row['properties'].get(layer.origin_field) if layer.origin_field else None,
    } for row in rows]


def prepare_assignments(connection, config, options=None, manual=False):
    '''*!*! Decide this run's assignments, saved atomically with task sync.

    Returns (config, feature_lists). `feature_lists` is None when the lists in
    the database stay as they are; otherwise `write_feature_lists` replaces
    them after the tasks are synchronized ({} clears them).
    '''

    expunged = read_expunged(connection, config.project_id)
    still_listed = sorted({reviewer.id for reviewer in config.reviewers} & set(expunged))
    if still_listed:
        raise ValueError(
            f'Expunged reviewers are still in the YAML: {", ".join(still_listed)}; '
            'remove them, or restore them with src.api.expunge --restore'
        )
    if manual:
        connection.execute('DELETE FROM auto_hex_assignments WHERE project_id = %s', [config.project_id])
        return config, {}
    if options is None:
        return saved_assignment_config(connection, config), None
    validate_options(config, options)
    signature = config_signature(config, expunged)
    saved = connection.execute(
        'SELECT signature, options, feature_lists FROM auto_hex_assignments WHERE project_id = %s',
        [config.project_id],
    ).fetchone()
    settings = asdict(options)
    # *!*! Confidence makes fraction irrelevant, including for restart reuse.
    if options.confidence_column is not None:
        settings['fraction'] = None
    if (saved and saved['feature_lists'] is not None
            and saved['signature'] == signature and saved['options'] == settings):
        print('Reusing saved automatic assignments', flush=True)
        return saved_assignment_config(connection, config), None

    feature_lists = {}
    cells = {}
    layer_settings = {}
    for layer in building_layers(config):
        resolution = options.resolution if options.resolution is not None else min(layer.h3_resolutions)
        eligible = annotators(config, layer)
        reviewers = [reviewer for reviewer, _ in eligible]
        caps = {reviewer: assignment.max_features for reviewer, assignment in eligible
                if assignment.max_features is not None}
        buildings = read_layer_buildings(connection, config, layer, resolution, expunged)
        lists, short, selected, total = allocate_feature_lists(
            buildings, reviewers, options, f'{config.project_id}:{layer.id}', caps,
        )
        cell_of = {building['id']: building['h3_index'] for building in buildings}
        cells[layer.id] = {reviewer: sorted({cell_of[feature] for feature in lists[reviewer]
                                             if cell_of[feature]})
                           for reviewer in reviewers}
        feature_lists[layer.id] = lists
        layer_settings[layer.id] = {
            'resolution': resolution,
            'reviewers': [{'id': reviewer, 'modes': layer_modes(config, reviewer, layer),
                           'max_features': assignment.max_features}
                          for reviewer, assignment in eligible],
        }
        print(f'Auto assignment {layer.id} at H3 r{resolution}: {selected}/{total} buildings, '
              f'{options.reviewers_per_hex} counted label(s) each', flush=True)
        for reviewer in reviewers:
            cap = f' (cap {caps[reviewer]})' if reviewer in caps else ''
            print(f'  {reviewer}: {len(cells[layer.id][reviewer])} hexes, '
                  f'{len(lists[reviewer])} buildings{cap}', flush=True)
        if short:
            print(f'  WARNING: {short} reviewer slot(s) are unfilled: caps or too few '
                  'reviewers. Add a reviewer or raise a cap, then rerun.', flush=True)
    connection.execute(
        '''
INSERT INTO auto_hex_assignments (project_id, signature, options, allocations, feature_lists)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (project_id) DO UPDATE SET signature = EXCLUDED.signature,
    options = EXCLUDED.options, allocations = EXCLUDED.allocations,
    feature_lists = EXCLUDED.feature_lists, updated_at = now()
''', [config.project_id, signature, Jsonb(settings), Jsonb({}), Jsonb(layer_settings)],
    )
    return apply_assignments(config, cells), feature_lists


def write_feature_lists(connection, config, feature_lists, task_ids):
    '''*!*! Replace the saved lists of every building layer's annotation task.

    `feature_lists` maps layer -> reviewer -> {feature_id: reason}; a layer
    missing from it goes back to whole-hex labelling.
    '''

    for layer in building_layers(config):
        task_id = task_ids.get(f'{layer.id}:annotation')
        if task_id is None:
            continue
        lists = feature_lists.get(layer.id)
        connection.execute('DELETE FROM task_feature_assignments WHERE task_id = %s', [task_id])
        connection.execute('UPDATE tasks SET feature_lists = %s WHERE id = %s',
                           [lists is not None, task_id])
        rows = [[task_id, reviewer, config.project_id, layer.id, feature_id, reason]
                for reviewer, listed in (lists or {}).items()
                for feature_id, reason in listed.items()]
        if rows:
            with connection.cursor() as cursor:
                cursor.executemany(
                    '''
INSERT INTO task_feature_assignments (
    task_id, reviewer_id, project_id, layer_id, feature_id, reason
) VALUES (%s, %s, %s, %s, %s, %s)
''', rows,
                )


def list_settings(connection, project_id, layer_id):
    '''*!*! Return (options, layer settings, annotation task ID) in list mode, else None.'''

    saved = connection.execute(
        'SELECT options, feature_lists FROM auto_hex_assignments WHERE project_id = %s',
        [project_id],
    ).fetchone()
    if saved is None or not saved['feature_lists'] or layer_id not in saved['feature_lists']:
        return None
    task = connection.execute(
        '''
SELECT id FROM tasks
WHERE project_id = %s AND layer_id = %s AND mode = 'annotation' AND active AND feature_lists
''',
        [project_id, layer_id],
    ).fetchone()
    if task is None:
        return None
    return saved['options'], saved['feature_lists'][layer_id], task['id']


def grant_feature(connection, project_id, layer_id, entry, feature_id, cell, resolution, reason):
    '''*!*! Put one building on a reviewer's list and give them its hex.'''

    for mode in entry['modes']:
        task = connection.execute(
            'SELECT id FROM tasks WHERE project_id = %s AND layer_id = %s AND mode = %s AND active',
            [project_id, layer_id, mode],
        ).fetchone()
        if task is None:
            continue
        connection.execute(
            '''
INSERT INTO task_reviewers (task_id, reviewer_id, all_features)
VALUES (%s, %s, false)
ON CONFLICT (task_id, reviewer_id) DO NOTHING
''', [task['id'], entry['id']],
        )
        if cell:
            connection.execute(
                '''
INSERT INTO task_h3_assignments (task_id, reviewer_id, resolution, h3_index)
VALUES (%s, %s, %s, %s)
ON CONFLICT (task_id, reviewer_id, h3_index) DO NOTHING
''', [task['id'], entry['id'], resolution, cell],
            )
        if mode == 'annotation':
            connection.execute(
                '''
INSERT INTO task_feature_assignments (
    task_id, reviewer_id, project_id, layer_id, feature_id, reason
) VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (task_id, reviewer_id, feature_id) DO NOTHING
''', [task['id'], entry['id'], project_id, layer_id, feature_id, reason],
            )
    if cell:
        # *!*! Point layers follow the building hexes, so the new hex brings
        # *!*! its points too, for point tasks the reviewer sees by hex.
        connection.execute(
            '''
INSERT INTO task_h3_assignments (task_id, reviewer_id, resolution, h3_index)
SELECT reviewer.task_id, reviewer.reviewer_id, %(resolution)s, %(cell)s
FROM task_reviewers AS reviewer
JOIN tasks AS task ON task.id = reviewer.task_id
JOIN feature_layers AS layer ON layer.project_id = task.project_id AND layer.id = task.layer_id
WHERE task.project_id = %(project_id)s
  AND task.active
  AND reviewer.reviewer_id = %(reviewer_id)s
  AND NOT reviewer.all_features
  AND NOT layer.geometry_types <@ ARRAY['Polygon', 'MultiPolygon']::text[]
  AND %(resolution)s = ANY(layer.h3_resolutions)
ON CONFLICT (task_id, reviewer_id, h3_index) DO NOTHING
''', {'resolution': resolution, 'cell': cell, 'project_id': project_id,
          'reviewer_id': entry['id']},
        )


def top_up_feature(connection, project_id, layer_id, feature_id, drawn_by=None):
    '''*!*! Give one building the reviewers it still needs, right away.

    Called when a building is drawn (with `drawn_by`) and when it gets an
    "unknown" label. Prefers reviewers who already have its hex, then the
    least open work, then YAML order. Returns the reviewers added.
    '''

    found = list_settings(connection, project_id, layer_id)
    if found is None:
        return []
    options, settings, task_id = found
    # *!*! Two saves on one building at once must not both add a reviewer.
    connection.execute(
        'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
        [f'top-up:{project_id}:{layer_id}:{feature_id}'],
    )
    resolution = settings['resolution']
    cell_row = connection.execute(
        '''
SELECT h3_index FROM feature_h3
WHERE project_id = %s AND layer_id = %s AND feature_id = %s AND resolution = %s
''',
        [project_id, layer_id, feature_id, resolution],
    ).fetchone()
    cell = cell_row['h3_index'] if cell_row else None
    entries = {entry['id']: entry for entry in settings['reviewers']}
    added = []
    if drawn_by in entries:
        # *!*! The drawer always gets their own building, even over a cap.
        grant_feature(connection, project_id, layer_id, entries[drawn_by],
                      feature_id, cell, resolution, 'drawn')

    expunged = set(read_expunged(connection, project_id))
    labels = {
        row['reviewer_id']: row['label']
        for row in connection.execute(
            'SELECT reviewer_id, label FROM annotations WHERE task_id = %s AND feature_id = %s',
            [task_id, feature_id],
        ).fetchall()
        if row['reviewer_id'] not in expunged
    }
    holders = {
        row['reviewer_id']
        for row in connection.execute(
            'SELECT reviewer_id FROM task_feature_assignments WHERE task_id = %s AND feature_id = %s',
            [task_id, feature_id],
        ).fetchall()
    }
    still_needed = reviewers_still_needed(labels, holders, options['reviewers_per_hex'])
    if not still_needed:
        return added

    loads = {
        row['reviewer_id']: row
        for row in connection.execute(
            '''
SELECT
    listed.reviewer_id,
    count(*) AS listed,
    count(*) FILTER (WHERE annotation.id IS NULL) AS open
FROM task_feature_assignments AS listed
LEFT JOIN annotations AS annotation
  ON annotation.task_id = listed.task_id
 AND annotation.layer_id = listed.layer_id
 AND annotation.feature_id = listed.feature_id
 AND annotation.reviewer_id = listed.reviewer_id
WHERE listed.task_id = %s
GROUP BY listed.reviewer_id
''',
            [task_id],
        ).fetchall()
    }
    with_cell = {
        row['reviewer_id']
        for row in connection.execute(
            'SELECT reviewer_id FROM task_h3_assignments WHERE task_id = %s AND h3_index = %s',
            [task_id, cell],
        ).fetchall()
    } if cell else set()

    candidates = []
    for position, entry in enumerate(settings['reviewers']):
        reviewer = entry['id']
        if reviewer in holders or reviewer in labels or reviewer in expunged:
            continue
        load = loads.get(reviewer, {'listed': 0, 'open': 0})
        if entry['max_features'] is not None and load['listed'] >= entry['max_features']:
            continue
        candidates.append(((reviewer not in with_cell, load['open'], position), entry))
    candidates.sort(key=lambda candidate: candidate[0])
    for _, entry in candidates[:still_needed]:
        grant_feature(connection, project_id, layer_id, entry, feature_id, cell, resolution, 'topped_up')
        added.append(entry['id'])
    return added
