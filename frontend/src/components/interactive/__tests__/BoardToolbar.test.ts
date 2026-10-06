/**
 * 底部工具栏接线测试（子智能体 A 负责）。
 *
 * 关注点都是契约 §8 里会被实机验收检查的行为，而不是「能编译」：
 * - 工具栏自包含：不传任何 props，选中 / 分组 / 指针模式全部从 store 读；
 * - 添加菜单收起时五类入口不在 DOM 里，点开后五类都在，且真的走 store.commit；
 * - 整理操作按可用性禁用，点击后调 board.ts 纯函数并 commit（不是发事件让别人转发）；
 * - 模式按钮读写 store.boardMode（画布共用这一份状态）；
 * - 板内搜索是浮层：输入框 data-im="search"，点结果 dispatch qio:interactive:locate-card；
 * - 撤销 / 重做跟随 store.canUndo / canRedo。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import BoardToolbar from "../BoardToolbar.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardState } from "../../../interactive/types";

describe("BoardToolbar 接线", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    labels = [];
    // 模拟 store.commit 的效果（推撤销栈 + 保存），但跳过自动保存的定时器与网络。
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
  });

  function setBoard(next: BoardState): void {
    store.board = board.normalizeState(next);
  }

  function twoCards(): BoardState {
    return board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一" }), {
      kind: "text",
      content: "二",
    });
  }

  it("自包含：不传 props 也能渲染主要入口，且没有旧的事件桥", () => {
    const wrapper = mount(BoardToolbar);
    for (const hook of [
      "board-toolbar",
      "add-menu",
      "group-form",
      "group-join",
      "group-leave",
      "group-dissolve",
      "group-ordered",
      "group-unordered",
      "group-merge",
      "delete-selected",
      "mode-select",
      "mode-rect",
      "mode-link",
      "undo",
      "redo",
      "search-toggle",
      "submit",
    ]) {
      expect(wrapper.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 文字卡片不再单独常驻：它只出现在添加菜单里（此时菜单收起，所以不在 DOM）
    expect(wrapper.find('[data-im="add-text"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("添加菜单：收起时五类入口都不在 DOM，点开后五类都在，点击真的添加卡片", async () => {
    const wrapper = mount(BoardToolbar);
    const trigger = wrapper.get('[data-im="add-menu"]');
    expect(trigger.attributes("aria-expanded")).toBe("false");
    for (const kind of ["text", "file", "image", "code", "url"]) {
      expect(wrapper.find('[data-im="add-' + kind + '"]').exists(), kind).toBe(false);
    }

    await trigger.trigger("click");
    expect(wrapper.get('[data-im="add-menu"]').attributes("aria-expanded")).toBe("true");
    for (const kind of ["text", "file", "image", "code", "url"]) {
      expect(wrapper.find('[data-im="add-' + kind + '"]').exists(), kind).toBe(true);
    }

    await wrapper.get('[data-im="add-file"]').trigger("click");
    expect(labels[0]).toContain("添加");
    expect(labels[0]).toContain("文件");
    expect(store.board?.cards).toHaveLength(1);
    expect(store.board?.cards[0].kind).toBe("file");
    expect(store.board?.cards[0].meta.name).toBe("未命名文件");
    // 添加完菜单收起：入口不再留在 DOM 里
    expect(wrapper.find('[data-im="add-file"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("整理按可用性禁用：没选够卡片时「所选成组」不可点，选两张后真的成组", async () => {
    setBoard(twoCards());
    const wrapper = mount(BoardToolbar);
    expect(wrapper.get('[data-im="group-form"]').attributes("disabled")).toBeDefined();

    const ids = (store.board?.cards ?? []).map((card) => card.id);
    store.board = board.setSelection(store.board as BoardState, ids);
    await nextTick();

    expect(wrapper.get('[data-im="group-form"]').attributes("disabled")).toBeUndefined();
    expect(wrapper.get('[data-im="group-leave"]').attributes("disabled")).toBeDefined();
    await wrapper.get('[data-im="group-form"]').trigger("click");

    expect(labels[0]).toContain("分成一组");
    expect(store.board?.groups).toHaveLength(1);
    expect([...(store.board?.groups[0].members ?? [])].sort()).toEqual([...ids].sort());
    wrapper.unmount();
  });

  it("合并组：只有所选卡片分布在两个组里才可用，合并后只剩一个组", async () => {
    const seed = twoCards();
    const grouped = board.createGroup(board.createGroup(seed, [seed.cards[0].id], "第一组"), [seed.cards[1].id], "第二组");
    setBoard(grouped);
    const wrapper = mount(BoardToolbar);
    expect(wrapper.get('[data-im="group-merge"]').attributes("disabled")).toBeDefined();

    store.board = board.setSelection(store.board as BoardState, grouped.cards.map((card) => card.id));
    await nextTick();
    expect(wrapper.get('[data-im="group-merge"]').attributes("disabled")).toBeUndefined();

    await wrapper.get('[data-im="group-merge"]').trigger("click");
    expect(labels[0]).toContain("合并组");
    const groups = (store.board?.groups ?? []).filter((group) => !group.deleted);
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
    wrapper.unmount();
  });

  it("指针模式写 store.boardMode（画布共用），按下态跟着变", async () => {
    const wrapper = mount(BoardToolbar);
    expect(store.boardMode).toBe("select");
    expect(wrapper.get('[data-im="mode-select"]').attributes("aria-pressed")).toBe("true");

    await wrapper.get('[data-im="mode-rect"]').trigger("click");
    expect(store.boardMode).toBe("rect");
    expect(wrapper.get('[data-im="mode-rect"]').attributes("aria-pressed")).toBe("true");
    expect(wrapper.get('[data-im="mode-select"]').attributes("aria-pressed")).toBe("false");

    await wrapper.get('[data-im="mode-link"]').trigger("click");
    expect(store.boardMode).toBe("link");
    wrapper.unmount();
  });

  it("板内搜索浮层：默认不在 DOM，点开后能搜到未勾选的注释，点结果 dispatch locate-card", async () => {
    setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "预算表要重做" }));
    const cardId = store.board?.cards[0].id as string;
    const seen: string[] = [];
    const onLocate = (event: Event) => {
      const detail = (event as CustomEvent<{ cardId?: string }>).detail;
      if (detail?.cardId) seen.push(detail.cardId);
    };
    window.addEventListener("qio:interactive:locate-card", onLocate);

    const wrapper = mount(BoardToolbar);
    expect(wrapper.find('[data-im="search"]').exists()).toBe(false);

    await wrapper.get('[data-im="search-toggle"]').trigger("click");
    expect(wrapper.find('[data-im="search-panel"]').exists()).toBe(true);
    const input = wrapper.get('[data-im="search"]');
    await input.setValue("预算");
    await nextTick();

    const result = wrapper.get('[data-im="search-result"]');
    expect(result.attributes("data-card-id")).toBe(cardId);
    // 未勾选的注释能被搜到，但界面必须写明「这不等于交给 QIO」
    expect(wrapper.text()).toContain("不等于交给 QIO");
    expect(result.text()).toContain("未勾选");

    await result.trigger("click");
    expect(seen).toEqual([cardId]);
    expect(wrapper.text()).toContain("已在板面上定位");

    await wrapper.get('[data-im="search-close"]').trigger("click");
    expect(wrapper.find('[data-im="search"]').exists()).toBe(false);
    window.removeEventListener("qio:interactive:locate-card", onLocate);
    wrapper.unmount();
  });

  it("撤销 / 重做跟随 store.canUndo / canRedo", async () => {
    vi.useFakeTimers();
    try {
      setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" }));
      const wrapper = mount(BoardToolbar);
      expect(wrapper.get('[data-im="undo"]').attributes("disabled")).toBeDefined();

      store.undoStack.push(JSON.stringify(board.emptyState("board_t")));
      await nextTick();
      expect(wrapper.get('[data-im="undo"]').attributes("disabled")).toBeUndefined();

      await wrapper.get('[data-im="undo"]').trigger("click");
      expect(store.board?.cards).toHaveLength(0);
      await nextTick();
      expect(wrapper.get('[data-im="redo"]').attributes("disabled")).toBeUndefined();

      await wrapper.get('[data-im="redo"]').trigger("click");
      expect(store.board?.cards).toHaveLength(1);
      wrapper.unmount();
    } finally {
      vi.useRealTimers();
    }
  });
});
