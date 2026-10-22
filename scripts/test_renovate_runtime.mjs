/**
 * Optional contract test against the real, pinned Renovate package (Node 24).
 * Install renovate@44.176.2 with --ignore-scripts in a temporary npm prefix, then:
 * RENOVATE_RUNTIME=/path/to/prefix node --experimental-test-module-mocks scripts/test_renovate_runtime.mjs
 * The runner must impose a process timeout. No network or repository mutations occur.
 */
import assert from 'node:assert/strict';
import { mock } from 'node:test';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

assert.ok(process.env.RENOVATE_RUNTIME, 'Set RENOVATE_RUNTIME to the temporary npm prefix');
const pkg = realpathSync(resolve(process.env.RENOVATE_RUNTIME, 'node_modules/renovate'));
assert.equal(JSON.parse(readFileSync(resolve(pkg, 'package.json'))).version, '44.176.2');
const mod = (path) => pathToFileURL(resolve(pkg, 'dist', path)).href;

// Preserve the actual approval/cache gates. External rate counters and the
// closed-PR notifier are stubs; the latter would otherwise delete saved branches.
mock.module(mod('workers/global/limits.js'), { namedExports: {
  calcLimit: () => 0, counts: {}, getCount: () => 0, hasMultipleLimits: () => false,
  incCountValue() {}, incLimitedValue() {}, isLimitReached: () => false,
  setCount() {}, setMaxLimit() {},
} });
mock.module(mod('workers/repository/update/branch/handle-existing.js'), { namedExports: {
  handleClosedPr: async () => {}, handleModifiedPr: async () => {},
} });
const { init } = await import(mod('logger/index.js'));
await init();
const { getConfig } = await import(mod('config/defaults.js'));
const { branchifyUpgrades } = await import(mod('workers/repository/updates/branchify.js'));
const { processBranch } = await import(mod('workers/repository/update/branch/index.js'));
const { ensureDependencyDashboard, readDashboardBody } = await import(mod('workers/repository/dependency-dashboard.js'));
const { default: apis } = await import(mod('modules/platform/api.js'));
const { platformScmImpls } = await import(mod('modules/platform/scm.js'));
const { setPlatformApi } = await import(mod('modules/platform/index.js'));
const { GlobalConfig } = await import(mod('config/global.js'));

let existingBranches = new Set();
const closedPrs = [];
function ignore(branch) { closedPrs.push({ branchName:branch.branchName, prTitle:branch.prTitle }); existingBranches.add(branch.branchName); }
let dashboardBody = '';
let checkedBody = '';
apis.set('github', {
  getBranchPr: async () => null,
  findPr: async (query) => closedPrs.some(pr => pr.branchName === query.branchName && (!query.prTitle || pr.prTitle === query.prTitle)) ? {number:1,state:'closed'} : null,
  getPr: async () => ({ state: 'closed' }),
  ensureIssue: async ({ body }) => { dashboardBody = body; },
  findIssue: async () => ({ number: 2, body: checkedBody }),
  maxBodyLength: () => 60000,
  massageMarkdown: (body) => body,
});
platformScmImpls.set('github', class {
  async branchExists(name) { return existingBranches.has(name); }
  async isBranchModified() { return false; }
  async checkoutBranch() {
    // Stop exactly before checking out a branch or calculating file writes.
    const error = new Error('repository-changed');
    error.fixtureBoundary = true;
    throw error;
  }
});
setPlatformApi('github');
GlobalConfig.set({ dryRun: null });

function dependency(name, value, old = '3.0.0', type = 'major') {
  return { depName: name, packageName: name, datasource: 'npm', currentValue: old,
    currentVersion: old, updates: [{ newValue: value, newVersion: value,
      newMajor: Number(value.split('.')[0]), updateType: type }] };
}
function productionRules(builder, items, generation = 123) {
  // Exercise both production builders, never an imitation of policy logic.
  const controller = process.env.RENOVATE_POLICY_SCRIPT ?? fileURLToPath(new URL('./renovate_policy.py', import.meta.url));
  const code = "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('policy',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(getattr(m,sys.argv[4])(json.loads(sys.argv[2]),int(sys.argv[3]))))";
  return JSON.parse(execFileSync('python3', ['-c', code, controller,
    JSON.stringify(items), String(generation), builder], {encoding:'utf8',timeout:5000}));
}
function update(name, value) {
  return {depName:name,newValue:value,datasource:'npm',newMajor:Number(value.split('.')[0])};
}
function failureRules(items, generation = 123) {
  return [...productionRules('recovery_rules',items,generation), ...productionRules('quarantine_rules',items,generation)];
}
function config(rules, {grouped = true} = {}) {
  return {...getConfig(), semanticCommits:'disabled', errors:[],warnings:[],
    repoIsOnboarded:true,baseBranch:'main',dependencyDashboard:true,recreateWhen:'auto',
    packageRules:[...(grouped?[{matchPackageNames:['*'],groupName:'weekly'}]:[]),...rules]};
}

async function branches(cfg, deps) {
  return (await branchifyUpgrades(cfg, { npm: [{ packageFile: 'package.json', deps }] })).branches;
}
async function gate(branch, checks = {}) {
  try {
    return await processBranch({ ...branch, cacheFingerprintMatch: 'no-match', dependencyDashboardChecks: checks });
  } catch (error) {
    if (error.fixtureBoundary) return { result: 'passed-approval-boundary' };
    throw error;
  }
}
async function explicitApproval(branch, cfg) {
  checkedBody = ` - [x] <!-- approve-branch=${branch.branchName} -->`;
  const dashboardConfig = { ...cfg };
  await readDashboardBody(dashboardConfig);
  assert.equal(dashboardConfig.dependencyDashboardChecks[branch.branchName], 'approve');
  return gate(branch, dashboardConfig.dependencyDashboardChecks);
}
const passed = 'passed-approval-boundary';

// Only an automatically failed dependency family may recreate its closed PR cache key.
const failedItem = update('a','4.0.0');
const [original] = await branches(config([], {grouped:false}),[dependency('a','4.0.0')]);
ignore(original);
const firstConfig = config(failureRules([failedItem]), {grouped:false});
const [firstRetry] = await branches(firstConfig,[dependency('a','4.0.0')]);
assert.equal((await gate(firstRetry)).result,'needs-approval');
assert.equal((await explicitApproval(firstRetry,firstConfig)).result,passed);
const [firstFix] = await branches(firstConfig,[dependency('a','4.0.1')]);
assert.equal(firstFix.recreateClosed,true);
assert.equal(firstFix.branchName,original.branchName);
assert.equal((await gate(firstFix)).result,passed);
ignore(firstFix); // Second automatic failure occurs on the first recovery branch.
const secondConfig = config(failureRules([update('a','4.0.1')],456),{grouped:false});
const [secondFix] = await branches(secondConfig,[dependency('a','4.0.2')]);
assert.equal(secondFix.recreateClosed,true);
assert.equal(secondFix.branchName,original.branchName);
assert.equal((await gate(secondFix)).result,passed);
console.log('PASS: successive failures keep future major fixes eligible through scoped recreation');

// Manual ignores outside this dependency/family retain their existing cache keys.
const [unrelated] = await branches(config([],{grouped:false}),[dependency('z','4.0.1')]);
const [otherMajor] = await branches(config([],{grouped:false}),[dependency('a','5.0.1')]);
ignore(unrelated);ignore(otherMajor);
for (const dep of [dependency('z','4.0.1'),dependency('a','5.0.1')]) {
  const [manual] = await branches(secondConfig,[dep]);
  assert.ok(!manual.branchName.includes('recovery'));
  assert.equal((await gate(manual)).result,'already-existed');
}
console.log('PASS: unrelated dependencies and other majors remain manually ignored');

// Recovered updates retain the original grouping and branch identity.
const groupItems = [update('a','4.0.0'),update('b','4.0.0')];
const [originalGroup] = await branches(config([]),[dependency('a','4.0.0'),dependency('b','4.0.0')]);
ignore(originalGroup);
const groupConfig = config(failureRules(groupItems));
const futureGroup = await branches(groupConfig,[dependency('a','4.0.1'),dependency('b','4.0.2')]);
assert.equal(futureGroup.length,1);
assert.equal(futureGroup[0].upgrades.length,2);
assert.equal(futureGroup[0].branchName,originalGroup.branchName);
assert.equal((await gate(futureGroup[0])).result,passed);
console.log('PASS: future fixes of the failed group stay grouped and eligible');

// A failed member is isolated while a newer member of its original group proceeds.
const mixed = await branches(groupConfig,[dependency('a','4.0.0'),dependency('b','4.0.2')]);
assert.equal(mixed.length,2);
assert.equal((await gate(mixed.find(branch=>branch.depName==='a'))).result,'needs-approval');
assert.equal((await gate(mixed.find(branch=>branch.depName==='b'))).result,passed);
console.log('PASS: failed group member waits while newer group member proceeds');

// The exact failed group becomes visible approvals; real Dashboard checkboxes work.
const exact = await branches(groupConfig,[dependency('a','4.0.0'),dependency('b','4.0.0')]);
assert.equal(exact.length,2);
for (const branch of exact) {
  assert.equal((await gate(branch)).result,'needs-approval');
  assert.equal((await explicitApproval(branch,groupConfig)).result,passed);
}
await ensureDependencyDashboard({...groupConfig,dependencyDashboardChecks:{}},
  exact.map(branch=>({...branch,result:'needs-approval'})),{},{});
assert.match(dashboardBody,/## Pending Approval/);
for (const branch of exact) assert.ok(dashboardBody.includes(`<!-- approve-branch=${branch.branchName} -->`));
console.log('PASS: exact failed group requires approval and real Dashboard retries pass');

// Closing an explicitly retried exact version creates a fresh approval generation.
for (const branch of exact) ignore(branch);
const nextConfig = config(failureRules(groupItems,456));
const next = await branches(nextConfig,[dependency('a','4.0.0'),dependency('b','4.0.0')]);
for (const branch of next) {
  assert.ok(branch.branchName.includes('fin211-retry-456-'));
  assert.equal((await gate(branch)).result,'needs-approval');
}
console.log('PASS: another failed retry requires a fresh explicit approval');

// Ordinary minor fixes also escape the exact failed-value quarantine.
const minorConfig = config(failureRules([update('a','3.1.0')]),{grouped:false});
const [minor] = await branches(minorConfig,[dependency('a','3.1.1','3.0.0','minor')]);
assert.equal(minor.dependencyDashboardApproval,false);
assert.equal((await gate(minor)).result,passed);
console.log('PASS: future minor/patch values remain eligible');

// Moving tags must not hide a newer security/fixed digest under the same tag.
const { applyPackageRules } = await import(mod('util/package-rules/index.js'));
const controllerPath = process.env.RENOVATE_POLICY_SCRIPT ?? fileURLToPath(new URL('./renovate_policy.py', import.meta.url));
const digestInput = [{ depName:'postgres', datasource:'docker', newValue:'18', newDigest:'sha256:bad' }];
const digestCode = "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('policy',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.quarantine_rules(json.loads(sys.argv[2]),99)))";
const digestRules = JSON.parse(execFileSync('python3',['-c',digestCode,controllerPath,JSON.stringify(digestInput)],{encoding:'utf8',timeout:5000}));
for (const newDigest of ['sha256:bad','sha256:fixed']) {
  const matched = await applyPackageRules({ packageName:'postgres', depName:'postgres', datasource:'docker', newValue:'18', newDigest, packageRules:digestRules }, 'test');
  assert.equal(matched.dependencyDashboardApproval === true, newDigest === 'sha256:bad');
}
console.log('PASS: fixed digest under the same tag escapes exact failed-digest approval');

// The documented exception is restricted to the recovered dependency family.
ignore(secondFix);
assert.equal((await gate(secondFix)).result,passed);
console.log('PASS: same-family manual ignore may be recreated after automatic recovery');

// Recovery never changes group branch keys or separates unaffected group members.
const [withUnaffected] = await branches(config(failureRules([update('a','4.0.0')])),
  [dependency('a','4.0.1'),dependency('c','5.0.1')]);
assert.equal(withUnaffected.upgrades.length,2);
assert.equal(withUnaffected.branchName,originalGroup.branchName);
assert.equal((await gate(withUnaffected)).result,passed);
console.log('PASS: recovered and unaffected dependencies remain in one original group');
