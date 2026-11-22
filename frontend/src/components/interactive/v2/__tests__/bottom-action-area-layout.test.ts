/**
 * 互动模式底部操作区（2026-11）布局守卫。
 *
 * 用户的确认稿要求：
 * - 收起：一条轻量操作行 + 下方独立胶囊输入；模式切换在左、扁长把手在整个输入区域水平正中、
 *   板面工具靠右；操作行透明底、按钮独立；
 * - 展开：同一根把手在面板顶边，顶边由把手 + 左右两段细线接成，两侧线到输入胶囊水平中线淡出；
 * - 玻璃材质集中在聊天面板并按真实面板范围裁剪；开合/拖动只更新显示态。
 *
 * 组件行为由 InteractiveConversation.test.ts 覆盖；这里只守**结构与样式契约**（源码级），
 * 防止布局/材质/把手定位被后续改动悄悄改回去。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
const view = readFileSync(resolve(SRC, "views/InteractiveView.vue"), "utf8");
const conversation = readFileSync(
  resolve(SRC, "components/interactive/v2/InteractiveConversation.vue"),
  "utf8",
);

function block(source: string, selector: string): string {
  const start = source.indexOf(selector + " {");
  if (start < 0) return "";
  const end = source.indexOf("\n}", start);
  return end < 0 ? "" : source.slice(start, end + 2);
}

describe("操作行：左模式切换 / 中常驻把手 / 右板面工具", () => {
  it("DOM 顺序：模式切换槽 → 把手 → 工具槽；同一页只有一根把手、没有独立收起按钮", () => {
    const mode = conversation.indexOf('<slot name="mode-switch"');
    const lever = conversation.indexOf("data-ic-toggle");
    const toolbar = conversation.indexOf('<slot name="toolbar"');
    expect(mode).toBeGreaterThan(-1);
    expect(lever).toBeGreaterThan(mode);
    expect(toolbar).toBeGreaterThan(lever);
    expect(conversation.indexOf("data-ic-close")).toBe(-1);
    expect(conversation.split("data-ic-toggle").length - 1).toBe(1);
    // 旧布局的收起条 / 分隔条 / 输入区模式槽不复存在
    expect(conversation).not.toContain('class="ic-bar"');
    expect(conversation).not.toContain("composer-leading");
    expect(conversation).not.toContain('name="header-extra"');
  });

  it("把手：绝对定位于操作行水平正中（左右内容宽度变化不影响居中），扁长外形", () => {
    const lever = block(conversation, ".ic-lever");
    expect(lever).toContain("position: absolute");
    expect(lever).toContain("left: 50%");
    expect(lever).toContain("transform: translateX(-50%)");
    expect(lever).toContain("touch-action: none");
    const bar = block(conversation, ".ic-lever-bar");
    expect(bar).toContain("height: 5px");
    expect(bar).toContain("border-radius: 4px");
    // 展开态可拖动、拖动中强调色
    expect(conversation).toContain('[data-ic-expanded="true"] .ic-lever');
    expect(conversation).toContain('[data-ic-dragging="true"] .ic-lever-bar');
  });

  it("操作行透明、左右两组等宽让位：右侧工具量到确定可用宽度", () => {
    const rail = block(conversation, ".ic-rail");
    expect(rail).toContain("background: transparent");
    expect(rail).toContain("justify-content: space-between");
    expect(conversation).toContain("--ic-rail-side");
    const right = block(conversation, ".ic-rail-right");
    expect(right).toContain("width: var(--ic-rail-side");
  });

  it("模式切换在页面接线里进入 #mode-switch 槽，工具栏进入 #toolbar 槽并走 rail 形态", () => {
    expect(view).toContain("<template #mode-switch>");
    expect(view).toContain("ConversationModeSwitch");
    expect(view).toContain("<template #toolbar>");
    expect(view).toContain("BoardToolbar");
    expect(view).toMatch(/<BoardToolbar[\s\S]{0,200}rail/);
    expect(view).not.toContain("<template #composer-leading>");
  });
});

describe("顶边线框与玻璃：面板范围 / 镜像路径 / 胶囊中线淡出", () => {
  it("两条镜像 SVG 开放路径按弧长揭示（同一条路径字符串来源）", () => {
    expect(conversation).toContain("data-ic-frame-path=\"left\"");
    expect(conversation).toContain("data-ic-frame-path=\"right\"");
    expect(conversation).toContain("strokeDasharray");
    expect(conversation).toContain("conversationFrameGeometry");
    // 镜像：左路径从中心左侧出发、右路径从中心右侧出发，圆弧后向下
    expect(conversation).toMatch(/leftPath|rightPath/);
  });

  it("玻璃只在聊天面板（::before 之外的独立层），按面板范围裁 14px 顶角、底边开口", () => {
    const glass = block(conversation, ".ic-glass");
    expect(glass).toContain("position: absolute");
    expect(glass).toContain("backdrop-filter: blur(16px)");
    expect(glass).toContain("clip-path: inset(");
    expect(glass).toContain("var(--ic-glass-inset");
    expect(glass).toContain("var(--qio-ui-radius)");
    expect(glass).toContain("bottom: 0");
    // 材质只有这一层：消息区不再各自叠模糊（FU7 的旧做法已随重做移除）
    expect(conversation).not.toContain("backdrop-filter: blur(26px)");
  });

  it("淡出终点以输入胶囊实测中线为准；线框高度 = 胶囊 top + 高度/2", () => {
    expect(conversation).toContain(".input-pill");
    expect(conversation).toContain("composerCapsuleRect");
    expect(conversation).toContain("inputTop");
    expect(conversation).toContain("inputHeight");
  });
});

describe("动效与降级：出现/消失同速非线性；消息随把手升降并虚化；减少动画直接落最终几何", () => {
  it("线框与高度共用一条 easeInOutCubic 时间轴（存在与消失同速，时长常量在模块内单测）", () => {
    expect(conversation).toContain("conversationMotion");
    expect(conversation).toContain("easeInOutCubic");
    expect(conversation).toContain("CONVERSATION_MOTION");
    expect(conversation).toContain("progressAnim");
    // 高度与揭示在同一帧内先后更新（同一 time step），不是两条独立 CSS transition
    expect(conversation).toContain("drawFrame()");
    // 旧的「展开 11 / 收起 21」两条独立弹簧轴已移除
    expect(conversation).not.toContain("stepMotionAxis");
    expect(conversation).not.toContain("motionRateFor");
    expect(conversation).not.toContain("heightAxis");
    expect(conversation).not.toContain("revealAxis");
  });

  it("消息流关闭浏览器滚动锚定：开合时消息内容与把手同速升降", () => {
    expect(conversation).toContain("overflow-anchor: none");
  });

  it("消息内容随下沉虚化：与开合同一进度，驱动 .ic-stream 的 opacity/filter", () => {
    expect(conversation).toContain("conversationContentMist");
    expect(conversation).toContain("dataset.icMist");
    expect(conversation).toContain("stream.style.opacity");
    expect(conversation).toContain("blur(");
  });

  it("消息内容固定在面板顶边下方、与把手同速升降（不垂直居中，把手永远在消息上方）", () => {
    const empty = conversation.slice(conversation.indexOf(".ic-stream :deep(.empty)"));
    expect(empty.slice(0, 400)).toContain("inset: 0 0 auto 0");
    expect(empty.slice(0, 400)).toContain("justify-content: flex-start");
    expect(empty.slice(0, 400)).toContain("var(--ic-empty-top, 40px)");
    // 点击开合降低到同一条时间轴：收起 / 展开走同一个 startMotion
    expect(conversation).toContain("startMotion()");
  });

  it("减少动画：监听 prefers-reduced-motion，直接落位 + 极短淡入淡出", () => {
    expect(conversation).toContain("prefers-reduced-motion");
    expect(conversation).toContain('data-ic-reduced');
    expect(conversation).toContain('[data-ic-reduced="true"] .ic-glass');
  });

  it("面板宽度与消息列/输入胶囊同轴（页面浮层 860）", () => {
    expect(view).toMatch(/width:\s*min\(calc\(100% - 24px\), 860px\)/);
  });
});