"""Preset schema tests exercise the full trust boundary, not a partial allowlist."""
import copy
import json
import re
import unittest

from renovate_policy_data import (RECOVERY_DESCRIPTION, QUARANTINE_DESCRIPTION, COMPOSE_DESCRIPTION, COMPOSE_PACKAGES, NO_DIGEST, HAS_DIGEST, validate_policy)


def recovery(**changes):
    return dict(description=RECOVERY_DESCRIPTION, matchPackageNames=['github.com/fin-assist/go-identity'],
                matchDatasources=['go'], recreateWhen='always', matchJsonata=['newMajor = 4'], **changes)


def quarantine():
    return dict(description=QUARANTINE_DESCRIPTION, matchPackageNames=['@scope/package'],
                matchDatasources=['npm'], recreateWhen='never', dependencyDashboardApproval=True,
                groupName=None, additionalBranchPrefix='fin211-retry-123-', matchJsonata=[NO_DIGEST], matchNewValue='/^4\\.0\\.0$/')


def compose():
    return dict(description=COMPOSE_DESCRIPTION, matchPackageNames=list(COMPOSE_PACKAGES),
                groupName='androidx compose stack', separateMajorMinor=False,
                dependencyDashboardApproval=True, recreateWhen='never',
                additionalBranchPrefix='fin211-retry-123-', automerge=False)


def policy(*rules):
    return {'baseBranchPatterns': ['main'], 'packageRules': list(rules)}


class PolicyDataTests(unittest.TestCase):
    def reject_rule(self, value):
        with self.assertRaisesRegex(ValueError, 'review the renovate-policy branch'):
            validate_policy(policy(value))

    def test_empty_and_full_policies_return_the_original_object(self):
        for value in [policy(), policy(recovery(), quarantine())]:
            self.assertIs(validate_policy(value), value)
        value = policy(quarantine())
        value['baseBranchPatterns'] = ['release/0.2.10']
        self.assertIs(validate_policy(value), value)

    def test_real_production_builders_are_accepted_when_available(self):
        try:
            from renovate_policy import recovery_rules, quarantine_rules
        except ImportError:
            self.skipTest('Production controller is not installed beside the validator')
        items = [{'depName': 'postgres', 'datasource': 'docker', 'newValue': '18', 'newDigest': 'sha256:abc'},
                 {'depName': 'github.com/fin-assist/go-identity', 'datasource': 'go', 'newValue': 'v4.0.0'}]
        value = policy(*(recovery_rules(items, 123) + quarantine_rules(items, 123)))
        self.assertIs(validate_policy(value), value)

    def test_repository_package_name_formats_are_literal(self):
        for name in ['@scope/pkg', 'androidx.compose:compose-bom', 'github.com/fin-assist/contracts/gen/go',
                     'actions/checkout', 'ghcr.io/fin-assist/service', 'postgres', 'org.jetbrains.kotlin:kotlin-gradle-plugin']:
            rule = quarantine()
            rule['matchPackageNames'] = [name]
            self.assertIs(validate_policy(policy(rule))['packageRules'][0], rule)

    def test_top_level_overrides_missing_fields_and_multiple_bases_rejected(self):
        for value in [{}, [], None, dict(policy(), automerge=True), dict(policy(), ignoreDeps=['x']),
                      {'baseBranchPatterns': ['main']}, dict(policy(), baseBranchPatterns=['main', 'release/1.0.0']),
                      dict(policy(), baseBranchPatterns=['release/*']), dict(policy(), baseBranchPatterns=['!main']),
                      dict(policy(), baseBranchPatterns=['feature/x'])]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy(value)

    def test_rule_overrides_and_matcher_combinations_rejected(self):
        for name, value in [('automerge', True), ('ignoreDeps', ['x']), ('allowedVersions', '<4'),
                            ('enabled', False), ('matchUpdateTypes', ['major']), ('group', {}),
                            ('matchJsonata', ['newDigest = "sha256:x"'])]:
            rule = quarantine()
            rule[name] = value
            self.reject_rule(rule)
        for key in quarantine():
            rule = quarantine()
            del rule[key]
            self.reject_rule(rule)

    def test_glob_regex_and_negative_package_matchers_rejected(self):
        for name in ['*', 'foo/*', '!foo', '/foo/', '/^foo$/', 'foo?', 'foo[bar]', 'foo{a,b}',
                     'foo\\bar', 'foo bar', '', '-foo', 'x\nname']:
            rule = quarantine()
            rule['matchPackageNames'] = [name]
            self.reject_rule(rule)
        for names in [[], ['x', 'y'], 'x']:
            rule = quarantine()
            rule['matchPackageNames'] = names
            self.reject_rule(rule)

    def test_only_exact_escaped_value_patterns_are_accepted(self):
        for version in ['4.0.0', '^2.1.0', 'v4', '18-alpine', 'https://example.com/v1', 'tag+build', 'a$b', 'x\\y']:
            rule = quarantine()
            rule['matchNewValue'] = '/^' + re.escape(version) + '$/'
            validate_policy(policy(rule))
        for expression in ['/^.*$/', '/^4.0.0$/', '/^a|b$/', '/^a$|^b$/', '/foo/', '^foo$', '/^$/',
                           '/^\\d+$/', '/^foo\\$/', '/^foo$/i', '/^foo\n$/']:
            rule = quarantine()
            rule['matchNewValue'] = expression
            self.reject_rule(rule)

    def test_jsonata_allows_only_exact_new_major_or_serialized_digest(self):
        for expression in ['newMajor = 0', 'newMajor = 4', 'newMajor = 100']:
            rule = recovery()
            rule['matchJsonata'] = [expression]
            validate_policy(policy(rule))
        for digest in ['sha256:abc', 'digest"quoted', 'x\\y']:
            rule = quarantine()
            del rule['matchNewValue']
            rule['matchJsonata'] = ['newDigest = ' + json.dumps(digest)]
            validate_policy(policy(rule))
        for expression in ['true', 'newMajor = 4 or true', 'newMajor = -1', 'newMajor = 04',
                           'newMajor = 4.0', 'newDigest = "x" or true', 'newDigest = 1', 'newDigest = null',
                           'newDigest = ""', 'newDigest = "x" ', 'newDigest = "x\\n"']:
            for base in [recovery(), quarantine()]:
                rule = copy.deepcopy(base)
                rule.pop('matchNewValue', None)
                rule['matchJsonata'] = [expression]
                self.reject_rule(rule)

    def test_quarantine_flags_prefix_and_recovery_fields_are_strict(self):
        for key, value in [('recreateWhen', 'always'), ('dependencyDashboardApproval', 1),
                           ('groupName', ''), ('additionalBranchPrefix', 'fin211-retry-0-'),
                           ('additionalBranchPrefix', 'fin211-retry-123-evil'), ('description', 'human rule')]:
            rule = quarantine()
            rule[key] = value
            self.reject_rule(rule)
        self.reject_rule(dict(recovery(), additionalBranchPrefix='prefix'))
        self.reject_rule(dict(recovery(), recreateWhen='auto'))

    def test_compose_approval_preserves_exact_known_stack_group(self):
        value = policy(compose())
        self.assertIs(validate_policy(value), value)
        for key in compose():
            rule = compose()
            del rule[key]
            self.reject_rule(rule)
        for key, replacement in [('automerge', True), ('automerge', 0), ('groupName', 'other'),
                                 ('separateMajorMinor', True), ('recreateWhen', 'always'),
                                 ('dependencyDashboardApproval', False), ('enabled', False),
                                 ('matchPackageNames', ['androidx.**']),
                                 ('matchPackageNames', COMPOSE_PACKAGES + ['*']),
                                 ('additionalBranchPrefix', 'fin211-retry-0-')]:
            rule = compose()
            rule[key] = replacement
            self.reject_rule(rule)
        # Fixed wildcard patterns are allowed only in the dedicated stack shape.
        rule = compose()
        rule['description'] = QUARANTINE_DESCRIPTION
        self.reject_rule(rule)

    def test_rule_count_and_payload_types_are_bounded(self):
        validate_policy(policy(*[recovery() for _ in range(1000)]))
        with self.assertRaises(ValueError):
            validate_policy(policy(*[recovery() for _ in range(1001)]))
        for rules in [None, {}, [None], ['rule']]:
            with self.assertRaises(ValueError):
                validate_policy(dict(policy(), packageRules=rules))
        rule = quarantine()
        rule['matchNewValue'] = '/^' + 'x' * 512 + '$/'
        self.reject_rule(rule)



class PolicyReadWriteBoundaryTests(unittest.TestCase):
    def test_real_read_and_write_reject_unexpected_native_automerge_before_mutation(self):
        import base64
        from renovate_policy import GitHub
        class Fake(GitHub):
            def get(self,path):return {'sha':'preset-sha','content':base64.b64encode(json.dumps(self.value).encode()).decode()}
            def request(self,*args,**kwargs):raise AssertionError('Rejected policy must not be written')
        api=Fake('','x');api.value=dict(policy(),automerge=True)
        with self.assertRaises(ValueError):api.read_policy()
        with self.assertRaises(ValueError):api.write_policy(api.value,'preset-sha')
        from renovate_policy import compose_stack_rule
        self.assertIs(validate_policy(policy(compose_stack_rule(123)))['packageRules'][0]['automerge'],False)

class DigestScopeSchemaTests(unittest.TestCase):
    def test_exact_value_rules_accept_only_the_fixed_digest_presence_guard(self):
        normal=quarantine();validate_policy(policy(normal))
        for guard in ([HAS_DIGEST],['newDigest = null'],['$exists(newDigest)'],['true']):
            changed=copy.deepcopy(normal);changed['matchJsonata']=guard
            with self.assertRaises(ValueError):validate_policy(policy(changed))
        tag=recovery();tag.pop('matchJsonata');tag['matchNewValue']='/^18$/';tag['matchJsonata']=[HAS_DIGEST]
        validate_policy(policy(tag))
        tag['matchJsonata']=[NO_DIGEST]
        with self.assertRaises(ValueError):validate_policy(policy(tag))

if __name__ == '__main__':
    unittest.main()
