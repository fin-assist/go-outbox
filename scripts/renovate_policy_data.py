"""Validate the complete generated Renovate preset before it becomes trusted input."""
import json
import re

RECOVERY_DESCRIPTION = 'FIN-211: recovery of an automatically failed dependency family'
QUARANTINE_DESCRIPTION = 'FIN-211: failed twice; retry explicitly in Dependency Dashboard'
COMMON_KEYS = {'description', 'matchPackageNames', 'matchDatasources', 'recreateWhen'}
QUARANTINE_KEYS = COMMON_KEYS | {'dependencyDashboardApproval', 'groupName', 'additionalBranchPrefix'}

COMPOSE_DESCRIPTION = 'FIN-211: failed Compose stack requires grouped Dashboard approval'
COMPOSE_PACKAGES = ['androidx.compose:compose-bom', 'androidx.activity:**', 'androidx.core:**',
                    'androidx.fragment:**', 'androidx.hilt:hilt-navigation-compose',
                    'androidx.lifecycle:**', 'androidx.navigation:**']
COMPOSE_KEYS = {'description', 'matchPackageNames', 'groupName', 'separateMajorMinor',
                'dependencyDashboardApproval', 'recreateWhen', 'additionalBranchPrefix', 'automerge'}


def _reject(detail):
    raise ValueError('Invalid generated Renovate policy: ' + detail + '; review the renovate-policy branch before enabling automation')


def _literal(value, pattern, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and re.fullmatch(pattern, value) is not None


def _one_literal(values, pattern, limit):
    return isinstance(values, list) and len(values) == 1 and _literal(values[0], pattern, limit)


def _exact_value_regex(value):
    if not isinstance(value, str) or not value.startswith('/^') or not value.endswith('$/') or len(value) > 2048:
        return False
    encoded = value[2:-2]
    decoded = []
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == '\\':
            index += 1
            if index == len(encoded):
                return False
            char = encoded[index]
        decoded.append(char)
        index += 1
    literal = ''.join(decoded)
    return (0 < len(literal) < 512 and all(ord(char) >= 32 and ord(char) != 127 for char in literal)
            and re.escape(literal) == encoded)


def _digest_expression(expressions):
    if not isinstance(expressions, list) or len(expressions) != 1:
        return False
    expression = expressions[0]
    prefix = 'newDigest = '
    if not isinstance(expression, str) or not expression.startswith(prefix) or len(expression) > 4096:
        return False
    encoded = expression[len(prefix):]
    try:
        literal = json.loads(encoded)
    except (ValueError, TypeError):
        return False
    return (isinstance(literal, str) and 0 < len(literal) < 512
            and all(ord(char) >= 32 and ord(char) != 127 for char in literal)
            and json.dumps(literal) == encoded)


def validate_policy(value):
    """Return the same preset only if every field matches a generated rule shape."""
    if not isinstance(value, dict) or set(value) != {'baseBranchPatterns', 'packageRules'}:
        _reject('only baseBranchPatterns and packageRules are allowed')
    if not _one_literal(value['baseBranchPatterns'], r'(?:main|release/[0-9]+\.[0-9]+\.[0-9]+)', 128):
        _reject('expected exactly one main or release/X.Y.Z target')
    rules = value['packageRules']
    if not isinstance(rules, list) or len(rules) > 1000:
        _reject('packageRules must contain at most 1000 generated rules')
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            _reject(f'rule {index} is not an object')
        if rule.get('description') == COMPOSE_DESCRIPTION:
            if (set(rule) != COMPOSE_KEYS or rule.get('matchPackageNames') != COMPOSE_PACKAGES
                    or rule.get('groupName') != 'androidx compose stack'
                    or rule.get('separateMajorMinor') is not False
                    or rule.get('dependencyDashboardApproval') is not True
                    or rule.get('recreateWhen') != 'never' or rule.get('automerge') is not False
                    or not _literal(rule.get('additionalBranchPrefix'), r'fin211-retry-[1-9][0-9]*-', 128)):
                _reject(f'rule {index} must preserve the fixed Compose stack approval group')
            continue
        if not _one_literal(rule.get('matchPackageNames'), r'[A-Za-z0-9@][A-Za-z0-9@._:/+\-]*', 511):
            _reject(f'rule {index} package name must be one literal, not a glob or regex')
        if not _one_literal(rule.get('matchDatasources'), r'[a-z][a-z0-9-]*', 128):
            _reject(f'rule {index} datasource must be one literal')
        description = rule.get('description')
        if description == RECOVERY_DESCRIPTION:
            keys = COMMON_KEYS
            if rule.get('recreateWhen') != 'always':
                _reject(f'rule {index} recovery must recreate only its matched family')
            expressions = rule.get('matchJsonata')
            matcher_valid = (isinstance(expressions, list) and len(expressions) == 1
                             and isinstance(expressions[0], str)
                             and re.fullmatch(r'newMajor = (?:0|[1-9][0-9]{0,18})', expressions[0]) is not None)
        elif description == QUARANTINE_DESCRIPTION:
            keys = QUARANTINE_KEYS
            if (rule.get('recreateWhen') != 'never' or rule.get('dependencyDashboardApproval') is not True
                    or rule.get('groupName', False) is not None
                    or not _literal(rule.get('additionalBranchPrefix'), r'fin211-retry-[1-9][0-9]*-', 128)):
                _reject(f'rule {index} quarantine approval or retry prefix is invalid')
            matcher_valid = _digest_expression(rule.get('matchJsonata'))
        else:
            _reject(f'rule {index} description is not generated')
        if 'matchNewValue' in rule:
            keys = keys | {'matchNewValue'}
            matcher_valid = _exact_value_regex(rule['matchNewValue'])
        else:
            keys = keys | {'matchJsonata'}
        if set(rule) != keys or not matcher_valid:
            _reject(f'rule {index} fields or exact matcher are not generated')
    return value
