import contextlib
import io
import os
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

from automation.git_sync import Git
from automation.run_sync import Gitee, failure_summary, main, reconcile


class StageDiagnosticTests(unittest.TestCase):
    @contextlib.contextmanager
    def local_reconciliation(self, fail_source=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--bare', str(root / 'fixture.git')], check=True, capture_output=True)
            git = Git(root / 'fixture.git')
            empty = git.run('mktree', data='')
            source = git.run('commit-tree', empty, data='source\n')
            git.run('update-ref', 'refs/heads/release', source)
            config_text = ('[submodule "private-entry"]\npath = Resources/private-path\n'
                           'url = https://gitee.com/private-owner/private-source.git\nbranch = release\n')
            blob = git.run('hash-object', '-w', '--stdin', data=config_text)
            index = Git(git.cwd, {**git.env, 'GIT_INDEX_FILE': str(root / 'index')})
            index.run('read-tree', empty)
            index.run('update-index', '--add', '--cacheinfo', '100644', blob, '.gitmodules')
            index.run('update-index', '--add', '--cacheinfo', '160000', source, 'Resources/private-path')
            target = git.run('commit-tree', index.run('write-tree'), data='target\n')
            git.run('update-ref', 'refs/heads/main', target)
            config = {'main_owner': 'private-owner', 'main_repo': 'private-main', 'target_branch': 'main',
                      'robot_branch': 'bot/sync', 'allowed_paths': ['Resources/private-path'],
                      'sources': ['private-owner/private-source'], 'author_name': 'Test', 'author_email': 'test@localhost'}
            original_run = Git.run

            def local_transport(instance, *args, **kwargs):
                if args[0] in {'fetch', 'ls-remote'}:
                    if fail_source and args[0] == 'ls-remote' and 'private-source' in args[2]:
                        raise subprocess.TimeoutExpired(['git', *args], 180)
                    args = tuple(str(root / 'fixture.git') if value.startswith('https://gitee.com/') else value for value in args)
                return original_run(instance, *args, **kwargs)

            with patch.object(Git, 'run', local_transport), patch.object(Gitee, 'request', side_effect=AssertionError('unexpected API write/read')):
                yield config, git

    def test_dry_run_completes_real_git_read_stages_without_writing_a_robot_branch(self):
        output = io.StringIO()
        with self.local_reconciliation() as (config, git), contextlib.redirect_stdout(output):
            self.assertEqual(reconcile(config, 'private-token', 'private-key', dry_run=True), 0)
            self.assertEqual(git.run('for-each-ref', '--format=%(refname)', 'refs/heads/bot'), '')
        self.assertIn('stage=source_ancestry status=completed source_index=1', output.getvalue())
        self.assertIn('stage=robot_head status=completed', output.getvalue())
        self.assertNotIn('stage=robot_push', output.getvalue())
        self.assertNotIn('private-', output.getvalue())

    def test_source_timeout_reports_its_config_ordinal_after_target_read_succeeds(self):
        output = io.StringIO()
        with self.local_reconciliation(fail_source=True) as (config, _), contextlib.redirect_stdout(output):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                reconcile(config, 'private-token', 'private-key', dry_run=True)
        summary = failure_summary(caught.exception)
        self.assertIn('stage=configuration_read status=completed', output.getvalue())
        for field in ['stage=source_head', 'source_index=1', 'operation=ls-remote']:
            self.assertIn(field, summary)
        self.assertNotIn('private-', output.getvalue() + summary)

    def test_configuration_failure_identifies_the_stage_without_echoing_input(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'SYNC_CONFIG': '{"private-credential": "secret-value"}'}), contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError) as caught:
                main()
        summary = failure_summary(caught.exception)
        self.assertIn('stage=configuration', summary)
        self.assertIn('status=started', output.getvalue())
        self.assertIn('status=failed', output.getvalue())
        self.assertNotIn('secret-value', output.getvalue() + summary)

    def test_fetch_timeout_identifies_phase_operation_and_limit(self):
        config = {'main_owner': 'private-owner', 'main_repo': 'private-repo',
                  'target_branch': 'private-branch', 'robot_branch': 'bot/sync',
                  'author_name': 'Test', 'author_email': 'test@localhost'}
        original_run = Git.run

        def network_timeout(git, *args, **kwargs):
            if args[0] == 'fetch':
                raise subprocess.TimeoutExpired(['git', *args], 180, stderr=b'private-network-details')
            return original_run(git, *args, **kwargs)

        output = io.StringIO()
        with patch.object(Git, 'run', network_timeout), contextlib.redirect_stdout(output):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                reconcile(config, 'private-token', 'private-key', dry_run=True)
        summary = failure_summary(caught.exception)
        for field in ['stage=target_fetch', 'operation=fetch', 'category=timeout', 'timeout_seconds=180', 'elapsed_seconds=']:
            self.assertIn(field, summary)
        self.assertIn('stage=repository_init status=completed', output.getvalue())
        self.assertIn('stage=target_fetch status=failed', output.getvalue())
        for value in ['private-owner', 'private-repo', 'private-branch', 'private-token', 'private-key', 'private-network-details']:
            self.assertNotIn(value, output.getvalue() + summary)

    def test_api_http_failure_reports_method_and_status_without_url_or_body(self):
        error = urllib.error.HTTPError('https://gitee.com/private-repo?secret=private-token', 403, 'private-reason', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                Gitee('private-token').request('GET', '/private-repo')
        summary = failure_summary(caught.exception)
        for field in ['operation=api_get', 'http_status=403', 'category=authentication']:
            self.assertIn(field, summary)
        self.assertNotIn('private-', summary)

    def test_api_connection_failure_has_a_safe_network_category(self):
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('private-network-address')):
            with self.assertRaises(RuntimeError) as caught:
                Gitee('private-token').request('PATCH', '/private-repo', {'body': 'private-body'})
        summary = failure_summary(caught.exception)
        self.assertIn('operation=api_patch', summary)
        self.assertIn('category=network', summary)
        self.assertNotIn('private-', summary)


if __name__ == '__main__':
    unittest.main()
