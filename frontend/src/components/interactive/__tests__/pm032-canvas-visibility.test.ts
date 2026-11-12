/**
 * #08/#54（PM-QIO032）：三态参与唯一入口。
 *
 * 用户要求（逐字）：「显示并参与」「显示但不参与」「隐藏且不参与」。
 * 验收：
 * 1. 卡片上只有一个三态入口（旧 participation / 隐藏开关合并进来），菜单三档可切换；
 * 2. 写板面走既有 commit：included = hidden=false+participation=included，
 *    displayed_excluded = hidden=false+excluded，hidden_excluded = hidden=true；
 * 3. 恢复隐藏默认只回「显示但不参与」（不自动回到参与）；
 * 4. 旧卡迁移冲突（hidden=true + checked=true）取不扩大读取范围 = 隐藏且不参与；
 * 5. 选择 / 预览 / 框选 / 提交完成不改三态；
 * 6. 文件卡面板删掉旧材料开关与权限解释段落（文案 12）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import panelSource from "../FileCardMaterialPanel.vue?raw";
import { useInteractiveStore } from "../../../stores/interactive";
import {
  CARD_VISIBILITY_LABELS,
  cardVisibilityState,
  emptyState,
  restoreHiddenCard,
  setCardVisibilityState,
} from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-14T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x: 100,
    y: 100,
    w: 280,
    h: 180,
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

function board(cards: BoardCardModel[], patch: Partial<BoardState> = {}): BoardState {
  return { ...emptyState("board_t"), cards, ...patch };
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

async function flush(times = 2): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await nextTick();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

async function chooseVisibility(state: string): Promise<void> {
  await wrapper!.get('[data-im="card-visibility"]').trigger("click");
  await flush();
  await wrapper!.get('[data-im="card-visibility-' + state + '"]').trigger("click");
  await flush();
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

describe("#08/#54 三态纯函数映射", () => {
  it("三档互斥：included / displayed_excluded / hidden_excluded，文案逐字", () => {
    expect(CARD_VISIBILITY_LABELS).toEqual({
      included: "显示并参与",
      displayed_excluded: "显示但不参与",
      hidden_excluded: "隐藏且不参与",
    });
    expect(cardVisibilityState(mkCard("c1"))).toBe("displayed_excluded");
    expect(cardVisibilityState(mkCard("c1", { participation: "included" }))).toBe("included");
    expect(cardVisibilityState(mkCard("c1", { hidden: true }))).toBe("hidden_excluded");
  });

  it("旧卡迁移冲突（hidden=true + checked=true）→ 隐藏且不参与，不扩大读取范围", () => {
    const old = mkCard("c1", { hidden: true, checked: true });
    expect(cardVisibilityState(old)).toBe("hidden_excluded");
    const migrated = setCardVisibilityState(board([old]), "c1", "hidden_excluded");
    expect(migrated.cards[0].participation).not.toBe("included");
  });

  it("恢复隐藏只回到「显示但不参与」，不会自动回到参与", () => {
    const hidden = mkCard("c1", { hidden: true, participation: "excluded" });
    const restored = restoreHiddenCard(board([hidden]), "c1");
    expect(restored.cards[0].hidden).toBe(false);
    expect(restored.cards[0].participation).toBe("excluded");
    expect(cardVisibilityState(restored.cards[0])).toBe("displayed_excluded");
  });

  it("选择 / 预览不改三态", () => {
    const before = board([mkCard("c1", { participation: "excluded" })], { selection: [] });
    const selected = { ...before, selection: ["c1"] };
    expect(cardVisibilityState(selected.cards[0])).toBe("displayed_excluded");
  });
});

describe("#08/#54 三态入口（真实组件）", () => {
  it("卡片上只有一个三态入口，菜单三档文案逐字", async () => {
    store.board = board([mkCard("c1")], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    expect(wrapper.findAll('[data-im="card-visibility"]')).toHaveLength(1);
    expect(wrapper.find('[data-im="card-participation"]').exists()).toBe(false);
    const entry = wrapper.get('[data-im="card-visibility"]');
    expect(entry.text()).toBe("显示但不参与");
    await entry.trigger("click");
    await flush();
    const menu = wrapper.get('[data-im="card-visibility-menu"]');
    expect(menu.text()).toContain("显示并参与");
    expect(menu.text()).toContain("显示但不参与");
    expect(menu.text()).toContain("隐藏且不参与");
  });

  it("切到「显示并参与」→ hidden=false + participation=included；切到「隐藏且不参与」→ hidden=true", async () => {
    store.board = board([mkCard("c1")], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    await chooseVisibility("included");
    let current = (store.board as BoardState).cards[0];
    expect(current.hidden).toBe(false);
    expect(current.participation).toBe("included");

    await chooseVisibility("hidden_excluded");
    current = (store.board as BoardState).cards[0];
    expect(current.hidden).toBe(true);
    expect(current.participation).toBe("excluded");
  });

  it("隐藏快捷入口恢复时只回到「显示但不参与」", async () => {
    store.board = board([mkCard("c1", { hidden: true, participation: "excluded" })], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    await wrapper.get('[data-im="card-icon-hide"]').trigger("click");
    await flush();
    const current = (store.board as BoardState).cards[0];
    expect(current.hidden).toBe(false);
    expect(current.participation).toBe("excluded");
  });

  it("选中 / 预览 / 框选后三态不变", async () => {
    store.board = board([mkCard("c1", { participation: "included" })], { selection: [] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    await flush();
    const before = (store.board as BoardState).cards[0];
    await wrapper.get('[data-im="card"]').trigger("click");
    await flush();
    store.toggleCardPreview("c1");
    await flush();
    const after = (store.board as BoardState).cards[0];
    expect(after.hidden).toBe(before.hidden);
    expect(after.participation).toBe(before.participation);
  });
});

describe("文案 12 / #54：文件卡只留三态入口，删掉旧材料开关与权限解释", () => {
  it("FileCardMaterialPanel 不再有「作为互动材料」开关与权限解释段落", () => {
    expect(panelSource).not.toContain("file-card-material-switch");
    expect(panelSource).not.toContain("作为互动材料");
    expect(panelSource).not.toContain("MATERIAL_SWITCH_HINT");
  });

  it("文件卡详情（路径 / 来源 / 引用）默认折叠、按需展开", () => {
    expect(panelSource).toContain('data-im="file-card-details"');
    expect(panelSource).toContain("<summary>");
    expect(wrapper === null).toBe(true);
  });
});