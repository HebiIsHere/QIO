/**
 * #60/#62/#72（PM-QIO032）· 组操作：
 * - #62 加入组用 QIO 风格选择器；显示成员数与成员摘要；同名组可区分；按真实组 ID 加入；
 * - #60 解除有组级连线的组前，列出受影响关系并确认；执行后保留成员自己的线；
 *        一次撤销同时恢复组及组级连线；
 * - #72 静态组名超长省略号、悬停/聚焦读全名、编辑态完整输入。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SelectionMenu from "../SelectionMenu.vue";
import BoardGroupFrame from "../BoardGroupFrame.vue";
import frameSource from "../BoardGroupFrame.vue?raw";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-25T00:00:00.000Z";

function mkCard(id: string, content: string, x = 0, y = 0): BoardCard {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x,
    y,
    w: 100,
    h: 60,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
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
    w: patch.w ?? 400,
    h: patch.h ?? 300,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function mkLink(id: string, src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: patch.direction ?? false,
    meaning: patch.meaning ?? "关联",
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
  return board.normalizeState({ ...board.emptyState("board_t"), cards, groups, links, selection });
}

let store: ReturnType<typeof useInteractiveStore>;
let wrapper: VueWrapper | null = null;
let labels: string[] = [];

async function openMenu(): Promise<VueWrapper> {
  wrapper = mount(SelectionMenu, { props: { openUp: true }, attachTo: document.body });
  await wrapper.get('[data-im="selection-menu"]').trigger("click");
  await wrapper.vm.$nextTick();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  labels = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("#62 加入组：QIO 风格选择器", () => {
  it("不再是原生 select；每个选项显示组名、成员数与成员摘要", async () => {
    store.board = mkState(
      [mkCard("a", "第一张"), mkCard("b", "第二张"), mkCard("c", "第三张"), mkCard("d", "第四张")],
      [mkGroup("g1", ["b", "c"], { name: "资料组" }), mkGroup("g2", ["d"], { name: "想法组" })],
      [],
      ["a"],
    );
    const menu = await openMenu();
    expect(menu.find("select").exists(), "加入组不该再用原生 select").toBe(false);
    await menu.get('[data-im="group-join"]').trigger("click");
    await menu.vm.$nextTick();

    const options = menu.findAll('[data-im="group-join-option"]');
    expect(options.length, "可选组没有列出来").toBe(2);
    const text = options.map((item) => item.text()).join(" | ");
    expect(text).toContain("资料组");
    expect(text).toContain("想法组");
    expect(text).toContain("成员");
    expect(text).toContain("第二张");
  });

  it("同名组显示成员数与成员摘要（标「同名组」），点击后按真实组 ID 加入而不是按名字", async () => {
    store.board = mkState(
      [mkCard("a", "甲"), mkCard("b", "乙"), mkCard("c", "丙"), mkCard("d", "丁")],
      [
        mkGroup("g_first", ["a", "b"], { name: "同名小组" }),
        mkGroup("g_second", ["c"], { name: "同名小组" }),
      ],
      [],
      ["d"],
    );
    const menu = await openMenu();
    await menu.get('[data-im="group-join"]').trigger("click");
    await menu.vm.$nextTick();

    const options = menu.findAll('[data-im="group-join-option"]');
    expect(options).toHaveLength(2);
    expect(options[0].attributes("data-group-name")).toBe("同名小组");
    expect(options[1].attributes("data-group-name")).toBe("同名小组");
    // 同名组必须有区分线索：成员数 / 摘要 / 「同名组」标记
    expect(options[0].text()).toContain("同名组");
    expect(options[0].text()).toContain("2 名成员");
    expect(options[1].text()).toContain("1 名成员");

    // 加入第二个同名组（真实 id = g_second）
    await options[1].trigger("click");
    await menu.vm.$nextTick();
    expect(labels).toEqual(["把所选卡片加入组"]);
    const second = store.board!.groups.find((group) => group.id === "g_second")!;
    expect(second.members, "按名字而不是真实组 ID 加入了").toContain("d");
    const first = store.board!.groups.find((group) => group.id === "g_first")!;
    expect(first.members).toEqual(["a", "b"]);
  });
});

describe("#60 解除有组级连线的组：先确认，成员自己的线保留，撤销一并恢复", () => {
  it("有组级连线时先弹出受影响关系清单，确认前不提交", async () => {
    store.board = mkState(
      [mkCard("a", "甲"), mkCard("b", "乙")],
      [mkGroup("g1", ["a", "b"], { name: "项目组" })],
      [mkLink("l_group", "a", "group:g1", { meaning: "属于" }), mkLink("l_member", "a", "b", { meaning: "相关" })],
      ["a"],
    );
    const menu = await openMenu();
    const dissolve = menu.get('[data-im="group-dissolve"]');
    await dissolve.trigger("click");
    await menu.vm.$nextTick();

    expect(labels, "确认前不该提交").toHaveLength(0);
    // 设置组 #82 把 QConfirm 的 layer 档统一成窗口层（Teleport 到 body，避免窄窗被顶部导航遮挡），
    // 所以确认框仍会真实显示，但不再挂在菜单子树里 —— 断言改成查 body 上的窗口层。
    const confirmBox = document.body.querySelector(".qio-confirm");
    expect(confirmBox, "没有显示受影响关系确认").not.toBeNull();
    expect(confirmBox?.textContent ?? "").toContain("组级关系");
    expect(confirmBox?.textContent ?? "").toContain("属于");

    // 取消：不解除组
    const cancel = confirmBox?.querySelector('[data-qio-confirm-action="cancel"]') as HTMLElement | null;
    expect(cancel, "没有取消按钮").not.toBeNull();
    cancel?.click();
    await flushPromises();
    await menu.vm.$nextTick();
    expect(labels).toHaveLength(0);
    expect(store.board!.groups.some((group) => group.id === "g1" && !group.deleted)).toBe(true);
  });

  it("确认后：组级关系移除、成员之间的关系保留；撤销一次同时恢复组与组级关系", async () => {
    vi.restoreAllMocks(); // 使用真实 store.commit：验证撤销能恢复组 + 组级连线
    store = useInteractiveStore();
    store.board = mkState(
      [mkCard("a", "甲"), mkCard("b", "乙")],
      [mkGroup("g1", ["a", "b"], { name: "项目组" })],
      [mkLink("l_group", "a", "group:g1", { meaning: "属于" }), mkLink("l_member", "a", "b", { meaning: "相关" })],
      ["a"],
    );
    const menu = await openMenu();
    await menu.get('[data-im="group-dissolve"]').trigger("click");
    await menu.vm.$nextTick();
    const confirm = document.body.querySelector('[data-qio-confirm-action="confirm"]') as HTMLElement | null;
    expect(confirm, "没有确认按钮").not.toBeNull();
    confirm?.click();
    await flushPromises();
    await menu.vm.$nextTick();

    expect(store.board!.groups.some((group) => group.id === "g1" && !group.deleted), "组没有被解除").toBe(false);
    const remaining = store.board!.links.map((link) => link.id);
    expect(remaining, "组级关系没有随组移除").not.toContain("l_group");
    expect(remaining, "成员之间的关系被误删").toContain("l_member");

    store.undo();
    await menu.vm.$nextTick();
    expect(store.board!.groups.some((group) => group.id === "g1" && !group.deleted), "撤销没有恢复组").toBe(true);
    expect(store.board!.links.map((link) => link.id), "撤销没有恢复组级关系").toContain("l_group");
  });
});

describe("#72 组名静态省略号与全名可读", () => {
  function mountFrame(name: string, patch: Record<string, unknown> = {}): VueWrapper {
    wrapper = mount(BoardGroupFrame, {
      props: {
        group: mkGroup("g1", ["a", "b"], { name }),
        cards: [mkCard("a", "甲"), mkCard("b", "乙")],
        selectedIds: [],
        dropTarget: false,
        dropMerge: false,
        dropIndex: null,
        dropCardId: null,
        ...patch,
      },
    });
    return wrapper;
  }

  it("静态组名用省略号样式，title 与 aria-label 能读全名", () => {
    const long = "这是一个非常非常长的组名用来测试省略号与全名读取";
    const frame = mountFrame(long);
    const staticName = frame.get('[data-im="group-name-static"]');
    expect(staticName.text()).toBe(long);
    expect(staticName.attributes("title")).toBe(long);
    const input = frame.get('[data-im="group-name"]');
    expect(input.attributes("title")).toBe(long);
    expect(input.attributes("aria-label")).toContain(long);
    // 省略号由样式保证（jsdom 不做布局，按源码断言同一条规则）
    const styleBlock = frameSource.slice(frameSource.indexOf(".group-name-static {"));
    expect(styleBlock.slice(0, styleBlock.indexOf("}"))).toContain("text-overflow: ellipsis");
  });

  it("聚焦输入框后切到编辑态（静态文本隐藏），可完整输入并触发改名", async () => {
    const long = "很长很长的组名超过容器宽度";
    const frame = mountFrame(long);
    const input = frame.get('[data-im="group-name"]');
    await input.trigger("focus");
    expect(frame.find('[data-im="group-name-static"]').exists(), "编辑态还盖着静态文本").toBe(false);
    (input.element as HTMLInputElement).value = "完整的新组名";
    await input.trigger("change");
    expect(frame.emitted("rename")).toEqual([["g1", "完整的新组名"]]);
  });
});