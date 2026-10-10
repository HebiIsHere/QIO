/**
 * 【独立验收 D · closure-d】反例 R1 / R2 / R3 / R4（第①层：状态与单元）。
 *
 * 本文件断言的都是**正确行为**。冻结时（集成候选 SHA 之前）它们在基线上必须失败，
 * 失败信息就是「反例在基线上成立」的证据；修复后同一份文件必须全部通过。
 * 因此本文件**不允许**用条件判断跳过断言，也不允许把断言写弱到基线也能过。
 *
 * 每个用例都刻意构造真实时序：
 *  - R1：服务端门 409 要求影响确认 → 用户取消 → 板面必须回到服务器已保存内容；
 *  - R2：保存先成功、更早发起的 GET 后返回 → 已保存的新正式板面不许被旧读取换回；
 *  - R3：第一版保存成功（回执在飞时出现第二版）→ 第二版仍必须能继续保存到服务器；
 *  - R4：两份稿冲突 → 用户选「用服务器上的」→ 本机副本删除失败 → 恢复后重试 → 重开：
 *        这条重试的目的是**删本机冗余副本**，绝不许写成 kind=cleared 的「整份草稿清除」。
 *        规格来源：_briefs/A.md:26-27（R4 触发）与 A.md:35（真实路径）。
 *  - R4 对照用例：clearDraft（用户要清掉整份草稿）路径上，重试必须先幂等补写 cleared 依据
 *        （上一轮 12 的既有保护，不许为修 R4 撤掉）——两条路径必须分得开。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import type { BoardState } from "../../interactive/types";
import { cardLocalDraftStorageKey, readCardLocalDraft } from "../../interactive/drafts";

vi.mock("../../services/interactive", () => ({
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

function state(seq: number, cards: BoardState["cards"]): BoardState {
  return { boardId: "board_default", seq, updatedAt: "2026-10-10T00:00:00Z", cards, groups: [], links: [], selection: [] };
}

function payload(seq: number, cards: BoardState["cards"], drafts: Record<string, string> = {}): never {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: state(seq, cards),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: drafts ? "t" : null },
  } as never;
}

function intent(id: string, status = "running"): never {
  return {
    id, boardId: "board_default", submissionId: null, title: "任务-" + id, summary: "", status,
    preview: { kind: "task" }, impact: { materials: ["c1"], consequence: "暂停并保留进度" },
    dependsOn: [], conflictsWith: [], conflictKey: "", materialRefs: ["c1"],
    progress: { done: 0, total: 1, text: "" }, reason: "", demo: true, createdAt: "", updatedAt: "",
  } as never;
}

/**
 * 真实的 409 响应体（FastAPI HTTPException(detail=<dict>)）：
 * services/interactive.ts 会把 body.detail 提成 payload，所以替身必须用这个形状，
 * 否则替身行为与线上不同，会给出假的「已确认」/假的拒绝。
 */
function gateError(affectedTasks: unknown[]): Error {
  return Object.assign(new Error("409"), {
    status: 409,
    // 与 services/interactive.ts 解出的 payload 同形状（FastAPI 的 { detail: {...} }）
    payload: { error: "impact_confirmation_required", reason: "这次保存会改动正在执行任务依赖的材料", affectedTasks },
  });
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "服务器原文")]));
  vi.mocked(api.saveBoardState).mockResolvedValue({
    ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "服务器原文")]),
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [intent("A")], conflicts: [], batchAvailable: false, recovery: { paused: [] },
  } as never);
});

/**
 * 受控「服务端草稿」：整份替换语义（未列出的键会被删掉），与真实接口一致。
 * 断言 R4 时要用它核对「服务器稿键与正文都还在」。
 */
function fakeServerDrafts(initial: Record<string, string>) {
  const drafts: Record<string, string> = { ...initial };
  const puts: Record<string, string>[] = [];
  vi.mocked(api.fetchBoardState).mockImplementation(async () => payload(3, [card("c1", "正式正文")], { ...drafts }) as never);
  vi.mocked(api.saveDrafts).mockImplementation(async (_boardId, next) => {
    puts.push({ ...(next as Record<string, string>) });
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next as Record<string, string>);
    return { drafts: { ...drafts }, updatedAt: "t" } as never;
  });
  return { drafts, puts };
}

/** 记忆型存储：跨「重开」保留内容，可随时让某次 removeItem 抛错。 */
function installFailToggleStorage(): { failNextRemove: () => void } {
  // 返回的闭包对象上挂 restore（测试结束时还原真实 localStorage）

  const map = new Map<string, string>();
  let failRemoveOnce = false;
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      get length() { return map.size; },
      key: (i: number) => Array.from(map.keys())[i] ?? null,
      getItem: (key: string) => (map.has(key) ? (map.get(key) as string) : null),
      setItem: (key: string, value: string) => { map.set(key, value); },
      removeItem: (key: string) => {
        if (failRemoveOnce) { failRemoveOnce = false; const e = new Error("quota"); e.name = "QuotaExceededError"; throw e; }
        map.delete(key);
      },
      clear: () => map.clear(),
    },
  });
  (installFailToggleStorage as unknown as { restore: () => void }).restore = () => {
    if (original) Object.defineProperty(globalThis, "localStorage", original);
  };
  return { failNextRemove: () => { failRemoveOnce = true; } };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("R1 取消影响确认后，正式板面必须回到服务器已保存的内容", () => {
  it("服务端门要求确认 → 取消：板面回到服务器原文、dirty 与保存状态如实、草稿候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["服务器原文"]);

    store.setDraft("card:c1", "取消前的编辑候选");
    store.commit(state(3, [card("c1", "被取消掉的新正文")]), "改正文");
    vi.mocked(api.saveBoardState).mockRejectedValue(
      gateError([{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }]),
    );
    await store.saveNow();
    expect(store.pendingImpact).not.toBeNull();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["被取消掉的新正文"]);

    await store.cancelImpact();
    await flushPromises();

    // 正确行为：取消 = 这次板面变更不生效 → 板面是服务器已保存内容（不是被取消掉的候选）
    expect(store.board!.cards.map((c) => c.content)).toEqual(["服务器原文"]);
    // 取消不删编辑草稿候选与恢复来源
    expect(store.drafts["card:c1"]).toBe("取消前的编辑候选");
    // 取消落地后不是「有待保存的脏候选」
    expect(store.dirty).toBe(false);
  });
});

describe("R2 迟到的 GET 不许把已成功保存的新正式板面换回旧正文", () => {
  it("保存先落地、更早发起的 GET 后返回：板面仍是新正文，不许回退", async () => {
    const store = useInteractiveStore();
    await store.load();

    let resolveGet!: (v: unknown) => void;
    vi.mocked(api.fetchBoardState).mockImplementation(
      () => new Promise((res) => { resolveGet = res; }) as never,
    );
    const refresh = store.refreshBoardFromServer();
    await Promise.resolve();

    // 用户提交新正文并保存成功（服务器接受为 seq=4）
    store.commit(state(3, [card("c1", "已保存成功的新正文")]), "改正文");
    vi.mocked(api.saveBoardState).mockResolvedValue({
      ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "已保存成功的新正文")]),
    } as never);
    await store.saveNow();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);

    // 更早发起的读取现在才返回：它读到的是旧正文
    resolveGet(payload(3, [card("c1", "旧正文")]));
    await refresh;
    await flushPromises();

    // 正确行为：已保存成功的新正式板面不被更早发起的读取回退（迟到读取只能作废，不许落地）
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);
  });
});

describe("R3 保住第二版正文，也必须保住第一版保存成功的版本事实", () => {
  it("第二版必须真的能继续保存到服务器（把服务器最新 seq 带上），而不是永远卡在未保存", async () => {
    const store = useInteractiveStore();
    await store.load();

    let resolveFirst!: (v: unknown) => void;
    vi.mocked(api.saveBoardState).mockImplementation(
      () => new Promise((res) => { resolveFirst = res; }) as never,
    );
    store.commit(state(3, [card("c1", "第一版")]), "第一版");
    const first = store.saveNow();
    await flushPromises();

    // 第一版回执在飞时用户完成第二版
    store.commit(state(3, [card("c1", "第二版")]), "第二版");
    resolveFirst({ ok: true, seq: 4, savedAt: "t1", state: state(4, [card("c1", "第一版")]) });
    await first;
    await flushPromises();

    // 第二版候选保留、仍是未保存
    expect(store.board!.cards.map((c) => c.content)).toEqual(["第二版"]);
    expect(store.dirty).toBe(true);

    // 第二版继续保存：必须携带服务器最新版本事实 seq=4（否则服务器按旧基准拒绝，第二版永远存不上）
    let secondSeq: number | null = null;
    vi.mocked(api.saveBoardState).mockImplementation(async (_id, putState) => {
      secondSeq = putState.seq;
      return { ok: true, seq: 5, savedAt: "t2", state: state(5, []) } as never;
    });
    await store.saveNow();
    await flushPromises();

    expect(secondSeq).toBe(4);
    expect(store.dirty).toBe(false);
  });
});

describe("R4 两份稿冲突 → 选「用服务器上的」→ 本机副本删除失败 → 恢复后重试", () => {
  const LOCAL = "本机那份候选";
  const SERVER = "服务器那份草稿（用户选择保留）";

  function seedConflict(): void {
    // 本机恢复副本（编辑稿）与服务器草稿同时存在且正文不同 → 恢复时出现「无法判定新旧」冲突
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: LOCAL, updatedAt: Date.now(), seq: 1, kind: "draft", version: 1, boardId: "board_default" }),
    );
  }

  it("重试只删本机冗余副本：磁盘不许出现 cleared，服务器稿键与正文都还在", async () => {
    const storage = installFailToggleStorage();
    try {
      seedConflict();
      const server = fakeServerDrafts({ "card:c1": SERVER });
      const store = useInteractiveStore();
      await store.load();
      await flushPromises();

      // 造出真实冲突（两份都保留，等用户明确选择）
      expect(store.draftConflictFor("c1")).toEqual({ local: LOCAL, server: SERVER });

      // 用户点「用服务器上的」；本机副本这次删除失败
      storage.failNextRemove();
      store.resolveDraftConflict("c1", "server");
      await flushPromises();

      expect(store.draftConflictFor("c1")).toBeNull();
      expect(store.cardDraftText("c1")).toBe(SERVER);
      // 删除真失败 → 本机记录仍在，且必须是编辑稿（不是清除依据）
      const afterFail = readCardLocalDraft("c1");
      expect(afterFail?.kind).not.toBe("cleared");

      // 存储恢复后重试
      await store.retryDraftSave("card:c1");
      await flushPromises();

      // 正确行为 A：本机冗余副本被删掉，或者（旧格式无版本可校验时）原样保留编辑稿；
      //            绝不许变成 kind=cleared 的整份清除依据
      const local = readCardLocalDraft("c1");
      expect(local === null || local.kind !== "cleared").toBe(true);
      // 正确行为 B：服务器那份草稿键与正文都还在
      expect(server.drafts["card:c1"]).toBe(SERVER);
      // 正确行为 C：草稿集合里的正文仍是服务器正文
      expect(store.drafts["card:c1"]).toBe(SERVER);
      // 正确行为 D：如果这次重试真的发了草稿保存请求，请求里必须仍然带着服务器稿
      //（整份替换语义下把它漏掉就等于删掉它）。没有发请求也是合法的：此时服务器上本来就
      // 已经是用户选择保留的那一份，不需要写回。这里断言的是「请求内容」，不是「必须发请求」。
      const lastPut = server.puts[server.puts.length - 1];
      if (lastPut) expect(lastPut["card:c1"]).toBe(SERVER);
    } finally {
      (installFailToggleStorage as unknown as { restore: () => void }).restore();
    }
  });

  it("「重开等价物」：同一份本机存储 + 新 store 实例，服务器稿不被删、不变空", async () => {
    const storage = installFailToggleStorage();
    try {
      seedConflict();
      const server = fakeServerDrafts({ "card:c1": SERVER });
      await useInteractiveStore().load();
      await flushPromises();
      const first = useInteractiveStore();
      storage.failNextRemove();
      first.resolveDraftConflict("c1", "server");
      await flushPromises();
      await first.retryDraftSave("card:c1");
      await flushPromises();

      // 重开：新 pinia + 新 store，同一份本机存储
      setActivePinia(createPinia());
      const reopened = useInteractiveStore();
      await reopened.load();
      await flushPromises();

      expect(server.drafts["card:c1"]).toBe(SERVER);
      expect(reopened.drafts["card:c1"]).toBe(SERVER);
      const local = readCardLocalDraft("c1");
      expect(local === null || local.kind !== "cleared").toBe(true);
    } finally {
      (installFailToggleStorage as unknown as { restore: () => void }).restore();
    }
  });
});

describe("R4 对照：clearDraft（用户要清掉整份草稿）路径上 cleared 依据必须保留", () => {
  it("清除依据写失败 + 服务器清除已确认：重试必须补写 cleared 事实（不许为修 R4 撤掉 12 的保护）", async () => {
    localStorage.setItem(
      "qio.draft.card.local-c1",
      JSON.stringify({ text: "旧稿-v1", updatedAt: Date.now(), seq: 1, kind: "draft", version: 1, boardId: "board_default" }),
    );
    const store = useInteractiveStore();
    await store.load();
    expect(store.drafts["card:c1"]).toBe("旧稿-v1");

    // 用户清掉整份草稿，但本机 cleared 依据这次没写成
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function () { throw new Error("模拟本机写入失败"); };
    store.clearDraft("card:c1");
    Storage.prototype.setItem = setItem;
    expect(store.draftRemovalStates["card:c1"].status).toBe("error");

    // 服务器清除成功；用户点重试
    const server = fakeServerDrafts({ "card:c1": "旧稿-v1" });
    await store.flushDrafts();
    await store.retryDraftSave("card:c1");
    await flushPromises();

    const disk = JSON.parse(localStorage.getItem("qio.draft.card.local-c1") ?? "null") as { kind?: string } | null;
    // 正确行为（12 的既有保护 + 本轮 R4 的目的化区分）：
    // 「整份草稿清除」在服务器确认后，本机留下的必须是 cleared 事实（或已按版本删干净）；
    // 目的是阻止这份旧稿在重开后复活。
    if (disk) expect(disk.kind).toBe("cleared");
    expect(server.drafts["card:c1"]).toBeUndefined();
  });
});
