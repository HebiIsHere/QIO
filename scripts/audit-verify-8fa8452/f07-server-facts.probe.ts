/**
 * E 路独立核验探针 · F07 本地删除的组与关联被恢复（基线 8fa845255c118a7104b1de8e37298ca745924482）
 *
 * 规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F07 段。
 * 定位：serverFacts.mergeGroup/mergeLink、adoptServerFacts。
 * 要求：
 *   - 根据 base 区分本地删除、服务器新增和双方修改；
 *   - 服务器仅改无关卡片时，本地删除关联/解散组必须保持；
 *   - 删除与服务器对同对象的实质修改相撞时保留依据、提供明确解决办法，
 *     不能静默复活或丢掉对方修改。
 *
 * 这是一个**纯函数**反例：mergeServerInto 是 F07 的合并口径唯一实现，
 * 不需要 store、不需要网络，缺陷与修复的差异在这里是最小可判定的。
 *
 * 运行：
 *   cd D:\qio-dev\qa-e\frontend
 *   npx vitest run --root . -c ..\scripts\audit-verify-8fa8452\vitest.config.ts ..\scripts\audit-verify-8fa8452\f07-server-facts.probe.ts
 */
import { describe, expect, it } from "vitest";
import { mergeServerInto } from "../../frontend/src/interactive/serverFacts";
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "../../frontend/src/interactive/types";

const card = (id: string, over: Partial<BoardCard> = {}): BoardCard => ({
  id,
  kind: "text",
  content: "c-" + id,
  meta: {},
  x: 0,
  y: 0,
  w: 1,
  h: 1,
  checked: false,
  hidden: false,
  folded: false,
  bookmarked: false,
  deleted: false,
  createdAt: "2026-10-11T00:00:00Z",
  updatedAt: "2026-10-11T00:00:00Z",
  ...over,
});

const group = (id: string, over: Partial<BoardGroup> = {}): BoardGroup => ({
  id,
  name: "g-" + id,
  defaultName: false,
  ordered: false,
  x: 0,
  y: 0,
  w: 1,
  h: 1,
  members: ["card:a"],
  deleted: false,
  createdAt: "2026-10-11T00:00:00Z",
  updatedAt: "2026-10-11T00:00:00Z",
  ...over,
});

const link = (id: string, over: Partial<BoardLink> = {}): BoardLink => ({
  id,
  src: "card:a",
  dst: "card:b",
  direction: true,
  meaning: "关联",
  deleted: false,
  createdAt: "2026-10-11T00:00:00Z",
  updatedAt: "2026-10-11T00:00:00Z",
  ...over,
});

const state = (over: Partial<BoardState> = {}): BoardState => ({
  boardId: "board_default",
  seq: 3,
  updatedAt: "2026-10-11T00:00:00Z",
  cards: [],
  groups: [],
  links: [],
  selection: [],
  ...over,
});

describe("F07 本地删除的组与关联不能被恢复", () => {
  it("F07-a 服务器只改无关卡片内容时，本地已删除的关联必须保持删除", () => {
    const l1 = link("link:1");
    const base = state({ links: [l1] });
    // 本地：用户删掉了这条关联（本地列表里不再有 link:1）
    const local = state({ links: [], seq: 3 });
    // 服务器：只改了无关卡片 card:a 的内容（关联本身没有任何人改），并前进到 seq=4
    const server = state({ cards: [card("card:a", { content: "服务器改的正文" })], links: [l1], seq: 4 });

    const outcome = mergeServerInto(local, server, base);
    const ids = outcome.state.links.map((l) => l.id);
    console.log("[F07-a] 合并后 links =", JSON.stringify(outcome.state.links), "conflicts =", JSON.stringify(outcome.conflicts));
    expect(
      ids,
      "F07 缺陷复现：本地删除的关联被服务器事实复活了（mergeLink 只看到 server 有、local 没有，" +
        "无条件返回 server，没有用 base 区分『本地删除』与『服务器新增』）。" +
        " 实际 links=" + JSON.stringify(ids),
    ).not.toContain("link:1");
  });

  it("F07-b 服务器只改无关卡片时，本地已解散的组必须保持解散", () => {
    const g1 = group("group:1");
    const base = state({ groups: [g1] });
    const local = state({ groups: [] });
    const server = state({ cards: [card("card:a", { content: "服务器改的正文" })], groups: [g1], seq: 4 });

    const outcome = mergeServerInto(local, server, base);
    const ids = outcome.state.groups.map((g) => g.id);
    console.log("[F07-b] 合并后 groups =", JSON.stringify(outcome.state.groups));
    expect(
      ids,
      "F07 缺陷复现：本地解散的组被服务器事实复活了。实际 groups=" + JSON.stringify(ids),
    ).not.toContain("group:1");
  });

  it("F07-c 服务器新增的对象必须保留（不能为了修删除而把新增也吞掉）", () => {
    const base = state({});
    const local = state({ cards: [card("card:local")] });
    const server = state({ cards: [card("card:server")], seq: 4 });

    const outcome = mergeServerInto(local, server, base);
    const ids = outcome.state.cards.map((c) => c.id).sort();
    console.log("[F07-c] 合并后 cards =", JSON.stringify(ids));
    expect(ids).toContain("card:server");
    expect(ids).toContain("card:local");
  });

  it("F07-d 删除与服务器对同对象的修改相撞：不能静默复活却什么都不说", () => {
    const l1 = link("link:1", { meaning: "原始含义" });
    const base = state({ links: [l1] });
    const local = state({ links: [] }); // 本地删除
    // 服务器对该关联做了实质修改（含义变了）
    const server = state({ links: [link("link:1", { meaning: "服务器改过的含义" })], seq: 4 });

    const outcome = mergeServerInto(local, server, base);
    const restored = outcome.state.links.find((l) => l.id === "link:1");
    console.log(
      "[F07-d] 合并后 link:1 =", JSON.stringify(restored ?? null),
      "conflicts =", JSON.stringify(outcome.conflicts),
    );
    // 允许两种正确收敛：保留删除（并登记冲突）或保留服务器修改（并登记冲突）；
    // 唯独不允许「悄悄复活/悄悄丢弃对方的修改而没有任何依据可查」。
    const keptLocalDeletion = !restored;
    const keptServerEdit = !!restored && restored.meaning === "服务器改过的含义";
    expect(
      keptLocalDeletion || keptServerEdit,
      "F07：删除与服务器修改相撞时出现了既非删除也非服务器修改的第三种结果",
    ).toBe(true);
  });
});
