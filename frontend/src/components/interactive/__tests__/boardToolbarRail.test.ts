/**
 * BoardToolbar rail 形态（互动页底部「操作行」，2026-11-17 互动底部操作区改版）的组件契约。
 *
 * 口径：
 * - rail=false（默认）时既有 dock 的 DOM / 行为不变（既有测试继续管，这里只做反向守卫）；
 * - rail=true 时根带 class is-rail + data-tb-rail="true" + data-tb-mode，仍保留 .tb-main 单行结构；
 * - 降级只由**工具栏自己的实测宽度**决定：jsdom 没有布局能力，所以 stub 根节点矩形再手动触发
 *   ResizeObserver 回调，模拟父容器（.ic-rail-right）把操作行压窄 / 放宽；
 * - full → icons：先省略能由图标说明的文字（添加 / 详情），其它工具不变；
 * - icons → more：撤销 / 重做 / 书签 / 提交详情收进「更多」，添加 + 提交 + 更多常驻行上；
 * - 「更多」面板：aria-haspopup / aria-expanded、Esc（共用层级栈）/ 外点关闭、焦点进第一项再回按钮；
 *   开合只改显示，不写板面数据。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import BoardToolbar from "../BoardToolbar.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardState } from "../../../interactive/types";

type RoCallback = ResizeObserverCallback;

/** jsdom 既没有 ResizeObserver 也不会自己触发尺寸变化：用可手动触发的替身。 */
class FakeResizeObserver {
  callback: RoCallback;
  constructor(callback: RoCallback) {
    this.callback = callback;
    roCallbacks.push(this);
  }
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const roCallbacks: FakeResizeObserver[] = [];
let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = board.emptyState("board_t");
  labels = [];
  // 模拟 store.commit 的效果（推撤销栈 + 保存），但跳过自动保存的定时器与网络
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
  roCallbacks.length = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

function mountRail(): VueWrapper {
  // attachTo：焦点断言需要元素真的在 document 里
  wrapper = mount(BoardToolbar, { props: { rail: true }, attachTo: document.body });
  return wrapper;
}

function railRect(width: number): DOMRect {
  return {
    width,
    height: 32,
    top: 0,
    left: 0,
    right: width,
    bottom: 32,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

/** 模拟父容器把操作行压到 width px（jsdom 无布局：写死矩形 + 手动触发 RO 回调）。 */
async function resizeRail(width: number): Promise<void> {
  const current = wrapper as VueWrapper;
  const el = current.get('[data-im="board-toolbar"]').element as HTMLElement;
  el.getBoundingClientRect = () => railRect(width);
  const ro = roCallbacks[roCallbacks.length - 1];
  expect(ro, "BoardToolbar 没有注册 ResizeObserver").toBeDefined();
  ro!.callback([], ro as unknown as ResizeObserver);
  await nextTick();
}

function modeOf(): string | undefined {
  return (wrapper as VueWrapper).get('[data-im="board-toolbar"]').attributes("data-tb-mode");
}

function activeHook(): string | null {
  return document.activeElement instanceof HTMLElement
    ? document.activeElement.getAttribute("data-im")
    : null;
}

describe("BoardToolbar rail 三档降级", () => {
  it("rail=false（默认）：不带 rail 标记、不渲染「更多」，既有 dock 钩子与分档不变", () => {
    wrapper = mount(BoardToolbar);
    const root = wrapper.get('[data-im="board-toolbar"]');
    expect(root.attributes("data-tb-rail")).toBeUndefined();
    expect(root.attributes("data-tb-mode")).toBeUndefined();
    expect(root.classes()).not.toContain("is-rail");
    // dock 的分档契约仍在（jsdom 量不到宽度 → 最宽档，不误判成最窄）
    expect(root.attributes("data-tb-band")).toBe("wide");
    expect(wrapper.find('[data-im="board-more"]').exists()).toBe(false);
    for (const hook of ["add-menu", "undo", "redo", "bookmarks-toggle", "submit"]) {
      expect(wrapper.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
  });

  it("rail 渲染：is-rail / data-tb-rail / data-tb-mode=full（量不到宽度保持 full），单行结构保留 .tb-main", () => {
    mountRail();
    const root = wrapper!.get('[data-im="board-toolbar"]');
    expect(root.attributes("data-tb-rail")).toBe("true");
    expect(root.classes()).toContain("is-rail");
    expect(root.attributes("data-tb-mode")).toBe("full");
    expect(root.attributes("role")).toBe("toolbar");
    expect(root.attributes("aria-label")).toBe("板面操作");
    expect(root.find(".tb-main").exists()).toBe(true);
    // full 档：添加 / 详情带文字
    expect(wrapper!.get('[data-im="add-menu"]').text()).toContain("添加");
    expect(wrapper!.get('[data-im="submit-details"]').text()).toContain("详情");
    // 「更多」只在 more 档出现
    expect(wrapper!.find('[data-im="board-more"]').exists()).toBe(false);
  });

  it("icons 档：先省略能由图标说明的文字（添加 / 详情），其它工具仍在行上", async () => {
    mountRail();
    await resizeRail(326); // 实机落点：800×600 的槽宽 326（300 <= 326 < 344）
    expect(modeOf()).toBe("icons");

    const add = wrapper!.get('[data-im="add-menu"]');
    expect(add.attributes("data-icon-only")).toBe("true");
    expect(add.classes()).toContain("is-icon-only");
    // 图标化后仍有可访问名称与悬停说明，不是无名按钮
    expect(add.attributes("aria-label")).toBe("添加");
    expect(add.attributes("title")).toBe("添加");
    // 文字退成 sr-only 而不是从 DOM 删除：读屏 / 按文字断言仍可读到
    expect(add.find(".tb-label").exists()).toBe(true);

    for (const hook of ["undo", "redo", "bookmarks-toggle"]) {
      expect(wrapper!.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    expect(wrapper!.find('[data-im="board-more"]').exists()).toBe(false);

    // 详情变图标（compact 形态），不再露「详情」文字，但 aria-label 保留
    const details = wrapper!.get('[data-im="submit-details"]');
    expect(details.classes()).toContain("sc-details-icon");
    expect(details.text()).not.toContain("详情");
    expect(details.attributes("aria-label")).toBe("提交详情");
  });

  it("升档带滞回：宽度回到 full 阈值附近不立刻换文字，回到 368 才回 full", async () => {
    mountRail();
    await resizeRail(326);
    expect(modeOf()).toBe("icons");
    await resizeRail(360); // >= 344（full 下界）但不足 344 + 24：仍停在 icons，不抖动
    expect(modeOf()).toBe("icons");
    expect(wrapper!.get('[data-im="add-menu"]').attributes("data-icon-only")).toBe("true");
    await resizeRail(368); // 最大槽宽（1000×600）才恢复 full
    expect(modeOf()).toBe("full");
    expect(wrapper!.get('[data-im="add-menu"]').attributes("data-icon-only")).toBeUndefined();
    expect(wrapper!.get('[data-im="add-menu"]').text()).toContain("添加");
    expect(wrapper!.get('[data-im="submit-details"]').text()).toContain("详情");
  });

  it("more 档：低频工具收进「更多」，添加 + 提交 + 更多常驻行上", async () => {
    mountRail();
    await resizeRail(286); // 实机落点：720×600 的槽宽 286（< 300）
    expect(modeOf()).toBe("more");

    for (const gone of ["undo", "redo", "bookmarks-toggle", "submit-details"]) {
      expect(wrapper!.find('[data-im="' + gone + '"]').exists(), gone).toBe(false);
    }
    // 添加仍然常驻（图标形态）；提交按钮由 SubmitCluster 自己维护（不改文案）
    const add = wrapper!.get('[data-im="add-menu"]');
    expect(add.attributes("data-icon-only")).toBe("true");
    expect(wrapper!.find('[data-im="submit"]').exists()).toBe(true);

    const more = wrapper!.get('[data-im="board-more"]');
    expect(more.attributes("aria-haspopup")).toBe("dialog");
    expect(more.attributes("aria-expanded")).toBe("false");
    expect(wrapper!.find('[data-im="board-more-panel"]').exists()).toBe(false);
  });
});

describe("rail 的「更多」面板", () => {
  it("开合 / 禁用态 / 焦点 / aria：开合只改显示，不改板面数据", async () => {
    mountRail();
    await resizeRail(286);
    await wrapper!.get('[data-im="board-more"]').trigger("click");
    await nextTick();
    await nextTick();

    expect(wrapper!.get('[data-im="board-more"]').attributes("aria-expanded")).toBe("true");
    const panel = wrapper!.get('[data-im="board-more-panel"]');
    expect(panel.attributes("role")).toBe("dialog");
    expect(panel.attributes("aria-label")).toBe("更多板面工具");
    for (const hook of ["board-more-undo", "board-more-redo", "board-more-bookmarks", "board-more-details"]) {
      expect(panel.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 没有可撤销 / 重做时两项禁用（禁用是可辨识的状态，不是靠猜灰图标）
    expect(panel.get('[data-im="board-more-undo"]').attributes("disabled")).toBeDefined();
    expect(panel.get('[data-im="board-more-redo"]').attributes("disabled")).toBeDefined();
    // 打开后焦点进入第一项（跳过禁用项）
    expect(activeHook()).toBe("board-more-bookmarks");
    // 开合不改任何板面数据：没有 commit、没有新增撤销栈
    expect(labels).toEqual([]);
    expect(store.undoStack).toHaveLength(0);

    // Esc 走共用层级栈关闭，并把焦点还给「更多」按钮
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    await nextTick();
    expect(wrapper!.find('[data-im="board-more-panel"]').exists()).toBe(false);
    expect(wrapper!.get('[data-im="board-more"]').attributes("aria-expanded")).toBe("false");
    expect(activeHook()).toBe("board-more");
  });

  it("书签栏 / 提交详情走既有入口；外点关闭", async () => {
    mountRail();
    await resizeRail(286);

    // 书签栏：emit('toggle-bookmarks')，aria-pressed 跟随 props.bookmarksOpen
    await wrapper!.get('[data-im="board-more"]').trigger("click");
    await nextTick();
    const bookmarks = wrapper!.get('[data-im="board-more-bookmarks"]');
    expect(bookmarks.attributes("aria-pressed")).toBe("false");
    await bookmarks.trigger("click");
    await nextTick();
    expect(wrapper!.emitted("toggle-bookmarks")).toHaveLength(1);
    expect(wrapper!.find('[data-im="board-more-panel"]').exists()).toBe(false);
    expect(activeHook()).toBe("board-more");

    await wrapper!.setProps({ bookmarksOpen: true });
    await wrapper!.get('[data-im="board-more"]').trigger("click");
    await nextTick();
    expect(wrapper!.get('[data-im="board-more-bookmarks"]').attributes("aria-pressed")).toBe("true");

    // 提交详情：调 SubmitCluster expose 的 openDetails()，详情面板出现；行上不渲染详情按钮
    await wrapper!.get('[data-im="board-more-details"]').trigger("click");
    await nextTick();
    await nextTick();
    expect(wrapper!.find('[data-im="submit-details-box"]').exists()).toBe(true);
    expect(wrapper!.find('[data-im="submit-details"]').exists()).toBe(false);
    expect(labels).toEqual([]); // 打开面板不改板面数据

    // 再开「更多」，点面板外部（pointerdown 捕获阶段）关闭；详情面板不受影响
    await wrapper!.get('[data-im="board-more"]').trigger("click");
    await nextTick();
    expect(wrapper!.find('[data-im="board-more-panel"]').exists()).toBe(true);
    document.body.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    await nextTick();
    expect(wrapper!.find('[data-im="board-more-panel"]').exists()).toBe(false);
    expect(wrapper!.find('[data-im="submit-details-box"]').exists()).toBe(true);
  });
});

/* --------------------------------------------------------------------------
 * 源码守卫：jsdom 不做布局，rail 的外观 / 面板位置 / 跨组件 props 只能按源码钉住。
 * ------------------------------------------------------------------------ */
const read = (relative: string) => readFileSync(resolve(process.cwd(), "src", relative), "utf8");
const toolbarSrc = read("components/interactive/BoardToolbar.vue");
const addMenuSrc = read("components/interactive/AddMenu.vue");
const submitSrc = read("components/interactive/SubmitCluster.vue");

describe("rail 外观与跨组件契约（源码守卫）", () => {
  it("操作行透明、单行不换行；独立按钮用中性描边 + --qio-ui-radius、高 28", () => {
    expect(toolbarSrc).toContain(".board-toolbar.is-rail");
    expect(toolbarSrc).toMatch(/\.board-toolbar\.is-rail \{[^}]*background: transparent/);
    expect(toolbarSrc).toMatch(/\.board-toolbar\.is-rail \{[^}]*border: 0/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] \.tb-main \{[^}]*flex-wrap: nowrap/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] \.tb-btn \{[^}]*height: 28px/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] \.tb-btn \{[^}]*border-radius: var\(--qio-ui-radius\)/);
    // 颜色只从语义令牌来（rail 样式里不出现硬编码色值）
    expect(toolbarSrc).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it("「更多」面板锚在操作行右上方；提交说明区浮起、不参与行高", () => {
    expect(toolbarSrc).toMatch(/\.tb-more-panel \{[^}]*right: 0/);
    expect(toolbarSrc).toMatch(/\.tb-more-panel \{[^}]*bottom: calc\(100% \+ 6px\)/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] :deep\(\.sc-notes\) \{[^}]*position: absolute/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] :deep\(\.sc-notes\) \{[^}]*bottom: calc\(100% \+ 6px\)/);
    expect(toolbarSrc).toMatch(/\[data-tb-rail="true"\] :deep\(\.sc-notes\) \{[^}]*max-height: 40vh/);
  });

  it("跨组件契约：AddMenu.iconOnly（名称/提示保留）、SubmitCluster.hideDetails + openDetails", () => {
    expect(addMenuSrc).toContain("iconOnly?: boolean");
    expect(addMenuSrc).toContain('data-im="add-menu"');
    expect(addMenuSrc).toContain("v-qio-tip");
    expect(submitSrc).toContain("hideDetails?: boolean");
    expect(submitSrc).toContain("openDetails");
    expect(submitSrc).toContain('v-if="!hideDetails"');
    expect(submitSrc).toContain('data-im="submit-details"');
  });

  it("rail=false 的 dock 栅格分档与 :has() 收行规则原样保留（不因 rail 改版回退）", () => {
    expect(toolbarSrc).toContain('[data-tb-band="mid"]');
    expect(toolbarSrc).toContain('[data-tb-band="narrow"]');
    expect(toolbarSrc).toContain("toolbarBand");
    expect(toolbarSrc).toContain(':not(:has([data-im="submit-history"]))');
  });
});