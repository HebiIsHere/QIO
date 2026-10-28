/**
 * 一条命令跑完设置页取证：起隔离环境 → 视觉核查 → 交互取证 → 录屏 → 汇总 → 停环境。
 *
 * 用法：
 *   node scripts/settings-verify/run-all.mjs --label d --report-label baseline
 *   常用开关：--quick（只 1440x900 dark，冒烟用）/ --skip-video / --skip-interaction
 *            --skip-visual / --keep-up（跑完不停服务）/ --allow-fail（基线负对照用）
 *            --url http://127.0.0.1:5106（已有实例时配合 --no-up）
 *
 * 退出码：0 = 全部步骤通过；非 0 = 有步骤失败（--allow-fail 时仍返回 0，但报告如实记录）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { EVIDENCE_DIR, REPO, SHOTS_DIR, VIDEO_DIR, stamp } from "./lib/env.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const reportLabel = String(flag("report-label", stamp()));
const serveLabel = String(flag("label", "d"));
const url = String(flag("url", "http://127.0.0.1:5106"));
const quick = !!flag("quick", false);
const allowFail = !!flag("allow-fail", false);
const keepUp = !!flag("keep-up", false);
const noUp = !!flag("no-up", false);
const outShots = String(flag("out-shots", SHOTS_DIR));
const outVideo = String(flag("out-video", VIDEO_DIR));
let apiUrl = String(flag("api", process.env.QIO_SETTINGS_API || ""));
const steps = [];

function runNode(script, extraArgs, options = {}) {
  const result = spawnSync(process.execPath, [join(here, script), ...extraArgs], {
    cwd: REPO,
    stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
    encoding: "utf8",
    timeout: options.timeoutMs ?? 900000,
  });
  return { status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function recordStep(name, exitCode, detail) {
  steps.push({ name, exitCode, ok: exitCode === 0, ...detail });
  console.log("[run-all] " + name + " → exit " + exitCode);
}

let upInfo = null;
try {
  if (!noUp) {
    console.log("[run-all] 起隔离环境（label=" + serveLabel + "）…");
    const up = runNode("serve.mjs", ["up", "--label", serveLabel], { capture: true });
    recordStep("serve-up", up.status, { stdout: up.stdout.slice(-4000), stderr: up.stderr.slice(-2000) });
    if (up.status !== 0) throw new Error("环境没起来：\n" + up.stdout + "\n" + up.stderr);
    try {
      upInfo = JSON.parse(up.stdout.slice(up.stdout.indexOf("{")));
    } catch {
      upInfo = null;
    }
  }
  if (!apiUrl) apiUrl = upInfo?.backend || String(flag("api", "http://127.0.0.1:8756"));

  if (!flag("skip-visual", false)) {
    const visualArgs = ["--url", url, "--api", apiUrl, "--out", outShots, "--report-label", reportLabel];
    if (quick) visualArgs.push("--quick");
    if (allowFail) visualArgs.push("--allow-fail");
    recordStep("visual", runNode("visual.mjs", visualArgs).status, { out: outShots });
  }

  if (!flag("skip-interaction", false)) {
    const interactionArgs = ["--url", url, "--api", apiUrl, "--label", reportLabel];
    if (allowFail) interactionArgs.push("--allow-fail");
    recordStep("interaction", runNode("interaction.mjs", interactionArgs).status, { out: join(EVIDENCE_DIR, "interaction-" + reportLabel + ".json") });
  }

  if (!flag("skip-video", false)) {
    const recordArgs = ["--url", url, "--api", apiUrl, "--out", outVideo, "--label", reportLabel];
    if (quick) recordArgs.push("--flows", "categories");
    recordStep("record", runNode("record.mjs", recordArgs).status, { out: outVideo });
  }
} catch (error) {
  console.error("[run-all] 失败：" + (error?.message ?? error));
  steps.push({ name: "fatal", exitCode: 1, ok: false, error: String(error?.message ?? error) });
} finally {
  if (!noUp && !keepUp) {
    const down = runNode("serve.mjs", ["down", "--label", serveLabel], { capture: true });
    recordStep("serve-down", down.status, { stdout: down.stdout.slice(-1000) });
  }
  const summary = {
    label: reportLabel,
    quick,
    url,
    startedAt: new Date().toISOString(),
    steps,
    artifacts: {
      shots: outShots,
      video: outVideo,
      visualReport: join(outShots, "report.json"),
      visualMarkdown: join(outShots, "report.md"),
      interactionReport: join(EVIDENCE_DIR, "interaction-" + reportLabel + ".json"),
      videoReport: join(outVideo, "record-report.json"),
    },
  };
  // 汇总每条步骤的报告数字，给人看的摘要
  const lines = ["# 设置页取证汇总（" + reportLabel + "）", ""];
  for (const step of steps) lines.push("- " + step.name + "：exit " + step.exitCode + (step.ok ? "" : "（失败）"));
  lines.push("", "| 步骤 | 结果 | 产物 |", "| --- | --- | --- |");
  for (const step of steps) lines.push("| " + step.name + " | " + (step.ok ? "通过" : "失败") + " | " + JSON.stringify(step.out ?? step.stdout?.slice(0, 120) ?? "") + " |");
  try {
    const visual = JSON.parse(readFileSync(join(outShots, "report.json"), "utf8"));
    summary.visual = visual.summary;
    lines.push("", "## 视觉核查", "", "- 截图 " + visual.summary.screenshots + " 张；检查 " + visual.summary.total + " 条，通过 " + visual.summary.passed + "，失败 " + visual.summary.failed + "。", "", "| 范围 | 检查 | 结果 |", "| --- | --- | --- |");
    for (const check of visual.checks) lines.push("| " + check.scope + " | " + check.name + " | " + (check.ok ? "通过" : "**失败**") + " |");
  } catch { /* 没有报告就跳过 */ }
  try {
    const interaction = JSON.parse(readFileSync(join(EVIDENCE_DIR, "interaction-" + reportLabel + ".json"), "utf8"));
    summary.interaction = interaction.summary;
    lines.push("", "## 交互取证", "", "- 检查 " + interaction.summary.total + " 条，通过 " + interaction.summary.passed + "，失败 " + interaction.summary.failed + "。", "", "| 场景 | 检查 | 结果 |", "| --- | --- | --- |");
    for (const check of interaction.checks) lines.push("| " + check.scenario + " | " + check.name + " | " + (check.ok ? "通过" : "**失败**") + " |");
  } catch { /* 没有报告就跳过 */ }
  try {
    const video = JSON.parse(readFileSync(join(outVideo, "record-report.json"), "utf8"));
    summary.video = video.summary;
    lines.push("", "## 录屏", "", "- 完整 " + video.summary.recorded + "/" + video.summary.flows + " 条，视频 " + video.summary.videos + " 个 → " + outVideo);
    for (const flow of video.flows) lines.push("- " + flow.name + "（" + flow.title + "）：" + flow.status + (flow.video ? " · " + flow.video : ""));
  } catch { /* 没有报告就跳过 */ }
  mkdirSync(EVIDENCE_DIR, { recursive: true });
  writeFileSync(join(EVIDENCE_DIR, "run-all-" + reportLabel + ".json"), JSON.stringify(summary, null, 2), "utf8");
  writeFileSync(join(EVIDENCE_DIR, "run-all-" + reportLabel + ".md"), lines.join("\n"), "utf8");
  console.log("\n===== 汇总 =====");
  for (const step of steps) console.log(step.name + "：" + (step.ok ? "通过" : "失败") + "（exit " + step.exitCode + "）");
  console.log("证据目录：" + EVIDENCE_DIR);
  const failed = steps.filter((step) => !step.ok);
  process.exitCode = failed.length && !allowFail ? 1 : 0;
}
