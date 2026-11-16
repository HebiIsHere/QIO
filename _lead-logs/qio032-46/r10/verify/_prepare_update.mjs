const mod = await import("file:///D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify/lib-r9.mjs");
const port = Number(process.env.QIO_CDP_PORT || 9223);
try {
  const page = await mod.connectApp({ port });
  const r = await page.eval("window.__TAURI_INTERNALS__.invoke('qio_prepare_for_update')");
  console.log("PREPARE=" + JSON.stringify(r));
  if (r && r.stopped === true && r.verified === true) console.log("PREPARE_OK");
} catch (e) { console.log("PREPARE_ERR=" + String(e).slice(0, 300)); }
process.exit(0);
