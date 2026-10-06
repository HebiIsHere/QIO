/**
 * 互动模式验收探针的薄封装：调用 scripts/visual_probe.mjs，把 JSON 结果里的
 * eval 值 / 截图名 / 控制台错误 / 失败请求挑出来打成人能读的报告。
 *
 * 用法：node scripts/interactive-verify/run-probe.mjs <steps.json>
 * 退出码：0 = 探针本身跑完（业务断言在调用方）；1 = 探针崩溃或返回非 JSON。
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const stepsPath = process.argv[2];
if (!stepsPath) {
  console.error("用法：node scripts/interactive-verify/run-probe.mjs <steps.json>");
  process.exit(1);
}

const probe = spawnSync(
  process.execPath,
  [resolve(root, "scripts", "visual_probe.mjs"), "@" + resolve(stepsPath)],
  { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
);

const raw = probe.stdout ?? "";
const start = raw.indexOf("{");
if (start < 0) {
  console.error("探针没有返回 JSON：", raw.slice(0, 500), probe.stderr?.slice(0, 500));
  process.exit(1);
}
let payload;
try {
  payload = JSON.parse(raw.slice(start));
} catch (error) {
  console.error("探针输出不是 JSON：", String(error), raw.slice(0, 300));
  process.exit(1);
}

const lines = [];
for (const item of payload.results ?? []) {
  if (item.op === "eval") lines.push(`eval: ${item.error ? "ERROR " + item.error : item.value}`);
  else if (item.op === "screenshot") lines.push(`shot: ${item.name}`);
  else lines.push(`${item.op}: ${JSON.stringify(item.value ?? item.error ?? "").slice(0, 300)}`);
}
if (payload.errors?.length) lines.push(`页面错误：${payload.errors.join(" | ")}`);
const fails = (payload.httpFails ?? []).filter((x) => !x.includes("@fontsource"));
if (fails.length) lines.push(`请求失败：${fails.slice(0, 8).join(" | ")}`);
console.log(lines.join("\n"));
