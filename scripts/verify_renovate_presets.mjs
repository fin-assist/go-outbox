// Optional live check. Use the pinned Renovate runtime and an outer process timeout.
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const runtime = resolve(process.env.RENOVATE_RUNTIME, 'node_modules/renovate/dist');
const load = (name) => import(pathToFileURL(resolve(runtime, name)).href);
const { init } = await load('logger/index.js');
const { GlobalConfig } = await load('config/global.js');
const { resolveConfigPresets } = await load('config/presets/index.js');
const { add } = await load('util/host-rules.js');
import { readFileSync } from 'node:fs';
await init();
GlobalConfig.set({platform:'github',endpoint:'https://api.github.com/'});
if (process.env.RENOVATE_TOKEN) add({hostType:'github',matchHost:'https://api.github.com/',token:process.env.RENOVATE_TOKEN});
for (const repo of (process.argv.length > 2 ? process.argv.slice(2) : [process.cwd()])) {
 const root=JSON.parse(readFileSync(repo+'/renovate.json'));
 const baseline=JSON.parse(readFileSync(repo+'/renovate-base.json'));
 const {config:resolved}=await resolveConfigPresets(root,{});
 if (resolved.baseBranchPatterns?.length!==1 || (process.env.EXPECTED_RENOVATE_BASE && resolved.baseBranchPatterns[0]!==process.env.EXPECTED_RENOVATE_BASE)) throw new Error('Wrong target: '+repo);
 for (const rule of baseline.packageRules??[]) {
  if (!resolved.packageRules.some(actual=>Object.entries(rule).filter(([key])=>key!=='description').every(([key,value])=>JSON.stringify(actual[key])===JSON.stringify(value)))) throw new Error('Lost baseline rule: '+repo);
 }
 console.log(repo.split('/').at(-1)+': both live presets resolved, baseline preserved');
}
