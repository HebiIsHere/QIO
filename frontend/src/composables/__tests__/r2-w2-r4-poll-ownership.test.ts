/**
 * R4（W2）旧轮询结果不得覆盖较新的重新定位结果（round2）。
 *
 * 反例（基线 6ca65f9 红）：`beginAttachmentOp` 对条目级操作只**读**当前 token，
 * 所以「重试（poll）」与随后的「重新定位（relocate）」拿到同一个身份 ——
 * 两个结果都判成 ui。逆序返回时，旧轮询的过期事实会把已经 ready 的那条
 * 覆盖回旧状态，并且真的写进持久化。
 *
 * 冻结规则：**过期操作 token** 的轮询结果不得写入界面**或持久化**；
 * 仍然有效但属于**别的**话题的操作，只能按它自己的原话题落盘。
 *
 * 这里用与 Composer.commitAttachment 同口径的最小组合（decideAttachmentWrite → 写哪里）
 * 做受控逆序返回，不 sleep。
 * 运行：cd frontend; npx vitest run src/composables/__tests__/r2-w2-r4-poll-ownership.test.ts
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  beginAttachmentOp,
  decideAttachmentWrite,
  registerAttachmentIdentity,
  resetAttachmentOpState,
  type AttachmentOpCapture,
} from "../attachmentOps";
import {
  loadPendingAttachments,
  pendingRevision,
  savePendingAttachments,
  type AttachmentRef,
} from "../../services/attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_x",
    name: "报告.pdf",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

/** 测试里的「组件」：一个当前话题 + 它的界面列表（等价 Composer 的 pending）。 */
interface FakeComposer {
  topicId: string | null;
  ui: AttachmentRef[];
}

function upsert(list: readonly AttachmentRef[], item: AttachmentRef): AttachmentRef[] {
  const index = list.findIndex((a) => a.id === item.id);
  const next = list.slice();
  if (index >= 0) next[index] = item;
  else next.push(item);
  return next;
}

/** 与 Composer.commitAttachment 同口径：ui → 界面 + 持久化；persistence → 只落发起话题。 */
function commit(store: FakeComposer, capture: AttachmentOpCapture, item: AttachmentRef) {
  const verdict = decideAttachmentWrite(capture, {
    id: item.id,
    isCurrentTopic: store.topicId === capture.topicId,
  });
  if (verdict.target === "drop") return verdict;
  const base = verdict.target === "ui" ? store.ui : loadPendingAttachments(capture.topicId);
  const next = upsert(base, item);
  if (verdict.target === "ui") store.ui = next;
  savePendingAttachments(capture.topicId, next);
  return verdict;
}

beforeEach(() => {
  localStorage.clear();
  resetAttachmentOpState();
});

describe("R4 轮询 / 重新定位的归属与顺序", () => {
  it("轮询与重新定位逆序返回：旧轮询结果不得覆盖较新的 ready，也不得落盘", () => {
    const store: FakeComposer = { topicId: "A", ui: [] };
    registerAttachmentIdentity("A", "att_x");
    store.ui = [ref({ id: "att_x", state: "prepared" })];
    savePendingAttachments("A", store.ui);

    // 用户先点「重试」（轮询），随后点「重新定位」
    const poll = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    const relocate = beginAttachmentOp({ kind: "relocate", topicId: "A", attachmentIds: ["att_x"] });

    // 重新定位先返回：ready 落定
    expect(commit(store, relocate, ref({ id: "att_x", state: "ready" })).target).toBe("ui");
    const revisionAfterReady = pendingRevision("A");
    expect(store.ui.map((i) => i.state)).toEqual(["ready"]);

    // 旧轮询后返回：它捕获的是重新定位之前的事实
    const staleVerdict = decideAttachmentWrite(poll, { id: "att_x", isCurrentTopic: true });
    expect(staleVerdict, "过期操作 token 的旧轮询结果仍被允许写界面").toEqual({
      target: "drop",
      reason: "superseded",
    });
    expect(commit(store, poll, ref({ id: "att_x", state: "missing", error: "旧事实" })).target).toBe("drop");

    expect(store.ui.map((i) => i.state), "界面被旧轮询结果覆盖了").toEqual(["ready"]);
    expect(loadPendingAttachments("A").map((i) => i.state), "持久化被旧轮询结果覆盖了").toEqual(["ready"]);
    expect(pendingRevision("A"), "旧结果一个字节都不该写").toBe(revisionAfterReady);
  });

  it("同一附件连续两次轮询：后发起的才是当前操作，先发起的旧结果被丢弃", () => {
    const store: FakeComposer = { topicId: "A", ui: [ref({ id: "att_x", state: "prepared" })] };
    savePendingAttachments("A", store.ui);

    const first = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    const second = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });

    expect(commit(store, second, ref({ id: "att_x", state: "ready" })).target).toBe("ui");
    const revisionAfterReady = pendingRevision("A");
    expect(decideAttachmentWrite(first, { id: "att_x", isCurrentTopic: true }).target).toBe("drop");
    expect(commit(store, first, ref({ id: "att_x", state: "failed", error: "旧事实" })).target).toBe("drop");

    expect(store.ui.map((i) => i.state)).toEqual(["ready"]);
    expect(loadPendingAttachments("A").map((i) => i.state)).toEqual(["ready"]);
    expect(pendingRevision("A")).toBe(revisionAfterReady);
  });

  it("仍然有效但属于别的话题的操作：只按它自己的原话题落盘，不写当前话题", () => {
    const store: FakeComposer = { topicId: "B", ui: [ref({ id: "att_b", name: "B.txt" })] };
    savePendingAttachments("B", store.ui);

    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_a"] });
    expect(decideAttachmentWrite(capture, { id: "att_a", isCurrentTopic: false })).toEqual({
      target: "persistence",
      reason: "other-topic",
    });
    expect(commit(store, capture, ref({ id: "att_a", name: "A.txt", state: "ready" })).target).toBe("persistence");

    expect(loadPendingAttachments("A").map((i) => i.id), "结果没有落到它自己的原话题").toEqual(["att_a"]);
    expect(loadPendingAttachments("B").map((i) => i.id), "当前话题 B 被别的操作改了").toEqual(["att_b"]);
    expect(store.ui.map((i) => i.id), "别的操作写了当前界面").toEqual(["att_b"]);
  });

  it("回归：当前话题 + 身份仍然有效 → 照旧写界面", () => {
    const store: FakeComposer = { topicId: "A", ui: [ref({ id: "att_x", state: "prepared" })] };
    savePendingAttachments("A", store.ui);
    const capture = beginAttachmentOp({ kind: "poll", topicId: "A", attachmentIds: ["att_x"] });
    expect(commit(store, capture, ref({ id: "att_x", state: "ready" })).target).toBe("ui");
    expect(store.ui.map((i) => i.state)).toEqual(["ready"]);
  });
});
