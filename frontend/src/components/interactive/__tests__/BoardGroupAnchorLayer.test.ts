/**
 * 组端点锚点视觉层（BoardGroupAnchorLayer）。
 *
 * 只画传入的锚点：没有关系使用时不补四个端口；位置与 BoardLinkLayer 同源；
 * 组悬停 / 选中变短条，锚点自身悬停向外鼓出圆角半圆；按下只发起点意图。
 */
import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardGroupAnchorLayer from "../BoardGroupAnchorLayer.vue";
import type { GroupAnchor } from "../../../interactive/linkEndpoints";

const anchors: GroupAnchor[] = [
  {
    groupId: "g1",
    endpoint: "group:g1",
    groupName: "发布计划",
    side: "right",
    x: 440,
    y: 120,
    linkIds: ["l1"],
  },
  {
    groupId: "g1",
    endpoint: "group:g1",
    groupName: "发布计划",
    side: "bottom",
    x: 260,
    y: 200,
    linkIds: ["l2"],
  },
  {
    groupId: "g2",
    endpoint: "group:g2",
    groupName: "材料组",
    side: "top",
    x: 700,
    y: 40,
    linkIds: ["l3"],
  },
];

let wrapper: VueWrapper | null = null;

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
});

function mountLayer(props: Record<string, unknown> = {}): VueWrapper {
  wrapper = mount(BoardGroupAnchorLayer, {
    props: { anchors, scale: 1, selectedGroupIds: [], hoveredGroupId: null, ...props },
  });
  return wrapper;
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
}

describe("只画被关系使用的组锚点", () => {
  it("传入几个就画几个：空数组时不会常驻四个端口", () => {
    const empty = mountLayer({ anchors: [] });
    expect(empty.findAll('[data-im="group-anchor"]')).toHaveLength(0);
    empty.unmount();

    wrapper = mountLayer({ anchors: anchors.slice(0, 1) });
    expect(wrapper.findAll('[data-im="group-anchor"]')).toHaveLength(1);
    wrapper = mountLayer();
    expect(wrapper.findAll('[data-im="group-anchor"]')).toHaveLength(3);
  });

  it("每个锚点带组端点身份与边方向；位置 = 板面坐标 × 缩放", () => {
    const layer = mountLayer({ scale: 2 });
    const items = layer.findAll('[data-im="group-anchor"]');
    expect(items.map((item) => item.attributes("data-endpoint"))).toEqual([
      "group:g1",
      "group:g1",
      "group:g2",
    ]);
    expect(items.map((item) => item.attributes("data-side"))).toEqual(["right", "bottom", "top"]);
    expect(items[0].attributes("data-link-ids")).toBe("l1");
    expect((items[0].element as HTMLElement).style.left).toBe("880px");
    expect((items[0].element as HTMLElement).style.top).toBe("240px");
    expect(items[0].attributes("aria-label")).toContain("发布计划");
    expect(items[0].attributes("aria-label")).toContain("右边");
  });
});

describe("组悬停 / 选中 / 锚点悬停的视觉状态", () => {
  it("选中的组：锚点进入短条态；未选中的组保持空心圆点态", () => {
    const layer = mountLayer({ selectedGroupIds: ["g1"] });
    const items = layer.findAll('[data-im="group-anchor"]');
    expect(items[0].classes()).toContain("is-active");
    expect(items[1].classes()).toContain("is-active");
    expect(items[2].classes()).not.toContain("is-active");
    expect(items[0].attributes("data-active")).toBe("true");
    expect(items[2].attributes("data-active")).toBe("false");
  });

  it("悬停的组：只有这个组的锚点进入短条态", () => {
    const layer = mountLayer({ hoveredGroupId: "g2" });
    const items = layer.findAll('[data-im="group-anchor"]');
    expect(items.map((item) => item.classes().includes("is-active"))).toEqual([false, false, true]);
  });

  it("锚点按下只发起点意图（组端点身份原样），不建链、不改数据", async () => {
    const layer = mountLayer();
    const anchor = layer.findAll('[data-im="group-anchor"]')[0];
    anchor.element.dispatchEvent(pointer("pointerdown", 440, 120));
    await layer.vm.$nextTick();
    const emitted = layer.emitted("anchor-down");
    expect(emitted).toHaveLength(1);
    expect((emitted![0][0] as GroupAnchor).endpoint).toBe("group:g1");
    expect(emitted![0][1]).toBeInstanceOf(MouseEvent);
    expect((emitted![0][1] as MouseEvent).defaultPrevented).toBe(true);
  });
});

describe("样式守卫：空心圆点 / 短条 / 外侧圆角半圆", () => {
  const source = readFileSync(
    resolve(process.cwd(), "src/components/interactive/BoardGroupAnchorLayer.vue"),
    "utf8",
  );
  const style = source.slice(source.indexOf("<style"));

  it("静止态是空心圆点（品牌色描边 + 纸面底色）", () => {
    const dot = /\.group-anchor::before\s*\{([^}]*)\}/.exec(style)?.[1] ?? "";
    expect(dot).toMatch(/width:\s*7px/);
    expect(dot).toMatch(/height:\s*7px/);
    expect(dot).toMatch(/border:\s*1\.5px solid var\(--accent\)/);
    expect(dot).toMatch(/border-radius:\s*50%/);
    expect(dot).toMatch(/background:\s*var\(--bg-surface\)/);
  });

  it("组悬停 / 选中变短条：竖边 6×16、横边 16×6，圆角 8px", () => {
    const bar = /\.group-anchor::after\s*\{([^}]*)\}/.exec(style)?.[1] ?? "";
    expect(bar).toMatch(/width:\s*6px/);
    expect(bar).toMatch(/height:\s*16px/);
    expect(bar).toMatch(/border-radius:\s*8px/);
    expect(style).toMatch(/\[data-side="top"\]::after,[\s\S]{0,80}?\[data-side="bottom"\]::after\s*\{[^}]*width:\s*16px[^}]*height:\s*6px/);
    expect(style).toMatch(/\.group-anchor\.is-active::after\s*\{[^}]*opacity:\s*0\.72/);
  });

  it("锚点悬停：半圆基座贴边、只向外鼓出（四向各自固定基座）", () => {
    expect(style).toMatch(/\.group-anchor\[data-side="right"\] \.group-anchor-shape\s*\{[^}]*left:\s*17px[^}]*transform-origin:\s*left center/);
    expect(style).toMatch(/\.group-anchor\[data-side="left"\] \.group-anchor-shape\s*\{[^}]*right:\s*17px[^}]*transform-origin:\s*right center/);
    expect(style).toMatch(/\.group-anchor\[data-side="top"\] \.group-anchor-shape\s*\{[^}]*bottom:\s*17px[^}]*transform-origin:\s*center bottom/);
    expect(style).toMatch(/\.group-anchor\[data-side="bottom"\] \.group-anchor-shape\s*\{[^}]*top:\s*17px[^}]*transform-origin:\s*center top/);
    expect(style).toMatch(/\.group-anchor:hover \.group-anchor-shape[^{]*\{[^}]*opacity:\s*1[^}]*transform:\s*scale\(1\)/);
    expect(style).toMatch(/\.group-anchor:hover::before,\s*\n\.group-anchor:hover::after\s*\{\s*opacity:\s*0/);
  });
});
