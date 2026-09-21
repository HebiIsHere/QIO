/**
 * 把「控制器那一路 conv 采集」的截图补登记进目录。
 *
 * 背景：这一轮开工时工作区里同时有两路在采对话页 ——
 * 控制器的 conv.mjs（实例 6200/8835，产物落在 `conv/`）与另一路的 conv-root（`conv-root/`）。
 * 两路边跑边写同一份 manifest，先跑完的那一路的条目被后写的那份覆盖掉了；
 * 图还在磁盘上，但没有任何清单登记它们（在图册里会显示成「未登记」）。
 *
 * 这个脚本把那一路的 71 张图按原采集时的标题 / 主题 / 视口补登记到
 * `manifest-conv-parallel.json`（独立文件，不覆盖任何在用的清单）。
 * 只登记「文件存在、且没有被别的清单占用」的条目 —— 已经被别的清单登记的 id 说明
 * 那是别人的同名产物，不动它。
 *
 * 用法：
 *   node scripts/ui-catalog/conv-parallel-manifest.mjs
 */
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { SHOT_ROOT } from "./lib.mjs";

const GROUP = "conv";
const OUT = path.join(SHOT_ROOT, "manifest-conv-parallel.json");

/**
 * 原始采集记录：`id|主题|视口|标题`。
 * 数据来自那一次真实运行的控制台输出（不是事后回想）：标题与主题就是当时写进 manifest 的值。
 * 视口：1440=默认 1440×900，820=窄窗口 820×900。
 */
const RAW = `
conv-01-multiturn|dark|1440|多轮对话·全貌
conv-02-long-answer|dark|1440|长回复正文
conv-03-table|dark|1440|正文里的表格
conv-04-code-wide|dark|1440|宽代码块与复制按钮
conv-05-turn-meta-hover|dark|1440|轮次分隔头（hover）
conv-06-message-hover|dark|1440|消息 hover 出现复制按钮
conv-07-copy-failed|dark|1440|复制失败（显式提示）
conv-09-multiturn-light|light|1440|多轮对话·浅色
conv-10-narrow|dark|820|窄窗口布局（820×900）
conv-11-table-narrow|dark|820|窄窗口下表格横向滚动
conv-12-back-latest|dark|820|上翻阅读·回到最新消息与未读计数
conv-13-back-latest-zoom|dark|820|「回到最新消息」入口
conv-14-empty|dark|1440|空状态（无消息）
conv-15-empty-composer|dark|1440|空状态下的输入区
conv-16-waiting|dark|1440|等待中·三圆点 + 正在处理
conv-17-waiting-zoom|dark|1440|等待中状态行
conv-18-generating|dark|1440|正在生成·流式正文
conv-19-tool-running|dark|1440|正在使用工具·工具卡运行中
conv-20-tool-running-zoom|dark|1440|工具卡·运行中
conv-21-tool-ok|dark|1440|工具卡·成功（折叠）
conv-22-tool-expanded|dark|1440|工具卡·展开（参数 + 耗时 + 输出）
conv-23-tool-failed|dark|1440|工具卡·失败（err 色标）
conv-24-subagent-running|dark|1440|独立任务·running
conv-25-subagent-done|dark|1440|独立任务·done
conv-26-subagent-failed|dark|1440|独立任务·failed
conv-27-notify|dark|1440|正在整理独立任务的结果（notify）
conv-28-queue-chip|dark|1440|排队条·折叠（1 运行中 · 2 排队中 · 1 已取消）
conv-29-queue-open|dark|1440|排队条·展开
conv-30-continue-bar|dark|1440|继续条·已达迭代上限
conv-31-notice-error|dark|1440|错误条（含 前往设置 / 查看详情）
conv-31-notice-error-zoom|dark|1440|错误条（放大）
conv-32-notice-warning|dark|1440|警告条
conv-32-notice-warning-zoom|dark|1440|警告条（放大）
conv-33-notice-cancelled|dark|1440|已停止条（正常结局，不当错误）
conv-33-notice-cancelled-zoom|dark|1440|已停止条（放大）
conv-34-notice-unavailable|dark|1440|模型不可用条
conv-34-notice-unavailable-zoom|dark|1440|模型不可用条（放大）
conv-35-notice-fallback|dark|1440|兼容模式条
conv-35-notice-fallback-zoom|dark|1440|兼容模式条（放大）
conv-36-credential-unavailable|dark|1440|凭据状态：不可用
conv-38-credential-revoked|dark|1440|凭据状态：已撤销
conv-39-history-error|dark|1440|历史读取失败 + 重试
conv-40-topic-switch|dark|1440|话题切换提示（低干扰、不遮罩）
conv-42-anchor|dark|1440|「从…继续」锚点提示
conv-43-knowledge-candidate|dark|1440|知识候选卡（回答完成后出现）
conv-44-knowledge-candidate-zoom|dark|1440|知识候选卡（放大）
conv-46-tool-create-proposed|dark|1440|工具创建·提案
conv-47-tool-create-building|dark|1440|工具创建·构建中
conv-48-tool-create-testing|dark|1440|工具创建·测试中
conv-49-tool-create-awaiting|dark|1440|工具创建·等待确认
conv-50-tool-create-registered|dark|1440|工具创建·已注册
conv-51-tool-create-failed|dark|1440|工具创建·失败
conv-52-tool-create-detail|dark|1440|工具创建·展开详情
conv-53-approval-tool-high|dark|1440|工具创建审批（高风险·批准按钮降调）
conv-53-approval-tool-high-zoom|dark|1440|工具创建审批·高风险（放大）
conv-54-approval-tool-low|dark|1440|工具创建审批（低风险·主操作色）
conv-54-approval-tool-low-zoom|dark|1440|工具创建审批·低风险（放大）
conv-55-approval-credential|dark|1440|凭据授权审批
conv-55-approval-credential-zoom|dark|1440|凭据授权审批（放大）
conv-56-approval-knowledge|dark|1440|高影响知识确认
conv-56-approval-knowledge-zoom|dark|1440|高影响知识确认（放大）
conv-57-approval-subagent|dark|1440|子 agent 创建审批（含预算编辑）
conv-57-approval-subagent-zoom|dark|1440|子 agent 创建审批（放大）
conv-58-approval-advanced|dark|1440|审批·高级详情展开（参数 / 指纹 / 逐条测试）
conv-59-approval-entry|dark|1440|审批入口条（不抢焦点）
conv-60-approval-entry-zoom|dark|1440|「有 1 项操作等待确认」入口
conv-61-developer-mode|dark|1440|开发者模式·每条消息的诊断明细
conv-62-developer-meta|dark|1440|开发者模式·消息明细
conv-63-floats-default|dark|1440|右下角浮动入口·默认
conv-64-settings-float-hover|dark|1440|设置入口·hover
conv-65-planet-dock|dark|1440|星球入口球（与全屏星球同一对象）
conv-66-dock-dragged|dark|1440|拖动入口球后的位置（贴边吸附）
conv-67-float-edge-hidden|dark|1440|贴边隐藏（指针移开后淡出到边缘）
conv-68-planet-open-over-conversation|dark|1440|打开星球（对话页保留在下方）
conv-69-back-from-planet|dark|1440|关闭星球后回到对话页（位置保留）
`;

const VIEWPORTS = { 1440: { width: 1440, height: 900 }, 820: { width: 820, height: 900 } };

/** 已经被别的清单登记的文件（绝对路径）：同名 id 是别人的产物，不抢 */
const taken = new Set();
for (const name of readdirSync(SHOT_ROOT)) {
  if (!/^manifest-.*\.json$/.test(name) || name === "manifest-merged.json" || name === "manifest-conv-parallel.json") continue;
  try {
    const data = JSON.parse(readFileSync(path.join(SHOT_ROOT, name), "utf-8"));
    for (const e of data.entries ?? []) {
      if (e.file) taken.add(path.resolve(e.file));
    }
  } catch {
    /* 忽略读不动的清单 */
  }
}

const entries = [];
const skipped = [];
for (const line of RAW.split("\n")) {
  const row = line.trim();
  if (!row) continue;
  const [id, theme, vp, title] = row.split("|");
  const file = path.join(SHOT_ROOT, GROUP, `${id}.png`);
  if (!existsSync(file)) {
    skipped.push(`${id}（文件不存在：那次采集这一张没成功）`);
    continue;
  }
  if (taken.has(path.resolve(file))) {
    skipped.push(`${id}（已被别的清单登记，保留对方的记录）`);
    continue;
  }
  entries.push({
    id,
    group: GROUP,
    title,
    note: "控制器那一路的采集（与 conv-root 并行，两组都在，互为补充）",
    file,
    theme,
    viewport: VIEWPORTS[vp],
    url: "",
  });
}

writeFileSync(
  OUT,
  JSON.stringify(
    {
      group: GROUP,
      generatedAt: new Date().toISOString(),
      source: "scripts/ui-catalog/conv.mjs（控制器那一路，实例 6200/8835）",
      count: entries.length,
      entries,
    },
    null,
    2,
  ),
  "utf-8",
);

console.log(`补登记 ${entries.length} 条 → ${OUT}`);
if (skipped.length) {
  console.log(`跳过 ${skipped.length} 条：`);
  for (const s of skipped) console.log(`  - ${s}`);
}
