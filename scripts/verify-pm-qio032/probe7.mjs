import { CdpBrowser, Page, sleep } from "./cdp.mjs";
const b = await CdpBrowser.launch({ port: 9339 });
const p = await Page.create(b); await p.setViewport(1440,900);
await p.goto("http://127.0.0.1:5199/"); await p.waitFor("document.querySelector('#composer-mode-switch .mode-btn')", {timeout:40000});
await p.click("#composer-mode-switch .mode-btn"); await p.waitFor("document.querySelector('[data-im=\"board\"]')", {timeout:30000}); await sleep(1200);
for (const w of [1140,1150,1158,1159,1160,1161,1162,1170]) {
  await p.setViewport(w,700); await sleep(500);
  const info = await p.eval("(function(){const el=document.querySelector('.interactive'); const r=el?el.getBoundingClientRect():null; const cs=el?getComputedStyle(el):null; return {inner:innerWidth, rootW:r?Math.round(r.width):null, padL:cs?cs.paddingLeft:null, padR:cs?cs.paddingRight:null, space:el?el.getAttribute('data-im-panel-space'):null};})()");
  console.log(w, JSON.stringify(info));
}
await p.setViewport(1161,700); await sleep(600);
await p.click('[data-im="submit-details"]'); await sleep(500);
await p.click('[data-im="tasks-entry"]'); await sleep(600);
console.log("both:", JSON.stringify(await p.eval("(function(){const d=document.querySelector('[data-im=\"submit-details-box\"]'); const t=document.querySelector('[data-im=\"task-center\"]'); const rect=e=>e?{x:Math.round(e.getBoundingClientRect().x),y:Math.round(e.getBoundingClientRect().y),w:Math.round(e.getBoundingClientRect().width),h:Math.round(e.getBoundingClientRect().height)}:null; return {space:document.querySelector('.interactive').getAttribute('data-im-panel-space'),details:rect(d),task:rect(t)};})()")));
await p.screenshot("D:/qio-dev/pm032-interactive/_lead-logs/pm-qio032/e2e/shots/probe7-1161.png");
await b.close();
