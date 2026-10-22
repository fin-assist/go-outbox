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
            self.assertFalse(trusted(pr,[commit,changed],repo))
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

if __name__ == '__main__': unittest.main()

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
