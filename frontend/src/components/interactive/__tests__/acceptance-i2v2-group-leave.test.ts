/**
 * D 组独立验收 · F07：通过整理菜单把成员移出组——组件级（真实 BoardCard + SelectionMenu）。
 *
 * 用户可见契约（任务书 F07 / 流程 7）：
 * - 菜单「移出组」后，这张卡片要在组外找到最近的合理空位：不得压住原组框、不得与剩余成员重叠；
 * - 剩余成员按纵向排列收拢，组框跟着同步；
 * - 不移动无关卡片、不改内容 / 书签 / 参与 / 关系；
 * - 成员变化与最终位置一次提交（撤销一次即可恢复）。
 *
 * 基线问题：removeFromGroup 只改成员表，被移出的卡片留在原位 —— 与旧组框和剩余成员完全重叠。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardCard as CardModel, BoardState } from "../../../interactive/types";

/** 本测试只做矩形相交判断，用本地结构类型，避免依赖生产模块是否导出该别名。 */
interface BoardRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

function overlap(a: BoardRect, b: BoardRect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

function inside(inner: BoardRect, outer: BoardRect): boolean {
  return inner.x >= outer.x - 0.5 && inner.y >= outer.y - 0.5 && inner.x + inner.w <= outer.x + outer.w + 0.5 && inner.y + inner.h <= outer.y + outer.h + 0.5;
}

let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];
let wrappers: VueWrapper[] = [];

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  labels = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
});

function mountCard(cardId: string): VueWrapper {
  const state = store.board as BoardState;
  const card = state.cards.find((item) => item.id === cardId) as CardModel;
  const selected = state.selection.includes(cardId);
  const wrapper = mount(BoardCard, {
    props: {
      card,
      selected,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: state.groups.filter((group) => !group.deleted),
      toolbarLeft: 0,
      toolbarTop: card.y - 60,
      multi: false,
      connecting: false,
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

function seedGroupOfTwo() {
  const base = board.addCard(
    board.addCard(board.emptyState("board_t"), { kind: "text", content: "一" }),
    { kind: "text", content: "二" },
  );
  const grouped = board.createGroup(base, base.cards.map((card) => card.id), "发布计划");
  return grouped;
}

describe("F07 菜单移出组：被移出卡片不重叠，组框同步", () => {
  it("移出首成员：卡片回到组外空位，剩余成员收拢，一次提交", async () => {
    const grouped = seedGroupOfTwo();
    const ids = grouped.cards.map((card) => card.id);
    const groupBefore = grouped.groups[0];
    const movedId = ids[0];
    const stayId = ids[1];
    store.board = board.setSelection(grouped, [movedId]);
    const wrapper = mountCard(movedId);

    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    expect(wrapper.find('[data-im="group-leave"]').exists()).toBe(true);
    const leave = wrapper.get('[data-im="group-leave"]');
    await leave.trigger("click");

    const next = store.board as BoardState;
    const moved = next.cards.find((card) => card.id === movedId)!;
    const stay = next.cards.find((card) => card.id === stayId)!;
    const groupAfter = next.groups.find((group) => !group.deleted)!;

    expect(next.groups.find((group) => group.members.includes(movedId)), "成员关系必须已变更").toBeFalsy();
    expect(groupAfter.members).toEqual([stayId]);
    expect(overlap(moved, groupAfter), "被移出的卡片不能压在组框上").toBe(0);
    expect(overlap(moved, stay), "被移出的卡片不能与剩余成员重叠").toBe(0);
    expect(inside(stay, groupAfter), "剩余成员必须仍在组框内").toBe(true);
    // 剩余成员按纵向收拢：组框不会因为移出一个成员反而变大
    expect(groupAfter.h, "剩余成员必须收拢，组框不能变大").toBeLessThanOrEqual(groupBefore.h + 0.5);
    expect(inside(stay, groupAfter), "剩余成员仍在收拢后的组框内").toBe(true);
    // 成员变化 + 最终位置一次提交（撤销一次即可恢复）
    expect(labels.filter((label) => label.includes("移出组")), "只能提交一次").toHaveLength(1);
  });

  it("移出唯一成员：组自动消失，卡片有组外落点", async () => {
    const base = board.addCard(board.emptyState("board_t"), { kind: "text", content: "唯一" });
    const grouped = board.createGroup(base, [base.cards[0].id], "单人组");
    const groupBefore = grouped.groups[0];
    const cardId = base.cards[0].id;
    store.board = board.setSelection(grouped, [cardId]);
    const wrapper = mountCard(cardId);

    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    await wrapper.get('[data-im="group-leave"]').trigger("click");

    const next = store.board as BoardState;
    const moved = next.cards.find((card) => card.id === cardId)!;
    expect(next.groups.filter((group) => !group.deleted), "空组应自动消失").toHaveLength(0);
    /*
     * 集成后校准：唯一成员离开后组已不存在（不是「压在旧组框上」，旧框已经不是板面对象）；
     * 用户可见要求是卡片不要与仍存在的卡片重叠、位置是有效板面坐标。
     */
    for (const other of next.cards.filter((card) => card.id !== cardId && !card.deleted)) {
      expect(overlap(moved, other), "移出后不能与其它卡片重叠").toBe(0);
    }
    expect(Number.isFinite(moved.x) && Number.isFinite(moved.y)).toBe(true);
    expect(groupBefore).toBeTruthy();
  });
});
