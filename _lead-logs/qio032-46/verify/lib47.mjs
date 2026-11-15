/**
 * QIO032-46 第七轮（R7）浏览器验收 harness 的**追加层**。
 *
 * 只新增能力，不改 lib.mjs 既有函数语义：lib.mjs 的 setup() 返回的 ctx 已经暴露
 * page / browser / network / fetchRules / record / shot / log / fulfill / fail / pass 等，
 * 这里在它之上补 R7 需要的观察与驱动原语：
 *  - snapshot(ctx)：一次读出 store 的公开事实（草稿冲突 / 待清登记 / 撤回恢复 / 导入任务…）
 *  - ui(ctx, sel)：元素是否真实出现、可见、可点（命中测试）
 *  - clickAsserted(ctx, sel)：先断言「真实出现且可点」，再走真实鼠标点击
 *  - pageText(ctx)：整页可见文本（跨板串文案的判据）
 *  - reqs/brief：请求台账（method / URL / status / 请求体）
 *  - seedBoard / seedRunning / openBoard / typeCard / uploadViaAddMenu：与 run-46 同口径的种子与驱动
 *
 * 纪律：只写 _lead-logs/qio032-46；不碰产品文件；不编造 PASS。
 */
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as H from './lib.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
export const DATA_DIR =
  process.env.QIO_UI_DATA_DIR ||
  'D:/qio-dev/pm-qio032-integration/_lead-logs/qio032-46/e2e-data';

/* ------------------------------------------------------------------ */
/* 种子（与 run-46 同形状）                                            */
/* ------------------------------------------------------------------ */
export function card(id, text, x) {
  return {
    id,
    kind: 'text',
    content: text,
    meta: {
      docText: text,
      doc: { schema: 'qio-text-doc-v1', blocks: [{ id: 'b_' + id, kind: 'paragraph', text }] },
    },
    x: x || 80,
    y: 80,
    w: 320,
    h: 200,
    checked: true,
    hidden: false,
    participation: 'included',
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

export function stateOf(boardId, cards, seq) {
  return {
    boardId,
    seq: seq || 0,
    updatedAt: new Date().toISOString(),
    cards,
    groups: [],
    links: [],
    selection: [],
    participationVersion: 1,
  };
}

export async function seedBoard(id, cards) {
  await H.createBoard(id, id);
  const cur = await H.readBoard(id);
  const seq = cur.seq || 0;
  const resp = await H.putBoardDirect(id, stateOf(id, cards, seq), 'harness-r7-seed');
  return { status: resp.status, seq: resp.data && resp.data.seq };
}

/** 在隔离数据目录里给板面卡插一条「执行中」任务（与 run-46 同一个 seed.py）。 */
export function seedRunning(boardId, cardId, title) {
  const seedPy = join(HERE, 'seed.py');
  const r = spawnSync(
    'uv',
    ['run', '--frozen', 'python', seedPy, '--board', boardId, '--card', cardId, '--title', title],
    {
      cwd: join(H.WT, 'backend'),
      env: Object.assign({}, process.env, {
        QIO_DATA_DIR: DATA_DIR,
        QIO_UI_DATA_DIR: DATA_DIR,
        PYTHONPATH: join(H.WT, 'backend', 'src'),
      }),
      encoding: 'utf8',
      timeout: 180000,
    },
  );
  let parsed = null;
  try {
    parsed = JSON.parse((r.stdout || '').trim().split('\n').pop());
  } catch {
    parsed = { ok: false, raw: (r.stdout || '').slice(-500), err: (r.stderr || '').slice(-500) };
  }
  return parsed;
}

/* ------------------------------------------------------------------ */
/* 板面驱动                                                            */
/* ------------------------------------------------------------------ */
export async function openBoard(ctx, id) {
  await ctx.ensureStore();
  const switched = await ctx.switchBoard(id);
  await ctx.waitExpr('document.querySelector("[data-im=board]")', 20000, '板面');
  await H.sleep(900);
  return switched;
}

export async function switchBoard(ctx, id) {
  const r = await ctx.switchBoard(id);
  await H.sleep(700);
  return r;
}

/** 选中第 index 张卡并进入编辑，写入 text（模拟用户输入），可选完成编辑。 */
export async function typeCard(ctx, index, text, finish) {
  try {
    await ctx.waitExpr('document.querySelectorAll("[data-im=card]").length > ' + index, 20000, '卡片渲染');
  } catch (e) {}
  await H.sleep(300);
  const btnVisible = async () =>
    ctx.page.eval(
      '(function(){ const sels = ["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels) { const el = document.querySelector(q); if (el) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return q; } } return null; })()',
    );
  let btn = await btnVisible();
  if (!btn) {
    await ctx.click('[data-im="card"]', index).catch(() => null);
    for (let i = 0; i < 12 && !btn; i += 1) {
      await H.sleep(300);
      btn = await btnVisible();
    }
  }
  if (!btn) {
    await ctx.page.eval(
      '(function(){ const el=document.querySelectorAll("[data-im=card]")[' +
        index +
        ']; if(!el) return false; const r=el.getBoundingClientRect(); const opts={bubbles:true,cancelable:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,pointerId:1,pointerType:"mouse",isPrimary:true,button:0,buttons:1}; el.dispatchEvent(new PointerEvent("pointerdown",opts)); const up=Object.assign({},opts,{buttons:0}); el.dispatchEvent(new PointerEvent("pointerup",up)); return true; })()',
    );
    for (let i = 0; i < 8 && !btn; i += 1) {
      await H.sleep(250);
      btn = await btnVisible();
    }
  }
  if (!btn) {
    await ctx.page.doubleClick('[data-im="card-body"]', index).catch(() => null);
    await H.sleep(800);
  }
  if (!btn) {
    await ctx.click('[data-im="card"]', index).catch(() => null);
    await H.sleep(700);
  }
  btn = await btnVisible();
  if (!btn) throw new Error('卡片编辑入口不可见（index=' + index + '）');
  await ctx.click(btn, 0, { timeout: 15000 });
  await H.sleep(450);
  const editorReady = await ctx.page.eval('!!document.querySelector("[data-im=card-editor]")');
  if (!editorReady) {
    await ctx.page.doubleClick('[data-im="card-body"]', 0).catch(() => null);
    await H.sleep(700);
  }
  const ok = await ctx.page.eval(
    '(function(){ const el = document.querySelector("[data-im=card-editor]"); if (!el) return false; el.focus(); el.value = ' +
      JSON.stringify(text) +
      '; el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ' +
      JSON.stringify(text) +
      ', inputType: "insertText" })); return true; })()',
  );
  await H.sleep(150);
  if (finish) {
    const btn2 = await btnVisible();
    if (btn2) await ctx.click(btn2, 0, { timeout: 15000 });
    await H.sleep(450);
  }
  return ok;
}

/**
 * 进入某张卡的编辑态并写入 text（显式等到编辑器出现、校验 value 真的变了）。
 * 与 typeCard 的差别：不靠 toggle 猜测，也不在编辑态里误点「完成编辑」。
 */
export async function editCardText(ctx, index, text) {
  try {
    await ctx.waitExpr('document.querySelectorAll("[data-im=card]").length > ' + index, 20000, '卡片渲染');
  } catch (e) {}
  await H.sleep(300);
  const editorOpen = async () => ctx.page.eval('!!document.querySelector("[data-im=card-editor]")');
  const editBtnVisible = async () =>
    ctx.page.eval(
      '(function(){ const sels=["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels){ const el=document.querySelector(q); if (el){ const r=el.getBoundingClientRect(); if (r.width>0&&r.height>0) return q; } } return null; })()',
    );
  if (!(await editorOpen())) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await ctx.click('[data-im="card"]', index).catch(() => null);
      await H.sleep(350);
      let btn = await editBtnVisible();
      if (!btn) {
        // 首屏提示 / 命中测试可能挡住卡片：合成同一条 pointerdown/up 路径
        await ctx.page.eval(
          '(function(){ const el=document.querySelectorAll("[data-im=card]")[' +
            index +
            ']; if(!el) return false; const r=el.getBoundingClientRect(); const opts={bubbles:true,cancelable:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,pointerId:1,pointerType:"mouse",isPrimary:true,button:0,buttons:1}; el.dispatchEvent(new PointerEvent("pointerdown",opts)); const up=Object.assign({},opts,{buttons:0}); el.dispatchEvent(new PointerEvent("pointerup",up)); return true; })()',
        ).catch(() => null);
        await H.sleep(400);
        btn = await editBtnVisible();
      }
      if (!btn) {
        await ctx.page.doubleClick('[data-im="card-body"]', index).catch(() => null);
        await H.sleep(600);
        btn = await editBtnVisible();
      }
      if (btn) await ctx.click(btn, 0, { timeout: 8000 }).catch(() => null);
      await H.sleep(500);
      if (await editorOpen()) break;
    }
  }
  if (!(await editorOpen())) return { ok: false, reason: 'editor-not-open' };
  const set = await ctx.page.eval(
    '(function(){ const el=document.querySelector("[data-im=card-editor]"); if(!el) return null; el.focus(); el.value=' +
      JSON.stringify(text) +
      '; el.dispatchEvent(new InputEvent("input",{bubbles:true,data:' +
      JSON.stringify(text) +
      ',inputType:"insertText"})); return el.value; })()',
  );
  await H.sleep(200);
  return { ok: set === text, value: set };
}

/** 只往当前编辑器里再输入一版（不退出编辑）。 */
export async function retypeCard(ctx, text) {
  return ctx.page.eval(
    '(function(){ const el = document.querySelector("[data-im=card-editor]"); if (!el) return false; el.focus(); el.value = ' +
      JSON.stringify(text) +
      '; el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ' +
      JSON.stringify(text) +
      ', inputType: "insertText" })); return true; })()',
  );
}

/** 影响确认层 / 结果层是否开着（scrim 可见）。 */
export async function overlayOpen(ctx) {
  return ctx.page.eval(
    '(function(){ const s=document.querySelector("[data-im=impact-confirm-scrim]"); return !!(s && getComputedStyle(s).display !== "none"); })()',
  );
}

/**
 * 关掉所有可能挡住板面的浮层（影响确认 / 结果层 / 关闭影响批注）。
 * 只用于场景之间的清场：不影响被测行为，也不会用 store 动作替代界面操作。
 * 返回实际做了哪些关闭动作，便于在证据里写清楚。
 */
export async function dismissOverlays(ctx) {
  const actions = [];
  for (let i = 0; i < 4; i += 1) {
    if (await overlayOpen(ctx)) {
      const opened = await ctx.page.eval(
        '(function(){ const el=document.querySelector("[data-im=impact-confirm]"); return !!(el && el.offsetParent !== null); })()',
      );
      if (opened) {
        const cancel = await ui(ctx, '[data-im="impact-confirm-cancel"]');
        if (cancel.exists && cancel.visible) {
          await clickAsserted(ctx, '[data-im="impact-confirm-cancel"]', { timeout: 8000 });
          actions.push('impact-confirm-cancel');
          await H.sleep(600);
          continue;
        }
      }
      // 只剩 scrim（异常残留）：用 Escape 关
      await ctx.page.pressKey('Escape').catch(() => null);
      actions.push('escape');
      await H.sleep(400);
      continue;
    }
    const outcome = await ui(ctx, '[data-im="impact-confirm-outcome-close"]');
    if (outcome.exists && outcome.visible) {
      await clickAsserted(ctx, '[data-im="impact-confirm-outcome-close"]', { timeout: 8000 });
      actions.push('outcome-close');
      await H.sleep(400);
      continue;
    }
    break;
  }
  return { actions, open: await overlayOpen(ctx) };
}

/** 点一次编辑按钮完成编辑；返回编辑器是否真的关闭。 */
export async function finishEditing(ctx) {
  const btn = await ctx.page.eval(
    '(function(){ const sels=["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels){ const el=document.querySelector(q); if (el){ const r=el.getBoundingClientRect(); if (r.width>0&&r.height>0) return q; } } return null; })()',
  );
  if (!btn) return { clicked: false, closed: false };
  await ctx.click(btn, 0, { timeout: 15000 }).catch(() => null);
  await H.sleep(500);
  const closed = !(await ctx.page.eval('!!document.querySelector("[data-im=card-editor]")'));
  return { clicked: true, closed };
}

/* ------------------------------------------------------------------ */
/* store 事实快照                                                      */
/* ------------------------------------------------------------------ */
export async function snapshot(ctx) {
  await ctx.ensureStore();
  return ctx.page.eval(`(async function(){
    const s = window.__q46Store;
    const cards = (s.board && s.board.cards) ? s.board.cards : [];
    const jobs = Object.values(s.cardImportJobs || {}).map(function(j){
      return { attemptId: j.attemptId, boardId: j.boardId, cardId: j.cardId, fileName: j.fileName,
        phase: j.phase, awaitingSave: j.awaitingSave, acknowledged: j.acknowledged, error: j.error,
        attachmentId: (j.attachment && (j.attachment.id || j.attachment.attachmentId)) || null,
        pendingAttachmentId: (j.pendingAttachment && (j.pendingAttachment.id || j.pendingAttachment.attachmentId)) || null };
    });
    const batch = s.currentImportBatch;
    return {
      boardId: s.boardId,
      seq: s.board ? s.board.seq : null,
      saveStatus: s.saveStatus,
      saveError: s.saveError,
      dirty: s.dirty,
      loadError: s.loadError,
      boardLoadedOnce: s.boardLoadedOnce,
      boardCards: cards.map(function(c){ return { id: c.id, kind: c.kind, content: (c.content||"").slice(0,120),
        name: (c.meta && c.meta.name) || null,
        attachmentId: (c.meta && (c.meta.attachmentId || (c.meta.file && c.meta.file.attachmentId))) || null,
        docText: (c.meta && c.meta.docText) || null,
        blocks: c.meta && c.meta.doc && c.meta.doc.blocks ? c.meta.doc.blocks.map(function(b){ return b.text; }) : [] }; }),
      drafts: JSON.parse(JSON.stringify(s.drafts || {})),
      draftStates: JSON.parse(JSON.stringify(s.draftStates || {})),
      draftConflicts: JSON.parse(JSON.stringify(s.draftConflicts || {})),
      pendingDraftClearKeys: JSON.parse(JSON.stringify(s.pendingDraftClearKeys || [])),
      draftSaveStatus: s.draftSaveStatus,
      draftSaveError: s.draftSaveError,
      pendingImpact: s.pendingImpact ? { boardId: s.pendingImpact.boardId, checkId: s.pendingImpact.checkId,
        previewRev: s.pendingImpact.previewRev, stateVersion: s.pendingImpact.stateVersion,
        candidateKey: String(s.pendingImpact.candidateKey || "").slice(0, 600),
        affected: (s.pendingImpact.affected || []).map(function(a){ return a.intentId || a.intent_id; }) } : null,
      impactCheckError: s.impactCheckError,
      intents: (s.intents || []).map(function(i){ return { id: i.id, title: i.title, status: i.status }; }),
      intentViews: (s.intentViews || []).map(function(v){ return { id: v.intent_id || v.intentId, title: v.title, status: v.status }; }),
      intentsLoadState: s.intentsLoadState,
      intentsLoadError: s.intentsLoadError,
      cancelRecovery: s.cancelRecovery ? { boardId: s.cancelRecovery.boardId, active: s.cancelRecovery.active, reason: s.cancelRecovery.reason } : null,
      cardImportJobs: jobs,
      currentImportBatch: batch ? { id: batch.id, boardId: batch.boardId,
        items: (batch.items || []).map(function(it){ return { key: it.key, name: it.name, status: it.status, error: it.error, cardId: it.cardId || null }; }) } : null,
      activeCardImport: s.activeCardImportForBoard ? (function(){ const a = s.activeCardImportForBoard; return a ? { attemptId: a.attemptId, phase: a.phase, fileName: a.fileName } : null; })() : null,
    };
  })()`);
}

/* ------------------------------------------------------------------ */
/* 界面事实：真实出现 / 可见 / 可点                                    */
/* ------------------------------------------------------------------ */
export async function ui(ctx, selector, index) {
  const i = index || 0;
  return ctx.page.eval(
    '(function(){ const el = document.querySelectorAll(' +
      JSON.stringify(selector) +
      ')[' +
      i +
      ']; if (!el) return { exists: false }; el.scrollIntoView({ block: "center", inline: "center" }); const r = el.getBoundingClientRect(); const st = getComputedStyle(el); const cx = r.x + r.width/2, cy = r.y + r.height/2; const hit = document.elementFromPoint(cx, cy); return { exists: true, visible: r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none" && Number(st.opacity) > 0.05, inViewport: cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight, disabled: !!el.disabled, hitSelf: !!(hit && (hit === el || el.contains(hit))), hitTag: hit ? hit.tagName.toLowerCase() : null, text: (el.innerText || el.textContent || "").trim().slice(0, 120), rect: { x: r.x, y: r.y, w: r.width, h: r.height } }; })()',
  );
}

/**
 * 断言「按钮真实出现且可点」，然后从**这个按钮**完成真实鼠标点击。
 * 返回 { ok, ui, clicked }；ok=false 时调用方必须记 FAIL（不许用 store 动作冒充）。
 */
export async function clickAsserted(ctx, selector, opts) {
  const o = opts || {};
  const index = o.index || 0;
  const deadline = Date.now() + (o.timeout || 20000);
  let info = null;
  while (Date.now() < deadline) {
    info = await ui(ctx, selector, index);
    if (info && info.exists && info.visible && !info.disabled && info.inViewport) break;
    await H.sleep(150);
  }
  if (!info || !info.exists) return { ok: false, reason: 'not-found', ui: info };
  if (!info.visible) return { ok: false, reason: 'not-visible', ui: info };
  if (info.disabled) return { ok: false, reason: 'disabled', ui: info };
  if (!info.inViewport) return { ok: false, reason: 'out-of-viewport', ui: info };
  await ctx.page.clickAt(info.rect.x + info.rect.w / 2, info.rect.y + info.rect.h / 2, info);
  await H.sleep(o.settleMs === undefined ? 400 : o.settleMs);
  return { ok: true, ui: info, clicked: true };
}

export async function pageText(ctx) {
  return ctx.page.eval('(function(){ return (document.body && document.body.innerText || "").slice(0, 20000); })()');
}

/* ------------------------------------------------------------------ */
/* 请求台账                                                            */
/* ------------------------------------------------------------------ */
export function reqs(ctx, filter) {
  return ctx.requests(filter || {});
}

export function brief(list) {
  return (list || []).map((r) => ({
    m: r.method,
    u: String(r.url).replace(H.API, ''),
    s: r.status,
    failed: r.failed,
    body: r.postData ? String(r.postData).slice(0, 400) : '',
  }));
}

export function parseBody(entry) {
  try {
    return JSON.parse(entry.postData || '{}');
  } catch {
    return null;
  }
}

export async function waitNetwork(ctx, pred, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  while (Date.now() < deadline) {
    const found = ctx.network.find(pred);
    if (found) return found;
    await H.sleep(100);
  }
  throw new Error('等待请求超时：' + (label || 'network'));
}

export async function waitFor(ctx, fn, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) {
      last = 'ERR:' + String(e).slice(0, 120);
    }
    await H.sleep(150);
  }
  throw new Error('等待超时（' + (label || '') + '），最后=' + JSON.stringify(last).slice(0, 300));
}

/* ------------------------------------------------------------------ */
/* 后端只读回读                                                        */
/* ------------------------------------------------------------------ */
export async function backendDrafts(boardId) {
  const r = await H.readDrafts(boardId);
  return r.data;
}

export async function backendBoard(boardId) {
  const r = await H.readBoard(boardId);
  return { seq: r.seq, state: r.state };
}

/* ------------------------------------------------------------------ */
/* 上传（真实「添加 → 文件」入口）                                     */
/* ------------------------------------------------------------------ */
export async function uploadViaAddMenu(ctx, file) {
  await ctx.click('[data-im="add-menu"]', 0);
  await H.sleep(400);
  const chooser = ctx.page.setFilesOnChooser([file], { timeout: 20000 });
  await ctx.click('[data-im="add-file"]', 0);
  await chooser;
  return ctx.waitForNetwork(
    (r) => r.method === 'POST' && r.url.indexOf('/api/attachments/upload') >= 0,
    20000,
    '上传请求',
  );
}

/**
 * 按「第几次命中」决定延迟 / 动作的 Fetch 规则（lib.mjs 的 ctx.pass 只能一条规则一个延迟）。
 *
 * steps[i] = { delayMs, action: 'pass' | 'fail' | 'fulfill', status?, body? }
 * 第 1 次命中用 steps[0]，第 2 次用 steps[1]，之后沿用最后一条。
 * 匹配谓词始终命中（避免请求落到 cdp.mjs 的 30 分钟长挂规则上）。
 */
export function sequence(ctx, predicate, steps) {
  const list = steps && steps.length ? steps : [{ delayMs: 0, action: 'pass' }];
  ctx.page.delayRequests(predicate, 30 * 60 * 1000);
  const delayRule = ctx.page.fetchDelayRules[ctx.page.fetchDelayRules.length - 1];
  const rule = { match: predicate, hits: 0, active: true, repeat: true, delayRule: null };
  const stepFor = () => list[Math.min(Math.max(rule.hits - 1, 0), list.length - 1)];
  Object.defineProperty(rule, 'kind', { get: () => stepFor().action || 'pass' });
  Object.defineProperty(rule, 'delayMs', { get: () => stepFor().delayMs || 0 });
  Object.defineProperty(rule, 'status', { get: () => stepFor().status || 200 });
  Object.defineProperty(rule, 'body', { get: () => stepFor().body || {} });
  Object.defineProperty(rule, 'contentType', { get: () => stepFor().contentType });
  rule.delayRule = delayRule;
  ctx.fetchRules.push(rule);
  return rule;
}

/** 浏览器拖放一批文件（走 AddMenu 的批次导入入口）。 */
export async function dropFiles(ctx, files) {
  const specs = files.map((f) => ({
    name: f.name,
    type: f.type || 'text/plain',
    dataUrl: 'data:' + (f.type || 'text/plain') + ';base64,' + Buffer.from(f.content, 'utf8').toString('base64'),
  }));
  return ctx.page.dragFiles('[data-im="add-menu"]', specs);
}
