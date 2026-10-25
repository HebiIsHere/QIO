/**
 * 卡片边框：单条 SVG 环形路径（2026-10-24 交互演示最终稿）。
 *
 * 设计口径（docs/interactive-v2/qio-card-interaction-demo.html 的 .frame / ringPath）：
 * - 边框不再由 CSS border + ::after 粗条拼出来，而是**一条** SVG path：
 *   外轮廓 + 内轮廓两条闭合子路径，fill-rule=evenodd 只留下环带；
 * - 环带平时 1px，越靠近右下角越粗（角上约 3px），圆角沿轮廓连续；
 * - 尺寸直接跟随卡片 w/h；折叠 / 编辑态也必须有边框，不能被状态切掉；
 * - 颜色只在 CSS 里换 fill：平时浅色、悬停提一档、选中主题色。
 *
 * 标注：【组件/DOM + 纯几何】挂载真实 BoardCard.vue；几何函数与组件同源（同一个命名导出）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { nextTick } from "vue";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard, {
  CARD_FRAME_MAX_THICKNESS,
  CARD_FRAME_MIN_THICKNESS,
  cardFramePath,
  cardFrameThickness,
} from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-24T00:00:00.000Z";
const SFC = readFileSync(resolve(process.cwd(), "src/components/interactive/BoardCard.vue"), "utf8");

function mkCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id: "c_frame",
    kind: "text",
    content: "边框测试",
    meta: {},
    x: 0,
    y: 0,
    w: 320,
    h: 220,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

let store: ReturnType<typeof useInteractiveStore>;
let wrapper: VueWrapper | null = null;

function mountCard(card: BoardCardModel, selected = true): VueWrapper {
  store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] };
  wrapper = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card,
      selected,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: card.y,
      multi: false,
      connecting: false,
    },
  });
  return wrapper;
}

function frame(w: VueWrapper) {
  return w.get('[data-im="card-frame"]');
}

function framePath(w: VueWrapper): string {
  return w.get('[data-im="card-frame-path"]').attributes("d") ?? "";
}

function subpathCount(d: string): number {
  return (d.match(/M /g) ?? []).length;
}

function closedSubpathCount(d: string): number {
  return (d.match(/Z/g) ?? []).length;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("单一路径：外轮廓 + 内轮廓，fill-rule=evenodd", () => {
  it("渲染一个 evenodd 的 SVG 环形路径，viewBox 跟随卡片 w/h", async () => {
    const card = mkCard({ w: 320, h: 220 });
    const w = mountCard(card);

    expect(frame(w).attributes("viewBox")).toBe("0 0 320 220");
    const path = w.get('[data-im="card-frame-path"]');
    expect(path.attributes("fill-rule")).toBe("evenodd");
    const d = framePath(w);
    expect(subpathCount(d)).toBe(2);
    expect(closedSubpathCount(d)).toBe(2);
    expect(d).toContain("M 14 0");
    expect(d).toContain("M 14 1");
    expect(d).toContain("A 14 14 0 0 1 320 14");
    expect(d).toContain("A 13 13 0 0 1 319 14");
    expect(d).not.toMatch(/NaN|undefined|Infinity/);

    await w.setProps({ card: { ...card, w: 400, h: 260 } });
    expect(frame(w).attributes("viewBox")).toBe("0 0 400 260");
    const resized = framePath(w);
    expect(resized).not.toBe(d);
    expect(resized).toContain("M 14 0");
    expect(resized).toContain("M 14 1");
    expect(resized).toContain("400");
  });

  it("折叠与编辑态都保留边框（状态只换内容，不摘边框）", async () => {
    const card = mkCard({ w: 300, h: 200 });
    const w = mountCard(card);

    await w.setProps({ card: { ...card, folded: true } });
    expect(w.find('[data-im="card-frame"]').exists()).toBe(true);
    expect(framePath(w)).toContain("M 14 0");

    await w.setProps({ card });
    await w.get('[data-im="card-edit"]').trigger("click");
    expect(w.find('[data-im="card-frame"]').exists()).toBe(true);
    expect(frame(w).attributes("viewBox")).toBe("0 0 300 200");
  });

  it("折叠态高度由真实布局决定：自动高度不会把边框停在旧 h", async () => {
    const card = mkCard({ w: 300, h: 200 });
    const w = mountCard(card);
    const element = w.get('[data-im="card"]').element as HTMLElement;
    Object.defineProperty(element, "offsetWidth", { value: 300, configurable: true });
    Object.defineProperty(element, "clientWidth", { value: 300, configurable: true });
    Object.defineProperty(element, "offsetHeight", { value: 52, configurable: true });
    Object.defineProperty(element, "clientHeight", { value: 52, configurable: true });

    await w.setProps({ card: { ...card, folded: true } });
    await nextTick();
    await nextTick();
    expect(frame(w).attributes("viewBox")).toBe("0 0 300 52");
  });

  it("最小卡片（180×96）也生成闭合的双轮廓，圆角收敛到可用范围", () => {
    const d = cardFramePath(180, 96);
    expect(subpathCount(d)).toBe(2);
    expect(closedSubpathCount(d)).toBe(2);
    expect(d).toContain("M 14 0");
    expect(d).toContain("M 14 1");
    expect(d).not.toMatch(/NaN|undefined|Infinity/);
  });
});

describe("厚度：1px 起，越靠近右下角越粗（约 3px），连续无跳变", () => {
  it("上 / 左边 1px，右下角约 3px，离开右下角后回到 1px", () => {
    const w = 320;
    const h = 220;

    expect(CARD_FRAME_MIN_THICKNESS).toBeCloseTo(1, 6);
    expect(CARD_FRAME_MAX_THICKNESS).toBeGreaterThanOrEqual(2.8);
    expect(CARD_FRAME_MAX_THICKNESS).toBeLessThanOrEqual(3.2);

    expect(cardFrameThickness(w, h, w / 2, 0)).toBeCloseTo(1, 6);
    expect(cardFrameThickness(w, h, 0, h / 2)).toBeCloseTo(1, 6);
    expect(cardFrameThickness(w, h, w / 2, h)).toBeCloseTo(1, 6);
    expect(cardFrameThickness(w, h, w, 0)).toBeCloseTo(1, 6);
    expect(cardFrameThickness(w, h, w, h)).toBeCloseTo(CARD_FRAME_MAX_THICKNESS, 6);
    expect(cardFrameThickness(w, h, w, h - 30)).toBeGreaterThan(1.2);
    expect(cardFrameThickness(w, h, w, h - 30)).toBeLessThan(CARD_FRAME_MAX_THICKNESS);
  });

  it("从右下角沿对角线向内采样，厚度连续变化（不出现双线 / 断角式的跳变）", () => {
    const w = 320;
    const h = 220;
    const step = 0.25;
    let previous = cardFrameThickness(w, h, w, h);
    for (let i = 1; i <= 240; i += 1) {
      const next = cardFrameThickness(w, h, w - i * step, h - i * step);
      // 平滑函数在这条对角线上的最大斜率约 0.082 / px；0.25px 步长的理论最大差 ≈ 0.021
      expect(Math.abs(next - previous), "厚度在第 " + i + " 个采样点跳变").toBeLessThan(0.03);
      previous = next;
    }
    expect(previous).toBeCloseTo(1, 6);
  });
});

describe("封面 / 图片 / 编辑态的层叠安全", () => {
  it("封面模式下边框层仍绝对定位，不被 .card.is-cover 的直系子规则拉回文档流", () => {
    expect(SFC).toContain(".card.is-cover > :not(.card-cover):not(.card-frame)");
  });

  it("颜色只换 fill：平时浅色、悬停提一档、选中主题色", () => {
    expect(SFC).toMatch(/\.card-frame-path\s*\{[^}]*fill:\s*var\(--border-subtle\)/);
    expect(SFC).toMatch(/\.card:hover \.card-frame-path\s*\{[^}]*fill:\s*var\(--border-strong\)/);
    expect(SFC).toMatch(/\.card\.selected \.card-frame-path\s*\{[^}]*fill:\s*var\(--accent\)/);
    // 旧的“CSS border + 右下角 ::after 粗条”必须不再显示
    expect(SFC).not.toMatch(/\.card::after\s*\{/);
    expect(SFC).not.toMatch(/border-right:\s*3px solid/);
    expect(SFC).not.toMatch(/border-bottom:\s*3px solid/);
  });
});
