'''*!*! Verify whole-hex sampling, overlap, permissions, and restart persistence.'''

from dataclasses import asdict, replace
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from src.api.auto_assign import (
    AutoAssignOptions, allocate_hexes, apply_assignments, assignment_options,
    config_signature, prepare_assignments, saved_assignment_config, validate_options,
)
from src.api.bootstrap import build_parser, sync_tasks
from src.project_config import (
    EditingConfig, LayerConfig, ReviewConfig, ReviewerAssignmentConfig, ReviewerConfig,
)


class AutoAssignTests(unittest.TestCase):
    '''*!*! Exercise assignments without a live database or remote Parquet.'''

    def config(self):
        '''*!*! Reuse a validated multi-reviewer project with a context point layer.'''

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
            ) for layer in (buildings, points)),
        ) for name in ('alice', 'bob', 'carol'))
        return ReviewConfig(
            source_path=Path('/project.yaml'), project_id='test', project_name='Test',
            imagery_cog='/cog.tif', layers=(buildings, points),
            annotation_labels=('damaged', 'undamaged'), modes=('annotation', 'editing'),
            reviewers=reviewers,
        )

    def rows(self, counts):
        '''*!*! Make whole-cell populations with an ordinary confidence value.'''

        return [{'h3_index': cell, 'properties': {'confidence': 0.9}}
                for cell, count in counts.items() for _ in range(count)]

    def test_balancing_uses_buildings_and_yaml_order_for_ties(self):
        '''*!*! Largest cells are distributed first and extra work favors earlier users.'''

        assignments, loads, selected, total = allocate_hexes(
            self.rows({'a': 10, 'b': 9, 'c': 2, 'd': 1, 'e': 1}),
            ['first', 'second'], AutoAssignOptions(1), 'project',
        )
        self.assertEqual(loads, {'first': 12, 'second': 11})
        self.assertEqual(assignments['first'], ['a', 'd', 'e'])
        self.assertEqual((selected, total), (23, 23))

        _, loads, _, _ = allocate_hexes(
            self.rows({'a': 8, 'b': 7, 'c': 6}), ['first', 'second'], AutoAssignOptions(1), 'p',
        )
        self.assertEqual(loads, {'first': 13, 'second': 8})

    def test_overlap_is_distinct_and_applies_to_every_selected_cell(self):
        '''*!*! Each whole cell is delivered to exactly N different reviewers.'''

        assignments, loads, selected, total = allocate_hexes(
            self.rows({'a': 10, 'b': 7, 'c': 3}),
            ['first', 'second', 'third'], AutoAssignOptions(1, 2), 'project',
        )
        for cell in ('a', 'b', 'c'):
            self.assertEqual(sum(cell in cells for cells in assignments.values()), 2)
        self.assertEqual(sum(loads.values()), 2 * total)

    def test_fraction_is_repeatable_and_whole_cell_approximate(self):
        '''*!*! Source row order does not change selected cells or reviewer allocation.'''

        rows = self.rows({str(index): 3 for index in range(10)})
        options = AutoAssignOptions(0.25)
        result = allocate_hexes(rows, ['a', 'b'], options, 'project')
        self.assertEqual(result, allocate_hexes(rows[::-1], ['a', 'b'], options, 'project'))
        self.assertLessEqual(abs(result[2] - 7.5), 1.5)
        self.assertEqual(allocate_hexes(rows, ['a'], AutoAssignOptions(0), 'p')[2], 0)
        self.assertEqual(allocate_hexes(rows, ['a'], AutoAssignOptions(1), 'p')[2], 30)

    def test_confidence_overrides_fraction_and_includes_neighbors(self):
        '''*!*! One qualifying building includes its entire cell and threshold is inclusive.'''

        rows = self.rows({'a': 5, 'b': 3, 'c': 2})
        rows[0]['properties']['confidence'] = 0.7
        rows[5]['properties']['confidence'] = None
        for fraction in (0, 1):
            assignments, loads, selected, total = allocate_hexes(
                rows, ['first', 'second'], AutoAssignOptions(fraction, 1, 'confidence', 0.7), 'p',
            )
            self.assertEqual(assignments, {'first': ['a'], 'second': []})
            self.assertEqual((selected, total), (5, 10))

    def test_bad_confidence_fails_instead_of_silently_dropping_work(self):
        '''*!*! Missing columns and invalid non-null values cannot produce an allocation.'''

        for properties in ({}, {'confidence': 'bad'}, {'confidence': float('nan')}, {'confidence': True}):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                allocate_hexes([{'h3_index': 'a', 'properties': properties}], ['a'],
                               AutoAssignOptions(1, 1, 'confidence', 0.5), 'p')

    def test_empty_results_do_not_grant_all_feature_access(self):
        '''*!*! Empty auto assignments disappear while context-layer permissions survive.'''

        config = self.config()
        layer = next(layer for layer in config.layers if 'Polygon' in layer.geometry_types)
        allocations = {layer.id: {reviewer.id: [] for reviewer in config.reviewers}}
        effective = apply_assignments(config, allocations)
        for before, after in zip(config.reviewers, effective.reviewers):
            self.assertEqual(after.assignments, tuple(
                assignment for assignment in before.assignments if assignment.layer_id != layer.id
            ))
        connection = MagicMock()
        sync_tasks(connection, effective)
        task_inserts = [call for call in connection.execute.call_args_list
                        if 'INSERT INTO task_reviewers' in call.args[0]]
        # *!*! No polygon reviewer can acquire the empty-list all-features shortcut.
        from src.api.bootstrap import task_id_for
        polygon_tasks = {task_id_for(config.project_id, layer.id, mode) for mode in layer.modes}
        self.assertTrue(all(call.args[1][0] not in polygon_tasks for call in task_inserts))

    def test_restart_reuses_allocations_without_reading_features(self):
        '''*!*! Both ordinary and flagged restarts retain work even after geometry edits.'''

        config = self.config()
        options = AutoAssignOptions(0.5)
        saved = {'signature': config_signature(config), 'options': asdict(options), 'allocations': {}}
        for supplied in (None, options):
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = saved
            self.assertEqual(prepare_assignments(connection, config, supplied), config)
            connection.execute.assert_called_once()

    def test_changed_reviewer_order_requires_explicit_reassignment(self):
        '''*!*! A YAML eligibility change cannot silently change an existing allocation.'''

        config = self.config()
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {
            'signature': config_signature(config), 'allocations': {},
        }
        with self.assertRaisesRegex(ValueError, 'eligibility changed'):
            saved_assignment_config(connection, replace(config, reviewers=config.reviewers[::-1]))

    def test_manual_flag_removes_saved_override(self):
        '''*!*! Explicit manual mode restores the original YAML without modifying it.'''

        config = self.config()
        connection = MagicMock()
        self.assertIs(prepare_assignments(connection, config, manual=True), config)
        self.assertIn('DELETE FROM auto_hex_assignments', connection.execute.call_args.args[0])

    def test_new_assignments_persist_and_reach_browser_configuration(self):
        '''*!*! Database task permissions and browser filters share the saved hex lists.'''

        from app import make_handler
        config = self.config()
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        cell = '85283083fffffff'
        connection.execute.return_value.fetchall.return_value = self.rows({cell: 3})
        effective = prepare_assignments(connection, config, AutoAssignOptions(1, 2))
        write = connection.execute.call_args
        self.assertIn('INSERT INTO auto_hex_assignments', write.args[0])
        saved_allocations = write.args[1][3].obj
        self.assertEqual(saved_allocations['buildings'], {'alice': [cell], 'bob': [cell], 'carol': []})
        handler_class = make_handler(effective)
        handler = handler_class.__new__(handler_class)
        alice = handler.frontend_config(effective.reviewers[0])
        carol = handler.frontend_config(effective.reviewers[2])
        self.assertEqual(alice['layers'][0]['assignments'], {
            'annotation': [cell], 'editing': [cell],
        })
        self.assertEqual([layer['id'] for layer in carol['layers']], ['points'])

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
