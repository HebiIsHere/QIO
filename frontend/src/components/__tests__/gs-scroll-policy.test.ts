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

  it("统一审批弹窗：正文是唯一滚动层，按钮区在正文之外（固定操作不被滚走）", () => {
    // 旧 components/ApprovalModal.vue 在 A-CP1 后由 C 删除；同一约束改由统一弹窗承接。
    const src = read("components/approval/UnifiedApprovalModal.vue");
    const body = ruleBody(src, ".ua-body");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("min-height: 0");
    expect(src).toContain('class="ua-body" v-qio-scrollbar');
    // 模板顺序：.ua-body 先出现、.ua-actions 在后 —— 按钮在滚动区外
    expect(src.indexOf('class="ua-body"')).toBeLessThan(src.indexOf('class="ua-actions"'));
  });
});

describe("统一滚动策略：不再有原生/自绘滚动条", () => {
  const converted = [
    "components/DevTaskEntry.vue",
    "components/ui/QCombo.vue",
    "components/ui/QSelect.vue",
    "components/interactive/SubmitCluster.vue",
    "components/planet/EntityPanel.vue",
    "components/planet/KnowledgePanel.vue",
    "components/onboarding/OnboardingWizard.vue",
    "views/DebugView.vue",
    "views/PlanetView.vue",
    "components/planet/MemoryTab.vue",
    "components/planet/GlobalSearchPanel.vue",
    "components/planet/ManagementArea.vue",
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
      if (!/overflow-y:\s*auto/.test(src)) return false;
      return !/v-qio-scrollbar|useQioScrollbars/.test(src);
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
  // 2026-10-27 改版：话题列表/概览/讨论记录搬进管理区的记忆标签页，星球页只保留舞台与管理面板
  const src = read("views/PlanetView.vue");

  it("整层与面板布局容器改成两轴 clip（不再是可被程序性滚动的容器）", () => {
    expect(ruleBody(src, ".planet-view")).toContain("overflow: clip");
    expect(ruleBody(src, ".panel-inner")).toContain("overflow: clip");
    expect(ruleBody(src, ".panel")).toContain("overflow: clip");
  });

  it("窄窗口：内容区是唯一纵向滚动层，横向裁切；面板正文一律可换行", () => {
    const stacked = ruleBody(src, ".planet-view.managing.stacked .planet-content");
    expect(stacked).toContain("overflow-y: auto");
    expect(stacked).toContain("overflow-x: clip");
    // 根因修复：内容宽度不超过容器（否则 overflow-x 会退化成 hidden，仍能左右滚）
    expect(src).toContain("overflow-wrap: anywhere");
    expect(src).toMatch(/\.panel-inner :where\([^)]*\)\s*\{/);
    // 管理区头部与标签在窄窗换行，不把面板撑宽
    const area = read("components/planet/ManagementArea.vue");
    expect(ruleBody(area, ".ma-head")).toContain("flex-wrap: wrap");
    expect(ruleBody(area, ".ma-tabs")).toContain("flex-wrap: wrap");
  });

  it("记忆标签页：列表 / 概览 / 讨论记录三层各自纵向滚动、横向锁死", () => {
    const mem = read("components/planet/MemoryTab.vue");
    const body = ruleBody(mem, ".mt-scroll");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("overflow-x: clip");
    expect(ruleBody(mem, ".topic-list .name")).toContain("min-width: 0");
    // 长摘要 / 长 id 可换行
    expect(mem).toContain("overflow-wrap: anywhere");
  });

  it("滚动区域都接入共享策略（指示条），且不再用 overflow:hidden 兜底横向", () => {
    expect(src).toContain("v-qio-scrollbar");
    expect(ruleBody(src, ".planet-view")).not.toContain("overflow: hidden");
    for (const rel of ["components/planet/MemoryTab.vue", "components/planet/GlobalSearchPanel.vue"]) {
      const s = read(rel);
      expect(s, rel).toContain("v-qio-scrollbar");
      // 原生滚动条外观与自绘滚动条在这两处同样不允许
      expect(s, rel).not.toMatch(/::-webkit-scrollbar|scrollbar-width\s*:/);
    }
    // 「不再用 overflow:hidden 兜底横向」只约束**滚动容器本身**：
    // 结果行第二行用 overflow:hidden + text-overflow 做单行省略，不是滚动层（这里按选择器判定）
    expect(ruleBody(read("components/planet/GlobalSearchPanel.vue"), ".gs-scroll")).not.toContain("overflow: hidden");
    expect(ruleBody(read("components/planet/MemoryTab.vue"), ".mt-scroll")).not.toContain("overflow: hidden");
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