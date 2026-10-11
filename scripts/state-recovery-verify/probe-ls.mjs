/** 排查：profile 里 localStorage 的删除在「正常关闭 → 重开」后是否还在。 */
import { Browser } from "./cdp.mjs";

const b1 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b1.launch();
await b1.navigate("http://127.0.0.1:5433/?probe-ls=1#/interactive", 3000);
await b1.evalJs("(function () { localStorage.setItem('sr-probe-keep', 'KEEP'); localStorage.setItem('sr-probe-drop', 'DROP'); return 'set'; })()");
await b1.evalJs("(function () { localStorage.removeItem('sr-probe-drop'); return 'removed'; })()");
const before = await b1.evalJs("(function () { return [localStorage.getItem('sr-probe-keep'), localStorage.getItem('sr-probe-drop')]; })()");
console.log("before close:", JSON.stringify(before));
await b1.closeGraceful();
const gone = await b1.waitGone();
console.log("gone:", gone);

const b2 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b2.launch();
await b2.navigate("http://127.0.0.1:5433/?probe-ls=2#/interactive", 3000);
const after = await b2.evalJs("(function () { return [localStorage.getItem('sr-probe-keep'), localStorage.getItem('sr-probe-drop')]; })()");
console.log("after reopen:", JSON.stringify(after));
await b2.closeGraceful();
