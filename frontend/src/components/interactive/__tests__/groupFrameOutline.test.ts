/**
 * 真实组框：贴合成员的轮廓 + 头部拖动入口（问题 4a / 4c）。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";

import BoardGroupFrame from "../BoardGroupFrame.vue";
import type { BoardCard, BoardGroup } from "../../../interactive/types";

function card(id: string, x: number, y: number, w = 100, h = 60): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w,
    h,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-22T00:00:00+00:00",
    updatedAt: "2026-10-22T00:00:00+00:00",
  };
}

function group(members: string[]): BoardGroup {
  return {
    id: "g1",
    name: "发布计划",
    defaultName: false,
    ordered: false,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    members,
    deleted: false,
    createdAt: "2026-10-22T00:00:00+00:00",
    updatedAt: "2026-10-22T00:00:00+00:00",
  };
}

const props = {
  selectedIds: [] as string[],
  dropTarget: false,
  dropMerge: false,
  dropIndex: null as number | null,
  dropCardId: null as string | null,
  active: false,
};

function mountFrame(members: string[], cards: BoardCard[], active = false) {
  return mount(BoardGroupFrame, {
    props: { ...props, group: group(members), cards, active },
    attachTo: document.body,
  });
}

describe("组框呈现（方案 A：组条常驻 + 轮廓只在交互时出现）", () => {
  it("默认不画轮廓：常驻只有组条（组名 / 成员数 / 有序）", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)]);
    expect(wrapper.find('[data-im="group"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="group-outline"]').exists()).toBe(false);
    const nameInput = wrapper.find('[data-im="group-name"]').element as HTMLInputElement;
    expect(nameInput.value).toBe("发布计划");
    expect(wrapper.text()).toContain("成员 2");
  });

  it("激活（悬停 / 选中 / 拖动）时才显示贴合轮廓，位置按成员推算", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)], true);
    const outline = wrapper.find('[data-im="group-outline"]');
    expect(outline.exists()).toBe(true);
    // 不画大空框：轮廓只覆盖成员扩张区，起点是成员左下扩张后的坐标
    const style = outline.attributes("style") ?? "";
    expect(style).toContain("left: -16px");
  });

  it("没有活成员：不渲染组条与轮廓", () => {
    const wrapper = mountFrame(["missing"], [], true);
    expect(wrapper.find('[data-im="group"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="group-outline"]').exists()).toBe(false);
  });
});

describe("整组拖动入口", () => {
  it("按住头部空白处发出 drag-start；点组名输入框不触发", async () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)]);
    const head = wrapper.find('[data-im="group-head"]');
    head.element.dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: 10, clientY: 10 }),
    );
    expect(wrapper.emitted("drag-start")?.length).toBe(1);
    expect(wrapper.emitted("drag-start")?.[0]?.[0]).toBe("g1");

    const input = wrapper.find('[data-im="group-name"]');
    input.element.dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: 10, clientY: 10 }),
    );
    expect(wrapper.emitted("drag-start")?.length).toBe(1);
  });

  it("悬停组条发出 hover 进入 / 离开（用于临时显示轮廓）", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)]);
    const rail = wrapper.get('[data-im="group"]');
    rail.element.dispatchEvent(new MouseEvent("pointerenter", { bubbles: false }));
    rail.element.dispatchEvent(new MouseEvent("pointerleave", { bubbles: false }));
    expect(wrapper.emitted("hover")?.[0]).toEqual(["g1", true]);
    expect(wrapper.emitted("hover")?.[1]).toEqual(["g1", false]);
  });
});
