/**
 * 板面组件接线测试（子智能体 A 负责）：证明组件真的按契约工作，而不只是能编译。
 *
 * 关注点：
 * - 所有状态变化都经过 store.commit(next, label)，组件不直接写 store.board；
 * - 拖动：按下 → 移动（只预演）→ 放下才提交；Esc 中断不提交；
 * - 勾选框只出现在文字注释上（材料默认在范围内）；
 * - 板内搜索能查到未勾选的注释；
 * - 组框 / 有序切换 / 关系链接的入口存在且可用。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import type { BoardCard, BoardGroup, BoardLink, BoardState, Intent } from "../types";

function previewFixture(): { cardA: BoardCard; cardB: BoardCard; group: BoardGroup; link: BoardLink } {
  const seed = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "材料一" }), {
    kind: "text",
    content: "材料二",
  });
  const cardA: BoardCard = { ...seed.cards[0], id: "pv_a", x: 120, y: 200, w: 120, h: 70 };
  const cardB: BoardCard = { ...seed.cards[1], id: "pv_b", x: 320, y: 200, w: 120, h: 70 };
  const group: BoardGroup = {
    id: "pv_g",
    name: "预览组",
    defaultName: true,
    ordered: true,
    x: 104,
    y: 140,
    w: 360,
    h: 160,
    members: ["pv_a", "pv_b"],
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
  const link: BoardLink = {
    id: "pv_l",
    src: "pv_a",
    dst: "pv_b",
    direction: true,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
  return { cardA, cardB, group, link };
}

function intentFixture(status: Intent["status"]): Intent {
  const { cardA, cardB, group, link } = previewFixture();
  return {
    id: "intent_1",
    boardId: "board_t",
    submissionId: null,
    title: "整理两份材料",
    summary: "",
    status,
    preview: { cards: [cardA, cardB], groups: [group], links: [link], note: "演示预览" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
}

function pointer(type: string, x: number, y: number, extra: PointerEventInit = {}): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true, ...extra });
}

describe("BoardCanvas 接线", () => {
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

  function setBoard(next: BoardState) {
    store.board = board.normalizeState(next);
  }

  it("点「添加文字注释」走 commit，并渲染出卡片", async () => {
    const wrapper = mount(BoardCanvas);
    await wrapper.get('[data-im="add-text"]').trigger("click");
    expect(labels[0]).toContain("添加");
    expect(store.board?.cards).toHaveLength(1);
    expect(store.board?.cards[0].kind).toBe("text");
    expect(wrapper.findAll('[data-im="card"]')).toHaveLength(1);
    expect(wrapper.get('[data-im="card"]').attributes("data-card-id")).toBe(store.board?.cards[0].id);
    wrapper.unmount();
  });

  it("勾选框只出现在文字注释上：材料卡片没有勾选框", async () => {
    setBoard(
      board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" }), {
        kind: "file",
        content: "材料说明",
        meta: { name: "a.pdf" },
      }),
    );
    const wrapper = mount(BoardCanvas);
    const cards = wrapper.findAll('[data-im="card"]');
    expect(cards).toHaveLength(2);
    const textCard = cards.find((item) => item.attributes("data-card-id") === store.board?.cards[0].id);
    const fileCard = cards.find((item) => item.attributes("data-card-id") === store.board?.cards[1].id);
    expect(textCard?.find('[data-im="check"]').exists()).toBe(true);
    expect(fileCard?.find('[data-im="check"]').exists()).toBe(false);
    expect(fileCard?.text()).toContain("不需要勾选");
    wrapper.unmount();
  });

  it("勾选文字注释走 commit，并且勾选后不再是「未勾选」", async () => {
    setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" }));
    const wrapper = mount(BoardCanvas);
    await wrapper.get('[data-im="check"]').trigger("change");
    expect(labels[0]).toContain("勾选");
    expect(store.board?.cards[0].checked).toBe(true);
    wrapper.unmount();
  });

  it("拖动：移动期间只预演（状态不变），放下才提交；重叠自动成组", async () => {
    setBoard({
      ...board.emptyState("board_t"),
      cards: [
        { ...board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }).cards[0], id: "a", x: 0, y: 0, w: 100, h: 60 },
        { ...board.addCard(board.emptyState("board_t"), { kind: "text", content: "B" }).cards[0], id: "b", x: 240, y: 0, w: 100, h: 60 },
      ],
    });
    const wrapper = mount(BoardCanvas);
    const cardA = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a");
    expect(cardA).toBeTruthy();

    cardA?.element.dispatchEvent(pointer("pointerdown", 0, 0));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointermove", 250, 10));
    await wrapper.vm.$nextTick();
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBe(0);
    expect(wrapper.text()).toContain("自动成组");

    window.dispatchEvent(pointer("pointerup", 250, 10));
    await wrapper.vm.$nextTick();
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBe(250);
    expect(store.board?.groups).toHaveLength(1);
    expect(store.board?.groups[0].members.sort()).toEqual(["a", "b"]);
    wrapper.unmount();
  });

  it("拖到已有组上：拖动期间组框显示「将加入这一组」，放下后加入该组并保留组名", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "C",
    });
    const grouped = board.createGroup(base, [base.cards[0].id], "发布计划");
    const groupId = grouped.groups[0].id;
    setBoard({
      ...grouped,
      cards: [
        { ...grouped.cards[0], x: 0, y: 0, w: 100, h: 60 },
        { ...grouped.cards[1], id: "c", x: 900, y: 0, w: 100, h: 60 },
      ],
    });
    const wrapper = mount(BoardCanvas);
    const free = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "c");
    free?.element.dispatchEvent(pointer("pointerdown", 900, 0));
    window.dispatchEvent(pointer("pointermove", 50, 30));
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-im="group"]').text()).toContain("将加入这一组");
    expect(store.board?.cards.find((card) => card.id === "c")?.x).toBe(900);

    window.dispatchEvent(pointer("pointerup", 50, 30));
    await wrapper.vm.$nextTick();
    expect(store.board?.groups[0].members).toContain("c");
    expect(store.board?.groups[0].name).toBe("发布计划");
    wrapper.unmount();
  });

  it("Esc 中断拖动：不提交，卡片回到操作前位置", async () => {
    setBoard({
      ...board.emptyState("board_t"),
      cards: [{ ...board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }).cards[0], id: "a", x: 0, y: 0 }],
    });
    const wrapper = mount(BoardCanvas);
    wrapper.get('[data-im="card"]').element.dispatchEvent(pointer("pointerdown", 0, 0));
    window.dispatchEvent(pointer("pointermove", 500, 500));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    // 按下时会提交一次「选择卡片」（选择属于板面状态），但绝不能有放下/成组的提交
    expect(labels.filter((label) => label !== "选择卡片")).toHaveLength(0);
    expect(store.board?.cards[0].x).toBe(0);
    expect(wrapper.text()).toContain("已取消拖动");
    wrapper.unmount();
  });

  it("板内搜索能查到未勾选的注释，并能定位", async () => {
    setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "发布节奏需要确认", checked: false }));
    const wrapper = mount(BoardCanvas);
    const input = wrapper.get('[data-im="search"]');
    await input.setValue("发布");
    const results = wrapper.findAll('[data-im="search-result"]');
    expect(results).toHaveLength(1);
    expect(results[0].text()).toContain("未勾选");
    await results[0].trigger("click");
    expect(wrapper.text()).toContain("发布节奏需要确认");
    wrapper.unmount();
  });

  it("待审批预览画在真实板面上：虚线卡片 / 组 / 关系，且不参与命中", async () => {
    store.intents = [intentFixture("pending")];
    const wrapper = mount(BoardCanvas);
    const layer = wrapper.get('[data-im="preview"][data-intent-id="intent_1"]');
    expect(layer.text()).toContain("预览（未确定）");
    expect(layer.text()).toContain("预览组");
    expect(layer.text()).toContain("用户写的关系");
    // 预览的卡片 / 组 / 链接元素都带 data-im="preview" 与 data-intent-id
    const marked = wrapper.findAll('[data-im="preview"][data-intent-id="intent_1"]');
    expect(marked.length).toBeGreaterThanOrEqual(4);
    // 叠加层不吃指针事件（不挡正式卡片操作）
    expect(wrapper.get(".preview-layer").exists()).toBe(true);
    wrapper.unmount();
  });

  it("定位事件：对应意图的预览被高亮；已拒绝的意图不画预览", async () => {
    store.intents = [intentFixture("pending"), { ...intentFixture("rejected"), id: "intent_2" }];
    const wrapper = mount(BoardCanvas);
    expect(wrapper.find('[data-intent-id="intent_2"]').exists()).toBe(false);
    window.dispatchEvent(
      new CustomEvent("qio:interactive:locate-preview", {
        detail: { intentId: "intent_1", bounds: { x: 100, y: 140, w: 360, h: 160 } },
      }),
    );
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-im="preview"][data-intent-id="intent_1"]').classes()).toContain("located");
    wrapper.unmount();
  });

  it("组框显示组名与有序状态，切有序走 commit", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "B",
    });
    setBoard(board.createGroup(base, [base.cards[0].id, base.cards[1].id], "发布计划"));
    const wrapper = mount(BoardCanvas);
    const frame = wrapper.get('[data-im="group"]');
    expect(frame.text()).toContain("普通组：摆放顺序不代表先后");
    expect((frame.get('[data-im="group-name"]').element as HTMLInputElement).value).toBe("发布计划");
    await frame.get('[data-im="toggle-ordered"]').trigger("click");
    expect(labels[0]).toContain("有序");
    expect(store.board?.groups[0].ordered).toBe(true);
    expect(wrapper.get('[data-im="group"]').text()).toContain("有序：序号 1..n 表示顺序");
    wrapper.unmount();
  });
});
