/**
 * E 路独立核验 · F10 最小隔离反例（只测 submit 的两段事实，不依赖 store 初始化/防抖）。
 *
 * 规格：F10 —— 分开记录提交请求结果与后续读取结果；POST 明确成功后 GET 失败应显示
 * 「提交成功，页面刷新失败」，保留成功事实、提供只重读的重试，绝不自动重复提交。
 *
 * 运行（在**被测 worktree** 的 frontend 下）：
 *   $env:QIO_FE_SRC='D:/qio-dev/qa-a/frontend/src'
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.config.ts' f10-min
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../frontend/src/services/interactive", () => ({
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
  updateIntentPreview: vi.fn(),
  saveDrafts: vi.fn(),
}));
vi.mock("../../frontend/src/services/api", () => ({
  api: { sendTurn: vi.fn(), getSessionContext: vi.fn(), stopTurn: vi.fn(), saveDrafts: vi.fn(), lookupTurnByRequest: vi.fn(), getInstance: vi.fn() },
}));

import { useInteractiveStore } from "../../frontend/src/stores/interactive";
import * as interactiveApi from "../../frontend/src/services/interactive";

function installLocalStorage(): void {
  const map = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
      setItem: (k: string, v: string) => void map.set(k, String(v)),
      removeItem: (k: string) => void map.delete(k),
      clear: () => map.clear(),
      key: (i: number) => Array.from(map.keys())[i] ?? null,
      get length() { return map.size; },
    },
  });
}
const card = (id: string, content: string) => ({
  id, kind: "text" as const, content, meta: {}, x: 0, y: 0, w: 1, h: 1,
  checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
  createdAt: "2026-10-11T00:00:00Z", updatedAt: "2026-10-11T00:00:00Z",
});
const boardState = (seq: number) => ({
  boardId: "board_default", seq, updatedAt: "2026-10-11T00:00:00Z",
  cards: [card("card:a", "要提交的内容")], groups: [], links: [], selection: [],
});
const payload = (seq: number) => ({
  board: { id: "board_default", title: "板面", createdAt: "x", updatedAt: "x" },
  state: boardState(seq), seq, savedAt: "2026-10-11T00:00:00Z", submissions: [],
});
async function settle(n = 12): Promise<void> { for (let i = 0; i < n; i += 1) await Promise.resolve(); }

beforeEach(() => {
  installLocalStorage();
  vi.resetAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: false });
  setActivePinia(createPinia());
  vi.mocked(interactiveApi.fetchBoardState).mockResolvedValue(payload(3) as never);
  vi.mocked(interactiveApi.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } } as never);
  vi.mocked(interactiveApi.fetchIntents).mockResolvedValue({ intents: [], batches: [] } as never);
  // 自动/防抖保存也要能成功：saveNow 需要 result.state / result.seq 的完整形状
  vi.mocked(interactiveApi.saveBoardState).mockResolvedValue({
    seq: 3,
    savedAt: "2026-10-11T00:00:00Z",
    state: boardState(3),
    pending: { baselineSeq: 3, expressions: [], updatedAt: "x" },
  } as never);
});

describe("F10 最小隔离反例", () => {
  it("F10-min-a POST 明确成功 + 随后所有读取失败 → 提交成功事实必须保留，刷新失败必须独立可见", async () => {
    const store = useInteractiveStore();
    await store.refreshBoardFromServer();
    // 进入「已保存、无未保存候选」的干净状态
    if (store.dirty) { await store.saveNow(); await settle(); }
    vi.advanceTimersByTime(0); await settle();
    console.log("[F10-min-a] 提交前 saveStatus =", store.saveStatus, "dirty =", store.dirty, "boardSeq =", store.board?.seq);

    vi.mocked(interactiveApi.submitBoard).mockResolvedValue({ ok: true, submissionId: "sub_1" } as never);
    vi.mocked(interactiveApi.fetchBoardState).mockRejectedValue(new TypeError("Failed to fetch"));
    vi.mocked(interactiveApi.fetchVisibleRange).mockRejectedValue(new TypeError("Failed to fetch"));
    vi.mocked(interactiveApi.fetchIntents).mockRejectedValue(new TypeError("Failed to fetch"));

    await store.saveNow();
    await settle();
    console.log("[F10-min-a] saveNow 之后 saveStatus =", store.saveStatus, "dirty =", store.dirty, "saveError =", store.saveError);
    const result = await store.submit();
    await settle();
    const refreshErr = (store as never as { submitRefreshError?: string | null }).submitRefreshError;
    const refreshState = (store as never as { submitRefreshState?: string }).submitRefreshState;
    console.log("[F10-min-a] result =", JSON.stringify(result), "submitStatus =", store.submitStatus,
      "submitError =", store.submitError, "submitRefreshError =", refreshErr, "submitRefreshState =", refreshState,
      "submitBoard 调用 =", JSON.stringify(vi.mocked(interactiveApi.submitBoard).mock.calls));

    expect(vi.mocked(interactiveApi.submitBoard).mock.calls.length, "前置条件：提交请求必须真的发出").toBe(1);
    expect(store.submitStatus, "F10 缺陷复现：POST 已明确成功，提交状态却被后续读取失败改成 failed").not.toBe("failed");
    if (refreshErr !== undefined) {
      expect(String(refreshErr ?? "").length, "F10 缺陷复现：提交成功但刷新失败没有任何独立记录").toBeGreaterThan(0);
    }
  });

  it("F10-min-b POST 被明确拒绝 → 必须 failed，且不得显示成提交成功", async () => {
    const store = useInteractiveStore();
    await store.refreshBoardFromServer();
    if (store.dirty) { await store.saveNow(); await settle(); }
    vi.advanceTimersByTime(0); await settle();

    // 明确拒绝 = 后端给了响应（带 HTTP status），不是「没有回执」
    const rejection = Object.assign(new Error("409 拒绝"), { status: 409 });
    vi.mocked(interactiveApi.submitBoard).mockRejectedValue(rejection);
    const result = await store.submit();
    await settle();
    console.log("[F10-min-b] result =", JSON.stringify(result), "submitStatus =", store.submitStatus, "submitError =", store.submitError);
    expect(store.submitStatus, "F10：明确拒绝（带 status）必须落成 failed").toBe("failed");
    expect(String(store.submitError ?? "").includes("提交成功"), "F10：拒绝不得显示成成功").toBe(false);
  });

  it("F10-min-c POST 没有拿到回执（无 status）→ 结果未知，不得假定成功也不得假定失败", async () => {
    const store = useInteractiveStore();
    await store.refreshBoardFromServer();
    if (store.dirty) { await store.saveNow(); await settle(); }
    vi.advanceTimersByTime(0); await settle();

    vi.mocked(interactiveApi.submitBoard).mockRejectedValue(new TypeError("Failed to fetch"));
    const result = await store.submit();
    await settle();
    const unknown = (store as never as { submitResultUnknown?: boolean }).submitResultUnknown;
    console.log("[F10-min-c] result =", JSON.stringify(result), "submitStatus =", store.submitStatus,
      "submitResultUnknown =", unknown, "submitError =", store.submitError);
    if (unknown !== undefined) {
      expect(unknown, "F10：没有回执必须标记提交结果未知").toBe(true);
      expect(store.submitStatus, "F10：结果未知不得显示成提交成功").not.toBe("accepted");
      expect(String(store.submitError ?? "").includes("未知"), "F10：必须明确说明结果未知").toBe(true);
    } else {
      expect(JSON.stringify(result ?? store.submitError).length, "F10：结果未知必须有明确表述").toBeGreaterThan(0);
    }
  });
});