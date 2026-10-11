/** 排查三：写入后等待 30 秒再正常关闭，localStorage 是否落盘；同时检查 profile 里的 leveldb 文件。 */
import { Browser } from "./cdp.mjs";
import { setTimeout as sleep } from "node:timers/promises";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const profile = process.env.QIO_SR_PROFILE || join(process.env.TEMP || ".", "qio-sr-edge");
const b1 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b1.launch();
await b1.navigate("http://127.0.0.1:5433/?probe-ls3=1#/interactive", 3000);
await b1.evalJs("(function () { localStorage.setItem('sr-probe-keep3', 'KEEP3'); return 'set'; })()");
console.log("waiting 30s for leveldb commit ...");
await sleep(30000);
const dir = join(profile, "Default", "Local Storage", "leveldb");
if (existsSync(dir)) {
  console.log("leveldb files:", readdirSync(dir).map((f) => f + ":" + statSync(join(dir, f)).size).join(" | "));
} else {
  console.log("no leveldb dir:", dir);
}
await b1.closeGraceful();
console.log("gone:", await b1.waitGone());
const b2 = new Browser({ outDir: process.env.TEMP + "\\qio-sr-shots" });
await b2.launch();
await b2.navigate("http://127.0.0.1:5433/?probe-ls3=2#/interactive", 3000);
console.log("after reopen:", JSON.stringify(await b2.evalJs("localStorage.getItem('sr-probe-keep3')")));
console.log("origin keys:", JSON.stringify(await b2.evalJs("Object.keys(localStorage).slice(0, 10)")));
await b2.closeGraceful();
