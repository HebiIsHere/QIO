/**
 * QIO032-46 定向验收 harness 公共层（只写 _lead-logs/qio032-46，不改产品文件）。
 *
 * 真实浏览器：系统 Edge headless + CDP（复用 scripts/verify-pm-qio032/cdp.mjs）。
 * 可控时序：CDP Fetch 拦截（延迟 / 失败 / 自定义响应）。
 * 事实来源：
 *  - 浏览器 Network 事件（请求方法 / URL / 状态码 / 正文）
 *  - 页面 store（真实 Pinia store：boardId/board/seq/saveStatus/saveError/dirty/
 *    loadError/pendingImpact/intents/visibleRange/drafts/cardImportJobs）
 *  - 后端只读 API（GET state / drafts 等）与隔离数据目录（QIO_DATA_DIR）
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const WT = process.env.QIO_UI_WT || 'D:/qio-dev/pm-qio032-integration';
export const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
export const API = process.env.QIO_E2E_API || 'http://127.0.0.1:8734';
export const PROVIDER = process.env.QIO_E2E_PROVIDER || 'http://127.0.0.1:8807';
export const OUT = resolve(process.env.QIO_UI_OUT || join(HERE, '..', 'runs'));
export const CDP_PORT = Number(process.env.QIO_UI_CDP_PORT || '9333');

const cdp = await import(pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href);
export const { CdpBrowser, Page, sleep } = cdp;

export function nowIso() { return new Date().toISOString(); }

/** 后端只读/测试数据 API（Node 侧直连，不经过浏览器）。 */
export async function api(method, path, body) {
  const init = { method, headers: { 'content-type': 'application/json' } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(API + path, init);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { status: resp.status, ok: resp.ok, data };
}
export async function apiGet(path) { return api('GET', path); }
export async function apiPut(path, body) { return api('PUT', path, body); }
export async function apiPost(path, body) { return api('POST', path, body); }

export function boardStatePath(id) { return '/api/interactive/boards/' + encodeURIComponent(id) + '/state'; }
export function draftsPath(id) { return '/api/interactive/drafts/' + encodeURIComponent(id); }

export async function readBoard(id) {
  const r = await apiGet(boardStatePath(id));
  return { status: r.status, seq: r.data && r.data.state ? r.data.state.seq : null, state: r.data && r.data.state, boardId: r.data && r.data.board ? r.data.board.id : null, raw: r.data };
}
export async function putBoardDirect(id, state, reason, confirm) {
  const body = { state: state, reason: reason || 'harness' };
  if (confirm) body.confirm = confirm;
  return apiPut(boardStatePath(id), body);
}
export async function readDrafts(id) { return apiGet(draftsPath(id)); }

export async function createBoard(id, title) {
  return apiPost('/api/interactive/boards', { boardId: id, title: title || id });
}

/** 新建 harness：启动 Edge + 页面 + 网络采集 + Fetch 规则引擎。 */
export async function setup(opts) {
  const options = opts || {};
  const outDir = options.outDir || OUT;
  const shotsDir = join(outDir, 'shots');
  const materialsDir = join(outDir, 'materials');
  mkdirSync(shotsDir, { recursive: true });
  mkdirSync(materialsDir, { recursive: true });

  const browser = await CdpBrowser.launch({ port: options.cdpPort || CDP_PORT, headless: true });
  const page = await Page.create(browser);
  await page.setViewport(1440, 900);
  const sessionId = page.sessionId;

  const network = [];
  const byRequestId = new Map();
  const fetchRules = [];
  const results = [];
  const simulated = [];
  const consoleErrors = [];
  let stepNo = 0;

  const logFile = join(outDir, 'run.log');

  browser.on((msg) => {
    if (msg.sessionId !== sessionId) return;
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
      byRequestId.set(p.requestId, entry);
    } else if (msg.method === 'Network.responseReceived') {
      const entry = byRequestId.get(p.requestId);
      if (entry) entry.status = p.response.status;
    } else if (msg.method === 'Network.loadingFailed') {
      const entry = byRequestId.get(p.requestId);
      if (entry) entry.failed = p.errorText || 'failed';
    } else if (msg.method === 'Runtime.exceptionThrown') {
      const d = p.exceptionDetails || {};
      consoleErrors.push(String((d.exception && d.exception.description) || d.text || '').slice(0, 300));
    } else if (msg.method === 'Fetch.requestPaused') {
      const req = p.request || {};
      const url = String(req.url || '');
      const method = String(req.method || 'GET');
      const rule = fetchRules.find((item) => item.active && item.match(url, method));
      if (!rule) return; // 未命中：cdp.mjs 的默认监听会 continueRequest
      rule.hits += 1;
      if (!rule.repeat && rule.delayRule) {
        const at = page.fetchDelayRules.indexOf(rule.delayRule);
        if (at >= 0) page.fetchDelayRules.splice(at, 1);
        rule.delayRule = null;
      }
      const finish = () => {
        if (rule.kind === 'fulfill') {
          browser.send('Fetch.fulfillRequest', {
            requestId: p.requestId,
            responseCode: rule.status || 200,
            responseHeaders: [
              { name: 'Content-Type', value: rule.contentType || 'application/json' },
              { name: 'Access-Control-Allow-Origin', value: '*' },
              { name: 'Access-Control-Allow-Headers', value: '*' },
              { name: 'Access-Control-Allow-Methods', value: '*' },
            ],
            body: Buffer.from(typeof rule.body === 'string' ? rule.body : JSON.stringify(rule.body || {}), 'utf8').toString('base64'),
          }, sessionId).catch(() => null);
        } else if (rule.kind === 'fail') {
          browser.send('Fetch.failRequest', { requestId: p.requestId, errorReason: rule.errorReason || 'Failed' }, sessionId).catch(() => null);
        } else {
          browser.send('Fetch.continueRequest', { requestId: p.requestId }, sessionId).catch(() => null);
        }
      };
      if (rule.delayMs) setTimeout(finish, rule.delayMs);
      else finish();
    }
  });

  await browser.send('Network.enable', {}, sessionId);

  const ctx = {
    browser, page, sessionId, network, results, simulated, consoleErrors,
    outDir, shotsDir, materialsDir, fetchRules, startTime: Date.now(),
    log(line) {
      const text = String(line);
      console.log(text);
      try { writeFileSync(logFile, text + '\n', { encoding: 'utf8', flag: 'a' }); } catch {}
    },
    record(name, ok, detail, opts) {
      const o = opts || {};
      const entry = { name, ok: Boolean(ok) && !o.observe, observe: Boolean(o.observe), detail: detail === undefined ? null : detail };
      results.push(entry);
      const tag = o.observe ? 'OBSERVE' : (entry.ok ? 'PASS' : 'FAIL');
      let body = '';
      try { body = ' :: ' + JSON.stringify(detail).slice(0, 1600); } catch { body = ''; }
      ctx.log('[' + tag + '] ' + name + body);
      return entry.ok;
    },
    async shot(name) {
      stepNo += 1;
      const file = join(shotsDir, String(stepNo).padStart(2, '0') + '-' + name + '.png');
      try { await page.screenshot(file); } catch { return null; }
      return file;
    },
    async gotoApp(query) {
      await page.goto(BASE + '/?' + (query || ('run46=' + Date.now())));
      await page.waitFor('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', { timeout: 60000, label: '应用就绪' });
      await sleep(900);
    },
    async enterInteractive() {
      const hasBoard = await page.eval('!!document.querySelector("[data-im=board]")');
      if (hasBoard) return { already: true };
      await page.click('#composer-mode-switch .mode-btn', 0, { timeout: 20000 });
      await page.waitFor('!!document.querySelector("[data-im=board]")', { timeout: 30000, label: '互动板面' });
      await sleep(1500);
      return { already: false };
    },
    async backToConversation() {
      if (!(await page.eval('!!document.querySelector("[data-im=board]")'))) return true;
      await page.click('#composer-mode-switch .mode-btn', 0, { timeout: 20000 });
      await page.waitFor('!document.querySelector("[data-im=board]")', { timeout: 20000, label: '对话页' });
      await sleep(600);
      return true;
    },
    /** 真实点击：按钮中心坐标 + 命中测试（cdp.mjs Page.click）。 */
    async click(selector, index) { return page.click(selector, index || 0, { timeout: 20000 }); },
    async waitExpr(expr, timeout, label) {
      const deadline = Date.now() + (timeout || 20000);
      let last = null;
      while (Date.now() < deadline) {
        try { last = await page.eval('(function(){ try { return (' + expr + '); } catch (e) { return null; } })()'); } catch { last = 'ERR'; }
        if (last) return last;
        await sleep(150);
      }
      throw new Error('等待超时（' + (label || expr).slice(0, 80) + '），最后值=' + JSON.stringify(last));
    },
    async waitForNetwork(predicate, timeout, label) {
      const deadline = Date.now() + (timeout || 20000);
      while (Date.now() < deadline) {
        const found = network.find(predicate);
        if (found) return found;
        await sleep(100);
      }
      throw new Error('等待请求超时：' + (label || 'network'));
    },
    requests(filter) {
      const f = filter || {};
      return network.filter((item) => {
        if (f.method && item.method !== f.method) return false;
        if (f.contains && item.url.indexOf(f.contains) < 0) return false;
        if (f.since && item.startedAt < f.since) return false;
        if (f.status !== undefined && item.status !== f.status) return false;
        return true;
      });
    },
    countRequests(filter) { return ctx.requests(filter).length; },
    clearNetwork() { network.length = 0; byRequestId.clear(); },

    /* ---------- Fetch 可控时序 ---------- */
    /** 仅延迟（放行）。 */
    delay(match, ms) { page.delayRequests(match, ms); },
    /** 延迟后返回自定义响应。 */
    fulfill(match, opts) {
      const o = opts || {};
      page.delayRequests(match, 30 * 60 * 1000);
      const delayRule = page.fetchDelayRules[page.fetchDelayRules.length - 1];
      const rule = { kind: 'fulfill', match, delayMs: o.delayMs || 0, status: o.status || 200, body: o.body || {}, contentType: o.contentType, hits: 0, active: true, repeat: Boolean(o.repeat), delayRule };
      fetchRules.push(rule);
      return rule;
    },
    /** 延迟后让请求失败（网络层）。 */
    fail(match, opts) {
      const o = opts || {};
      page.delayRequests(match, 30 * 60 * 1000);
      const delayRule = page.fetchDelayRules[page.fetchDelayRules.length - 1];
      const rule = { kind: 'fail', match, delayMs: o.delayMs || 0, errorReason: o.errorReason, hits: 0, active: true, repeat: Boolean(o.repeat), delayRule };
      fetchRules.push(rule);
      return rule;
    },
    /** 延迟后放行（真实后端响应），用于「旧请求迟到但内容真实」的时序。 */
    pass(match, opts) {
      const o = opts || {};
      page.delayRequests(match, 30 * 60 * 1000);
      const delayRule = page.fetchDelayRules[page.fetchDelayRules.length - 1];
      const rule = { kind: 'pass', match, delayMs: o.delayMs || 0, hits: 0, active: true, repeat: Boolean(o.repeat), delayRule };
      fetchRules.push(rule);
      return rule;
    },
    ruleHits(rule) { return rule ? rule.hits : 0; },
    disableRule(rule) { if (rule) rule.active = false; },
    async clearRules() {
      for (const rule of fetchRules) rule.active = false;
      fetchRules.length = 0;
      await page.clearFailRequests().catch(() => null);
    },

    /* ---------- store（真实 Pinia store） ---------- */
    async ensureStore() {
      const info = await page.eval('(async function(){ if (!window.__q46Store) { const mod = await import("/src/stores/interactive.ts"); window.__q46Store = mod.useInteractiveStore(); } return { boardId: window.__q46Store.boardId, seq: window.__q46Store.board ? window.__q46Store.board.seq : null }; })()');
      return info;
    },
    async storeEval(expression) {
      await ctx.ensureStore();
      return page.eval('(async function(){ const s = window.__q46Store; ' + expression + ' })()');
    },
    async storeState() {
      await ctx.ensureStore();
      return page.eval('(async function(){ const s = window.__q46Store; const cards = (s.board && s.board.cards) ? s.board.cards : []; return { boardId: s.boardId, seq: s.board ? s.board.seq : null, boardCardIds: cards.map(function(c){return c.id;}), boardCards: cards.map(function(c){return {id:c.id, kind:c.kind, content:(c.content||"").slice(0,80), name:(c.meta&&c.meta.name)||null, attachmentId:(c.meta&&c.meta.attachmentId)||null};}), saveStatus: s.saveStatus, saveError: s.saveError, dirty: s.dirty, loadError: s.loadError, pendingImpact: s.pendingImpact ? { boardId: s.pendingImpact.boardId, checkId: s.pendingImpact.checkId, affected: (s.pendingImpact.affected||[]).map(function(a){return a.intentId||a.intent_id;}) } : null, impactCheckError: s.impactCheckError, visibleRange: s.visibleRange, intents: (s.intents||[]).map(function(i){return {id:i.id,title:i.title,status:i.status,materialRefs:i.materialRefs||i.material_refs||[]};}), intentViews: (s.intentViews||[]).map(function(v){return {id:v.intent_id||v.intentId,title:v.title,status:v.status};}), drafts: JSON.parse(JSON.stringify(s.drafts||{})), draftStates: JSON.parse(JSON.stringify(s.draftStates||{})), cardImportJobs: Object.keys(s.cardImportJobs||{}).map(function(k){const j=s.cardImportJobs[k]; return {key:k,attemptId:j.attemptId,boardId:j.boardId,cardId:j.cardId,fileName:j.fileName,phase:j.phase,attachment:j.attachment,pendingAttachment:j.pendingAttachment,awaitingSave:j.awaitingSave,acknowledged:j.acknowledged,error:j.error};}), activeImportBatch: s.currentImportBatch ? {id:s.currentImportBatch.id, items:(s.currentImportBatch.items||[]).map(function(it){return {key:it.key,name:it.name,status:it.status,error:it.error};})} : null, canUndo: s.canUndo, undoStackLen: (s.undoStack||[]).length }; })()');
    },
    async switchBoard(id) {
      return ctx.storeEval('return await s.selectBoard(' + JSON.stringify(String(id)) + ');');
    },
    async waitStore(predicate, timeout, label) {
      const deadline = Date.now() + (timeout || 20000);
      let last = null;
      while (Date.now() < deadline) {
        last = await ctx.storeState();
        try { if (predicate(last)) return last; } catch {}
        await sleep(150);
      }
      throw new Error('等待 store 状态超时（' + (label || '') + '），最后=' + JSON.stringify(last).slice(0, 500));
    },
    async uiSaveStatus() {
      return page.eval('(function(){ const el = document.querySelector("[data-iw-chrome-save]"); if (!el) return null; return { text: (el.innerText || "").trim(), tone: el.getAttribute("data-tone"), retry: !!document.querySelector("[data-iw-chrome-save-retry]") }; })()');
    },
    async uiImpact() {
      return page.eval('(function(){ const el = document.querySelector("[data-im=impact-confirm]"); const scrim = document.querySelector("[data-im=impact-confirm-scrim]"); const open = !!(el && scrim && getComputedStyle(scrim).display !== "none"); return { open, body: open && el ? (el.innerText || "").trim().slice(0, 600) : null, mode: el ? el.getAttribute("data-impact-mode") : null, submit: !!(open && document.querySelector("[data-im=impact-confirm-submit]")), cancel: !!(open && document.querySelector("[data-im=impact-confirm-cancel]")), outcome: (function(){const o=document.querySelector("[data-im=impact-confirm-outcome]"); return o && getComputedStyle(o).display !== "none" ? (o.innerText||"").trim().slice(0,400) : null;})() }; })()');
    },
    async uiCards() {
      return page.eval('(function(){ return [...document.querySelectorAll("[data-im=card]")].map(function(el){ const id = el.getAttribute("data-card-id") || (el.querySelector("[data-im=card-edit]") && el.querySelector("[data-im=card-edit]").getAttribute("data-card-id")); return { id: id, state: el.getAttribute("data-card-state"), text: (el.innerText||"").trim().slice(0,160) }; }); })()');
    },
    async finish() {
      const pass = results.filter((r) => r.ok && !r.observe).length;
      const fail = results.filter((r) => !r.ok && !r.observe).length;
      const observe = results.filter((r) => r.observe).length;
      const summary = {
        runId: new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14),
        at: nowIso(),
        base: BASE, api: API, provider: PROVIDER, cdpPort: options.cdpPort || CDP_PORT,
        out: outDir,
        mode: options.mode || 'observe',
        pass, fail, observe, total: results.length,
        results, simulated, consoleErrors: consoleErrors.slice(0, 60),
        note: '真实浏览器（系统 Edge + CDP）驱动真实 store 与真实后端；模型只打本机 fake provider；[模拟] 见 simulated。',
      };
      writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
      writeFileSync(join(outDir, 'console.log'), results.map((r) => '[' + (r.observe ? 'OBSERVE' : (r.ok ? 'PASS' : 'FAIL')) + '] ' + r.name + (r.detail ? ' :: ' + JSON.stringify(r.detail) : '')).join('\n') + '\n', 'utf8');
      ctx.log('');
      ctx.log('== 结果：PASS ' + pass + ' / FAIL ' + fail + ' / OBSERVE ' + observe + ' / TOTAL ' + results.length + ' ==');
      try { await browser.close(); } catch {}
      return summary;
    },
  };
  ctx.page = page;
  try { writeFileSync(logFile, '', 'utf8'); } catch {}
  return ctx;
}
