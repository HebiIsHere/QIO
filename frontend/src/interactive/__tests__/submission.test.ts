import { describe, expect, it } from "vitest";
import {
  changeBearingText,
  changeKindLabel,
  draftHintText,
  isVisibleCard,
  localVisibleRange,
  saveStateText,
  submitFailureText,
  submitStateLabel,
  submitStatusText,
  summarizeChanges,
  visibleRangeCounts,
  visibleRangeText,
} from "../submission";
import type {
  BoardCard,
  BoardGroup,
  BoardLink,
  BoardState,
  Expression,
  ExpressionKind,
  SubmissionResult,
} from "../types";

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
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...fields,
  };
}

function state(parts: Partial<BoardState> = {}): BoardState {
  return {
    boardId: "board_test",
    seq: 1,
    updatedAt: "2026-10-06T00:00:00.000Z",
    cards: [],
    groups: [],
    links: [],
    selection: [],
    ...parts,
  };
}

function group(id: string, members: string[], fields: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: "组 1",
    defaultName: true,
    ordered: false,
    x: 0,
    y: 0,
    w: 320,
    h: 240,
    members,
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...fields,
  };
}

function link(id: string, src: string, dst: string, fields: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "相关",
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
    ...fields,
  };
}

function expression(
  kind: ExpressionKind,
  intentBearing: boolean,
  summary = `摘要-${kind}`,
): Expression {
  return { id: `x_${kind}`, kind, intentBearing, summary, cardIds: [], groupId: null, linkId: null };
}

const ALL_KINDS: ExpressionKind[] = [
  "note_added",
  "note_edited",
  "note_deleted",
  "material_added",
  "material_removed",
  "link_added",
  "link_removed",
  "link_meaning_changed",
  "group_formed",
  "group_merged",
  "group_renamed",
  "group_membership_changed",
  "order_changed",
  "ordered_changed",
  "focus_selection",
  "layout_only",
];

describe("可见范围预览（与服务端 models.selectable_cards 同规则）", () => {
  it("材料默认在范围内，文字注释要勾选，隐藏 / 删除 / reply 不在其中", () => {
    const noteChecked = card("n1", "text", { checked: true });
    const noteUnchecked = card("n2", "text");
    const noteHidden = card("n3", "text", { checked: true, hidden: true });
    const file = card("f1", "file", { meta: { name: "材料.pdf" } });
    const reply = card("r1", "reply");
    const deleted = card("d1", "file", { deleted: true });

    expect(isVisibleCard(noteChecked)).toBe(true);
    expect(isVisibleCard(noteUnchecked)).toBe(false);
    expect(isVisibleCard(noteHidden)).toBe(false);
    expect(isVisibleCard(file)).toBe(true);
    expect(isVisibleCard(reply)).toBe(false);
    expect(isVisibleCard(deleted)).toBe(false);

    const range = localVisibleRange(
      state({ cards: [noteChecked, noteUnchecked, noteHidden, file, reply, deleted] }),
    );
    expect(range.cards.map((item) => item.id)).toEqual(["n1", "f1"]);
    // 未勾选注释 + 隐藏注释 + QIO 结果：已删除的卡片不算「没交出去的内容」
    expect(range.notVisibleCount).toBe(3);
    expect(range.empty).toBe(false);
  });

  it("组只在有可见成员时出现且只列可见成员；链接两端都可见才出现；选择只留可见卡片", () => {
    const visible = card("n1", "text", { checked: true });
    const secret = card("n2", "text");
    const range = localVisibleRange(
      state({
        cards: [visible, secret],
        groups: [group("g1", ["n1", "n2"]), group("g2", ["n2"])],
        links: [link("l1", "n1", "n2"), link("l2", "n1", "n1")],
        selection: ["n1", "n2"],
      }),
    );
    expect(range.groups.map((item) => item.id)).toEqual(["g1"]);
    expect(range.groups[0].members).toEqual(["n1"]);
    expect(range.links.map((item) => item.id)).toEqual(["l2"]);
    expect(range.selection).toEqual(["n1"]);
  });

  it("范围为空时 empty 为 true", () => {
    const range = localVisibleRange(state({ cards: [card("n1", "text")] }));
    expect(range.empty).toBe(true);
    expect(visibleRangeCounts(range).cards).toBe(0);
  });
});

describe("允许查看范围文案", () => {
  it("列出注释与材料数量，并明确写出未勾选的不在其中", () => {
    const range = localVisibleRange(
      state({
        cards: [
          card("n1", "text", { checked: true }),
          card("f1", "file"),
          card("u1", "url"),
          card("n2", "text"),
        ],
        groups: [group("g1", ["n1"])],
        links: [link("l1", "n1", "f1")],
      }),
    );
    const text = visibleRangeText(range);
    expect(text).toContain("注释 1 条");
    expect(text).toContain("材料 2 项");
    expect(text).toContain("组 1 个");
    expect(text).toContain("关系 1 条");
    expect(text).toContain("未勾选的注释");
    expect(text).toContain("不会被查看");
  });

  it("空范围与未拿到服务端结果时都给文字说明，不靠颜色", () => {
    expect(visibleRangeText(localVisibleRange(state()))).toContain("范围是空的");
    expect(visibleRangeText(null)).toContain("未勾选");
    expect(visibleRangeText(localVisibleRange(state({ cards: [card("f1", "file")] })))).toContain(
      "没有被排除的卡片",
    );
  });
});

describe("本次有效改动摘要", () => {
  it("区分作为表达的改动、只记录的变化与普通移动", () => {
    const summary = summarizeChanges([
      expression("note_added", true),
      expression("material_added", false),
      expression("layout_only", false),
    ]);
    expect(summary.bearingCount).toBe(1);
    expect(summary.nonIntent).toHaveLength(1);
    expect(summary.layoutCount).toBe(1);
    expect(summary.total).toBe(3);
    expect(summary.hasContent).toBe(true);
    expect(summary.headline).toContain("1 项有效表达");
    expect(summary.headline).toContain("只影响显示");
  });

  it("只有普通移动时不构成可提交内容", () => {
    const summary = summarizeChanges([expression("layout_only", false)]);
    expect(summary.hasContent).toBe(false);
    expect(summary.bearingCount).toBe(0);
    expect(summary.headline).toContain("都不构成工作请求");
  });

  it("没有改动时给出明确文字", () => {
    const summary = summarizeChanges([]);
    expect(summary.total).toBe(0);
    expect(summary.headline).toBe("本次还没有可提交的改动");
    expect(summarizeChanges(undefined).total).toBe(0);
  });

  it("每种表达式都有中文标签与「是否作为表达」的文字说明", () => {
    for (const kind of ALL_KINDS) {
      expect(changeKindLabel(kind)).not.toBe("");
      expect(changeKindLabel(kind)).not.toBe(kind);
      expect(changeBearingText(expression(kind, !["layout_only", "material_added", "material_removed", "note_deleted", "link_removed"].includes(kind)))).not.toBe("");
    }
    expect(changeBearingText(expression("layout_only", false))).toContain("只影响显示");
    expect(changeBearingText(expression("material_added", false))).toContain("不作为工作请求");
    expect(changeBearingText(expression("note_added", true))).toBe("作为表达");
  });
});

describe("提交状态文案", () => {
  const succeeded = {
    status: "succeeded",
    expressions: [expression("note_added", true)],
    checkedCleared: ["n1"],
  } as unknown as SubmissionResult;

  it("未提交 / 已提交状态标签", () => {
    expect(submitStateLabel("idle")).toBe("未提交");
    expect(submitStateLabel("idle", { hasSubmission: true, pendingCount: 0 })).toContain("没有新的改动");
    expect(submitStateLabel("idle", { hasSubmission: true, pendingCount: 2 })).toContain("尚未提交");
    expect(submitStateLabel("submitting")).toBe("正在提交");
    expect(submitStateLabel("succeeded")).toBe("已提交");
    expect(submitStateLabel("failed")).toContain("未提交");
    expect(submitStateLabel("empty")).toBe("无内容可提交");
    expect(submitStateLabel("duplicate")).toBe("未重复提交");
  });

  it("成功提交必须写明第一阶段没有接入模型调用，以及勾选已被取消", () => {
    const text = submitStatusText("succeeded", { result: succeeded });
    expect(text).toContain("已提交");
    expect(text).toContain("没有接入 QIO 模型调用");
    expect(text).toContain("取消 1 条注释的勾选");
  });

  it("失败要写明保留了什么、基准没有更新", () => {
    const text = submitStatusText("failed", { error: "网络中断" });
    expect(text).toContain("网络中断");
    expect(text).toContain("保留");
    expect(text).toContain("基准没有更新");
    expect(submitFailureText(null)).toContain("不会丢改动");
  });

  it("empty / duplicate 都说明未更新基准、未调用 QIO", () => {
    expect(submitStatusText("empty")).toContain("未调用 QIO");
    expect(submitStatusText("empty")).toContain("未更新基准");
    expect(submitStatusText("duplicate")).toContain("未重复提交");
    expect(submitStatusText("duplicate")).toContain("未调用 QIO");
  });

  it("未提交时说明保存与提交是两件事", () => {
    const text = submitStatusText("idle", { pendingCount: 2 });
    expect(text).toContain("尚未提交");
    expect(text).toContain("2 项");
    expect(text).toContain("保存都不会调用 QIO");
  });
});

describe("保存与草稿说明", () => {
  it("保存状态不把保存说成提交", () => {
    expect(saveStateText("saving", true, null)).toContain("不调用 QIO");
    expect(saveStateText("error", true, null)).toContain("未提交");
    expect(saveStateText("idle", true, null)).toContain("尚未保存");
    expect(saveStateText("saved", false, "2026-10-06T10:00:00.000Z")).toContain("已保存");
  });

  it("草稿说明写出真实时机，且不承诺未保存的输入能恢复", () => {
    const text = draftHintText();
    expect(text).toContain("0.6 秒");
    expect(text).toContain("0.45 秒");
    expect(text).toContain("草稿不是提交内容");
    expect(text).toContain("意外退出只保证已经保存的内容能恢复");
    expect(text).toContain("不承诺未保存的输入能恢复");
  });
});
