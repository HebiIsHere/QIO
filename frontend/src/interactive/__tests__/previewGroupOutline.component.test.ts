/**
 * 预览组：贴合成员的轮廓 + 头部拖动（问题 1 / 4c 的用户验收点）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

import BoardPreviewLayer from "../../components/interactive/BoardPreviewLayer.vue";
import { useInteractiveStore } from "../../stores/interactive";
import type { BoardCard, BoardGroup, Intent } from "../types";

function previewCard(id: string, x: number, y: number, w = 320, h = 240): BoardCard {
  return {
    id,
    kind: "reply",
    content: "结果 " + id,
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
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
  };
}

function previewGroup(members: string[]): BoardGroup {
  return {
    id: "g1",
    name: "论文组",
    defaultName: false,
    ordered: true,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    members,
    deleted: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
  };
}

function makeIntent(group: BoardGroup | null): Intent {
  return {
    id: "in_1",
    boardId: "board_1",
    submissionId: null,
    title: "预演任务",
    summary: "",
    status: "proposed",
    preview: {
      cards: [previewCard("a", 100, 100), previewCard("b", 460, 100, 260, 170)],
      groups: group ? [group] : [],
      links: [],
      note: "",
    },
    operations: [
      { op: "add_reply_card", ref: "a", content: "结果 a", x: 100, y: 100 },
      { op: "add_text_card", ref: "b", content: "结果 b", x: 460, y: 100 },
    ],
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
  } as Intent;
}

let pinia: ReturnType<typeof createPinia>;
const mounted: ReturnType<typeof mount>[] = [];

function mountLayer(intent: Intent) {
  const wrapper = mount(BoardPreviewLayer, {
    props: {
      intents: [intent],
      locatedIntentId: null,
      width: 2400,
      height: 1600,
      toBoardPoint: (x: number, y: number) => ({ x, y }),
    },
    attachTo: document.body,
    global: { plugins: [pinia] },
  });
  mounted.push(wrapper);
  return wrapper;
}

function pointerDown(el: Element, x: number, y: number): void {
  el.dispatchEvent(
    new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y }),
  );
}

function pointerMove(x: number, y: number): void {
  window.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: x, clientY: y }));
}

function pointerUp(): void {
  window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }));
}

beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
});

afterEach(() => {
  for (const wrapper of mounted.splice(0)) wrapper.unmount();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("预览组轮廓（贴合成员）", () => {
  it("组轮廓与头部按成员虚卡位置画出；头部显示组名 / 有序 / 张数", () => {
    const wrapper = mountLayer(makeIntent(previewGroup(["a", "b"])));
    const outline = wrapper.find('[data-im="preview-group-outline"]');
    expect(outline.exists()).toBe(true);
    // 轮廓 svg 按成员包围盒定位（不是占位的 0/0 320×240）
    expect(outline.attributes("style") ?? "").toContain("left: 84px");
    const head = wrapper.find('[data-im="preview-group-head"]');
    expect(head.exists()).toBe(true);
    expect(head.text()).toContain("论文组");
    expect(head.text()).toContain("有序");
    expect(head.text()).toContain("2 张");
  });

  it("组成员不在预览卡片里：不画大空框（也不画头部）", () => {
    const wrapper = mountLayer(makeIntent(previewGroup(["missing_1", "missing_2"])));
    expect(wrapper.find('[data-im="preview-group-outline"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="preview-group-head"]').exists()).toBe(false);
  });
});

describe("整组拖动（改计划坐标后 PATCH）", () => {
  it("按住组头部拖动：所有成员虚卡的 add 操作同位移，一次 PATCH", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(makeIntent(previewGroup(["a", "b"])));

    const head = wrapper.find('[data-im="preview-group-head"]');
    pointerDown(head.element, 100, 100);
    pointerMove(150, 140);
    pointerUp();
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const operations = patch.mock.calls[0][1];
    expect(operations[0]).toMatchObject({ x: 150, y: 140 });
    expect(operations[1]).toMatchObject({ x: 510, y: 140 });
  });
});
