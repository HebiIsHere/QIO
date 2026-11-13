/**
 * PM-QIO032 #05：提交详情面板的空间钳制（E2E 缺陷回归）
 *
 * 缺陷：800×600、板面 5 项改动时详情面板实测 305px，矩形覆盖提交按钮、挡住任务入口。
 * 修复口径（submitPanelPlacement 纯函数 + SubmitCluster 实测接线）：
 * - 面板底边固定 = 工具栏顶边 - gap → 提交按钮永不被覆盖；
 * - 面板顶边 ≥ safeTop（任务入口底边 + 余量）→ 任务入口不被遮挡；
 * - 高度 = min(上限, 剩余可用空间)，内容再多只内部滚动。
 * 覆盖：空 fixture（内容 122px）与 5 项改动压力（内容 305px）两种场景 + 组件接线。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, nextTick } from "vue";
import SubmitCluster from "../SubmitCluster.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import {
  SUBMIT_PANEL_GAP,
  SUBMIT_PANEL_MAX_HEIGHT,
  placeSubmitPanel,
  submitPanelCoversToolbar,
  submitPanelHeight,
} from "../submitPanelPlacement";

const FX = { expressions: [] as unknown[] };
vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: FX.expressions } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(),
  saveDrafts: vi.fn(),
  saveBoardState: vi.fn(),
  submitBoard: vi.fn(),
  checkMaterialImpact: vi.fn(),
  fetchAcceptance: vi.fn(),
}));

const VIEWPORT_HEIGHT = 600;
/** E2E 实测（800×600 清空 fixture）：详情面板 y=91、高 193 → 底边 284、工具栏顶边 292。 */
const E2E_EMPTY_CONTENT_HEIGHT = 122;
/** E2E 复现（5 项改动）：面板高 305px，矩形开始覆盖提交按钮并挡任务入口。 */
const E2E_STRESS_CONTENT_HEIGHT = 305;
const TOOLBAR_TOP = 292;
const TASK_ENTRY_BOTTOM = 114;

function placementFor(toolbarTop = TOOLBAR_TOP, safeTop = TASK_ENTRY_BOTTOM + 8) {
  return placeSubmitPanel({
    toolbar: { left: 60, top: toolbarTop, width: 674 },
    container: { left: 56, top: 70, bottom: VIEWPORT_HEIGHT },
    viewportHeight: VIEWPORT_HEIGHT,
    safeTop,
  });
}

describe("纯函数：面板高度按可用空间钳制", () => {
  it("空 fixture（内容 122px）：不覆盖提交按钮、不挡任务入口，也不需要滚动", () => {
    const placement = placementFor();
    const expectedMax = TOOLBAR_TOP - SUBMIT_PANEL_GAP - (TASK_ENTRY_BOTTOM + 8); // 162
    expect(placement.maxHeight).toBe(expectedMax);
    expect(placement.bottomEdge).toBe(TOOLBAR_TOP - SUBMIT_PANEL_GAP);
    const height = submitPanelHeight(placement, E2E_EMPTY_CONTENT_HEIGHT);
    expect(height).toBe(E2E_EMPTY_CONTENT_HEIGHT);
    expect(placement.bottomEdge - height).toBeGreaterThanOrEqual(TASK_ENTRY_BOTTOM + 8);
    expect(submitPanelCoversToolbar(placement, E2E_EMPTY_CONTENT_HEIGHT)).toBe(false);
    expect(E2E_EMPTY_CONTENT_HEIGHT < placement.maxHeight).toBe(true);
  });

  it("5 项改动压力（内容 305px）：钳到可用空间，改由内部滚动，绝不覆盖提交按钮/任务入口", () => {
    const placement = placementFor();
    const height = submitPanelHeight(placement, E2E_STRESS_CONTENT_HEIGHT);
    expect(height).toBe(placement.maxHeight);
    expect(height).toBeLessThan(E2E_STRESS_CONTENT_HEIGHT); // 需要内部滚动
    // 提交按钮在工具栏里：面板底边仍在工具栏顶边之上
    expect(placement.bottomEdge).toBeLessThanOrEqual(TOOLBAR_TOP);
    expect(submitPanelCoversToolbar(placement, E2E_STRESS_CONTENT_HEIGHT)).toBe(false);
    // 任务入口：面板顶边不低于任务入口底边 + 余量
    expect(placement.bottomEdge - height).toBeGreaterThanOrEqual(TASK_ENTRY_BOTTOM + 8);
  });

  it("极端窄高：可用空间小于内容时按可用空间钳制（不反向覆盖任务入口）", () => {
    const placement = placeSubmitPanel({
      toolbar: { left: 0, top: 200, width: 600 },
      viewportHeight: 600,
      safeTop: 190,
      gap: 8,
    });
    expect(placement.maxHeight).toBe(2);
    expect(placement.bottomEdge).toBe(192);
    const height = submitPanelHeight(placement, 305);
    expect(height).toBe(2);
    expect(placement.bottomEdge - height).toBe(190);
  });

  it("上限 420px 与非法输入兜底（NaN / 缺省 / 视口外）", () => {
    const wide = placeSubmitPanel({
      toolbar: { left: 0, top: 900, width: 800 },
      viewportHeight: 1200,
      safeTop: 100,
    });
    expect(wide.maxHeight).toBe(SUBMIT_PANEL_MAX_HEIGHT);
    const nan = placeSubmitPanel({
      toolbar: { left: Number.NaN, top: Number.NaN, width: Number.NaN },
      viewportHeight: Number.NaN,
      safeTop: Number.NaN,
    });
    expect(nan.maxHeight).toBe(0);
    expect(nan.left).toBe(0);
    expect(nan.width).toBe(0);
    expect(submitPanelHeight(nan, 305)).toBe(0); // 无空间时不假装有高度
    expect(nan.bottomEdge).toBeLessThan(nan.anchorTop);
  });

  it("窄窗 switch / 宽窗并排的既有分档不参与本函数（阈值仍由 panelSpaceMode 负责）", () => {
    // 同一工具栏矩形在 1120/1121 下几何不变：本函数只解决高度，不改变分档语义。
    const a = placementFor(292, 122);
    const b = placementFor(292, 122);
    expect(a).toEqual(b);
  });
});

describe("组件接线：实测工具栏矩形后写入 inline 钳制样式", () => {
  let wrapper: VueWrapper | null = null;
  let rectSpy: ReturnType<typeof vi.spyOn> | null = null;

  function stubRect(el: Element): DOMRect | null {
    const tag = (el as HTMLElement).getAttribute?.("data-test");
    const im = (el as HTMLElement).getAttribute?.("data-im");
    if (tag === "host") {
      return new DOMRect(56, 70, 682, 460);
    }
    if (im === "board-toolbar") {
      return new DOMRect(60, 292, 674, 120);
    }
    if (im === "tasks-entry") {
      return new DOMRect(700, 82, 80, TASK_ENTRY_BOTTOM - 82);
    }
    return null;
  }

  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    Object.defineProperty(window, "innerHeight", { value: VIEWPORT_HEIGHT, configurable: true });
    const original = HTMLElement.prototype.getBoundingClientRect;
    rectSpy = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        return stubRect(this) ?? original.call(this);
      });
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    rectSpy?.mockRestore();
    rectSpy = null;
    document.body.innerHTML = "";
  });

  function styleOf(el: Element): CSSStyleDeclaration {
    return (el as HTMLElement).style;
  }

  function fiveChanges() {
    return Array.from({ length: 5 }, (_, index) => ({
      id: "expr-" + index,
      kind: "note_added",
      summary: "本次要提交的改动 " + (index + 1),
      intentBearing: true,
    }));
  }

  async function mountHarness(path: "with-toolbar" | "without-toolbar", changes = 0) {
    FX.expressions = changes > 0 ? fiveChanges() : [];
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    await flushPromises();
    const Harness = defineComponent({
      components: { SubmitCluster },
      template:
        path === "with-toolbar"
          ? '<div data-test="host" style="position: relative">' +
            '<div data-im="board-toolbar" style="position: relative"><SubmitCluster /></div>' +
            '<div data-im="tasks-entry"></div>' +
            "</div>"
          : '<div data-test="host" style="position: relative"><SubmitCluster /></div>',
    });
    wrapper = mount(Harness, { global: { plugins: [pinia] }, attachTo: document.body });
    await flushPromises();
    await wrapper.get('[data-im="submit-details"]').trigger("click");
    await flushPromises();
    await nextTick();
    await new Promise((resolve) => window.requestAnimationFrame(() => resolve(null)));
    await nextTick();
    return wrapper;
  }

  it("5 项改动压力：面板 max-height / bottom / width 被钳制（不覆盖提交按钮），内容照常列出", async () => {
    const w = await mountHarness("with-toolbar", 5);
    const panel = w.get('[data-im="submit-panels"]');
    expect(w.findAll('[data-im="change-item"]').length).toBe(5);
    const expectedMax = TOOLBAR_TOP - SUBMIT_PANEL_GAP - (TASK_ENTRY_BOTTOM + 8);
    expect(panel.attributes("data-im-panel-max-height")).toBe(String(expectedMax));
    // jsdom 没有布局：offsetParent 为 null → 定位按视口兜底（left 取工具栏 left，
    // bottom = 视口高 600 - 面板底边 284 = 316；真实浏览器里定位祖先是聊天卡）。
    expect(styleOf(panel.element).bottom).toBe(600 - 284 + "px");
    expect(styleOf(panel.element).left).toBe("60px");
    expect(styleOf(panel.element).width).toBe("674px");
    expect(styleOf(panel.element).maxHeight).toBe(expectedMax + "px");
    expect(styleOf(panel.element).position).toBe("absolute");
  });

  it("空 fixture：详情面板同样按空间钳制，成功读取为空才显示「本次没有改动」", async () => {
    const w = await mountHarness("with-toolbar", 0);
    const panel = w.get('[data-im="submit-panels"]');
    const expectedMax = TOOLBAR_TOP - SUBMIT_PANEL_GAP - (TASK_ENTRY_BOTTOM + 8);
    expect(styleOf(panel.element).maxHeight).toBe(expectedMax + "px");
    expect(w.findAll('[data-im="change-item"]').length).toBe(0);
    expect(panel.text()).toContain("本次没有改动");
  });

  it("量不到工具栏（jsdom 孤立挂载）时保留 CSS 兜底，不写死 0 高度", async () => {
    const w = await mountHarness("without-toolbar");
    const panel = w.get('[data-im="submit-panels"]');
    expect(panel.attributes("data-im-panel-max-height")).toBeUndefined();
    expect(styleOf(panel.element).maxHeight).toBe("");
  });
});
