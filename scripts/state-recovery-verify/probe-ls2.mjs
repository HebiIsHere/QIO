/** 排查二：写入/删除后等待更久再正常关闭，localStorage 是否能落盘。 */
import { Browser } from "./cdp.mjs";
import { setTimeout as sleep } from "node:timers/promises";

const b1 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b1.launch();
await b1.navigate("http://127.0.0.1:5433/?probe-ls2=1#/interactive", 3000);
await b1.evalJs("(function () { localStorage.setItem('sr-probe-keep2', 'KEEP2'); localStorage.setItem('sr-probe-drop2', 'DROP2'); return 'set'; })()");
await sleep(1000);
await b1.evalJs("(function () { localStorage.removeItem('sr-probe-drop2'); return 'removed'; })()");
console.log("before close:", JSON.stringify(await b1.evalJs("(function () { return [localStorage.getItem('sr-probe-keep2'), localStorage.getItem('sr-probe-drop2')]; })()")));
// 1) 关掉标签页 2) 等 12 秒 3) 正常关闭浏览器
try {
  const targets = await (await fetch("http://127.0.0.1:" + b1.port + "/json/list")).json();
  for (const t of targets) {
    if (t.type === "page" && t.url.indexOf("probe-ls2") >= 0) {
      await fetch("http://127.0.0.1:" + b1.port + "/json/close/" + t.id);
      console.log("closed tab");
    }
  }
} catch (e) {
  console.log("close tab failed", e.message);
}
await sleep(12000);
await b1.closeGraceful();
console.log("gone:", await b1.waitGone());

const b2 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b2.launch();
await b2.navigate("http://127.0.0.1:5433/?probe-ls2=2#/interactive", 3000);
console.log("after reopen:", JSON.stringify(await b2.evalJs("(function () { return [localStorage.getItem('sr-probe-keep2'), localStorage.getItem('sr-probe-drop2')]; })()")));
await b2.closeGraceful();
