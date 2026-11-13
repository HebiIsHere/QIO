import { CdpBrowser, Page, sleep } from "./cdp.mjs";
const b = await CdpBrowser.launch({ port: 9334 });
const p = await Page.create(b);
await p.setViewport(1440, 900);
const logs = [];
b.on((m) => { const q=m.params||{}; if (m.method==="Network.loadingFailed") logs.push("LOADFAIL " + q.errorText + " " + q.requestId); if (m.method==="Runtime.exceptionThrown") logs.push("EXC " + JSON.stringify(q.exceptionDetails).slice(0,300)); });
await b.send("Network.enable", {}, p.sessionId);
try { await p.goto("http://127.0.0.1:5199/"); } catch(e) { console.log("goto err", e.message); }
await sleep(6000);
console.log("title:", await p.eval("document.title"));
console.log("body:", (await p.eval("document.body.innerText")).slice(0,500).replace(/\n/g," | "));
console.log("fetch health:", JSON.stringify(await p.eval("fetch('http://127.0.0.1:8734/api/health').then(r=>r.text()).then(t=>t.slice(0,200)).catch(e=>'ERR '+e.message)")));
console.log("fetch bootstrap:", JSON.stringify(await p.eval("fetch('http://127.0.0.1:8734/api/bootstrap').then(r=>r.status+' '+r.statusText).catch(e=>'ERR '+e.message)")));
console.log("fetch interactive:", JSON.stringify(await p.eval("fetch('http://127.0.0.1:8734/api/interactive/board').then(r=>r.status).catch(e=>'ERR '+e.message)")));
console.log("logs:", logs.slice(0,20));
await p.screenshot("D:/qio-dev/pm032-interactive/_lead-logs/pm-qio032/e2e/shots/probe2.png");
await b.close();
