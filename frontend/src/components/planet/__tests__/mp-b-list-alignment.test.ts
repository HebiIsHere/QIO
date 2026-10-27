/**
 * B · 列表卡的排版兜底（静态约定测试）。
 *
 * 背景：知识 / 实体 / 待处理 / 整理记录的列表卡根类都曾经同时挂全局行式类
 * qio-row。行式类带 align-items:center，而列表卡是**列方向**的 ——
 * 列方向下 align-items:center 就是「内容水平居中 + 缩窄」，实测正文 left 比卡片
 * 左边界缩进了 79px、卡片中间留大片空白。修法是列表卡不再依赖行式类，并显式
 * align-items: stretch 保证子元素铺满一行宽度、左对齐成一条竖线。
 *
 * 这里用**源码静态断言**兜住这三件事（vitest/jsdom 不注入 SFC scoped 样式，
 * 计算样式在单测里拿不到，真正的 getComputedStyle 验证在浏览器截图环节做）：
 * 1) 四个列表根类不再出现 qio-row；
 * 2) 每个根类显式 align-items: stretch + justify-content: flex-start；
 * 3) 各自保留 hover / 焦点表面（不能为了修居中把行式类给的视觉一起删掉）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * 从仓库里的真实源码读 SFC（vitest 的 cwd 是 frontend/）。
 * 不用 import.meta.url：jsdom 环境下它不是 file: URL，readFileSync 会直接拒绝。
 */
const COMPONENTS_DIR = resolve(process.cwd(), "src/components/planet");

function sourceOf(name: string): string {
  return readFileSync(resolve(COMPONENTS_DIR, name), "utf8");
}

/** 取某条规则的声明块：按「选择器 + 空格 + {」定位，取到下一个 } 为止。 */
function cssBlock(source: string, selector: string): string {
  const start = source.indexOf(selector + " {");
  if (start < 0) return "";
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  if (open < 0 || close < 0) return "";
  return source.slice(open + 1, close);
}

interface Case {
  file: string;
  root: string;
  /** 对应的 hover / 焦点规则（至少一条要在源码里） */
  surfaces: string[];
}

const CASES: Case[] = [
  { file: "KnowledgePanel.vue", root: ".k-item", surfaces: [".k-item:hover", ".k-item:focus-visible"] },
  { file: "EntityPanel.vue", root: ".e-item", surfaces: [".e-item:hover", ".e-item:focus-visible"] },
  { file: "PendingItems.vue", root: ".p-item", surfaces: [".p-item:hover", ".p-item:focus-visible"] },
  { file: "MaintenanceRecords.vue", root: ".mr-batch", surfaces: [".mr-batch:hover", ".mr-batch:focus-visible"] },
];

describe("列表卡排版：列方向必须 stretch + 左对齐，且不依赖全局行式类", () => {
  for (const testCase of CASES) {
    it(testCase.file + " 的 " + testCase.root + " 显式 stretch 且不再挂 qio-row", () => {
      const source = sourceOf(testCase.file);
      // 1) 列表卡的 class 里不再挂全局行式类（它的 align-items:center 会毁掉列方向布局）
      //    注释里提到 qio-row 说明原因不算违规，只查真实 class 属性。
      expect(source).not.toMatch(/class="[^"]*qio-row/);
      expect(source).not.toMatch(/:class="\[[^\]]*qio-row/);
      // 2) 列方向显式拉伸 + 左对齐
      const block = cssBlock(source, testCase.root);
      expect(block, testCase.root + " 缺少规则块").not.toBe("");
      expect(block).toContain("align-items: stretch");
      expect(block).toContain("justify-content: flex-start");
      // 3) 自己保留 hover / 焦点表面（不把视觉一起删掉）
      expect(testCase.surfaces.some((selector) => source.includes(selector))).toBe(true);
    });
  }

  it("整理记录的受影响条目 .mr-item 也要 stretch（默认折叠的差异区不歪）", () => {
    const source = sourceOf("MaintenanceRecords.vue");
    const block = cssBlock(source, ".mr-item");
    expect(block).toContain("align-items: stretch");
    expect(block).toContain("justify-content: flex-start");
  });
});

describe("知识列表：关联话题只出现一次，且长名单行省略", () => {
  it("有可点话题链接时不再打印「话题：X」的 scope 文本", () => {
    const source = sourceOf("KnowledgePanel.vue");
    expect(source).toContain("scopeLabelOf");
    expect(source).toContain('v-if="scopeLabelOf(k)"');
  });

  it("话题链接单行省略 + title/v-qio-tip 给全名", () => {
    const source = sourceOf("KnowledgePanel.vue");
    const block = cssBlock(source, ".k-topic-link");
    expect(block).toContain("text-overflow: ellipsis");
    expect(block).toContain("white-space: nowrap");
    expect(source).toContain(':title="k.topic_name"');
    expect(source).toContain('v-qio-tip="k.topic_name"');
  });
});

describe("列表元数据不能和详情元数据共用类名（同名会把列方向样式串到列表上）", () => {
  it("实体列表用 .e-item-meta，详情用 .e-detail-meta", () => {
    const source = sourceOf("EntityPanel.vue");
    expect(source).toContain('class="e-item-meta mono"');
    expect(source).toContain('class="e-detail-meta"');
    // 同一个 .e-meta 既被列表行用、又被详情列用 → 详情列的 column/center 会把列表文字居中
    expect(source).not.toMatch(/class="e-meta[ "]/);
    expect(source).not.toMatch(/^\s*\.e-meta\s*\{/m);
  });
});

describe("待处理列表：标题行是唯一主视觉（标签 → 标题 → 正文一条竖线）", () => {
  it("标题加粗、与正文同列左对齐", () => {
    const source = sourceOf("PendingItems.vue");
    const block = cssBlock(source, ".p-item-title");
    expect(block).toContain("font-weight: 600");
    expect(block).toContain("color: var(--text-strong)");
  });
});
