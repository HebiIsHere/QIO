/**
 * #10/#11（PM-QIO032）· 画布连线：
 * - 有效拖线松手即建链，方向 / 含义可稍后填；普通关闭保留新线；
 * - 编辑框对**本次新建**的线明确给「撤销这条新连线」，按连线身份撤销；
 * - 拖到无效目标不落盘；
 * - 成功提示自动消失；失败提示就地可关闭、可重试，重试绑定原操作且不会重复建同一条线。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardCard, BoardLink, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-25T00:00:00.000Z";
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;

function mkCard(id: string, x: number, y: number): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w: 100,
    h: 60,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkLink(id: string, src: string, dst: string): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkState(cards: BoardCard[], links: BoardLink[], selection: string[]): BoardState {
  return board.normalizeState({ ...board.emptyState("board_t"), cards, links, selection });
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

function stubGeometry(wrapper: VueWrapper): void {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

function screenOf(boardX: number, boardY: number) {
  return { x: VIEW_LEFT + boardX, y: VIEW_TOP + boardY };
}

async function dragFromSelectedCard(wrapper: VueWrapper, target: { x: number; y: number }): Promise<void> {
  const point = wrapper.findAll('[data-im="connect-point"]')[0];
  expect(point, "选中卡片后必须有连接点").toBeTruthy();
  const from = screenOf(50, 9);
  point.element.dispatchEvent(pointer("pointerdown", from.x, from.y));
  await wrapper.vm.$nextTick();
  const to = screenOf(target.x, target.y);
  window.dispatchEvent(pointer("pointermove", to.x, to.y));
  await wrapper.vm.$nextTick();
  window.dispatchEvent(pointer("pointerup", to.x, to.y));
  await wrapper.vm.$nextTick();
}

let store: ReturnType<typeof useInteractiveStore>;
let wrapper: VueWrapper | null = null;
let labels: string[] = [];

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  stubGeometry(wrapper);
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = board.emptyState("board_t");
  labels = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("#10 有效拖线建链 + 普通关闭保留新线", () => {
  it("拖到有效卡片松手即建一条新线；关闭编辑框不删除它", async () => {
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], ["a"]);
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 450, y: 30 });

    expect(store.board!.links, "有效拖线没有建链").toHaveLength(1);
    const created = store.board!.links[0];
    expect(labels.some((label) => label.includes("新建关系"))).toBe(true);
    const dialog = canvas.find(".link-editor");
    expect(dialog.exists(), "建链后没有打开编辑框").toBe(true);
    expect(canvas.find('[data-im="link-undo-new"]').exists(), "新建的线没有撤销入口").toBe(true);

    // 普通关闭：新线保留（不是撤销）
    await canvas.findAll(".link-editor button").find((item) => item.text().includes("关闭"))!.trigger("click");
    await canvas.vm.$nextTick();
    expect(store.board!.links).toHaveLength(1);
    expect(store.board!.links[0].id).toBe(created.id);
  });
});

describe("#10 编辑框「撤销这条新连线」按连线身份撤销", () => {
  it("撤销删除的正是这一条新线；两端卡片与其它关系保持不变", async () => {
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0), mkCard("c", 800, 0)], [mkLink("old", "b", "c")], ["a"]);
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 450, y: 30 });

    expect(store.board!.links).toHaveLength(2);
    const newLink = store.board!.links.find((link) => link.id !== "old")!;
    const undo = canvas.get('[data-im="link-undo-new"]');
    await undo.trigger("click");
    await canvas.vm.$nextTick();

    expect(labels.filter((label) => label.includes("撤销这条新连线")), "撤销没有走按身份删除").toHaveLength(1);
    expect(store.board!.links, "撤销后新线还在").toHaveLength(1);
    expect(store.board!.links[0].id).toBe("old");
    expect(store.board!.cards.map((card) => card.id)).toEqual(["a", "b", "c"]);
    expect(canvas.find(".link-editor").exists()).toBe(false);
    const banner = canvas.get('[data-im="canvas-notice"]');
    expect(banner.text()).toContain("已撤销这条新连线");
  });
});

describe("#10 无效目标不落盘", () => {
  it("拖到空白处松手：不建链、不提交，也没有可重试的建链操作", async () => {
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], ["a"]);
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 1500, y: 900 });

    expect(store.board!.links).toHaveLength(0);
    expect(labels.some((label) => label.includes("新建关系"))).toBe(false);
    const banner = canvas.get('[data-im="canvas-notice"]');
    expect(banner.text()).toContain("没有建立关系");
    expect(canvas.find('[data-im="canvas-notice-retry"]').exists(), "无效目标不该提供建链重试").toBe(false);
  });
});

describe("#11 失败提示可关闭 / 可重试，重试绑定原操作且不重复建线", () => {
  it("板面只读时建链失败：有失败提示与重试；恢复后重试成功且只建一条", async () => {
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], ["a"]);
    store.loadError = "网络断了";
    store.boardLoadedOnce = true;
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 450, y: 30 });

    expect(store.board!.links, "只读期间不应写入关系").toHaveLength(0);
    const banner = canvas.get('[data-im="canvas-notice"]');
    expect(banner.attributes("data-tone")).toBe("error");
    expect(banner.text()).toContain("不可编辑");
    const close = canvas.find('[data-im="canvas-notice-dismiss"]');
    expect(close.exists(), "失败提示没有关闭入口").toBe(true);
    const retry = canvas.find('[data-im="canvas-notice-retry"]');
    expect(retry.exists(), "失败提示没有重试入口").toBe(true);

    // 恢复可编辑后重试：绑定原操作，建一条线
    store.loadError = null;
    await retry.trigger("click");
    await canvas.vm.$nextTick();
    expect(store.board!.links).toHaveLength(1);
  });

  it("失败后同一条线已被别处建好：重试只打开原线，不重复创建", async () => {
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], ["a"]);
    store.loadError = "网络断了";
    store.boardLoadedOnce = true;
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 450, y: 30 });
    expect(store.board!.links).toHaveLength(0);

    // 模拟「另一条路径已经建好」：板面直接出现同一对端点的关系
    store.board = { ...store.board!, links: [mkLink("existing", "a", "b")] };
    const retry = canvas.get('[data-im="canvas-notice-retry"]');
    await retry.trigger("click");
    await canvas.vm.$nextTick();

    expect(store.board!.links, "重试重复创建了同一条线").toHaveLength(1);
    expect(store.board!.links[0].id).toBe("existing");
    expect(labels.filter((label) => label.includes("新建关系")), "重试不该再建一条新线").toHaveLength(0);
  });

  it("失败提示可手动关闭（关闭后不再占着板面顶部）", async () => {
    store.board = mkState([mkCard("a", 0, 0)], [], ["a"]);
    store.loadError = "网络断了";
    store.boardLoadedOnce = true;
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 900, y: 900 });
    expect(canvas.find('[data-im="canvas-notice"]').exists()).toBe(true);
    await canvas.get('[data-im="canvas-notice-dismiss"]').trigger("click");
    await canvas.vm.$nextTick();
    expect(canvas.find('[data-im="canvas-notice"]').exists()).toBe(false);
  });
});

describe("#11 成功提示自动消失", () => {
  it("建链成功的提示约 4 秒后自动消失（不是常驻横幅）", async () => {
    vi.useFakeTimers();
    store.board = mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], ["a"]);
    const canvas = mountCanvas();
    await dragFromSelectedCard(canvas, { x: 450, y: 30 });
    expect(store.board!.links).toHaveLength(1);
    const banner = canvas.find('[data-im="canvas-notice"]');
    expect(banner.exists(), "建链成功后没有提示").toBe(true);
    expect(banner.attributes("data-tone")).toBe("success");

    await vi.advanceTimersByTimeAsync(4100);
    await canvas.vm.$nextTick();
    expect(canvas.find('[data-im="canvas-notice"]').exists(), "成功提示没有自动消失").toBe(false);
  });
});
