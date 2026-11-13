"""Reconcile configured Gitee gitlinks without checking out repository code."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from contextlib import contextmanager

from automation.git_sync import Git, signed_commit, update_tree, verify_robot
from automation.reconcile import build_update_plan, parse_gitmodules

SHA = re.compile(r'^[0-9a-f]{40}$')
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
PR_MARKER = '<!-- submodule-auto-sync -->'
STAGES = {'configuration', 'runtime_secrets', 'repository_init', 'target_fetch',
          'configuration_read', 'source_config', 'source_head', 'source_ancestry',
          'update_plan', 'robot_head', 'robot_fetch', 'robot_verify',
          'write_precondition', 'update_tree', 'commit_create', 'commit_verify',
          'robot_push', 'push_verify', 'pr_sync'}


@contextmanager
def sync_stage(name, source_index=None):
    """Log fixed stage identifiers; map source ordinals using the private config."""
    if name not in STAGES or (source_index is not None and (type(source_index) is not int or source_index < 1)):
        raise ValueError('invalid diagnostic stage')
    source = '' if source_index is None else f' source_index={source_index}'
    started = time.monotonic()
    print(f'Sync stage={name} status=started{source}', flush=True)
    try:
        yield
    except Exception as error:
        elapsed = round(time.monotonic() - started, 3)
        error.sync_stage = name
        error.sync_source_index = source_index
        error.sync_elapsed_seconds = elapsed
        print(f'Sync stage={name} status=failed{source} elapsed_seconds={elapsed}', flush=True)
        raise
    else:
        elapsed = round(time.monotonic() - started, 3)
        print(f'Sync stage={name} status=completed{source} elapsed_seconds={elapsed}', flush=True)


class GiteeRequestError(RuntimeError):
    def __init__(self, method, status=None):
        super().__init__('Gitee request did not complete; inspect remote state before retrying')
        self.method = method if method in {'GET', 'POST', 'PATCH'} else 'UNKNOWN'
        self.status = status


def load_config():
    config = json.loads(os.environ.get('SYNC_CONFIG') or '{}')
    for field in ('main_owner', 'main_repo', 'target_branch', 'robot_branch', 'allowed_paths', 'sources', 'author_name', 'author_email'):
        if not config.get(field):
            raise RuntimeError('incomplete runtime configuration')
    if not REPO.fullmatch(config['main_owner'] + '/' + config['main_repo']):
        raise RuntimeError('invalid main repository configuration')
    if config['robot_branch'] == config['target_branch']:
        raise RuntimeError('robot and target branches must differ')
    for branch in (config['robot_branch'], config['target_branch']):
        subprocess.run(['git', 'check-ref-format', 'refs/heads/' + branch], check=True, capture_output=True)
    if not isinstance(config['allowed_paths'], list) or not all(isinstance(p, str) and p.startswith('Resources/') and '..' not in p.split('/') for p in config['allowed_paths']):
        raise RuntimeError('invalid gitlink allowlist')
    if not isinstance(config['sources'], list) or not all(REPO.fullmatch(source) for source in config['sources']):
        raise RuntimeError('invalid source allowlist')
    return config


def plan_from_gitmodules(text, old_gitlinks, new_gitlinks, config):
    entries = [entry for entry in parse_gitmodules(text) if entry['path'] in config['allowed_paths']]
    return build_update_plan(entries, old_gitlinks, new_gitlinks)


def pr_head(config):
    return config['main_owner'] + ':' + config['robot_branch']


def build_pr_body(plan):
    lines = [PR_MARKER, '更新指定分支的子模块引用，由维护者审核合并。', '', '| 路径 | 分支 | 原 SHA | 新 SHA |', '| --- | --- | --- | --- |']
    for item in plan:
        lines.append(f"| `{item['path']}` | `{item['branch']}` | `{item['old']}` | `{item['new']}` |")
    lines += ['', '校验：跟踪分支存在、提交为快进、仅修改允许的 gitlink。未运行产品构建。']
    return '\n'.join(lines)


def is_forward(old, new, parents):
    seen, pending = set(), [new]
    while pending:
        current = pending.pop()
        if current == old:
            return True
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > 5000:
            raise RuntimeError('ancestry verification limit reached')
        pending.extend(parents(current))
    return False


class Gitee:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, fields=None):
        headers = {'Authorization': 'token ' + self.token, 'User-Agent': 'submodule-auto-sync', 'Accept': 'application/json'}
        data = None
        if fields is not None:
            data = urllib.parse.urlencode(fields).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request('https://gitee.com/api/v5' + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise GiteeRequestError(method, error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise GiteeRequestError(method) from None

    def parents(self, repository, sha):
        if not SHA.fullmatch(sha):
            raise RuntimeError('invalid source object')
        commit = self.request('GET', '/repos/' + repository + '/commits/' + sha)
        parents = commit.get('parents')
        if not isinstance(parents, list):
            raise RuntimeError('unavailable commit ancestry')
        result = [value['sha'] if isinstance(value, dict) else value for value in parents]
        if not all(isinstance(value, str) and SHA.fullmatch(value) for value in result):
            raise RuntimeError('invalid commit ancestry')
        return result

    def pulls(self, config):
        route = '/repos/' + config['main_owner'] + '/' + config['main_repo'] + '/pulls'
        result = []
        for page in range(1, 101):
            query = urllib.parse.urlencode({'state': 'open', 'head': pr_head(config), 'base': config['target_branch'], 'per_page': 100, 'page': page})
            batch = self.request('GET', route + '?' + query)
            if not isinstance(batch, list):
                raise RuntimeError('invalid PR query response')
            for pr in batch:
                head = pr.get('head')
                base = pr.get('base')
                if isinstance(head, dict):
                    head_ref, head_repo = head.get('ref'), (head.get('repo') or {}).get('full_name', '')
                else:
                    head_ref, head_repo = head, (config['main_owner'] + '/' + config['main_repo'])
                if isinstance(base, dict):
                    base_ref = base.get('ref')
                else:
                    base_ref = base
                if head_ref == config['robot_branch'] and base_ref == config['target_branch'] and head_repo.lower() == (config['main_owner'] + '/' + config['main_repo']).lower():
                    result.append(pr)
            if len(batch) < 100:
                return result
        raise RuntimeError('PR pagination limit reached')

    def ensure_pr(self, config, plan):
        route = '/repos/' + config['main_owner'] + '/' + config['main_repo'] + '/pulls'
        existing = self.pulls(config)
        if len(existing) > 1:
            raise RuntimeError('multiple matching PRs require review')
        if existing and PR_MARKER not in (existing[0].get('body') or ''):
            raise RuntimeError('existing PR requires ownership review')
        if not plan:
            if existing:
                self.request('PATCH', route + '/' + str(existing[0]['number']), {'state': 'closed'})
            return
        fields = {'title': 'chore: update resource submodule references', 'body': build_pr_body(plan)}
        if existing:
            self.request('PATCH', route + '/' + str(existing[0]['number']), fields)
        else:
            try:
                self.request('POST', route, {**fields, 'head': pr_head(config), 'base': config['target_branch'], 'prune_source_branch': 'false'})
            except RuntimeError:
                if len(self.pulls(config)) != 1:
                    raise
        verified = self.pulls(config)
        if len(verified) != 1 or PR_MARKER not in (verified[0].get('body') or ''):
            raise RuntimeError('PR verification failed')


def remote_head(git, url, branch):
    lines = git.run('ls-remote', '--refs', url, 'refs/heads/' + branch).splitlines()
    if not lines:
        return None
    if len(lines) != 1:
        raise RuntimeError('ambiguous remote branch')
    sha, ref = lines[0].split()
    if not SHA.fullmatch(sha) or ref != 'refs/heads/' + branch:
        raise RuntimeError('invalid remote branch')
    return sha


def source_repository(url):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip('/').removesuffix('.git')
    if parsed.scheme != 'https' or parsed.netloc != 'gitee.com' or parsed.query or parsed.fragment or not REPO.fullmatch(path):
        raise RuntimeError('source URL is outside trusted Gitee repositories')
    return path


def reconcile(config, token, signing_key, dry_run=False):
    api = Gitee(token)
    with tempfile.TemporaryDirectory(prefix='gitlink-sync-') as directory:
        askpass = Path(directory) / 'askpass'
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "oauth2" ;; *) printf "%s\\n" "$GITEE_TOKEN" ;; esac\n')
        askpass.chmod(0o700)
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0', 'GITEE_TOKEN': token,
               'GIT_AUTHOR_NAME': config['author_name'], 'GIT_AUTHOR_EMAIL': config['author_email'], 'GIT_COMMITTER_NAME': config['author_name'], 'GIT_COMMITTER_EMAIL': config['author_email']}
        repo = Path(directory) / 'main.git'
        repo.mkdir()
        git = Git(repo, env)
        with sync_stage('repository_init'):
            git.run('init', '--bare')
        main_url = 'https://gitee.com/' + config['main_owner'] + '/' + config['main_repo'] + '.git'
        with sync_stage('target_fetch'):
            git.run('fetch', '--no-tags', main_url, 'refs/heads/' + config['target_branch'] + ':refs/heads/base')
        with sync_stage('configuration_read'):
            target = git.run('rev-parse', 'refs/heads/base')
            text = git.run('show', target + ':.gitmodules')
            configured = {entry['path']: entry for entry in parse_gitmodules(text) if entry['branch']}
            old = {}
            for line in git.run('ls-tree', '-r', '-z', target).split('\0'):
                if not line:
                    continue
                mode, path = line.split('\t', 1)
                if mode.startswith('160000 commit '):
                    old[path] = mode.split()[2]
        new, branches = {}, {}
        for index, path in enumerate(config['allowed_paths'], 1):
            with sync_stage('source_config', index):
                if path not in configured or path not in old:
                    raise RuntimeError('configured path lacks an explicit branch or existing gitlink')
                entry = configured[path].copy()
                entry['branch'] = config['target_branch'] if entry['branch'] == '.' else entry['branch']
                repository = source_repository(entry['url'])
                if repository not in config['sources']:
                    raise RuntimeError('source repository is outside the allowlist')
            with sync_stage('source_head', index):
                sha = remote_head(git, entry['url'], entry['branch'])
                if not sha:
                    raise RuntimeError('source branch is missing')
            with sync_stage('source_ancestry', index):
                if not is_forward(old[path], sha, lambda value: api.parents(repository, value)):
                    raise RuntimeError('source branch is not a verified fast-forward')
            new[path], branches[path] = sha, entry['branch']
        with sync_stage('update_plan'):
            plan = plan_from_gitmodules(text, old, new, config)
            for item in plan:
                item['branch'] = branches[item['path']]
        with sync_stage('robot_head'):
            bot = remote_head(git, main_url, config['robot_branch'])
        if bot:
            with sync_stage('robot_fetch'):
                git.run('fetch', '--no-tags', main_url, 'refs/heads/' + config['robot_branch'] + ':refs/heads/robot')
                if git.run('rev-parse', 'refs/heads/robot') != bot:
                    raise RuntimeError('robot branch changed during fetch; retry reconciliation')
            with sync_stage('robot_verify'):
                verify_robot(git, target, bot, config['allowed_paths'], signing_key)
        if dry_run:
            return len(plan)
        with sync_stage('write_precondition'):
            if remote_head(git, main_url, config['target_branch']) != target:
                raise RuntimeError('target branch changed; retry reconciliation')
        if not plan:
            with sync_stage('pr_sync'):
                api.ensure_pr(config, [])
            return 0
        with sync_stage('update_tree'):
            tree = update_tree(git, target, plan)
            reuse_bot = bot and git.run('rev-parse', bot + '^{tree}') == tree
        if reuse_bot:
            head = bot
        else:
            parents = [target] if not bot else [bot, target]
            with sync_stage('commit_create'):
                head = signed_commit(git, tree, parents, signing_key)
            # Expect an exact old SHA (or absence) and verify our signature before pushing.
            with sync_stage('commit_verify'):
                verify_robot(git, target, head, config['allowed_paths'], signing_key)
            with sync_stage('robot_push'):
                git.run('push', '--force-with-lease=refs/heads/' + config['robot_branch'] + ':' + (bot or ''), main_url, head + ':refs/heads/' + config['robot_branch'])
        with sync_stage('push_verify'):
            if remote_head(git, main_url, config['robot_branch']) != head:
                raise RuntimeError('pushed branch could not be verified')
        with sync_stage('pr_sync'):
            api.ensure_pr(config, plan)
        return len(plan)


def main():
    with sync_stage('configuration'):
        config = load_config()
    with sync_stage('runtime_secrets'):
        token, key = os.environ.get('GITEE_TOKEN'), os.environ.get('SYNC_SIGNING_KEY')
        if not token or not key:
            raise RuntimeError('runtime secrets are incomplete')
    count = reconcile(config, token, key, os.environ.get('DRY_RUN') == 'true')
    print('Reconciliation completed. Changed gitlinks: ' + str(count))


def failure_summary(error):
    """Expose fixed diagnostic fields without logging private command arguments."""
    detail = type(error).__name__
    if getattr(error, 'sync_stage', None) in STAGES:
        detail += f'; stage={error.sync_stage}; elapsed_seconds={error.sync_elapsed_seconds}'
        if error.sync_source_index is not None:
            detail += f'; source_index={error.sync_source_index}'
    if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        commands = {'init', 'fetch', 'ls-remote', 'show', 'ls-tree', 'rev-parse', 'check-ref-format',
                    'rev-list', 'diff', 'read-tree', 'update-index', 'write-tree',
                    'commit-tree', 'push'}
        args = error.cmd if isinstance(error.cmd, (list, tuple)) else []
        operation = next((arg for arg in args if arg in commands), 'unknown')
        stderr = error.stderr or ''
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        stderr = stderr.lower()
        category = 'other'
        if isinstance(error, subprocess.TimeoutExpired):
            category = 'timeout'
        elif any(value in stderr for value in ('authentication failed', 'could not read username', 'returned error: 401', 'returned error: 403')):
            category = 'authentication'
        elif any(value in stderr for value in ('returned error: 500', 'returned error: 502', 'returned error: 503', 'returned error: 504', 'bad gateway', 'service unavailable')):
            category = 'server'
        elif any(value in stderr for value in ('could not resolve host', 'failed to connect', 'connection timed out', 'connection reset', 'ssl_error', 'gnutls', 'rpc failed', 'early eof')):
            category = 'network'
        detail += f'; operation={operation}; category={category}'
        if isinstance(error, subprocess.TimeoutExpired):
            detail += f'; timeout_seconds={error.timeout}'
        else:
            detail += f'; exit={error.returncode}'
    elif isinstance(error, GiteeRequestError):
        category = 'network' if error.status is None else 'authentication' if error.status in (401, 403) else 'server' if error.status >= 500 else 'http'
        detail += f'; operation=api_{error.method.lower()}; category={category}'
        if error.status is not None:
            detail += f'; http_status={error.status}'
    return f'Reconciliation failed ({detail}). Inspect private repository state before retrying.'


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Repository addresses, commit IDs and HTTP bodies stay out of public logs.
        print(failure_summary(error), file=sys.stderr)
        sys.exit(1)
