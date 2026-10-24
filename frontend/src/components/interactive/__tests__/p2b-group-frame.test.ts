/**
 * Phase 2-B 组框组件（BoardGroupFrame）：
 * 收起概览（固定槽位、不代表执行顺序）、组标题拖整组、组缩放把手、既有入口回归。
 *
 * 组件只发意图（props/emits），不改板面状态：Lead 在 BoardCanvas 接线。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardGroupFrame from "../BoardGroupFrame.vue";
import type { BoardCard, BoardGroup } from "../../../interactive/types";

const STAMP = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, x: number, y: number): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w: 120,
    h: 80,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkGroup(members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id: "g1",
    name: "发布计划",
    defaultName: false,
    ordered: false,
    x: 80,
    y: 40,
    w: 352,
    h: 154,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

const cards = [mkCard("a", 96, 100), mkCard("b", 300, 100)];

let wrapper: VueWrapper | null = null;

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
});

function mountFrame(props: Record<string, unknown> = {}): VueWrapper {
  wrapper = mount(BoardGroupFrame, {
    props: {
      group: mkGroup(["a", "b"]),
      cards,
      selectedIds: [],
      dropTarget: false,
      dropMerge: false,
      dropIndex: null,
      dropCardId: null,
      ...props,
    },
  });
  return wrapper;
}

/** jsdom 没有 PointerEvent：用 MouseEvent 带同样字段（与既有手势用例一致）。 */
function mouse(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
}

describe("既有入口回归（展开态不受影响）", () => {
  it("组名 / 成员与顺序 / 有序 / 解除组 / 移出 / 升降序仍在", () => {
    const frame = mountFrame();
    for (const hook of [
      "group-name",
      "group-members-toggle",
      "toggle-ordered",
      "dissolve-group",
      "leave-member",
    ]) {
      expect(frame.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 上移 / 下移只属于有序组（普通组没有序号，也就没有升降序按钮）
    frame.unmount();
    wrapper = mountFrame({ group: mkGroup(["a", "b"], { ordered: true }) });
    for (const hook of ["move-member-up", "move-member-down"]) {
      expect(wrapper.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    expect(frame.find('[data-im="group-overview"]').exists()).toBe(false);
    expect(frame.find('[data-im="group-collapse-toggle"]').exists()).toBe(true);
  });

  it("改名即用；成员管理面板按需展开", async () => {
    const frame = mountFrame();
    const name = frame.get('[data-im="group-name"]');
    (name.element as HTMLInputElement).value = "新名字";
    await name.trigger("change");
    expect(frame.emitted("rename")).toEqual([["g1", "新名字"]]);

    expect(frame.find('[data-im="group-tools"]').isVisible()).toBe(false);
    await frame.get('[data-im="group-members-toggle"]').trigger("click");
    expect(frame.find('[data-im="group-tools"]').exists()).toBe(true);
  });
});

describe("拖线目标：组框只加显示高亮", () => {
  it("linkTarget 时组框加 link-target 类；默认没有，松手前后都不改数据", async () => {
    const plain = mountFrame();
    expect(plain.get('[data-im="group"]').classes()).not.toContain("link-target");
    expect(plain.get('[data-im="group"]').attributes("data-link-target")).toBeUndefined();
    plain.unmount();

    wrapper = mountFrame({ linkTarget: true });
    const frame = wrapper.get('[data-im="group"]');
    expect(frame.classes()).toContain("link-target");
    expect(frame.attributes("data-link-target")).toBe("group");
    // 只是显示态：组名 / 成员 / 顺序一个字段都没变
    const group = (wrapper.props() as { group: BoardGroup }).group;
    expect(group).toMatchObject({ id: "g1", name: "发布计划", members: ["a", "b"] });
  });
});

describe("组收起概览（固定成员槽位，只显示不代表执行顺序）", () => {
  it("收起后显示固定槽位概览：没有序号、没有成员管理面板，无障碍文案说明不代表顺序", () => {
    const frame = mountFrame({
      collapsed: true,
      group: mkGroup(["a", "b"], { x: 80, y: 40, w: 420, h: 260 }),
    });
    const overview = frame.get('[data-im="group-overview"]');
    expect(overview.attributes("aria-label")).toContain("不代表执行顺序");
    expect(overview.findAll(".slot")).toHaveLength(5); // 固定槽位：两名成员也保留空槽
    expect(overview.findAll('.slot[data-card-id="a"]')).toHaveLength(1);
    expect(overview.findAll('.slot[data-empty="true"]')).toHaveLength(3);
    expect(frame.find('[data-im="group-members-toggle"]').exists()).toBe(false);
    expect(frame.find('[data-im="group-tools"]').exists()).toBe(false);
    expect(frame.find('[data-im="group-overview-more"]').exists()).toBe(false);
    expect(frame.find('[data-im="group-collapse-toggle"]').attributes("aria-expanded")).toBe("false");
  });

  it("成员多于固定槽位时如实显示还剩多少个", () => {
    const many = Array.from({ length: 7 }, (_, index) => mkCard("m" + index, index * 20, 100));
    const frame = mountFrame({
      collapsed: true,
      group: mkGroup(many.map((card) => card.id), { x: 80, y: 40, w: 420, h: 260 }),
      cards: many,
    });
    expect(frame.get('[data-im="group-overview-more"]').text()).toContain("还有 2 个");
  });

  it("收起 / 展开按钮发意图，不改任何数据", async () => {
    const expanded = mountFrame();
    await expanded.get('[data-im="group-collapse-toggle"]').trigger("click");
    expect(expanded.emitted("toggle-collapse")).toEqual([["g1", true]]);
    expanded.unmount();

    wrapper = mountFrame({ collapsed: true });
    await wrapper.get('[data-im="group-collapse-toggle"]').trigger("click");
    expect(wrapper.emitted("toggle-collapse")).toEqual([["g1", false]]);
  });

  it("收起时不再渲染缩放把手；标题仍可拖整组", async () => {
    const frame = mountFrame({ collapsed: true });
    expect(frame.find('[data-im="group-resize-handle"]').exists()).toBe(false);
    const grip = frame.get('[data-im="group-drag-handle"]').element;
    grip.dispatchEvent(mouse("pointerdown", 100, 100));
    document.dispatchEvent(mouse("pointermove", 110, 130));
    await nextTick();
    expect(frame.emitted("group-drag-move")![0]).toEqual(["g1", { dx: 10, dy: 30 }]);
    document.dispatchEvent(mouse("pointerup", 110, 130));
  });
});

describe("组标题拖动整组 / 组缩放把手（只发意图）", () => {
  it("标题拖动：start / move / end 都带组 id 与屏幕位移", async () => {
    const frame = mountFrame();
    const grip = frame.get('[data-im="group-drag-handle"]').element;
    grip.dispatchEvent(mouse("pointerdown", 100, 100));
    document.dispatchEvent(mouse("pointermove", 130, 140));
    document.dispatchEvent(mouse("pointerup", 130, 140));
    await nextTick();

    expect(frame.emitted("group-drag-start")).toEqual([["g1", { x: 100, y: 100 }]]);
    expect(frame.emitted("group-drag-move")).toEqual([["g1", { dx: 30, dy: 40 }]]);
    expect(frame.emitted("group-drag-end")).toEqual([["g1", { dx: 30, dy: 40 }]]);
  });

  it("Esc / 指针取消：发出 cancel，不发出 end（板面状态不该被提交）", async () => {
    const frame = mountFrame();
    frame.get('[data-im="group-drag-handle"]').element.dispatchEvent(mouse("pointerdown", 100, 100));
    document.dispatchEvent(mouse("pointermove", 120, 120));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(frame.emitted("group-drag-cancel")).toEqual([["g1"]]);
    expect(frame.emitted("group-drag-end")).toBeUndefined();

    frame.get('[data-im="group-drag-handle"]').element.dispatchEvent(mouse("pointerdown", 100, 100));
    document.dispatchEvent(mouse("pointercancel", 100, 100));
    await nextTick();
    expect(frame.emitted("group-drag-cancel")).toHaveLength(2);
  });

  it("从输入框 / 按钮按下不会开始拖整组", async () => {
    const frame = mountFrame();
    frame.get('[data-im="group-name"]').element.dispatchEvent(mouse("pointerdown", 100, 100));
    document.dispatchEvent(mouse("pointermove", 150, 150));
    frame.get('[data-im="group-members-toggle"]').element.dispatchEvent(mouse("pointerdown", 100, 100));
    await nextTick();
    expect(frame.emitted("group-drag-start")).toBeUndefined();
  });

  it("缩放把手：右 / 下 / 右下三向，发 start / move / end 并把边告诉父组件", async () => {
    const frame = mountFrame();
    const handles = frame.findAll('[data-im="group-resize-handle"]');
    expect(handles.map((item) => item.attributes("data-edge"))).toEqual(["e", "s", "se"]);

    const handle = frame.get('[data-im="group-resize-handle"][data-edge="se"]').element;
    handle.dispatchEvent(mouse("pointerdown", 432, 194));
    document.dispatchEvent(mouse("pointermove", 472, 234));
    document.dispatchEvent(mouse("pointerup", 472, 234));
    await nextTick();

    expect(frame.emitted("group-resize-start")).toEqual([["g1", "se", { x: 432, y: 194 }]]);
    expect(frame.emitted("group-resize-move")).toEqual([["g1", "se", { dx: 40, dy: 40 }]]);
    expect(frame.emitted("group-resize-end")).toEqual([["g1", "se", { dx: 40, dy: 40 }]]);
  });

  it("draggable / resizable 可关闭（组件层支持，不绑架 BoardCanvas）", () => {
    const frame = mountFrame({ draggable: false, resizable: false });
    expect(frame.find('[data-im="group-drag-handle"]').exists()).toBe(false);
    expect(frame.find('[data-im="group-resize-handle"]').exists()).toBe(false);
  });
});
