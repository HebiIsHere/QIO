/**
 * 独立验收 D5-E：本机保护失败必须可见（契约 §11.3）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 场景：浏览器不让写本机存储（隐私模式 / 配额满 / 被策略禁用）时，用户正在编辑卡片，
 * 界面必须如实说明「这份编辑保护不了」，同时保留内存里的内容 —— 不许一句解释都没有。
 *
 * 标注：【组件/DOM】挂载真实 BoardCard.vue（含 CardDraftHint）与 store；【模拟】让 setItem 抛错。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyBoardState, type BoardCard as BoardCardType } from "../../../interactive/types";
import * as imApi from "../../../services/interactive";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

function textCard(): BoardCardType {
  return {
    id: "c1",
    kind: "text",
    x: 40,
    y: 40,
    w: 240,
    h: 140,
    content: "正式正文",
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardCardType;
}

function mountCard() {
  const card = textCard();
  const store = useInteractiveStore();
  store.board = { ...emptyBoardState("board_default"), cards: [card], selection: [card.id] };
  const wrapper = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card,
      selected: true,
      highlight: false,
      dragging: false,
      x: 40,
      y: 40,
      groupName: null,
      groups: [],
      toolbarLeft: 40,
      toolbarTop: 200,
      multi: false,
      connecting: false,
    },
  });
  return { wrapper, store, card };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

describe("场景 3：本机写入失败时界面必须说明（§11.3）", () => {
  it("【组件/DOM】本机存储写入失败：编辑处要如实说明，并保留输入内容", async () => {
    const wrapper = mountCard().wrapper;
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.vm.$nextTick();

    // 浏览器拒绝写入本机存储（隐私模式 / 配额满 / 策略禁用）
    const failure = () => {
      throw new Error("本机存储不可用（模拟）");
    };
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(failure);

    const editor = wrapper.find('[data-im="card-editor"]');
    await editor.setValue("本机存不下的这段编辑");
    await wrapper.vm.$nextTick();

    const text = wrapper.text();
    expect(text, "本机写入失败后，界面上一句解释都没有").toMatch(/本机|本地|这台|关闭后|重开后|恢复副本/);
    expect(text, "只说了一句套话，没有说明这份编辑保护不了").toMatch(
      /失败|没有保存|不可用|无法|存不下|不能恢复|会丢/,
    );
    expect((editor.element as HTMLTextAreaElement).value, "写入失败时把用户输入的内容也弄丢了").toBe(
      "本机存不下的这段编辑",
    );
  });
});
