import base64
import copy
import json
import unittest
from renovate_policy import (BOT_ID, GitHub, UPDATES, compose_update, quarantine_rules,
                             selected_base, terminal_checks, trusted, updates)

class PolicyTests(unittest.TestCase):
    def test_release_selection(self):
        self.assertEqual(selected_base([], set(), lambda _: True), 'main')
        self.assertEqual(selected_base(['release/0.2.1'], set(), lambda _: True), 'release/0.2.1')
        branches = ['release/0.2.9', 'release/0.2.10', 'release/0.1.0']
        self.assertEqual(selected_base(branches, {'release/0.2.10'}, lambda _: True), 'release/0.2.9')
        self.assertEqual(selected_base(branches, set(branches), lambda _: False), 'release/0.2.10')
        self.assertEqual(selected_base(branches, set(branches), lambda _: True), 'main')

    def test_structured_upgrades(self):
        item = {'depName':'x', 'newValue':'v4.1.2', 'datasource':'npm'}
        encoded = base64.b64encode(json.dumps([item]).encode()).decode()
        self.assertEqual(updates(UPDATES + encoded + ' -->'), [item])
        self.assertEqual(updates('old PR'), [])
        with self.assertRaises(ValueError):
            updates(UPDATES + '!!' + ' -->')

    def test_quarantine_exact_major_and_group_versions(self):
        items = [{'depName':'x', 'newValue':'v4.1.2', 'datasource':'npm'},
                 {'depName':'y', 'newValue':'^2.2.0', 'datasource':'npm'}]
        import re
        rules = quarantine_rules(items, 42)
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(r['groupName'] is None and r['additionalBranchPrefix']=='fin211-retry-42-' for r in rules))
        self.assertTrue(re.fullmatch(rules[0]['matchNewValue'][1:-1], 'v4.1.2'))
        self.assertFalse(re.fullmatch(rules[0]['matchNewValue'][1:-1], 'v4.1.3'))
        self.assertTrue(all(r['dependencyDashboardApproval'] and r['recreateWhen']=='never' for r in rules))
        self.assertTrue(compose_update([dict(items[0],depName='androidx.core:core')]))
        self.assertTrue(compose_update(items + [dict(items[0],depName='androidx.compose:compose-bom')]))
        self.assertFalse(compose_update(items))

    def test_required_checks_missing_pending_cancelled_skipped(self):
        job = {'name':'go', 'status':'completed', 'conclusion':'success'}
        self.assertEqual(terminal_checks([job],['go']), [job])
        self.assertIsNone(terminal_checks([],['go']))
        self.assertIsNone(terminal_checks([job,job],['go']))
        for conclusion in ('cancelled','skipped',None):
            self.assertIsNone(terminal_checks([dict(job,conclusion=conclusion)],['go']))
        self.assertIsNone(terminal_checks([dict(job,status='in_progress')],['go']))

    def test_only_signed_renovate_commits_in_same_repo(self):
        repo='fin-assist/x'
        pr={'commits':1,'state':'open','user':{'id':BOT_ID,'login':'renovate[bot]'},
            'head':{'repo':{'full_name':repo},'ref':'renovate/x'}}
        commit={'_trusted_signer':True,'author':{'id':BOT_ID},'commit':{'verification':{'verified':True}}}
        self.assertTrue(trusted(pr,[commit],repo))
        for bad in ({'author':None}, {'_trusted_signer':False}, {'author':{'id':1}}, {'commit':{'verification':{'verified':False}}}):
            changed=copy.deepcopy(commit);changed.update(bad)
            self.assertFalse(trusted(dict(pr,commits=2),[commit,changed],repo))
            self.assertFalse(trusted(pr,[changed],repo))
        self.assertFalse(trusted(dict(pr,user={'id':1,'login':'renovate[bot]'}),[commit],repo))
        self.assertFalse(trusted(pr,[],repo))

    def test_paginated_partial_evidence_fails_closed(self):
        class Fake(GitHub):
            def get(self,path): return list(range(100))
        with self.assertRaises(ValueError): Fake('', 'x').pages('pulls')

    def test_ancestry_comparison(self):
        class Fake(GitHub):
            def pages(self,path,key=None):
                return [{'name':'release/0.2.1'}] if path=='branches' else [{'head':{'ref':'release/0.2.1','repo':{'full_name':'x'}},'merged_at':'today'}]
            def get(self,path): return {'behind_by': 0}
        self.assertEqual(Fake('','x').base(),'main')

class NativeEvidenceTests(unittest.TestCase):
    def test_exact_pr_attempt_beats_push_skipped_and_extra_required_still_blocks(self):
        class Fake(GitHub):
            def get(self,path):return {'required_status_checks':{'checks':[{'context':'go','app_id':15368},{'context':'security','app_id':99}]}}
        api=Fake('','x')
        jobs=[{'name':'go','status':'completed','conclusion':'success'}]
        checks=[{'name':'go','app':{'id':15368},'status':'completed','conclusion':'skipped'},
                {'name':'security','app':{'id':99},'status':'completed','conclusion':'success'}]
        self.assertEqual(len(api.native_checks('main',checks,[],jobs)),2)
        self.assertIsNone(api.native_checks('main',checks[:1],[],jobs))
        checks[1]['status']='in_progress'
        self.assertIsNone(api.native_checks('main',checks,[],jobs))
        checks[1].update(status='completed',conclusion='failure')
        self.assertEqual(api.native_checks('main',checks,[],jobs)[1]['conclusion'],'failure')
    def test_owner_admin_bypass_must_be_enforced(self):
        class Fake(GitHub):
            def get(self,path):return {'enforce_admins':{'enabled':False},'required_status_checks':{'strict':True,'contexts':['go']}}
        self.assertFalse(Fake('','x').protection('main',['go']))

class TokenBoundaryTests(unittest.TestCase):
    def test_checks_use_read_token_and_mutations_use_policy_token(self):
        from unittest.mock import patch
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{}'
        captured=[]
        def capture(request,timeout):
            captured.append((request.get_method(),request.get_header('Authorization')))
            return Response()
        api=GitHub('policy-placeholder','x','read-placeholder')
        with patch('urllib.request.urlopen',capture):
            api.get('commits/abc/check-runs')
            api.request('PUT','pulls/1/merge',{'sha':'abc'})
        self.assertEqual(captured,[('GET','Bearer read-placeholder'),('PUT','Bearer policy-placeholder')])
        with self.assertRaises(ValueError): GitHub('policy-placeholder','x').get('commits/abc/check-runs')

class DigestUpdateTests(unittest.TestCase):
    def test_quarantine_matches_digest_not_moving_tag(self):
        item={'depName':'postgres','newValue':'18','datasource':'docker','newDigest':'sha256:abc'}
        rule=quarantine_rules([item],42)[0]
        self.assertNotIn('matchNewValue',rule)
        self.assertEqual(rule['matchJsonata'],['newDigest = "sha256:abc"'])
        encoded=base64.b64encode(json.dumps([item]).encode()).decode()
        self.assertEqual(updates(UPDATES+encoded+' -->'),[item])

class BaselineSyncTests(unittest.TestCase):
    def test_release_baseline_is_pinned_and_synced_without_source_override(self):
        class Fake(GitHub):
            def __init__(self):super().__init__('','x');self.writes=[];self.reads=[]
            def get(self,path):
                self.reads.append(path)
                if path=='branches/release%2F0.2.12':return {'commit':{'sha':'approved-release-sha'}}
                if path=='contents/renovate-base.json?ref=approved-release-sha':return {'content':base64.b64encode(b'{"packageRules":[{"allowedVersions":"<2"}]}').decode()}
                if path=='contents/base.json?ref=renovate-policy':return {'sha':'old-preset-sha','content':base64.b64encode(b'{}').decode()}
                raise AssertionError(path)
            def request(self,method,path,data):self.writes.append((path,data))
        api=Fake();api.sync_baseline('release/0.2.12')
        self.assertEqual(json.loads(base64.b64decode(api.writes[0][1]['content'])),{'packageRules':[{'allowedVersions':'<2'}]})
        self.assertEqual(api.writes[0][1]['sha'],'old-preset-sha')
        self.assertTrue(all('main' not in path for path in api.reads))

class RecoveryFamilyTests(unittest.TestCase):
    def test_recovery_is_limited_to_the_automatically_failed_major_family(self):
        from renovate_policy import recovery_rules
        rule=recovery_rules([{'depName':'x','datasource':'npm','newValue':'^4.0.0'}],42)[0]
        self.assertEqual(rule['matchPackageNames'],['x'])
        self.assertEqual(rule['matchJsonata'],['newMajor = 4'])
        self.assertEqual(rule['recreateWhen'],'always')
        self.assertNotIn('additionalBranchPrefix',rule)

class DigestRecoveryTests(unittest.TestCase):
    def test_digest_without_explicit_major_uses_tag_even_when_numeric(self):
        from renovate_policy import recovery_rules
        rule=recovery_rules([{'depName':'postgres','datasource':'docker','newValue':'18','newDigest':'sha256:bad'}],42)[0]
        self.assertEqual(rule['matchNewValue'],'/^18$/')
        self.assertEqual(rule['matchJsonata'],['$exists(newDigest) and newDigest != null'])
        self.assertEqual(rule['recreateWhen'],'always')

class CurrentRunAdapterTests(unittest.TestCase):
    def test_real_adapter_rejects_wrong_pr_base_and_head_and_uses_latest_run(self):
        import copy
        pr={'number':7,'head':{'sha':'current'},'base':{'ref':'release/0.2.12'}}
        valid={'id':10,'head_sha':'current','pull_requests':[{'number':7,'base':{'ref':'release/0.2.12'}}],'status':'completed','conclusion':'success'}
        stale_head=dict(copy.deepcopy(valid),id=100,head_sha='old')
        wrong_pr=dict(copy.deepcopy(valid),id=200,pull_requests=[{'number':8,'base':{'ref':'release/0.2.12'}}])
        wrong_base=dict(copy.deepcopy(valid),id=300,pull_requests=[{'number':7,'base':{'ref':'main'}}])
        latest=dict(copy.deepcopy(valid),id=11,status='in_progress',conclusion=None)
        class Fake(GitHub):
            def pages(self,path,key=None):
                self.path=path
                return copy.deepcopy(self.runs)
        api=Fake('','x');api.runs=[stale_head,wrong_pr,wrong_base,valid,latest]
        self.assertEqual(api.current_run(pr)['id'],11)
        self.assertEqual(api.current_run(pr)['status'],'in_progress')
        self.assertIn('event=pull_request&head_sha=current',api.path)
        api.runs=[stale_head,wrong_pr,wrong_base]
        self.assertIsNone(api.current_run(pr))

class PresetProtectionAdapterTests(unittest.TestCase):
    def test_only_controller_can_write_generated_presets(self):
        import copy
        protected={'enforce_admins':{'enabled':True},'restrictions':{'users':[{'login':'controller'}],'teams':[],'apps':[]},'allow_force_pushes':{'enabled':False},'allow_deletions':{'enabled':False}}
        class Fake(GitHub):
            def get(self,path):return self.rule
        api=Fake('','x');api.rule=protected
        self.assertTrue(api.policy_protected('controller'))
        for mutate in (
            lambda x:x.update(restrictions=None),
            lambda x:x['restrictions']['users'].append({'login':'other'}),
            lambda x:x['restrictions']['teams'].append({'slug':'writers'}),
            lambda x:x['restrictions']['apps'].append({'slug':'renovate'}),
            lambda x:x['enforce_admins'].update(enabled=False),
            lambda x:x['allow_force_pushes'].update(enabled=True),
            lambda x:x['allow_deletions'].update(enabled=True),
        ):
            api.rule=copy.deepcopy(protected);mutate(api.rule)
            self.assertFalse(api.policy_protected('controller'))

class RepositoryMergeMethodTests(unittest.TestCase):
    def test_supported_repository_merge_method_is_selected_without_bypass(self):
        class Fake(GitHub):
            def get(self,path):return self.settings
        api=Fake('','x')
        for settings,method in [({'allow_squash_merge':True},'squash'),({'allow_merge_commit':True},'merge'),({'allow_rebase_merge':True},'rebase')]:
            api.settings=settings;self.assertEqual(api.merge_method(),method)
        api.settings={}
        with self.assertRaises(ValueError):api.merge_method()

class CompactMetadataTests(unittest.TestCase):
    def test_compact_signed_commit_markers_parse_without_editable_body(self):
        from renovate_policy import COMPACT_UPDATE
        items=[{'depName':'x','newValue':'4.0.1','datasource':'npm'},
               {'depName':'postgres','newValue':'18','newDigest':'sha256:abc','datasource':'docker'}]
        message='Update dependencies\n\n'+''.join(COMPACT_UPDATE+base64.b64encode(json.dumps(item).encode()).decode()+' -->\n' for item in items)
        self.assertEqual(updates(message),items)

class ProtectedReceiptAdapterTests(unittest.TestCase):
    def test_actual_contents_adapter_validates_cas_and_initializes_missing_file(self):
        import base64,json,urllib.error
        from renovate_policy import GitHub,validate_receipts
        api=GitHub('token','owner/repo');requests=[]
        receipt={'1:abc':{'state':{'sha':'abc','runs':{'ci.yml':{'run_id':1,'attempt':1,'retried':True}}}}}
        def missing(path):raise urllib.error.HTTPError('https://api.github.com/fixture',404,'fixture',{},None)
        api.get=missing
        with self.assertRaisesRegex(ValueError,'initialization'):api.read_receipts()
        def request(method,path,data):requests.append((method,path,data));return {'content':{'sha':'next'}}
        api.request=request
        self.assertEqual(api.write_receipts(receipt,None),'next')
        self.assertNotIn('sha',requests[-1][2])
        api.write_receipts(receipt,'old');self.assertEqual(requests[-1][2]['sha'],'old')
        api.get=lambda path:{'sha':'next','content':base64.b64encode(json.dumps(receipt).encode()).decode()}
        self.assertEqual(api.read_receipts(),(receipt,'next'))
        for invalid in ([],{'1:abc':{}},{'1:abc':{'state':{'sha':'other','runs':{}}}}):
            with self.assertRaises(ValueError):validate_receipts(invalid)

class RulesetProtectionTests(unittest.TestCase):
    def test_active_or_unknown_inherited_rulesets_fail_closed(self):
        api=GitHub('token','owner/repo');api.controller_login='controller'
        api.get=lambda path:{'enforce_admins':{'enabled':True},'required_status_checks':{'strict':True,'contexts':['go']},'restrictions':{'users':[{'login':'controller'}],'teams':[],'apps':[]}}
        paths=[]
        def pages(path):paths.append(path);return api.rules
        api.pages=pages
        for rules in ([{'enforcement':'active'}],[{'enforcement':'unknown'}],[{}]):
            api.rules=rules;self.assertFalse(api.protection('main',['go']))
        self.assertEqual(set(paths),{'rulesets?includes_parents=true'})
    def test_absent_disabled_or_evaluate_rulesets_preserve_classic_requirements(self):
        api=GitHub('token','owner/repo');api.controller_login='controller'
        api.get=lambda path:{'enforce_admins':{'enabled':True},'required_status_checks':{'strict':True,'contexts':['go']},'restrictions':{'users':[{'login':'controller'}],'teams':[],'apps':[]}}
        for rules in ([],[{'enforcement':'disabled'}],[{'enforcement':'evaluate'}]):
            api.pages=lambda path:rules
            self.assertTrue(api.protection('main',['go']))

class TargetOwnerProtectionTests(unittest.TestCase):
    def test_only_owner_can_write_target_and_create_release(self):
        import copy
        api=GitHub('token','owner/repo');api.controller_login='controller';api.pages=lambda path:[]
        rule={'enforce_admins':{'enabled':True},'required_status_checks':{'strict':True,'contexts':['go']},'restrictions':{'users':[{'login':'controller'}],'teams':[],'apps':[]},'block_creations':{'enabled':True}}
        api.get=lambda path:api.rule
        api.rule=copy.deepcopy(rule)
        self.assertTrue(api.protection('main',['go']));self.assertTrue(api.protection('release/1.0.0',['go']))
        for mutate in (lambda r:r.update(restrictions=None),lambda r:r['restrictions']['users'].append({'login':'writer'}),lambda r:r['restrictions']['apps'].append({'slug':'app'}),lambda r:r['restrictions']['teams'].append({'slug':'team'})):
            api.rule=copy.deepcopy(rule);mutate(api.rule);self.assertFalse(api.protection('main',['go']))
        api.rule=copy.deepcopy(rule);api.rule['block_creations']['enabled']=False
        self.assertTrue(api.protection('main',['go']));self.assertFalse(api.protection('release/1.0.0',['go']))


class RootRepositoryRequestTests(unittest.TestCase):
    def test_merge_settings_root_has_no_trailing_slash(self):
        import io,json
        from unittest.mock import patch
        api=GitHub('credential','owner/repo');seen=[]
        def urlopen(request,timeout):
            seen.append(request.full_url)
            return io.BytesIO(json.dumps({'allow_merge_commit':True}).encode())
        with patch('urllib.request.urlopen',urlopen):self.assertEqual(api.merge_method(),'merge')
        self.assertEqual(seen,['https://api.github.com/repos/owner/repo'])

class ReleaseQueryAdapterTests(unittest.TestCase):
    def test_no_release_does_not_fetch_any_pr_history(self):
        api=GitHub('credential','owner/repo');paths=[]
        def pages(path,key=None):
            paths.append(path)
            if path=='branches':return [{'name':'main'},{'name':'feature/x'}]
            raise AssertionError('Unexpected full PR history fetch')
        api.pages=pages
        self.assertEqual(api.base(),'main');self.assertEqual(paths,['branches'])
    def test_latest_active_release_queries_only_its_same_repo_head(self):
        api=GitHub('credential','owner/repo');paths=[]
        def pages(path,key=None):
            paths.append(path)
            if path=='branches':return [{'name':'release/1.0.0'},{'name':'release/2.0.0'}]
            return [{'merged_at':'today','head':{'ref':'release/2.0.0','repo':{'full_name':'fork/repo'}}}]
        api.pages=pages
        self.assertEqual(api.base(),'release/2.0.0')
        self.assertEqual(paths,['branches','pulls?state=closed&base=main&head=owner%3Arelease%2F2.0.0'])


class NativeStatusBoundaryTests(unittest.TestCase):
    def test_external_status_cannot_be_hidden_by_green_job_for_unbound_context(self):
        api=GitHub('credential','owner/repo')
        jobs=[{'name':'external','status':'completed','conclusion':'success'}]
        for app in (None,-1):
            api.get=lambda path:{'required_status_checks':{'checks':[{'context':'external','app_id':app}]}}
            for state in ('failure','pending','error','success'):
                result=api.native_checks('main',[],[{'context':'external','state':state}],jobs)
                if state in ('pending','error'):self.assertIsNone(result)
                elif state=='failure':self.assertTrue(any(check['conclusion']!='success' for check in result))
                else:self.assertTrue(all(check['conclusion']=='success' for check in result))
    def test_legacy_required_contexts_read_commit_statuses(self):
        api=GitHub('credential','owner/repo')
        api.get=lambda path:{'required_status_checks':{'contexts':['external']}}
        self.assertIsNone(api.native_checks('main',[],[],[]))
        self.assertEqual(api.native_checks('main',[],[{'context':'external','state':'failure'}],[]),[{'status':'completed','conclusion':'failure'}])
        self.assertEqual(api.native_checks('main',[],[{'context':'external','state':'success'}],[]),[{'status':'completed','conclusion':'success'}])
    def test_non_strict_or_missing_required_context_never_passes_protection(self):
        import copy
        api=GitHub('credential','owner/repo');api.controller_login='controller';api.pages=lambda path:[]
        valid={'enforce_admins':{'enabled':True},'required_status_checks':{'strict':True,'contexts':['go']},'restrictions':{'users':[{'login':'controller'}],'teams':[],'apps':[]}}
        api.rule=copy.deepcopy(valid);api.get=lambda path:api.rule
        self.assertTrue(api.protection('main',['go']))
        api.rule['required_status_checks']['strict']=False
        self.assertFalse(api.protection('main',['go']))
        api.rule=copy.deepcopy(valid);api.rule['required_status_checks']['contexts']=[]
        self.assertFalse(api.protection('main',['go']))


class ComposeTargetBaselineBoundaryTests(unittest.TestCase):
    def test_different_selected_release_stack_cannot_replace_generated_baseline(self):
        from renovate_policy_data import COMPOSE_PACKAGES
        api=GitHub('credential','fin-assist/android');writes=[]
        target={'packageRules':[{'groupName':'androidx compose stack','separateMajorMinor':False,'matchPackageNames':COMPOSE_PACKAGES+['androidx.navigation3:**']}]}
        def get(path):
            if path.startswith('branches/'):return {'commit':{'sha':'release-sha'}}
            if path=='contents/renovate-base.json?ref=release-sha':return {'content':base64.b64encode(json.dumps(target).encode()).decode()}
            raise AssertionError('Baseline mismatch must fail before reading/writing generated preset')
        api.get=get;api.request=lambda *args:writes.append(args)
        with self.assertRaisesRegex(ValueError,'coordinate code/preset migration'):api.sync_baseline('release/1.0.0')
        self.assertEqual(writes,[])
    def test_saved_old_stack_shape_requires_explicit_owner_migration(self):
        from unittest.mock import patch
        from renovate_policy import compose_stack_rule
        import renovate_policy_data
        rule=compose_stack_rule(123)
        api=GitHub('credential','fin-assist/android')
        saved={'baseBranchPatterns':['main'],'packageRules':[rule]}
        api.get=lambda path:{'sha':'saved-sha','content':base64.b64encode(json.dumps(saved).encode()).decode()}
        with patch.object(renovate_policy_data,'COMPOSE_PACKAGES',renovate_policy_data.COMPOSE_PACKAGES+['androidx.navigation3:**']):
            with self.assertRaises(ValueError):api.read_policy()

if __name__ == '__main__':
    unittest.main()
