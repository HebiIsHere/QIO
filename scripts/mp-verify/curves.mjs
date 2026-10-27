/**
 * 逐帧曲线图（把 frames/probe 的 JSON 画成 SVG + 表格，写进证据目录）。
 *
 * 为什么单独出图：动画质量不能只看文字结论。曲线图能一眼看出「有没有跳、有没有反向、
 * 长帧落在哪、终点有没有精确落位」，而且和 JSON 数据一一对应，可复核。
 *
 * 用法：node scripts/mp-verify/curves.mjs --dir docs/mp-verify/c-planet
 */
import { existsSync, readFileSync, writeFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  return args[index + 1] ?? fallback;
};
const root = resolve(String(flag("dir", join(here, "..", "..", "docs", "mp-verify", "c-planet"))));
const outFile = join(root, "frames", "curves.md");

function findFrameReports(dir) {
  const base = join(dir, "frames");
  if (!existsSync(base)) return [];
  return readdirSync(base)
    .filter((name) => name.endsWith("-report.json"))
    .map((name) => join(base, name));
}

/** 画一条 mp(t) 折线；x 轴时间（ms），y 轴 0..1；长帧用灰色竖条标出，触发时刻用细竖线。 */
function svgCurve(curve, options = {}) {
  const width = 720;
  const height = 180;
  const pad = 26;
  if (!curve.length) return "";
  const maxT = Math.max(1, curve[curve.length - 1].t);
  const x = (t) => pad + (t / maxT) * (width - pad * 2);
  const y = (mp) => height - pad - mp * (height - pad * 2);
  const points = curve.map((f) => x(f.t).toFixed(1) + "," + y(f.mp).toFixed(1)).join(" ");
  const bars = [];
  for (let i = 1; i < curve.length; i += 1) {
    const dt = curve[i].t - curve[i - 1].t;
    if (dt > 60) {
      bars.push(
        '<rect x="' + x(curve[i - 1].t).toFixed(1) + '" y="' + pad + '" width="' + Math.max(1, x(curve[i].t) - x(curve[i - 1].t)).toFixed(1) + '" height="' + (height - pad * 2) + '" fill="#000" fill-opacity="0.08" />',
      );
    }
  }
  const target = options.target ?? null;
  const targetLine =
    target === null
      ? ""
      : '<line x1="' + pad + '" y1="' + y(target).toFixed(1) + '" x2="' + (width - pad) + '" y2="' + y(target).toFixed(1) + '" stroke="#c51b7d" stroke-dasharray="4 3" stroke-width="1" />';
  const trigger = options.triggerAt
    ? '<line x1="' + x(options.triggerAt).toFixed(1) + '" y1="' + pad + '" x2="' + x(options.triggerAt).toFixed(1) + '" y2="' + (height - pad) + '" stroke="#b0136a" stroke-width="1" stroke-opacity="0.6" />'
    : "";
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + " " + height + '">' +
    '<rect width="100%" height="100%" fill="#fbfaf9" />' +
    bars.join("") +
    targetLine +
    trigger +
    '<polyline points="' + points + '" fill="none" stroke="#c51b7d" stroke-width="2" />' +
    '<text x="' + pad + '" y="' + (height - 6) + '" font-size="11" fill="#666">0ms</text>' +
    '<text x="' + (width - pad - 44) + '" y="' + (height - 6) + '" font-size="11" fill="#666">' + Math.round(maxT) + "ms</text>" +
    '<text x="4" y="' + (pad + 4) + '" font-size="11" fill="#666">1.0</text>' +
    '<text x="4" y="' + (height - pad + 4) + '" font-size="11" fill="#666">0.0</text>' +
    "</svg>"
  );
}

const lines = [];
lines.push("# 逐帧曲线（自动生成，数据来自 frames/*.json 与 probe/*.json）");
lines.push("");
lines.push("横轴时间（ms），纵轴 `--mp`（0..1）。**灰色竖条 = 长帧（dt > 60ms）**，玫红虚线 = 目标进度，细竖线 = 反向触发时刻。");
lines.push("本机 headless WebGL 走 SwiftShader，帧间隔是**双模态**（约 7ms 与 100–180ms 交替），所以：");
lines.push("**动画质量用「几何不变式 + 归一化步进 + 长帧单列」判断，不看原始 |Δmp|**。");
lines.push("");

for (const file of findFrameReports(root)) {
  const report = JSON.parse(readFileSync(file, "utf8"));
  lines.push("## " + report.label + " · " + report.viewport);
  lines.push("");
  for (const action of report.actions) {
    const target = action.action === "expand" ? 1 : 0;
    lines.push("### " + action.action);
    lines.push("");
    lines.push(svgCurve(action.curve, { target, triggerAt: action.trigger && action.trigger.at }));
    lines.push("");
    lines.push("- 帧数 " + action.frames + "，时长 " + Math.round(action.durationMs) + "ms，fps≈" + action.frameRate);
    lines.push("- 归一化基准 " + action.baseDurationMs + "ms（令牌动画时长），长帧 " + (action.longFrames || []).length + " 个");
    lines.push("- 球心 x：" + action.centerX.start + " → " + action.centerX.end + "；选中标签 x：" + action.labelX.start + " → " + action.labelX.end);
    lines.push("- **teleports（归一化步进 > 1.6）：" + action.teleports.length + " 处**" + (action.teleports.length ? "：" + action.teleports.map((t) => "t=" + t.t + "ms dt=" + t.dt + "ms step=" + t.step + " norm=" + t.normStep).join("；") : ""));
    if (action.trigger && action.trigger.at !== null) {
      lines.push("- 反向触发：t=" + Math.round(action.trigger.at) + "ms（此时 --mp=" + action.trigger.mp + "，点的是 " + action.trigger.selector + "），过冲 " + action.reverseOvershoot);
    }
    lines.push("");
  }
}

const probeFile = join(root, "probe", "rev-1440x970-probe.json");
if (existsSync(probeFile)) {
  const probe = JSON.parse(readFileSync(probeFile, "utf8"));
  const reduced = probe.checks && probe.checks.reducedMotion;
  if (reduced && Array.isArray(reduced.curve)) {
    lines.push("## 减少动态效果（data-motion=reduced）");
    lines.push("");
    lines.push(svgCurve(reduced.curve, { target: 1 }));
    lines.push("");
    lines.push("- " + reduced.verdict);
    lines.push("- 0..1 之间的中间帧数量：" + reduced.intermediateFrames + "；点击派发到生效：" + reduced.jumpFrameMs + "ms");
    lines.push("");
  }
  const idle = probe.checks && probe.checks.idleStopsRendering;
  if (idle) {
    lines.push("## 渲染收尾（静置 2.5s）");
    lines.push("");
    lines.push("- 渲染计数 " + idle.rendersBefore + " → " + idle.rendersAfter + "（Δ" + idle.rendersDelta + "），CDP Frames Δ" + idle.cdpFramesDelta + "，loopActive=" + idle.loopActive);
    lines.push("- 判定：" + idle.verdict);
    lines.push("");
  }
}

writeFileSync(outFile, lines.join("\n") + "\n", "utf8");
console.log("写出 " + outFile + "（" + lines.length + " 行）");
