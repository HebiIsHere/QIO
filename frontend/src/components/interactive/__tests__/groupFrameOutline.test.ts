/**
 * 组条呈现（方案 A）：常驻只有组条 + 成员标记；不画轮廓；
 * 拖入提示是组条上方的独立浮标（用户实测：塞进组条会把文字挤到显示不全）。
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

const baseProps = {
  selectedIds: [] as string[],
  dropTarget: false,
  dropMerge: false,
  dropIndex: null as number | null,
  dropCardId: null as string | null,
};

function mountFrame(
  members: string[],
  cards: BoardCard[],
  extra: Partial<typeof baseProps> = {},
) {
  return mount(BoardGroupFrame, {
    props: { ...baseProps, ...extra, group: group(members), cards },
    attachTo: document.body,
  });
}

describe("组条呈现（方案 A）", () => {
  it("常驻只有组条：组名 / 成员数 / 是否有默认名，且不画轮廓", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)]);
    expect(wrapper.find("[data-im='group']").exists()).toBe(true);
    expect(wrapper.find("[data-im='group-outline']").exists()).toBe(false);
    const nameInput = wrapper.find("[data-im='group-name']").element as HTMLInputElement;
    expect(nameInput.value).toBe("发布计划");
    expect(wrapper.text()).toContain("成员 2");
    // 组条上有拖动把手
    expect(wrapper.find(".grip").exists()).toBe(true);
  });

  it("没有活成员：不渲染组条", () => {
    const wrapper = mountFrame(["missing"], []);
    expect(wrapper.find("[data-im='group']").exists()).toBe(false);
  });

  it("拖入提示是组条上方的独立浮标：完整文字、不塞进组条", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)], {
      dropTarget: true,
      dropIndex: 1,
      dropCardId: "c",
    });
    const hint = wrapper.find("[data-im='group-drop-hint']");
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toBe("将插入第 2 位");
    // 不在组条内部（组条文字不会被挤掉，提示也不会被组条裁切）
    expect(wrapper.find("[data-im='group-head'] [data-im='group-drop-hint']").exists()).toBe(false);
    // 提示排在组条上方
    expect(wrapper.find(".drop-badge").exists()).toBe(true);
  });
});

describe("整组拖动入口", () => {
  it("按住组条空白处发出 drag-start；点组名输入框不触发", () => {
    const wrapper = mountFrame(["a", "b"], [card("a", 0, 0), card("b", 300, 0)]);
    const head = wrapper.find("[data-im='group-head']");
    head.element.dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: 10, clientY: 10 }),
    );
    expect(wrapper.emitted("drag-start")?.length).toBe(1);
    expect(wrapper.emitted("drag-start")?.[0]?.[0]).toBe("g1");

    const input = wrapper.find("[data-im='group-name']");
    input.element.dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: 10, clientY: 10 }),
    );
    expect(wrapper.emitted("drag-start")?.length).toBe(1);
  });
});
