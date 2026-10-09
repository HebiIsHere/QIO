/**
 * 收尾轮 B2：本机删除失败必须真实可见（契约 M2 / 反例 13，session.ts 侧调用点）。
 *
 * 正确行为期望（与 A2 在 drafts.ts 定稿的删除结果口径一致）：
 * - reason === "storage-failure" 才算真失败：保留待处理状态、显示真实原因、可重试，
 *   **不许**显示「已保存 / idle / 已完成」；
 * - "version-guard"（版本更高、有意保留）与 "missing-record"（本来就没有）不算失败，静默。
 *
 * 说明：本分支上 drafts.removeDraft 还是旧签名（void）；为了让「删不掉」这条路径可验证，
 * 这里把 drafts 模块按同一口径做**接口替身**（底层返回结果对象），session.ts 的消费逻辑
 * 与 A2 合并后的真实实现完全一致。标注：【模拟】底层删除结果替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { removeDraft } from "../../interactive/drafts";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(),
  },
}));

vi.mock("../../services/interactive", () => ({
  saveDrafts: vi.fn(),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

vi.mock("../../interactive/drafts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../interactive/drafts")>();
  return { ...actual, removeDraft: vi.fn(actual.removeDraft) };
});

function storageFailure(): never {
  return {
    ok: false,
    removed: false,
    reason: "storage-failure",
    error: "本机存储被禁用",
  } as never;
}

function acceptedSend(turnId = "turn_1") {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: null };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("反例 13：聊天清空草稿的本机删除失败不许静默报成完成", () => {
  it("【状态】清空草稿时底层删除失败：显示真实原因，绝不能显示 idle / 已保存", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "要清掉的草稿";
    session.flushDraft();
    expect(session.draftSaveStatus).toBe("saved");

    vi.mocked(removeDraft).mockReturnValueOnce(storageFailure());
    session.draft = "";
    session.flushDraft();

    expect(
      session.draftSaveStatus,
      "本机删除失败却显示成 idle（用户会以为已经清干净了）",
    ).toBe("error");
    expect(session.draftSaveError, "删除失败没有给出真实原因").toContain("本机存储被禁用");
  });

  it("【状态】version-guard（有意保留更高版本）与 missing-record 都不算失败", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一版";
    session.flushDraft();

    vi.mocked(removeDraft).mockReturnValueOnce({
      ok: true,
      removed: false,
      reason: "version-guard",
    } as never);
    session.draft = "";
    session.flushDraft();
    expect(session.draftSaveStatus, "版本守卫是有意保留，不该报成失败").toBe("idle");
    expect(session.draftSaveError).toBeNull();

    vi.mocked(removeDraft).mockReturnValueOnce({
      ok: true,
      removed: false,
      reason: "missing-record",
    } as never);
    session.draft = "";
    session.flushDraft();
    expect(session.draftSaveStatus, "本来就没有记录，不该报成失败").toBe("idle");
  });
});

describe("反例 13：已经受理的发送清理失败也不许显示成完成", () => {
  it("【状态】受理成功后本机删除失败：状态是 error 并带真实原因", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "已经发出去的话";
    session.flushDraft();

    const at = session.sendAttribution();
    expect(at.topicId).toBe("A");
    session.draft = "";
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend() as never);
    vi.mocked(removeDraft).mockReturnValueOnce(storageFailure());

    expect(await session.send("已经发出去的话")).toBe(true);
    expect(
      session.draftSaveStatus,
      "已经发出的原文没删掉，界面却显示完成（重开后它会被当成草稿装回来）",
    ).toBe("error");
    expect(session.draftSaveError).toContain("本机存储被禁用");
  });
});

describe("反例 13：话题迁移清理源键失败要如实说", () => {
  it("【状态】未绑定→绑定 A 时源键删不掉：显示真实原因", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    expect(session.currentTopicId).toBeNull();
    session.draft = "未绑定位置的草稿";
    session.flushDraft();

    vi.mocked(removeDraft).mockReturnValueOnce(storageFailure());
    session.currentTopicId = "A";

    expect(
      session.draftSaveStatus,
      "未绑定位置的旧副本没删掉，界面却显示一切正常（换个话题旧稿还会冒出来）",
    ).toBe("error");
    expect(session.draftSaveError).toContain("本机存储被禁用");
  });
});
