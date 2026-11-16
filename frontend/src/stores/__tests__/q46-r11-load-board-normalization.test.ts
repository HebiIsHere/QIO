/**
 * v0.3.3 CI 回归守卫：**首次加载的默认板面允许服务端规范化 id**。
 *
 * 反例（0.3.3 CI frontend 5 个文件失败中的 11 条断言）：
 * load() 在 refreshBoardFromServer 之后用冻结的 contextBoardId（board_default）
 * 判上下文；服务端把板面规范化为 board_t 后 contextIsValid() 判成切板提前返回，
 * loadIntents(board_default) 也因 boardId 已变成 board_t 而丢弃回执 ——
 * intents 永远停在 loading、fetchIntentViews 一次都不发，首屏任务/恢复/批量入口全丢。
 *
 * 本用例只锁 store 的公开事实（不依赖页面渲染）：
 * 首次 load 之后必须用**服务端规范化后的 id** 读取可见范围 / 任务清单 / 意图视图，
 * 且清单读取成功落地（不是 loading）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchBoards: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  fetchIntentViews: vi.fn(),
  fetchExecutions: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  saveIntentDisplay: vi.fn(),
}));

function boardPayload() {
  return {
    board: { id: "board_t", title: "规范化板面" },
    state: {
      boardId: "board_t",
      seq: 4,
      updatedAt: "2026-11-16T00:00:00Z",
      cards: [],
      groups: [],
      links: [],
      selection: [],
    },
    seq: 4,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} },
  } as never;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload());
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({
    available: true,
    source: "service",
    intents: [],
  } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.saveIntentDisplay).mockResolvedValue({ ok: true } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("首次加载：服务端规范化默认板面 id", () => {
  it("用规范化后的 board_t 读取可见范围/任务清单/意图视图，清单落地成功", async () => {
    const store = useInteractiveStore();
    vi.mocked(api.fetchIntents).mockResolvedValueOnce({
      intents: [
        {
          id: "i1",
          boardId: "board_t",
          submissionId: null,
          title: "首屏任务",
          summary: "",
          status: "pending",
          preview: { cards: [], groups: [], links: [] },
          impact: { objects: [], tasks: [], consequences: [] },
          dependsOn: [],
          conflictsWith: [],
          conflictKey: "",
          materialRefs: [],
          progress: { done: 0, total: 1, text: "" },
          reason: "",
          demo: false,
          createdAt: "2026-11-16T00:00:00Z",
          updatedAt: "2026-11-16T00:00:00Z",
        },
      ],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);

    await store.load();

    expect(store.boardId, "加载完成后板面身份应是服务端规范化 id").toBe("board_t");
    expect(api.fetchVisibleRange, "可见范围必须按规范化 id 读取").toHaveBeenCalledWith("board_t");
    expect(api.fetchIntents, "任务清单必须按规范化 id 读取").toHaveBeenCalledWith("board_t");
    expect(api.fetchIntentViews, "意图视图必须被读取一次").toHaveBeenCalledTimes(1);
    expect(api.fetchIntentViews).toHaveBeenCalledWith("board_t");
    expect(store.intents.map((item) => item.id), "首屏任务不能被丢掉").toEqual(["i1"]);
    expect(store.intentsLoadState, "清单读取必须落地成功（不能停在 loading）").toBe("ok");
    expect(store.intentViewsLoadState, "意图视图读取必须落地").toBe("ok");
  });
});
