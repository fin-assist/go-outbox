#!/usr/bin/env python3
"""Trusted default-branch controller for hosted Renovate, without merge bypasses."""
import argparse
import base64
import fnmatch
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
from renovate_policy_data import COMPOSE_PACKAGES, NO_DIGEST, HAS_DIGEST

BOT_ID = 29139614
BOT = 'renovate[bot]'
POLICY_BRANCH = 'renovate-policy'
MARKER = '<!-- fin211-state '
UPDATES = '<!-- fin211-updates '
COMPACT_UPDATE = '<!-- fin211-update '
PER_UPDATE_NOTE = "<!-- fin211-update {{{encodeBase64 (toJSON (toObject 'depName' depName 'packageName' packageName 'newValue' newValue 'datasource' datasource 'newDigest' newDigest 'newMajor' newMajor))}}} -->"
METADATA_TEMPLATE = '{{#each upgrades}}' + PER_UPDATE_NOTE + '\n{{/each}}'


def selected_base(branches, merged, ancestor):
    """A release is retired only when BOTH the merge PR and ancestry prove it."""
    versions = []
    for name in branches:
        match = re.fullmatch(r'release/(\d+)\.(\d+)\.(\d+)', name)
        if match:
            versions.append((tuple(map(int, match.groups())), name))
    for _, name in sorted(versions, reverse=True):
        if not ((merged(name) if callable(merged) else name in merged) and ancestor(name)):
            return name
    return 'main'


def updates(body):
    markers = re.findall(re.escape(COMPACT_UPDATE) + r'(.+?) -->', body or '')
    if markers:
        if len(markers) > 100:
            raise ValueError('Oversized Renovate metadata')
        data = [json.loads(base64.b64decode(marker, validate=True)) for marker in markers]
    else:
        # Parse legacy data for compatibility with saved fixtures; production uses
        # compact markers in the signed head commit, never the editable PR body.
        match = re.search(re.escape(UPDATES) + r'(.+?) -->', body or '')
        if not match:
            return []
        data = json.loads(base64.b64decode(match[1], validate=True))
    if not isinstance(data, list) or not 0 < len(data) <= 100:
        raise ValueError('Missing or oversized Renovate metadata')
    result = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError('Invalid upgrade')
        entry = {key: item.get(key) for key in ('depName', 'newValue', 'datasource')}
        entry['depName'] = item.get('packageName') or entry['depName']
        if type(item.get('newMajor')) is int and item['newMajor'] >= 0:
            entry['newMajor'] = item['newMajor']
        digest = item.get('newDigest')
        if digest is not None:
            entry['newDigest'] = digest
        mandatory = [entry['depName'], entry['datasource'], digest or entry['newValue']]
        if not all(isinstance(v, str) and 0 < len(v) < 512 for v in mandatory):
            raise ValueError('Invalid upgrade identity')
        if entry['newValue'] is not None and not isinstance(entry['newValue'], str):
            raise ValueError('Invalid upgrade value')
        result.append(entry)
    return result



def compose_package(name):
    return name.startswith(('androidx.', 'org.jetbrains.compose',
                            'org.jetbrains.kotlin.plugin.compose',
                            'org.jetbrains.kotlin:compose-compiler-gradle-plugin'))


def compose_update(items):
    return any(compose_package(item['depName']) for item in items)


def compose_stack_member(item):
    return any(fnmatch.fnmatchcase(item['depName'], pattern) for pattern in COMPOSE_PACKAGES)


def compose_stack_rule(generation):
    return {
        'description': 'FIN-211: failed Compose stack requires grouped Dashboard approval',
        'matchPackageNames': COMPOSE_PACKAGES,
        'groupName': 'androidx compose stack', 'separateMajorMinor': False,
        'dependencyDashboardApproval': True, 'recreateWhen': 'never',
        'additionalBranchPrefix': f'fin211-retry-{generation}-', 'automerge': False,
    }


def quarantine_rules(items, generation):
    # Exact proposed value, including a major's full version, never an entire major.
    rules = []
    for item in items:
        rule = {
            'description': 'FIN-211: failed twice; retry explicitly in Dependency Dashboard',
            'matchPackageNames': [item['depName']],
            'matchDatasources': [item['datasource']],
            'dependencyDashboardApproval': True,
            'recreateWhen': 'never',
            'groupName': None,
            'additionalBranchPrefix': f'fin211-retry-{generation}-',
        }
        if item.get('newDigest'):
            # A moving tag must not quarantine a future fixed digest of that tag.
            rule['matchJsonata'] = ['newDigest = ' + json.dumps(item['newDigest'])]
        else:
            rule['matchNewValue'] = '/^' + re.escape(item['newValue']) + '$/'
            rule['matchJsonata'] = [NO_DIGEST]
        rules.append(rule)
    return rules



def recovery_rules(items, generation):
    """Recover future fixes only in an automatically failed dependency family."""
    rules = []
    for item in items:
        rule = {
            'description': 'FIN-211: recovery of an automatically failed dependency family',
            'matchPackageNames': [item['depName']],
            'matchDatasources': [item['datasource']],
            'recreateWhen': 'always',
        }
        major = item.get('newMajor')
        if type(major) is not int and item.get('newDigest') and item.get('newValue'):
            # Digest lookup has no newMajor: a numeric tag is still a tag.
            rule['matchNewValue'] = '/^' + re.escape(item['newValue']) + '$/'
            rule['matchJsonata'] = [HAS_DIGEST]
            rules.append(rule)
            continue
        if type(major) is not int:
            match = re.match(r'^[~^=v ]*(\d+)(?:\.|$)', item.get('newValue') or '')
            major = int(match[1]) if match else None
        if major is not None:
            rule['matchJsonata'] = ['newMajor = ' + str(major)]
        else:
            # Opaque non-major values use their version-specific default PR keys.
            continue
        rules.append(rule)
    return rules


def trusted(pr, commits, repo):
    return (pr.get('state') == 'open' and not pr.get('draft')
            and pr.get('user', {}).get('id') == BOT_ID
            and pr.get('user', {}).get('login') == BOT
            and (pr.get('head', {}).get('repo') or {}).get('full_name') == repo
            and pr.get('head', {}).get('ref', '').startswith('renovate/')
            and bool(commits) and len(commits) == pr.get('commits') and len(commits) <= 250
            and all((c.get('author') or {}).get('id') == BOT_ID
                    and c.get('commit', {}).get('verification', {}).get('verified') is True
                    and c.get('_trusted_signer') is True
                    for c in commits))


def terminal_checks(jobs, names):
    """Only complete, unambiguous required jobs permit a decision."""
    by_name = {}
    for job in jobs:
        if job['name'] in by_name:
            return None
        by_name[job['name']] = job
    if not set(names) <= by_name.keys():
        return None
    required = [by_name[name] for name in names]
    if any(j['status'] != 'completed' or j['conclusion'] not in ('success', 'failure') for j in required):
        return None
    return required


class GitHub:
    def __init__(self, token, repo, read_token=None):
        self.token, self.repo, self.read_token = token, repo, read_token
        self.controller_login = None

    def request(self, method, path, data=None):
        url = 'https://api.github.com' + path if path.startswith('/') else 'https://api.github.com/repos/' + self.repo + ('/' + path if path else '')
        token = self.read_token if method == 'GET' and '/check-runs' in path else self.token
        if not token:
            raise ValueError('Read-only checks token is required')
        req = urllib.request.Request(url, method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def get(self, path):
        return self.request('GET', path)

    def pages(self, path, key=None):
        result = []
        for page in range(1, 101):
            data = self.get(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            values = data[key] if key else data
            result.extend(values)
            if len(values) < 100:
                return result
        raise ValueError('Pagination limit exceeded; refusing partial evidence')

    def base(self):
        branches = [x['name'] for x in self.pages('branches')]
        def merged(branch):
            head = urllib.parse.quote(self.repo.split('/')[0] + ':' + branch, safe='')
            return any(item.get('merged_at') and item['head']['ref'] == branch
                       and (item['head'].get('repo') or {}).get('full_name') == self.repo
                       for item in self.pages('pulls?state=closed&base=main&head=' + head))
        def ancestor(branch):
            # compare(base=release, head=main) behind/ahead semantics: release ancestor
            # iff there are zero commits exclusive to release (behind_by == 0).
            value = self.get('compare/' + urllib.parse.quote(branch, safe='') + '...main')
            return value['behind_by'] == 0
        return selected_base(branches, merged, ancestor)

    def sync_baseline(self, base):
        # Read approved JSON from a pinned target commit, never execute target code.
        branch = self.get('branches/' + urllib.parse.quote(base, safe=''))
        try:
            source = self.get('contents/renovate-base.json?ref=' + branch['commit']['sha'])
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f'Target branch {base} has no renovate-base.json; add the reviewed baseline before activation') from error
            raise
        baseline = base64.b64decode(source['content'])
        value = json.loads(baseline)  # Never execute target JSON.
        if self.repo == 'fin-assist/android':
            stack = [rule for rule in value.get('packageRules', []) if rule.get('groupName') == 'androidx compose stack']
            members = stack[0].get('matchPackageNames') if len(stack) == 1 else None
            if (len(stack) != 1 or not isinstance(members, list)
                    or len(members) != len(COMPOSE_PACKAGES) or not all(isinstance(name, str) for name in members)
                    or set(members) != set(COMPOSE_PACKAGES)
                    or stack[0].get('separateMajorMinor') is not False):
                raise ValueError('Target Compose baseline differs from trusted controller; owner must coordinate code/preset migration before sync')
        current = self.get('contents/base.json?ref=' + POLICY_BRANCH)
        if baseline != base64.b64decode(current['content']):
            self.request('PUT', 'contents/base.json', {
                'branch': POLICY_BRANCH, 'sha': current['sha'],
                'message': 'Sync approved Renovate baseline from target branch',
                'content': base64.b64encode(baseline).decode()})

    def read_receipts(self):
        try:
            obj = self.get('contents/state.json?ref=' + POLICY_BRANCH)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError('Protected state.json missing; explicit owner initialization required') from error
            raise
        data = json.loads(base64.b64decode(obj['content']))
        validate_receipts(data)
        return data, obj['sha']

    def write_receipts(self, data, sha):
        validate_receipts(data)
        payload = {'branch': POLICY_BRANCH, 'message': 'Persist immutable Renovate retry receipts',
                   'content': base64.b64encode((json.dumps(data, indent=2) + '\n').encode()).decode()}
        if sha is not None:
            payload['sha'] = sha
        response = self.request('PUT', 'contents/state.json', payload)
        return response['content']['sha']

    def bootstrap(self):
        try:
            self.get('git/ref/heads/' + POLICY_BRANCH)
            raise ValueError('Policy branch already exists; refusing to overwrite')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        policy = {'baseBranchPatterns': [self.base()], 'packageRules': []}
        tree = self.request('POST', 'git/trees', {'tree': [{'path': 'policy.json', 'mode': '100644', 'type': 'blob', 'content': json.dumps(policy, indent=2)+'\n'}, {'path': 'base.json', 'mode': '100644', 'type': 'blob', 'content': Path('renovate-base.json').read_text()}, {'path': 'state.json', 'mode': '100644', 'type': 'blob', 'content': '{}\n'}]})
        commit = self.request('POST', 'git/commits', {'message': 'Initialize generated Renovate policy', 'tree': tree['sha'], 'parents': []})
        self.request('POST', 'git/refs', {'ref': 'refs/heads/' + POLICY_BRANCH, 'sha': commit['sha']})

    def read_policy(self):
        try:
            obj = self.get('contents/policy.json?ref=' + POLICY_BRANCH)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError('Policy branch not bootstrapped; see docs/renovate-policy.md') from error
            raise
        from renovate_policy_data import validate_policy
        return validate_policy(json.loads(base64.b64decode(obj['content']))), obj['sha']

    def write_policy(self, policy, sha):
        from renovate_policy_data import validate_policy
        validate_policy(policy)
        self.request('PUT', 'contents/policy.json', {
            'branch': POLICY_BRANCH, 'sha': sha,
            'message': 'Update Renovate target and failed-version approvals',
            'content': base64.b64encode((json.dumps(policy, indent=2) + '\n').encode()).decode()})

    def signed_commits(self, number):
        commits = self.pages(f'pulls/{number}/commits')
        for commit in commits:
            query = {"query": "query($owner:String!,$name:String!,$sha:GitObjectID!){repository(owner:$owner,name:$name){object(oid:$sha){... on Commit{signature{isValid signer{login}}}}}}", "variables": dict(zip(('owner','name'), self.repo.split('/')), sha=commit['sha'])}
            req = urllib.request.Request('https://api.github.com/graphql', method='POST',
                    data=json.dumps(query).encode(), headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.load(response)
            if not isinstance(data, dict) or data.get('errors'):
                raise ValueError('GitHub signature query failed; owner inspection required')
            result = data.get('data')
            repository = result.get('repository') if isinstance(result, dict) else None
            obj = repository.get('object') if isinstance(repository, dict) else None
            if not isinstance(obj, dict) or 'signature' not in obj:
                raise ValueError('GitHub signature query returned missing or invalid data')
            signature = obj['signature']
            if signature is not None:
                if (not isinstance(signature, dict) or type(signature.get('isValid')) is not bool
                        or 'signer' not in signature
                        or (signature['signer'] is not None
                            and (not isinstance(signature['signer'], dict)
                                 or not isinstance(signature['signer'].get('login'), str)
                                 or not signature['signer']['login']))):
                    raise ValueError('GitHub signature query returned missing or invalid data')
            signature = signature or {}
            committer = (commit.get('committer') or {}).get('login')
            signer = (signature.get('signer') or {}).get('login')
            commit['_trusted_signer'] = signature.get('isValid') is True and signer == committer and signer in ('web-flow', BOT)
        return commits

    def current_run(self, pr, workflow='ci.yml'):
        runs = self.pages('actions/workflows/' + urllib.parse.quote(workflow, safe='') + '/runs?event=pull_request&head_sha=' + pr['head']['sha'], 'workflow_runs')
        # A run from another PR, base, or stale head must never count.
        runs = [r for r in runs if r['head_sha'] == pr['head']['sha'] and
                any(p['number'] == pr['number'] and p['base']['ref'] == pr['base']['ref'] for p in r['pull_requests'])]
        return max(runs, key=lambda r: r['id']) if runs else None

    def current_runs(self, pr, workflows):
        return {workflow: self.current_run(pr, workflow) for workflow in workflows}

    def workflow_jobs(self, run, names):
        """Keep the latest execution of each job; earlier attempts may supply successes only."""
        selected = {}
        attempt = run['run_attempt']
        if type(attempt) is not int or not 1 <= attempt <= 50:
            return []
        for number in range(attempt, 0, -1):
            jobs = self.pages(f"actions/runs/{run['id']}/attempts/{number}/jobs", 'jobs')
            seen = set()
            for job in jobs:
                name = job['name']
                if name not in names:
                    continue
                if (name in seen or job.get('run_id', run['id']) != run['id']
                        or job.get('head_sha', run['head_sha']) != run['head_sha']):
                    return []
                seen.add(name)
                if name not in selected:
                    # Never reuse an old failure as evidence of a failed retry.
                    if number != attempt and job.get('conclusion') != 'success':
                        return []
                    selected[name] = dict(job, _run_id=run['id'], _run_attempt=number)
            if set(names) <= selected.keys():
                return list(selected.values())
        return []

    def fresh(self, number, sha, base, runs=None):
        pr = self.get(f'pulls/{number}')
        commits = self.signed_commits(number)
        if not trusted(pr, commits, self.repo) or pr['head']['sha'] != sha or pr['base']['ref'] != base or self.base() != base:
            return None
        if runs:
            current = self.current_runs(pr, runs)
            if any(not current.get(name) or any(current[name].get(k) != run.get(k)
                   for k in ('id', 'run_attempt', 'head_sha', 'status', 'conclusion'))
                   for name, run in runs.items()):
                return None
        return pr

    def android_stack_changed(self, pr):
        if self.repo != 'fin-assist/android':
            return False
        files = self.pages(f"pulls/{pr['number']}/files")
        for item in files:
            if re.search(r'androidx[.:]|org\.jetbrains\.(?:compose|kotlin\.plugin\.compose)|compose-compiler-gradle-plugin', item.get('patch', '')):
                return True
        if not any(f['filename'] == 'gradle/libs.versions.toml' for f in files):
            return False
        import tomllib
        catalogs = []
        for ref in (pr['base']['sha'], pr['head']['sha']):
            content = self.get('contents/gradle/libs.versions.toml?ref=' + ref)
            catalogs.append(tomllib.loads(base64.b64decode(content['content']).decode()))
        def stack(catalog):
            result = {}
            for section in ('libraries', 'plugins'):
                for key, item in catalog.get(section, {}).items():
                    if isinstance(item, str):
                        name, _, version = item.rpartition(':')
                        if section == 'libraries':
                            name = ':'.join(item.split(':')[:2])
                    elif isinstance(item, dict):
                        name = item.get('id', '') if section == 'plugins' else item.get('module', item.get('group', '') + ':' + item.get('name', ''))
                        version = item.get('version')
                        if isinstance(version, dict) and 'ref' in version:
                            version = catalog.get('versions', {}).get(version['ref'])
                    else:
                        continue
                    if compose_package(name):
                        result[(section, key)] = (name, version)
            return result
        return stack(catalogs[0]) != stack(catalogs[1])

    def native_checks(self, base, checks, statuses, jobs):
        rule = self.get('branches/' + urllib.parse.quote(base, safe='') + '/protection')
        status_rule = rule.get('required_status_checks') or {}
        requirements = status_rule.get('checks') or [{'context': c, 'app_id': None} for c in status_rule.get('contexts', [])]
        if not requirements:
            return None
        result = []
        for required in requirements:
            name, app = required['context'], required.get('app_id')
            # A push workflow may publish a skipped check with the same name.
            # Required CI evidence comes from the exact pull_request run attempt.
            matches = [c for c in jobs if c['name'] == name] if app in (None, -1, 15368) else []
            if not matches:
                matches = [c for c in checks if c['name'] == name
                           and (app in (None, -1) or c.get('app', {}).get('id') == app)]
            states = [dict(status='completed', conclusion=s['state']) for s in statuses
                      if s['context'] == name] if app in (None, -1) else []
            evidence = matches + states
            if not evidence or any(c['status'] != 'completed' or c['conclusion'] not in ('success', 'failure') for c in evidence):
                return None
            result.extend(evidence)
        return result

    def merge_method(self):
        settings = self.get('')
        for key, method in [('allow_squash_merge', 'squash'), ('allow_merge_commit', 'merge'), ('allow_rebase_merge', 'rebase')]:
            if settings.get(key) is True:
                return method
        raise ValueError('Repository permits no supported merge method')

    def identity(self):
        return self.get('/user').get('login')

    def policy_protected(self, login):
        rule = self.get('branches/' + POLICY_BRANCH + '/protection')
        restrictions = rule.get('restrictions') or {}
        users = [user.get('login') for user in restrictions.get('users', [])]
        return (rule.get('enforce_admins', {}).get('enabled') is True
                and users == [login] and restrictions.get('teams') == []
                and restrictions.get('apps') == []
                and rule.get('allow_force_pushes', {}).get('enabled') is False
                and rule.get('allow_deletions', {}).get('enabled') is False)

    def protection(self, base, names):
        # Refuse unprotected branches and missing checks. No admin or ruleset bypass.
        rule = self.get('branches/' + urllib.parse.quote(base, safe='') + '/protection')
        checks = rule.get('required_status_checks') or {}
        classic = (rule.get('enforce_admins', {}).get('enabled') is True
                   and checks.get('strict') is True and set(names) <= set(checks.get('contexts', [])))
        restrictions = rule.get('restrictions') or {}
        owned = (self.controller_login is not None
                 and [user.get('login') for user in restrictions.get('users', [])] == [self.controller_login]
                 and restrictions.get('teams') == [] and restrictions.get('apps') == []
                 and (not base.startswith('release/') or rule.get('block_creations', {}).get('enabled') is True))
        if not classic or not owned:
            return False
        # Until explicitly supported, active inherited/repository rulesets may
        # grant our owner actor bypass of additional requirements. Fail closed.
        return all(rule.get('enforcement') in ('disabled', 'evaluate')
                   for rule in self.pages('rulesets?includes_parents=true'))


def retry_decision(runs, state, sha):
    """Claim once per head; only the exact retry of every failed run proves closure."""
    if any(run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure')
           for run in runs.values()):
        return {'action': 'wait'}
    failed = {name: run for name, run in runs.items() if run['conclusion'] == 'failure'}
    if not failed:
        return {'action': 'wait'}
    if state is not None:
        if not isinstance(state, dict) or not isinstance(state.get('sha'), str):
            return {'action': 'wait'}
        if state['sha'] == sha:
            claimed = state.get('runs')
            if not isinstance(claimed, dict) or claimed.keys() != runs.keys():
                return {'action': 'wait'}
            for name, run in runs.items():
                claim = claimed[name]
                if (not isinstance(claim, dict) or type(claim.get('attempt')) is not int
                        or type(claim.get('run_id')) is not int
                        or type(claim.get('retried')) is not bool
                        or claim['attempt'] < 1 or claim['run_id'] != run['id']
                        or run['run_attempt'] != claim['attempt'] + int(claim['retried'])):
                    return {'action': 'wait'}
            # A failure of an un-retried workflow is not evidence of our retry.
            if any(not claimed[name]['retried'] for name in failed):
                return {'action': 'wait'}
            return {'action': 'close'}
    return {'action': 'retry', 'state': {'sha': sha, 'runs': {
        name: {'run_id': run['id'], 'attempt': run['run_attempt'], 'retried': name in failed}
        for name, run in runs.items()}}}


def validate_receipts(data):
    if not isinstance(data, dict) or len(json.dumps(data)) > 1000000:
        raise ValueError('Invalid or oversized protected retry state; owner retention review required')
    for key, receipt in data.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]*:[A-Za-z0-9]{1,128}', key)
                or not isinstance(receipt, dict) or set(receipt) - {'state', 'comment_id'}
                or not isinstance(receipt.get('state'), dict)):
            raise ValueError('Invalid protected retry receipt')
        state = receipt['state']
        if set(state) != {'sha', 'runs'} or key.split(':', 1)[1] != state['sha'] or not isinstance(state['runs'], dict) or not state['runs']:
            raise ValueError('Invalid protected retry state')
        for workflow, run in state['runs'].items():
            if (workflow not in ('ci.yml', 'renovate-policy-tests.yml') or not isinstance(run, dict)
                    or set(run) != {'run_id', 'attempt', 'retried'}
                    or type(run['run_id']) is not int or run['run_id'] < 1
                    or type(run['attempt']) is not int or run['attempt'] < 1
                    or type(run['retried']) is not bool):
                raise ValueError('Invalid protected run receipt')
        if 'comment_id' in receipt and (type(receipt['comment_id']) is not int or receipt['comment_id'] < 1):
            raise ValueError('Invalid protected comment identity')


def explain_spent_retry(api, config, number, key, receipt, body):
    """Repair an informational PR notice; comments never change retry authority."""
    comments = api.pages(f'issues/{number}/comments')
    comment = next((item for item in comments if item.get('id') == receipt.get('comment_id')), None)
    if comment is not None:
        if comment.get('body') != body:
            api.request('PATCH', f"issues/comments/{comment['id']}", {'body': body})
        return
    comment = next((item for item in comments if (item.get('user') or {}).get('login') == config['controllerLogin']
                    and item.get('body', '').startswith(body.split(' -->', 1)[0] + ' -->')), None)
    if comment is None:
        comment = api.request('POST', f'issues/{number}/comments', {'body': body})
    elif comment.get('body') != body:
        api.request('PATCH', f"issues/comments/{comment['id']}", {'body': body})
    latest, latest_sha = api.read_receipts()
    if latest.get(key, {}).get('state') != receipt['state']:
        raise ValueError('Protected retry receipt changed during owner notice repair')
    latest[key]['comment_id'] = comment['id']
    api.write_receipts(latest, latest_sha)


def reconcile(api, config, trigger=None, mutate_prs=True):
    if api.identity() != config['controllerLogin']:
        raise ValueError('Mutation credential identity differs from controllerLogin; no retry claim is allowed')
    api.controller_login = config['controllerLogin']
    if not api.policy_protected(config['controllerLogin']):
        raise ValueError('Generated preset branch must restrict pushes to the controller identity, with admins enforced and force/delete disabled')
    base = api.base()
    policy, policy_sha = api.read_policy()
    if not api.protection(base, config['requiredChecks']):
        raise ValueError('Target branch must enforce required CI and administrator protection before preset sync')
    api.sync_baseline(base)
    if policy.get('baseBranchPatterns') != [base]:
        policy['baseBranchPatterns'] = [base]
        api.write_policy(policy, policy_sha)
        policy, policy_sha = api.read_policy()
    if not mutate_prs:
        print('Renovate PR mutations disabled; configuration synchronized')
        return
    failures = []
    for candidate in api.pages('pulls?state=open'):
        try:
            reconcile_pr(api, config, base, candidate, trigger)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, KeyError, AttributeError, TypeError, OSError) as error:
            number = candidate.get('number', '?')
            print(f'PR {number}: {type(error).__name__}; owner inspection required; continuing other PRs')
            failures.append(str(number))
    if failures:
        raise ValueError('Renovate reconciliation failed for PRs: ' + ', '.join(failures))


def reconcile_pr(api, config, base, candidate, trigger):
    if candidate.get('user', {}).get('id') != BOT_ID:
        return
    number = candidate['number']
    pr = api.get(f'pulls/{number}')
    commits = api.signed_commits(number)
    if not trusted(pr, commits, api.repo) or pr['base']['ref'] != base:
        return
    sha = pr['head']['sha']
    try:
        head = next((commit for commit in commits if commit.get('sha') == sha), None)
        items = updates(head.get('commit', {}).get('message')) if head else []
    except (ValueError, TypeError):
        print(f'PR {number}: invalid metadata; owner review required')
        return
    # Older PRs without structured metadata remain for the owner; never guess.
    if not items:
        return
    workflow_checks = {'ci.yml': config.get('ciChecks', [name for name in config['requiredChecks']
                                                            if name not in ('Renovate policy tests', 'Renovate runtime contract')])}
    if 'Renovate policy tests' in config['requiredChecks']:
        workflow_checks['renovate-policy-tests.yml'] = [name for name in ('Renovate policy tests', 'Renovate runtime contract') if name in config['requiredChecks']]
    runs = api.current_runs(pr, workflow_checks)
    if any(not run for run in runs.values()):
        return
    if trigger and not any(all(trigger.get(k) == run.get(k)
                               for k in ('id', 'run_attempt', 'head_sha')) for run in runs.values()):
        return
    if not api.protection(base, config['requiredChecks']):
        return
    jobs, required, verdicts = [], [], {}
    complete = True
    for workflow, run in runs.items():
        current_jobs = api.workflow_jobs(run, workflow_checks[workflow])
        current_required = terminal_checks(current_jobs, workflow_checks[workflow])
        if (run['head_sha'] != sha or run['status'] != 'completed'
                or run['conclusion'] not in ('success', 'failure') or not current_required):
            complete = False
            break
        jobs.extend(current_jobs)
        required.extend(current_required)
        verdicts[workflow] = dict(run, conclusion='failure' if any(job['conclusion'] == 'failure' for job in current_required) else 'success')
    if not complete:
        return
    all_checks = api.pages(f'commits/{sha}/check-runs', 'check_runs')
    all_statuses = api.get(f'commits/{sha}/status')
    native = api.native_checks(base, all_checks, all_statuses['statuses'], jobs)
    if not native:
        return
    receipts, receipt_sha = api.read_receipts()
    receipt_key = str(number) + ':' + sha
    receipt = receipts.get(receipt_key)
    state = receipt['state'] if receipt else None
    if all(j['conclusion'] == 'success' for j in required):
        if state is not None:
            claims = state.get('runs') if isinstance(state, dict) else None
            if (not isinstance(state, dict) or state.get('sha') != sha or not isinstance(claims, dict) or claims.keys() != runs.keys()
                    or any(not isinstance(claims[name], dict)
                           or claims[name].get('run_id') != run['id']
                           or type(claims[name].get('attempt')) is not int
                           or type(claims[name].get('retried')) is not bool
                           or run['run_attempt'] != claims[name]['attempt'] + int(claims[name]['retried'])
                           for name, run in runs.items())):
                return  # A manual extra attempt/new run stays with the owner.
        if compose_update(items) or api.android_stack_changed(pr):
            return
        # All native required checks/statuses must be green.
        if any(c['conclusion'] != 'success' for c in native):
            return
        merge_method = api.merge_method()
        current = api.fresh(number, sha, base, runs)
        if current and current.get('mergeable') is True and current.get('mergeable_state') in ('clean', 'unstable'):
            api.request('PUT', f'pulls/{number}/merge', {'sha': sha, 'merge_method': merge_method})
        return
    decision = retry_decision(verdicts, state, sha)
    if decision['action'] == 'wait' and state is not None and state.get('sha') == sha:
        # A persisted budget must not turn a failed/unknown dispatch into a
        # permanently green no-op. Never reset it or spend a second retry.
        claims = state.get('runs', {})
        if isinstance(claims, dict) and any(isinstance(claims.get(name), dict) and claims[name].get('retried') is True
               and claims[name].get('run_id') == run['id']
               and claims[name].get('attempt') == run['run_attempt']
               and run['status'] == 'completed' and run['conclusion'] == 'failure'
               for name, run in verdicts.items()):
            print(f'PR {number}: claimed retry has not advanced; owner must inspect Actions; budget retained')
            body = MARKER + json.dumps(state) + ' -->\nАвтоматический retry ещё не подтверждён новым attempt; dispatch неизвестен или не отправлен. Бюджет сохранён. Владелец: проверьте Actions и при необходимости вручную выберите Re-run failed jobs. Автоматический второй retry запрещён.'
            explain_spent_retry(api, config, number, receipt_key, receipt, body)
            raise ValueError('Claimed retry has not advanced; owner inspection required')
    if decision['action'] == 'retry' and api.fresh(number, sha, base, runs):
        # Claim before calling rerun: event redelivery never spends a second retry.
        body = MARKER + json.dumps(decision['state']) + ' -->\nОдин автоматический повтор упавших проверок CI для SHA `' + sha + '`. Cancelled/skipped/timed_out и дополнительные ручные attempts оставляют PR владельцу без автоматического закрытия; проверьте Actions перед ручным повтором.'
        # Protected state is authority. Comments are editable/deletable UX only.
        receipts[receipt_key] = {'state': decision['state']}
        receipt_sha = api.write_receipts(receipts, receipt_sha)
        claim = api.request('POST', f'issues/{number}/comments', {'body': body})
        if (claim.get('user') or {}).get('login') != config['controllerLogin']:
            raise ValueError('Retry claim author differs from authenticated controller identity')
        receipts[receipt_key]['comment_id'] = claim['id']
        api.write_receipts(receipts, receipt_sha)
        try:
            dispatched = set()
            for workflow, run in runs.items():
                if verdicts[workflow]['conclusion'] == 'failure':
                    # Already dispatched runs may now be queued/in progress.
                    # Every remaining run must still match the claimed snapshot.
                    remaining = {name: value for name, value in runs.items() if name not in dispatched}
                    if not api.fresh(number, sha, base, remaining):
                        api.request('PATCH', f"issues/comments/{claim['id']}", {'body': body + '\nОставшиеся повторы не запрошены: head, цель или run изменились после claim. Claim сохранён; владелец проверяет Actions перед ручным повтором.'})
                        break
                    api.request('POST', f"actions/runs/{run['id']}/rerun-failed-jobs")
                    dispatched.add(workflow)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            api.request('PATCH', f"issues/comments/{claim['id']}", {'body': body + '\nЗапрос повтора завершился ошибкой API; claim сохранён, чтобы не запускать дубликаты. Владелец: проверьте Actions run и при необходимости вручную выберите Re-run failed jobs. Следующая завершённая попытка будет оценена автоматически.'})
            raise
    elif decision['action'] == 'close' and api.fresh(number, sha, base, runs):
        stack_failed = api.repo == 'fin-assist/android' and any(compose_stack_member(item) for item in items)
        ordinary_items = [item for item in items if not (stack_failed and compose_stack_member(item))]
        recovery = recovery_rules(ordinary_items, number)
        new_rules = quarantine_rules(ordinary_items, number)
        if stack_failed:
            new_rules.append(compose_stack_rule(number))
        policy, policy_sha = api.read_policy()
        rules = policy.setdefault('packageRules', [])
        for rule in recovery + new_rules:
            def identity(value):
                return tuple(value.get(key) for key in ('description', 'matchPackageNames', 'matchDatasources', 'matchNewValue', 'matchJsonata'))
            def generation(value):
                match = re.fullmatch(r'fin211-retry-([0-9]+)-', value.get('additionalBranchPrefix', ''))
                return int(match[1]) if match else 0
            matching = [old for old in rules if identity(old) == identity(rule)]
            # Reopened/late old PRs must never restore an already consumed prefix.
            if any(generation(old) > generation(rule) for old in matching):
                continue
            rules[:] = [old for old in rules if identity(old) != identity(rule)]
            rules.append(rule)
        # Exact failed values override family recreation.
        rules.sort(key=lambda rule: rule.get('description') != 'FIN-211: recovery of an automatically failed dependency family')
        # Persist version-scoped approval BEFORE closing, including major/groups.
        api.write_policy(policy, policy_sha)
        current = api.fresh(number, sha, base, runs)
        if not current:
            return
        failed = '\n'.join('- [' + j['name'] + '](' + j['html_url'] + ')' for j in required if j['conclusion'] == 'failure')
        body = (MARKER + json.dumps(dict(state, closed=True)) + ' -->\n'
                + 'CI повторно упал на SHA `' + sha + '`. Попытки: ' + ', '.join(name + '=' + str(value['run_attempt']) for name, value in runs.items()) + '.\n' + failed
                + '\nПовторное падение подтверждено; запрошено автоматическое закрытие. Контроллер ветку не удаляет. Повтор: отметьте обновление в Closed/Ignored или Pending Approval в Dependency Dashboard. '
                + ('Стек FIN-184 остаётся одной видимой Pending Approval группой: будущие версии требуют совместного Dashboard approval и ручной UI-проверки. ' if stack_failed else 'Новые значения версий (включая исправления безопасности) не блокируются этим ограничением; та же версия требует явного подтверждения. ')
                + 'См. docs/renovate-policy.md.')
        # A collaborator may edit/delete comments; neither changes the receipt.
        comment_id = receipt.get('comment_id')
        comments = api.pages(f'issues/{number}/comments')
        if any(comment.get('id') == comment_id for comment in comments):
            api.request('PATCH', f'issues/comments/{comment_id}', {'body': body})
        else:
            # Recover an accepted POST whose response/CAS acknowledgement was lost.
            # This is UX deduplication only; the protected receipt authorized close.
            replacement = next((comment for comment in comments
                                if (comment.get('user') or {}).get('login') == config['controllerLogin']
                                and comment.get('body') == body), None)
            if replacement is None:
                replacement = api.request('POST', f'issues/{number}/comments', {'body': body})
            latest, latest_sha = api.read_receipts()
            if latest.get(receipt_key, {}).get('state') != state:
                raise ValueError('Protected retry receipt changed during comment replacement')
            latest[receipt_key]['comment_id'] = replacement['id']
            api.write_receipts(latest, latest_sha)
        if api.fresh(number, sha, base, runs):
            api.request('PATCH', f'pulls/{number}', {'state': 'closed'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap', action='store_true', help='Publish the initial preset branch; does not enable automation')
    args = parser.parse_args()
    if args.bootstrap:
        token = os.environ.get('RENOVATE_POLICY_TOKEN')
        if not token:
            raise ValueError('RENOVATE_POLICY_TOKEN is required')
        GitHub(token, os.environ['GITHUB_REPOSITORY']).bootstrap()
        return
    token = os.environ.get('RENOVATE_POLICY_TOKEN')
    if not token:
        raise ValueError('RENOVATE_POLICY_TOKEN is required; GITHUB_TOKEN is not a merge credential')
    config = json.loads(Path('.github/renovate-policy.json').read_text())
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    trigger = event.get('workflow_run') if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run' else None
    reconcile(GitHub(token, os.environ['GITHUB_REPOSITORY'], os.environ.get('GITHUB_READ_TOKEN')), config, trigger, mutate_prs=os.environ.get('RENOVATE_POLICY_ENABLED') == 'true')


if __name__ == '__main__':
    main()
