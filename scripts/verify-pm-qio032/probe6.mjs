import { CdpBrowser, Page, sleep } from "./cdp.mjs";
const b = await CdpBrowser.launch({ port: 9338 });
const p = await Page.create(b); await p.setViewport(1440,900);
await p.goto("http://127.0.0.1:5199/"); await p.waitFor("document.querySelector('#composer-mode-switch .mode-btn')", {timeout:40000});
await p.click("#composer-mode-switch .mode-btn"); await p.waitFor("document.querySelector('[data-im=\"board\"]')", {timeout:30000}); await sleep(1200);
const st = async (tag) => console.log(tag, JSON.stringify(await p.eval("(function(){const q=(s)=>document.querySelectorAll(s).length; const d=document.querySelector('[data-im=\"submit-details\"]'); const box=document.querySelector('[data-im=\"submit-details-box\"]'); const proc=document.querySelector('[data-im=\"submit-process-panel\"]'); const res=document.querySelector('[data-im=\"submit-result-panel\"]'); const t=document.querySelector('[data-im=\"task-center\"]'); const space=document.querySelector('.interactive'); return {expand:d&&d.getAttribute('aria-expanded'), details:!!box, process:!!proc, result:!!res, task:!!t, space:space&&space.getAttribute('data-im-panel-space'), panelText: box?(box.innerText||'').slice(0,60):null};})()")));
await p.setViewport(800,600); await sleep(800); await st("800-before");
await p.click('[data-im="submit-details"]'); await sleep(800); await st("800-after-details");
console.log("details box rect:", JSON.stringify(await p.box('[data-im="submit-details-box"]').catch(e=>e.message)));
if (await p.exists('[data-im="tasks-entry"]')) { await p.click('[data-im="tasks-entry"]'); await sleep(800); await st("800-after-tasks"); }
await p.keyboard; // noop
await p.pressKey("Escape"); await sleep(500); await st("800-after-esc");
await p.setViewport(1121,700); await sleep(800); await st("1121-before");
await p.click('[data-im="submit-details"]'); await sleep(600); await st("1121-after-details");
await p.click('[data-im="tasks-entry"]'); await sleep(600); await st("1121-after-tasks");
console.log("details rect:", JSON.stringify(await p.box('[data-im="submit-details-box"]').catch(e=>e.message)));
console.log("task rect:", JSON.stringify(await p.box('[data-im="task-center"]').catch(e=>e.message)));
await p.setViewport(1120,700); await sleep(600); await st("1120");
await p.screenshot("D:/qio-dev/pm032-interactive/_lead-logs/pm-qio032/e2e/shots/probe6-1120.png");
await b.close();
