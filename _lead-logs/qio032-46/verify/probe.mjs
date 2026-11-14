/**
 * 探针：打开真实应用（Edge+CDP），进入互动模式，dump 关键 DOM/网络/store 可访问性。
 * 只读；不改产品文件。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const WT = 'D:/qio-dev/pm-qio032-integration';
const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
const API = process.env.QIO_E2E_API || 'http://127.0.0.1:8734';
const OUT = resolve(HERE, '..', 'probe');
mkdirSync(OUT, { recursive: true });
const cdp = await import(pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href);
const { CdpBrowser, Page, sleep } = cdp;

const network = [];
const browser = await CdpBrowser.launch({ port: 9333 });
const page = await Page.create(browser);
await page.setViewport(1440, 900);
const sess = page.sessionId;
browser.on((msg) => {
  if (msg.sessionId !== sess) return;
  if (msg.method === 'Network.requestWillBeSent') {
    const p = msg.params;
    network.push({ method: p.request.method, url: p.request.url, ts: Date.now(), postData: (p.request.postData || '').slice(0, 2000) });
  }
});
await browser.send('Network.enable', {}, sess);
await page.goto(BASE + '/?probe=1');
const ready = await page.waitFor('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', { timeout: 60000, label: '应用就绪' });
console.log('APP_READY', ready);
await sleep(1200);

const mode = await page.eval('(function(){ const btns=[...document.querySelectorAll("#composer-mode-switch .mode-btn")]; return btns.map(function(b){ return {label:(b.innerText||"").trim(), aria:b.getAttribute("aria-label"), cls:b.className, data:b.getAttribute("data-mode")}; }); })()');
console.log('MODE_BTNS', JSON.stringify(mode));
const clicked = await page.eval('(function(){ const btn=document.querySelector("#composer-mode-switch .mode-btn"); if(!btn) return false; btn.click(); return true; })()');
console.log('CLICKED_MODE', clicked);
await sleep(2500);
const boardFacts = await page.eval('(function(){ var e=document.querySelector("[data-im=\\"save-status\\"],[data-save-status]"); return { url: location.hash, hasBoard: !!document.querySelector("[data-im=\\"board\\"]"), hasStage: !!document.querySelector("[data-im=\\"interactive-stage\\"]"), toolbar: !!document.querySelector("[data-im=\\"board-toolbar\\"]"), addMenu: !!document.querySelector("[data-im=\\"add-menu\\"]"), saveStatus: e ? {text:(e.innerText||"").trim(), attrs:[...e.attributes].map(function(a){return a.name+"="+a.value;})} : null, bodyIm: [...new Set([...document.querySelectorAll("[data-im]")].map(function(x){return x.getAttribute("data-im");}))].sort() }; })()');
console.log('BOARD_FACTS', JSON.stringify(boardFacts));
await page.screenshot(join(OUT, 'probe-board.png'));

try {
  const storeInfo = await page.eval('(async function(){ const mod = await import("/src/stores/interactive.ts"); const names = Object.keys(mod); const store = mod.useInteractiveStore(); return { names: names.slice(0, 80), boardId: store.boardId, boardSeq: store.board && store.board.seq, saveStatus: store.saveStatus, dirty: store.dirty, hasSelectBoard: typeof store.selectBoard }; })()');
  console.log('STORE', JSON.stringify(storeInfo));
} catch (e) { console.log('STORE_ERR', String(e).slice(0,500)); }

const imReqs = network.filter(function(n){ return n.url.indexOf('/api/interactive') >= 0; });
console.log('IM_REQS', JSON.stringify(imReqs.map(function(n){ return {m:n.method,u:n.url.replace(API,'')}; }), null, 1).slice(0,3000));
writeFileSync(join(OUT, 'probe-network.json'), JSON.stringify(network, null, 2));
await browser.close();
