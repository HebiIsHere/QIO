/**
 * F04（B 组）· 重复拖线不得覆盖已有关系。
 *
 * 基线缺陷（9b23634）：BoardCanvas.finishLinkDraft 在端点对已存在时仍调用
 * addLink(from, target, false, "")，board.addLink 对已有 pair 直接改 direction/meaning，
 * 随后 commit + 打开编辑器 —— 用户只是「又拖了一次线」，原关系的含义就被空内容覆盖并自动保存。
 *
 * 本文件断言的是用户可见行为（状态结果 / 编辑器内容 / 提交次数），不是源码字符串：
 * 1. 卡片↔卡片（两个方向）、卡片↔组、组↔组：端点对已存在 → 选中 / 打开原关系，不新建、不覆盖；
 * 2. 打开编辑器 ≠ 修改；关闭 / 取消后原关系逐字段不变；
 * 3. 只有用户明确点「保存」才更新含义 / 方向，且保留 id、端点、曲线；
 * 4. addLink 的显式创建/更新能力保留（纯函数层）；删除后可按契约重建。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "../types";

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

function mkLink(id: string, src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: patch.direction ?? false,
    meaning: patch.meaning ?? "用户写的关系",
    curve: patch.curve,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkState(
  cards: BoardCard[],
  groups: BoardGroup[] = [],
  links: BoardLink[] = [],
  selection: string[] = [],
): BoardState {
  return board.normalizeState({
    ...board.emptyState("board_t"),
    cards,
    groups,
    links,
    selection,
  });
}

// ---------------------------------------------------------------------------
// 纯函数层：linkBetween / addLink 的「创建」与「编辑」语义
// ---------------------------------------------------------------------------

describe("F04 纯函数：端点对查询与显式更新语义", () => {
  it("linkBetween 忽略方向与 card:<id> 写法，找到原关系；自连 / 不存在 / 已删除返回 null", () => {
    const state = mkState(
      [mkCard("a", 0, 0), mkCard("b", 400, 0), mkCard("c", 800, 0)],
      [],
      [mkLink("l1", "a", "b", { meaning: "原来的含义", direction: true, curve: { x: 18, y: -9 } })],
    );
    expect(board.linkBetween(state, "a", "b")?.id).toBe("l1");
    // 从另一端看也是同一条
    expect(board.linkBetween(state, "b", "a")?.id).toBe("l1");
    // 兼容写法 card:<id>
    expect(board.linkBetween(state, "card:a", "b")?.id).toBe("l1");
    // 自连 / 不存在 / 没关系的对端
    expect(board.linkBetween(state, "a", "a")).toBeNull();
    expect(board.linkBetween(state, "a", "ghost")).toBeNull();
    expect(board.linkBetween(state, "a", "c")).toBeNull();
    // 删除后的关系不复存在（normalizeState 会丢弃删除标记的关系）→ 可以重建
    const removed = board.removeLink(state, "l1");
    expect(board.linkBetween(removed, "a", "b")).toBeNull();
  });

  it("addLink 的显式更新能力保留：再次显式建立会更新方向 / 含义，id 与曲线不变", () => {
    const once = board.addLink(mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)]), "a", "b", false, "原来的含义");
    const linkId = once.links[0].id;
    const bent = board.updateLink(once, linkId, { curve: { x: 30, y: -12 } });
    const twice = board.addLink(bent, "b", "a", true, "用户明确改写的含义");
    expect(twice.links).toHaveLength(1);
    expect(twice.links[0].id).toBe(linkId);
    expect(twice.links[0].src).toBe("a");
    expect(twice.links[0].dst).toBe("b");
    expect(twice.links[0].direction).toBe(true);
    expect(twice.links[0].meaning).toBe("用户明确改写的含义");
    expect(twice.links[0].curve).toEqual({ x: 30, y: -12 });
  });

  it("删除后的关系可按既有契约重建：得到新 id，不与已删除记录纠缠", () => {
    const linked = board.addLink(mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)]), "a", "b", false, "关联");
    const removed = board.removeLink(linked, linked.links[0].id);
    expect(removed.links).toHaveLength(0);
    const rebuilt = board.addLink(removed, "a", "b", false, "重新建立的关系");
    expect(rebuilt.links).toHaveLength(1);
    expect(rebuilt.links[0].id).not.toBe(linked.links[0].id);
    expect(rebuilt.links[0].meaning).toBe("重新建立的关系");
  });
});

// ---------------------------------------------------------------------------
// 组件层：真实拖线手势 + 真实编辑器交互
// ---------------------------------------------------------------------------

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

/** 板面坐标 → 屏幕坐标（jsdom 无滚动、缩放 1，与组件换算一致）。 */
function screenOf(boardX: number, boardY: number) {
  return { x: VIEW_LEFT + boardX, y: VIEW_TOP + boardY };
}

/** 打开链接编辑器（原关系或新关系都走同一块 UI）。 */
function editor(wrapper: VueWrapper) {
  return wrapper.find(".link-editor");
}

describe("F04 组件：重复拖线只打开原关系，不覆盖", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) {
      const wrapper = mounted.pop();
      try {
        wrapper?.unmount();
      } catch {
        /* 已经卸载过 */
      }
    }
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

  function cardEl(wrapper: VueWrapper, id: string): Element {
    return wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === id)!.element;
  }

  /** 从选中卡片的连接点拖线到指定板面坐标并松手。 */
  async function dragFromSelectedCard(wrapper: VueWrapper, target: { x: number; y: number }) {
    const point = wrapper.findAll('[data-im="connect-point"]')[0];
    expect(point, "选中卡片后必须有连接点").toBeTruthy();
    const from = screenOf(50, 9);
    point.element.dispatchEvent(pointer("pointerdown", from.x, from.y));
    await wrapper.vm.$nextTick();
    const to = screenOf(target.x, target.y);
    window.dispatchEvent(pointer("pointermove", to.x, to.y));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointerup", to.x, to.y));
    await wrapper.vm.$nextTick();
  }

  it("卡片↔卡片（正反两个方向）：只打开原关系，含义 / 方向 / 曲线 / id 全部保留", async () => {
    for (const [selected, from, to] of [
      ["a", "a", "b"] as const,
      ["b", "b", "a"] as const,
    ]) {
      labels.length = 0;
      setBoard(
        mkState(
          [mkCard("a", 0, 0), mkCard("b", 400, 0)],
          [],
          [mkLink("l1", "a", "b", { direction: true, meaning: "原来的含义", curve: { x: 18, y: -9 } })],
          [selected],
        ),
      );
      const wrapper = mountCanvas();
      const targetBoard = from === "a" ? { x: 450, y: 30 } : { x: 50, y: 30 };
      await dragFromSelectedCard(wrapper, targetBoard);

      // 没有新建、没有提交（打开编辑器不等于修改），原关系逐字段不变
      expect(labels.filter((label) => label.includes("新建关系")), "重复拖线不该新建关系").toHaveLength(0);
      expect(labels, "重复拖线不该产生任何提交").toHaveLength(0);
      const links = store.board!.links;
      expect(links).toHaveLength(1);
      expect(links[0]).toMatchObject({
        id: "l1",
        src: "a",
        dst: "b",
        direction: true,
        meaning: "原来的含义",
        curve: { x: 18, y: -9 },
      });
      // 打开的是**原关系**的编辑器：含义 / 方向就是原值
      const dialog = editor(wrapper);
      expect(dialog.exists(), "应打开原关系编辑器").toBe(true);
      expect((wrapper.get('[data-im="link-meaning"]').element as HTMLInputElement).value).toBe("原来的含义");
      expect((wrapper.get('[data-im="link-direction"]').element as HTMLInputElement).checked).toBe(true);

      // 关闭编辑器：原关系仍然逐字段不变，且没有新提交
      await wrapper.findAll(".link-editor button").find((item) => item.text().includes("关闭"))!.trigger("click");
      await wrapper.vm.$nextTick();
      expect(labels).toHaveLength(0);
      expect(store.board!.links[0]).toMatchObject({ id: "l1", meaning: "原来的含义", direction: true });
      wrapper.unmount();
      mounted.splice(mounted.indexOf(wrapper), 1);
    }
  });

  it("打开原关系后明确保存：只更新含义 / 方向，保留 id、端点与曲线", async () => {
    setBoard(
      mkState(
        [mkCard("a", 0, 0), mkCard("b", 400, 0)],
        [],
        [mkLink("l1", "a", "b", { direction: true, meaning: "原来的含义", curve: { x: 12, y: -6 } })],
        ["a"],
      ),
    );
    const wrapper = mountCanvas();
    await dragFromSelectedCard(wrapper, { x: 450, y: 30 });
    expect(editor(wrapper).exists()).toBe(true);

    await wrapper.get('[data-im="link-meaning"]').setValue("用户明确改写的含义");
    await wrapper.get('[data-im="link-direction"]').setValue(false);
    await wrapper.findAll(".link-editor button").find((item) => item.text().includes("保存"))!.trigger("click");
    await wrapper.vm.$nextTick();

    expect(labels.filter((label) => label.includes("修改关系含义"))).toHaveLength(1);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(0);
    const links = store.board!.links;
    expect(links).toHaveLength(1);
    expect(links[0]).toMatchObject({ id: "l1", src: "a", dst: "b", meaning: "用户明确改写的含义", direction: false });
    expect(links[0].curve).toEqual({ x: 12, y: -6 });
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("卡片↔组、组↔组：端点对已存在时同样只打开原关系", async () => {
    // 卡片 ↔ 组：预置 a ↔ group:g1 的关系，从 a 拖到组框标题区
    setBoard(
      mkState(
        [mkCard("a", 0, 0), mkCard("c", 400, 0)],
        [mkGroup("g1", ["c"], { name: "目标组" })],
        [mkLink("l_cg", "a", "group:g1", { meaning: "卡片到组", curve: { x: 5, y: 4 } })],
        ["a"],
      ),
    );
    let wrapper = mountCanvas();
    // 组框由成员推导：c(400,0) → g1 框 (384,-58) 起，标题区（450,-30）没有卡片
    await dragFromSelectedCard(wrapper, { x: 450, y: -30 });
    expect(labels, "卡片-组重复拖线不该提交").toHaveLength(0);
    expect(store.board!.links).toHaveLength(1);
    expect(store.board!.links[0]).toMatchObject({ id: "l_cg", src: "a", dst: "group:g1", meaning: "卡片到组" });
    expect(editor(wrapper).exists()).toBe(true);
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);

    // 组 ↔ 组：预置 group:g1 ↔ group:g2 的关系，从 g1 锚点拖到 g2 标题区
    labels.length = 0;
    setBoard(
      mkState(
        [mkCard("a", 0, 0), mkCard("b", 400, 0)],
        [mkGroup("g1", ["a"], { name: "第一组" }), mkGroup("g2", ["b"], { name: "第二组" })],
        [mkLink("l_gg", "group:g1", "group:g2", { meaning: "组到组", direction: true, curve: { x: -7, y: 3 } })],
      ),
    );
    wrapper = mountCanvas();
    const anchor = wrapper.findAll('[data-im="group-anchor"]').find((item) => item.attributes("data-endpoint") === "group:g1")!;
    expect(anchor, "已有组端点关系时该组应有锚点").toBeTruthy();
    const style = (anchor.element as HTMLElement).style;
    const anchorBoard = { x: Number(style.left.replace("px", "")), y: Number(style.top.replace("px", "")) };
    const from = screenOf(anchorBoard.x, anchorBoard.y);
    anchor.element.dispatchEvent(pointer("pointerdown", from.x, from.y));
    await wrapper.vm.$nextTick();
    const to = screenOf(450, -30);
    window.dispatchEvent(pointer("pointermove", to.x, to.y));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointerup", to.x, to.y));
    await wrapper.vm.$nextTick();

    expect(labels, "组-组重复拖线不该提交").toHaveLength(0);
    expect(store.board!.links).toHaveLength(1);
    expect(store.board!.links[0]).toMatchObject({
      id: "l_gg",
      src: "group:g1",
      dst: "group:g2",
      meaning: "组到组",
      direction: true,
      curve: { x: -7, y: 3 },
    });
    expect(editor(wrapper).exists()).toBe(true);
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("不含关系的端点对仍然按既有行为新建关系并打开编辑器", async () => {
    setBoard(mkState([mkCard("a", 0, 0), mkCard("b", 400, 0)], [], [], ["a"]));
    const wrapper = mountCanvas();
    await dragFromSelectedCard(wrapper, { x: 450, y: 30 });
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(1);
    expect(store.board!.links).toHaveLength(1);
    expect(store.board!.links[0]).toMatchObject({ src: "a", dst: "b", meaning: "" });
    expect(editor(wrapper).exists()).toBe(true);
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });
});
