/**
 * C 轮定向测试：F4 结果卡许可 / F6 冻结视图载荷（前端数据层）。
 *
 * 只覆盖数据层与纯函数；提交闭环（真的要 view 传上去）靠后端集成用例与 API 闭环验证。
 */
import { describe, expect, it } from "vitest";
import {
  arrangeText,
  executionRefsText,
  operationsText,
  previewSymbols,
  previewSymbolsText,
} from "../approval";
import { addCard, CHECKABLE_KINDS, normalizeState, setChecked } from "../board";
import {
  changeKindLabel,
  frozenViewPayload,
  isVisibleCard,
  localVisibleRange,
  publishBoardScale,
  readBoardScale,
  summarizeChanges,
} from "../submission";
import type { BoardCard, BoardState, Execution, Expression, Intent, IntentPreview } from "../types";

function card(id: string, kind: BoardCard["kind"], fields: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind,
    content: `内容-${id}`,
    meta: {},
    x: 0,
    y: 0,
    w: 240,
    h: 120,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-15T00:00:00.000Z",
    updatedAt: "2026-10-15T00:00:00.000Z",
    ...fields,
  };
}

function state(parts: Partial<BoardState> = {}): BoardState {
  return {
    boardId: "board_test",
    seq: 1,
    updatedAt: "2026-10-15T00:00:00.000Z",
    cards: [],
    groups: [],
    links: [],
    selection: [],
    ...parts,
  };
}

function expr(kind: Expression["kind"]): Expression {
  return {
    id: `x_${kind}`,
    kind,
    intentBearing: false,
    summary: "",
    cardIds: [],
    groupId: null,
    linkId: null,
  };
}

describe("F4：结果卡与文字卡同一套许可语义", () => {
  it("reply 在可勾选名单里，未勾选 / 隐藏 / 删除都不可见", () => {
    expect(CHECKABLE_KINDS).toContain("reply");
    expect(isVisibleCard(card("r", "reply", { checked: true }))).toBe(true);
    expect(isVisibleCard(card("r", "reply", { checked: false }))).toBe(false);
    expect(isVisibleCard(card("r", "reply", { checked: true, hidden: true }))).toBe(false);
    expect(isVisibleCard(card("r", "reply", { checked: true, deleted: true }))).toBe(false);
  });

  it("localVisibleRange 把已许可的结果卡算进范围，未许可的不算", () => {
    const range = localVisibleRange(
      state({
        cards: [
          card("r", "reply", { checked: true }),
          card("r2", "reply", { checked: false }),
          card("n", "text", { checked: true }),
        ],
      }),
    );
    expect(range.cards.map((item) => item.id)).toEqual(["r", "n"]);
  });

  it("normalizeState 保留结果卡上用户给出的许可；setChecked 能勾选结果卡", () => {
    const normalized = normalizeState(state({ cards: [card("r", "reply", { checked: true })] }));
    expect(normalized.cards[0].checked).toBe(true);
    const licensed = setChecked(
      normalizeState(state({ cards: [card("r", "reply")] })),
      "r",
      true,
    );
    expect(licensed.cards[0].checked).toBe(true);
    const revoked = setChecked(licensed, "r", false);
    expect(revoked.cards[0].checked).toBe(false);
  });

  it("隐藏仍然强制取消许可（G6 不变）", () => {
    const hidden = normalizeState(
      state({ cards: [card("r", "reply", { checked: true, hidden: true })] }),
    );
    expect(hidden.cards[0].checked).toBe(false);
  });

  it("新结果卡默认未许可（addCard / 执行落地路径一致）", () => {
    const next = addCard(state(), { kind: "reply", content: "结果" });
    expect(next.cards[0].checked).toBe(false);
  });

  it("result_added / result_updated 不是用户提出的新工作", () => {
    expect(changeKindLabel("result_added")).toBe("新增可见结果卡");
    expect(changeKindLabel("result_updated")).toBe("可见结果卡内容变化");
    const summary = summarizeChanges([expr("result_added"), expr("result_updated")]);
    expect(summary.nonIntent).toHaveLength(2);
    expect(summary.bearing).toHaveLength(0);
    expect(summary.hasContent).toBe(true);
  });
});

describe("F6：冻结视图载荷（量不到就 null，不编造）", () => {
  it("合法区域 + 正缩放 → 原样组装", () => {
    expect(frozenViewPayload({ x: 10, y: 20, w: 800, h: 600 }, 1.5)).toEqual({
      x: 10,
      y: 20,
      w: 800,
      h: 600,
      scale: 1.5,
    });
  });

  it("区域缺失 / 面积为零 / 缩放非法 → null", () => {
    expect(frozenViewPayload(null, 1)).toBeNull();
    expect(frozenViewPayload({ x: 0, y: 0, w: 0, h: 100 }, 1)).toBeNull();
    expect(frozenViewPayload({ x: 0, y: 0, w: 100, h: 100 }, null)).toBeNull();
    expect(frozenViewPayload({ x: 0, y: 0, w: 100, h: 100 }, 0)).toBeNull();
    expect(frozenViewPayload({ x: Number.NaN, y: 0, w: 100, h: 100 }, 1)).toBeNull();
    expect(frozenViewPayload({ x: 0, y: 0, w: Infinity, h: 100 }, 1)).toBeNull();
  });

  it("publishBoardScale 只接受正的有限数，其余按「量不到」处理", () => {
    publishBoardScale(1.25);
    expect(readBoardScale()).toBe(1.25);
    publishBoardScale(Number.NaN);
    expect(readBoardScale()).toBeNull();
    publishBoardScale(0);
    expect(readBoardScale()).toBeNull();
    publishBoardScale(-2);
    expect(readBoardScale()).toBeNull();
    publishBoardScale(null);
    expect(readBoardScale()).toBeNull();
  });
});

describe("F5 / F8：审批预览的符号映射与排序文案", () => {
  it("previewSymbols 只列裸符号并排序，别名不重复", () => {
    const preview = {
      cards: [],
      groups: [],
      links: [],
      refs: { b: "b", "card:b": "b", a: "a", "card:a": "a" },
    } as unknown as IntentPreview;
    expect(previewSymbols(preview)).toEqual([
      { symbol: "a", objectId: "a" },
      { symbol: "b", objectId: "b" },
    ]);
    expect(previewSymbolsText(preview)).toContain("a → a");
  });

  it("executionRefsText 显示符号 → 正式对象 id；没有映射时为空串", () => {
    const execution = { objectRefs: { a: "c_1", b: "c_2", "card:a": "c_1" } } as unknown as Execution;
    const text = executionRefsText(execution);
    expect(text).toContain("a → c_1");
    expect(text).toContain("b → c_2");
    expect(text).not.toContain("card:a");
    expect(executionRefsText(null)).toBe("");
  });

  it("arrange 文案：没有 arrange 必须明说只改逻辑顺序", () => {
    expect(arrangeText({ op: "order_group" })).toBe("只改逻辑顺序（不移动卡片）");
    expect(arrangeText({ op: "order_group", arrange: "row" })).toBe("真实重排：从左到右");
    expect(arrangeText({ op: "order_group", arrange: "column" })).toBe("真实重排：从上到下一列");
    expect(arrangeText({ op: "add_link" })).toBe("");
    const intent = {
      operations: [
        { op: "order_group", groupId: "g1", memberOrder: ["a"] },
        { op: "order_group", groupId: "g1", memberOrder: ["a"], arrange: "row" },
      ],
    } as unknown as Intent;
    const lines = operationsText(intent);
    expect(lines[0]).toContain("只改逻辑顺序");
    expect(lines[1]).toContain("真实重排：从左到右");
  });
});
