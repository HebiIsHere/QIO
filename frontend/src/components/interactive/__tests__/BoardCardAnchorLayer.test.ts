import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardCardAnchorLayer from "../BoardCardAnchorLayer.vue";
import type { CardAnchor } from "../../../interactive/linkEndpoints";

const anchors: CardAnchor[] = [
  { cardId: "a", endpoint: "a", side: "right", x: 440, y: 120, linkIds: ["l1"] },
  { cardId: "a", endpoint: "a", side: "bottom", x: 260, y: 200, linkIds: ["l2"] },
  { cardId: "b", endpoint: "b", side: "top", x: 700, y: 40, linkIds: ["l3"] },
];

let wrapper: VueWrapper | null = null;
afterEach(() => { wrapper?.unmount(); wrapper = null; });

function mountLayer(props: Record<string, unknown> = {}): VueWrapper {
  wrapper = mount(BoardCardAnchorLayer, {
    props: { anchors, scale: 1, selectedCardIds: [], hoveredCardId: null, ...props },
  });
  return wrapper;
}

describe("卡片端点锚点层", () => {
  it("只画传入的已连接锚点，不常驻四个端口", () => {
    const empty = mountLayer({ anchors: [] });
    expect(empty.findAll('[data-im="card-anchor"]')).toHaveLength(0);
    empty.unmount();
    wrapper = mountLayer();
    expect(wrapper.findAll('[data-im="card-anchor"]')).toHaveLength(3);
  });

  it("带卡片端点身份、边方向，位置按缩放换算", () => {
    const layer = mountLayer({ scale: 2 });
    const items = layer.findAll('[data-im="card-anchor"]');
    expect(items.map((item) => item.attributes("data-endpoint"))).toEqual(["a", "a", "b"]);
    expect(items.map((item) => item.attributes("data-side"))).toEqual(["right", "bottom", "top"]);
    expect((items[0].element as HTMLElement).style.left).toBe("880px");
    expect((items[0].element as HTMLElement).style.top).toBe("240px");
  });

  it("选中/悬停卡片时锚点进入短条态", () => {
    const layer = mountLayer({ selectedCardIds: ["a"] });
    const items = layer.findAll('[data-im="card-anchor"]');
    expect(items.map((item) => item.attributes("data-active"))).toEqual(["true", "true", "false"]);
  });

  it("悬停卡片只激活该卡锚点", () => {
    const layer = mountLayer({ hoveredCardId: "b" });
    const items = layer.findAll('[data-im="card-anchor"]');
    expect(items.map((item) => item.classes().includes("is-active"))).toEqual([false, false, true]);
  });

  it("按下只发出卡片端点起点，不建链", async () => {
    const layer = mountLayer();
    const anchor = layer.findAll('[data-im="card-anchor"]')[0];
    anchor.element.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0 }));
    await layer.vm.$nextTick();
    const emitted = layer.emitted("anchor-down");
    expect(emitted).toHaveLength(1);
    expect((emitted![0][0] as CardAnchor).endpoint).toBe("a");
    expect((emitted![0][1] as MouseEvent).defaultPrevented).toBe(true);
  });
});
