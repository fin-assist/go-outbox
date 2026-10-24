"""Exercise the actual release workflow shell with a stateful GitHub CLI fake."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


def workflow_script(name):
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    step = re.search(r'^      - name: ' + re.escape(name)
                     + r'\n(.*?)(?=^      - |\Z)', workflow, re.M | re.S)
    if step is None:
        raise AssertionError(f'Missing workflow step: {name}')
    block = re.search(r'^        run: \|\n((?:          .*\n|\n)+)', step[1], re.M)
    if block:
        return textwrap.dedent(block[1])
    return re.search(r'^        run: (.+)$', step[1], re.M)[1]


FAKE_GH = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ['FAKE_GITHUB'])
state = json.loads((root / 'state.json').read_text())
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[1:3]
if command == ['release', 'view']:
    if state.get('read_error'):
        print(state['read_error'], file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if state.get('exists') else 1)
if command == ['release', 'create']:
    if state.get('concurrent'):
        state['exists'] = True
        state['notes'] = 'Published by another actor'
        (root / 'state.json').write_text(json.dumps(state))
    if state.get('exists'):
        print('HTTP 422: Release.tag_name already exists', file=sys.stderr)
        sys.exit(1)
    if state.get('create_error'):
        print(state['create_error'], file=sys.stderr)
        sys.exit(1)
    state['exists'] = True
    state['notes'] = Path(sys.argv[sys.argv.index('--notes-file') + 1]).read_text()
    (root / 'state.json').write_text(json.dumps(state))
    sys.exit(0)
print('Unexpected mutation: ' + ' '.join(sys.argv[1:]), file=sys.stderr)
sys.exit(1)
'''


class ReleasePublicationTests(unittest.TestCase):
    def publish(self, initial, attempts=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state.json').write_text(json.dumps(initial))
            (root / 'notes.md').write_text('Notes from the tagged CHANGELOG\n')
            gh = root / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + FAKE_GH)
            gh.chmod(0o755)
            env = dict(os.environ, TAG='v0.1.1', FAKE_GITHUB=directory,
                       PATH=directory + os.pathsep + os.environ['PATH'])
            results = [subprocess.run(
                ['/bin/bash', '-e', '-o', 'pipefail', '-c', workflow_script('GitHub release')],
                cwd=root, env=env, capture_output=True, text=True, timeout=10)
                for _ in range(attempts)]
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            return results, calls, json.loads((root / 'state.json').read_text())

    def test_existing_release_is_preserved_without_creation(self):
        initial = {'exists': True, 'notes': 'Owner notes', 'assets': ['archive.zip']}
        results, calls, state = self.publish(initial)
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        self.assertEqual([call[:2] for call in calls], [['release', 'view']])
        self.assertEqual(state, initial)

    def test_new_release_can_be_published_twice(self):
        results, calls, state = self.publish({'exists': False}, attempts=2)
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        self.assertEqual([call[:2] for call in calls],
                         [['release', 'view'], ['release', 'create'], ['release', 'view']])
        self.assertIn('--verify-tag', calls[1])
        self.assertEqual(state['notes'], 'Notes from the tagged CHANGELOG\n')

    def test_concurrent_publication_is_success_and_preserves_other_release(self):
        results, _, state = self.publish({'exists': False, 'concurrent': True})
        self.assertEqual(results[0].returncode, 0, results[0].stderr)
        self.assertEqual(state['notes'], 'Published by another actor')

    def test_api_errors_without_a_confirmed_release_still_fail(self):
        for error in ('HTTP 403: Resource not accessible', 'HTTP 500: Server error',
                      'Network connection failed'):
            with self.subTest(error=error):
                results, _, state = self.publish({'exists': False, 'read_error': error,
                                                  'create_error': error})
                self.assertNotEqual(results[0].returncode, 0)
                self.assertFalse(state['exists'])


class ReleaseValidationTests(unittest.TestCase):
    def validate_and_publish(self, *, off_main=False, changelog_tag='v0.1.1', tag='v0.1.1'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True,
                                      capture_output=True, text=True, timeout=10).stdout.strip()

            git('init', '-b', 'main')
            git('config', 'user.name', 'Release Test')
            git('config', 'user.email', 'release-test@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            (root / 'go.mod').write_text('module example.invalid/library\n')
            (root / 'scripts').mkdir()
            (root / 'scripts/changelog.py').write_text((ROOT / 'scripts/changelog.py').read_text())
            (root / 'CHANGELOG.md').write_text('# Changelog\n\n## [Unreleased]\n')
            git('add', '.')
            git('commit', '-m', 'Initial main')
            if off_main:
                git('checkout', '-b', 'unmerged')
            (root / 'CHANGELOG.md').write_text(
                '# Changelog\n\n## [Unreleased]\n\n'
                f'## [{changelog_tag}] — 2026-10-24\n\n### Исправлено\n\n- Tagged notes.\n')
            git('add', '.')
            git('commit', '-m', 'Prepare tagged release')
            git('tag', tag)
            if off_main:
                git('checkout', 'main')
            # The workflow's main SHA differs from the checked-out historical tag.
            (root / 'CHANGELOG.md').write_text(
                '# Changelog\n\n## [Unreleased]\n\n## [v0.1.2] — 2026-10-25\n')
            git('add', '.')
            git('commit', '-m', 'Advance main')
            main_sha = git('rev-parse', 'HEAD')
            git('remote', 'add', 'origin', directory)
            git('checkout', '--detach', tag)
            (root / 'state.json').write_text(json.dumps({'exists': True, 'notes': 'Owner notes'}))
            gh = root / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + FAKE_GH)
            gh.chmod(0o755)
            env = dict(os.environ, TAG=tag, GITHUB_SHA=main_sha, FAKE_GITHUB=directory,
                       PATH=directory + os.pathsep + os.environ['PATH'])
            script = (workflow_script('The tag is a release of this module') + '\n'
                      + workflow_script('GitHub release'))
            result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', script],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=15)
            calls = root / 'calls.jsonl'
            return result, calls.exists(), (root / 'notes.md').read_text() if (root / 'notes.md').exists() else ''

    def test_manual_run_uses_tagged_metadata_after_main_has_advanced(self):
        result, published, notes = self.validate_and_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(published)
        self.assertIn('Tagged notes.', notes)

    def test_existing_release_does_not_bypass_tag_validation(self):
        for case, expected in (
                ({'off_main': True}, 'does not point to a commit on main'),
                ({'changelog_tag': 'v0.1.0'}, 'The newest section of CHANGELOG.md is not'),
                ({'tag': 'v2.1.1', 'changelog_tag': 'v2.1.1'}, 'needs the module path to end in /v2'),
                ({'tag': 'v0.01.1'}, 'is not vX.Y.Z'),
                ({'tag': 'v0.1.1-rc1'}, 'is not vX.Y.Z')):
            with self.subTest(case=case):
                result, published, _ = self.validate_and_publish(**case)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout)
                self.assertFalse(published, 'Invalid tags must never reach the GitHub release step')

    def test_dispatch_uses_main_workflow_and_explicit_tag_checkout(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertRegex(workflow, r'(?s)  workflow_dispatch:\n    inputs:\n      tag:\n.*?        required: true\n        type: string')
        self.assertIn("if: github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'", workflow)
        self.assertIn('ref: refs/tags/${{ inputs.tag || github.ref_name }}', workflow)
