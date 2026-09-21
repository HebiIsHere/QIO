/**
 * 通知条家族的重拍 + 断言（`conv-root` 分组）。
 *
 * 为什么要重拍：后端事件总线会把最近的 50 条事件重放给新连接
 * （backend/src/agent/api/bus.py::_replay，只过滤掉 APPROVAL_REQUIRED）。
 * 于是「注入 paused」的那一页里先收到了重放出来的上一条 unavailable，
 * 界面上显示的是上一条的文案，而截图标题却写着 paused。
 * 实测证据：conv-66-cred-paused-crop.png 里是「当前没有可用的模型凭据…」。
 *
 * 这一次的做法：
 * 1. 先重启后端清空事件历史（在脚本外面做，见用法）；
 * 2. 每条状态都断言期望文案真的出现在页面上，对不上就如实记为失败；
 * 3. paused / revoked 两条不假设一定有提示条：有就拍，没有就按实测标题记。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py down --name convroot
 *   python scripts/ui-catalog/instance.py up --name convroot --backend-port 8839 --frontend-port 6204
 *   $env:QIO_BASE="http://127.0.0.1:6204"; $env:QIO_API="http://127.0.0.1:8839"
 *   node scripts/ui-catalog/notice-verify.mjs
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

const MAIN = { base: ENV_BASE, api: ENV_API };
const GROUP = "conv-root";

const entries = readManifest(GROUP)?.entries ?? [];
const log = [];

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

const browser = await launchBrowser();

/** 取页面上第一个提示条的文本（没有就返回 null） */
async function noticeText(page, selector) {
  const loc = page.locator(selector).first();
  if (!(await loc.count())) return null;
  return (await loc.innerText()).replace(/\s+/g, " ").trim();
}

async function runCase({ id, title, injects, selector, expect }) {
  const s = await createSession(browser, { group: GROUP, name: "通知条复核", theme: "dark", ...MAIN });
  try {
    await s.goto("#/", { waitFor: ".conversation", settle: 1800 });
    for (const [type, data] of injects) await s.inject(type, data);
    await sleep(900);
    const text = await noticeText(s.page, selector);
    const ok = expect ? !!text && text.includes(expect) : text === null;
    log.push(`${id}: ${ok ? "PASS" : "FAIL"} 期望=${expect} 实际=${text ?? "（无）"}`);
    put(
      await s.shot(id, title, {
        note: text
          ? `断言通过：页面文案「${text.slice(0, 60)}」`
          : "实测：这一状态下对话页没有出现提示条",
      }),
    );
    if (text) put(await s.shotEl(selector, `${id}-crop`, `${title}（局部）`, { pad: 8 }));
  } catch (err) {
    log.push(`${id}: FAIL ${String(err?.message ?? err).slice(0, 120)}`);
  } finally {
    await s.close({ save: false });
  }
  saveManifest(GROUP, entries);
}

await runCase({
  id: "conv-60-error",
  title: "错误条：本轮执行失败",
  injects: [
    ["TURN_START", { turn_id: "nv_1", revision: 1001 }],
    ["ERROR", { message: "这一轮执行失败：模型调用被拒绝（401）", turn_id: "nv_1" }],
  ],
  selector: ".notice.err",
  expect: "模型调用被拒绝",
});

await runCase({
  id: "conv-61-warning",
  title: "警告条：非致命提示",
  injects: [["WARNING", { message: "搜索结果里有 2 条被截断，结论可能不完整。" }]],
  selector: ".notice.warn",
  expect: "搜索结果里有 2 条被截断",
});

await runCase({
  id: "conv-62-cancelled",
  title: "已停止条：取消是正常结局",
  injects: [
    ["TURN_START", { turn_id: "nv_2", revision: 1002 }],
    ["TURN_END", { turn_id: "nv_2", status: "cancelled", revision: 1003 }],
  ],
  selector: ".notice.quiet",
  expect: "已按你的要求停止",
});

await runCase({
  id: "conv-63-unavailable",
  title: "模型不可用：TURN_END status=unavailable",
  injects: [
    ["TURN_START", { turn_id: "nv_3", revision: 1004 }],
    ["TURN_END", { turn_id: "nv_3", status: "unavailable", revision: 1005 }],
  ],
  selector: ".notice.warn",
  expect: "没有可用的模型凭据",
});

await runCase({
  id: "conv-64-fallback",
  title: "兼容模式条：模型能力降级",
  injects: [["FALLBACK", { message: "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）" }]],
  selector: ".notice.fallback",
  expect: "兼容模式",
});

await runCase({
  id: "conv-65-cred-unavailable",
  title: "凭据状态：没有可用凭据（有提示条）",
  injects: [["CREDENTIAL_STATUS", { status: "unavailable" }]],
  selector: ".notice.warn",
  expect: "没有可用的模型凭据",
});

for (const [status, id] of [
  ["paused", "conv-66-cred-paused"],
  ["revoked", "conv-67-cred-revoked"],
]) {
  const s = await createSession(browser, { group: GROUP, name: "通知条复核", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await s.inject("CREDENTIAL_STATUS", { status });
  await sleep(1000);
  const warn = await noticeText(s.page, ".notice.warn");
  const quiet = await noticeText(s.page, ".notice.quiet");
  const err = await noticeText(s.page, ".notice.err");
  const actual = warn ?? quiet ?? err;
  log.push(`${id}: 期望=无提示条 实际=${actual ?? "（无）"}`);
  put(
    await s.shot(
      id,
      actual
        ? `凭据 ${status}：对话页出现提示条`
        : `凭据 ${status}：对话页没有可见提示（只有内部状态变化）`,
      {
        note: actual
          ? `实测文案：${actual.slice(0, 80)}`
          : "实测：CREDENTIAL_STATUS=" +
            status +
            " 只写入 store 的 credentialNotice，没有任何组件渲染它，用户看不到这一条",
      },
    ),
  );
  await s.close({ save: false });
  saveManifest(GROUP, entries);
}

await runCase({
  id: "conv-68-resync",
  title: "事件流抖动：正在同步最新状态",
  injects: [["RESYNC", { reason: "subscriber_backlog_overflow" }]],
  selector: ".notice.warn",
  expect: "同步最新状态",
});

await browser.close();
saveManifest(GROUP, entries);
console.log("断言结果：");
for (const line of log) console.log(`  - ${line}`);
process.exit(0);
