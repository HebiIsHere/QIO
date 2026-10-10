/** 排查用探针：直接按真实 API 走一遍「铺材料 → 生成演示意图 → 批准 → 推进完成」，打印真实响应。 */
const B = "http://127.0.0.1:8933";
const BOARD = "board_default";
const api = async (p, init) => {
  const r = await fetch(B + p, { ...(init || {}), headers: { "Content-Type": "application/json" } });
  const t = await r.text();
  try { return { status: r.status, body: JSON.parse(t) }; } catch { return { status: r.status, body: t }; }
};
const card = (id, kind, content, meta) => ({ id, kind, x: 40, y: 40, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false, hidden: false, checked: false, bookmarked: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() });
const st = await api("/api/interactive/boards/" + BOARD + "/state");
const seq = Number(st.body.seq || 0);
const put = await api("/api/interactive/boards/" + BOARD + "/state", { method: "PUT", body: JSON.stringify({ state: { boardId: BOARD, seq, updatedAt: new Date().toISOString(), cards: [card("m1", "file", "材料一", { name: "a.pdf" }), card("m2", "file", "材料二", { name: "b.pdf" }), { ...card("n1", "text", "我的注释"), checked: true }], groups: [], links: [], selection: [] }, reason: "probe-intents" }) });
console.log("PUT", put.status, put.body && put.body.seq);
const created = await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
console.log("CREATE", created.status, (created.body.created || []).map((i) => i.title + ":" + i.status).join(" | "));
const list = await api("/api/interactive/boards/" + BOARD + "/intents");
console.log("LIST", (list.body.intents || []).map((i) => i.title.slice(0, 24) + ":" + i.status).join(" | "));
const sep = (list.body.intents || []).find((i) => i.title.indexOf("保持材料分开") >= 0);
const ap = await api("/api/interactive/intents/" + sep.id + "/approve", { method: "POST", body: "{}" });
console.log("APPROVE", JSON.stringify(ap.body).slice(0, 400));
const done = await api("/api/interactive/intents/" + sep.id + "/demo/advance", { method: "POST", body: JSON.stringify({ outcome: "done" }) });
console.log("DONE", JSON.stringify(done.body).slice(0, 700));
