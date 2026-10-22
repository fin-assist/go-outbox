"""API-level reconciliation tests: the fake models GitHub, not production code."""
import base64
import copy
import json
import unittest
from renovate_policy import GitHub, UPDATES, reconcile, MARKER

class Fake(GitHub):
    def __init__(self):
        super().__init__('', 'fin-assist/x')
        self.target='main'; self.actions=[]; self.comments=[]
        self.policy={'baseBranchPatterns':['main']}
        self.pr={'number':1,'state':'open','commits':1,'user':{'id':29139614,'login':'renovate[bot]'},
                 'head':{'sha':'abc','ref':'renovate/x','repo':{'full_name':self.repo}},
                 'base':{'ref':'main','sha':'base'},'mergeable':True,'mergeable_state':'clean',
                 'body':UPDATES+base64.b64encode(json.dumps([{'depName':'x','newValue':'v2.0.0','datasource':'npm'}]).encode()).decode()+' -->'}
        self.commits=[{'author':{'id':29139614},'_trusted_signer':True,'commit':{'verification':{'verified':True}}}]
        self.run={'id':1,'run_attempt':1,'head_sha':'abc','status':'completed','conclusion':'success'}
        self.jobs=[{'name':'go','status':'completed','conclusion':'success','html_url':'https://github.com/job'}]
        self.checks=[dict(self.jobs[0],app={'id':15368})]
        self.config={'requiredChecks':['go'],'controllerLogin':'controller'}
        self.protected=True; self.before_fresh=None
    def base(self): return self.target
    def sync_baseline(self,base):pass
    def read_policy(self):return copy.deepcopy(self.policy),'policy-sha'
    def write_policy(self,policy,sha):self.policy=copy.deepcopy(policy);self.actions.append(('policy',copy.deepcopy(policy)))
    def signed_commits(self,number):return self.commits
    def current_run(self,pr,workflow='ci.yml'):return copy.deepcopy(self.run)
    def protection(self,base,names):return self.protected
    def native_checks(self,base,checks,statuses,jobs):return jobs if all(c['status']=='completed' and c['conclusion'] in ('success','failure') for c in jobs) else None
    def pages(self,path,key=None):
        if path=='pulls?state=open':return [self.pr] if self.pr['state']=='open' else []
        if path.endswith('/comments'):return self.comments
        if '/jobs' in path:return copy.deepcopy(self.jobs)
        if path.endswith('/check-runs'):return copy.deepcopy(self.checks)
        raise AssertionError(path)
    def get(self,path):
        if path=='pulls/1':return copy.deepcopy(self.pr)
        if path.endswith('/status'):return {'total_count':0,'state':'pending','statuses':[]}
        raise AssertionError(path)
    def fresh(self,*args,**kwargs):
        if self.before_fresh:self.before_fresh(self)
        return super().fresh(*args,**kwargs)
    def request(self,method,path,data=None):
        self.actions.append((path,data))
        if path=='issues/1/comments':self.comments.append({'id':10,'user':{'login':'controller'},'body':data['body']})
        if path=='issues/comments/10':self.comments[0]['body']=data['body']
        if path=='pulls/1':self.pr['state']=data['state']
        return {'id':10}

class ControllerTests(unittest.TestCase):
    def test_green_merges_expected_sha(self):
        api=Fake();reconcile(api,api.config)
        self.assertIn(('pulls/1/merge',{'sha':'abc','merge_method':'squash'}),api.actions)
    def test_no_merge_after_new_head_or_release(self):
        for update in (lambda a:a.pr['head'].update(sha='new'),lambda a:setattr(a,'target','release/0.3.0')):
            api=Fake();api.before_fresh=update;reconcile(api,api.config)
            self.assertFalse(any(path=='pulls/1/merge' for path,_ in api.actions))
    def test_mixed_compose_never_merges(self):
        api=Fake();api.pr['body']=UPDATES+base64.b64encode(json.dumps([{'depName':'x','newValue':'2','datasource':'npm'},{'depName':'androidx.core:core','newValue':'2','datasource':'maven'}]).encode()).decode()+' -->'
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
        api=Fake();api.comments=[{'id':10,'user':{'login':'controller'},'body':MARKER+json.dumps({'sha':'abc','run_id':1,'attempt':1})+' -->'}]
        api.run['run_attempt']=2;reconcile(api,api.config)
        self.assertTrue(any(p=='pulls/1/merge' for p,_ in api.actions))
    def test_stale_event_or_pending_or_human_or_missing_or_unprotected_waits(self):
        for update in (lambda a:a.run.update(status='in_progress'),lambda a:a.commits.append({'author':{'id':1}}),lambda a:a.jobs.clear(),lambda a:setattr(a,'protected',False)):
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
        with self.assertRaises(urllib.error.URLError):reconcile(api,api.config)
        self.assertEqual(len(api.comments),1)
        self.assertIn('Re-run failed jobs',api.comments[0]['body'])
        self.assertIn('run_id',api.comments[0]['body'])
        reconcile(api,api.config)
        self.assertEqual(len(api.comments),1)

    def test_close_api_error_does_not_claim_pr_was_closed(self):
        import urllib.error
        api=Fake();api.run.update(conclusion='failure',run_attempt=2);api.jobs[0]['conclusion']='failure'
        state={'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}
        api.comments=[{'id':10,'user':{'login':'controller'},'body':MARKER+json.dumps(state)+' -->'}]
        original=api.request
        def request(method,path,data=None):
            if path=='pulls/1' and method=='PATCH':raise urllib.error.URLError('test outage')
            return original(method,path,data)
        api.request=request
        with self.assertRaises(urllib.error.URLError):reconcile(api,api.config)
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
            api.comments = [{'id': 10, 'user': {'login': 'controller'}, 'body': MARKER + json.dumps(state) + ' -->'}]
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
