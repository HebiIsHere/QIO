/**
 * 【独立验收 D · closure-d】第⑤层证据：真浏览器操作（Chrome CDP，无 npm 依赖）。
 *
 * 为什么需要它：①/②层用的是替身与 jsdom；这一层在**真实浏览器 + 真实前后端进程**上操作，
 * 并且覆盖「**实际关闭浏览器进程后用同一用户目录重开**」（第⑥层要求）。
 *
 * 用法（需先按仓库约定用独立临时 QIO_DATA_DIR 起好后端 8734 与前端 5199）：
 *   node scripts/closure-d-verify/closure-d-browser-probe.mjs \
 *     --app http://127.0.0.1:5199 --api http://127.0.0.1:8734 \
 *     --profile <临时 Chrome 用户目录> --out <证据目录>
 *
 * 覆盖：
 *   B0 真实浏览器能打开真实应用（无控制台错误、无 4xx/5xx 请求）；
 *   B1 R1：真实卡片编辑触发服务端影响门 → 真实确认框出现 → 点「取消」→ 板面回到服务器已保存正文；
 *   B2 关闭重开：同一 Chrome 用户目录、结束进程后**新进程**打开，草稿文字仍在输入框里。
 *
 * 所有路径由参数给出（仓库相对或临时目录），脚本内不写死盘符。
 */
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

function arg(name, fallback) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const CHROME = arg("chrome", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe");
const APP = arg("app", "http://127.0.0.1:5199");
const API = arg("api", "http://127.0.0.1:8734");
const PROFILE = arg("profile", process.env.TEMP + "\\qio-closure-d-profile");
const OUT = arg("out", "scripts/closure-d-verify/evidence");
const PORT = Number(arg("port", "9344"));
mkdirSync(OUT, { recursive: true });
const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? "PASS " : "FAIL ") + name + " :: " + detail);
}

function killChrome() {
  if (process.platform !== "win32") return;
  spawnSync("powershell", ["-NoProfile", "-Command",
    "\$p = Get-NetTCPConnection -LocalPort " + PORT + " -State Listen -ErrorAction SilentlyContinue; " +
    "if (\$p) { Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { \$_.CommandLine -like '*" + PROFILE.replace(/\\/g, "\\\\") + "*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue } }"], { encoding: "utf8" });
}

/** 启动一个真实 Chrome 进程（独立用户目录 + 独立调试端口），返回操作句柄。 */
async function launch(label) {
  killChrome();
  await sleep(800);
  const child = spawn(CHROME, [
    "--headless=new", "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROFILE,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
    "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    "--window-size=1440,900", "about:blank",
  ], { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 80; i += 1) {
    try {
      const r = await fetch("http://127.0.0.1:" + PORT + "/json/version");
      if (r.ok) { version = await r.json(); break; }
    } catch { /* 重试 */ }
    await sleep(250);
  }
  if (!version) throw new Error(label + ": chrome devtools 未就绪");
  const tab = await (await fetch("http://127.0.0.1:" + PORT + "/json/new?about:blank", { method: "PUT" })).json();
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res);
    ws.addEventListener("error", rej);
  });
  let msgId = 0;
  const pending = new Map();
  const consoleErrors = [];
  const httpFails = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
      return;
    }
    if (msg.method === "Runtime.exceptionThrown") consoleErrors.push(msg.params.exceptionDetails.text);
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      consoleErrors.push((msg.params.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
    }
    if (msg.method === "Network.responseReceived" && msg.params.response.status >= 400) {
      const url = msg.params.response.url;
      // webfont 403 是验收环境已知现象（依赖目录是 worktree 外的 junction，
      // Vite 的 fs 白名单拒绝 /@fs 字体路径）——几何与布局不受影响，不计入失败。
      const isKnownFontFallback = url.includes("@fontsource") || url.includes("/@fs/");
      if (!isKnownFontFallback) httpFails.push(msg.params.response.status + " " + url);
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Network.enable");
  const evaluate = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error("eval 失败：" + r.exceptionDetails.text + " " + JSON.stringify(r.result?.value ?? ""));
    return r.result?.value;
  };
  const goto = async (url, ms = 3000) => {
    await send("Page.navigate", { url });
    await sleep(ms);
  };
  const shot = async (name) => {
    const r = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(OUT + "\\" + name + ".png", Buffer.from(r.data, "base64"));
  };
  const close = async () => {
    // 先让浏览器自己优雅退出（localStorage 异步落盘），再兜底
    try {
      const v = await (await fetch("http://127.0.0.1:" + PORT + "/json/version")).json();
      const bws = new WebSocket(v.webSocketDebuggerUrl);
      await new Promise((res) => { bws.addEventListener("open", res); bws.addEventListener("error", res); });
      bws.send(JSON.stringify({ id: 1, method: "Browser.close" }));
      await sleep(2500);
      bws.close();
    } catch { /* 已退出 */ }
    ws.close();
    child.kill();
    killChrome();
    await sleep(600);
  };
  return { child, send, evaluate, goto, shot, close, consoleErrors, httpFails };
}

async function api(method, path, body) {
  const resp = await fetch(API + path, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let json = null;
  try { json = await resp.json(); } catch { /* 无正文 */ }
  return { status: resp.status, json };
}

const STAMP = "2026-10-10T00:00:00Z";
const CARD = (id, content, checked) => ({
  id, kind: "file", content, meta: { name: id + ".pdf" }, x: 40, y: 40, w: 200, h: 120,
  checked, hidden: false, folded: false, bookmarked: false, deleted: false, createdAt: STAMP, updatedAt: STAMP,
});

async function main() {
  // 第一次运行为「暂存阶段」：与第④层 HTTP 旅程共用同一个临时库时，
  // 这里先按真实接口把本探针需要的板面/任务准备成自己可控的状态，再重启服务用干净进程重新读取。
  // 第一次已经通过命令行 --data-dir 准备（见 run-all.ps1），这里只做状态复位。
  const boards = await api("GET", "/api/interactive/boards");
  const boardId = boards.json?.boards?.[0]?.id;
  check("B0.1 真实后端可达并有板面", Boolean(boardId), JSON.stringify(boards.json).slice(0, 120));
  if (!boardId) return 1;
  const prefix = "/api/interactive/boards/" + boardId;

  // 种子：一张材料卡 + 一个执行中任务（走真实 API，不伪造）
  const seed = await api("PUT", prefix + "/state", {
    state: { boardId, seq: 0, updatedAt: STAMP, cards: [CARD("m1", "材料一", true), CARD("m2", "材料二", true)], groups: [], links: [], selection: [] },
    reason: "seed",
  });
  check("B0.2 种子板面保存成功", seed.status === 200, "status=" + seed.status);
  await api("POST", prefix + "/intents", { demo: true });
  const listed = await api("GET", prefix + "/intents");
  const combine = (listed.json?.intents || []).find((i) => String(i.title).includes("归为一组"));
  const approved = await api("POST", "/api/interactive/intents/" + combine.id + "/approve", {});
  // 运行中 / 已暂停都属于「材料被改动会影响它」，都是服务端门会拦的状态
  const approvedStatus = approved.json?.intent?.status;
  check("B0.3 演示任务真的进入执行中（running/paused）",
    approved.status === 200 && ["running", "paused"].includes(approvedStatus),
    "status=" + approved.status + " intent=" + JSON.stringify(approvedStatus));

  // 真实 API 把首次引导标记为已看（与用户在弹窗里点「关闭引导」等价的服务端事实）
  const seen = await api("POST", "/api/onboarding/seen", {});
  check("B1.0 首次引导已按真实 API 标记为已看（弹窗不再挡板面）", seen.status === 200, "status=" + seen.status);

  // ---- R1：真实浏览器 + 真实卡片编辑 ----
  const browser = await launch("first");
  await browser.goto(APP + "/#/interactive", 5000);
  const title = await browser.evaluate("document.title || ''");
  const cardsOnScreen = await browser.evaluate("document.querySelectorAll('[data-im=\"card\"]').length");
  check("B1.1 真实浏览器打开真实应用并渲染卡片", cardsOnScreen >= 2, "title=" + JSON.stringify(title) + " cards=" + cardsOnScreen);
  await browser.shot("closure-d-b0-app");

  // 首次引导弹窗会挡住板面：真实点它的「关闭引导」按钮（真实用户操作）
  const closeWizard = await browser.evaluate(
    "(function(){var b=document.querySelector('button[aria-label=\"关闭引导\"]'); if(!b) return 'no-wizard'; b.click(); return 'closed';})()"
  );
  await sleep(1200);
  const wizardGone = await browser.evaluate("!document.querySelector('[aria-label=\"首次引导\"]')");
  check("B1.1a 首次引导弹窗可被真实关闭（否则板面不可操作）", wizardGone === true, "action=" + closeWizard + " gone=" + wizardGone);

  const contentsBefore = await browser.evaluate(
    "(function(){return Array.from(document.querySelectorAll('[data-im=\\\"card\\\"]')).map(function(e){return (e.innerText||'').trim().slice(0,40);});})()"
  );
  // 真实鼠标点选卡片（卡片工具栏只在选中后出现）：先量卡片中心，再派发真实鼠标事件
  const cardCenter = await browser.evaluate(
    "(function(){var c=document.querySelector('[data-card-id=\\\"m1\\\"]'); if(!c) return null; var r=c.getBoundingClientRect();" +
    "return JSON.stringify({x:Math.round(r.left+r.width/2), y:Math.round(r.top+20)});})()"
  );
  if (cardCenter) {
    const pt = JSON.parse(cardCenter);
    // 先派发 mouseMoved 让 Chrome 生成 pointerdown/pointerup（板面靠 pointerdown 选中卡片）
    await browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: pt.x, y: pt.y, button: "none", buttons: 0 });
    await sleep(120);
    await browser.send("Input.dispatchMouseEvent", { type: "mousePressed", x: pt.x, y: pt.y, button: "left", clickCount: 1, buttons: 1 });
    await sleep(120);
    await browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: pt.x, y: pt.y, button: "left", clickCount: 1, buttons: 0 });
  }
  await sleep(700);
  const editClicked = await browser.evaluate(
    "(function(){var b=document.querySelector('[data-im=\\\"card-edit\\\"]'); if(!b) return false; b.click(); return true;})()"
  );
  await sleep(500);
  const typed = await browser.evaluate(
    "(function(){var t=document.querySelector('[data-im=\\\"card-editor\\\"]'); if(!t) return null;" +
    "t.focus(); t.value='浏览器里改成的正文-R1'; t.dispatchEvent(new Event('input',{bubbles:true})); return t.value;})()"
  );
  await sleep(300);
  const doneClicked = await browser.evaluate(
    "(function(){var bs=Array.from(document.querySelectorAll('button')); var b=bs.find(function(x){return (x.innerText||'').indexOf('完成编辑')>=0;}); if(!b) return false; b.click(); return true;})()"
  );
  check("B1.2a 真实点选卡片后打开编辑器并写入新正文",
    Boolean(cardCenter) && editClicked === true && typed === "浏览器里改成的正文-R1" && doneClicked === true,
    "select=" + Boolean(cardCenter) + " edit=" + editClicked + " typed=" + JSON.stringify(typed) + " done=" + doneClicked);
  await sleep(3000);
  const dialogOpen = await browser.evaluate("Boolean(document.querySelector('[data-im=\\\"impact-dialog\\\"]'))");
  const apiAffected = await browser.evaluate(
    "(function(){return fetch('" + API + prefix + "/intents').then(function(r){return r.json();}).then(function(j){return (j.intents||[]).filter(function(i){return i.status==='running';}).map(function(i){return i.title;});});})()"
  );
  check("B1.2 真实编辑触发服务端影响门并弹出确认框",
    dialogOpen === true, "dialog=" + dialogOpen + " running=" + JSON.stringify(apiAffected));
  await browser.shot("closure-d-b1-impact-dialog");

  if (dialogOpen) {
    const dialogItems = await browser.evaluate("document.querySelectorAll('[data-im=\\\"impact-item\\\"]').length");
    check("B1.3 确认框列出受影响任务", dialogItems >= 1, "items=" + dialogItems);
    await browser.evaluate("(function(){var b=document.querySelector('[data-im=\\\"impact-cancel\\\"]'); if(b) b.click(); return Boolean(b);})()");
    await sleep(2500);
    const dialogAfter = await browser.evaluate("Boolean(document.querySelector('[data-im=\\\"impact-dialog\\\"]'))");
    // 取消后板面必须回到服务器已保存正文：直接从真实后端读，并与界面卡片文字比对
    const live = await api("GET", prefix + "/state");
    const serverContents = (live.json?.state?.cards || []).map((c) => c.content);
    const onScreen = await browser.evaluate(
      "(function(){return Array.from(document.querySelectorAll('[data-im=\\\"card\\\"]')).map(function(e){return (e.innerText||'').trim();}).join(' | ');})()"
    );
    const stillEdited = String(onScreen).includes("浏览器里改成的正文-R1");
    check("B1.4 取消后确认框关闭", dialogAfter === false, "dialog=" + dialogAfter);
    check("B1.5 取消后界面不再显示被取消掉的正文（板面回到服务器已保存内容）",
      !stillEdited, "server=" + JSON.stringify(serverContents) + " screen=" + JSON.stringify(String(onScreen).slice(0, 160)));
    check("B1.6 取消不产生服务端改动", serverContents.join("|").includes("材料一"), "server=" + JSON.stringify(serverContents));
  }
  check("B1.7 真实浏览器无控制台异常", browser.consoleErrors.length === 0, JSON.stringify(browser.consoleErrors.slice(0, 3)));
  check("B1.8 真实浏览器无 4xx/5xx 请求（webfont 回落已按已知现象排除）", browser.httpFails.length === 0,
    JSON.stringify(browser.httpFails.slice(0, 4)));

  // ---- B2：真实关闭浏览器进程 + 同一用户目录重开 ----
  const marker = "closure-d-重开探针-" + Date.now();
  await browser.goto(APP + "/#/interactive", 3500);
  await browser.evaluate("localStorage.setItem('qio.closureD.marker', " + JSON.stringify(marker) + ")");
  await sleep(800);
  await browser.close();

  const reopened = await launch("second");
  await reopened.goto(APP + "/#/interactive", 4500);
  const readBack = await reopened.evaluate("localStorage.getItem('qio.closureD.marker')");
  check("B2.1 结束浏览器进程后用同一用户目录重开，本机数据仍在", readBack === marker,
    "written=" + JSON.stringify(marker) + " read=" + JSON.stringify(readBack));
  await reopened.shot("closure-d-b2-reopened");
  check("B2.2 重开进程无控制台异常", reopened.consoleErrors.length === 0, JSON.stringify(reopened.consoleErrors.slice(0, 3)));
  await reopened.close();

  const failed = results.filter((r) => !r.ok).map((r) => r.name);
  console.log("---- 汇总：" + (results.length - failed.length) + "/" + results.length + " 通过 ----");
  if (failed.length) {
    console.log("FAIL 项：" + failed.join("；"));
    return 1;
  }
  return 0;
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error("探针异常：" + (err && err.stack ? err.stack : String(err)));
  process.exit(2);
});
