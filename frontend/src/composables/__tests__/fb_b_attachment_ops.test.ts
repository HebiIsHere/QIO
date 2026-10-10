/**
 * fb-B 共享守卫单测（R3/R4 · 契约 K1.1—K1.7）。
 *
 * 守卫是「附件异步操作身份 + 写权限判定」的唯一入口：Composer 与 MessageItem
 * （C 的范围）都用它，所以这里先冻结 API 行为，再让实现对齐。
 *
 * 规格（与 docs/plans/2026-10-10-final-boundaries-r1-r7.md §二 K1 一致）：
 *   * 发起时捕获 {topicId, attachmentId(s), opToken, kind}，之后不再读 currentTopicId；
 *   * 附件级 opToken（注册/替换递增）+ topic 级 topicEpoch（发送受理/清空/重挂载）；
 *   * removed tombstone 持久化、sent 失效集；
 *   * 写权限：ui / persistence / drop 三态；removed 与 sent 一律静默丢弃；
 *   * 恢复补丁合并只新增，绝不复活 removed/sent，绝不覆盖更晚状态。
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  attachmentOpToken,
  beginAttachmentOp,
  bumpTopicEpoch,
  decideAttachmentWrite,
  invalidateAttachmentIdentity,
  isAttachmentDead,
  isAttachmentSent,
  markAttachmentsSent,
  mergeRestorePatch,
  registerAttachmentIdentity,
  resetAttachmentOpState,
  restoreAttachmentIdentity,
  topicEpochOf,
} from "../attachmentOps";
import {
  attachmentRemovalBarrier,
  forgetAttachmentRemoved,
  loadRemovedAttachmentIds,
  markAttachmentRemoved,
  pendingRevision,
  savePendingAttachments,
  type AttachmentRef,
} from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "报告.pdf",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

beforeEach(() => {
  localStorage.clear();
  resetAttachmentOpState();
});

describe("K1.1 发起时刻捕获身份", () => {
  it("捕获 {kind, topicId, attachmentIds, opToken, topicEpoch}，与之后的 currentTopicId 无关", () => {
    registerAttachmentIdentity("A", "att_a1");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_a1"] });

    expect(capture.kind).toBe("poll");
    expect(capture.topicId).toBe("A");
    expect(capture.attachmentIds).toEqual(["att_a1"]);
    expect(capture.opToken).toBe(attachmentOpToken("att_a1"));
    expect(capture.topicEpoch).toBe(topicEpochOf("A"));
    // 捕获对象是冻结的：后续函数不得就地改写归属
    expect(Object.isFrozen(capture)).toBe(true);
  });

  it("新建类操作分配独立 token：注册到新附件后 token 即该操作身份", () => {
    const capture = beginAttachmentOp({ kind: "upload", topicId: "A" });
    expect(capture.attachmentIds).toEqual([]);
    expect(capture.opToken).toBeGreaterThan(0);
    const token = registerAttachmentIdentity("A", "att_new", capture.opToken);
    expect(token).toBe(capture.opToken);
    expect(attachmentOpToken("att_new")).toBe(capture.opToken);
  });

  it("替换（重新注册）后 token 递增：旧操作的 token 不再等于当前值", () => {
    const first = registerAttachmentIdentity("A", "att_x");
    const second = registerAttachmentIdentity("A", "att_x");
    expect(second).toBeGreaterThan(first);
    expect(attachmentOpToken("att_x")).toBe(second);
  });
});

describe("K1.3 写权限判定（ui / persistence / drop）", () => {
  it("当前话题 + token 一致 + 未移除未发送 → ui", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true })).toEqual({
      target: "ui",
      reason: "ok",
    });
  });

  it("结果属于其它话题 → 落原话题持久化（不写当前 UI），不是丢弃", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: false })).toEqual({
      target: "persistence",
      reason: "other-topic",
    });
  });

  it("removed tombstone → 静默丢弃（绝不 upsert 回来）", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    markAttachmentRemoved("A", "att_x");
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true })).toEqual({
      target: "drop",
      reason: "removed",
    });
    expect(isAttachmentDead("A", "att_x")).toBe(true);
  });

  it("发送已受理（sent）→ 静默丢弃", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    markAttachmentsSent("A", ["att_x"]);
    expect(isAttachmentSent("A", "att_x")).toBe(true);
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true })).toEqual({
      target: "drop",
      reason: "sent",
    });
  });

  it("token 已失效（移除/替换）→ 只允许落持久化，不写 UI", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "relocate", topicId: "A", attachmentIds: ["att_x"] });
    invalidateAttachmentIdentity("att_x");
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true })).toEqual({
      target: "persistence",
      reason: "token-stale",
    });
  });

  it("restore 类整表快照：话题世代被推进后整体失效（只落持久化）", () => {
    savePendingAttachments("A", [ref({ id: "att_x" })]);
    const capture = beginAttachmentOp({ kind: "restore", topicId: "A" });
    bumpTopicEpoch("A");
    expect(decideAttachmentWrite(capture, { isCurrentTopic: true })).toEqual({
      target: "persistence",
      reason: "epoch-passed",
    });
  });

  it("条目级操作：只在附件条目本身在操作发起后被重新登记时才算过期（发送受理不冻结在飞轮询）", () => {
    registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    bumpTopicEpoch("A"); // 别的一轮被受理：话题世代前进
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true })).toEqual({
      target: "ui",
      reason: "ok",
    });
    // 同一个附件被重新登记（新条目）之后，旧操作不得再写 UI
    registerAttachmentIdentity("A", "att_x");
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true }).target).toBe("persistence");
  });

  it("删除失败恢复身份后，旧操作可以继续更新这一条（而不是永久失效）", () => {
    const token = registerAttachmentIdentity("A", "att_x");
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    invalidateAttachmentIdentity("att_x");
    restoreAttachmentIdentity("att_x", token);
    expect(decideAttachmentWrite(capture, { id: "att_x", isCurrentTopic: true }).target).toBe("ui");
  });
});

describe("K1.6 恢复补丁合并", () => {
  it("只新增：已在当前列表的条目保持更晚状态，不被旧快照覆盖", () => {
    const current = [ref({ id: "att_keep", state: "ready", name: "本地更晚.txt" })];
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 3, restored: [ref({ id: "att_keep", state: "prepared", name: "旧快照.txt" }), ref({ id: "att_new" })], missing: [], missingIds: [] },
      current,
    );
    expect(merged.list.map((i) => i.id)).toEqual(["att_keep", "att_new"]);
    expect(merged.list[0].state).toBe("ready");
    expect(merged.list[0].name).toBe("本地更晚.txt");
    expect(merged.added).toEqual(["att_new"]);
  });

  it("已移除（tombstone）的 id 绝不复活", () => {
    markAttachmentRemoved("A", "att_gone");
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 1, restored: [ref({ id: "att_gone" })], missing: [], missingIds: [] },
      [],
    );
    expect(merged.list).toEqual([]);
    expect(merged.skipped).toEqual([{ id: "att_gone", reason: "removed" }]);
  });

  it("移除水位：核对其间（水位之后）被移除的候选一律挡住", () => {
    const barrier = attachmentRemovalBarrier(); // 发起核对
    markAttachmentRemoved("A", "att_x"); // 核对在途时用户移除
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 1, restored: [ref({ id: "att_x" })], missing: [], missingIds: [] },
      [ref({ id: "att_keep" })],
      { removedBarrier: barrier },
    );
    expect(merged.list.map((i) => i.id)).toEqual(["att_keep"]);
    expect(merged.skipped).toEqual([{ id: "att_x", reason: "removed" }]);
  });

  it("移除水位：水位之前就存在的 tombstone 不会出现在真实补丁里（服务核对阶段已硬跳过）", () => {
    markAttachmentRemoved("A", "att_old");
    const barrier = attachmentRemovalBarrier(); // 之后才发起核对：服务根本不会核对这个候选
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 1, restored: [ref({ id: "att_old" })], missing: [], missingIds: [] },
      [],
      { removedBarrier: barrier },
    );
    // 不传水位时一律挡（严格口径）；传了水位则只挡「水位之后」的移除
    expect(mergeRestorePatch({
      topicId: "A", revision: 1, restored: [ref({ id: "att_old" })], missing: [], missingIds: [],
    }, []).list).toEqual([]);
    expect(merged.added).toEqual(["att_old"]);
  });

  it("已发送（sent）的 id 绝不复活", () => {
    markAttachmentsSent("A", ["att_sent"]);
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 1, restored: [ref({ id: "att_sent" })], missing: [], missingIds: [] },
      [],
    );
    expect(merged.list).toEqual([]);
    expect(merged.skipped).toEqual([{ id: "att_sent", reason: "sent" }]);
  });

  it("确认永久无效的 id 从当前列表剔除并报出名字", () => {
    const current = [ref({ id: "att_ok" }), ref({ id: "att_gone", name: "没了.txt" })];
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 2, restored: [ref({ id: "att_ok" })], missing: ["没了.txt"], missingIds: ["att_gone"] },
      current,
    );
    expect(merged.list.map((i) => i.id)).toEqual(["att_ok"]);
    expect(merged.skipped).toEqual([{ id: "att_gone", reason: "missing" }]);
  });

  it("暂时无法确认：保留身份（id/名字/状态）只加 unconfirmed 标记，不覆盖其它事实", () => {
    const current = [ref({ id: "att_u", name: "还在.txt", state: "ready", sizeBytes: 42 })];
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 4, restored: [ref({ id: "att_u", name: "还在.txt", state: "ready", sizeBytes: 42, unconfirmed: true })], missing: [], missingIds: [] },
      current,
    );
    expect(merged.list).toHaveLength(1);
    expect(merged.list[0].id).toBe("att_u");
    expect(merged.list[0].name).toBe("还在.txt");
    expect(merged.list[0].unconfirmed).toBe(true);
    expect(merged.added).toEqual([]);
  });

  it("这次核对确认后清掉「暂时无法确认」标记（身份仍是同一条，用后端新事实）", () => {
    const current = [ref({ id: "att_u", name: "还在.txt", state: "ready", unconfirmed: true, error: "上次没确认" })];
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 1, restored: [ref({ id: "att_u", name: "还在.txt", state: "ready", error: null })], missing: [], missingIds: [] },
      current,
    );
    expect(merged.list).toHaveLength(1);
    expect(merged.list[0].id).toBe("att_u");
    expect(merged.list[0].unconfirmed).toBeUndefined();
    expect(merged.list[0].state).toBe("ready");
  });

  it("dropMissing=false（修订号不一致）时保留当前列表里确认无效的条目，交给下一次恢复清理", () => {
    const current = [ref({ id: "att_gone" })];
    const merged = mergeRestorePatch(
      { topicId: "A", revision: 2, restored: [], missing: ["没了.txt"], missingIds: ["att_gone"] },
      current,
      { dropMissing: false },
    );
    expect(merged.list.map((i) => i.id)).toEqual(["att_gone"]);
  });
});

describe("tombstone / 修订号持久化", () => {
  it("tombstone 写入持久化：新的守卫实例（模拟重挂载）仍能读到", () => {
    markAttachmentRemoved("A", "att_x");
    expect(loadRemovedAttachmentIds("A")).toEqual(["att_x"]);
    // 模拟组件重挂载：内存状态重建，但磁盘上的 tombstone 仍在
    resetAttachmentOpState();
    expect(isAttachmentDead("A", "att_x")).toBe(true);
    forgetAttachmentRemoved("A", "att_x");
    expect(isAttachmentDead("A", "att_x")).toBe(false);
  });

  it("每次持久化写入都会推进该话题的修订号", () => {
    const first = pendingRevision("A");
    savePendingAttachments("A", [ref({ id: "att_1" })]);
    const second = pendingRevision("A");
    savePendingAttachments("A", [ref({ id: "att_1" }), ref({ id: "att_2" })]);
    expect(second).toBeGreaterThan(first);
    expect(pendingRevision("A")).toBeGreaterThan(second);
    expect(pendingRevision("B")).toBe(first);
  });
});
