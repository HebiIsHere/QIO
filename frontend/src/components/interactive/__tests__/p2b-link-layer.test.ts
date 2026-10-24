/**
 * Phase 2-B 关系链接层（BoardLinkLayer）：
 * - 连线锚定最终显示边缘（不是卡片中心）；
 * - 组端点 `group:<id>` 锚到组框边缘，成员集合由组表达（不按成员复制）；
 * - 组收起时成员对外线汇聚到组边缘，真实 src/dst 不变；组内线收起期间不画；展开恢复；
 * - 拖线支持组端点（目标可以是卡片或组），无效目标不建链；
 * - 组端点的无障碍文案不泄露成员内容。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardLinkLayer from "../BoardLinkLayer.vue";
import type { BoardCard, BoardGroup, BoardLink } from "../../../interactive/types";

const STAMP = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
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
    ...patch,
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: "发布计划",
    defaultName: false,
    ordered: false,
    x: 80,
    y: 40,
    w: 360,
    h: 160,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function mkLink(id: string, src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

const cards = [mkCard("a", 100, 100), mkCard("b", 400, 100), mkCard("c", 100, 400)];
const groups = [mkGroup("g1", ["a", "b"])];
const base = {
  cards,
  groups,
  activeLinkId: null,
  width: 2000,
  height: 2000,
  draftFromId: null,
  draftPoint: null,
  draftTargetId: null,
};

let wrapper: VueWrapper | null = null;

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
});

function mountLayer(props: Record<string, unknown> = {}): VueWrapper {
  wrapper = mount(BoardLinkLayer, { props: { ...base, links: [mkLink("l1", "a", "b")], ...props } });
  return wrapper;
}

function hitPathOf(frame: VueWrapper, linkId: string): { x1: number; y1: number; x2: number; y2: number } {
  const g = frame.get('[data-link-id="' + linkId + '"]');
  const d = String(g.get("path.hit").attributes("d") ?? "");
  const numbers = (d.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
  return { x1: numbers[0], y1: numbers[1], x2: numbers[numbers.length - 2], y2: numbers[numbers.length - 1] };
}

describe("最终显示边缘锚点", () => {
  it("卡片—卡片：两端落在卡片边缘，而不是中心点", () => {
    const frame = mountLayer();
    const g = frame.get('[data-link-id="l1"]');
    const line = hitPathOf(frame, "l1");
    expect(line.x1).toBe(220); // a 右边
    expect(line.y1).toBe(140);
    expect(line.x2).toBe(400); // b 左边
    expect(line.y2).toBe(140);
    expect(g.attributes("data-src")).toBe("a");
    expect(g.attributes("data-dst")).toBe("b");
    expect(g.attributes("data-src-kind")).toBe("card");
    expect(g.attributes("data-aggregated")).toBeUndefined();
  });

  it("组端点：锚到组框边缘，一条线表示当前成员集合", () => {
    const frame = mountLayer({ links: [mkLink("l2", "group:g1", "c")] });
    const g = frame.get('[data-link-id="l2"]');
    const line = hitPathOf(frame, "l2");
    expect(g.attributes("data-src-kind")).toBe("group");
    expect(g.attributes("data-display-src")).toBe("group:g1");
    expect(line.y1).toBe(200); // 组框下边缘
    expect(line.x1).toBeGreaterThanOrEqual(80);
    expect(line.x1).toBeLessThanOrEqual(440);
    // 不按成员复制成多条
    expect(frame.findAll('[data-link-id="l2"]')).toHaveLength(1);
  });

  it("拖动中的实时坐标由调用方传入，锚点跟着最终显示矩形走", () => {
    const moved = [mkCard("a", 100, 100), mkCard("b", 600, 100)];
    const frame = mountLayer({ cards: moved });
    const line = hitPathOf(frame, "l1");
    expect(line.x2).toBe(600);
  });
});

describe("组收起 / 展开", () => {
  it("收起时成员对外线汇聚到组边缘，真实端点身份不变", () => {
    const frame = mountLayer({
      links: [mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    const g = frame.get('[data-link-id="l3"]');
    const line = hitPathOf(frame, "l3");
    expect(g.attributes("data-src")).toBe("a"); // 真实端点不变
    expect(g.attributes("data-display-src")).toBe("group:g1");
    expect(g.attributes("data-aggregated")).toBe("true");
    expect(line.y1).toBe(200); // 组框边缘
  });

  it("展开后恢复成员边缘；组内成员之间的线在收起期间不画、展开后恢复", () => {
    const folded = mountLayer({
      links: [mkLink("internal", "a", "b"), mkLink("l3", "a", "c")],
      foldedGroupIds: ["g1"],
    });
    expect(folded.find('[data-link-id="internal"]').exists()).toBe(false);
    expect(folded.find('[data-link-id="l3"]').exists()).toBe(true);
    folded.unmount();

    wrapper = mountLayer({ links: [mkLink("internal", "a", "b"), mkLink("l3", "a", "c")] });
    expect(wrapper.find('[data-link-id="internal"]').exists()).toBe(true);
    const g = wrapper.get('[data-link-id="l3"]');
    expect(g.attributes("data-display-src")).toBe("a");
    expect(g.attributes("data-aggregated")).toBeUndefined();
    expect(hitPathOf(wrapper!, "l3").y1).toBe(180); // a 下边缘
  });
});

describe("端点无效 / 已删除：如实跳过，不画假线", () => {
  it("已删除卡片、未知组、非法端点字符串都不渲染连线", () => {
    const frame = mountLayer({
      links: [
        mkLink("gone", "a", "dead"),
        mkLink("unknown", "group:missing", "a"),
        mkLink("junk", "bad:thing", "a"),
      ],
      cards: [...cards, mkCard("dead", 0, 0, { deleted: true })],
    });
    expect(frame.findAll("[data-link-id]")).toHaveLength(0);
    expect(frame.find(".link-layer").exists()).toBe(true);
  });

  it("只画可交给 QIO 的关系时，未参与卡片的关系不出现", () => {
    const withExcluded = [...cards, mkCard("excluded", 700, 100)];
    const frame = mountLayer({
      cards: withExcluded,
      includedCardIds: ["a", "c"],
      onlyInRange: true,
      links: [mkLink("ok", "a", "c"), mkLink("out", "excluded", "c")],
    });
    expect(frame.findAll("[data-link-id]").map((item) => item.attributes("data-link-id"))).toEqual(["ok"]);
  });
});

describe("拖线预览（目标可以是卡片或组）", () => {
  it("从卡片拖线：起点在卡片边缘；无有效目标时明确说拖到卡片或组", () => {
    const frame = mountLayer({ draftFromId: "a", draftPoint: { x: 500, y: 140 } });
    const draft = frame.get('[data-im="link-draft"]');
    expect(draft.attributes("data-from")).toBe("a");
    expect(draft.attributes("data-target")).toBe("");
    expect(Number(draft.get(".draft-dot").attributes("cx"))).toBe(220);
    expect(Number(draft.get(".draft-dot").attributes("cy"))).toBe(140);
    expect(draft.get(".draft-line").classes()).not.toContain("valid");
    expect(draft.text()).toContain("卡片或一个组");
  });

  it("目标是组：有效、给出组端点与组框高亮，松手才建链", () => {
    const frame = mountLayer({
      draftFromId: "a",
      draftPoint: { x: 300, y: 120 },
      draftTargetId: "group:g1",
    });
    const draft = frame.get('[data-im="link-draft"]');
    expect(draft.attributes("data-target")).toBe("group:g1");
    expect(draft.attributes("data-target-kind")).toBe("group");
    expect(draft.get(".draft-line").classes()).toContain("valid");
    expect(draft.find(".draft-target").exists()).toBe(true);
  });

  it("目标是卡片：浮标明确写「连接到卡片」，不是组", () => {
    const frame = mountLayer({
      draftFromId: "a",
      draftPoint: { x: 400, y: 140 },
      draftTargetId: "b",
    });
    const badge = frame.get('[data-im="link-target-badge"]');
    expect(badge.attributes("data-target-kind")).toBe("card");
    expect(badge.attributes("data-target")).toBe("b");
    expect(badge.text()).toBe("连接到卡片");
  });

  it("目标是组：浮标明确写「连接到组」，不自动改写成成员卡", () => {
    const frame = mountLayer({
      draftFromId: "a",
      draftPoint: { x: 300, y: 120 },
      draftTargetId: "group:g1",
    });
    const badge = frame.get('[data-im="link-target-badge"]');
    expect(badge.attributes("data-target-kind")).toBe("group");
    expect(badge.attributes("data-target")).toBe("group:g1");
    expect(badge.text()).toBe("连接到组");
  });

  it("没有有效目标时不出浮标（只说拖到卡片或组）", () => {
    const frame = mountLayer({ draftFromId: "a", draftPoint: { x: 500, y: 140 } });
    expect(frame.find('[data-im="link-target-badge"]').exists()).toBe(false);
    expect(frame.get('[data-im="link-draft"]').text()).toContain("卡片或一个组");
  });

  it("从组拖线：起点在组边缘；目标卡片同样有效", () => {
    const frame = mountLayer({
      draftFromId: "group:g1",
      draftPoint: { x: 100, y: 500 },
      draftTargetId: "c",
    });
    const draft = frame.get('[data-im="link-draft"]');
    expect(draft.attributes("data-from")).toBe("group:g1");
    expect(Number(draft.get(".draft-dot").attributes("cy"))).toBe(200); // 组框下边缘
    expect(draft.get(".draft-line").classes()).toContain("valid");
  });
});

describe("文案与无障碍", () => {
  it("含义标签、颜色方向和选中态照旧（不使用箭头）", () => {
    const frame = mountLayer({ links: [mkLink("l1", "a", "b", { direction: true, meaning: "属于" })], activeLinkId: "l1" });
    expect(frame.get('[data-link-id="l1"] .label').text()).toBe("属于");
    expect(frame.get('[data-link-id="l1"] path.line').classes()).toContain("active");
    expect(frame.find("marker").exists()).toBe(false);
    expect(frame.find('[data-link-id="l1"] path.line').attributes("stroke")).toContain("url(#im-link-grad-");
  });

  it("拖动含义标签会发出曲线变更，松手时保留端点身份", async () => {
    const frame = mountLayer();
    const label = frame.get('[data-link-label="l1"] .label-bg');
    const element = label.element as Element;
    element.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: 100, clientY: 100 }));
    window.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: 140, clientY: 70 }));
    window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true, clientX: 140, clientY: 70 }));
    await frame.vm.$nextTick();
    expect(frame.emitted("curve-change")?.[0]).toEqual(["l1", { x: 40, y: -30 }]);
  });

  it("组端点的标题只用组名与「卡片」，不泄露成员正文", () => {
    const secretCards = [mkCard("a", 100, 100, { content: "秘密正文不该出现在关系里" }), mkCard("c", 400, 100)];
    const frame = mountLayer({
      cards: secretCards,
      links: [mkLink("l2", "group:g1", "c")],
    });
    const title = frame.get('[data-link-id="l2"] title').text();
    expect(title).toContain("发布计划");
    expect(title).not.toContain("秘密正文");
    expect(title).not.toContain("卡片 a");
  });
});
