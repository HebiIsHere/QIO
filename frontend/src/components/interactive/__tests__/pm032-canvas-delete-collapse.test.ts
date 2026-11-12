/**
 * #43 / #58 / #59（PM-QIO032）：
 * - #43 Delete / Backspace 只在画布拥有焦点且对象当前可见时生效；工具栏 / 详情 / 弹窗 / 菜单 /
 *   编辑器里的删除键不被画布劫持。
 * - #58 收起组时清除不可见成员的选择；删除时再校验一次可见性。
 * - #59 组收起偏好写进板面状态（group.collapsed），刷新 / 重载后由板面状态恢复。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardGroup, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-14T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w: 260,
    h: 170,
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

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: "组 " + id,
    defaultName: true,
    ordered: false,
    x: 0,
    y: 0,
    w: 560,
    h: 400,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function board(cards: BoardCardModel[], groups: BoardGroup[], patch: Partial<BoardState> = {}): BoardState {
  return { ...emptyState("board_t"), cards, groups, ...patch };
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

async function flush(times = 2): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await nextTick();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

function keydown(target: EventTarget, key: string): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

describe("#59 组收起偏好", () => {
  it("收起组写入板面状态（collapsed=true），展开写回 false —— 刷新后由板面状态恢复", async () => {
    store.board = board([mkCard("c1", 0, 0), mkCard("c2", 300, 0)], [mkGroup("g1", ["c1", "c2"])], {
      selection: ["c1", "c2"],
    });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    const toggle = wrapper.get('[data-im="group-collapse-toggle"]');
    await toggle.trigger("click");
    await flush();
    const collapsedBoard = store.board as BoardState;
    expect(collapsedBoard.groups[0].collapsed).toBe(true);
    // #58：收起后不可见成员从选择里剔除（本组两名成员都不可见 → 选择清空）
    expect(collapsedBoard.selection).toEqual([]);

    // 模拟刷新：新画布挂载后按板面状态恢复收起（成员卡片不再单独渲染）
    wrapper.unmount();
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    const frames = wrapper.findAll('[data-im="group"]');
    expect(frames[0].attributes("data-collapsed")).toBe("true");
  });
});

describe("#43 删除快捷键的焦点与可见性隔离", () => {
  it("焦点在卡片工具栏按钮上时 Delete 不删除卡片", async () => {
    store.board = board([mkCard("c1", 100, 100)], [], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    const toolbarButton = wrapper.get('[data-im="card-icon-edit"]').element;
    const event = keydown(toolbarButton, "Delete");
    await flush();
    expect(event.defaultPrevented).toBe(false);
    expect((store.board as BoardState).cards[0].deleted).toBe(false);
  });

  it("画布拥有焦点（焦点在 body）时 Delete 删除所选可见卡片", async () => {
    store.board = board([mkCard("c1", 100, 100)], [], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    keydown(document.body, "Delete");
    await flush();
    expect((store.board as BoardState).cards[0].deleted).toBe(true);
  });

  it("收起组里的成员即使仍在选择里也不被删除（删除时再校验可见性）", async () => {
    store.board = board(
      [mkCard("c1", 0, 0), mkCard("c2", 300, 0)],
      [mkGroup("g1", ["c1", "c2"], { collapsed: true })],
      { selection: ["c2"] },
    );
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    keydown(document.body, "Delete");
    await flush();
    const current = store.board as BoardState;
    expect(current.cards.find((card) => card.id === "c2")?.deleted).toBe(false);
  });
});