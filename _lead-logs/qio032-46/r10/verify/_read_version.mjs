const mod = await import("file:///D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify/lib-r9.mjs");
const port = Number(process.env.QIO_CDP_PORT || 9223);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let page = null;
for (let i = 0; i < 30 && !page; i += 1) {
  try { page = await mod.connectApp({ port }); } catch (e) { await sleep(400); }
}
if (!page) { console.log("VERSION_ERR=connect failed"); process.exit(0); }
for (let i = 0; i < 30; i += 1) {
  try {
    const v = await page.eval("window.__TAURI_INTERNALS__.invoke('plugin:app|version')");
    if (v) { console.log("VERSION=" + String(v)); process.exit(0); }
  } catch (e) { /* internals 未就绪 */ }
  await sleep(400);
}
console.log("VERSION_ERR=timeout");
process.exit(0);
