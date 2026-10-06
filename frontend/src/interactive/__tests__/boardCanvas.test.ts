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
import type { BoardState } from "../types";

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
