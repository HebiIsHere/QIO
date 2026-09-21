/**
 * 给「新打开页面会看到上一次残留」这条问题留一张可复验的证据图。
 *
 * 做法：同一个后端上先开一页（甲），注入一条错误提示 + 一份队列快照；
 * 关掉甲，再开一个全新的页面（乙）并立刻截图 —— 如果乙页面上出现了甲留下的
 * 提示条 / 队列，那就说明「新页面收到了上一次的事件」这件事是真的。
 * 乙页面全程没有注入任何事件。
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "conv-root";
const entries = readManifest(GROUP)?.entries ?? [];
const log = [];

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

const browser = await launchBrowser();

// 甲：制造一段「上一次的痕迹」
const a = await createSession(browser, { group: GROUP, name: "证据", theme: "dark", ...INSTANCE });
await a.goto("#/", { waitFor: ".conversation", settle: 1500 });
await a.inject("TURN_QUEUE", {
  revision: 9001,
  running: { turn_id: "replay_1", message: "上一条还在跑的任务" },
  queued: [
    { turn_id: "replay_2", message: "排队里的第一条" },
    { turn_id: "replay_3", message: "排队里的第二条" },
  ],
  cancelled: [{ turn_id: "replay_4", message: "上一条被取消的任务" }],
});
await a.inject("ERROR", { message: "上一条消息执行失败：模型调用被拒绝（401）" });
await sleep(1000);
log.push("甲页面：已注入一份队列快照 + 一条错误提示");
await a.close({ save: false });

// 乙：全新页面，什么都不注入
const b = await createSession(browser, { group: GROUP, name: "证据", theme: "dark", ...INSTANCE });
await b.goto("#/", { waitFor: ".conversation", settle: 2500 });
const residue = await b.page.evaluate(() => {
  const text = (sel) => {
    const el = document.querySelector(sel);
    return el ? el.innerText.replace(/\s+/g, " ").trim() : null;
  };
  return { err: text(".notice.err"), queue: text(".queue") };
});
log.push(`乙页面（全新打开、未注入）：错误条=${residue.err ?? "无"} / 队列=${residue.queue ?? "无"}`);
put(
  await b.shot("conv-120-replay-residue", "新打开的页面里出现了上一次的提示与队列（事件重放）", {
    note: `复验：甲页面注入后关掉，乙页面全程没有注入任何事件，却出现了「${(residue.err ?? "无").slice(0, 24)}」与「${(residue.queue ?? "无").slice(0, 24)}」`,
  }),
);
await b.close({ save: false });
await browser.close();

saveManifest(GROUP, entries);
console.log("证据日志：");
for (const line of log) console.log(`  - ${line}`);
process.exit(0);
