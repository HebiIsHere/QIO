/**
 * 滚动策略守卫（2026-10-14 · 全局滚动体验 / task-4 / task-7）。
 *
 * 这些扫描测试守的是「会慢慢退回去的东西」：
 * 1. 原生滚动条外观与自绘滚动条（::-webkit-scrollbar / scrollbar-width）不再出现；
 * 2. 同一个连续阅读流里不再切出第二层固定高度的小滚动窗口；
 * 3. 需要滚动的区域必须接入共享策略（v-qio-scrollbar / data-qio-fit）；
 * 4. 星球页的横向溢出按「修布局 + 去掉横向滚动能力」处理。
 *
 * 判定口径（实测，Chrome）：
 * - overflow:clip 两轴 → 不建立滚动容器，scrollLeft=500 不动（moved=0）；
 * - overflow-x:clip + overflow-y:auto → overflow-x 计算成 hidden，仍可被程序性滚动，
 *   所以「纵向滚动容器」上的 data-qio-fit 只是锁手势 + 不画条，不等于不可横向滚动，
 *   这类区域的横向判定必须靠内容宽度（scrollWidth == clientWidth），见 PlanetView 的用例。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
function read(rel: string): string {
  // 注释里会引用被删掉的旧写法（例如「不再自己写 webkit 滚动条」），
  // 所以断言前先剥掉块注释，避免把说明文字当成实现。
  return readFileSync(resolve(SRC, rel), "utf8")
    .split("/*")
    .map((part) => {
      const end = part.indexOf("*/");
      return end >= 0 ? part.slice(end + 2) : part;
    })
    .join("");
}
/** 取某条选择器的规则体（不跨 \} ） */
function ruleBody(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp(escaped + "\\s*\\{([^}]*)\\}").exec(source);
  return m ? m[1] : "";
}

const FAILED_SEND = "components/interactive/FailedSendNotice.vue";

describe("task-7：失败恢复浮层的滚动层收敛（FailedSendNotice）", () => {
  it("四个选择器都不再自开滚动层（max-height + overflow-y 的组合已移除）", () => {
    const src = read(FAILED_SEND);
    for (const sel of [".failed-send", ".recovery", ".pending", ".quote"]) {
      const body = ruleBody(src, sel);
      expect(body, sel + " 规则必须存在").not.toBe("");
      expect(body, sel + " 不应再有 max-height（会在同一阅读流里再切一层小滚动窗口）").not.toContain("max-height");
      expect(body, sel + " 不应再有 overflow-y").not.toContain("overflow-y");
      expect(body, sel + " 不应再有 overscroll-behavior").not.toContain("overscroll-behavior");
    }
  });

  it("恢复入口仍然带边界保护；规模由「默认 3 条 + 查看其余 N 条」控制", () => {
    const src = read(FAILED_SEND);
    expect(src).toContain("data-qio-fit");
    expect(src).toContain("DEFAULT_SHOWN");
    expect(src).toContain("查看其余");
  });
});

describe("恢复/审批正文的 scroll 层数（task-7 连带范围）", () => {
  it("RecoveryInbox 没有自开滚动层，长原文默认简述 + 就地展开", () => {
    const src = read("components/RecoveryInbox.vue");
    expect(src).not.toContain("overflow-y: auto");
    expect(src).not.toContain("max-height");
    expect(src).toContain("data-qio-fit");
    expect(src).toContain("展开原文");
    expect(src).toContain("收起原文");
  });

  it("ApprovalFacts 的技术明细 / raw 走 content 策略（不在审批正文里开第二层滚动）", () => {
    const src = read("components/ApprovalFacts.vue");
    for (const sel of [".af-pre", ".adv-raw"]) {
      const body = ruleBody(src, sel);
      expect(body, sel + " 规则必须存在").not.toBe("");
      expect(body).not.toContain("max-height");
      expect(body).not.toContain("overflow:");
    }
    expect(src).toContain("data-qio-fit");
  });

  it("ApprovalModal：正文是唯一滚动层，按钮区在正文之外（固定操作不被滚走）", () => {
    const src = read("components/ApprovalModal.vue");
    const body = ruleBody(src, ".body");
    expect(body).toContain("max-height");
    expect(body).toContain("overflow-y: auto");
    expect(src).toContain('class="body" v-qio-scrollbar data-qio-fit');
    // 模板顺序：.body 先出现、.actions 在后 —— 按钮在滚动区外
    expect(src.indexOf('class="body"')).toBeLessThan(src.indexOf('class="actions"'));
  });

  it("ImpactConfirmDialog：滚动收敛到 .body 一层，操作区固定在框内", () => {
    const src = read("components/interactive/ImpactConfirmDialog.vue");
    expect(ruleBody(src, ".dialog")).toContain("overflow: clip");
    const body = ruleBody(src, ".body");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("flex: 1 1 auto");
    expect(src).toContain('class="body" v-qio-scrollbar data-qio-fit');
    expect(ruleBody(src, ".actions")).toContain("flex: none");
  });
});

describe("统一滚动策略：不再有原生/自绘滚动条", () => {
  const converted = [
    "components/DevTaskEntry.vue",
    "components/ui/QCombo.vue",
    "components/ui/QSelect.vue",
    "components/interactive/IntentBatchTray.vue",
    "components/interactive/IntentPreviewCard.vue",
    "components/interactive/IntentStatusPopover.vue",
    "components/interactive/SubmitCluster.vue",
    "components/planet/EntityPanel.vue",
    "components/planet/KnowledgePanel.vue",
    "components/onboarding/OnboardingWizard.vue",
    "views/settings/CredentialModal.vue",
    "views/DebugView.vue",
    "views/PlanetView.vue",
    "views/SettingsView.vue",
  ];

  it("没有任何一处自绘滚动条（::-webkit-scrollbar / scrollbar-width / scrollbar-color）", () => {
    const hits = converted.filter((rel) => {
      const src = read(rel);
      return /::-webkit-scrollbar|scrollbar-width\s*:|scrollbar-color\s*:/.test(src);
    });
    expect(hits).toEqual([]);
  });

  it("声明了纵向滚动的区域都接入了共享策略（指令 / 组合式 / 容器组件）", () => {
    const missing = converted.filter((rel) => {
      const src = read(rel);
      if (!/overflow-y:\s*auto/.test(src) && !/class="qio-scroll-area"/.test(src)) return false;
      return !/v-qio-scrollbar|useQioScrollbars|QioScrollArea|qio-scroll-area/.test(src);
    });
    expect(missing).toEqual([]);
  });

  it("DevTaskEntry 删掉了局部 ::-webkit-scrollbar{width:8px} 那一套，改用共享策略", () => {
    const src = read("components/DevTaskEntry.vue");
    expect(src).not.toContain("::-webkit-scrollbar");
    expect(src).not.toContain("scrollbar-width");
    expect(src).toContain('class="dev-task-list"');
    expect(src).toContain("v-qio-scrollbar");
  });
});

describe("下拉 / 浮层：列表滚动走统一指示条、正文不横向滚", () => {
  it("QCombo 菜单列表与 QSelect 浮层都接入共享策略且只允许纵向滚动", () => {
    const combo = read("components/ui/QCombo.vue");
    expect(combo).toContain("v-qio-scrollbar");
    expect(combo).toContain("data-qio-fit");
    expect(ruleBody(combo, ".qio-combo-list")).toContain("overflow-y: auto");
    expect(ruleBody(combo, ".qio-combo-list")).not.toContain("overflow: auto");

    const select = read("components/ui/QSelect.vue");
    expect(select).toContain("v-qio-scrollbar");
    expect(select).toContain("data-qio-fit");
    const menu = ruleBody(select, ".qio-select-menu");
    expect(menu).toContain("overflow-y: auto");
    // 浮层必须仍是绝对的浮层（指令会补 .qio-scroll-host，需要组件锁死定位）
    expect(menu).toContain("position: absolute");
    expect(menu).toContain("min-width: 0");
  });
});

describe("星球页（D6）：横向溢出按「修布局 + 去掉横向滚动能力」处理", () => {
  const src = read("views/PlanetView.vue");

  it("整层与面板布局容器改成两轴 clip（不再是可被程序性滚动的容器）", () => {
    expect(ruleBody(src, ".planet-view")).toContain("overflow: clip");
    expect(ruleBody(src, ".panel-inner")).toContain("overflow: clip");
    expect(ruleBody(src, ".panel-body")).toContain("overflow: clip");
  });

  it("话题列表 / 详情保留各自纵向滚动，横向锁死；面板正文一律可换行", () => {
    for (const sel of [".topic-scroll", ".detail-scroll"]) {
      const body = ruleBody(src, sel);
      expect(body, sel).toContain("overflow-y: auto");
      expect(body, sel).toContain("overflow-x: clip");
    }
    // 根因修复：内容宽度不超过容器（否则 overflow-x 会退化成 hidden，仍能左右滚）
    expect(src).toContain("overflow-wrap: anywhere");
    expect(src).toMatch(/\.panel-inner :where\([^)]*\)\s*\{/);
    expect(ruleBody(src, ".tabs")).toContain("flex-wrap: wrap");
    expect(ruleBody(src, ".topic-list .name")).toContain("min-width: 0");
  });

  it("三处滚动区域都接入共享策略（指示条），且不再用 overflow:hidden 兜底横向", () => {
    expect(src).toContain("v-qio-scrollbar");
    expect(ruleBody(src, ".topic-scroll")).not.toContain("overflow: hidden");
    expect(ruleBody(src, ".planet-view")).not.toContain("overflow: hidden");
  });
});

describe("调试视图：一层正文滚动 + 日志软换行（不留横向滚动）", () => {
  it(".debug 是唯一纵向滚动层，.json 不再 overflow-x:auto、也不再限高", () => {
    const src = read("views/DebugView.vue");
    const root = ruleBody(src, ".debug");
    expect(root).toContain("overflow-y: auto");
    expect(root).toContain("overflow-x: clip");
    expect(src).toContain('class="debug" v-qio-scrollbar data-qio-fit');
    const json = ruleBody(src, ".json");
    expect(json).not.toContain("overflow-x: auto");
    expect(json).not.toContain("max-height");
    expect(json).toContain("overflow-wrap: anywhere");
  });
});

describe("折叠动画的纯裁剪盒不建立滚动容器（2026-10-14 收尾）", () => {
  /* overflow:hidden 的视觉裁切与 clip 一致，但它仍然是一个可被脚本/焦点滚动的容器
     （scrollTop/scrollLeft/scrollIntoView/focus 都能推动它）；这两处只做 0fr→1fr 折叠时的裁切，
     不该有滚动语义。改成 clip 后裁切行为不变，但不再有滚动容器。 */
  const boxes: [string, string][] = [
    ["components/ToolCreationCard.vue", ".detail-clip"],
    ["components/NarrativeStage.vue", ".ndrawer-clip"],
  ];

  it.each(boxes)("%s 的 %s 用 clip 而不是 hidden", (rel, sel) => {
    const body = ruleBody(read(rel), sel);
    expect(body, sel + " 规则必须存在").not.toBe("");
    expect(body, sel + " 不应再用 overflow: hidden（仍可被程序性滚动）").not.toContain("overflow: hidden");
    expect(body, sel + " 应为 overflow: clip").toContain("overflow: clip");
  });

  it("两处折叠盒里的宽内容靠换行适配（clip 只裁切，不提供滚动通道）", () => {
    // 工具创建卡：详情段落可任意位置换行
    expect(read("components/ToolCreationCard.vue")).toMatch(/\.detail p \{[^}]*overflow-wrap: anywhere/);
    // 叙事抽屉：正文（含插槽内容）可任意位置换行，调用行的名称/元信息可收缩
    const narrative = read("components/NarrativeStage.vue");
    expect(narrative).toMatch(/\.ndrawer-body :where\([^)]*\) \{[^}]*overflow-wrap: anywhere/);
    expect(narrative).toMatch(/\.callname,\s*\n\.callmeta \{ min-width: 0; \}/);
  });
});