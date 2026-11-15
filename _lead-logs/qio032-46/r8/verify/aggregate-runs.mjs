/**
 * 把分组跑的 R8 浏览器验收结果汇总成 baseline/summary.json 与 baseline/run.log。
 * 只读各组 summary.json / 日志，不改任何产品文件。
 * 用法：node aggregate-runs.mjs <OutRoot> [group...]
 */
import { readFileSync, writeFileSync, readdirSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const root = resolve(process.argv[2] || 'D:/qio-dev/q46-r8/_lead-logs/qio032-46/r8/baseline');
const wanted = process.argv.slice(3);
const groups = readdirSync(root)
  .filter((name) => /^g\d+$/.test(name) && existsSync(join(root, name, 'summary.json')))
  .filter((name) => wanted.length === 0 || wanted.indexOf(name) >= 0)
  .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));

const results = [];
const logs = [];
const groupSummaries = [];
for (const g of groups) {
  const dir = join(root, g);
  const s = JSON.parse(readFileSync(join(root, g, 'summary.json'), 'utf8'));
  groupSummaries.push({ group: g, out: dir, pass: s.pass, fail: s.fail, observe: s.observe, total: s.total, results: (s.results || []).map((r) => r.name) });
  for (const r of s.results || []) results.push(Object.assign({ group: g }, r));
  const logFile = readdirSync(root).find((n) => n.indexOf(g + '-') === 0 && n.endsWith('.log'));
  if (logFile) {
    const text = readFileSync(join(root, logFile), 'utf8');
    logs.push('##### ' + g + ' (' + logFile + ') #####\n' + text);
  }
}
const summary = {
  harness: 'QIO #46 R8 browser acceptance (run-48.mjs) · grouped runs',
  at: new Date().toISOString(),
  root,
  groups,
  groupSummaries,
  pass: results.filter((r) => r.ok && !r.observe).length,
  fail: results.filter((r) => !r.ok && !r.observe).length,
  observe: results.filter((r) => r.observe).length,
  total: results.length,
  results,
  note: '真实 Edge + CDP 驱动真实前端 store 与隔离后端（backend 8735 / fake provider 8808 / 数据目录 r8/e2e-data 隔离）。失败注入与真实回执在 detail 里分别记录；驱动不稳记 OBSERVE/UNVERIFIED。',
};
writeFileSync(join(root, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
writeFileSync(join(root, 'run.log'), logs.join('\n\n'), 'utf8');
console.log('aggregated', groups.length, 'groups,', summary.pass, 'PASS /', summary.fail, 'FAIL /', summary.observe, 'OBSERVE /', summary.total, 'TOTAL');
console.log('groups:', groupSummaries.map((g) => g.group + '=' + g.pass + 'P/' + g.fail + 'F').join(' '));
