/**
 * E 路独立核验探针 · F08 / F10 / F11（interactive store，基线 8fa845255c118a7104b1de8e37298ca745924482）
 *
 * 规格来源：D:\qio-dev\_audit-findings-8fa8452.md。
 *
 * F08 保存已确认，旧读取却回退版本：
 *   定位 saveNow 的 newerCandidate 分支、knownServerSeq、refreshBoardFromServer。
 *   验收：seq=3 → 保存 version 1 → 本地 version 2 → 保存确认 seq=4 → 旧 GET seq=3 晚到；
 *   最终保留 seq=4 和 version 2，不产生针对旧正文的假冲突。
 * F10 成功提交被刷新失败改成提交失败：
 *   定位 interactive.submit。POST 已明确成功后 GET 失败应显示「提交成功，刷新失败」，
 *   保留成功事实，提供重试刷新，不自动重复提交。
 * F11 任务清单读取失败被显示成任务消失：
 *   定位 loadIntents。暂时读取失败要保留最后一次已知任务并标明刷新失败；
 *   成功返回空集合与请求失败必须区分。
 *
 * 这些是 store 级反例：用 vi.mock 接管 services/interactive（网络边界），
 * 只断言用户可观察到的 store 状态，不依赖任何内部实现细节。
 *
 * 运行（在**被测 worktree** 的 frontend 目录下）：
 *   $env:QIO_FE_SRC='D:/qio-dev/qa-e/frontend/src'
 *   npx vitest run --root '..' -c 'D:\qio-dev\qa-e\scripts\audit-verify-8fa8452\vitest.config.ts'
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
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(),
    lookupTurnByRequest: vi.fn(),
    getInstance: vi.fn(),
  },
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
      get length() {
        return map.size;
      },
    },
  });
}

function card(id: string, content: string, over: Record<string, unknown> = {}) {
  return {
    id,
    kind: "text" as const,
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-11T00:00:00Z",
    updatedAt: "2026-10-11T00:00:00Z",
    ...over,
  };
}

function state(seq: number, cards: ReturnType<typeof card>[]) {
  return {
    boardId: "board_default",
    seq,
    updatedAt: "2026-10-11T00:00:00Z",
    cards,
    groups: [],
    links: [],
    selection: [],
  };
}

function boardPayload(seq: number, cards: ReturnType<typeof card>[]) {
  return {
    board: { id: "board_default", title: "板面", createdAt: "x", updatedAt: "x" },
    state: state(seq, cards),
    seq,
    savedAt: "2026-10-11T00:00:00Z",
    submissions: [],
  };
}

async function settle(times = 6): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}

beforeEach(() => {
  installLocalStorage();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

describe("F08 已确认的保存版本不能被旧读取回退", () => {
  it("F08-a 保存确认 seq=4 后，迟到的 GET seq=3 不得回退板面/清洁基线，也不得制造假冲突", async () => {
    vi.mocked(interactiveApi.fetchBoardState).mockResolvedValue(boardPayload(3, [card("card:a", "第三版")]) as never);
    const store = useInteractiveStore();
    await store.refreshBoardFromServer();
    expect(store.board?.seq, "前置条件：首读 seq=3").toBe(3);

    // 用户本地继续编辑成 version 2
    store.board = { ...state(3, [card("card:a", "用户本地第 2 版")]) } as never;
    store.dirty = true;

    // 一次保存被服务器确认，返回 seq=4
    vi.mocked(interactiveApi.saveBoardState).mockResolvedValue({
      seq: 4,
      savedAt: "2026-10-11T00:00:10Z",
      state: state(4, [card("card:a", "用户本地第 2 版")]),
      pending: { baselineSeq: 4, expressions: [], updatedAt: "x" },
    } as never);
    await store.saveNow();
    await settle();
    console.log("[F08-a] 保存确认后 board.seq =", store.board?.seq, "cleanSeq =", (store as never as {cleanState?: {seq?: number}}).cleanState?.seq);

    // 旧 GET（seq=3）晚到
    vi.mocked(interactiveApi.fetchBoardState).mockResolvedValue(boardPayload(3, [card("card:a", "第三版")]) as never);
    await store.refreshBoardFromServer();
    await settle();

    const seq = store.board?.seq;
    const content = store.board?.cards?.[0]?.content;
    const conflicts = Object.values(store.conflicts ?? {});
    console.log(
      "[F08-a] 旧 GET 之后 board.seq =", seq,
      "card.a =", content, "conflicts =", JSON.stringify(conflicts),
    );

    expect(seq, "F08 缺陷复现：旧 GET（seq=3）把已确认的 seq=4 回退了").toBe(4);
    expect(content, "F08：更晚的本地候选正文不得被旧读取覆盖").toBe("用户本地第 2 版");
    expect(conflicts, "F08：不得针对旧正文制造假冲突").toEqual([]);
  });
});

/**
 * F10 的独立反例在 f10-min.probe.ts（那里给了最小隔离的「已保存 + 提交成功 + 读取全失败」场景）。
 * 这里保留一条「明确拒绝不得显示成成功」的回归保护。
 */
describe("F10 回归保护", () => {
  it("F10-b 提交被真正拒绝：不得显示成成功", async () => {
    const store = useInteractiveStore();
    store.board = state(3, [card("card:a", "要提交的内容", { checked: true })]) as never;
    vi.mocked(interactiveApi.submitBoard).mockRejectedValue(
      Object.assign(new Error("409 拒绝"), { status: 409 }),
    );
    const result = await store.submit();
    await settle();
    console.log("[F10-b] 拒绝时 result =", JSON.stringify(result));
    expect(JSON.stringify(result).includes("提交成功"), "F10：被拒绝不得显示成提交成功").toBe(false);
  });
});

describe("F11 任务清单读取失败不能被显示成任务消失", () => {
  it("F11-a 已加载到 running 任务后读取失败：必须保留最后一次已知任务并标明刷新失败", async () => {
    const running = [
      { id: "intent_1", title: "待审批任务", status: "running", batchId: "b1", createdAt: "x", updatedAt: "x" },
    ];
    vi.mocked(interactiveApi.fetchIntents).mockResolvedValue({ intents: running, batches: [] } as never);
    const store = useInteractiveStore();
    await store.loadIntents();
    await settle();
    const before = store.intents?.length ?? 0;
    console.log("[F11-a] 首次加载 intents =", before);
    expect(before, "前置条件：先有 1 条 running 任务").toBe(1);

    vi.mocked(interactiveApi.fetchIntents).mockRejectedValue(new TypeError("Failed to fetch"));
    await store.loadIntents();
    await settle();

    const after = store.intents?.length ?? 0;
    const loadState = (store as never as { intentsLoadState?: string }).intentsLoadState;
    const loadErr = (store as never as { intentsLoadError?: string | null }).intentsLoadError;
    const err = loadErr ?? (store.recoverNotice ?? []).join(" | ");
    console.log(
      "[F11-a] 读取失败后 intents =", after,
      "intentsLoadState =", loadState, "intentsLoadError =", loadErr,
      "recoverNotice =", (store.recoverNotice ?? []).join(" | "),
    );
    expect(after, "F11 缺陷复现：读取失败把已知任务清空/显示成任务消失").toBe(1);
    expect(String(err).length, "F11：必须标明刷新失败（要有可读错误）").toBeGreaterThan(0);
  });

  it("F11-b 成功返回空集合必须与请求失败区分开", async () => {
    vi.mocked(interactiveApi.fetchIntents).mockResolvedValue({ intents: [], batches: [] } as never);
    const store = useInteractiveStore();
    await store.loadIntents();
    await settle();
    const loadErrField = (store as never as { intentsLoadError?: string | null }).intentsLoadError;
    const emptyOk = store.intents?.length ?? 0;
    const errAfterSuccess = loadErrField ?? (store.recoverNotice ?? []).join(" | ");
    console.log("[F11-b] 成功空清单 intents =", emptyOk, " intentsLoadError =", loadErrField);

    vi.mocked(interactiveApi.fetchIntents).mockRejectedValue(new TypeError("Failed to fetch"));
    await store.loadIntents();
    await settle();
    const errAfterFail = (store as never as { intentsLoadError?: string | null }).intentsLoadError
      ?? (store.recoverNotice ?? []).join(" | ");
    console.log("[F11-b] 失败后 intentsLoadError =", errAfterFail);
    expect(errAfterSuccess, "F11：成功返回空集合不得留下错误提示").toBeFalsy();
    expect(String(errAfterFail).length, "F11：请求失败必须有错误提示（与空集合可区分）").toBeGreaterThan(0);
  });
});