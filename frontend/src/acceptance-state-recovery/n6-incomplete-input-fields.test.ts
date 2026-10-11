/**
 * 【独立验收 D · 第八轮】N6：未完成的附加字段（不止正文）必须一起保存与恢复。
 *
 * 触发（契约 §4 / 本轮反例 N6）：
 *   编辑卡片正文和附加字段，不完成编辑 → 刷新或正常关闭重开 → 再打开编辑。
 * 基线现状：草稿只存正文；网址 / 标题 / 文件名 / 图片名 / 代码语言在重开后丢回卡片原值。
 * 正确行为：正文 + 适用附加字段（含空值）作为完整未完成输入共同保存恢复；
 *   兼容旧的「仅正文」记录；恢复不自动形成正式改动。
 *
 * 证据分层：②真实组件 DOM（真实 BoardCard 编辑入口 + 真实 store / localStorage）。
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../components/interactive/BoardCard.vue";
import { useInteractiveStore } from "../stores/interactive";
import { cardLocalDraftStorageKey } from "../interactive/drafts";
import type { BoardCard as BoardCardType } from "../interactive/types";
import { card, payload, state } from "./support/harness";

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

import * as api from "../services/interactive";

let wrapper: VueWrapper | null = null;

function mountCard(cardValue: BoardCardType): VueWrapper {
  const store = useInteractiveStore();
  store.board = { ...state(3, [cardValue]), selection: [cardValue.id] };
  wrapper = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card: cardValue,
      selected: true,
      highlight: false,
      dragging: false,
      x: 0,
      y: 0,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: 0,
      multi: false,
      connecting: false,
    },
  });
  return wrapper;
}

async function openEditor(w: VueWrapper): Promise<void> {
  await w.find('[data-im="card-edit"]').trigger("click");
  await w.vm.$nextTick();
}

/** 依次填入正文与附加字段（正文输入框 + 该类型适用的字段输入框）。 */
async function fillEditor(w: VueWrapper, text: string, fields: string[]): Promise<void> {
  await w.find('[data-im="card-editor"]').setValue(text);
  const inputs = w.findAll('input[type="text"]');
  expect(inputs.length, "这个卡片的附加字段数量与预期不符").toBe(fields.length);
  for (let index = 0; index < fields.length; index += 1) {
    await inputs[index].setValue(fields[index]);
  }
  await w.vm.$nextTick();
}

/** 真实的「正常关闭重开」等价物：同一份本机存储 + 新 pinia + 新 store 实例。 */
async function reopenStore(cardValue: BoardCardType): Promise<ReturnType<typeof useInteractiveStore>> {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  await store.load();
  await flushPromises();
  expect(store.board!.cards.map((c) => c.id)).toEqual([cardValue.id]);
  return store;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t" } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.clearAllMocks();
});

describe("N6 未完成的附加字段", () => {
  const cases: { name: string; value: BoardCardType; text: string; fields: string[]; labels: string[] }[] = [
    {
      name: "网址（地址 + 标题）",
      value: card("c_url", "原始说明", { kind: "url", meta: { href: "https://旧.example", title: "旧标题" } }),
      text: "网址卡的新正文",
      fields: ["https://新.example/path", "新标题"],
      labels: ["href", "title"],
    },
    {
      name: "代码（语言）",
      value: card("c_code", "// 原始代码", { kind: "code", meta: { language: "typescript" } }),
      text: "// 新的未完成代码",
      fields: ["python"],
      labels: ["language"],
    },
    {
      name: "文件（名称）",
      value: card("c_file", "文件说明", { kind: "file", meta: { name: "旧文件名.pdf" } }),
      text: "文件的新说明",
      fields: ["新文件名.pdf"],
      labels: ["name"],
    },
  ];

  for (const item of cases) {
    it(item.name + "：未完成输入在关闭重开后完整恢复，且不自动形成正式改动", async () => {
      vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [item.value]));

      const first = mountCard(item.value);
      await openEditor(first);
      await fillEditor(first, item.text, item.fields);

      // 不点「完成编辑」就正常关闭重开
      first.unmount();
      wrapper = null;
      const reopened = await reopenStore(item.value);
      // 恢复不自动形成正式改动
      expect(
        reopened.board!.cards[0].content,
        "重开时把未完成输入自动变成了正式内容",
      ).toBe(item.value.content);

      const second = mountCard(item.value);
      await openEditor(second);
      expect(
        (second.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value,
        "正文没有恢复",
      ).toBe(item.text);
      const inputs = second.findAll('input[type="text"]');
      for (let index = 0; index < item.labels.length; index += 1) {
        expect(
          (inputs[index].element as HTMLInputElement).value,
          "附加字段 " + item.labels[index] + " 没有随未完成输入一起恢复（N6）",
        ).toBe(item.fields[index]);
      }
    });
  }

  it("兼容旧的「仅正文」记录：正文恢复，附加字段沿用卡片原值", async () => {
    const value = card("c_url", "原始说明", { kind: "url", meta: { href: "https://旧.example", title: "旧标题" } });
    vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [value]));
    // 旧格式记录：只有正文，没有附加字段、没有 kind
    localStorage.setItem(
      cardLocalDraftStorageKey("c_url"),
      JSON.stringify({ text: "旧格式只存下来的正文", updatedAt: Date.now(), seq: 1 }),
    );

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.cardDraftText("c_url")).toBe("旧格式只存下来的正文");

    const w = mountCard(value);
    await openEditor(w);
    expect((w.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value).toBe(
      "旧格式只存下来的正文",
    );
    const inputs = w.findAll('input[type="text"]');
    expect((inputs[0].element as HTMLInputElement).value).toBe("https://旧.example");
    expect((inputs[1].element as HTMLInputElement).value).toBe("旧标题");
  });
});
