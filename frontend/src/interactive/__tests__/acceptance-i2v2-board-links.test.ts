/**
 * D 组独立验收 · F04：关系端点的纯函数级契约（board.ts）。
 *
 * 用户可见契约：
 * - 删除一条关系后再次连接同一对端点必须真的重建（可见的关系回来了）；
 * - 同一对端点永远只有一条有效关系；
 * - addLink 的显式更新语义保留（同一对端点再次明确写入时，更新那一条而不是产生第二份）。
 *
 * 基线问题：addLink 找到一条 deleted=true 的旧关系后只改 direction/meaning，不恢复 deleted，
 * 于是「删除后再连接」在界面上什么都不会出现。
 */
import { describe, expect, it } from "vitest";
import {
  addLink,
  createGroup,
  emptyState,
  removeLink,
  updateLink,
} from "../board";
import type { BoardState } from "../types";

const STAMP = "2026-10-25T00:00:00.000Z";

function base(): BoardState {
  const state = emptyState("board_t");
  state.cards = ["a", "b", "c"].map((id, index) => ({
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x: 100 + index * 200,
    y: 100,
    w: 120,
    h: 80,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  }));
  return state;
}

function active(state: BoardState) {
  return state.links.filter((link) => !link.deleted);
}

describe("F04 关系纯函数：删除后可重建、同一对端点只有一条", () => {
  it("删除关系后再连接同一对端点：关系真的恢复（不是改一条已删除的记录）", () => {
    let state = base();
    state = addLink(state, "a", "b", true, "属于");
    const linkId = active(state)[0].id;
    state = removeLink(state, linkId);
    expect(active(state), "删除后没有有效关系").toHaveLength(0);

    state = addLink(state, "b", "a", false, "换成另一条说明");
    const links = active(state);
    expect(links, "再次连接必须恢复一条有效关系").toHaveLength(1);
    expect(links[0].deleted).toBe(false);
    expect(links[0].meaning).toBe("换成另一条说明");
  });

  it("同一对端点重复建立只保留一条有效关系（显式更新语义保留）", () => {
    let state = base();
    state = addLink(state, "a", "b", false, "第一次说明");
    state = addLink(state, "a", "b", true, "第二次说明");
    const links = active(state);
    expect(links).toHaveLength(1);
    expect(links[0].meaning).toBe("第二次说明");
    expect(links[0].direction).toBe(true);
  });

  it("组端点同样适用：删除 group 关系后可重建，真实端点身份不变", () => {
    let state = base();
    state = createGroup(state, ["b", "c"], "发布计划");
    const groupId = state.groups[0].id;
    const endpoint = "group:" + groupId;
    state = addLink(state, "a", endpoint, true, "属于组");
    expect(active(state)).toHaveLength(1);
    state = removeLink(state, active(state)[0].id);
    expect(active(state)).toHaveLength(0);

    state = addLink(state, "a", endpoint, false, "重新连接");
    const links = active(state);
    expect(links, "组端点删除后也必须能重建").toHaveLength(1);
    expect([links[0].src, links[0].dst].sort()).toEqual(["a", endpoint].sort());
  });

  it("updateLink 的曲线变更只影响显示并保留端点身份（撤销重做依赖同一条记录）", () => {
    let state = base();
    state = addLink(state, "a", "b", true, "属于");
    const id = active(state)[0].id;
    state = updateLink(state, id, { curve: { x: 40, y: -20 } });
    const link = state.links.find((item) => item.id === id)!;
    expect(link.curve).toEqual({ x: 40, y: -20 });
    expect(link.meaning).toBe("属于");
    expect([link.src, link.dst].sort()).toEqual(["a", "b"]);
  });
});
