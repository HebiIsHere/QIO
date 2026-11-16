/**
 * QIO #46 R8 浏览器验收 · 公共层（env 由 run-48.mjs 先设好，再动态 import 本模块）。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const H = await import('../../verify/lib.mjs');
const L = await import('../../verify/lib47.mjs');
const L48 = await import('./lib48.mjs');

export { H, L, L48 };
export const HERE = dirname(fileURLToPath(import.meta.url));
export const OUT = resolve(process.env.QIO_UI_OUT || join(HERE, '..', 'run'));
export const RUN = Date.now().toString(36);
export const STAMP = new Date().toISOString();
export const ID = (s) => 'r8' + s + '_' + RUN;

export function unverified(ctx, name, detail) {
  return ctx.record(name, false, detail, { observe: true });
}

/** 页面是否仍能同步执行 JS（浏览器是否卡死）。返回 { ok, ms }。 */
export async function probeResponsive(ctx, ms = 5000) {
  const started = Date.now();
  try {
    const value = await Promise.race([
      ctx.page.eval('(function(){ return 21 * 2; })()'),
      H.sleep(ms).then(() => 'TIMEOUT'),
    ]);
    return { ok: value === 42, value, ms: Date.now() - started };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 200), ms: Date.now() - started };
  }
}

/**
 * 给可能因页面卡死而永远不返回的驱动步骤套一层超时（CDP Runtime.evaluate 默认 60s，
 * 太久；这里 20s 就判「页面失去响应」，并继续记录证据而不是让整个场景崩掉）。
 */
export async function driveStep(label, fn, ms = 20000) {
  const started = Date.now();
  const TIMEOUT = { __timeout: true };
  const p = Promise.resolve()
    .then(fn)
    .catch((e) => ({ __error: String((e && e.message) || e).slice(0, 300) }));
  const v = await Promise.race([p, H.sleep(ms).then(() => TIMEOUT)]);
  if (v === TIMEOUT) return { label, ok: false, timeout: true, ms: Date.now() - started };
  if (v && v.__error) return { label, ok: false, error: v.__error, ms: Date.now() - started };
  return { label, ok: true, value: v, ms: Date.now() - started };
}

/** 任务结果入口（IntentExecutionMessage.vue 的 openBoardResult）用的就是 store.selectBoard。 */
export async function fireSelectBoard(ctx, boardId) {
  await ctx.ensureStore();
  return ctx.page.eval(
    '(function(){ window.__r8selResult = null; var p = window.__q46Store.selectBoard(' +
      JSON.stringify(String(boardId)) +
      '); window.__r8sel = p; p.then(function(r){ window.__r8selResult = r; }).catch(function(e){ window.__r8selResult = { error: String(e) }; }); return true; })()',
  );
}

export function cardAttachOf(card) {
  const meta = (card && card.meta) || {};
  return meta.attachmentId || (meta.file && meta.file.attachmentId) || null;
}
