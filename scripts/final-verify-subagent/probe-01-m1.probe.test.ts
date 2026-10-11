/**
 * 独立验收（E）反例 01 / M1：草稿「GET 在飞期间 PUT 先成功」的乱序回执。
 *
 * 与仓库内 final-lead-m1.test.ts 的差别（为什么另写一个）：
 * 仓库用例里迟到的 GET 返回的服务器草稿集**不含**该键，于是
 * mergeDraftsFromPayload 的 `!hasOwnProperty(serverDrafts, key)` 分支就会保住本机正文 ——
 * 即使 M1 新增的 `savedNow > readSaved` 条件被删掉，那个用例仍然通过。
 * 本探针把条件逼到唯一：
 *   1) GET 载荷里**带着**该键的旧正文（hasOwnProperty 为真）；
 *   2) GET 开始后用户没有新输入（memoryIsNewer 为假）；
 *   3) 只有「GET 在飞期间草稿 PUT 成功（saved 越过读取基线）」这一条能保住新正文。
 * 因此破坏 store 的 savedNow > readSaved 条件时，本用例必须变红（见同目录 mutation 记录）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../frontend/src/stores/interactive";
import * as api from "../../frontend/src/services/interactive";
import type { BoardState } from "../../frontend/src/interactive/types";

vi.mock("../../frontend/src/services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

function card(id: string, content: string): BoardState["cards"][number] {
  return {
    id, kind: "text", content, meta: {}, x: 0, y: 0, w: 1, h: 1,
    checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
    createdAt: "", updatedAt: "",
  };
}

function boardPayload(
  seq = 3,
  drafts: Record<string, string> = {},
  cards: BoardState["cards"] = [],
): Awaited<ReturnType<typeof api.fetchBoardState>> {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: { boardId: "board_default", seq, updatedAt: "2026-10-09T10:00:00Z", cards, groups: [], links: [], selection: [] },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: "2026-10-09T10:00:00Z" },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3));
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t", state: boardPayload(4).state } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("E-01/M1 迟到 GET 不得回退「GET 在飞期间已保存成功」的新草稿", () => {
  it("尖锐用例：服务器旧正文与本地新正文同时存在，唯一保命条件是 savedNow > readSaved", async () => {
    const store = useInteractiveStore();
    // 首次加载：服务器上已有这份草稿的**旧**正文。
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(3, { "card:c1": "服务器旧正文" }, [card("c1", "")]),
    );
    await store.load();
    expect(store.drafts["card:c1"], "初始应取服务器正文").toBe("服务器旧正文");

    // 用户在本次 GET 之前就把正文改成新稿（本机还没保存成功）。
    store.setDraft("card:c1", "新稿-v2-GET前输入");

    // 第二次 GET 在飞期间：草稿 PUT 先成功（saved 越过读取基线），随后 GET 才返回旧正文。
    let putLandedDuringGet = false;
    vi.mocked(api.fetchBoardState).mockImplementation((async () => {
      await store.flushDrafts();
      putLandedDuringGet = vi.mocked(api.saveDrafts).mock.calls.length > 0;
      // 迟到的旧读取：同一个键，旧正文（hasOwnProperty 分支救不了本机）
      return boardPayload(3, { "card:c1": "服务器旧正文" }, [card("c1", "")]);
    }) as never);

    await store.refreshBoardFromServer();
    await flushPromises();

    expect(putLandedDuringGet, "PUT 必须真的在 GET 在飞期间成功过").toBe(true);
    const sent = vi.mocked(api.saveDrafts).mock.calls.at(-1)?.[1] as Record<string, string>;
    expect(sent["card:c1"], "PUT 载荷必须是新正文").toBe("新稿-v2-GET前输入");
    expect(store.drafts["card:c1"], "迟到 GET 不得把已保存的新正文回退成服务器旧正文").toBe("新稿-v2-GET前输入");
    expect(store.draftStateFor("card:c1").status, "已保存成功的事实不许被迟到读取改写成旧状态").toBe("saved");
  });

  it("对照组：GET 在飞期间用户又输入（memoryIsNewer 路径）时同样不回退", async () => {
    const store = useInteractiveStore();
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(3, { "card:c1": "服务器旧正文" }, [card("c1", "")]),
    );
    await store.load();
    store.setDraft("card:c1", "第一版");
    vi.mocked(api.fetchBoardState).mockImplementation((async () => {
      // GET 在飞期间用户又输入了第二版（内存更新）
      store.setDraft("card:c1", "第二版-在飞期间输入");
      return boardPayload(3, { "card:c1": "服务器旧正文" }, [card("c1", "")]);
    }) as never);
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.drafts["card:c1"]).toBe("第二版-在飞期间输入");
  });
});
