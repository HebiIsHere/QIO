/**
 * QIO032-46 修复后浏览器定向验收（12 项 + 原始上传路径）。
 *
 * 真实浏览器（系统 Edge headless + CDP）驱动真实前端 store 与真实隔离后端；
 * 可控时序只用 CDP Fetch（延迟 / 失败 / 自定义 409 响应），模型只打本机 fake provider。
 * 只读产品代码，不写产品文件；所有证据写 _lead-logs/qio032-46/fixed/。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as H from './lib.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(process.env.QIO_UI_OUT || join(HERE, '..', 'fixed'));
// 显式固定到隔离数据目录：不继承系统 QIO_DATA_DIR（这台机器上它指向用户真实数据 D:\QIO-data）
const DATA_DIR = process.env.QIO_UI_DATA_DIR || 'D:/qio-dev/pm-qio032-integration/_lead-logs/qio032-46/e2e-data';
const ONLY = String(process.env.Q46_ONLY || '').split(',').map((s) => s.trim()).filter(Boolean);

const A = 'board_a_46';
const B = 'board_b_46';
const A2 = 'board_a_46b';
const B2 = 'board_b_46b';
const B3 = 'board_b_46c';
const A3 = 'board_a_46c';
const RUN = Date.now().toString(36);
const stamp = new Date().toISOString();

function card(id, text, x) {
  return {
    id, kind: 'text', content: text,
    meta: { docText: text, doc: { schema: 'qio-text-doc-v1', blocks: [{ id: 'b_' + id, kind: 'paragraph', text }] } },
    x: x || 80, y: 80, w: 320, h: 200,
    checked: true, hidden: false, participation: 'included',
    folded: false, bookmarked: false, deleted: false,
    createdAt: stamp, updatedAt: stamp,
  };
}
function stateOf(boardId, cards, seq) {
  return { boardId, seq: seq || 0, updatedAt: new Date().toISOString(), cards, groups: [], links: [], selection: [], participationVersion: 1 };
}
async function seedBoard(id, cards) {
  await H.createBoard(id, id);
  const cur = await H.readBoard(id);
  const seq = cur.seq || 0;
  const resp = await H.putBoardDirect(id, stateOf(id, cards, seq), 'harness-seed');
  return { status: resp.status, seq: resp.data && resp.data.seq, body: resp.data };
}
function seedRunning(boardId, cardId, title) {
  const seedPy = join(HERE, 'seed.py');
  const r = spawnSync('uv', ['run', '--frozen', 'python', seedPy, '--board', boardId, '--card', cardId, '--title', title], {
    cwd: join(H.WT, 'backend'),
    env: Object.assign({}, process.env, { QIO_DATA_DIR: DATA_DIR, QIO_UI_DATA_DIR: DATA_DIR, PYTHONPATH: join(H.WT, 'backend', 'src') }),
    encoding: 'utf8',
    timeout: 120000,
  });
  let parsed = null;
  try { parsed = JSON.parse((r.stdout || '').trim().split('\n').pop()); } catch { parsed = { ok: false, raw: (r.stdout || '').slice(-500), err: (r.stderr || '').slice(-500) }; }
  return parsed;
}

async function openBoard(ctx, id) {
  await ctx.ensureStore();
  const switched = await ctx.switchBoard(id);
  await ctx.waitExpr('document.querySelector("[data-im=board]")', 20000, '板面');
  await H.sleep(900);
  return switched;
}

/** 选中第 index 张卡并进入编辑，写入 text（模拟用户输入），可选完成编辑。 */
async function typeCard(ctx, index, text, finish) {
  try { await ctx.waitExpr('document.querySelectorAll("[data-im=card]").length > ' + index, 20000, '卡片渲染'); } catch (e) {}
  await H.sleep(300);
  const btnVisible = async () => ctx.page.eval('(function(){ const sels = ["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels) { const el = document.querySelector(q); if (el) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return q; } } return null; })()');
  let btn = await btnVisible();
  if (!btn) {
    await ctx.click('[data-im="card"]', index).catch(() => null);
    for (let i = 0; i < 12 && !btn; i += 1) { await H.sleep(300); btn = await btnVisible(); }
  }
  if (!btn) {
    // 合成指针按下/抬起（与真实鼠标同一条 pointerdown 路径；首屏提示可能挡住命中测试）
    await ctx.page.eval('(function(){ const el=document.querySelectorAll("[data-im=card]")[' + index + ']; if(!el) return false; const r=el.getBoundingClientRect(); const opts={bubbles:true,cancelable:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,pointerId:1,pointerType:"mouse",isPrimary:true,button:0,buttons:1}; el.dispatchEvent(new PointerEvent("pointerdown",opts)); const up=Object.assign({},opts,{buttons:0}); el.dispatchEvent(new PointerEvent("pointerup",up)); return true; })()');
    for (let i = 0; i < 8 && !btn; i += 1) { await H.sleep(250); btn = await btnVisible(); }
  }
  if (!btn) {
    // 双击正文进入编辑（组件支持的双击入口）
    await ctx.page.doubleClick('[data-im="card-body"]', index).catch(() => null);
    await H.sleep(800);
  }
  if (!btn) {
    // 再试一次单击（工具栏可能被首屏提示/动画短暂挡住）
    await ctx.click('[data-im="card"]', index).catch(() => null);
    await H.sleep(700);
  }
  btn = await btnVisible();
  if (!btn) {
    const dbg = await ctx.page.eval('(function(){ const cards=[...document.querySelectorAll("[data-im=card]")].map(function(el){const r=el.getBoundingClientRect(); return {id:el.getAttribute("data-card-id"),state:el.getAttribute("data-card-state"),x:r.x,y:r.y,w:r.width,h:r.height};}); const host=document.querySelector("[data-im=card-toolbar-host]"); return {cards:cards, host: host ? {visible:host.getAttribute("data-toolbar-visible"), owner:host.getAttribute("data-owner-card-id")} : null, board: !!document.querySelector("[data-im=board]"), viewHint: !!document.querySelector("[data-im=board-view-hint]")}; })()');
    throw new Error('卡片编辑入口不可见（index=' + index + '）' + JSON.stringify(dbg));
  }
  await ctx.click(btn, 0, { timeout: 15000 });
  await H.sleep(450);
  const editorReady = await ctx.page.eval('!!document.querySelector("[data-im=card-editor]")');
  if (!editorReady) {
    await ctx.page.doubleClick('[data-im="card-body"]', 0).catch(() => null);
    await H.sleep(700);
  }
  const ok = await ctx.page.eval('(function(){ const el = document.querySelector("[data-im=card-editor]"); if (!el) return false; el.focus(); el.value = ' + JSON.stringify(text) + '; el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ' + JSON.stringify(text) + ', inputType: "insertText" })); return true; })()');
  await H.sleep(150);
  if (finish) {
    const btn2 = await btnVisible();
    if (btn2) await ctx.click(btn2, 0, { timeout: 15000 });
    await H.sleep(450);
  }
  return ok;
}

function reqs(ctx, opts) { return ctx.requests(opts); }
function brief(list) {
  return list.map((r) => ({ m: r.method, u: r.url.replace(H.API, ''), s: r.status, failed: r.failed }));
}

/* ------------------------------------------------------------------ */
/* 场景 1：B 输入草稿自动保存前切 A；保存完成重读两板；旧回执晚于新输入 */
async function s1(ctx) {
  await seedBoard(B, [card('cb1', 'B 草稿起点')]);
  await seedBoard(A, [card('ca1', 'A 稳定内容')]);
  await openBoard(ctx, B);
  const since = Date.now();
  ctx.fulfill(function (url, m) { return m === 'PUT' && url.indexOf('/api/interactive/drafts/' + B) >= 0; }, { delayMs: 2600, status: 200, body: { drafts: {}, updatedAt: new Date().toISOString(), rev: 99 } });
  await typeCard(ctx, 0, 'B 草稿第一版', false);
  await H.sleep(250);
  // 草稿防抖 600ms 后发出 PUT；在它回执前切 A
  await ctx.switchBoard(A);
  await H.sleep(3200);
  const afterSwitch = await ctx.storeState();
  const draftsB = await H.readDrafts(B);
  const draftsA = await H.readDrafts(A);
  const putDrafts = reqs(ctx, { method: 'PUT', contains: '/api/interactive/drafts/', since: since });
  // 第二阶段：旧回执晚于新输入（同一张卡再输入新文字，旧回执不得覆盖新文本）
  await ctx.switchBoard(B);
  await H.sleep(700);
  await ctx.clearRules();
  ctx.fulfill(function (url, m) { return m === 'PUT' && url.indexOf('/api/interactive/drafts/' + B) >= 0; }, { delayMs: 2600, status: 200, body: { drafts: {}, updatedAt: new Date().toISOString(), rev: 100 } });
  await typeCard(ctx, 0, 'B 草稿第二版（新输入）', false);
  await H.sleep(300);
  const newer = 'B 草稿第三版（更新输入）';
  await ctx.page.eval('(function(){ const el = document.querySelector("[data-im=card-editor]"); if (!el) return false; el.focus(); el.value = ' + JSON.stringify(newer) + '; el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ' + JSON.stringify(newer) + ', inputType: "insertText" })); return true; })()');
  await H.sleep(4200);
  const localDraft = await ctx.storeEval('return { drafts: JSON.parse(JSON.stringify(s.drafts)), lastDraftKey: s.lastDraftKey, draftSaveStatus: s.draftSaveStatus, boardId: s.boardId };');
  const draftTextNow = await ctx.page.eval('(function(){ const el = document.querySelector("[data-im=card-editor]"); return el ? el.value : null; })()');
  const ok = putDrafts.every((r) => r.url.indexOf('/' + B) >= 0 || r.url.indexOf('/board_b_46') >= 0)
    && draftTextNow === newer
    && localDraft.drafts && Object.values(localDraft.drafts).some((v) => String(v).indexOf('第三版') >= 0);
  ctx.record('1) B 草稿自动保存前切 A：PUT 只写 B；回执/旧回执不覆盖新输入', ok, { putDrafts: brief(putDrafts.slice(-6)), boardAfterSwitch: afterSwitch.boardId, draftsB: draftsB.data && draftsB.data.drafts, draftsA: draftsA.data && draftsA.data.drafts, draftTextNow, localDraft });
  await ctx.shot('s1-draft-cross-board');
}

/* ------------------------------------------------------------------ */
/* 场景 2：每次 B 保存后切 A，再放回 B 的 409；观察 PUT 次数与 B 失败状态 */
async function s2(ctx) {
  await seedBoard(B2, [card('cb2', 'B 保存起点')]);
  await seedBoard(A2, [card('ca2', 'A 保存起点')]);
  await openBoard(ctx, B2);
  const cur = await H.readBoard(B2);
  const seq = cur.seq || 0;
  await ctx.switchBoard(A2);
  await H.sleep(500);
  // 回到 B，安排一次「延迟 409」：先让 PUT 挂 2.5s，再返回 stale_state
  const conflictRule = ctx.fulfill(function (url, m) { return m === 'PUT' && url.indexOf('/boards/' + B2 + '/state') >= 0; }, {
    delayMs: 2500, status: 409,
    body: { detail: { error: 'stale_state', reason: 'harness: 板面已被另一处推进', currentSeq: seq + 1 } },
  });
  await ctx.switchBoard(B2);
  await H.sleep(700);
  const since = Date.now();
  await typeCard(ctx, 0, 'B 触发保存（会先撞一次 409）', true);
  // 等第一次 PUT 真的开始，再撤掉 30 分钟长挂规则（只让第一次吃 409；恢复重试应打真实后端）
  try { await ctx.waitForNetwork((r) => r.method === 'PUT' && r.url.indexOf('/boards/' + B2 + '/state') >= 0, 8000, '首次 PUT'); } catch (e) {}
  ctx.disableRule(conflictRule);
  if (ctx.page.fetchDelayRules && ctx.page.fetchDelayRules.length) ctx.page.fetchDelayRules.length = 0;
  await ctx.switchBoard(A2); // 409 回执将在 A 停留期间到达
  await H.sleep(4000);
  const onA = await ctx.storeState();
  await ctx.switchBoard(B2);
  let onB = null;
  try { onB = await ctx.waitStore((x) => x.boardId === B2 && (x.saveStatus === 'saved' || x.saveStatus === 'error'), 15000, 'B 从 409 恢复'); } catch (e) { onB = await ctx.storeState(); }
  await H.sleep(500);
  const onBFinal = await ctx.storeState();
  const puts = reqs(ctx, { method: 'PUT', contains: '/boards/', since: since });
  const putsB = puts.filter((r) => r.url.indexOf('/boards/' + B2 + '/state') >= 0);
  const putsA = puts.filter((r) => r.url.indexOf('/boards/' + A2 + '/state') >= 0);
  const ok = putsA.length === 0 && putsB.length >= 1 && putsB.every((r) => r.url.indexOf('/boards/' + B2 + '/state') >= 0) && onBFinal.saveStatus === 'saved' && !onA.saveError;
  ctx.record('2) B 保存后切 A、放回 B 的 409：PUT 只写 B，B 从 409 恢复为已保存，A 无错误', ok, { putCount: puts.length, putsB: brief(putsB), putsA: brief(putsA), onA: { boardId: onA.boardId, saveStatus: onA.saveStatus, saveError: onA.saveError }, onB: { boardId: onBFinal.boardId, saveStatus: onBFinal.saveStatus, saveError: onBFinal.saveError, seq: onBFinal.seq } });
  await ctx.shot('s2-409-cross-board');
}

/* ------------------------------------------------------------------ */
/* 场景 3：B 达自动恢复上限（4 次 PUT），反复切出切回重读不新增；手动重试只写 B */
async function s3(ctx) {
  await seedBoard(B3, [card('cb3', 'B 冲突起点')]);
  await seedBoard(A3, [card('ca3', 'A 冲突起点')]);
  await openBoard(ctx, B3);
  const since = Date.now();
  const baseSeq = (await H.readBoard(B3)).seq || 0;
  let conflictSeq = baseSeq + 1;
  // 确定性 409：每次 PUT /state 都回 stale_state（currentSeq 递增），GET /state 走真实后端
  const conflictRule = ctx.fulfill(function (url, m) { return m === 'PUT' && url.indexOf('/boards/' + B3 + '/state') >= 0; }, {
    delayMs: 0, status: 409,
    body: { detail: { error: 'stale_state', reason: 'harness: 每次保存都冲突', currentSeq: conflictSeq } },
  });
  await typeCard(ctx, 0, 'B 反复 409 直到上限', true);
  let st = null;
  try { st = await ctx.waitStore((x) => x.boardId === B3 && x.saveStatus === 'error', 30000, 'B 达到自动恢复上限'); } catch (e) { st = await ctx.storeState(); }
  await H.sleep(2000);
  const putsAtLimit = reqs(ctx, { method: 'PUT', contains: '/boards/' + B3 + '/state', since: since });
  const beforeSwitchCount = putsAtLimit.length;
  await ctx.switchBoard(A3); await H.sleep(600);
  await ctx.switchBoard(B3); await H.sleep(2200);
  await ctx.switchBoard(A3); await H.sleep(500);
  await ctx.switchBoard(B3); await H.sleep(2200);
  const afterSwitchCount = reqs(ctx, { method: 'PUT', contains: '/boards/' + B3 + '/state', since: since }).length;
  const putsA = reqs(ctx, { method: 'PUT', contains: '/boards/' + A3 + '/state', since: since });
  // 手动重试：撤掉 409 桩后点顶栏「重试」
  ctx.disableRule(conflictRule);
  await ctx.clearRules();
  await H.sleep(600);
  const ui = await ctx.uiSaveStatus();
  let retried = null;
  if (ui && ui.retry) {
    await ctx.click('[data-iw-chrome-save-retry]', 0);
    try { retried = await ctx.waitStore((x) => x.boardId === B3 && x.saveStatus === 'saved', 20000, 'B 手动重试保存成功'); } catch (e) { retried = await ctx.storeState(); }
  }
  const putsAfterRetry = reqs(ctx, { method: 'PUT', contains: '/boards/' + B3 + '/state', since: since });
  const retryPuts = putsAfterRetry.length - afterSwitchCount;
  const ok = putsAtLimit.length === 4 && afterSwitchCount === 4 && putsA.length === 0 && retried && retried.saveStatus === 'saved' && retryPuts >= 1 && putsAfterRetry.every((r) => r.url.indexOf('/boards/' + B3 + '/state') >= 0);
  ctx.record('3) B 409 达上限：恰好 4 次 PUT；切出切回不新增；手动重试恢复且只写 B', ok, { putsAtLimit: putsAtLimit.length, afterSwitchCount, putsA: putsA.length, retryPuts, uiBeforeRetry: ui, afterRetry: retried && { saveStatus: retried.saveStatus, saveError: retried.saveError }, st: st && { saveStatus: st.saveStatus, saveError: st.saveError }, allPutStatuses: putsAfterRetry.map((r) => r.status) });
  await ctx.shot('s3-recovery-limit');
}

/* ------------------------------------------------------------------ */
/* 场景 4：B 新旧两次预判反序到达；确认归属与状态 */
async function s4(ctx) {
  await ctx.gotoApp('s4=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r4_' + RUN;
  const boardA = 'board_a_46_r4_' + RUN;
  await seedBoard(boardB, [card('cb4', 'B 预判材料')]);
  await seedBoard(boardA, [card('ca4', 'A 预判材料')]);
  const running = seedRunning(boardB, 'cb4', 'B 执行中任务（预判）');
  await openBoard(ctx, boardB);
  await H.sleep(1200);
  const since = Date.now();
  // 第一次预判（旧）：延迟 4s 放行；第二次预判（新）：立即放行
  // 只延迟第一次预判（真实响应、旧版本），第二次立即走真实后端
  const slowRule = ctx.pass(function (url, m) { return m === 'POST' && url.indexOf('/boards/' + boardB + '/impact-check') >= 0; }, { delayMs: 4000 });
  await typeCard(ctx, 0, 'B 预判版本一（旧）', true);
  await H.sleep(350);
  // 第二次编辑：改动候选，触发新预判（此时旧预判仍在 4s 延迟中）
  const firstImpact = await ctx.uiImpact();
  await typeCard(ctx, 0, 'B 预判版本二（新）', true);
  let openImpact = null;
  try { openImpact = await ctx.waitExpr('(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()', 15000, '影响确认出现'); } catch (e) { openImpact = false; }
  const beforeLate = await ctx.storeEval('return s.pendingImpact ? { boardId: s.pendingImpact.boardId, checkId: s.pendingImpact.checkId, rev: s.pendingImpact.previewRev } : null;');
  await H.sleep(4500); // 旧预判迟到
  const uiAfterLate = await ctx.uiImpact();
  const stLate = await ctx.storeState();
  const afterLate = await ctx.storeEval('return s.pendingImpact ? { boardId: s.pendingImpact.boardId, checkId: s.pendingImpact.checkId, rev: s.pendingImpact.previewRev } : null;');
  const impactPosts = reqs(ctx, { method: 'POST', contains: '/boards/' + boardB + '/impact-check', since: since });
  // 确认这次改动：必须是新候选
  let confirmed = false;
  try { await ctx.click('[data-im="impact-confirm-submit"]', 0); confirmed = true; } catch (e) { confirmed = false; }
  let saved = null;
  try { saved = await ctx.waitStore((x) => x.boardId === boardB && x.saveStatus === 'saved', 20000, 'B 确认后保存成功'); } catch (e) { saved = await ctx.storeState(); }
  const puts = reqs(ctx, { method: 'PUT', contains: '/boards/', since: since });
  const putsB = puts.filter((r) => r.url.indexOf('/boards/' + boardB + '/state') >= 0);
  const putsA = puts.filter((r) => r.url.indexOf('/boards/' + boardA + '/state') >= 0);
  const stableConfirm = beforeLate && afterLate && beforeLate.checkId === afterLate.checkId && beforeLate.boardId === boardB;
  const ok = running && running.ok && openImpact === true && uiAfterLate.open === true && stableConfirm && impactPosts.length >= 2 && confirmed && saved && saved.saveStatus === 'saved' && putsA.length === 0 && putsB.length >= 1 && putsB.every((r) => r.status === 200);
  ctx.record('4) B 旧/新两次预判反序到达：旧响应不覆盖新确认；确认归属 B，保存只写 B', ok, { running, beforeLate, afterLate, firstImpact: firstImpact && firstImpact.open, openImpact, uiAfterLate: { open: uiAfterLate.open, mode: uiAfterLate.mode, body: uiAfterLate.body && uiAfterLate.body.slice(0, 200) }, impactPosts: brief(impactPosts), confirmed, saved: saved && { saveStatus: saved.saveStatus, seq: saved.seq }, putsB: brief(putsB), putsA: brief(putsA) });
  await ctx.shot('s4-impact-order');
}

/* ------------------------------------------------------------------ */
/* 场景 5：B 点确认后切已保存 A，再让 B 保存失败；B 失败、A 无错误/成功提示 */
async function s5(ctx) {
  await ctx.gotoApp('s5=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r5_' + RUN;
  const boardA = 'board_a_46_r5_' + RUN;
  await seedBoard(boardB, [card('cb5', 'B 确认材料')]);
  await seedBoard(boardA, [card('ca5', 'A 已保存材料')]);
  const running = seedRunning(boardB, 'cb5', 'B 执行中任务（确认）');
  await openBoard(ctx, boardA);
  await H.sleep(800);
  // A 先保存一次，处于「已保存」
  await typeCard(ctx, 0, 'A 已保存内容（第二轮）', true);
  try { await ctx.waitStore((x) => x.boardId === boardA && x.saveStatus === 'saved', 15000, 'A 已保存'); } catch (e) {}
  await ctx.switchBoard(boardB); await H.sleep(1200);
  const since = Date.now();
  // 预置：B 确认后的 PUT 会失败
  ctx.fail(function (url, m) { return m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0; }, { delayMs: 1200, repeat: true });
  await typeCard(ctx, 0, 'B 触发影响确认（确认后保存会失败）', true);
  let impactShown = false;
  try { impactShown = await ctx.waitExpr('(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return s && getComputedStyle(s).display !== "none" ? true : false; })()', 15000, '影响确认'); } catch (e) { impactShown = false; }
  // 点「确认改动」，然后立刻切到已保存的 A
  let confirmed = false;
  try { await ctx.click('[data-im="impact-confirm-submit"]', 0); confirmed = true; } catch (e) { confirmed = false; }
  await ctx.switchBoard(boardA);
  const onA = await ctx.storeState();
  const uiSaveOnA = await ctx.uiSaveStatus();
  await H.sleep(3500);
  await ctx.switchBoard(boardB);
  let onB = null;
  try { onB = await ctx.waitStore((x) => x.boardId === boardB && x.saveStatus === 'error', 20000, 'B 保存失败'); } catch (e) { onB = await ctx.storeState(); }
  const puts = reqs(ctx, { method: 'PUT', contains: '/boards/', since: since });
  const putsB = puts.filter((r) => r.url.indexOf('/boards/' + boardB + '/state') >= 0);
  const putsA = puts.filter((r) => r.url.indexOf('/boards/' + boardA + '/state') >= 0);
  const onAHasBError = Boolean(onA.saveError) || String((uiSaveOnA && uiSaveOnA.text) || '').indexOf('失败') >= 0;
  const ok = running && running.ok && impactShown && confirmed && onB && onB.saveStatus === 'error' && !onAHasBError && (String((uiSaveOnA && uiSaveOnA.text) || '').indexOf('已保存') >= 0 || String((uiSaveOnA && uiSaveOnA.text) || '').indexOf('尚未保存过') >= 0) && putsA.length === 0 && putsB.length >= 1;
  ctx.record('5) B 点确认后切已保存 A、B 保存失败：B 返回失败，A 无错误/成功提示串入', ok, { running, impactShown, confirmed, onA: { saveStatus: onA.saveStatus, saveError: onA.saveError }, uiSaveOnA, onB: { saveStatus: onB && onB.saveStatus, saveError: onB && onB.saveError }, putsB: brief(putsB), putsA: brief(putsA) });
  await ctx.shot('s5-confirm-cross-board');
}

/* ------------------------------------------------------------------ */
/* 场景 7：B 整板读取延迟/失败；A 成功读取并可编辑保存；旧 B 请求不盖新 B 读取 */
async function s7(ctx) {
  const boardB = 'board_b_46_r7_' + RUN;
  const boardA = 'board_a_46_r7_' + RUN;
  await seedBoard(boardB, [card('cb7', 'B 读取版本一')]);
  await seedBoard(boardA, [card('ca7', 'A 读取内容')]);
  await openBoard(ctx, boardA);
  const since = Date.now();
  await typeCard(ctx, 0, 'A 编辑后保存内容', true);
  let aSaved = null;
  try { aSaved = await ctx.waitStore((x) => x.boardId === boardA && x.saveStatus === 'saved', 15000, 'A 保存成功'); } catch (e) { aSaved = await ctx.storeState(); }
  const aPut = reqs(ctx, { method: 'PUT', contains: '/boards/' + boardA + '/state', since: since });
  // 旧 B 快照（服务端先推进到新版本，旧快照的 seq/内容都过期）
  const oldSnap = await H.apiGet(H.boardStatePath(boardB));
  const oldBody = oldSnap.data;
  const curB = await H.readBoard(boardB);
  const st = curB.state;
  st.cards[0].content = 'B 读取版本二（新读取）';
  st.cards[0].meta = Object.assign({}, st.cards[0].meta, { docText: 'B 读取版本二（新读取）' });
  st.seq = curB.seq; // 认领当前版本 -> 服务端推进为更新版本
  await H.putBoardDirect(boardB, st, 'harness-r7-new');
  const newB = await H.readBoard(boardB);
  // 第一次 B 读取：延迟 5s 返回旧快照；同板再发一次读取会立即拿到新事实
  ctx.fulfill(function (url, m) { return m === 'GET' && url.indexOf('/boards/' + boardB + '/state') >= 0; }, { delayMs: 5000, status: 200, body: oldBody });
  await ctx.storeEval('void s.selectBoard(' + JSON.stringify(boardB) + '); return true;');
  await H.sleep(600);
  // 同板再发一次真实读取：新事实应立即落地（旧迟到回执 5s 后才到）
  const refreshed = await ctx.storeEval('return await s.refreshBoardFromServer(' + JSON.stringify(boardB) + ');');
  await H.sleep(1200);
  const mid = await ctx.storeState();
  const midLoading = await ctx.storeEval('return { loading: s.loading, seq: s.board ? s.board.seq : null, content: s.board && s.board.cards[0] ? s.board.cards[0].content : null };');
  await H.sleep(5000); // 旧迟到回执落地
  const after = await ctx.storeState();
  const getReqs = reqs(ctx, { method: 'GET', contains: '/boards/' + boardB + '/state' });
  const seqOk = Number(after.seq) === Number(newB.seq);
  const contentOk = (after.boardCards || []).some((c) => String(c.content).indexOf('版本二') >= 0);
  const ok = aSaved && aSaved.saveStatus === 'saved' && aPut.length >= 1 && aPut.every((r) => r.url.indexOf('/boards/' + boardA) >= 0) && after.boardId === boardB && !after.loadError && seqOk && contentOk;
  ctx.record('7) B 读取延迟 + A 读取保存：A 可编辑保存；切回 B 的新读取不被旧迟到响应覆盖', ok, { refreshed, aSaved: aSaved && aSaved.saveStatus, aPut: brief(aPut), newBSeq: newB.seq, mid: { boardId: mid.boardId, seq: mid.seq, cards: mid.boardCards }, after: { boardId: after.boardId, loadError: after.loadError, seq: after.seq, cards: after.boardCards }, midLoading, getReqs: brief(getReqs), seqOk, contentOk });
  await ctx.shot('s7-stale-read');
}

/* ------------------------------------------------------------------ */
/* 场景 8/* ------------------------------------------------------------------ */
/* 场景 8：B 保存后材料范围（visible-range）刷新迟到（成功/失败）；A 保留自己的范围与保存错误 */
async function s8(ctx) {
  const boardB = 'board_b_46_r8';
  const boardA = 'board_a_46_r8';
  await seedBoard(boardB, [card('cb8', 'B 范围起点')]);
  await seedBoard(boardA, [card('ca8', 'A 范围起点')]);
  await openBoard(ctx, boardA);
  const rangeA0 = (await ctx.storeState()).visibleRange;
  // 先让 B 保存成功一次，再让它的范围刷新迟到
  await ctx.switchBoard(boardB);
  await H.sleep(800);
  // 第一次：迟到但成功
  ctx.delay(function (url, m) { return m === 'GET' && url.indexOf('/boards/' + boardB + '/visible-range') >= 0; }, 4000);
  await typeCard(ctx, 0, 'B 保存后范围刷新（成功迟到）', true);
  let bSaved = null;
  try { bSaved = await ctx.waitStore((s) => s.boardId === boardB && s.saveStatus === 'saved', 15000, 'B 保存成功'); } catch (e) { bSaved = await ctx.storeState(); }
  await ctx.switchBoard(boardA);
  const rangeA1 = (await ctx.storeState()).visibleRange;
  await H.sleep(4500);
  const rangeA2 = (await ctx.storeState()).visibleRange;
  // 第二次：迟到且失败
  await ctx.clearRules();
  await ctx.switchBoard(boardB); await H.sleep(600);
  ctx.fail(function (url, m) { return m === 'GET' && url.indexOf('/boards/' + boardB + '/visible-range') >= 0; }, { delayMs: 3500 });
  await typeCard(ctx, 0, 'B 保存后范围刷新（失败迟到）', true);
  let bSaved2 = null;
  try { bSaved2 = await ctx.waitStore((s) => s.boardId === boardB && s.saveStatus === 'saved', 15000, 'B 第二次保存成功'); } catch (e) { bSaved2 = await ctx.storeState(); }
  await ctx.switchBoard(boardA);
  await H.sleep(4500);
  const rangeA3 = (await ctx.storeState()).visibleRange;
  const same = (x, y) => JSON.stringify(x) === JSON.stringify(y);
  const ok = bSaved && bSaved.saveStatus === 'saved' && bSaved2 && bSaved2.saveStatus === 'saved' && same(rangeA1, rangeA2) && same(rangeA1, rangeA3) && rangeA0 !== undefined;
  ctx.record('8) B 保存后材料范围刷新迟到（成功/失败）：A 的范围不被 B 的回执改写', ok, { bSaved: bSaved && bSaved.saveStatus, bSaved2: bSaved2 && bSaved2.saveStatus, rangeA1, rangeA2, rangeA3 });
  await ctx.shot('s8-range-cross-board');
}

/* ------------------------------------------------------------------ */
/* 场景 9：B 含运行中任务、A 无任务；A 载入后才放回 B 任务列表；A 不显示 B 任务且能保存 */
async function s9(ctx) {
  await ctx.gotoApp('s9=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r9_' + RUN;
  const boardA = 'board_a_46_r9_' + RUN;
  const seedB = await seedBoard(boardB, [card('cb9', 'B 任务材料')]);
  const seedA = await seedBoard(boardA, [card('ca9', 'A 无任务材料')]);
  let seeded = seedRunning(boardB, 'cb9', 'B 执行中任务（harness）');
  const apiCardsBeforeSeed = ((await H.readBoard(boardB)).state.cards || []).map((c) => c.id);
  if (!seeded || !seeded.ok) { await H.sleep(1500); seeded = seedRunning(boardB, 'cb9', 'B 执行中任务（harness）'); }
  const apiCardsAfterSeed = ((await H.readBoard(boardB)).state.cards || []).map((c) => c.id);
  await openBoard(ctx, boardA);
  await H.sleep(800);
  const since = Date.now();
  // B 的 intents 读取延迟
  ctx.delay(function (url, m) { return m === 'GET' && url.indexOf('/boards/' + boardB + '/intents') >= 0; }, 4000);
  await ctx.switchBoard(boardB);
  await H.sleep(400);
  await ctx.switchBoard(boardA); // A 载入，B 的 intents 回执迟到
  await H.sleep(5000);
  const onA = await ctx.storeState();
  await typeCard(ctx, 0, 'A 在 B 任务回执迟到后仍可保存', true);
  let aSaved = null;
  try { aSaved = await ctx.waitStore((s) => s.boardId === boardA && s.saveStatus === 'saved', 15000, 'A 保存成功'); } catch (e) { aSaved = await ctx.storeState(); }
  const puts = reqs(ctx, { method: 'PUT', contains: '/boards/', since: since });
  const ok = seeded && seeded.ok && onA.boardId === boardA && (onA.intents || []).length === 0 && aSaved && aSaved.saveStatus === 'saved' && puts.every((r) => r.url.indexOf('/boards/' + boardA) >= 0);
  ctx.record('9) B 任务回执迟到：A 不显示 B 的任务且能正常保存', ok, { seedB: { status: seedB.status, seq: seedB.seq }, seedA: { status: seedA.status, seq: seedA.seq }, apiCardsBeforeSeed, apiCardsAfterSeed, seeded, onA: { boardId: onA.boardId, intents: onA.intents, intentViews: onA.intentViews }, aSaved: aSaved && aSaved.saveStatus, putTargets: brief(puts) });
  await ctx.shot('s9-tasks-cross-board');
}

/* ------------------------------------------------------------------ */
/* 场景 10：A 停留期间 B 上传完成，切回 B 立即显示真实状态/恢复入口 */
async function s10(ctx) {
  await ctx.gotoApp('s10=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r10';
  const boardA = 'board_a_46_r10';
  await seedBoard(boardB, []);
  await seedBoard(boardA, [card('ca10', 'A 上传期间停留')]);
  const file = join(OUT, 'materials', 'attach-' + RUN + '.txt');
  writeFileSync(file, 'QIO032-46 upload fixture ' + RUN + '\n', 'utf8');
  await openBoard(ctx, boardA);
  await ctx.switchBoard(boardB); await H.sleep(900);
  const since = Date.now();
  ctx.delay(function (url, m) { return m === 'POST' && url.indexOf('/api/attachments/upload') >= 0; }, 5000);
  await ctx.click('[data-im="add-menu"]', 0); await H.sleep(400);
  const chooser = ctx.page.setFilesOnChooser([file], { timeout: 20000 });
  await ctx.click('[data-im="add-file"]', 0);
  await chooser;
  await ctx.waitForNetwork((r) => r.method === 'POST' && r.url.indexOf('/api/attachments/upload') >= 0, 15000, '上传请求');
  await H.sleep(500);
  await ctx.switchBoard(boardA); // 上传在 A 停留期间完成
  await H.sleep(7000);
  const onA = await ctx.storeState();
  await ctx.switchBoard(boardB);
  await H.sleep(1500);
  const onB = await ctx.storeState();
  const uploads = reqs(ctx, { method: 'POST', contains: '/api/attachments/upload', since: since });
  const uiNotice = await ctx.page.eval('(function(){ const el = document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()');
  const jobs = onB.cardImportJobs || [];
  const ok = uploads.length === 1 && jobs.some((j) => j.phase === 'succeeded' || j.phase === 'failed' || j.awaitingSave || j.attachment) && (onB.boardCards || []).length >= 1 && onA.boardId === boardA;
  ctx.record('10) A 停留期间 B 上传完成：切回 B 立即显示真实状态/恢复入口（只上传一次）', ok, { uploads: brief(uploads), onA: { boardId: onA.boardId, cards: onA.boardCards }, onB: { boardId: onB.boardId, cards: onB.boardCards, jobs }, uiNotice });
  await ctx.shot('s10-upload-across-board');
}

/* ------------------------------------------------------------------ */
/* 场景 11：上传成功后点重试关联（单卡）：每份文件只有一次成功上传，写入第一次附件 ID */
async function s11(ctx) {
  await ctx.gotoApp('s11=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r11';
  await seedBoard(boardB, []);
  const file = join(OUT, 'materials', 'retry-' + RUN + '.txt');
  writeFileSync(file, 'retry association fixture ' + RUN + '\n', 'utf8');
  await openBoard(ctx, boardB);
  const since = Date.now();
  const failRule = ctx.fail(function (url, m) { return m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0; }, { delayMs: 1200 });
  await ctx.click('[data-im="add-menu"]', 0); await H.sleep(400);
  const chooser = ctx.page.setFilesOnChooser([file], { timeout: 20000 });
  await ctx.click('[data-im="add-file"]', 0);
  await chooser;
  await ctx.waitForNetwork((r) => r.method === 'POST' && r.url.indexOf('/api/attachments/upload') >= 0, 15000, '上传请求');
  // 等保存失败落定（提示短命，先抓 job 状态）
  let failSettled = null;
  try { failSettled = await ctx.waitStore((x) => (x.cardImportJobs || []).some((j) => j.boardId === boardB && j.phase === 'failed'), 20000, '关联保存失败'); } catch (e) { failSettled = await ctx.storeState(); }
  ctx.disableRule(failRule);
  await ctx.clearRules();
  const failedJob = ((failSettled.cardImportJobs || []).find((j) => j.boardId === boardB && j.phase === 'failed')) || null;
  const failedState = await ctx.storeState();
  // 重试入口：优先真实界面（卡片工具栏 / 添加菜单提示），拿不到才退回 store 动作（如实记录）
  const cardId = failedJob && failedJob.cardId ? failedJob.cardId : ((failedState.boardCards || []).find((c) => c.kind === 'file') || {}).id;
  let retryHow = null;
  let cardRetryVisible = false;
  try {
    await ctx.click('[data-im="card"]', 0); await H.sleep(400);
    cardRetryVisible = await ctx.page.eval('!!document.querySelector("[data-im=card-attach-retry]")');
  } catch (e) { cardRetryVisible = false; }
  if (cardRetryVisible) { await ctx.click('[data-im="card-attach-retry"]', 0); retryHow = 'card-toolbar'; }
  else if (await ctx.page.eval('!!document.querySelector("[data-im=add-material-retry]")')) { await ctx.click('[data-im="add-material-retry"]', 0); retryHow = 'add-menu-notice'; }
  else if (cardId) { await ctx.storeEval('return s.retryCardAttachment(' + JSON.stringify(cardId) + ');'); retryHow = 'store-action-fallback'; }
  try { await ctx.waitExpr('(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el && (el.innerText||"").indexOf("已关联附件")>=0; })()', 20000, '重试后成功提示'); } catch (e) {}
  await H.sleep(1500);
  const after = await ctx.storeState();
  const uploads = reqs(ctx, { method: 'POST', contains: '/api/attachments/upload', since: since });
  const boardRead = await H.readBoard(boardB);
  const fileCard = (boardRead.state.cards || []).find((c) => c.kind === 'file');
  const attachOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const firstAttachment = failedJob && failedJob.pendingAttachment && (failedJob.pendingAttachment.id || failedJob.pendingAttachment.attachmentId);
  const ok = Boolean(retryHow) && uploads.length === 1 && after.saveStatus === 'saved' && Boolean(attachOf(fileCard)) && (!firstAttachment || String(attachOf(fileCard)) === String(firstAttachment));
  ctx.record('11) 已上传成功后重试关联（单卡）：只上传一次，写入第一次附件 ID', ok, { retryHow, cardRetryVisible, failedJob, uploads: brief(uploads), afterSave: after.saveStatus, fileCardAttachmentId: attachOf(fileCard), firstAttachment });
  await ctx.shot('s11-retry-association');
}

/* ------------------------------------------------------------------ */
/* 场景 12/* ------------------------------------------------------------------ */
/* 场景 12：上传成功但保存关联失败；修复故障后重试，回读原板确认关联成功再显示成功 */
async function s12(ctx) {
  await ctx.gotoApp('s12=' + RUN); await ctx.enterInteractive();
  const boardB = 'board_b_46_r12';
  await seedBoard(boardB, []);
  const file = join(OUT, 'materials', 'savefail-' + RUN + '.txt');
  writeFileSync(file, 'association save failure fixture ' + RUN + '\n', 'utf8');
  await openBoard(ctx, boardB);
  const since = Date.now();
  const failRule = ctx.fulfill(function (url, m) { return m === 'PUT' && url.indexOf('/boards/' + boardB + '/state') >= 0; }, { delayMs: 400, status: 500, body: { detail: 'harness: 保存故障' }, repeat: true });
  await ctx.click('[data-im="add-menu"]', 0); await H.sleep(400);
  const chooser = ctx.page.setFilesOnChooser([file], { timeout: 20000 });
  await ctx.click('[data-im="add-file"]', 0);
  await chooser;
  await ctx.waitForNetwork((r) => r.method === 'POST' && r.url.indexOf('/api/attachments/upload') >= 0, 15000, '上传请求');
  let failSettled = null;
  try { failSettled = await ctx.waitStore((x) => (x.cardImportJobs || []).some((j) => j.boardId === boardB && j.phase === 'failed'), 20000, '关联保存失败'); } catch (e) { failSettled = await ctx.storeState(); }
  ctx.disableRule(failRule);
  await ctx.clearRules(); // 修复故障
  const failedState = await ctx.storeState();
  const failedJob = ((failedState.cardImportJobs || []).find((j) => j.boardId === boardB && j.phase === 'failed')) || null;
  const cardId = failedJob && failedJob.cardId ? failedJob.cardId : ((failedState.boardCards || []).find((c) => c.kind === 'file') || {}).id;
  let retryHow = null;
  let cardRetryVisible = false;
  try {
    await ctx.click('[data-im="card"]', 0); await H.sleep(400);
    cardRetryVisible = await ctx.page.eval('!!document.querySelector("[data-im=card-attach-retry]")');
  } catch (e) { cardRetryVisible = false; }
  if (cardRetryVisible) { await ctx.click('[data-im="card-attach-retry"]', 0); retryHow = 'card-toolbar'; }
  else if (await ctx.page.eval('!!document.querySelector("[data-im=add-material-retry]")')) { await ctx.click('[data-im="add-material-retry"]', 0); retryHow = 'add-menu-notice'; }
  else if (cardId) { await ctx.storeEval('return s.retryCardAttachment(' + JSON.stringify(cardId) + ');'); retryHow = 'store-action-fallback'; }
  let successSeen = false;
  try { await ctx.waitExpr('(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el && (el.innerText||"").indexOf("已关联附件")>=0; })()', 20000, '重试后成功提示'); successSeen = true; } catch (e) { successSeen = false; }
  await H.sleep(1500);
  const readBack = await H.readBoard(boardB);
  const fileCard = (readBack.state.cards || []).find((c) => c.kind === 'file');
  const attachOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const uiText = await ctx.page.eval('(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()');
  const uploads = reqs(ctx, { method: 'POST', contains: '/api/attachments/upload', since: since });
  const ok = Boolean(retryHow) && failSettled && (failedState.saveStatus === 'error') && successSeen && Boolean(attachOf(fileCard)) && uploads.length === 1 && String(uiText || '').indexOf('已关联附件') >= 0;
  ctx.record('12) 上传成功但保存关联失败 → 修复后重试：回读原板确认关联成功再显示成功', ok, { retryHow, cardRetryVisible, failedSaveStatus: failedState.saveStatus, readBackSeq: readBack.seq, fileCardAttachmentId: attachOf(fileCard), uiText, successSeen, uploads: brief(uploads) });
  await ctx.shot('s12-savefail-retry');
}

/* ------------------------------------------------------------------ */
/* 原始路径/* ------------------------------------------------------------------ */
/* 原始路径：上传 → 打开设置 → 返回互动页 */
async function s0(ctx) {
  const boardB = 'board_b_46_r0';
  await seedBoard(boardB, []);
  const file = join(OUT, 'materials', 'settings-' + RUN + '.txt');
  writeFileSync(file, 'settings round trip fixture ' + RUN + '\n', 'utf8');
  await openBoard(ctx, boardB);
  const since = Date.now();
  ctx.delay(function (url, m) { return m === 'POST' && url.indexOf('/api/attachments/upload') >= 0; }, 3500);
  await ctx.click('[data-im="add-menu"]', 0); await H.sleep(400);
  const chooser = ctx.page.setFilesOnChooser([file], { timeout: 20000 });
  await ctx.click('[data-im="add-file"]', 0);
  await chooser;
  await ctx.waitForNetwork((r) => r.method === 'POST' && r.url.indexOf('/api/attachments/upload') >= 0, 15000, '上传请求');
  // 进设置页
  const opened = await ctx.page.eval('(function(){ const b=[...document.querySelectorAll("button")].find(function(x){ const l=x.getAttribute("aria-label")||""; return l.indexOf("设置")===0; }); if(!b) return false; b.click(); return true; })()');
  await H.sleep(1200);
  await ctx.page.eval('(function(){ const b=document.querySelector("[data-testid=settings-back]"); if(b) b.click(); return !!b; })()');
  await H.sleep(4500);
  try { await ctx.waitStore((x) => x.boardId === boardB && (x.saveStatus === 'saved' || x.saveStatus === 'idle'), 12000, '返回后保存落定'); } catch (e) {}
  await H.sleep(800);
  const state = await ctx.storeState();
  const readBack = await H.readBoard(boardB);
  const serverCards = (readBack.state && readBack.state.cards) || [];
  const uploads = reqs(ctx, { method: 'POST', contains: '/api/attachments/upload', since: since });
  const ui = await ctx.page.eval('(function(){ const el=document.querySelector("[data-im=add-material-status-text]"); return el ? (el.innerText||"").trim() : null; })()');
  const cards = state.boardCards || [];
  const attachmentOf = (c) => (c && c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null;
  const ok = opened && uploads.length === 1 && serverCards.some((c) => c.kind === 'file' && attachmentOf(c)) && state.boardId === boardB;
  ctx.record('0) 原始路径：上传 → 打开设置 → 返回互动页（卡片/成功状态保留，不重复上传）', ok, { opened, uploads: brief(uploads), ui, cards, serverCards: serverCards.map((c) => ({ id: c.id, kind: c.kind, attachmentId: attachmentOf(c), meta: c.meta })) });
  await ctx.shot('s0-settings-roundtrip');
}

/* ------------------------------------------------------------------ */
async function main() {
  mkdirSync(OUT, { recursive: true });
  const ctx = await H.setup({ outDir: OUT, mode: 'fixed' });
  ctx.log('== QIO032-46 修复后定向验收 runId=' + new Date().toISOString());
  ctx.log('base=' + H.BASE + ' api=' + H.API + ' provider=' + H.PROVIDER + ' out=' + OUT + ' only=' + JSON.stringify(ONLY));
  await ctx.gotoApp('fixed46=' + RUN);
  await ctx.enterInteractive();
  const run = (id) => ONLY.length === 0 || ONLY.indexOf(id) >= 0;
  const scenarios = [
    ['s0', '原始路径 上传→设置→返回', s0],
    ['s1', '草稿自动保存跨板', s1],
    ['s2', 'B 保存后切 A 的 409', s2],
    ['s3', '409 达上限与手动重试', s3],
    ['s4', '旧/新预判反序到达', s4],
    ['s5', '确认后切 A 再让 B 失败', s5],
    ['s7', 'B 读取延迟/失败与迟到读取', s7],
    ['s8', '材料范围刷新迟到', s8],
    ['s9', 'B 任务列表迟到', s9],
    ['s10', 'A 停留期间 B 上传完成', s10],
    ['s11', '重试关联不重复上传', s11],
    ['s12', '关联保存失败后重试回读', s12],
  ];
  for (const [id, label, fn] of scenarios) {
    if (!run(id)) continue;
    ctx.log('');
    ctx.log('---- ' + id + ' ' + label + ' ----');
    try { await fn(ctx); } catch (error) {
      ctx.record(id + ' 场景未跑完', false, { error: String((error && error.stack) || error).slice(0, 1200) });
      try { await ctx.shot(id + '-failed'); } catch {}
    }
  }
  // 未在 harness 内复现的两项：如实标 UNVERIFIED（不编造）。
  ctx.record('6) B 撤回读取挂起 → A 撤回 → 返回 B 失败', false, { note: 'UNVERIFIED：撤回（revert）决策读取链路未能在本轮窗口内稳定驱动；见报告' }, { observe: true });
  const summary = await ctx.finish();
  return summary;
}

const summary = await main();
process.exit(summary.fail > 0 ? 1 : 0);
