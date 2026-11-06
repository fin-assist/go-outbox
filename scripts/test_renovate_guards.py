"""Exercise live target-protection and conflict guards before privileged writes."""
import copy
import unittest

from renovate_policy import GitHub, reconcile
from test_renovate_controller import Fake


def protected_target():
    return {'enforce_admins': {'enabled': True},
            'required_status_checks': {'strict': True, 'contexts': ['go'],
                                      'checks': [{'context': 'go', 'app_id': 15368}]},
            'restrictions': {'users': [{'login': 'controller'}], 'teams': [], 'apps': []},
            'allow_force_pushes': {'enabled': False}, 'allow_deletions': {'enabled': False},
            'block_creations': {'enabled': True}}


class ProtectionContractTests(unittest.TestCase):
    def test_wrong_absent_duplicate_binding_or_force_delete_fails_before_baseline_sync(self):
        class GuardedFake(Fake):
            def protection(self, base, names):
                return GitHub.protection(self, base, names)

            def get(self, path):
                if path.endswith('/protection'):
                    return copy.deepcopy(self.target_rule)
                return super().get(path)

            def pages(self, path, key=None):
                if path == 'rulesets?includes_parents=true':
                    return []
                return super().pages(path, key)

            def sync_baseline(self, base):
                raise AssertionError('Invalid target protection cannot publish a baseline')

        weak = []
        for app_id in (99, -1, None, '15368'):
            rule = protected_target()
            rule['required_status_checks']['checks'][0]['app_id'] = app_id
            weak.append(rule)
        for bindings in ([], None, [{'context': 'other', 'app_id': 15368}],
                         [{'context': 'go', 'app_id': 15368}, {'context': 'go', 'app_id': 99}]):
            rule = protected_target()
            rule['required_status_checks']['checks'] = bindings
            weak.append(rule)
        rule = protected_target()
        del rule['required_status_checks']['checks']
        weak.append(rule)
        for key in ('allow_force_pushes', 'allow_deletions'):
            for enabled in (True, None):
                rule = protected_target()
                rule[key] = {'enabled': enabled}
                weak.append(rule)
            rule = protected_target()
            del rule[key]
            weak.append(rule)
        for rule in weak:
            with self.subTest(rule=rule):
                api = GuardedFake()
                api.target_rule = rule
                with self.assertRaisesRegex(ValueError, 'Target branch'):
                    reconcile(api, api.config)
                self.assertEqual(api.actions, [])
                self.assertEqual(api.receipts, {})

    def test_valid_main_release_and_additional_bound_checks_pass(self):
        api = GitHub('', 'owner/repo')
        api.controller_login = 'controller'
        rule = protected_target()
        rule['required_status_checks']['contexts'].append('external')
        rule['required_status_checks']['checks'].append({'context': 'external', 'app_id': 99})
        api.get = lambda path: copy.deepcopy(rule)
        api.pages = lambda path: []
        self.assertTrue(api.protection('main', ['go']))
        self.assertTrue(api.protection('release/1.0.0', ['go']))
        self.assertFalse(api.protection('main', ['go', 'external']))


class ConflictLifecycleTests(unittest.TestCase):
    def failed(self, attempt):
        api = Fake()
        api.run.update(conclusion='failure', run_attempt=attempt)
        api.jobs[0]['conclusion'] = 'failure'
        api.checks[0]['conclusion'] = 'failure'
        if attempt == 2:
            api.receipts = {'1:abc': {'state': {'sha': 'abc', 'runs': {
                'ci.yml': {'run_id': 1, 'attempt': 1, 'retried': True}}}}}
        return api

    def test_initial_and_late_conflict_or_unknown_do_not_claim_quarantine_or_close(self):
        for attempt in (1, 2):
            for late in (False, True):
                for mergeable, state in ((False, 'dirty'), (None, 'unknown'),
                                         (True, 'dirty'), (True, 'unknown'), (True, None)):
                    with self.subTest(attempt=attempt, late=late, mergeable=mergeable, state=state):
                        api = self.failed(attempt)
                        receipts = copy.deepcopy(api.receipts)
                        if late:
                            api.before_fresh = lambda a: a.pr.update(
                                mergeable=mergeable, mergeable_state=state)
                        else:
                            api.pr.update(mergeable=mergeable, mergeable_state=state)
                        reconcile(api, api.config)
                        self.assertEqual(api.actions, [])
                        self.assertEqual(api.comments, [])
                        self.assertEqual(api.receipts, receipts)
                        self.assertEqual(api.pr['state'], 'open')

    def test_conflict_after_persisted_claim_keeps_budget_without_dispatch_or_redelivery(self):
        for mergeable, state in ((False, 'dirty'), (None, 'unknown'),
                                 (True, 'dirty'), (True, 'unknown'), (True, None)):
            with self.subTest(mergeable=mergeable, state=state):
                api = self.failed(1)
                policy = copy.deepcopy(api.policy)
                write_receipts = api.write_receipts

                def persist_then_conflict(data, sha):
                    result = write_receipts(data, sha)
                    api.pr.update(mergeable=mergeable, mergeable_state=state)
                    return result

                api.write_receipts = persist_then_conflict
                reconcile(api, api.config)
                expected = {'sha': 'abc', 'runs': {
                    'ci.yml': {'run_id': 1, 'attempt': 1, 'retried': True}}}
                self.assertEqual(api.receipts['1:abc']['state'], expected)
                self.assertFalse(any(path.endswith('/rerun-failed-jobs') for path, _ in api.actions))
                self.assertEqual(api.policy, policy)
                self.assertEqual(api.pr['state'], 'open')
                actions, receipts = copy.deepcopy(api.actions), copy.deepcopy(api.receipts)
                reconcile(api, api.config)
                self.assertEqual(api.actions, actions)
                self.assertEqual(api.receipts, receipts)
                # Resolving the conflict does not reset a persisted retry claim.
                api.pr.update(mergeable=True, mergeable_state='clean')
                with self.assertRaisesRegex(ValueError, 'Renovate reconciliation failed'):
                    reconcile(api, api.config)
                self.assertFalse(any(path.endswith('/rerun-failed-jobs') for path, _ in api.actions))
                self.assertEqual(api.receipts, receipts)
                self.assertEqual(api.policy, policy)
                self.assertEqual(api.pr['state'], 'open')

    def test_conflict_during_policy_read_blocks_quarantine_before_policy_write(self):
        for mergeable, state in ((False, 'dirty'), (None, 'unknown'),
                                 (True, 'dirty'), (True, 'unknown'), (True, None)):
            with self.subTest(mergeable=mergeable, state=state):
                api = self.failed(2)
                policy, receipts = copy.deepcopy(api.policy), copy.deepcopy(api.receipts)
                read_policy = api.read_policy
                api.before_fresh = lambda a: setattr(a, 'closing', True)

                def read_then_conflict():
                    result = read_policy()
                    if getattr(api, 'closing', False):
                        api.pr.update(mergeable=mergeable, mergeable_state=state)
                    return result

                api.read_policy = read_then_conflict
                reconcile(api, api.config)
                self.assertEqual(api.policy, policy)
                self.assertEqual(api.receipts, receipts)
                self.assertEqual(api.actions, [])
                self.assertEqual(api.comments, [])
                self.assertEqual(api.pr['state'], 'open')
                reconcile(api, api.config)
                self.assertEqual(api.policy, policy)
                self.assertEqual(api.receipts, receipts)
                self.assertEqual(api.actions, [])

    def test_known_nonconflicting_red_ci_still_retries_and_closes(self):
        for state in ('clean', 'unstable', 'blocked', 'behind', 'has_hooks'):
            with self.subTest(state=state):
                api = self.failed(1)
                api.pr['mergeable_state'] = state
                reconcile(api, api.config)
                self.assertTrue(any(path.endswith('/rerun-failed-jobs') for path, _ in api.actions))
                api.run['run_attempt'] = 2
                reconcile(api, api.config)
                self.assertEqual(api.pr['state'], 'closed')


if __name__ == '__main__':
    unittest.main()
