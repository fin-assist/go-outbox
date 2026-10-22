#!/usr/bin/env python3
"""Trusted default-branch controller for hosted Renovate, without merge bypasses."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

BOT_ID = 29139614
BOT = 'renovate[bot]'
POLICY_BRANCH = 'renovate-policy'
MARKER = '<!-- fin211-state '
UPDATES = '<!-- fin211-updates '


def selected_base(branches, merged, ancestor):
    """A release is retired only when BOTH the merge PR and ancestry prove it."""
    versions = []
    for name in branches:
        match = re.fullmatch(r'release/(\d+)\.(\d+)\.(\d+)', name)
        if match:
            versions.append((tuple(map(int, match.groups())), name))
    for _, name in sorted(versions, reverse=True):
        if not (name in merged and ancestor(name)):
            return name
    return 'main'


def updates(body):
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


def compose_update(items):
    # Conservatively keep all AndroidX, Compose and mixed groups for the owner.
    return any(x['depName'].startswith(('androidx.', 'org.jetbrains.compose')) for x in items)


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
        if type(major) is not int:
            match = re.match(r'^[~^=v ]*(\d+)(?:\.|$)', item.get('newValue') or '')
            major = int(match[1]) if match else None
        if major is not None:
            rule['matchJsonata'] = ['newMajor = ' + str(major)]
        elif item.get('newDigest') and item.get('newValue'):
            rule['matchNewValue'] = '/^' + re.escape(item['newValue']) + '$/'
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

    def request(self, method, path, data=None):
        url = 'https://api.github.com/repos/' + self.repo + '/' + path
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
        merged = {x['head']['ref'] for x in self.pages('pulls?state=closed&base=main') if x.get('merged_at') and (x['head'].get('repo') or {}).get('full_name') == self.repo}
        def ancestor(branch):
            # compare(base=release, head=main) behind/ahead semantics: release ancestor
            # iff there are zero commits exclusive to release (behind_by == 0).
            value = self.get('compare/' + urllib.parse.quote(branch, safe='') + '...main')
            return value['behind_by'] == 0
        return selected_base(branches, merged, ancestor)

    def sync_baseline(self, base):
        # Read approved JSON from a pinned target commit, never execute target code.
        branch = self.get('branches/' + urllib.parse.quote(base, safe=''))
        source = self.get('contents/renovate-base.json?ref=' + branch['commit']['sha'])
        baseline = base64.b64decode(source['content'])
        json.loads(baseline)  # Refuse an invalid source without replacing the preset.
        current = self.get('contents/base.json?ref=' + POLICY_BRANCH)
        if baseline != base64.b64decode(current['content']):
            self.request('PUT', 'contents/base.json', {
                'branch': POLICY_BRANCH, 'sha': current['sha'],
                'message': 'Sync approved Renovate baseline from target branch',
                'content': base64.b64encode(baseline).decode()})

    def bootstrap(self):
        try:
            self.get('git/ref/heads/' + POLICY_BRANCH)
            raise ValueError('Policy branch already exists; refusing to overwrite')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        policy = {'baseBranchPatterns': [self.base()], 'packageRules': []}
        tree = self.request('POST', 'git/trees', {'tree': [{'path': 'policy.json', 'mode': '100644', 'type': 'blob', 'content': json.dumps(policy, indent=2)+'\n'}, {'path': 'base.json', 'mode': '100644', 'type': 'blob', 'content': Path('renovate-base.json').read_text()}]})
        commit = self.request('POST', 'git/commits', {'message': 'Initialize generated Renovate policy', 'tree': tree['sha'], 'parents': []})
        self.request('POST', 'git/refs', {'ref': 'refs/heads/' + POLICY_BRANCH, 'sha': commit['sha']})

    def read_policy(self):
        try:
            obj = self.get('contents/policy.json?ref=' + POLICY_BRANCH)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError('Policy branch not bootstrapped; see docs/renovate-policy.md') from error
            raise
        return json.loads(base64.b64decode(obj['content'])), obj['sha']

    def write_policy(self, policy, sha):
        self.request('PUT', 'contents/policy.json', {
            'branch': POLICY_BRANCH, 'sha': sha,
            'message': 'Update Renovate target and failed-version approvals',
            'content': base64.b64encode((json.dumps(policy, indent=2) + '\n').encode()).decode()})

    def signed_commits(self, number):
        commits = self.pages(f'pulls/{number}/commits')
        for commit in commits:
            query = {"query": "query($owner:String!,$name:String!,$sha:String!){repository(owner:$owner,name:$name){object(oid:$sha){... on Commit{signature{isValid signer{login}}}}}}", "variables": dict(zip(('owner','name'), self.repo.split('/')), sha=commit['sha'])}
            req = urllib.request.Request('https://api.github.com/graphql', method='POST',
                    data=json.dumps(query).encode(), headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.load(response)
            signature = (((data.get('data') or {}).get('repository') or {}).get('object') or {}).get('signature') or {}
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
            if re.search(r'androidx[.:]|org\.jetbrains\.compose', item.get('patch', '')):
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
            for key, lib in catalog.get('libraries', {}).items():
                module = lib.get('module', lib.get('group', '') + ':' + lib.get('name', ''))
                if not module.startswith(('androidx.', 'org.jetbrains.compose')):
                    continue
                version = lib.get('version')
                if isinstance(version, dict) and 'ref' in version:
                    version = catalog.get('versions', {}).get(version['ref'])
                result[key] = (module, version)
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

    def protection(self, base, names):
        # Refuse unprotected branches and missing checks. No admin or ruleset bypass.
        rule = self.get('branches/' + urllib.parse.quote(base, safe='') + '/protection')
        checks = rule.get('required_status_checks') or {}
        return (rule.get('enforce_admins', {}).get('enabled') is True
                and checks.get('strict') is True and set(names) <= set(checks.get('contexts', [])))


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


def reconcile(api, config, trigger=None):
    base = api.base()
    api.sync_baseline(base)
    policy, policy_sha = api.read_policy()
    if policy.get('baseBranchPatterns') != [base]:
        policy['baseBranchPatterns'] = [base]
        api.write_policy(policy, policy_sha)
        policy, policy_sha = api.read_policy()
    for candidate in api.pages('pulls?state=open'):
        if candidate.get('user', {}).get('id') != BOT_ID:
            continue
        number = candidate['number']
        pr = api.get(f'pulls/{number}')
        commits = api.signed_commits(number)
        if not trusted(pr, commits, api.repo) or pr['base']['ref'] != base:
            continue
        sha = pr['head']['sha']
        try:
            items = updates(pr.get('body'))
        except (ValueError, TypeError):
            print(f'PR {number}: invalid metadata; owner review required')
            continue
        # Older PRs without structured metadata remain for the owner; never guess.
        if not items:
            continue
        workflow_checks = {'ci.yml': config.get('ciChecks', [name for name in config['requiredChecks']
                                                                if name != 'Renovate policy tests'])}
        if 'Renovate policy tests' in config['requiredChecks']:
            workflow_checks['renovate-policy-tests.yml'] = ['Renovate policy tests']
        runs = api.current_runs(pr, workflow_checks)
        if any(not run for run in runs.values()):
            continue
        if trigger and not any(all(trigger.get(k) == run.get(k)
                                   for k in ('id', 'run_attempt', 'head_sha')) for run in runs.values()):
            continue
        if not api.protection(base, config['requiredChecks']):
            continue
        jobs, required = [], []
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
        if not complete:
            continue
        all_checks = api.pages(f'commits/{sha}/check-runs', 'check_runs')
        all_statuses = api.get(f'commits/{sha}/status')
        native = api.native_checks(base, all_checks, all_statuses['statuses'], jobs)
        if not native:
            continue
        comments = api.pages(f'issues/{number}/comments')
        records = [c for c in comments if c['user']['login'] == config['controllerLogin'] and c['body'].startswith(MARKER)]
        state = json.loads(records[-1]['body'].split(MARKER, 1)[1].split(' -->', 1)[0]) if records else None
        if all(run['conclusion'] == 'success' for run in runs.values()) and all(j['conclusion'] == 'success' for j in required):
            if compose_update(items) or api.android_stack_changed(pr):
                continue
            # All native required checks/statuses must be green.
            if any(c['conclusion'] != 'success' for c in native):
                continue
            current = api.fresh(number, sha, base, runs)
            if current and current.get('mergeable') is True and current.get('mergeable_state') == 'clean':
                api.request('PUT', f'pulls/{number}/merge', {'sha': sha, 'merge_method': 'squash'})
            continue
        decision = retry_decision(runs, state, sha)
        if decision['action'] == 'retry' and api.fresh(number, sha, base, runs):
            # Claim before calling rerun: event redelivery never spends a second retry.
            body = MARKER + json.dumps(decision['state']) + ' -->\nОдин автоматический повтор упавших проверок CI для SHA `' + sha + '`.'
            claim = api.request('POST', f'issues/{number}/comments', {'body': body})
            try:
                dispatched = set()
                for workflow, run in runs.items():
                    if run['conclusion'] == 'failure':
                        # Already dispatched runs may now be queued/in progress.
                        # Every remaining run must still match the claimed snapshot.
                        remaining = {name: value for name, value in runs.items() if name not in dispatched}
                        if not api.fresh(number, sha, base, remaining):
                            break
                        api.request('POST', f"actions/runs/{run['id']}/rerun-failed-jobs")
                        dispatched.add(workflow)
            except (urllib.error.HTTPError, urllib.error.URLError):
                api.request('PATCH', f"issues/comments/{claim['id']}", {'body': body + '\nЗапрос повтора завершился ошибкой API; claim сохранён, чтобы не запускать дубликаты. Владелец: проверьте Actions run и при необходимости вручную выберите Re-run failed jobs. Следующая завершённая попытка будет оценена автоматически.'})
                raise
        elif decision['action'] == 'close' and api.fresh(number, sha, base, runs):
            recovery = recovery_rules(items, number)
            new_rules = quarantine_rules(items, number)
            policy, policy_sha = api.read_policy()
            rules = policy.setdefault('packageRules', [])
            for rule in recovery + new_rules:
                rules[:] = [old for old in rules if (old.get('matchPackageNames'),old.get('matchDatasources'),old.get('matchNewValue'),old.get('matchJsonata')) != (rule['matchPackageNames'],rule['matchDatasources'],rule.get('matchNewValue'),rule.get('matchJsonata'))]
                rules.append(rule)
            # Exact failed values override family recreation.
            rules.sort(key=lambda rule: rule.get('description') != 'FIN-211: recovery of an automatically failed dependency family')
            # Persist version-scoped approval BEFORE closing, including major/groups.
            api.write_policy(policy, policy_sha)
            current = api.fresh(number, sha, base, runs)
            if not current:
                continue
            failed = '\n'.join('- [' + j['name'] + '](' + j['html_url'] + ')' for j in required if j['conclusion'] == 'failure')
            body = (MARKER + json.dumps(dict(state, closed=True)) + ' -->\n'
                    + 'CI повторно упал на SHA `' + sha + '`. Попытки: ' + ', '.join(name + '=' + str(value['run_attempt']) for name, value in runs.items()) + '.\n' + failed
                    + '\nПовторное падение подтверждено; запрошено автоматическое закрытие. Контроллер ветку не удаляет. Повтор: отметьте обновление в Closed/Ignored или Pending Approval в Dependency Dashboard. '
                    + 'Новые значения версий (включая исправления безопасности) не блокируются этим ограничением; '
                    + 'та же версия или группа требует явного подтверждения. См. docs/renovate-policy.md.')
            # Update the claim instead of adding another explanation on redelivery.
            api.request('PATCH', f"issues/comments/{records[-1]['id']}", {'body': body})
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
    if os.environ.get('RENOVATE_POLICY_ENABLED') != 'true':
        print('Renovate controller disabled; no mutations')
        return
    token = os.environ.get('RENOVATE_POLICY_TOKEN')
    if not token:
        raise ValueError('RENOVATE_POLICY_TOKEN is required; GITHUB_TOKEN is not a merge credential')
    config = json.loads(Path('.github/renovate-policy.json').read_text())
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    trigger = event.get('workflow_run') if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run' else None
    reconcile(GitHub(token, os.environ['GITHUB_REPOSITORY'], os.environ.get('GITHUB_READ_TOKEN')), config, trigger)


if __name__ == '__main__':
    main()
