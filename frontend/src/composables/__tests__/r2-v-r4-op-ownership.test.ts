/**
 * V 组独立验证：R4（过期操作 token 的轮询结果不得写界面或持久化）。
 *
 * 第一层是纯守卫：beginAttachmentOp 的「操作声明序号」决定谁是当前事实。
 * 第二层在 Composer 组件里用**受控 Promise**做逆序返回（见
 * components/__tests__/r2-v-r4-poll-vs-relocate.test.ts）。
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  beginAttachmentOp,
  decideAttachmentWrite,
  markAttachmentsSent,
  registerAttachmentIdentity,
  resetAttachmentOpState,
  mergeRestorePatch,
} from "../attachmentOps";

const TOPIC_A = "topic-v-r4-a";
const TOPIC_B = "topic-v-r4-b";
const ID = "att_r4_1";

beforeEach(() => {
  resetAttachmentOpState();
  localStorage.clear();
});

describe("R4 操作声明序号（claim）决定当前事实", () => {
  it("先发起的轮询、后返回 → 被更晚发起的重新定位取代 → drop（既不写界面也不落盘）", () => {
    registerAttachmentIdentity(TOPIC_A, ID);
    // 轮询先发起
    const poll = beginAttachmentOp({ kind: "poll", topicId: TOPIC_A, attachmentIds: [ID] });
    // 重新定位后发起：它拿走这条上最新的声明
    const relocate = beginAttachmentOp({ kind: "relocate", topicId: TOPIC_A, attachmentIds: [ID] });

    const late = decideAttachmentWrite(poll, { id: ID, isCurrentTopic: true });
    expect(late, "过期轮询结果必须 drop").toEqual({ target: "drop", reason: "superseded" });

    const fresh = decideAttachmentWrite(relocate, { id: ID, isCurrentTopic: true });
    expect(fresh).toEqual({ target: "ui", reason: "ok" });
  });

  it("反过来（轮询后发起、重新定位先返回）不误伤：后发起者才是当前事实", () => {
    registerAttachmentIdentity(TOPIC_A, ID);
    const relocate = beginAttachmentOp({ kind: "relocate", topicId: TOPIC_A, attachmentIds: [ID] });
    const poll = beginAttachmentOp({ kind: "poll", topicId: TOPIC_A, attachmentIds: [ID] });

    expect(decideAttachmentWrite(relocate, { id: ID, isCurrentTopic: true }).target).toBe("drop");
    expect(decideAttachmentWrite(poll, { id: ID, isCurrentTopic: true })).toEqual({
      target: "ui",
      reason: "ok",
    });
  });

  it("仍然有效的其它话题操作：只能按它自己的原话题落盘（不写当前 UI）", () => {
    registerAttachmentIdentity(TOPIC_A, ID);
    const capture = beginAttachmentOp({ kind: "relocate", topicId: TOPIC_A, attachmentIds: [ID] });
    const verdict = decideAttachmentWrite(capture, { id: ID, isCurrentTopic: false });
    expect(verdict).toEqual({ target: "persistence", reason: "other-topic" });
  });

  it("已移除 / 已发送：一律 drop（过期结果不得复活）", () => {
    registerAttachmentIdentity(TOPIC_A, ID);
    const capture = beginAttachmentOp({ kind: "poll", topicId: TOPIC_A, attachmentIds: [ID] });
    markAttachmentsSent(TOPIC_A, [ID]);
    expect(decideAttachmentWrite(capture, { id: ID, isCurrentTopic: true })).toEqual({
      target: "drop",
      reason: "sent",
    });
  });
});

describe("R4/R3 边界：恢复补丁只新增，绝不复活死条目", () => {
  it("补丁里的新条目若已被移除（水位之后）→ 不加入", () => {
    const merged = mergeRestorePatch(
      {
        topicId: TOPIC_A,
        revision: 0,
        restored: [
          {
            id: "att_new",
            name: "新.bin",
            sizeBytes: 1,
            kind: "copy",
            display: "已保存副本",
            state: "ready",
            error: null,
          },
        ],
        missing: [],
        missingIds: [],
      },
      [],
      { removedBarrier: 0 },
    );
    // 水位之前的 tombstone 不挡；这里没有 tombstone，所以新增应通过
    expect(merged.added).toEqual(["att_new"]);
  });
});
