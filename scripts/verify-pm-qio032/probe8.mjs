import { CdpBrowser, Page, sleep } from "./cdp.mjs";
const b = await CdpBrowser.launch({ port: 9340 });
const p = await Page.create(b); await p.setViewport(1440,900);
await p.goto("http://127.0.0.1:5199/"); await p.waitFor("document.querySelector('#composer-mode-switch .mode-btn')", {timeout:40000});
await p.click("#composer-mode-switch .mode-btn"); await p.waitFor("document.querySelector('[data-im=\"board\"]')", {timeout:30000}); await sleep(1200);
for (const w of [1176,1177]) { await p.setViewport(w,700); await sleep(500); console.log(w, await p.eval("(function(){const el=document.querySelector('.interactive'); return {inner:innerWidth,space:el.getAttribute('data-im-panel-space')};})()")); }
await p.setViewport(1177,700); await sleep(700); await p.click('[data-im="submit-details"]'); await sleep(400); await p.click('[data-im="tasks-entry"]'); await sleep(600);
console.log("both1177:", JSON.stringify(await p.eval("(function(){const rect=e=>e?{x:Math.round(e.getBoundingClientRect().x),y:Math.round(e.getBoundingClientRect().y),w:Math.round(e.getBoundingClientRect().width),h:Math.round(e.getBoundingClientRect().height)}:null; return {space:document.querySelector('.interactive').getAttribute('data-im-panel-space'),details:rect(document.querySelector('[data-im=\"submit-details-box\"]')),task:rect(document.querySelector('[data-im=\"task-center\"]')),submit:rect(document.querySelector('[data-im=\"submit\"]'))};})()")));
await p.screenshot("D:/qio-dev/pm032-interactive/_lead-logs/pm-qio032/e2e/shots/probe8-1177.png");
await b.close();
