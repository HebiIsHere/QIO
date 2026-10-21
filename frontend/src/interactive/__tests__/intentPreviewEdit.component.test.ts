/**
 * 预演编辑（Stage 2）的轻量组件测试：多选成组 / 连线 / 正文编辑真的接上 PATCH plan。
 *
 * 只挂 BoardPreviewLayer（不挂整个板面），-store.updateIntentPlan 换成受控 spy：
 * - Ctrl/⌘+点击多选 2 张 → 工具条出现；成组 / 连线按钮产生的 operations 正确；
 * - 双击幽灵卡 → 编辑器；保存 = 改 add 操作 content 后 PATCH；
 * - 空白正文本地拦截（不发注定失败的请求）；
 * - PATCH 失败：编辑器里如实提示，不假成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import BoardPreviewLayer from "../../components/interactive/BoardPreviewLayer.vue";
import { useInteractiveStore } from "../../stores/interactive";
import type { BoardCard, Intent } from "../types";

function previewCard(id: string, overrides: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: "reply",
    content: "结果 " + id,
    meta: {},
    x: 100,
    y: 100,
    w: 320,
    h: 240,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
    ...overrides,
  };
}

function editableIntent(): Intent {
  const a = previewCard("a", { kind: "reply", content: "结果 A" });
  const b = previewCard("b", { kind: "text", content: "文字 B", x: 460, w: 260, h: 170 });
  return {
    id: "in_1",
    boardId: "board_1",
    submissionId: null,
    title: "预演任务",
    summary: "",
    status: "proposed",
    preview: { cards: [a, b], groups: [], links: [], note: "" },
    operations: [
      { op: "add_reply_card", ref: "a", content: "结果 A" },
      { op: "add_text_card", ref: "b", content: "文字 B" },
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

function pointerDown(el: Element, x = 100, y = 100): void {
  el.dispatchEvent(
    new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y }),
  );
}

function pointerUp(options: { ctrlKey?: boolean; metaKey?: boolean } = {}): void {
  window.dispatchEvent(
    new MouseEvent("pointerup", { bubbles: true, ctrlKey: Boolean(options.ctrlKey), metaKey: Boolean(options.metaKey) }),
  );
}

function ctrlClick(wrapper: ReturnType<typeof mount>, cardId: string): void {
  const card = wrapper.find('[data-preview-card-id="' + cardId + '"]');
  expect(card.exists()).toBe(true);
  pointerDown(card.element);
  pointerUp({ ctrlKey: true });
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

describe("多选 → 成组 / 连线（PATCH plan）", () => {
  it("Ctrl+点击选中 2 张：工具条出现；成组追加 group_cards", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    ctrlClick(wrapper, "a");
    ctrlClick(wrapper, "b");
    await nextTick();

    const toolbar = wrapper.find('[data-im="preview-select-toolbar"]');
    expect(toolbar.exists()).toBe(true);
    expect(toolbar.text()).toContain("已选 2 张");

    await wrapper.find('[data-im="preview-group-action"]').trigger("click");
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const operations = patch.mock.calls[0][1];
    expect(operations[operations.length - 1]).toEqual({
      op: "group_cards",
      ref: "group_1",
      cardIds: ["a", "b"],
    });
  });

  it("恰好 2 张：连线追加 add_link（含义空、无方向）", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    ctrlClick(wrapper, "a");
    ctrlClick(wrapper, "b");
    await nextTick();

    await wrapper.find('[data-im="preview-link-action"]').trigger("click");
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const operations = patch.mock.calls[0][1];
    expect(operations[operations.length - 1]).toEqual({
      op: "add_link",
      src: "a",
      dst: "b",
      meaning: "",
      direction: false,
    });
  });

  it("普通点击会清空选择（工具条消失，不提交任何修改）", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    ctrlClick(wrapper, "a");
    ctrlClick(wrapper, "b");
    await nextTick();
    expect(wrapper.find('[data-im="preview-select-toolbar"]').exists()).toBe(true);

    pointerDown(wrapper.find('[data-preview-card-id="a"]').element);
    pointerUp();
    await nextTick();
    expect(wrapper.find('[data-im="preview-select-toolbar"]').exists()).toBe(false);
    expect(patch).not.toHaveBeenCalled();
  });
});

describe("双击编辑预演正文（PATCH plan）", () => {
  it("双击卡片打开编辑器，保存后改对应 add 操作的 content", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    await wrapper.find('[data-preview-card-id="b"]').trigger("dblclick");
    await nextTick();
    const editor = wrapper.find('[data-im="preview-editor"]');
    expect(editor.exists()).toBe(true);
    expect((editor.find("textarea").element as HTMLTextAreaElement).value).toBe("文字 B");

    const markdown = "## 小标题\n\n- 一条\n- 两条";
    await editor.find("textarea").setValue(markdown);
    await wrapper.find('[data-im="preview-editor-save"]').trigger("click");
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const operations = patch.mock.calls[0][1];
    expect(operations[1]).toMatchObject({ op: "add_text_card", ref: "b", content: markdown });
  });

  it("空白正文本地拦截：不发请求，编辑器里显示真实原因", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    await wrapper.find('[data-preview-card-id="a"]').trigger("dblclick");
    await nextTick();
    await wrapper.find('[data-im="preview-editor-input"]').setValue("   ");
    await wrapper.find('[data-im="preview-editor-save"]').trigger("click");
    await flushPromises();

    expect(patch).not.toHaveBeenCalled();
    const error = wrapper.find('[data-im="preview-editor-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("正文不能为空");
    expect(wrapper.find('[data-im="preview-editor"]').exists()).toBe(true);
  });

  it("PATCH 失败：编辑器里如实提示，不假成功", async () => {
    const store = useInteractiveStore();
    vi.spyOn(store, "updateIntentPlan").mockResolvedValue({
      ok: false,
      error: "not_editable",
    });
    const wrapper = mountLayer(editableIntent());

    await wrapper.find('[data-preview-card-id="a"]').trigger("dblclick");
    await nextTick();
    await wrapper.find('[data-im="preview-editor-input"]').setValue("改过的正文");
    await wrapper.find('[data-im="preview-editor-save"]').trigger("click");
    await flushPromises();

    const error = wrapper.find('[data-im="preview-editor-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("not_editable");
    expect(error.text()).toContain("已回到修改前");
  });

  it("已批准（approved）的预演不能编辑：双击不打开编辑器", async () => {
    const intent = editableIntent();
    intent.status = "approved";
    const wrapper = mountLayer(intent);
    await wrapper.find('[data-preview-card-id="a"]').trigger("dblclick");
    await nextTick();
    expect(wrapper.find('[data-im="preview-editor"]').exists()).toBe(false);
  });
});
