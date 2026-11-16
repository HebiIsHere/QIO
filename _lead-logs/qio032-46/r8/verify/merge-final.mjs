import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
const root = 'D:\\qio-dev\\q46-r8\\_lead-logs\\qio032-46\\r8';
const finalDir = join(root, 'final');
const picks = [
  ['g1', null],           // all
  ['g2c', null],          // s2 + s2b
  ['g3b', null],          // s3a + s3b
  ['g4', (r) => r.name.startsWith('7)')],  // s4 from the s4+s5 run
  ['g5', null],           // s5 observe
  ['g6', (r) => r.name.startsWith('11)')], // s7a pass
  ['g6j', null],          // s6a final pass
];
const results = [];
const logs = [];
for (const [group, filter] of picks) {
  const file = join(finalDir, group, 'summary.json');
  if (!existsSync(file)) { console.error('missing', group); continue; }
  const s = JSON.parse(readFileSync(file, 'utf8'));
  for (const r of s.results || []) {
    if (filter && !filter(r)) continue;
    results.push(Object.assign({ group }, r));
  }
  const logFile = join(finalDir, group + '.log');
  if (existsSync(logFile)) logs.push('##### ' + group + ' #####\n' + readFileSync(logFile, 'utf8'));
}
const summary = {
  harness: 'QIO #46 R8 browser acceptance (run-48.mjs) · merged final runs',
  at: new Date().toISOString(),
  root: finalDir,
  picks: picks.map((p) => p[0]),
  pass: results.filter((r) => r.ok && !r.observe).length,
  fail: results.filter((r) => !r.ok && !r.observe).length,
  observe: results.filter((r) => r.observe).length,
  total: results.length,
  results,
  note: '真实 Edge + CDP 驱动真实前端 store 与隔离后端（q46-r8/backend，数据目录 r8/e2e-data/final 隔离，model 只打本机 fake provider）；失败注入（CDP Fetch）与真实回执分开标注；s2b/s5 为驱动无法隔离时序的 UNVERIFIED（OBSERVE），产品语义由 store 用例覆盖。',
};
writeFileSync(join(finalDir, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
writeFileSync(join(finalDir, 'run.log'), logs.join('\n\n'), 'utf8');
console.log('merged', summary.pass, 'PASS /', summary.fail, 'FAIL /', summary.observe, 'OBSERVE /', summary.total, 'TOTAL');
