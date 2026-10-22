/** Real Renovate commit/body rendering; invoke with Node 24 and a process timeout. */
import assert from 'node:assert/strict';
import {mock} from 'node:test';
import {readFileSync,realpathSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';

assert.ok(process.env.RENOVATE_RUNTIME);
const pkg=realpathSync(resolve(process.env.RENOVATE_RUNTIME,'node_modules/renovate'));
assert.equal(JSON.parse(readFileSync(resolve(pkg,'package.json'))).version,'44.176.2');
const mod=path=>pathToFileURL(resolve(pkg,'dist',path)).href;
const artifacts=()=>({updatedArtifacts:[],artifactErrors:[]});
mock.module(mod('workers/global/limits.js'),{namedExports:{calcLimit:()=>0,counts:{},getCount:()=>0,
  hasMultipleLimits:()=>false,incCountValue(){},incLimitedValue(){},isLimitReached:()=>false,setCount(){},setMaxLimit(){}}});
mock.module(mod('workers/repository/update/branch/get-updated.js'),{namedExports:{getUpdatedPackageFiles:async()=>({
  updatedPackageFiles:[{type:'addition',path:'package.json',contents:'{}'}],updatedArtifacts:[],artifactErrors:[]})}});
mock.module(mod('modules/manager/npm/post-update/index.js'),{namedExports:{getAdditionalFiles:async()=>artifacts(),
  determineLockFileDirs(){},updateYarnBinary(){},writeExistingFiles(){},writeUpdatedPackageFiles(){}}});
mock.module(mod('modules/manager/github-actions/artifacts.js'),{namedExports:{updateActionsLockfile:async()=>artifacts()}});
mock.module(mod('workers/repository/changelog/index.js'),{namedExports:{embedChangelog:async()=>{},embedChangelogs:async()=>{}}});
mock.module(mod('workers/repository/update/branch/execute-post-upgrade-commands.js'),{
  defaultExport:async()=>null,namedExports:{postUpgradeCommandsExecutor:async()=>null}});
mock.module(mod('workers/repository/update/branch/bump-versions.js'),{namedExports:{bumpVersions:async()=>{}}});

const {init}=await import(mod('logger/index.js'));await init();
const {getConfig}=await import(mod('config/defaults.js'));
const {branchifyUpgrades}=await import(mod('workers/repository/updates/branchify.js'));
const {processBranch}=await import(mod('workers/repository/update/branch/index.js'));
const {getPrBody}=await import(mod('workers/repository/update/pr/body/index.js'));
const {massageMarkdown,maxBodyLength}=await import(mod('modules/platform/github/index.js'));
const {default:apis}=await import(mod('modules/platform/api.js'));
const {platformScmImpls}=await import(mod('modules/platform/scm.js'));
const {setPlatformApi}=await import(mod('modules/platform/index.js'));
const {GlobalConfig}=await import(mod('config/global.js'));
let capturedCommit='';
apis.set('github',{getBranchPr:async()=>null,findPr:async()=>null,massageMarkdown,maxBodyLength});
platformScmImpls.set('github',class {
  async branchExists(){return false;}
  async checkoutBranch(){}
  async commitAndPush(config){
    capturedCommit=config.message;
    const error=new Error('repository-changed');error.fixtureBoundary=true;throw error;
  }
});
setPlatformApi('github');GlobalConfig.set({dryRun:null});
const sourceConfigPath=process.env.RENOVATE_POLICY_CONFIG??fileURLToPath(new URL('../renovate.json',import.meta.url));
const sourceConfig=JSON.parse(readFileSync(sourceConfigPath));
assert.ok(sourceConfig.commitBody?.includes('fin211-update'));
const cfg={...getConfig(),semanticCommits:'disabled',errors:[],warnings:[],repoIsOnboarded:true,
  baseBranch:'main',commitBody:sourceConfig.commitBody,prBodyNotes:sourceConfig.prBodyNotes,
  packageRules:[{matchPackageNames:['*'],groupName:'large dependency group',separateMajorMinor:false}]};
const giant='untrusted-release-field-'.repeat(10000);
const deps=Array.from({length:20},(_,i)=>({depName:`pkg-${i}`,packageName:`pkg-${i}`,datasource:'npm',
  currentValue:'1.0.0',currentVersion:'1.0.0',logJSON:{giant},releases:[{notes:giant}],
  updates:[{newValue:'2.0.0',newVersion:'2.0.0',newMajor:2,updateType:'major'}]}));
deps.push({depName:'postgres',packageName:'postgres',datasource:'docker',currentValue:'18',
  currentDigest:'sha256:'+'0'.repeat(64),logJSON:{giant},releases:[{notes:giant}],
  updates:[{newValue:'18',newDigest:'sha256:'+'a'.repeat(64),updateType:'digest'}]});
const {branches}=await branchifyUpgrades(cfg,{npm:[{packageFile:'package.json',deps}]});
// Digest and ordinary upgrades can land on different manager/default branch keys.
const allUpgrades=branches.flatMap(branch=>branch.upgrades);
const branch={...branches[0],baseBranch:'main',upgrades:allUpgrades,
  commitBody:sourceConfig.commitBody,cacheFingerprintMatch:'no-match',dependencyDashboardChecks:{}};
assert.equal(branch.upgrades.length,21);
assert.ok(JSON.stringify(branch.upgrades).length>1000000,'Fixture must contain oversized nested upgrade data');
try {await processBranch(branch);}catch(error){if(!error.fixtureBoundary)throw error;}
assert.ok(capturedCommit,'Actual processBranch must reach the SCM commit adapter');
assert.equal((capturedCommit.match(/<!-- fin211-update /g)??[]).length,21);
assert.ok(Buffer.byteLength(capturedCommit)<12000,'Signed metadata must not serialize release notes or nested config');
assert.ok(!capturedCommit.includes('untrusted-release-field'));
const controller=process.env.RENOVATE_POLICY_SCRIPT??fileURLToPath(new URL('./renovate_policy.py',import.meta.url));
function parseProduction(message){
  const code="import importlib.util,json,sys; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1]).resolve().parent)); s=importlib.util.spec_from_file_location('policy',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.updates(sys.stdin.read())))";
  return JSON.parse(execFileSync('python3',['-c',code,controller],{input:message,encoding:'utf8',timeout:5000}));
}
const expected=Array.from({length:20},(_,i)=>({depName:`pkg-${i}`,newValue:'2.0.0',datasource:'npm',newMajor:2}));
expected.push({depName:'postgres',newValue:'18',datasource:'docker',newDigest:'sha256:'+'a'.repeat(64)});
const sort=items=>items.toSorted((a,b)=>a.depName.localeCompare(b.depName));
assert.deepEqual(sort(parseProduction(capturedCommit)),sort(expected));
// Exercise the actual GitHub truncation path with visible, oversized release notes.
branch.hasReleaseNotes=true;
for(const upgrade of branch.upgrades){
  upgrade.hasReleaseNotes=true;upgrade.repoName=upgrade.depName;
  upgrade.releases=[{version:'2.0.0',releaseNotes:{url:'https://example.com/release',body:giant}}];
}
const prBody=getPrBody(branch,{debugData:{targetBranch:'main'}},cfg);
assert.ok(prBody.includes('truncated'),'Visible release notes must exercise real platform truncation');
assert.ok(prBody.length<=maxBodyLength());
assert.equal((prBody.match(/<!-- fin211-update /g)??[]).length,21,
  'PR notes run once per upgrade, unlike commitBody: use the correct template context');
assert.deepEqual(sort(parseProduction(prBody)),sort(expected));
console.log(`PASS: actual signed-commit and GitHub PR rendering preserve 21 compact upgrades (${Buffer.byteLength(capturedCommit)} commit bytes, ${prBody.length} body characters/${Buffer.byteLength(prBody)} bytes, limit ${maxBodyLength()} characters)`);
