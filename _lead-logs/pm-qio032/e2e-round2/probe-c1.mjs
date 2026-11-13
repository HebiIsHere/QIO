/**
 * 补一张「联网搜索通道行」的可视证据：滚到该分区再截图（DOM 断言已在 ui-verify-round2 里）。
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const WT = process.env.QIO_UI_WT || 'D:/qio-dev/pm-qio032-integration';
const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
const OUT = resolve(process.env.QIO_UI_OUT || '.');
const SHOTS = join(OUT, 'shots');
mkdirSync(SHOTS, { recursive: true });
const cdp = await import(pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href);
const { CdpBrowser, Page, sleep } = cdp;
const browser = await CdpBrowser.launch({ port: Number(process.env.QIO_UI_CDP_PORT || '9333') });
const page = await Page.create(browser);
try {
  await page.setViewport(1440, 900);
  await page.goto(BASE + '/?probe=' + Date.now());
  await sleep(2500);
  const opened = await page.eval("(function(){ const b=[...document.querySelectorAll('button')].find(function(x){ const l=x.getAttribute('aria-label')||''; return l.indexOf('设置')===0; }); if(!b) return false; b.click(); return true; })()");
  await sleep(2000);
  await page.click('[data-settings-nav-tab="models"]');
  await sleep(1200);
  const scrolled = await page.eval("(function(){ const el=document.querySelector('[data-settings-key=\"models.search.status\"]'); if(!el) return null; el.scrollIntoView({block:'center'}); return el.getBoundingClientRect().top; })()");
  await sleep(900);
  await page.screenshot(join(SHOTS, '20-c1-search-channels-scrolled.png'));
  const facts = await page.eval("(function(){ const rows=[...document.querySelectorAll('[data-search-channel]')].map(function(r){ const c=r.querySelector('[data-channel-config]'); const k=r.querySelector('[data-channel-check]'); return { id:r.getAttribute('data-search-channel'), config:c?(c.innerText||'').trim():null, check:k?(k.innerText||'').trim():null, state:k?k.getAttribute('data-channel-check-state'):null }; }); return { rows: rows, scrolledTop: " + JSON.stringify(scrolled) + " }; })()");
  writeFileSync(join(OUT, 'probe-c1.json'), JSON.stringify({ opened, facts }, null, 2), 'utf8');
  console.log(JSON.stringify({ opened, facts }));
} finally {
  await browser.close();
}
