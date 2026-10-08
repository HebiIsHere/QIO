/**
 * 底部工具栏 + 选中整理菜单的接线测试（子智能体 A 负责）。
 *
 * 这一版测的是**契约 §9.6 的新规则**，不是旧实现的形状：
 * - 工具栏常态只留：添加、板内搜索、撤销/重做、视图控制、右端提交区；
 *   未选中时**没有一排禁用按钮**（反例：旧实现把「所选成组 / 移出组 / 解除组 / 设为有序 /
 *   取消有序 / 合并组 / 删除所选」常驻在工具栏上，下面第一条用例会直接判它不通过）；
 * - 分组类操作跟着**所选对象**出现（SelectionMenu，由所选里的最后一张卡片渲染），
 *   多选只显示共同可用的操作；
 * - 卡片局部工具栏只放这张卡片自己的操作（编辑 / 复制 / 折叠 / 隐藏 / 书签 / 删除 / 勾选），
 *   不再出现「加入组 / 移出组」；
 * - 视图控制读写 store.boardMode（画布共用这一份状态）；
 * - 板内搜索是浮层：输入框 data-im="search"，点结果 dispatch qio:interactive:locate-card；
 * - 撤销 / 重做跟随 store.canUndo / canRedo。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import BoardToolbar from "../BoardToolbar.vue";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardCard as CardModel, BoardState } from "../../../interactive/types";

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

  it("常态只留必要入口：添加 / 搜索 / 撤销重做 / 视图 / 提交，没有一组常驻的分组按钮", () => {
    const wrapper = mount(BoardToolbar);
    for (const hook of ["board-toolbar", "add-menu", "search-toggle", "undo", "redo", "help-toggle", "submit"]) {
      expect(wrapper.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 反例（旧实现会挂）：分组类操作曾经常驻工具栏，且未选中时是一排禁用按钮
    for (const old of [
      "group-form",
      "group-join",
      "group-leave",
      "group-dissolve",
      "group-ordered",
      "group-unordered",
      "group-merge",
      "delete-selected",
    ]) {
      expect(wrapper.find('[data-im="board-toolbar"] [data-im="' + old + '"]').exists(), "工具栏不应再有 " + old).toBe(false);
    }
    // 未选中时工具栏里只剩撤销 / 重做可能不可用（收纳次要操作，而不是摆一排点不动的）
    const disabled = wrapper.findAll('[data-im="board-toolbar"] button:disabled').map((item) => item.attributes("data-im"));
    expect(disabled.every((hook) => hook === "undo" || hook === "redo"), String(disabled)).toBe(true);
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

  it("手势固定：工具栏不再有会改变手势的常驻模式，只留操作说明（契约 §10.7）", async () => {
    const wrapper = mount(BoardToolbar);
    // 反例（旧实现会挂）：选择 / 框选 / 连线 三个常驻模式按钮
    for (const old of ["mode-select", "mode-rect", "mode-link"]) {
      expect(wrapper.find('[data-im="' + old + '"]').exists(), "工具栏不应再有常驻模式 " + old).toBe(false);
    }
    expect(
      (store as unknown as { boardMode?: unknown }).boardMode,
      "store 不应再暴露可切换的指针模式",
    ).toBeUndefined();

    // 说明入口默认收起，点开后把三条固定手势讲清楚
    expect(wrapper.find('[data-im="help-popover"]').exists()).toBe(false);
    await wrapper.get('[data-im="help-toggle"]').trigger("click");
    const pop = wrapper.get('[data-im="help-popover"]');
    expect(pop.text()).toContain("空格");
    expect(pop.text()).toContain("连接点");
    expect(pop.text()).toContain("平移");
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

describe("选中对象附近的整理菜单（SelectionMenu）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    labels = [];
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
  });

  function mountCard(cardId: string, opts: { y?: number; toolbarTop?: number } = {}) {
    const card = store.board?.cards.find((item) => item.id === cardId) as CardModel;
    const selection = store.board?.selection ?? [];
    return mount(BoardCard, {
      props: {
        card,
        selected: selection.includes(cardId),
        highlight: false,
        dragging: false,
        x: card.x,
        y: opts.y ?? card.y,
        groupName: null,
        groups: (store.board?.groups ?? []).filter((group) => !group.deleted),
        toolbarLeft: 0,
        toolbarTop: opts.toolbarTop ?? card.y - 60,
        multi: selection.length > 1,
        connecting: false,
      },
    });
  }

  it("未选中时既不渲染卡片工具栏，也不渲染整理菜单", () => {
    const seed = board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" });
    store.board = seed;
    const wrapper = mountCard(seed.cards[0].id);
    expect(wrapper.find('[data-im="card-toolbar"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="selection-menu"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("选中一张：卡片工具栏里只有卡片自己的操作，分组操作在整理菜单里", async () => {
    const seed = board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" });
    store.board = board.setSelection(seed, [seed.cards[0].id]);
    const wrapper = mountCard(seed.cards[0].id);

    const toolbar = wrapper.get('[data-im="card-toolbar"]');
    for (const hook of ["check", "card-edit", "card-duplicate", "card-fold", "delete-card"]) {
      expect(toolbar.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 反例（旧实现会挂）：加入组 / 移出组 曾经在卡片工具栏里
    expect(toolbar.text()).not.toContain("移出组");
    expect(toolbar.find("select").exists()).toBe(false);

    const trigger = wrapper.get('[data-im="selection-menu"]');
    expect(trigger.attributes("aria-expanded")).toBe("false");
    expect(wrapper.find('[data-im="selection-menu-list"]').exists()).toBe(false);

    await trigger.trigger("click");
    const menu = wrapper.get('[data-im="selection-menu-list"]');
    // 单张卡片：成组、合并组都没有意义 → 不出现（不是禁用）
    expect(menu.find('[data-im="group-form"]').exists()).toBe(false);
    expect(menu.find('[data-im="group-merge"]').exists()).toBe(false);
    expect(menu.find('[data-im="group-leave"]').exists()).toBe(false);
    expect(menu.find('[data-im="delete-selected"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("两张未分组卡片：整理菜单里能成组，真的调 board.ts 纯函数并 commit", async () => {
    const seed = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一" }), { kind: "text", content: "二" });
    const ids = seed.cards.map((card) => card.id);
    store.board = board.setSelection(seed, ids);
    const wrapper = mountCard(ids[1]);

    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    const form = wrapper.get('[data-im="group-form"]');
    await form.trigger("click");

    expect(labels[0]).toContain("分成一组");
    expect(store.board?.groups).toHaveLength(1);
    expect([...(store.board?.groups[0].members ?? [])].sort()).toEqual([...ids].sort());
    wrapper.unmount();
  });

  it("多选只显示共同可用的操作：只有一张卡在组里时没有「移出组」，两张都在才有", async () => {
    const seed = twoCardsLocal();
    const grouped = board.createGroup(seed, [seed.cards[0].id], "第一组");
    const ids = grouped.cards.map((card) => card.id);
    store.board = board.setSelection(grouped, ids);

    const mixed = mountCard(ids[1]);
    await mixed.get('[data-im="selection-menu"]').trigger("click");
    expect(mixed.find('[data-im="group-leave"]').exists()).toBe(false);
    mixed.unmount();

    const both = board.createGroup(grouped, [grouped.cards[1].id], "第二组");
    store.board = board.setSelection(both, ids);
    const wrapper = mountCard(ids[1]);
    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    const menu = wrapper.get('[data-im="selection-menu-list"]');
    expect(menu.find('[data-im="group-leave"]').exists()).toBe(true);
    // 两个组 → 合并可用；两个都是普通组 → 「取消有序」不出现
    expect(menu.find('[data-im="group-merge"]').exists()).toBe(true);
    expect(menu.find('[data-im="group-unordered"]').exists()).toBe(false);
    expect(menu.find('[data-im="group-ordered"]').exists()).toBe(true);

    await menu.get('[data-im="group-merge"]').trigger("click");
    expect(labels.some((label) => label.includes("合并组"))).toBe(true);
    expect((store.board?.groups ?? []).filter((group) => !group.deleted)).toHaveLength(1);
    wrapper.unmount();
  });

  it("多选时只有所选里的最后一张卡片渲染工具栏（不再叠成一摞）", () => {
    const seed = twoCardsLocal();
    const ids = seed.cards.map((card) => card.id);
    store.board = board.setSelection(seed, ids);
    const first = mountCard(ids[0]);
    expect(first.find('[data-im="card-toolbar"]').exists()).toBe(false);
    first.unmount();
    const last = mountCard(ids[1]);
    const toolbar = last.get('[data-im="card-toolbar"]');
    expect(toolbar.attributes("data-card-id")).toBe(ids[1]);
    expect(toolbar.attributes("data-multi")).toBe("1");
    expect(toolbar.text()).toContain("多选：只显示共同适用的操作");
    last.unmount();
  });

  it("选中卡片后按下 Esc 只关菜单（不让板面把它当成取消拖动）", async () => {
    const seed = twoCardsLocal();
    const ids = seed.cards.map((card) => card.id);
    store.board = board.setSelection(seed, ids);
    const wrapper = mountCard(ids[1]);
    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    expect(wrapper.find('[data-im="selection-menu-list"]').exists()).toBe(true);

    const seen: number[] = [];
    const onWindowKeydown = (event: KeyboardEvent) => seen.push(event.keyCode);
    window.addEventListener("keydown", onWindowKeydown);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    window.removeEventListener("keydown", onWindowKeydown);

    expect(wrapper.find('[data-im="selection-menu-list"]').exists()).toBe(false);
    expect(seen).toEqual([]);
    wrapper.unmount();
  });

  function twoCardsLocal(): BoardState {
    return board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一" }), {
      kind: "text",
      content: "二",
    });
  }
});
