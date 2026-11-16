/**
 * QIO #46 r9 真实 QIO 窗口验收（安装版 WebView2 + 真实隔离后端 + fake provider）。
 * 覆盖：任务 2（旧标记清理 / 正常清稿 / 未同步草稿）、任务 3（提交回读归发起板）、
 * 任务 4（撤回重试只在服务器事实达到 expectSeq 后成功）。
 * 只通过 store 公开动作与真实后端 API 驱动；可控时序用 CDP Fetch 闸门（延迟 / 失败 / 自定义回执）。
 */
const { connectApp, backendClient, sleep } = await import("file:///D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify/lib-r9.mjs");
const OUT = process.env.QIO_OUT || "D:/qio-dev/_win-r9/board";
const CDP_PORT = Number(process.env.QIO_CDP_PORT || 9223);
const API_PORT = Number(process.env.QIO_API_PORT || 0);
const TOKEN = process.env.QIO_API_TOKEN || "";
const results = [];
function rec(id, title, state, detail) { results.push({ id, title, state, detail }); console.log("[" + id + "] " + state + " " + title + " — " + detail); }
const api = backendClient({ port: API_PORT, token: TOKEN });

async function waitStoreReady(page) {
  for (let i = 0; i < 200; i += 1) {
    try { const st = await page.storeState(); if (st && st.boardId) return st; } catch (e) { /* store 未就绪 */ }
    await sleep(250);
  }
  throw new Error("互动 store 未就绪");
}
async function selectBoard(page, boardId) {
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardId) + "); return true;");
  for (let i = 0; i < 160; i += 1) {
    const ok = await page.storeEval("return s.boardId === " + JSON.stringify(boardId) + " && s.loading === false && !!s.board;");
    if (ok) return;
    await sleep(250);
  }
  throw new Error("板面未加载完成：" + boardId);
}
async function seedBoard(page, boardId, cardId, text) {
  await api.post("/api/interactive/boards", { boardId, title: boardId });
  await selectBoard(page, boardId);
  const expr = "const now = new Date().toISOString();" +
    " const seq = Number(s.board && s.board.seq || 0);" +
    " const state = { boardId: " + JSON.stringify(boardId) + ", seq: seq, updatedAt: now," +
    "   cards: [{ id: " + JSON.stringify(cardId) + ", kind: \"text\", content: " + JSON.stringify(text) + ", meta: {}," +
    "     x: 0, y: 0, w: 200, h: 120, checked: false, hidden: false, folded: false, bookmarked: false, deleted: false," +
    "     createdAt: now, updatedAt: now }]," +
    "   groups: [], links: [], selection: [] };" +
    " s.commit(state, \"r9 seed\"); await s.saveNow();" +
    " return { status: s.saveStatus, dirty: s.dirty, seq: s.board && s.board.seq };";
  const saved = await page.storeEval(expr);
  for (let i = 0; i < 80 && ((await page.storeEval("return s.saveStatus;")) !== "saved"); i += 1) await sleep(200);
  return saved;
}
async function forceSave(page, content) {
  await page.storeEval("const card = s.board.cards.find(function(c){return c.id === \"card_b\";});" +
    " const next = JSON.parse(JSON.stringify(s.board));" +
    " next.cards = next.cards.map(function(c){ return c.id === \"card_b\" ? Object.assign({}, c, { content: " + JSON.stringify(content) + " }) : c; });" +
    " s.commit(next, \"r9 save\"); await s.saveNow(); return s.saveStatus;");
  for (let i = 0; i < 100; i += 1) {
    const st = await page.storeEval("return s.saveStatus;");
    if (st === "saved") return true;
    if (st === "error") throw new Error("保存失败：" + (await page.storeEval("return s.saveError;")));
    await sleep(200);
  }
  throw new Error("保存未收敛");
}
function decodeBody(payload) { return payload && payload.board ? payload : null; }

async function main() {
  const page = await connectApp({ port: CDP_PORT });
  await waitStoreReady(page);
  const boardA = "r9_board_a";
  const boardB = "r9_board_b";
  const cardB = "card_b";
  const keyB = "card:card_b";
  const formalB = "B 的正式正文（种子）";

  // ---------- 预置两块板（真实后端） ----------
  await seedBoard(page, boardA, "card_a", "A 的正式正文");
  await seedBoard(page, boardB, cardB, formalB);
  rec("ENV", "真实 QIO 窗口 + 隔离后端 + fake provider", "INFO", "api=" + API_PORT + " cdp=" + CDP_PORT);

  // ---------- B1 任务2：旧标记 + 真实未同步草稿 + 正式内容 ----------
  await selectBoard(page, boardB);
  const draftText = "真实未同步草稿-r9";
  await page.storeEval("s.setCardDraftInput(" + JSON.stringify(cardB) + ", { text: " + JSON.stringify(draftText) + " }); return s.drafts[" + JSON.stringify(keyB) + "];");
  const markerText = "旧标记里的旧正文-r9-不得恢复";
  await page.localStorageSet("qio.draft.sync-marker.v1", JSON.stringify({ [boardB]: { [keyB]: { version: 9, text: markerText } } }));
  const beforeBackend = await api.get("/api/interactive/boards/" + boardB + "/state");
  await page.reload();
  await waitStoreReady(page);
  const markerAfter = await page.localStorageGet("qio.draft.sync-marker.v1");
  await selectBoard(page, boardB);
  const draftAfter = await page.storeEval("return s.drafts[" + JSON.stringify(keyB) + "] || null;");
  const contentAfter = await page.storeEval("const c = (s.board.cards || []).find(function(x){return x.id === " + JSON.stringify(cardB) + ";}); return c ? c.content : null;");
  const backendAfter = await api.get("/api/interactive/boards/" + boardB + "/state");
  const okB1 = markerAfter === null && draftAfter === draftText && contentAfter === formalB &&
    backendAfter.data.state.cards.find(function(c){return c.id === cardB;}).content === formalB;
  rec("B1", "旧标记清理：仅标记消失，真实未同步草稿与正式内容都保留", okB1 ? "PASS" : "FAIL",
    "marker=" + JSON.stringify(markerAfter) + " draft=" + JSON.stringify(draftAfter) + " content=" + JSON.stringify(contentAfter) + " backendDraftHasText=" + JSON.stringify(JSON.stringify(backendAfter.data.drafts).includes(draftText)));

  // ---------- B2 任务2：正常完成编辑清旧稿（真实后端） ----------
  const formal2 = "B 的正式正文（完成编辑后）";
  const finished = await page.storeEval("const next = JSON.parse(JSON.stringify(s.board));" +
    " next.cards = next.cards.map(function(c){ return c.id === " + JSON.stringify(cardB) + " ? Object.assign({}, c, { content: " + JSON.stringify(formal2) + " }) : c; });" +
    " s.commit(next, \"完成编辑\"); s.requestDraftClear(" + JSON.stringify(keyB) + "); await s.saveNow(); await s.flushDrafts(); return true;");
  let saveState = null;
  for (let i = 0; i < 100; i += 1) { saveState = await page.storeEval("return { status: s.saveStatus, dirty: s.dirty, removal: s.draftRemovalStateFor ? s.draftRemovalStateFor(" + JSON.stringify(keyB) + ") : null, drafts: s.drafts[" + JSON.stringify(keyB) + "] || null };"); if (saveState.status === "saved" && !saveState.dirty) break; await sleep(200); }
  await sleep(1000);
  const backendB2 = await api.get("/api/interactive/boards/" + boardB + "/state");
  const backendDraftKeys = Object.keys((backendB2.data.drafts && backendB2.data.drafts.drafts) || {});
  const localRec = await page.eval("localStorage.getItem(" + JSON.stringify("qio.card-local:card_b") + ")");
  const okB2 = backendB2.data.state.cards.find(function(c){return c.id === cardB;}).content === formal2 && !backendDraftKeys.includes(keyB) && (saveState.drafts === null || saveState.drafts === "");
  rec("B2", "正常完成编辑：服务端正式内容落库、旧草稿被清、界面不再显示旧稿", okB2 ? "PASS" : "FAIL",
    "content=" + JSON.stringify(backendB2.data.state.cards.find(function(c){return c.id === cardB;}).content) + " draftKeys=" + JSON.stringify(backendDraftKeys) + " storeDraft=" + JSON.stringify(saveState && saveState.drafts) + " localRec=" + String(localRec).slice(0, 80));

  // ---------- B3 任务3：B 提交后回读在 A 显示时失败 ----------
  await page.enableFetch([{ urlPattern: "*api/interactive/boards*" }]);
  const bStateGet = page.addRule({ once: true, hold: true, match: function(url, method) { return method === "GET" && url.indexOf("/boards/" + boardB + "/state") >= 0; } });
  await page.storeEval("s.submit().then(function(r){ window.__r9submit = r; }).catch(function(e){ window.__r9submitError = String(e); }); return true;");
  for (let i = 0; i < 120 && !bStateGet.heldRequestId; i += 1) await sleep(100);
  const heldB = !!bStateGet.heldRequestId;
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardA) + "); return s.boardId;");
  for (let i = 0; i < 80 && (await page.storeEval("return s.loading;")); i += 1) await sleep(150);
  if (bStateGet.heldRequestId) await page.releaseHeld(bStateGet, { type: "fail", errorReason: "Failed" });
  await sleep(1500);
  const aState = await page.storeState();
  const aSubmissions = await api.get("/api/interactive/boards/" + boardA + "/submissions");
  const bSubmissions = await api.get("/api/interactive/boards/" + boardB + "/submissions");
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardB) + "); return s.boardId;");
  for (let i = 0; i < 100; i += 1) { const v = await page.storeEval("return s.submitRefreshState;"); if (v !== "pending") break; await sleep(150); }
  const bState = await page.storeState();
  const okB3 = heldB && aState.submitRefreshState === "idle" && aState.boardId === boardA &&
    bState.submitRefreshState === "error" && typeof bState.submitRefreshError === "string" && bState.submitRefreshError.length > 0 &&
    bSubmissions.data.submissions.length === 1 && aSubmissions.data.submissions.length === 0;
  rec("B3", "B 的提交后回读在 A 显示时失败：错误只归 B，A 保持 idle；只有 B 一次受理", okB3 ? "PASS" : "FAIL",
    "held=" + heldB + " A=" + JSON.stringify({ id: aState.boardId, refresh: aState.submitRefreshState }) + " B=" + JSON.stringify({ refresh: bState.submitRefreshState, err: String(bState.submitRefreshError).slice(0, 90) }) + " submissions A/B=" + aSubmissions.data.submissions.length + "/" + bSubmissions.data.submissions.length);

  // ---------- B4 任务3：在 B 点重试刷新后立刻切 A ----------
  const bRetryGet = page.addRule({ once: true, hold: true, match: function(url, method) { return method === "GET" && url.indexOf("/boards/" + boardB + "/state") >= 0; } });
  await page.storeEval("s.retrySubmitRefresh().then(function(r){ window.__r9retry = r; }).catch(function(e){ window.__r9retryError = String(e); }); return true;");
  for (let i = 0; i < 120 && !bRetryGet.heldRequestId; i += 1) await sleep(100);
  const heldRetry = !!bRetryGet.heldRequestId;
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardA) + "); return s.boardId;");
  for (let i = 0; i < 80 && (await page.storeEval("return s.loading;")); i += 1) await sleep(150);
  if (bRetryGet.heldRequestId) await page.releaseHeld(bRetryGet, { type: "continue" });
  await sleep(2000);
  const aAfterRetry = await page.storeState();
  const retryResult = await page.eval("window.__r9retry || null");
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardB) + "); return s.boardId;");
  let bAfterRetry = null;
  for (let i = 0; i < 120; i += 1) { bAfterRetry = await page.storeState(); if (bAfterRetry.submitRefreshState !== "pending") break; await sleep(200); }
  const okB4 = heldRetry && aAfterRetry.boardId === boardA && aAfterRetry.submitRefreshState === "idle" && aAfterRetry.submitRefreshError === null && bAfterRetry.submitRefreshState === "idle";
  rec("B4", "B 点重试刷新后立刻切 A：不在 A 写状态/发读取，回 B 自动收尾", okB4 ? "PASS" : "FAIL",
    "held=" + heldRetry + " retry=" + JSON.stringify(retryResult) + " A=" + JSON.stringify({ id: aAfterRetry.boardId, refresh: aAfterRetry.submitRefreshState, err: aAfterRetry.submitRefreshError }) + " B=" + JSON.stringify({ refresh: bAfterRetry.submitRefreshState }));

  // ---------- B5 任务4：撤回重试只在采纳事实达到 expectSeq 后成功 ----------
  await page.clearRules();
  await page.storeEval("await s.selectBoard(" + JSON.stringify(boardB) + "); return s.boardId;");
  await forceSave(page, "B 的正式正文（撤回前）");
  for (let i = 0; i < 6; i += 1) await forceSave(page, "B 的正式正文（撤回前 v" + (i + 2) + ")");
  const beforeCancel = await page.storeEval("return { seq: s.board && s.board.seq, cleanSeq: null };");
  const cancelRead = page.addRule({ once: true, hold: true, match: function(url, method) { return method === "GET" && url.indexOf("/boards/" + boardB + "/state") >= 0; } });
  await page.storeEval("s.cancelImpact(); return true;");
  for (let i = 0; i < 120 && !cancelRead.heldRequestId; i += 1) await sleep(100);
  const cancelHeld = !!cancelRead.heldRequestId;
  let expectSeq = 0;
  try { expectSeq = Number(await page.storeEval("return (s.cancelRecovery && s.cancelRecovery.expectSeq) || 0;")); } catch (e) { expectSeq = 0; }
  const staleSeq = Math.max(0, expectSeq - 1);
  if (cancelRead.heldRequestId) {
    const payload = {
      board: { id: boardB, title: boardB },
      state: { boardId: boardB, seq: staleSeq, updatedAt: new Date().toISOString(),
        cards: [{ id: cardB, kind: "text", content: "服务器旧事实", meta: {}, x: 0, y: 0, w: 200, h: 120, checked: false, hidden: false, folded: false, bookmarked: false, deleted: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() }],
        groups: [], links: [], selection: [] },
      seq: staleSeq, baseline: null, submissions: [], drafts: { drafts: {}, updatedAt: null }, visibleRange: null,
    };
    await page.releaseHeld(cancelRead, { type: "fulfill", status: 200, body: payload });
  }
  await sleep(1500);
  const afterStale = await page.storeEval("return s.cancelRecovery ? { active: s.cancelRecovery.active, reason: s.cancelRecovery.reason, expectSeq: s.cancelRecovery.expectSeq } : null;");
  const firstRetry = await page.storeEval("return await s.retryCancelRecovery();");
  await sleep(500);
  const afterRetryCancel = await page.storeEval("return s.cancelRecovery ? { active: s.cancelRecovery.active, reason: s.cancelRecovery.reason } : null;");
  const okB5 = cancelHeld && afterStale !== null && afterStale.active === true && firstRetry && firstRetry.ok === true && afterRetryCancel !== null && afterRetryCancel.active === false;
  rec("B5", "撤回重试：读到旧版本不结算；之后读到达到 expectSeq 才报成功并结束等待", okB5 ? "PASS" : "FAIL",
    "held=" + cancelHeld + " expect=" + expectSeq + " staleSeq=" + staleSeq + " afterStale=" + JSON.stringify(afterStale) + " retry=" + JSON.stringify(firstRetry) + " afterRetry=" + JSON.stringify(afterRetryCancel));

  // ---------- B6 任务4：重试读到的版本缺失（seq=0）不结算 ----------
  // 先制造「撤回等待中」：第一次回读被注入失败（cancelRecovery active，expectSeq=当前已确认版本）；
  // 再让**重试那一次**回读返回 seq=0（版本缺失）——重试必须返回可重试失败且保持等待。
  await page.clearRules();
  await forceSave(page, "B 的正式正文（第二次撤回前）");
  const cancel2 = page.addRule({ once: true, hold: true, match: function(url, method) { return method === "GET" && url.indexOf("/boards/" + boardB + "/state") >= 0; } });
  await page.storeEval("s.cancelImpact(); return true;");
  for (let i = 0; i < 120 && !cancel2.heldRequestId; i += 1) await sleep(100);
  if (cancel2.heldRequestId) await page.releaseHeld(cancel2, { type: "fail", errorReason: "Failed" });
  await sleep(1200);
  const waiting = await page.storeEval("return s.cancelRecovery ? { active: s.cancelRecovery.active, expectSeq: s.cancelRecovery.expectSeq } : null;");
  const retryGate = page.addRule({ once: true, hold: true, match: function(url, method) { return method === "GET" && url.indexOf("/boards/" + boardB + "/state") >= 0; } });
  await page.storeEval("s.retryCancelRecovery().then(function(r){ window.__r9retry2 = r; }); return true;");
  for (let i = 0; i < 120 && !retryGate.heldRequestId; i += 1) await sleep(100);
  const retryHeld = !!retryGate.heldRequestId;
  if (retryGate.heldRequestId) {
    const payload = { board: { id: boardB, title: boardB }, state: { boardId: boardB, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] }, seq: 0, baseline: null, submissions: [], drafts: { drafts: {}, updatedAt: null }, visibleRange: null };
    await page.releaseHeld(retryGate, { type: "fulfill", status: 200, body: payload });
  }
  await sleep(1500);
  const retry2 = await page.eval("window.__r9retry2 || null");
  const after2 = await page.storeEval("return s.cancelRecovery ? { active: s.cancelRecovery.active, reason: s.cancelRecovery.reason } : null;");
  const okB6 = retryHeld && waiting !== null && waiting.active === true && waiting.expectSeq > 0 && retry2 && retry2.ok === false && after2 !== null && after2.active === true;
  rec("B6", "重试读到版本缺失（seq=0）不结算撤回，仍保留「重新核对」", okB6 ? "PASS" : "FAIL",
    "waiting=" + JSON.stringify(waiting) + " retryHeld=" + retryHeld + " retry=" + JSON.stringify(retry2) + " after=" + JSON.stringify(after2));

  const outFile = OUT + "/results.json";
  const fs = await import("node:fs");
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(outFile, JSON.stringify({ results, generatedAt: new Date().toISOString() }, null, 2), "utf8");
  const failed = results.filter(function(r){ return r.state === "FAIL"; });
  console.log("SUMMARY pass=" + results.filter(function(r){ return r.state === "PASS"; }).length + " fail=" + failed.length + " total=" + results.length);
  process.exit(failed.length ? 1 : 0);
}

main().catch(async function(err) {
  rec("RUN", "场景执行", "FAIL", String(err && err.stack || err).slice(0, 1200));
  try { const fs = await import("node:fs"); fs.mkdirSync(OUT, { recursive: true }); fs.writeFileSync(OUT + "/results.json", JSON.stringify({ results, generatedAt: new Date().toISOString() }, null, 2), "utf8"); } catch (e) {}
  process.exit(1);
});