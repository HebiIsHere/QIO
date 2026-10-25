/**
 * F07（B 组）· 通过菜单移出组时，卡片离开组框并避开剩余成员（不叠放）。
 *
 * 基线缺陷（9b23634）：board.removeFromGroup 只改成员列表、卡片位置不动；
 * 同时 arrangeGroupMembers 把剩余成员重排到组顶 —— 移出第一 / 中间成员时，
 * 剩余成员正好挪进被移出卡片的旧位置，两张卡叠在一起。
 *
 * 断言的是真实行为：成员结果、卡片实际矩形、组框是否同步、无关卡片是否被移动，
 * 以及「成员变化 + 最终位置」是否一次提交（撤销一次恢复、重做一致）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import SelectionMenu from "../../components/interactive/SelectionMenu.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import type { BoardCard, BoardGroup, BoardState } from "../types";

vi.mock("../../services/interactive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/interactive")>();
  return {
    ...actual,
    fetchBoardState: vi.fn(),
    saveBoardState: vi.fn(async (_boardId: string, putState: unknown) => ({
      ok: true,
      seq: 1,
      savedAt: "2026-10-25T00:00:00.000Z",
      state: putState,
    })),
    saveDrafts: vi.fn(async () => ({ drafts: {}, updatedAt: "2026-10-25T00:00:00.000Z" })),
    fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
    fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
    checkMaterialImpact: vi.fn(async () => ({
      ok: true,
      checkId: "chk",
      stateVersion: 0,
      affected: [],
      impactConfirmationRequired: false,
    })),
  };
});

const STAMP = "2026-10-25T00:00:00.000Z";
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: patch.kind ?? "text",
    content: patch.content ?? "卡片 " + id,
    meta: patch.meta ?? {},
    x,
    y,
    w: patch.w ?? 100,
    h: patch.h ?? 60,
    checked: patch.checked ?? false,
    hidden: patch.hidden ?? false,
    folded: patch.folded ?? false,
    bookmarked: patch.bookmarked ?? false,
    deleted: patch.deleted ?? false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: patch.name ?? "组 " + id,
    defaultName: patch.defaultName ?? true,
    ordered: patch.ordered ?? false,
    x: patch.x ?? 0,
    y: patch.y ?? 0,
    w: patch.w ?? 1,
    h: patch.h ?? 1,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkState(cards: BoardCard[], groups: BoardGroup[] = [], selection: string[] = []): BoardState {
  return board.normalizeState({ ...board.emptyState("board_t"), cards, groups, selection });
}

/** 一个三成员组：a / b / c 纵向排列在 x=100，组框由成员推导。 */
function threeMemberState(): BoardState {
  return mkState(
    [
      mkCard("a", 100, 100),
      mkCard("b", 100, 172),
      mkCard("c", 100, 244),
      mkCard("d", 1000, 700, { content: "无关卡片", bookmarked: true }),
    ],
    [mkGroup("g1", ["a", "b", "c"], { name: "发布计划" })],
  );
}

/** 某张卡片与所有未删除卡片 / 组框（除自己所在组以外的真实重叠）。 */
function overlapsAnything(state: BoardState, cardId: string): string[] {
  const card = board.cardById(state, cardId)!;
  const rect = board.cardRect(card);
  const hits: string[] = [];
  for (const other of state.cards) {
    if (other.deleted || other.id === cardId) continue;
    if (board.rectsOverlap(rect, board.cardRect(other))) hits.push(other.id);
  }
  for (const group of state.groups) {
    if (group.deleted || !group.members.length) continue;
    if (board.rectsOverlap(rect, board.groupFrame(group))) hits.push("group:" + group.id);
  }
  return hits;
}

describe("F07 纯函数：菜单移出组 = 组外最近合理空位 + 剩余成员收拢", () => {
  it("移出中间成员：剩余成员按纵向排列收拢，被移出卡片不与任何卡片 / 组框重叠", () => {
    const state = threeMemberState();
    const before = { x: 100, y: 172 };
    const out = board.leaveGroupToFreeSpots(state, ["b"]);

    expect(out.limited).toBe(false);
    expect(out.placements).toHaveLength(1);
    const placement = out.placements[0];
    expect(placement).toMatchObject({ cardId: "b", groupId: "g1", free: true, moved: true });
    expect(out.state.groups).toHaveLength(1);
    const group = board.groupById(out.state, "g1")!;
    expect(group.members).toEqual(["a", "c"]);
    // 剩余成员按既定纵向排列与间距收拢
    expect(board.cardById(out.state, "a")).toMatchObject({ x: group.x + 16, y: group.y + 58 });
    expect(board.cardById(out.state, "c")).toMatchObject({ x: group.x + 16, y: group.y + 58 + 60 + 12 });
    // 组框框得住成员
    for (const id of group.members) {
      const card = board.cardById(out.state, id)!;
      expect(card.x).toBeGreaterThanOrEqual(group.x);
      expect(card.y).toBeGreaterThanOrEqual(group.y);
      expect(card.x + card.w).toBeLessThanOrEqual(group.x + group.w);
      expect(card.y + card.h).toBeLessThanOrEqual(group.y + group.h);
    }
    // 被移出的卡片：不在原位置、与所有卡片 / 组框零重叠
    const removed = board.cardById(out.state, "b")!;
    expect({ x: removed.x, y: removed.y }).not.toEqual(before);
    expect(overlapsAnything(out.state, "b")).toEqual([]);
    expect(placement.x).toBe(removed.x);
    expect(placement.y).toBe(removed.y);
    // 无关卡片分毫不动，内容 / 书签 / 参与状态不变
    expect(board.cardById(out.state, "d")).toMatchObject({ x: 1000, y: 700, content: "无关卡片", bookmarked: true });
    expect(board.cardById(out.state, "a")!.content).toBe("卡片 a");
  });

  it("移出第一张：卡片移到组外（不再压住上移后的剩余成员）", () => {
    const out = board.leaveGroupToFreeSpots(threeMemberState(), ["a"]);
    expect(out.limited).toBe(false);
    const group = board.groupById(out.state, "g1")!;
    expect(group.members).toEqual(["b", "c"]);
    expect(board.cardById(out.state, "b")).toMatchObject({ y: group.y + 58 });
    expect(board.cardById(out.state, "c")).toMatchObject({ y: group.y + 58 + 60 + 12 });
    const removed = board.cardById(out.state, "a")!;
    expect(overlapsAnything(out.state, "a")).toEqual([]);
    // 「最近」：向上让开（离原位置最近的一个方向），而不是被丢到板面另一头
    expect(removed.y).toBeLessThan(100);
    expect(Math.abs(removed.x - 100)).toBeLessThanOrEqual(1);
  });

  it("移出最后一张：卡片落在缩小的组框下方，不与剩余成员重叠（基线会压住组框）", () => {
    const out = board.leaveGroupToFreeSpots(threeMemberState(), ["c"]);
    expect(out.limited).toBe(false);
    const group = board.groupById(out.state, "g1")!;
    expect(group.members).toEqual(["a", "b"]);
    const removed = board.cardById(out.state, "c")!;
    expect(overlapsAnything(out.state, "c")).toEqual([]);
    expect(removed.y).toBeGreaterThanOrEqual(group.y + group.h + 16 - 0.001);
    expect(removed.x).toBe(100);
  });

  it("唯一成员：组自动消失，卡片留在原地（本来就是空位）；没有需要收拢的成员", () => {
    const state = mkState([mkCard("a", 100, 100)], [mkGroup("g1", ["a"], { name: "单人组" })]);
    const out = board.leaveGroupToFreeSpots(state, ["a"]);
    expect(out.limited).toBe(false);
    expect(out.state.groups).toHaveLength(0);
    expect(out.placements[0]).toMatchObject({ cardId: "a", free: true, moved: false, x: 100, y: 100 });
  });

  it("跨组边界：空位必须避开另一个组的组框，且不移动那一组的成员", () => {
    // g1 = a / b 紧挨着 g2（g2 的框正好压在 b 被向上收拢后的位置附近）
    const state = mkState(
      [mkCard("a", 100, 100), mkCard("b", 100, 172), mkCard("c", 100, 220), mkCard("d", 1000, 700)],
      [mkGroup("g1", ["a", "b"], { name: "第一组" }), mkGroup("g2", ["c"], { name: "第二组" })],
    );
    const cBefore = { ...board.cardById(state, "c")! };
    const out = board.leaveGroupToFreeSpots(state, ["b"]);
    expect(out.limited).toBe(false);
    const removed = board.cardById(out.state, "b")!;
    const group2 = board.groupById(out.state, "g2")!;
    expect(board.rectsOverlap(board.cardRect(removed), board.groupFrame(group2))).toBe(false);
    expect(overlapsAnything(out.state, "b")).toEqual([]);
    // 另一个组的成员分毫不动
    expect(board.cardById(out.state, "c")).toMatchObject({ x: cBefore.x, y: cBefore.y });
    expect(board.groupById(out.state, "g2")!.members).toEqual(["c"]);
  });

  it("密集布局找不到完全空位时如实报告 limited / conflicts，不静默把两张卡放同一位置", () => {
    // 一张巨大的障碍卡片把被移出卡片整个包住：任何方向的位移都超过避让上限。
    const wall = mkCard("wall", -5000, -5000, { w: 10000, h: 10000 });
    const state = mkState([mkCard("a", 100, 100), mkCard("b", 100, 172), wall], [mkGroup("g1", ["a", "b"])]);
    const out = board.leaveGroupToFreeSpots(state, ["b"]);
    expect(out.limited).toBe(true);
    const placement = out.placements[0];
    expect(placement.free).toBe(false);
    expect(placement.conflicts).toContain("wall");
    // 明确报告了实际落点（仍重叠），而不是假装让开
    expect(board.cardById(out.state, "b")).toMatchObject({ x: placement.x, y: placement.y });
    expect(overlapsAnything(out.state, "b")).not.toEqual([]);
  });

  it("不修改无关卡片、已有关系与参与/书签/内容", () => {
    let state = threeMemberState();
    state = board.addLink(state, "a", "d", false, "既有关系");
    state = board.setBookmark(state, "b", true);
    state = board.setParticipation(state, "b", false);
    const before = JSON.parse(JSON.stringify(state)) as BoardState;
    const out = board.leaveGroupToFreeSpots(state, ["b"]);
    expect(out.state.links).toEqual(before.links);
    expect(board.cardById(out.state, "b")!.bookmarked).toBe(true);
    expect(board.participationOf(board.cardById(out.state, "b")!)).toBe("excluded");
    expect(board.cardById(out.state, "b")!.content).toBe(before.cards.find((card) => card.id === "b")!.content);
    // d 一字段都不改
    const dBefore = before.cards.find((card) => card.id === "d")!;
    expect(board.cardById(out.state, "d")).toMatchObject({
      x: dBefore.x,
      y: dBefore.y,
      w: dBefore.w,
      h: dBefore.h,
      content: dBefore.content,
      bookmarked: dBefore.bookmarked,
    });
  });
});

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

function stubGeometry(wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

/** 组内成员条上的「移出」按钮（组框由成员推导；面板 v-show 隐藏但仍在 DOM 里）。 */
async function clickLeaveMember(wrapper: VueWrapper, cardId: string) {
  const button = wrapper
    .findAll('[data-im="leave-member"]')
    .find((item) => item.attributes("data-card-id") === cardId);
  expect(button, "组内成员条必须提供「移出」入口").toBeTruthy();
  await button!.trigger("click");
  await wrapper.vm.$nextTick();
}

describe("F07 组件：菜单移出组 = 一次提交（位置 + 成员）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount();
    document.body.innerHTML = "";
  });

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

  function setBoard(next: BoardState) {
    store.board = board.normalizeState(next);
  }

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  it("组内「移出」中间成员：成员 + 位置一次提交，卡片不压住剩余成员，无关卡片不动", async () => {
    let state = threeMemberState();
    state = board.addLink(state, "a", "d", false, "既有关系");
    state = board.setBookmark(state, "b", true);
    setBoard(state);
    const dBefore = { ...board.cardById(store.board!, "d")! };
    const wrapper = mountCanvas();
    await clickLeaveMember(wrapper, "b");

    expect(labels).toEqual(["把卡片移出组（自动避开重叠）"]);
    const after = store.board!;
    expect(board.groupById(after, "g1")!.members).toEqual(["a", "c"]);
    expect(overlapsAnything(after, "b")).toEqual([]);
    expect(after.links).toEqual(state.links);
    expect(board.cardById(after, "b")!.bookmarked).toBe(true);
    expect(board.cardById(after, "d")).toMatchObject({ x: dBefore.x, y: dBefore.y, content: dBefore.content });
    // 回执说明实际做了什么
    expect(wrapper.text()).toContain("已移出组");
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("密集布局没有空位时给出明确回执（不静默叠放）", async () => {
    const wall = mkCard("wall", -5000, -5000, { w: 10000, h: 10000 });
    setBoard(mkState([mkCard("a", 100, 100), mkCard("b", 100, 172), wall], [mkGroup("g1", ["a", "b"])]));
    const wrapper = mountCanvas();
    await clickLeaveMember(wrapper, "b");
    expect(labels).toHaveLength(1);
    expect(wrapper.text()).toContain("没有找到完全不重叠的空位");
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("多选「移出组」（整理菜单）：一次提交，空组消失，卡片各自找到组外空位", async () => {
    setBoard(mkState([mkCard("a", 100, 100), mkCard("b", 100, 172)], [mkGroup("g1", ["a", "b"])], ["a", "b"]));
    const wrapper = mount(SelectionMenu, { props: { openUp: true }, attachTo: document.body });
    mounted.push(wrapper);
    await wrapper.get('[data-im="selection-menu"]').trigger("click");
    await wrapper.vm.$nextTick();
    const leave = wrapper.find('[data-im="group-leave"]');
    expect(leave.exists(), "全部所选都在组里时才显示「移出组」").toBe(true);
    await leave.trigger("click");
    await wrapper.vm.$nextTick();

    expect(labels).toEqual(["把所选卡片移出组（自动避开重叠）"]);
    expect(store.board!.groups).toHaveLength(0);
    expect(overlapsAnything(store.board!, "a")).toEqual([]);
    expect(overlapsAnything(store.board!, "b")).toEqual([]);
    expect(wrapper.text()).toContain("已把所选卡片移出组");
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });
});

describe("F07 组件：撤销一次即恢复，重做结果一致（真实 store.commit / undo / redo）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount();
    document.body.innerHTML = "";
  });

  beforeEach(async () => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = threeMemberState();
  });

  it("移出中间成员：撤销回移出前（成员 + 位置），重做回到同一结果", async () => {
    const before = {
      members: board.groupById(store.board!, "g1")!.members.slice(),
      positions: store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y })),
    };
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);

    await clickLeaveMember(wrapper, "b");
    await flushPromises();
    const after = {
      members: board.groupById(store.board!, "g1")!.members.slice(),
      positions: store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y })),
    };
    expect(after.members).toEqual(["a", "c"]);
    expect(overlapsAnything(store.board!, "b")).toEqual([]);
    expect(after.positions).not.toEqual(before.positions);

    store.undo();
    await wrapper.vm.$nextTick();
    expect(board.groupById(store.board!, "g1")!.members).toEqual(before.members);
    expect(store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y }))).toEqual(before.positions);

    store.redo();
    await wrapper.vm.$nextTick();
    expect(board.groupById(store.board!, "g1")!.members).toEqual(after.members);
    expect(store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y }))).toEqual(after.positions);
    expect(overlapsAnything(store.board!, "b")).toEqual([]);
    await flushPromises();
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });
});
