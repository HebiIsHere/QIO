import { CdpBrowser, Page, sleep } from "./cdp.mjs";
const b = await CdpBrowser.launch({ port: 9335 });
const p = await Page.create(b);
await p.setViewport(1440, 900);
await p.goto("http://127.0.0.1:5199/");
await p.waitFor("document.querySelector('#composer-mode-switch .mode-btn')", { timeout: 40000 });
console.log("mode attr:", await p.attr("#composer-mode-switch .mode-btn","data-current-mode"));
await p.click("#composer-mode-switch .mode-btn");
await p.waitFor("document.querySelector('[data-im=\"board\"]')", { timeout: 30000 });
await sleep(1200);
console.log("panel-space@1440:", await p.attr(".interactive","data-im-panel-space"));
await p.setViewport(800,600); await sleep(1000);
console.log("panel-space@800:", await p.attr(".interactive","data-im-panel-space"));
const dump = async (tag) => {
  const s = await p.eval("(function(){ const g=(sel)=>[...document.querySelectorAll(sel)].map(el=>{const r=el.getBoundingClientRect();return {im:el.getAttribute('data-im'),text:(el.innerText||'').replace(/\s+/g,' ').slice(0,80),x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),vis:r.width>0&&r.height>0}}); return {details:g('[data-im=\"submit-details\"]'),detailsBox:g('[data-im=\"submit-details-box\"]'),process:g('[data-im=\"submit-process-panel\"]'),result:g('[data-im=\"submit-result-panel\"]'),taskEntry:g('[data-im=\"tasks-entry\"]'),task:g('[data-im=\"task-center\"]'),taskClose:g('[data-im=\"task-close\"]'),submit:g('[data-im=\"submit\"]'),toolbar:g('[data-im=\"board-toolbar\"]')}; })()");
  console.log(tag, JSON.stringify(s));
};
await dump("closed");
await p.click('[data-im="submit-details"]').catch(e=>console.log("details click err",e.message));
await sleep(600); await dump("details-open");
if (await p.exists('[data-im="tasks-entry"]')) { await p.click('[data-im="tasks-entry"]').catch(e=>console.log("task click err",e.message)); await sleep(700); await dump("tasks-open"); }
console.log("task center text:", (await p.text('[data-im="task-center"]')).slice(0,300).replace(/\n/g," | "));
await p.screenshot("D:/qio-dev/pm032-interactive/_lead-logs/pm-qio032/e2e/shots/probe3-800.png");
await p.setViewport(1120,700); await sleep(700); console.log("panel-space@1120:", await p.attr(".interactive","data-im-panel-space"));
await p.setViewport(1121,700); await sleep(700); console.log("panel-space@1121:", await p.attr(".interactive","data-im-panel-space"));
await b.close();
