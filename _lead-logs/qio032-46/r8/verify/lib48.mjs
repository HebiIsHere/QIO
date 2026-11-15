/**
 * QIO #46 第八轮（R8）浏览器验收 harness 追加层。
 *
 * 在 r7 的 lib.mjs / lib47.mjs 之上**只追加**能力，不改 r7 文件：
 *  - gate()：手动「挂起 → 放行/注入失败/自定义响应」的 CDP Fetch 闸门（可多次命中、逐条控制），
 *    解决 r7 sequence() 只能按固定延迟放行、无法「先切板再放行」的问题；
 *  - openTab()：同一浏览器第二个真实标签页（同源共享 localStorage），带自己的网络台账与闸门；
 *  - 后端只读回读：submission 记录、草稿、板面；
 *  - 本机恢复记录（localStorage qio.draft.*）读取与列举；
 *  - 执行中任务种子（走 seed-r8.py，隔离数据目录）。
 *
 * 纪律：只写 _lead-logs/qio032-46/r8；不碰产品文件；驱动不稳记 UNVERIFIED，不编造 PASS。
 */
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as H from '../../verify/lib.mjs';
import * as L from '../../verify/lib47.mjs';

export { H, L };
export const HERE = dirname(fileURLToPath(import.meta.url));
export const DATA_DIR = process.env.QIO_UI_DATA_DIR || '';
export const BACKEND_ROOT = process.env.QIO_BACKEND_ROOT || 'D:/qio-dev/q46-r8';
export const API = H.API;

/* ------------------------------------------------------------------ */
/* 后端只读回读                                                        */
/* ------------------------------------------------------------------ */
export async function submissions(boardId, limit = 50) {
  const r = await H.apiGet('/api/interactive/boards/' + encodeURIComponent(boardId) + '/submissions?limit=' + limit);
  return { status: r.status, list: (r.data && r.data.submissions) || [], raw: r.data };
}
export async function submissionCount(boardId) {
  const s = await submissions(boardId);
  return s.list.length;
}
export async function backendDrafts(boardId) {
  const r = await H.readDrafts(boardId);
  return r.data || {};
}
export async function backendBoard(boardId) {
  const r = await H.readBoard(boardId);
  return { seq: r.seq, state: r.state || {}, status: r.status };
}

/* ------------------------------------------------------------------ */
/* 执行中任务种子（隔离数据目录）                                      */
/* ------------------------------------------------------------------ */
export function seedRunningTask(boardId, cardId, title) {
  const seedPy = join(HERE, 'seed-r8.py');
  const backend = H.WT; // lib.mjs 的 QIO_UI_WT = q46-r8（始终用产品后端）
  const r = spawnSync(
    'uv',
    ['run', '--frozen', 'python', seedPy, '--api', API, '--board', boardId, '--card', cardId, '--title', title],
    {
      cwd: join(backend, 'backend'),
      env: Object.assign({}, process.env, {
        QIO_DATA_DIR: DATA_DIR,
        PYTHONPATH: join(backend, 'backend', 'src'),
      }),
      encoding: 'utf8',
      timeout: 180000,
    },
  );
  try {
    return JSON.parse((r.stdout || '').trim().split('\n').pop());
  } catch {
    return { ok: false, raw: (r.stdout || '').slice(-500), err: (r.stderr || '').slice(-500) };
  }
}

/* ------------------------------------------------------------------ */
/* 手动 Fetch 闸门                                                     */
/* ------------------------------------------------------------------ */
/**
 * 挂起命中 predicate 的请求，直到调用方显式放行。
 *
 * target 需要 { browser, sessionId, page }（lib.mjs 的 ctx 与 openTab() 的 tab 都满足）。
 * 实现：先用 page.delayRequests 打开 Fetch 域并给一条 30 分钟兜底延迟，
 * 再挂自己的 Fetch.requestPaused 监听收集 requestId；release 时逐条 continue/fulfill/fail。
 * releaseAll() 之后同一 predicate 的后续请求自动放行（不会把场景外的请求挂死）。
 */
export function gate(target, predicate, label, opts) {
  const { browser, sessionId, page } = target;
  const o = opts || {};
  /** delayMatch 是交给 cdp.mjs 兜底延迟的较宽谓词（2 参数）；predicate 可额外看请求体。 */
  const delayMatch = o.delayMatch || ((u, m) => predicate(u, m, ''));
  const held = [];
  let open = true;
  let released = false;
  page.delayRequests(delayMatch, 30 * 60 * 1000);
  browser.on((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method !== 'Fetch.requestPaused') return;
    const req = (msg.params || {}).request || {};
    const url = String(req.url || '');
    const method = String(req.method || 'GET');
    const postData = String(req.postData || '');
    if (!predicate(url, method, postData)) {
      /**
       * 不归本闸门拦截、但被 cdp.mjs 的宽谓词挂住的请求：立刻放行，避免把队列里的其他请求挂死。
       */
      if (delayMatch(url, method) && !released) {
        browser.send('Fetch.continueRequest', { requestId: msg.params.requestId }, sessionId).catch(() => null);
      }
      return;
    }
    /**
     * 放行之后：同一 predicate 的后续请求必须立刻 continue。
     * 注意 cdp.mjs 的 delayRequests 兜底规则仍在（30 分钟），这里不主动 continue 就会把后续请求挂死。
     */
    if (released || !open || (o.once && held.length >= 1)) {
      browser.send('Fetch.continueRequest', { requestId: msg.params.requestId }, sessionId).catch(() => null);
      return;
    }
    held.push({ requestId: msg.params.requestId, url, method, postData, at: Date.now() });
  });
  const send = (action, h) => {
    const a = action || {};
    if (a.kind === 'fulfill') {
      browser
        .send(
          'Fetch.fulfillRequest',
          {
            requestId: h.requestId,
            responseCode: a.status || 200,
            responseHeaders: [
              { name: 'Content-Type', value: a.contentType || 'application/json' },
              { name: 'Access-Control-Allow-Origin', value: '*' },
              { name: 'Access-Control-Allow-Headers', value: '*' },
              { name: 'Access-Control-Allow-Methods', value: '*' },
            ],
            body: Buffer.from(typeof a.body === 'string' ? a.body : JSON.stringify(a.body || {}), 'utf8').toString('base64'),
          },
          sessionId,
        )
        .catch(() => null);
    } else if (a.kind === 'fail') {
      browser.send('Fetch.failRequest', { requestId: h.requestId, errorReason: a.errorReason || 'Failed' }, sessionId).catch(() => null);
    } else {
      browser.send('Fetch.continueRequest', { requestId: h.requestId }, sessionId).catch(() => null);
    }
  };
  return {
    label: label || 'gate',
    held,
    get hits() {
      return held.length;
    },
    async waitForHeld(timeout = 20000, count = 1) {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        if (held.length >= count) return held.slice(0, count);
        await H.sleep(100);
      }
      throw new Error('闸门没有拦住请求（' + this.label + '）：held=' + held.length);
    },
    async releaseOne(action) {
      const h = held.shift();
      if (!h) return null;
      send(action, h);
      await H.sleep(60);
      return h;
    },
    /** 放行当前已挂起的第一条（可注入失败/自定义响应），之后同 predicate 的请求一律直接放行。 */
    async releaseFirst(action) {
      const h = await this.releaseOne(action);
      released = true;
      return h;
    },
    async releaseAll(action) {
      released = true;
      const items = held.splice(0);
      for (const h of items) {
        send(action, h);
        await H.sleep(40);
      }
      return items;
    },
    /** 不对已挂起的请求动手，只让后续请求直接放行（用于「旧请求一直挂着」的对照） */
    stopHolding() {
      released = true;
    },
  };
}

/* ------------------------------------------------------------------ */
/* 第二标签页（同浏览器默认上下文 → 同源共享 localStorage）            */
/* ------------------------------------------------------------------ */
export async function openTab(ctx, query, opts) {
  const o = opts || {};
  const page = await H.Page.create(ctx.browser);
  await page.setViewport(1440, 900);
  const network = [];
  const byId = new Map();
  ctx.browser.on((msg) => {
    if (msg.sessionId !== page.sessionId) return;
    const p = msg.params || {};
    if (msg.method === 'Network.requestWillBeSent') {
      const entry = {
        requestId: p.requestId,
        method: p.request.method,
        url: p.request.url,
        postData: p.request.postData || '',
        startedAt: Date.now(),
        status: null,
        failed: null,
      };
      network.push(entry);
      byId.set(p.requestId, entry);
    } else if (msg.method === 'Network.responseReceived') {
      const entry = byId.get(p.requestId);
      if (entry) entry.status = p.response.status;
    } else if (msg.method === 'Network.loadingFailed') {
      const entry = byId.get(p.requestId);
      if (entry) entry.failed = p.errorText || 'failed';
    }
  });
  await ctx.browser.send('Network.enable', {}, page.sessionId);

  const tab = {
    page,
    sessionId: page.sessionId,
    browser: ctx.browser,
    network,
    isTab: true,
    async ensureStore() {
      return page.eval(
        '(async function(){ if (!window.__q46Store) { const mod = await import("/src/stores/interactive.ts"); window.__q46Store = mod.useInteractiveStore(); } return { boardId: window.__q46Store.boardId }; })()',
      );
    },
    async click(selector, index) {
      return page.click(selector, index || 0, { timeout: 20000 });
    },
    async waitExpr(expr, timeout, label) {
      return page.waitFor(expr, { timeout: timeout || 20000, label: label || expr });
    },
    async storeEval(expression) {
      await tab.ensureStore();
      return page.eval('(async function(){ const s = window.__q46Store; ' + expression + ' })()');
    },
    async storeState() {
      return tab.storeEval(
        'var cards=(s.board&&s.board.cards)||[]; return { boardId:s.boardId, seq:s.board?s.board.seq:null, saveStatus:s.saveStatus, saveError:s.saveError, dirty:s.dirty, loadError:s.loadError, drafts:JSON.parse(JSON.stringify(s.drafts||{})), draftStates:JSON.parse(JSON.stringify(s.draftStates||{})), draftConflicts:JSON.parse(JSON.stringify(s.draftConflicts||{})), pendingDraftClearKeys:JSON.parse(JSON.stringify(s.pendingDraftClearKeys||[])), cards:cards.map(function(c){return {id:c.id,content:(c.content||"").slice(0,200)}})};',
      );
    },
    async requests(filter) {
      const f = filter || {};
      return network.filter((item) => {
        if (f.method && item.method !== f.method) return false;
        if (f.contains && item.url.indexOf(f.contains) < 0) return false;
        if (f.since && item.startedAt < f.since) return false;
        return true;
      });
    },
    async waitForNetwork(predicate, timeout, label) {
      const deadline = Date.now() + (timeout || 20000);
      while (Date.now() < deadline) {
        const found = network.find(predicate);
        if (found) return found;
        await H.sleep(100);
      }
      throw new Error('等待请求超时：' + (label || 'network'));
    },
    gate(predicate, label) {
      return gate(tab, predicate, label);
    },
    async enterInteractive() {
      const has = await page.eval('!!document.querySelector("[data-im=board]")');
      if (has) return { already: true };
      await page.click('#composer-mode-switch .mode-btn', 0, { timeout: 20000 });
      await page.waitFor('!!document.querySelector("[data-im=board]")', { timeout: 30000, label: '第二标签页互动板面' });
      await H.sleep(1200);
      return { already: false };
    },
    async switchBoard(id) {
      await tab.ensureStore();
      return page.eval('(async function(){ return await window.__q46Store.selectBoard(' + JSON.stringify(String(id)) + '); })()');
    },
    async shot(name) {
      const file = join(o.outDir || L.OUT || HERE, 'shots', 'tab-' + name + '.png');
      try {
        await page.screenshot(file);
        return file;
      } catch {
        return null;
      }
    },
  };
  await page.goto(H.BASE + '/?' + (query || 'tab=' + Date.now()));
  await page.waitFor('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', {
    timeout: 60000,
    label: '第二标签页应用就绪',
  });
  await H.sleep(900);
  return tab;
}

/* ------------------------------------------------------------------ */
/* 本机恢复记录（localStorage）                                        */
/* ------------------------------------------------------------------ */
export function localCardKey(cardId) {
  return 'qio.draft.card.local-' + cardId;
}
export async function readLocalRecord(page, cardId) {
  const raw = await page.eval(
    '(function(){ try { var raw = localStorage.getItem(' + JSON.stringify(localCardKey(cardId)) + '); return raw == null ? null : raw; } catch (e) { return "ERR:" + String(e); } })()',
  );
  if (raw == null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return { raw: String(raw) };
  }
}
export async function listLocalKeys(page) {
  return page.eval(
    '(function(){ try { var out=[]; for (var i=0;i<localStorage.length;i++){ var k=localStorage.key(i); if (k && k.indexOf("qio.draft.")===0) out.push(k); } return out; } catch (e) { return ["ERR:"+String(e)]; } })()',
  );
}
export async function clearLocalDrafts(page) {
  return page.eval(
    '(function(){ try { var out=[]; for (var i=localStorage.length-1;i>=0;i--){ var k=localStorage.key(i); if (k && k.indexOf("qio.draft.")===0){ out.push(k); localStorage.removeItem(k); } } return out; } catch (e) { return ["ERR:"+String(e)]; } })()',
  );
}

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */
export function brief(list) {
  return (list || []).map((r) => ({
    m: r.method,
    u: String(r.url).replace(H.API, ''),
    s: r.status,
    failed: r.failed,
    body: r.postData ? String(r.postData).slice(0, 300) : '',
  }));
}
export function bodyOf(entry) {
  try {
    return JSON.parse(entry.postData || '{}');
  } catch {
    return null;
  }
}
export async function waitFor(fn, timeout, label) {
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
/** 打开板面并等到 store 真的是这块板面且已加载出卡片（区别于 r7 的 openBoard：等 store 事实）。 */
export async function openBoardReady(target, boardId, opts) {
  const o = opts || {};
  await target.ensureStore();
  const switched = await target.switchBoard(boardId);
  try {
    await waitFor(
      async () => {
        const st = await target.storeState();
        return st.boardId === boardId && (st.cards || []).length >= (o.cards === undefined ? 1 : o.cards) ? st : null;
      },
      o.timeout || 25000,
      '板面加载完成 ' + boardId,
    );
  } catch (e) {
    // 加载失败也返回事实，由调用方记录
    return { ok: false, reason: String(e).slice(0, 300), switched, state: await target.storeState() };
  }
  await H.sleep(o.settleMs === undefined ? 800 : o.settleMs);
  return { ok: true, state: await target.storeState(), switched };
}
/** 点真实「提交给 QIO」按钮前先确认它真实出现且可点。 */
export async function submitButtonInfo(ctx) {
  return L.ui(ctx, '[data-im="submit"]');
}
/** 读页面顶栏保存状态（与 r7 同口径）。 */
export async function uiSave(ctx) {
  return ctx.uiSaveStatus();
}
/** 真实点开某张卡的编辑入口并等编辑器出现；返回编辑器当前 value（不修改内容）。 */
export async function openCardEditor(target, index) {
  const i = index || 0;
  try {
    await target.waitExpr('document.querySelectorAll("[data-im=card]").length > ' + i, 20000, '卡片渲染');
  } catch (e) {}
  await H.sleep(300);
  const editorOpen = async () => target.page.eval('!!document.querySelector("[data-im=card-editor]")');
  const editBtnVisible = async () =>
    target.page.eval(
      '(function(){ const sels=["[data-im=card-icon-edit]","[data-im=card-edit]"]; for (const q of sels){ const el=document.querySelector(q); if (el){ const r=el.getBoundingClientRect(); if (r.width>0&&r.height>0) return q; } } return null; })()',
    );
  for (let attempt = 0; attempt < 8 && !(await editorOpen()); attempt += 1) {
    await target.click('[data-im="card"]', i).catch(() => null);
    await H.sleep(350);
    let btn = await editBtnVisible();
    if (!btn) {
      await target.page.eval(
        '(function(){ const el=document.querySelectorAll("[data-im=card]")[' + i + ']; if(!el) return false; const r=el.getBoundingClientRect(); const opts={bubbles:true,cancelable:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,pointerId:1,pointerType:"mouse",isPrimary:true,button:0,buttons:1}; el.dispatchEvent(new PointerEvent("pointerdown",opts)); const up=Object.assign({},opts,{buttons:0}); el.dispatchEvent(new PointerEvent("pointerup",up)); return true; })()',
      ).catch(() => null);
      await H.sleep(400);
      btn = await editBtnVisible();
    }
    if (btn) await target.click(btn, 0).catch(() => null);
    await H.sleep(500);
  }
  if (!(await editorOpen())) return { ok: false, reason: 'editor-not-open', value: null };
  const value = await target.page.eval('(function(){ const el=document.querySelector("[data-im=card-editor]"); return el ? el.value : null; })()');
  return { ok: true, value };
}

/** 点板面右下角空白，让当前选中卡片退出选中（等价用户点空白处）。 */
export async function clickBlankBoardArea(target) {
  const box = await target.page.eval(
    '(function(){ const b=document.querySelector("[data-im=board]"); if(!b) return null; const r=b.getBoundingClientRect(); return { x:r.x, y:r.y, w:r.width, h:r.height }; })()',
  );
  if (!box) return { ok: false, reason: 'no-board' };
  const x = box.x + box.w - 40;
  const y = box.y + box.h - 40;
  await target.page.clickAt(x, y);
  await H.sleep(600);
  return { ok: true, x, y };
}
/** 读提交相关公开状态（lib.mjs 的 storeState 不含 submitStatus，这里补上）。 */
export async function storeSubmitFacts(target) {
  await target.ensureStore();
  return target.page.eval(
    '(async function(){ const s = window.__q46Store; const cards=(s.board&&s.board.cards)||[]; return { boardId: s.boardId, seq: s.board?s.board.seq:null, submitStatus: s.submitStatus, submitError: s.submitError, submitNote: s.submitNote, saveStatus: s.saveStatus, saveError: s.saveError, dirty: s.dirty, loadError: s.loadError, understandingStatus: s.understandingStatus, cards: cards.map(function(c){ return { id:c.id, content:(c.content||"").slice(0,120) }; }) }; })()',
  );
}
/** 把某个 target 激活到前台（多标签页时被后台标签的计时器会被节流，驱动前先激活）。 */
export async function activate(target) {
  try {
    await target.browser.send('Target.activateTarget', { targetId: target.page.targetId });
    await H.sleep(150);
    return true;
  } catch (e) {
    return false;
  }
}
/** 关闭一个额外标签页（场景结束清场）。 */
export async function closeTab(tab) {
  try {
    await tab.browser.send('Target.closeTarget', { targetId: tab.page.targetId });
    return true;
  } catch (e) {
    return false;
  }
}
