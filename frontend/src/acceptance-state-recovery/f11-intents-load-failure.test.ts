/**
 * 【审计 8fa8452 · F11】任务清单读取失败被显示成「任务消失」。
 *
 * 反例（基线 loadIntents）：catch 里把 intents 清空、还把错误推进 recoverNotice。
 * 于是断网时已经运行中的任务从界面上消失、错误提示反复累积；
 * 而且界面分不清「成功返回空集合」与「这次读取失败」。
 *
 * 正确行为：
 * - 读取失败保留最后一次已知任务 / 冲突 / 批次依据，只标明刷新失败；
 * - 成功返回空集合与请求失败必须区分；
 * - 依赖最新事实的审批等动作先做服务端核验，核验不成则暂停（不拿旧快照当权威）；
 * - 重试成功后清掉过期错误。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { Intent } from "../interactive/types";
import { intent, payload } from "./support/harness";

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
}));

const RUNNING = intent("t1", "running") as unknown as Intent;

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, []) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [RUNNING],
    conflicts: [["t1", "t2"]],
    batchAvailable: true,
    recovery: { paused: ["p1"] },
  } as never);
  vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
    const put = putState as never as Record<string, unknown>;
    return { ok: true, seq: 4, savedAt: "t4", state: { ...put, seq: 4 } } as never;
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F11 任务清单读取失败", () => {
  it("已有 running 任务后读取失败：任务、冲突与批次依据都保留，并标明刷新失败", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.intents.map((item) => item.id)).toEqual(["t1"]);
    expect(store.intentsLoadState).toBe("ok");

    vi.mocked(api.fetchIntents).mockRejectedValue(new Error("网络断了"));
    await store.loadIntents();
    await flushPromises();

    expect(store.intents.map((item) => item.id), "读取失败被显示成任务消失了（F11）").toEqual(["t1"]);
    expect(store.conflicts, "冲突依据不许被清空").toEqual([["t1", "t2"]]);
    expect(store.batchAvailable, "批次依据不许被清空").toBe(true);
    expect(store.intentsLoadState, "必须标明这次刷新失败").toBe("error");
    expect(store.intentsLoadError ?? "").toContain("网络断了");
    expect(store.recoverNotice, "失败原因不许被塞进任务恢复提示里反复累积").toEqual(["p1"]);
  });

  it("首次加载就失败：如实说明「刷新失败」，不假装任务为空的成功结果", async () => {
    vi.mocked(api.fetchIntents).mockRejectedValue(new Error("后端还没起来"));
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    expect(store.intents).toEqual([]);
    expect(store.intentsLoadedOnce, "从未成功读到过").toBe(false);
    expect(store.intentsLoadState).toBe("error");
  });

  it("成功返回空集合与请求失败必须区分；恢复成功后清掉过期错误", async () => {
    const store = useInteractiveStore();
    await store.load();

    vi.mocked(api.fetchIntents).mockRejectedValue(new Error("网络断了"));
    await store.loadIntents();
    await store.loadIntents(); // 连续失败不许累积多条提示
    expect(store.intentsLoadState).toBe("error");

    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    expect(store.intentsLoadState, "恢复读取后状态必须回到 ok").toBe("ok");
    expect(store.intentsLoadError, "过期错误必须被清掉").toBeNull();
    expect(store.intentsLoadedOnce, "已经成功读到过空集合").toBe(true);
    expect(store.intents).toEqual([]);
  });

  it("事实可疑时审批先做服务端核验；核验不成则暂停，不拿旧快照放行", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockRejectedValue(new Error("网络断了"));
    await store.loadIntents();

    vi.mocked(api.approveIntent).mockResolvedValue({ ok: true } as never);
    const blocked = await store.approve("t1");
    expect(blocked.ok, "读取失败时不许拿旧快照批准").toBe(false);
    expect(blocked.reason ?? "", "必须说清为什么暂停").toContain("任务清单读取失败");
    expect(vi.mocked(api.approveIntent).mock.calls.length, "核验没通过就不许发出审批请求").toBe(0);

    // 恢复读取之后审批照常进行
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [RUNNING], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    await store.approve("t1");
    expect(vi.mocked(api.approveIntent).mock.calls.length, "核验通过后审批必须真的发出").toBe(1);
  });
});
