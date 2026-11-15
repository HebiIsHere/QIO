process.env.QIO_UI_WT = process.env.QIO_UI_WT || 'D:/qio-dev/q46-r8';
const H = await import('../../verify/lib.mjs');
import { mkdirSync } from 'node:fs';
const OUT = process.env.QIO_UI_OUT + '\\debug';
mkdirSync(OUT, { recursive: true });
const ctx = await H.setup({ outDir: OUT, mode: 'debug', cdpPort: 9334 });
ctx.log('base=' + H.BASE + ' api=' + H.API);
await ctx.gotoApp('debug=' + Date.now());
await H.sleep(1500);
const info = await ctx.page.eval(`(function(){
  const sw = document.querySelector('#composer-mode-switch');
  const btns = sw ? [...sw.querySelectorAll('button')].map(function(b){ return { text:(b.innerText||'').trim().slice(0,40), cls:b.className, disabled:!!b.disabled, aria:b.getAttribute('aria-label') }; }) : null;
  return { url: location.href, modeBtns: btns, hasBoard: !!document.querySelector('[data-im=board]'), streamText: (document.querySelector('.stream')?.innerText||'').slice(0,300), bodyText: (document.body.innerText||'').slice(0,600), notConnected: !!document.querySelector('[data-im=not-connected]') };
})()`);
ctx.log(JSON.stringify(info, null, 2));
await ctx.shot('debug-initial');
// 尝试点击模式按钮并再观察
try { await ctx.page.click('#composer-mode-switch .mode-btn', 0, { timeout: 10000 }); } catch (e) { ctx.log('click err ' + e.message); }
await H.sleep(2500);
const info2 = await ctx.page.eval(`(function(){ return { hasBoard: !!document.querySelector('[data-im=board]'), bodyText: (document.body.innerText||'').slice(0,800), url: location.href }; })()`);
ctx.log(JSON.stringify(info2, null, 2));
await ctx.shot('debug-after-click');
await ctx.finish();
