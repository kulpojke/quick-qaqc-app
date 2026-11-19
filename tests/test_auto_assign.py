'''*!*! Verify the counting rule, per-building lists, hex selection, and permissions.'''

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from src.api.auto_assign import (
    AutoAssignOptions, allocate_feature_lists, apply_assignments, assignment_options,
    config_signature, counted_labels, reviewers_still_needed, select_cells, validate_options,
)
from src.api.bootstrap import build_parser, sync_tasks
from src.project_config import (
    EditingConfig, LayerConfig, ReviewConfig, ReviewerAssignmentConfig, ReviewerConfig,
)


def building(feature_id, cell, labels=None, drawn_by=None, confidence=0.9):
    '''*!*! One building row in the shape read_layer_buildings returns.'''

    return {'id': feature_id, 'h3_index': cell, 'properties': {'confidence': confidence},
            'labels': labels or {}, 'drawn_by': drawn_by}


def holders_of(lists, feature_id):
    '''*!*! Reviewers that have one building on their list.'''

    return {reviewer for reviewer, listed in lists.items() if feature_id in listed}


class CountingRuleTests(unittest.TestCase):
    '''*!*! The table agreed for N = 2: the first "unknown" does not count.'''

    def test_counted_labels_and_still_needed_for_two_reviewers(self):
        table = [
            ({}, 0, 2),
            ({'a': 'damaged'}, 1, 1),
            ({'a': 'unknown'}, 0, 2),
            ({'a': 'damaged', 'b': 'unknown'}, 1, 1),
            ({'a': 'unknown', 'b': 'unknown'}, 1, 1),
            ({'a': 'damaged', 'b': 'damaged'}, 2, 0),
        ]
        for labels, counted, needed in table:
            with self.subTest(labels=labels):
                self.assertEqual(counted_labels(labels), counted)
                self.assertEqual(reviewers_still_needed(labels, set(labels), 2), needed)

    def test_holders_who_have_not_labelled_yet_count_as_coming(self):
        '''*!*! One person reviewed it and one more holds it: nobody new is needed.'''

        self.assertEqual(reviewers_still_needed({'a': 'damaged'}, {'a', 'b'}, 2), 0)
        self.assertEqual(reviewers_still_needed({}, {'drawer'}, 2), 1)
        self.assertEqual(reviewers_still_needed({'a': 'unknown'}, {'a', 'b'}, 2), 1)


class AllocationTests(unittest.TestCase):
    '''*!*! Share out buildings without a database.'''

    def test_fresh_allocation_gives_each_building_n_distinct_reviewers_by_hex(self):
        buildings = [building(f'{cell}{index}', cell) for cell, count in
                     (('h1', 6), ('h2', 5), ('h3', 3), ('h4', 2)) for index in range(count)]
        lists, short, selected, total = allocate_feature_lists(
            buildings, ['a', 'b', 'c', 'd'], AutoAssignOptions(1, 2), 'seed',
        )
        self.assertEqual((short, selected, total), (0, 16, 16))
        for item in buildings:
            self.assertEqual(len(holders_of(lists, item['id'])), 2)
        # *!*! Without labels, a whole hex goes to the same two people.
        for cell in ('h1', 'h2', 'h3', 'h4'):
            members = [item['id'] for item in buildings if item['h3_index'] == cell]
            self.assertEqual(len({frozenset(holders_of(lists, member)) for member in members}), 1)
        loads = sorted(len(listed) for listed in lists.values())
        self.assertLessEqual(loads[-1] - loads[0], 3)

    def test_labelled_buildings_need_only_the_missing_reviewers(self):
        '''*!*! One person reviewed it, so exactly one other person gets it.'''

        buildings = [
            building('both', 'h1', {'a': 'damaged', 'b': 'undamaged'}),
            building('one', 'h1', {'b': 'damaged'}),
            building('none', 'h1'),
        ]
        lists, short, _, _ = allocate_feature_lists(
            buildings, ['a', 'b', 'c'], AutoAssignOptions(1, 2), 'seed',
        )
        self.assertEqual(short, 0)
        self.assertEqual(holders_of(lists, 'both'), {'a', 'b'})
        self.assertEqual(len(holders_of(lists, 'one')), 2)
        self.assertIn('b', holders_of(lists, 'one'))
        self.assertEqual(len(holders_of(lists, 'none')), 2)

    def test_reviewer_who_left_still_counts_and_partner_takes_the_rest(self):
        '''*!*! The hex was A + B and A left halfway (the agreed example).'''

        buildings = [
            building('both', 'h1', {'a': 'damaged', 'b': 'damaged'}),
            building('b_only', 'h1', {'b': 'damaged'}),
            building('a_only', 'h1', {'a': 'damaged'}),
            building('none', 'h1'),
        ]
        lists, short, _, _ = allocate_feature_lists(
            buildings, ['b', 'c'], AutoAssignOptions(1, 2), 'seed',
        )
        self.assertEqual(short, 0)
        self.assertEqual(holders_of(lists, 'both'), {'b'})
        self.assertEqual(holders_of(lists, 'b_only'), {'b', 'c'})
        self.assertEqual(holders_of(lists, 'a_only'), {'b'})
        self.assertEqual(holders_of(lists, 'none'), {'b', 'c'})

    def test_first_unknown_brings_a_third_reviewer(self):
        lists, _, _, _ = allocate_feature_lists(
            [building('x', 'h1', {'a': 'unknown'})], ['a', 'b', 'c', 'd'],
            AutoAssignOptions(1, 2), 'seed',
        )
        self.assertEqual(len(holders_of(lists, 'x')), 3)
        self.assertIn('a', holders_of(lists, 'x'))

    def test_caps_are_exact_and_unfilled_slots_are_reported(self):
        buildings = [building(f'b{index}', f'h{index % 3}') for index in range(12)]
        lists, short, _, _ = allocate_feature_lists(
            buildings, ['capped', 'b', 'c'], AutoAssignOptions(1, 2), 'seed', {'capped': 5},
        )
        self.assertEqual(len(lists['capped']), 5)
        self.assertEqual(short, 0)
        self.assertEqual(sum(len(listed) for listed in lists.values()), 24)
        _, short, _, _ = allocate_feature_lists(
            buildings, ['capped', 'b'], AutoAssignOptions(1, 2), 'seed', {'capped': 5},
        )
        self.assertEqual(short, 7)

    def test_labels_and_drawn_buildings_count_toward_a_cap(self):
        buildings = [building('labelled', 'h1', {'m': 'damaged'}),
                     building('drawn', 'h1', drawn_by='m'),
                     building('new', 'h1')]
        lists, _, _, _ = allocate_feature_lists(
            buildings, ['m', 'b', 'c'], AutoAssignOptions(1, 2), 'seed', {'m': 2},
        )
        self.assertEqual(lists['m'], {'labelled': 'allocated', 'drawn': 'drawn'})
        # *!*! The drawer holds it, so it needs one other reviewer, not two.
        self.assertEqual(len(holders_of(lists, 'drawn')), 2)
        self.assertEqual(holders_of(lists, 'new'), {'b', 'c'})

    def test_labelled_work_outside_the_selection_stays_on_the_list(self):
        buildings = [building('kept', 'h1', {'a': 'damaged'}), building('other', 'h2')]
        lists, _, selected, _ = allocate_feature_lists(
            buildings, ['a', 'b'], AutoAssignOptions(0, 1, 'confidence', 0.1), 'seed',
        )
        self.assertEqual(selected, 0)
        self.assertEqual(lists, {'a': {'kept': 'allocated'}, 'b': {}})


class SelectionTests(unittest.TestCase):
    '''*!*! Whole-hex selection by fraction or confidence (unchanged behaviour).'''

    def rows(self, counts):
        return [building(f'{cell}-{index}', cell) for cell, count in counts.items()
                for index in range(count)]

    def test_fraction_is_repeatable_and_whole_cell_approximate(self):
        rows = self.rows({str(index): 3 for index in range(10)})
        counts, selected = select_cells(rows, AutoAssignOptions(0.25), 'project')
        self.assertEqual(selected, select_cells(rows[::-1], AutoAssignOptions(0.25), 'project')[1])
        self.assertLessEqual(abs(sum(counts[cell] for cell in selected) - 7.5), 1.5)
        self.assertEqual(select_cells(rows, AutoAssignOptions(0), 'p')[1], set())
        self.assertEqual(len(select_cells(rows, AutoAssignOptions(1), 'p')[1]), 10)

    def test_confidence_overrides_fraction_and_includes_neighbors(self):
        rows = self.rows({'a': 5, 'b': 3, 'c': 2})
        rows[0]['properties']['confidence'] = 0.7
        rows[5]['properties']['confidence'] = None
        for fraction in (0, 1):
            _, selected = select_cells(rows, AutoAssignOptions(fraction, 1, 'confidence', 0.7), 'p')
            self.assertEqual(selected, {'a'})

    def test_bad_confidence_fails_instead_of_silently_dropping_work(self):
        for properties in ({}, {'confidence': 'bad'}, {'confidence': float('nan')}, {'confidence': True}):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                select_cells([{'h3_index': 'a', 'properties': properties}],
                             AutoAssignOptions(1, 1, 'confidence', 0.5), 'p')


class PermissionTests(unittest.TestCase):
    '''*!*! Config, signature, and browser permissions.'''

    def config(self, caps=None):
        caps = caps or {}
        buildings = LayerConfig(
            id='buildings', name='Buildings', source=Path('/buildings.parquet'),
            source_crs='EPSG:4326', geometry_types=('Polygon',), feature_id_field='id',
            predicted_class_field='', confidence_field='confidence', display_fields=(),
            h3_prefix='h3_r', h3_resolutions=(5, 8), modes=('annotation', 'editing'),
            editing=EditingConfig(move=True),
        )
        points = replace(buildings, id='points', geometry_types=('Point',))
        reviewers = tuple(ReviewerConfig(
            id=name, assignments=tuple(ReviewerAssignmentConfig(
                layer_id=layer.id, modes=layer.modes, h3_indexes=(),
                max_features=caps.get(name) if layer is buildings else None,
            ) for layer in (buildings, points)),
        ) for name in ('alice', 'bob', 'carol'))
        return ReviewConfig(
            source_path=Path('/project.yaml'), project_id='test', project_name='Test',
            imagery_cog='/cog.tif', layers=(buildings, points),
            annotation_labels=('damaged', 'undamaged'), modes=('annotation', 'editing'),
            reviewers=reviewers,
        )

    def test_signature_is_unchanged_for_configs_saved_by_earlier_versions(self):
        '''*!*! The deployed project keeps working on a plain restart after upgrade.'''

        config = self.config()
        earlier = [
            (layer.id, str(layer.source), layer.h3_resolutions,
             [(reviewer.id, [assignment.modes for assignment in reviewer.assignments
                             if assignment.layer_id == layer.id])
              for reviewer in config.reviewers])
            for layer in config.layers if layer.id == 'buildings'
        ]
        earlier_hash = hashlib.sha256(json.dumps(earlier, sort_keys=True).encode()).hexdigest()
        self.assertEqual(config_signature(config), earlier_hash)
        self.assertNotEqual(config_signature(self.config({'alice': 50})), earlier_hash)
        self.assertNotEqual(config_signature(config, ['dave']), earlier_hash)

    def test_empty_results_do_not_grant_all_feature_access(self):
        '''*!*! Empty auto assignments disappear, and so do the points that follow them.'''

        config = self.config()
        allocations = {'buildings': {reviewer.id: [] for reviewer in config.reviewers}}
        effective = apply_assignments(config, allocations)
        for after in effective.reviewers:
            self.assertEqual(after.assignments, ())
        connection = MagicMock()
        sync_tasks(connection, effective)
        task_inserts = [call for call in connection.execute.call_args_list
                        if 'INSERT INTO task_reviewers' in call.args[0]]
        from src.api.bootstrap import task_id_for
        polygon_tasks = {task_id_for(config.project_id, layer, mode)
                         for layer in ('buildings', 'points') for mode in ('annotation', 'editing')}
        self.assertTrue(all(call.args[1][0] not in polygon_tasks for call in task_inserts))

    def test_point_layers_follow_building_hexes(self):
        '''*!*! Points show in the reviewer's building hexes; an own YAML list wins.'''

        import h3
        first, second = h3.latlng_to_cell(0.5, 0.5, 5), h3.latlng_to_cell(1.5, 1.5, 5)
        config = self.config()
        carol = config.reviewers[2]
        own = tuple(replace(assignment, h3_indexes=(second,)) if assignment.layer_id == 'points'
                    else assignment for assignment in carol.assignments)
        config = replace(config, reviewers=(*config.reviewers[:2], replace(carol, assignments=own)))
        effective = apply_assignments(config, {'buildings': {
            'alice': [first], 'bob': [first, second], 'carol': [first],
        }})
        points = {reviewer.id: reviewer.assignment_for('points', 'editing').h3_indexes
                  for reviewer in effective.reviewers}
        self.assertEqual(points, {'alice': (first,), 'bob': (first, second), 'carol': (second,)})

        # *!*! A point layer without the hex resolution cannot follow.
        coarse_points = replace(config.layers[1], h3_resolutions=(8,))
        coarse = replace(config, layers=(config.layers[0], coarse_points))
        with self.assertRaisesRegex(ValueError, 'follow the building hexes'):
            validate_options(coarse, AutoAssignOptions(1, 1, resolution=5))
        validate_options(coarse, AutoAssignOptions(1, 1, resolution=8))

    def test_reviewers_missing_from_an_allocation_keep_their_yaml_assignment(self):
        config = self.config()
        effective = apply_assignments(config, {'buildings': {'alice': ['cell']}})
        self.assertEqual(effective.reviewers[0].assignment_for('buildings', 'annotation').h3_indexes,
                         ('cell',))
        self.assertEqual(effective.reviewers[1], config.reviewers[1])

    def test_browser_config_reads_hexes_fresh_when_a_loader_is_given(self):
        '''*!*! Hexes added by a top-up appear on the next page load.'''

        from app import make_handler
        config = self.config()
        current = {'cells': ['first']}

        def loader():
            return apply_assignments(config, {'buildings': {'alice': list(current['cells'])}})

        handler_class = make_handler(loader(), config_loader=loader)
        handler = handler_class.__new__(handler_class)
        alice = config.reviewers[0]
        self.assertEqual(handler.frontend_config(alice)['layers'][0]['assignments'],
                         {'annotation': ['first'], 'editing': ['first']})
        current['cells'] = ['first', 'topped-up']
        self.assertEqual(handler.frontend_config(alice)['layers'][0]['assignments'],
                         {'annotation': ['first', 'topped-up'], 'editing': ['first', 'topped-up']})

    def test_cli_defaults_overlap_and_validates_inputs(self):
        '''*!*! Optional overlap, threshold override, and resolution use one shared parser.'''

        parser = build_parser()
        args = parser.parse_args(['--yaml', 'test.yaml', '--auto-assign-hex', '0.25'])
        self.assertEqual(assignment_options(args), AutoAssignOptions(0.25))
        args = parser.parse_args(['--yaml', 'test.yaml', '--auto-assign-hex', '0.25', '2',
                                  '--conf', 'confidence', '0.7', '--hex-resolution', '8'])
        self.assertEqual(assignment_options(args), AutoAssignOptions(0.25, 2, 'confidence', 0.7, 8))
        for arguments in (['--auto-assign-hex', 'nan'], ['--auto-assign-hex', '1.1'],
                          ['--auto-assign-hex', '0.5', '0'], ['--auto-assign-hex', '0.5', '1.5'],
                          ['--conf', 'confidence', '0.7']):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                assignment_options(parser.parse_args(['--yaml', 'test.yaml', *arguments]))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            validate_options(self.config(), AutoAssignOptions(1, 1000))
        with self.assertRaisesRegex(ValueError, 'resolution'):
            validate_options(self.config(), AutoAssignOptions(1, resolution=15))


if __name__ == '__main__':
    unittest.main()
