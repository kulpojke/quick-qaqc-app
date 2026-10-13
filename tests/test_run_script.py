'''*!*! Verify deployment sequencing without touching Docker or persistent data.'''

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RunScriptTests(unittest.TestCase):
    '''*!*! Exercise the actual shell script using a recording Docker executable.'''

    def setUp(self):
        '''*!*! Build a disposable checkout and capture every Docker invocation.'''

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout with spaces'
        self.repo.mkdir()
        shutil.copy(Path(__file__).resolve().parents[1] / 'run.sh', self.repo)
        (self.repo / '.env').write_text('POSTGRES_PASSWORD=test\n')
        (self.repo / 'compose.yaml').touch()
        (self.repo / 'src/api').mkdir(parents=True)
        (self.repo / 'src/api/r2_export.py').touch()
        self.yaml = self.repo / 'project with spaces.yaml'
        self.yaml.write_text('version: 3\n')
        self.log = self.root / 'calls.jsonl'
        binary = self.root / 'docker'
        binary.write_text('''#!/usr/bin/env python3
import json
import os
import sys
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps({
        'args': sys.argv[1:],
        'directory': os.environ['PROJECT_CONFIG_DIR'],
        'yaml': os.environ['PROJECT_CONFIG'],
    }) + '\\n')
if os.environ.get('FAIL_STEP') in sys.argv[1:]:
    sys.exit(17)
''')
        binary.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.root}:{os.environ["PATH"]}', CALL_LOG=str(self.log))

    def run_script(self, *args):
        '''*!*! Invoke from outside the checkout to check argument resolution.'''

        return subprocess.run(
            ['bash', str(self.repo / 'run.sh'), *map(str, args)],
            cwd=self.root, env=self.env, capture_output=True, text=True,
        )

    def calls(self):
        '''*!*! Decode the captured commands without retaining any real secrets.'''

        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_updates_preserve_database_and_use_selected_yaml(self):
        '''*!*! Paths with spaces survive and only application containers recreate.'''

        result = self.run_script(self.yaml.relative_to(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        for call in calls:
            self.assertEqual(call['directory'], str(self.repo))
            self.assertEqual(call['yaml'], self.yaml.name)
            self.assertNotIn('down', call['args'])
            self.assertNotIn('prune', call['args'])
        commands = [call['args'][7:] for call in calls]
        self.assertEqual(commands[0], ['config', '--quiet'])
        self.assertEqual(commands[1], ['build', 'app'])
        self.assertIn('load_review_config', commands[2][commands[2].index('-c') + 1])
        self.assertEqual(commands[3][-1], 'db')
        self.assertIn('--no-recreate', commands[3])
        self.assertEqual(commands[4], ['run', '--rm', '--no-deps', 'migrate'])
        self.assertEqual(commands[5], ['run', '--rm', '--no-deps', 'bootstrap'])
        self.assertEqual(commands[6][-3:], ['app', 'api', 'export'])
        self.assertIn('--wait', commands[6])
        self.assertIn('--force-recreate', commands[6])

    def test_failed_build_or_bootstrap_stops_before_app_replacement(self):
        '''*!*! A failed prerequisite cannot proceed to replacing live services.'''

        for step in ('build', 'migrate', 'bootstrap'):
            with self.subTest(step=step):
                self.log.unlink(missing_ok=True)
                self.env['FAIL_STEP'] = step
                result = self.run_script(self.yaml)
                self.assertEqual(result.returncode, 17)
                self.assertFalse(any('--force-recreate' in call['args'] for call in self.calls()))

    def test_external_yaml_mounts_its_parent(self):
        '''*!*! An external project's relative data resolves beneath its directory.'''

        external = self.root / 'external project.yaml'
        external.write_text('version: 3\n')
        result = self.run_script(external)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0]['directory'], str(self.root))
        self.assertEqual(self.calls()[0]['yaml'], external.name)

    def test_auto_assignment_flags_reach_preflight_and_bootstrap(self):
        '''*!*! Deployment forwards argument boundaries including a spaced column name.'''

        flags = ['--auto-assign-hex', '0.25', '2', '--conf', 'model confidence', '0.7',
                 '--hex-resolution', '8']
        result = self.run_script(self.yaml, *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        preflight = next(call['args'] for call in calls if '-c' in call['args'])
        bootstrap = next(call['args'] for call in calls if 'bootstrap' in call['args'])
        self.assertEqual(preflight[-len(flags):], flags)
        self.assertEqual(bootstrap[-len(flags):], flags)
        self.assertIn('src.api.bootstrap', bootstrap)

    def test_invalid_arguments_never_invoke_docker(self):
        '''*!*! Missing YAML or server settings fails before any Docker action.'''

        for args in ((), ('missing.yaml',), (self.yaml, self.yaml)):
            self.assertNotEqual(self.run_script(*args).returncode, 0)
        (self.repo / '.env').unlink()
        self.assertNotEqual(self.run_script(self.yaml).returncode, 0)
        self.assertFalse(self.log.exists())

    def test_missing_publisher_stops_before_build(self):
        '''*!*! An omitted new module is diagnosed before the expensive build.'''

        (self.repo / 'src/api/r2_export.py').unlink()
        result = self.run_script(self.yaml)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing src/api/r2_export.py', result.stderr)
        self.assertFalse(any('build' in call['args'] for call in self.calls()))
