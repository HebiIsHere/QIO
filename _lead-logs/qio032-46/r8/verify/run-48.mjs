/**
 * QIO #46 第八轮（R8）浏览器验收 harness 入口（真实 Edge + CDP + 真实隔离后端）。
 *
 * 场景模块（同目录）：
 *  r8-s1.mjs  第 1 项（含 1b 提交回执迟到）
 *  r8-s2.mjs  第 2 项  两个真实标签页
 *  r8-s3.mjs  第 5/6 项 A/B/C 草稿队列
 *  r8-s4s5.mjs 第 7 项 切板代次 / 第 8 项 撤回代次
 *  r8-s6s7.mjs 第 9 项 附件替换重试 / 第 11 项 批量导入归板
 *
 * 环境变量（由 env-up-r8.ps1 起服务后传入）：
 *  QIO_E2E_BASE / QIO_E2E_API / QIO_E2E_PROVIDER / QIO_UI_OUT / QIO_UI_DATA_DIR /
 *  QIO_UI_CDP_PORT / QIO_UI_WT(默认 q46-r8) / Q48_ONLY(逗号分隔场景 id)
 */
process.env.QIO_UI_WT = process.env.QIO_UI_WT || 'D:/qio-dev/q46-r8';
process.env.QIO_BACKEND_ROOT = process.env.QIO_BACKEND_ROOT || 'D:/qio-dev/q46-r8';
process.env.QIO_UI_CDP_PORT = process.env.QIO_UI_CDP_PORT || '9334';

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const { H, OUT } = await import('./r8-common.mjs');
const { s1, s1b } = await import('./r8-s1.mjs');
const { s2 } = await import('./r8-s2.mjs');
const { s3a, s3b } = await import('./r8-s3.mjs');
const { s4, s5 } = await import('./r8-s4s5.mjs');
const { s6a, s7a } = await import('./r8-s6s7.mjs');

const ONLY = String(process.env.Q48_ONLY || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const RUN = Date.now().toString(36);
const STAMP = new Date().toISOString();

async function main() {
  mkdirSync(OUT, { recursive: true });
  mkdirSync(join(OUT, 'materials'), { recursive: true });
  const ctx = await H.setup({ outDir: OUT, mode: 'r8', cdpPort: Number(process.env.QIO_UI_CDP_PORT || 9334) });
  ctx.log('== QIO #46 第八轮（R8）浏览器验收 runId=' + new Date().toISOString());
  ctx.log('base=' + H.BASE + ' api=' + H.API + ' provider=' + H.PROVIDER + ' out=' + OUT + ' only=' + JSON.stringify(ONLY));
  ctx.log('wt=' + H.WT + ' dataDir=' + (process.env.QIO_UI_DATA_DIR || '') + ' cdp=' + (process.env.QIO_UI_CDP_PORT || 9334));
  await ctx.gotoApp('r8=' + RUN);
  await ctx.enterInteractive();
  const run = (id) => ONLY.length === 0 || ONLY.indexOf(id) >= 0;
  const scenarios = [
    ['s1', '第1项 提交始终回到点击时的板面（含 1b）', async (c) => {
      await s1(c);
      if (run('s1')) await s1b(c);
    }],
    ['s2', '第2项 双标签页旧回执不删新副本', s2],
    ['s3a', '第5项 单板失败不堵其他板草稿', s3a],
    ['s3b', '第6项 冲突板被跳过、后续板继续', s3b],
    ['s4', '第7项 切板代次', s4],
    ['s5', '第8项 撤回代次', s5],
    ['s6a', '第9项 附件替换重试', s6a],
    ['s7a', '第11项 批量导入归板', s7a],
  ];
  for (const [id, label, fn] of scenarios) {
    if (!run(id) && id !== 's1') continue;
    if (id === 's1' && ONLY.length && ONLY.indexOf('s1') < 0) continue;
    ctx.log('');
    ctx.log('---- ' + id + ' ' + label + ' ----');
    try {
      await fn(ctx);
    } catch (error) {
      ctx.record(id + ' 场景未跑完', false, { error: String((error && error.stack) || error).slice(0, 1500) });
      try {
        await Promise.race([ctx.shot(id + '-failed'), H.sleep(6000)]);
      } catch {}
    }
    try {
      await ctx.clearRules();
    } catch {}
  }
  const summary = await ctx.finish();
  summary.runId = RUN;
  summary.stamp = STAMP;
  summary.out = OUT;
  writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  return summary;
}

const summary = await main();
process.exit(summary.fail > 0 ? 1 : 0);
