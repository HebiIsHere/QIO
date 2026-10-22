/**
 * 五入口的纯逻辑（可见性 / 计数去重 / 排队合并）。
 *
 * 这些用例只断言状态映射与身份规则，不依赖挂载与样式：
 * - 没有事项就不出现入口（不是常驻横幅）；
 * - 计数按身份去重，同一条记录不会因为多个来源被数两次；
 * - 「服务端已接受」「运行中」「本机已提交」「投递不明」必须分开表达。
 */
import { describe, expect, it } from "vitest";
import {
  COMPOSER_ENTRIES,
  continuationText,
  deriveComposerEntries,
  hasContinuation,
  hasSendIssues,
  pendingSendStateText,
  queueEntryCount,
  queueRows,
  sendIssueCount,
  snippetOf,
  whenText,
  type ComposerEntryInput,
  type SendIssueFacts,
} from "../entries";

function emptyFacts(over: Partial<SendIssueFacts> = {}): SendIssueFacts {
  return {
    failedSends: [],
    pendingSends: [],
    displacedDrafts: [],
    interruptedTurnIds: [],
    draftSaveFailed: false,
    storageWriteError: null,
    storageReadError: null,
    ...over,
  };
}

function input(over: Partial<ComposerEntryInput> = {}): ComposerEntryInput {
  return {
    sendIssues: emptyFacts(),
    continuation: { hasPendingIntent: false, anchorHistoric: false },
    topicSwitch: false,
    queue: { running: null, queued: [], cancelled: [] },
    localQueued: [],
    approvals: 0,
    ...over,
  };
}

describe("入口可见性：有事项才显示", () => {
  it("什么事项都没有时，五个入口全部不可见", () => {
    const badges = deriveComposerEntries(input());
    expect(badges.map((b) => b.id)).toEqual([...COMPOSER_ENTRIES]);
    expect(badges.every((b) => !b.visible)).toBe(true);
  });

  it("入口①在失败原文 / 受理未确认 / 未执行消息 / 被让位草稿出现时可见", () => {
    const cases: Partial<SendIssueFacts>[] = [
      { failedSends: [{ id: "f1", topicId: "t1" }] },
      { pendingSends: [{ id: "p1", topicId: "t1", state: "needs-confirm" }] },
      { interruptedTurnIds: ["turn_lost"] },
      { displacedDrafts: [{ id: "d1" }] },
      { draftSaveFailed: true },
      { storageReadError: "读取失败" },
    ];
    for (const c of cases) {
      const badges = deriveComposerEntries(input({ sendIssues: emptyFacts(c) }));
      expect(badges.find((b) => b.id === "sendIssues")?.visible, JSON.stringify(c)).toBe(true);
    }
  });

  it("旧消息恢复动作的结果也能让入口①保持可见（列表清空后仍需说清结果）", () => {
    const badges = deriveComposerEntries(
      input({ sendIssues: emptyFacts({ interruptedNotice: "已忽略 1 条没有执行的消息" }) }),
    );
    const badge = badges.find((b) => b.id === "sendIssues");
    expect(badge?.visible).toBe(true);
  });

  it("入口②：已登记接续或停在历史位置时可见", () => {
    expect(hasContinuation({ hasPendingIntent: false, anchorHistoric: false })).toBe(false);
    expect(hasContinuation({ hasPendingIntent: true, anchorHistoric: false })).toBe(true);
    expect(hasContinuation({ hasPendingIntent: false, anchorHistoric: true })).toBe(true);
  });

  it("入口⑤只按审批队列数量出现（数量为 0 不显示空入口）", () => {
    const none = deriveComposerEntries(input({ approvals: 0 }));
    expect(none.find((b) => b.id === "approvals")?.visible).toBe(false);
    const two = deriveComposerEntries(input({ approvals: 2 }));
    const badge = two.find((b) => b.id === "approvals");
    expect(badge?.visible).toBe(true);
    expect(badge?.count).toBe(2);
  });
});

describe("入口①计数：按身份去重", () => {
  it("同一条失败原文 / 未执行消息 / 草稿只计一次", () => {
    const facts = emptyFacts({
      failedSends: [
        { id: "f1", topicId: "t1" },
        { id: "f1", topicId: "t1" },
        { draftId: "draft-x", topicId: "t1" },
      ],
      pendingSends: [
        { id: "p1", topicId: "t1", state: "pending" },
        { id: "p1", topicId: "t1", state: "pending" },
      ],
      interruptedTurnIds: ["turn_1", "turn_1"],
      displacedDrafts: [{ id: "d1" }, { id: "d1" }],
    });
    expect(sendIssueCount(facts)).toBe(1 + 1 + 1 + 1 + 1);
    expect(hasSendIssues(facts)).toBe(true);
  });

  it("存储读写失败不是额外一条事项，只在没有任何其它事项时单独成一条", () => {
    expect(sendIssueCount(emptyFacts({ storageReadError: "读失败" }))).toBe(1);
    expect(
      sendIssueCount(
        emptyFacts({ storageReadError: "读失败", failedSends: [{ id: "f1", topicId: "t1" }] }),
      ),
    ).toBe(1);
  });

  it("草稿保存失败单独计一条（与失败原文不混为一谈）", () => {
    const facts = emptyFacts({ draftSaveFailed: true, failedSends: [{ id: "f1", topicId: "t1" }] });
    expect(sendIssueCount(facts)).toBe(2);
  });
});

describe("入口④排队行：服务端权威与本机乐观分开", () => {
  const running = { turn_id: "run_1", message: "正在跑的" };
  const queued = [
    { turn_id: "q1", message: "排队一" },
    { turn_id: "q2", message: "排队二" },
  ];

  it("顺序：运行中 → 排队（带位次）→ 已取消", () => {
    const rows = queueRows({ running, queued, cancelled: [{ turn_id: "c1", message: "取消掉的" }] });
    expect(rows.map((r) => r.state)).toEqual(["running", "queued", "queued", "cancelled"]);
    expect(rows.filter((r) => r.state === "queued").map((r) => r.position)).toEqual([1, 2]);
    expect(rows[1].turnId).toBe("q1");
  });

  it("本机乐观排队与服务端同文本行一一抵消（多重集，不吞掉同文两条）", () => {
    const rows = queueRows({ running: null, queued: [{ turn_id: "q1", message: "同一句" }], cancelled: [] }, [
      { id: "local_1", content: "同一句" },
      { id: "local_2", content: "同一句" },
    ]);
    // 服务端只确认了一条；另一条留在「本机已提交，等待后端确认」
    expect(rows.filter((r) => r.state === "queued")).toHaveLength(1);
    const locals = rows.filter((r) => r.state === "local");
    expect(locals).toHaveLength(1);
    expect(locals[0].turnId).toBeNull();
    expect(queueEntryCount(rows)).toBe(2);
  });

  it("排队计数不含运行中与已取消", () => {
    const rows = queueRows({ running, queued, cancelled: [{ turn_id: "c1", message: "已取消" }] });
    expect(queueEntryCount(rows)).toBe(2);
    const badge = deriveComposerEntries(
      input({ queue: { running, queued, cancelled: [{ turn_id: "c1", message: "已取消" }] } }),
    ).find((b) => b.id === "queue");
    expect(badge?.visible).toBe(true);
    expect(badge?.count).toBe(2);
  });

  it("只有运行中时入口仍可见（可以按身份停止），但徽标计数为 0", () => {
    const badge = deriveComposerEntries(
      input({ queue: { running, queued: [], cancelled: [] } }),
    ).find((b) => b.id === "queue");
    expect(badge?.visible).toBe(true);
    expect(badge?.count).toBe(0);
  });

  it("受理未确认的发送不混进排队数量（它属于入口①）", () => {
    const data = input({
      sendIssues: emptyFacts({ pendingSends: [{ id: "p1", topicId: "t1", state: "needs-confirm" }] }),
      queue: { running: null, queued: [], cancelled: [] },
    });
    const badges = deriveComposerEntries(data);
    expect(badges.find((b) => b.id === "queue")?.visible).toBe(false);
    expect(badges.find((b) => b.id === "sendIssues")?.count).toBe(1);
  });
});

describe("文案小工具", () => {
  it("接续说明不暴露内部 intent 身份", () => {
    expect(continuationText({ sourceTitle: "旧片段" })).toBe("下一条消息将从「旧片段」继续");
    expect(continuationText(null)).toBe("下一条消息将从所选历史继续");
  });

  it("受理未确认与刚发出的状态文案不同（不能把未知说成失败）", () => {
    expect(pendingSendStateText("needs-confirm")).toContain("未确认");
    expect(pendingSendStateText("needs-confirm")).not.toContain("失败");
    expect(pendingSendStateText("pending")).toContain("等后端回执");
  });

  it("whenText 解析不了时如实说未知，不猜", () => {
    const now = new Date("2026-10-22T10:00:00+08:00");
    expect(whenText("not-a-date", now)).toBe("时间未知");
    expect(whenText(null, now)).toBe("时间未知");
    expect(whenText("2026-10-22T09:05:00+08:00", now)).toContain("今天 09:05");
    expect(whenText("2026-10-20T09:05:00+08:00", now)).toContain("10 月 20 日");
  });

  it("snippetOf 压缩换行并在过长时保留省略号", () => {
    expect(snippetOf("第一行\n第二行")).toBe("第一行 第二行");
    expect(snippetOf("长".repeat(50), 10)).toBe("长".repeat(10) + "…");
    expect(snippetOf("   ")).toBe("（空内容）");
  });
});
