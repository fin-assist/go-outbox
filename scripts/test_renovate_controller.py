"""API-level reconciliation tests: the fake models GitHub, not production code."""
import base64
import copy
import json
import unittest
from renovate_policy import GitHub, UPDATES, reconcile, MARKER

class Fake(GitHub):
    def __init__(self):
        super().__init__('', 'fin-assist/x')
        self.target='main'; self.actions=[]; self.comments=[]; self.receipts={}
        self.policy={'baseBranchPatterns':['main']}
        self.pr={'number':1,'state':'open','commits':1,'user':{'id':29139614,'login':'renovate[bot]'},
                 'head':{'sha':'abc','ref':'renovate/x','repo':{'full_name':self.repo}},
                 'base':{'ref':'main','sha':'base'},'mergeable':True,'mergeable_state':'clean',
                 'body':UPDATES+base64.b64encode(json.dumps([{'depName':'x','newValue':'v2.0.0','datasource':'npm'}]).encode()).decode()+' -->'}
        self.message=self.pr['body']
        self.commits=[{'author':{'id':29139614},'_trusted_signer':True,'commit':{'verification':{'verified':True}}}]
        self.run={'id':1,'run_attempt':1,'head_sha':'abc','status':'completed','conclusion':'success'}
        self.jobs=[{'name':'go','status':'completed','conclusion':'success','html_url':'https://github.com/job'}]
        self.checks=[dict(self.jobs[0],app={'id':15368})]
        self.config={'requiredChecks':['go'],'controllerLogin':'controller'}
        self.protected=True; self.before_fresh=None
    def read_receipts(self):return copy.deepcopy(self.receipts),'receipt-sha'
    def write_receipts(self,data,sha):self.receipts=copy.deepcopy(data);return 'new-receipt-sha'
    def base(self): return self.target
    def sync_baseline(self,base):pass
    def read_policy(self):return copy.deepcopy(self.policy),'policy-sha'
    def write_policy(self,policy,sha):self.policy=copy.deepcopy(policy);self.actions.append(('policy',copy.deepcopy(policy)))
    def signed_commits(self,number):
        commits=copy.deepcopy(self.commits)
        for commit in commits:
            commit['sha']=self.pr['head']['sha']
            commit.setdefault('commit',{})['message']=self.message
        return commits
    def current_run(self,pr,workflow='ci.yml'):return copy.deepcopy(self.run)
    def protection(self,base,names):return self.protected
    def policy_protected(self,login):return True
    def identity(self):return 'controller'
    def merge_method(self):return 'squash'
    def native_checks(self,base,checks,statuses,jobs):return jobs if all(c['status']=='completed' and c['conclusion'] in ('success','failure') for c in jobs) else None
    def pages(self,path,key=None):
        if path=='pulls?state=open':return [self.pr] if self.pr['state']=='open' else []
        if path.endswith('/comments'):return self.comments
        if '/jobs' in path:return copy.deepcopy(self.jobs)
        if path.endswith('/check-runs'):return copy.deepcopy(self.checks)
        raise AssertionError(path)
    def get(self,path):
        if path==f"pulls/{self.pr['number']}":return copy.deepcopy(self.pr)
        if path.endswith('/status'):return {'total_count':0,'state':'pending','statuses':[]}
        raise AssertionError(path)
    def fresh(self,*args,**kwargs):
        if self.before_fresh:self.before_fresh(self)
        return super().fresh(*args,**kwargs)
    def request(self,method,path,data=None):
        self.actions.append((path,data))
        if path==f"issues/{self.pr['number']}/comments":self.comments.append({'id':10,'user':{'login':'controller'},'body':data['body']})
        if path=='issues/comments/10':self.comments[0]['body']=data['body']
        if path==f"pulls/{self.pr['number']}":self.pr['state']=data['state']
        return {'id':10,'user':{'login':'controller'}}

class ControllerTests(unittest.TestCase):
    def test_green_merges_expected_sha(self):
        api=Fake();reconcile(api,api.config)
        self.assertIn(('pulls/1/merge',{'sha':'abc','merge_method':'squash'}),api.actions)
    def test_no_merge_after_new_head_or_release(self):
        for update in (lambda a:a.pr['head'].update(sha='new'),lambda a:setattr(a,'target','release/0.3.0')):
            api=Fake();api.before_fresh=update;reconcile(api,api.config)
            self.assertFalse(any(path=='pulls/1/merge' for path,_ in api.actions))
    def test_mixed_compose_never_merges(self):
        api=Fake();api.message=UPDATES+base64.b64encode(json.dumps([{'depName':'x','newValue':'2','datasource':'npm'},{'depName':'androidx.core:core','newValue':'2','datasource':'maven'}]).encode()).decode()+' -->'
        reconcile(api,api.config);self.assertEqual(api.actions,[])
    def test_failure_one_retry_then_close_with_quarantine_and_one_comment(self):
        api=Fake();api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure';api.checks[0]['conclusion']='failure'
        reconcile(api,api.config)
        self.assertEqual(len(api.comments),1)
        self.assertEqual(sum(p=='actions/runs/1/rerun-failed-jobs' for p,_ in api.actions),1)
        reconcile(api,api.config)
        self.assertEqual(sum(p=='actions/runs/1/rerun-failed-jobs' for p,_ in api.actions),1)
        api.run['run_attempt']=2;reconcile(api,api.config)
        self.assertEqual(api.pr['state'],'closed');self.assertEqual(len(api.comments),1)
        self.assertTrue(any(rule.get('dependencyDashboardApproval') for rule in api.policy['packageRules']))
        self.assertLess(next(i for i,x in enumerate(api.actions) if x[0]=='policy'), next(i for i,x in enumerate(api.actions) if x[0]=='pulls/1'))
        reconcile(api,api.config);self.assertEqual(len(api.comments),1)
    def test_successful_retry_merges(self):
        api=Fake();api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
        reconcile(api,api.config)
        self.assertFalse(any(path=='pulls/1/merge' for path,_ in api.actions))
        api.run.update(run_attempt=2,conclusion='success');api.jobs[0]['conclusion']='success'
        reconcile(api,api.config)
        self.assertTrue(any(path=='pulls/1/merge' for path,_ in api.actions))
        self.assertEqual(sum(path.endswith('rerun-failed-jobs') for path,_ in api.actions),1)
    def test_stale_event_or_pending_or_human_or_missing_or_unprotected_waits(self):
        for update in (lambda a:a.run.update(status='in_progress'),lambda a:(a.commits.append({'author':{'id':1}}),a.pr.update(commits=2)),lambda a:a.jobs.clear()):
            api=Fake();update(api);reconcile(api,api.config);self.assertEqual(api.actions,[])
        api=Fake();reconcile(api,api.config,dict(api.run,run_attempt=0));self.assertEqual(api.actions,[])
    def test_failed_rerun_api_keeps_claim_and_explains_manual_recovery(self):
        import urllib.error
        api=Fake();api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
        original=api.request
        def request(method,path,data=None):
            if path.endswith('rerun-failed-jobs'):
                raise urllib.error.URLError('test outage')
            return original(method,path,data)
        api.request=request
        with self.assertRaises(ValueError):reconcile(api,api.config)
        self.assertEqual(len(api.comments),1)
        self.assertIn('Re-run failed jobs',api.comments[0]['body'])
        self.assertIn('run_id',api.comments[0]['body'])
        reconcile(api,api.config)
        self.assertEqual(len(api.comments),1)

    def test_close_api_error_does_not_claim_pr_was_closed(self):
        import urllib.error
        api=Fake();api.run.update(conclusion='failure',run_attempt=2);api.jobs[0]['conclusion']='failure'
        state={'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}
        api.comments=[{'id':10,'user':{'login':'controller'},'body':MARKER+json.dumps(state)+' -->'}];api.receipts={'1:abc':{'state':state,'comment_id':10}}
        original=api.request
        def request(method,path,data=None):
            if path=='pulls/1' and method=='PATCH':raise urllib.error.URLError('test outage')
            return original(method,path,data)
        api.request=request
        with self.assertRaises(ValueError):reconcile(api,api.config)
        self.assertEqual(api.pr['state'],'open')
        self.assertNotIn('PR закрыт',api.comments[0]['body'])

    def test_current_target_is_persisted_once(self):
        api=Fake();api.target='release/0.3.0';reconcile(api,api.config)
        self.assertEqual(api.policy['baseBranchPatterns'],['release/0.3.0'])
        self.assertEqual(len(api.actions),1)


class MultiWorkflowFake(Fake):
    def __init__(self):
        super().__init__()
        self.config.update(requiredChecks=['go', 'Renovate policy tests'], ciChecks=['go'])
        self.runs = {'ci.yml': self.run, 'renovate-policy-tests.yml': dict(self.run, id=2)}
        self.jobs_by_workflow = {'ci.yml': self.jobs, 'renovate-policy-tests.yml': [dict(self.jobs[0], name='Renovate policy tests')]}
        self.start_reruns = False
        self.job_paths = []

    def current_run(self, pr, workflow='ci.yml'):
        return copy.deepcopy(self.runs.get(workflow))

    def pages(self, path, key=None):
        if '/jobs' in path:
            self.job_paths.append(path)
            run_id = int(path.split('/')[2])
            workflow = next(name for name, run in self.runs.items() if run and run['id'] == run_id)
            return copy.deepcopy(self.jobs_by_workflow[workflow])
        return super().pages(path, key)

    def request(self, method, path, data=None):
        value = super().request(method, path, data)
        if self.start_reruns and path.endswith('/rerun-failed-jobs'):
            run_id = int(path.split('/')[2])
            run = next(run for run in self.runs.values() if run['id'] == run_id)
            run.update(run_attempt=run['run_attempt'] + 1, status='queued', conclusion=None)
        return value

    def outcome(self, workflow, conclusion, attempt=None):
        self.runs[workflow].update(status='completed', conclusion=conclusion)
        if attempt is not None:
            self.runs[workflow]['run_attempt'] = attempt
        self.jobs_by_workflow[workflow][0]['conclusion'] = conclusion

    def reruns(self):
        return [path for path, _ in self.actions if path.endswith('/rerun-failed-jobs')]


class MultiWorkflowControllerTests(unittest.TestCase):
    def test_policy_failure_with_green_main_retries_policy_then_closes(self):
        api = MultiWorkflowFake()
        api.outcome('renovate-policy-tests.yml', 'failure')
        reconcile(api, api.config, copy.deepcopy(api.runs['renovate-policy-tests.yml']))
        self.assertEqual(api.reruns(), ['actions/runs/2/rerun-failed-jobs'])
        state = json.loads(api.comments[0]['body'].split(MARKER)[1].split(' -->')[0])
        self.assertFalse(state['runs']['ci.yml']['retried'])
        self.assertTrue(state['runs']['renovate-policy-tests.yml']['retried'])
        reconcile(api, api.config)
        self.assertEqual(len(api.reruns()), 1)
        api.outcome('renovate-policy-tests.yml', 'failure', attempt=2)
        reconcile(api, api.config)
        self.assertEqual(api.pr['state'], 'closed')

    def test_both_failures_retry_each_once_despite_first_run_starting_immediately(self):
        api = MultiWorkflowFake()
        for name in api.runs:
            api.outcome(name, 'failure')
        api.start_reruns = True
        reconcile(api, api.config)
        self.assertEqual(api.reruns(), ['actions/runs/1/rerun-failed-jobs', 'actions/runs/2/rerun-failed-jobs'])
        reconcile(api, api.config)
        self.assertEqual(len(api.reruns()), 2)
        for name in api.runs:
            api.outcome(name, 'failure')
        reconcile(api, api.config)
        self.assertEqual(api.pr['state'], 'closed')

    def test_successful_policy_retry_merges_with_current_full_run_set(self):
        api = MultiWorkflowFake()
        api.outcome('renovate-policy-tests.yml', 'failure')
        reconcile(api, api.config)
        api.outcome('renovate-policy-tests.yml', 'success', attempt=2)
        reconcile(api, api.config)
        self.assertTrue(any(path == 'pulls/1/merge' for path, _ in api.actions))

    def test_both_workflows_green_merge(self):
        api = MultiWorkflowFake()
        reconcile(api, api.config)
        self.assertTrue(any(path == 'pulls/1/merge' for path, _ in api.actions))

    def test_missing_pending_or_inconclusive_workflow_never_mutates(self):
        for mutation in [lambda a: a.runs.update({'renovate-policy-tests.yml': None}),
                         lambda a: a.runs['renovate-policy-tests.yml'].update(status='in_progress', conclusion=None),
                         lambda a: a.jobs_by_workflow['renovate-policy-tests.yml'].clear(),
                         lambda a: a.outcome('renovate-policy-tests.yml', 'skipped')]:
            api = MultiWorkflowFake()
            api.outcome('ci.yml', 'failure')
            mutation(api)
            reconcile(api, api.config)
            self.assertEqual(api.actions, [])

    def test_stale_events_from_either_workflow_never_mutate(self):
        for workflow in ['ci.yml', 'renovate-policy-tests.yml']:
            for field, value in [('id', 99), ('run_attempt', 9), ('head_sha', 'old')]:
                api = MultiWorkflowFake()
                api.outcome('renovate-policy-tests.yml', 'failure')
                trigger = dict(api.runs[workflow], **{field: value})
                reconcile(api, api.config, trigger)
                self.assertEqual(api.actions, [])

    def test_changed_identity_after_claim_cannot_close_or_retry_again(self):
        for change in [lambda a: a.runs['renovate-policy-tests.yml'].update(id=99),
                       lambda a: a.runs['renovate-policy-tests.yml'].update(run_attempt=3),
                       lambda a: a.runs['renovate-policy-tests.yml'].update(head_sha='new'),
                       lambda a: a.runs['ci.yml'].update(run_attempt=2)]:
            api = MultiWorkflowFake()
            api.outcome('renovate-policy-tests.yml', 'failure')
            reconcile(api, api.config)
            change(api)
            before = copy.deepcopy(api.actions)
            reconcile(api, api.config)
            self.assertEqual(api.actions, before)

    def test_freshness_rereads_policy_workflow_before_mutation(self):
        for conclusion in ['success', 'failure']:
            api = MultiWorkflowFake()
            api.outcome('renovate-policy-tests.yml', conclusion)
            api.before_fresh = lambda a: a.runs['renovate-policy-tests.yml'].update(run_attempt=2)
            reconcile(api, api.config)
            self.assertEqual(api.actions, [])

    def test_each_workflow_jobs_are_read_from_its_exact_current_attempt(self):
        api = MultiWorkflowFake()
        api.runs['ci.yml']['run_attempt'] = 3
        api.runs['renovate-policy-tests.yml']['run_attempt'] = 7
        reconcile(api, api.config)
        self.assertEqual(api.job_paths, ['actions/runs/1/attempts/3/jobs', 'actions/runs/2/attempts/7/jobs'])

    def test_invalid_or_old_single_run_claim_cannot_spend_another_retry(self):
        for state in [{}, {'sha': 'abc', 'run_id': 2, 'attempt': 1},
                      {'sha': 'abc', 'runs': {}},
                      {'sha': 'abc', 'runs': {'ci.yml': None, 'renovate-policy-tests.yml': None}}]:
            api = MultiWorkflowFake()
            api.outcome('renovate-policy-tests.yml', 'failure')
            api.comments = [{'id': 10, 'user': {'login': 'controller'}, 'body': MARKER + json.dumps(state) + ' -->'}];api.receipts={'1:abc':{'state':state,'comment_id':10}}
            reconcile(api, api.config)
            self.assertEqual(api.actions, [])

    def test_rerun_reuses_previous_success_but_requires_current_failure_evidence(self):
        api = MultiWorkflowFake()
        api.config['ciChecks'] = ['go', 'lint']
        api.config['requiredChecks'].append('lint')
        api.outcome('ci.yml', 'failure')
        api.jobs_by_workflow['ci.yml'].append(dict(api.jobs[0], name='lint', conclusion='success'))
        reconcile(api, api.config)
        api.outcome('ci.yml', 'failure', attempt=2)
        original = api.pages
        def pages(path, key=None):
            if path == 'actions/runs/1/attempts/2/jobs':
                return [dict(api.jobs[0], conclusion='failure')]
            return original(path, key)
        api.pages = pages
        reconcile(api, api.config)
        self.assertEqual(api.pr['state'], 'closed')
        self.assertIn('actions/runs/1/attempts/1/jobs', api.job_paths)

    def test_previous_failed_job_cannot_prove_repeat_failure(self):
        api = MultiWorkflowFake()
        api.outcome('ci.yml', 'failure')
        reconcile(api, api.config)
        api.outcome('ci.yml', 'failure', attempt=2)
        original = api.pages
        api.pages = lambda path, key=None: [] if path == 'actions/runs/1/attempts/2/jobs' else original(path, key)
        before = copy.deepcopy(api.actions)
        reconcile(api, api.config)
        self.assertEqual(api.actions, before)

    def test_successful_partial_rerun_merges_using_prior_unchanged_success(self):
        api = MultiWorkflowFake()
        api.config['ciChecks'] = ['go', 'lint']
        api.config['requiredChecks'].append('lint')
        api.outcome('ci.yml', 'failure')
        api.jobs_by_workflow['ci.yml'].append(dict(api.jobs[0], name='lint', conclusion='success'))
        reconcile(api, api.config)
        api.outcome('ci.yml', 'success', attempt=2)
        original = api.pages
        api.pages = lambda path, key=None: [dict(api.jobs[0], conclusion='success')] if path == 'actions/runs/1/attempts/2/jobs' else original(path, key)
        reconcile(api, api.config)
        self.assertTrue(any(path == 'pulls/1/merge' for path, _ in api.actions))

    def test_second_run_change_between_reruns_is_not_rerun_blindly(self):
        api = MultiWorkflowFake()
        for name in api.runs:
            api.outcome(name, 'failure')
        original = api.request
        def request(method, path, data=None):
            result = original(method, path, data)
            if path == 'actions/runs/1/rerun-failed-jobs':
                api.runs['renovate-policy-tests.yml']['run_attempt'] = 2
            return result
        api.request = request
        reconcile(api, api.config)
        self.assertEqual(api.reruns(), ['actions/runs/1/rerun-failed-jobs'])


if __name__=='__main__':unittest.main()

class SignedCommitsAdapterTests(unittest.TestCase):
    def test_actual_graphql_signatures_allow_only_trusted_signer_and_committer(self):
        from unittest.mock import patch
        class RealSignerFake(Fake):
            signed_commits = GitHub.signed_commits
            def pages(self,path,key=None):
                if path=='pulls/1/commits':return [copy.deepcopy(self.raw_commit)]
                return super().pages(path,key)
        class Response:
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(self.data).encode()
        cases=[
            (True,'web-flow','web-flow',True),
            (True,'renovate[bot]','renovate[bot]',True),
            (False,'web-flow','web-flow',False),
            (True,'human','human',False),
            (True,'web-flow','human',False),
            (True,'human','web-flow',False),
            (None,None,'web-flow',False),
            ('errors',None,'web-flow',False),
            ('null_object',None,'web-flow',False),
            (True,'web-flow',None,False),
        ]
        for valid,signer,committer,allowed in cases:
            for phase in ('merge','retry','close'):
                with self.subTest(valid=valid,signer=signer,committer=committer,phase=phase):
                    api=RealSignerFake()
                    api.raw_commit={'sha':'abc','author':{'id':29139614},'committer':{'login':committer},'commit':{'message':api.message,'verification':{'verified':True}}}
                    if phase!='merge':
                        api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
                    if phase=='close':
                        api.run['run_attempt']=2
                        state={'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}
                        api.comments=[{'id':10,'user':{'login':'controller'},'body':MARKER+json.dumps(state)+' -->'}];api.receipts={'1:abc':{'state':state,'comment_id':10}}
                    signature=None if valid is None else {'isValid':valid,'signer':{'login':signer}}
                    payload={'data':{'repository':{'object':{'signature':signature}}}}
                    if valid=='errors':payload={'data':None,'errors':[{'message':'fixture refusal'}]}
                    if valid=='null_object':payload={'data':{'repository':{'object':None}}}
                    with patch('urllib.request.urlopen',lambda *args,**kwargs:Response(payload)):
                        self.assertEqual(api.signed_commits(1)[0]['_trusted_signer'],allowed)
                        reconcile(api,api.config)
                    self.assertEqual(bool(api.actions),allowed)
                    if not allowed:self.assertEqual(api.pr['state'],'open')


class AndroidCatalogAdapterTests(unittest.TestCase):
    def test_versions_only_catalog_changes_block_androidx_and_compose_without_metadata(self):
        class AndroidFake(Fake):
            def __init__(self,module,stack_changed,other_changed):
                super().__init__();self.repo='fin-assist/android';self.pr['head']['repo']['full_name']=self.repo
                self.module=module;self.stack_changed=stack_changed;self.other_changed=other_changed;self.catalog_reads=[]
            def pages(self,path,key=None):
                if path=='pulls/1/files':return [{'filename':'gradle/libs.versions.toml','patch':'@@ -1,2 +1,2 @@\n-stack="1.0"\n+stack="2.0"'}]
                return super().pages(path,key)
            def get(self,path):
                if path.startswith('contents/gradle/libs.versions.toml?ref='):
                    self.catalog_reads.append(path)
                    head=path.endswith('abc')
                    stack='2.0' if head and self.stack_changed else '1.0'
                    other='2.0' if head and self.other_changed else '1.0'
                    text=f'[versions]\nstack="{stack}"\nother="{other}"\n[libraries]\nui={{module="{self.module}",version.ref="stack"}}\ntool={{module="com.example:tool",version.ref="other"}}\n'
                    return {'content':base64.b64encode(text.encode()).decode()}
                return super().get(path)
        for module,stack_changed,other_changed in (
            ('androidx.core:core',True,False),
            ('androidx.compose:compose-bom',True,False),
            ('androidx.core:core',True,True),
            ('androidx.core:core',False,True),
            ('org.jetbrains.kotlin:compose-compiler-gradle-plugin',True,False),
        ):
            with self.subTest(module=module,stack_changed=stack_changed,other_changed=other_changed):
                api=AndroidFake(module,stack_changed,other_changed)
                reconcile(api,api.config)
                self.assertEqual(len(api.catalog_reads),2)
                merged=any(path=='pulls/1/merge' for path,_ in api.actions)
                self.assertEqual(merged,not stack_changed)

class ConfigurationOnlyTests(unittest.TestCase):
    def test_disabled_pr_mutations_still_sync_baseline_and_single_target(self):
        class ConfigFake(Fake):
            def sync_baseline(self,base):self.synced=base
            def pages(self,*args,**kwargs):raise AssertionError('PRs must not be evaluated in sync-only mode')
        api=ConfigFake();api.target='release/0.2.12'
        reconcile(api,api.config,mutate_prs=False)
        self.assertEqual(api.synced,'release/0.2.12')
        self.assertEqual(api.policy['baseBranchPatterns'],['release/0.2.12'])
        self.assertEqual([action[0] for action in api.actions],['policy'])

class ComposeCompilerCatalogTests(unittest.TestCase):
    def test_kotlin_version_bump_through_plugin_alias_blocks_merge(self):
        class PluginFake(Fake):
            def __init__(self):
                super().__init__();self.repo='fin-assist/android';self.pr['head']['repo']['full_name']=self.repo
            def pages(self,path,key=None):
                if path=='pulls/1/files':return [{'filename':'gradle/libs.versions.toml'}]
                return super().pages(path,key)
            def get(self,path):
                if path.startswith('contents/gradle/libs.versions.toml?ref='):
                    version='2.1.21' if path.endswith('abc') else '2.1.20'
                    text=f'[versions]\nkotlin="{version}"\n[plugins]\nkotlin-compose={{id="org.jetbrains.kotlin.plugin.compose",version.ref="kotlin"}}\n'
                    return {'content':base64.b64encode(text.encode()).decode()}
                return super().get(path)
        api=PluginFake();reconcile(api,api.config)
        self.assertEqual(api.actions,[])

    def test_catalog_string_libraries_and_group_name_form_are_safe(self):
        class CatalogFake(Fake):
            def __init__(self,definition):
                super().__init__();self.repo='fin-assist/android';self.definition=definition
            def pages(self,path,key=None):return [{'filename':'gradle/libs.versions.toml'}]
            def get(self,path):
                version='2.0' if path.endswith('abc') else '1.0'
                text=f'[versions]\nstack="{version}"\n[libraries]\nui={self.definition.format(version=version)}\n'
                return {'content':base64.b64encode(text.encode()).decode()}
        pr={'number':1,'head':{'sha':'abc'},'base':{'sha':'base'}}
        for definition in ('"androidx.core:core:{version}"','{{group="androidx.core",name="core",version.ref="stack"}}'):
            self.assertTrue(CatalogFake(definition).android_stack_changed(pr))


class ComposeStackCloseTests(unittest.TestCase):
    def test_red_stack_is_retried_and_closed_with_one_preserved_group_gate(self):
        api=Fake();api.repo='fin-assist/android';api.pr['head']['repo']['full_name']=api.repo
        items=[{'depName':'androidx.compose:compose-bom','newValue':'2026.10','datasource':'maven'},
               {'depName':'androidx.activity:activity-compose','newValue':'1.10.0','datasource':'maven'}]
        api.message=UPDATES+base64.b64encode(json.dumps(items).encode()).decode()+' -->'
        api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
        reconcile(api,api.config)
        self.assertTrue(any(path.endswith('/rerun-failed-jobs') for path,_ in api.actions))
        api.run['run_attempt']=2;reconcile(api,api.config)
        self.assertEqual(api.pr['state'],'closed')
        self.assertEqual(len(api.policy['packageRules']),1)
        rule=api.policy['packageRules'][0]
        self.assertEqual(rule['groupName'],'androidx compose stack')
        self.assertTrue(rule['dependencyDashboardApproval'])
        self.assertFalse(rule['automerge'])
        self.assertFalse(rule['separateMajorMinor'])
        self.assertEqual(rule['additionalBranchPrefix'],'fin211-retry-1-')

class ActorAndMandatoryChecksTests(unittest.TestCase):
    def test_wrong_credential_identity_fails_before_claim_or_preset_write(self):
        api=Fake();api.identity=lambda:'other-user'
        with self.assertRaisesRegex(ValueError,'identity differs'):reconcile(api,api.config)
        self.assertEqual(api.actions,[])

    def test_unexpected_claim_actor_never_dispatches_retry(self):
        api=Fake();api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
        original=api.request
        def request(method,path,data=None):
            result=original(method,path,data)
            if path=='issues/1/comments':result['user']={'login':'unexpected-user'}
            return result
        api.request=request
        with self.assertRaises(ValueError):reconcile(api,api.config)
        self.assertFalse(any(path.endswith('/rerun-failed-jobs') for path,_ in api.actions))

    def test_unprotected_target_is_never_copied_into_trusted_preset(self):
        api=Fake();api.protected=False
        api.sync_baseline=lambda base:(_ for _ in ()).throw(AssertionError('Unsafe baseline must not be copied'))
        with self.assertRaisesRegex(ValueError,'Target branch'):reconcile(api,api.config)
        self.assertEqual(api.actions,[])

    def test_optional_job_failure_does_not_retry_or_close_green_required_jobs(self):
        api=Fake();api.run['conclusion']='failure';api.pr['mergeable_state']='unstable'
        api.jobs.append({'name':'optional-report','status':'completed','conclusion':'failure'})
        reconcile(api,api.config)
        self.assertIn(('pulls/1/merge',{'sha':'abc','merge_method':'squash'}),api.actions)
        self.assertFalse(any(path.endswith('/rerun-failed-jobs') or path=='policy' for path,_ in api.actions))

    def test_editable_pr_body_cannot_override_signed_head_metadata(self):
        api=Fake();api.message='An old signed commit without structured metadata'
        reconcile(api,api.config)
        self.assertEqual(api.actions,[])
        api=Fake();api.pr['body']='Human edited body'
        reconcile(api,api.config)
        self.assertTrue(any(path=='pulls/1/merge' for path,_ in api.actions))


class PerPrFailureIsolationTests(unittest.TestCase):
    def test_one_merge_api_error_keeps_other_prs_progressing_and_workflow_red(self):
        import urllib.error
        class TwoPrFake(Fake):
            def pages(self,path,key=None):
                if path=='pulls?state=open':return [self.pr,dict(self.pr,number=2)]
                return super().pages(path,key)
            def get(self,path):
                if path=='pulls/2':return dict(copy.deepcopy(self.pr),number=2)
                return super().get(path)
            def request(self,method,path,data=None):
                if path=='pulls/1/merge':raise urllib.error.HTTPError('https://api.github.com/fixture',409,'fixture',{},None)
                return super().request(method,path,data)
        api=TwoPrFake()
        with self.assertRaisesRegex(ValueError,'PRs: 1'):reconcile(api,api.config)
        self.assertIn(('pulls/2/merge',{'sha':'abc','merge_method':'squash'}),api.actions)

class ProtectedReceiptTests(unittest.TestCase):
    def red(self):
        api=Fake();api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
        return api
    def test_edited_or_deleted_comments_never_reset_retry_budget(self):
        for delete in (False,True):
            api=self.red();reconcile(api,api.config)
            original=copy.deepcopy(api.receipts)
            if delete:api.comments.clear()
            else:api.comments[0]['body']=MARKER+json.dumps({'sha':'forged','runs':{}})+' -->'
            reconcile(api,api.config)
            self.assertEqual(api.receipts,original)
            self.assertEqual(sum(path.endswith('rerun-failed-jobs') for path,_ in api.actions),1)
            api.run['run_attempt']=2;reconcile(api,api.config)
            self.assertEqual(api.pr['state'],'closed')
    def test_forged_comment_cannot_authorize_premature_close(self):
        api=self.red()
        api.comments=[{'id':99,'user':{'login':'controller'},'body':MARKER+json.dumps({'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':0,'retried':True}}})+' -->'}]
        reconcile(api,api.config)
        self.assertEqual(api.pr['state'],'open')
        self.assertTrue(api.receipts)
        self.assertFalse(any(path=='policy' for path,_ in api.actions))
    def test_receipt_write_failure_prevents_comment_and_rerun(self):
        api=self.red()
        def fail(*args):raise ValueError('CAS conflict')
        api.write_receipts=fail
        with self.assertRaises(ValueError):reconcile(api,api.config)
        self.assertEqual(api.actions,[])
    def test_return_to_old_head_does_not_restore_budget(self):
        api=self.red();reconcile(api,api.config)
        api.pr['head']['sha']='def';api.run['head_sha']='def';reconcile(api,api.config)
        api.pr['head']['sha']='abc';api.run['head_sha']='abc';reconcile(api,api.config)
        self.assertEqual(sum(path.endswith('rerun-failed-jobs') for path,_ in api.actions),2)
        self.assertEqual(set(api.receipts),{'1:abc','1:def'})

class ReplacementCommentTests(unittest.TestCase):
    def test_deleted_claim_close_error_then_redelivery_reuses_replacement(self):
        import urllib.error
        class ReplacementFake(Fake):
            def __init__(self):super().__init__();self.sequence=20;self.fail_close=True
            def request(self,method,path,data=None):
                if path=='issues/1/comments':
                    self.actions.append((path,data));ident=self.sequence;self.sequence+=1
                    self.comments.append({'id':ident,'user':{'login':'controller'},'body':data['body']})
                    return {'id':ident,'user':{'login':'controller'}}
                if path.startswith('issues/comments/'):
                    self.actions.append((path,data))
                    next(c for c in self.comments if c['id']==int(path.rsplit('/',1)[1]))['body']=data['body']
                    return {}
                if path=='pulls/1' and self.fail_close:
                    self.fail_close=False
                    raise urllib.error.HTTPError('https://api.github.com/fixture',503,'fixture',{},None)
                return super().request(method,path,data)
        api=ReplacementFake();api.run.update(conclusion='failure',run_attempt=2);api.jobs[0]['conclusion']='failure'
        state={'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}
        api.receipts={'1:abc':{'state':state,'comment_id':10}}
        with self.assertRaises(ValueError):reconcile(api,api.config)
        self.assertEqual(api.receipts['1:abc']['comment_id'],20)
        reconcile(api,api.config)
        self.assertEqual(api.pr['state'],'closed')
        self.assertEqual([c['id'] for c in api.comments],[20])
        self.assertTrue(any(path=='issues/comments/20' for path,_ in api.actions))

class SuccessiveQuarantineTests(unittest.TestCase):
    def test_two_real_reconciliations_replace_exact_value_generation_and_preserve_order(self):
        policy={'baseBranchPatterns':['main']}
        for number in (1,2):
            api=Fake();api.pr['number']=number;api.policy=policy
            api.run['conclusion']='failure';api.jobs[0]['conclusion']='failure'
            reconcile(api,api.config);api.run['run_attempt']=2;reconcile(api,api.config)
            self.assertEqual(api.pr['state'],'closed');policy=api.policy
        rules=policy['packageRules']
        self.assertEqual(len(rules),2)
        self.assertEqual(rules[0]['recreateWhen'],'always')
        self.assertEqual(rules[1]['recreateWhen'],'never')
        self.assertEqual(rules[1]['additionalBranchPrefix'],'fin211-retry-2-')

class ReplacementAcknowledgementTests(unittest.TestCase):
    def test_accepted_replacement_with_failed_receipt_ack_is_reused(self):
        class AckFake(Fake):
            def __init__(self):super().__init__();self.sequence=20;self.fail_ack=True
            def request(self,method,path,data=None):
                if path=='issues/1/comments':
                    ident=self.sequence;self.sequence+=1
                    self.comments.append({'id':ident,'user':{'login':'controller'},'body':data['body']})
                    self.actions.append((path,data));return {'id':ident,'user':{'login':'controller'}}
                if path.startswith('issues/comments/'):
                    next(c for c in self.comments if c['id']==int(path.rsplit('/',1)[1]))['body']=data['body'];return {}
                return super().request(method,path,data)
            def write_receipts(self,data,sha):
                if self.fail_ack:self.fail_ack=False;raise ValueError('CAS acknowledgement lost')
                return super().write_receipts(data,sha)
        api=AckFake();api.run.update(conclusion='failure',run_attempt=2);api.jobs[0]['conclusion']='failure'
        state={'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}
        api.receipts={'1:abc':{'state':state,'comment_id':10}}
        with self.assertRaises(ValueError):reconcile(api,api.config)
        reconcile(api,api.config)
        self.assertEqual([c['id'] for c in api.comments],[20])
        self.assertEqual(api.receipts['1:abc']['comment_id'],20)
        self.assertEqual(api.pr['state'],'closed')
