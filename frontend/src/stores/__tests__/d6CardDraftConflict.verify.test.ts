/**
 * 独立验收 D（第六轮）：草稿冲突的选择与清除依据（契约 §12.1 / §12.2）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。对应验收条文：
 * - §12.1 反例 1 剩余口径：未选择冲突 → 编辑别的卡片、保存、刷新后冲突仍在、两份都可处理
 *   （基线已含主智能体 §12.1 首步修复；选择两种版本、旧格式无法判定、在飞新输入是本轮重点）。
 * - §12.1 反例 2【组件】：未选择 → 仅打开 A 编辑器 —— 冲突提示不许消失、本机版本不许被自动写上服务器。
 * - §12.2 反例 3：清除 → 新输入 → 本地写失败 → 重读板面 —— 旧清除记录不许因本机写失败重新取得决定权。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【组件】挂载真实组件；【模拟失败】受控拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { cardLocalDraftStorageKey, writeDraft } from "../../interactive/drafts";
import BoardCard from "../../components/interactive/BoardCard.vue";
import type { BoardState, BoardStateResponse } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
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

function boardState(content = "正式正文"): BoardState {
  return {
    boardId: "board_default",
    cards: [
      {
        id: "cA", kind: "text", x: 40, y: 40, w: 240, h: 120, content: content,
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-09T00:00:00.000Z", updatedAt: "2026-10-09T00:00:00.000Z",
      },
      {
        id: "cB", kind: "text", x: 300, y: 40, w: 240, h: 120, content: "B 卡正文",
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-09T00:00:00.000Z", updatedAt: "2026-10-09T00:00:00.000Z",
      },
    ],
    groups: [], links: [], selection: [], updatedAt: "2026-10-09T00:00:00.000Z",
  } as unknown as BoardState;
}

function response(drafts: Record<string, string>, updatedAt: string | null): BoardStateResponse {
  return {
    board: { id: "board_default", title: "板面" },
    state: boardState(),
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: drafts, updatedAt: updatedAt },
  } as unknown as BoardStateResponse;
}

/** 受控服务器：草稿整份替换保存（未列出的键被删掉），与真实接口语义一致。 */
function fakeServer(initial: Record<string, string>, updatedAt: string | null) {
  const drafts: Record<string, string> = { ...initial };
  const view = { drafts: drafts };
  vi.mocked(imApi.fetchBoardState).mockImplementation(async () => response({ ...drafts }, updatedAt));
  vi.mocked(imApi.saveDrafts).mockImplementation(async (_boardId: string, next: Record<string, string>) => {
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next);
    return { drafts: { ...drafts }, updatedAt: new Date().toISOString() };
  });
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  return view;
}

/** 可控的本机存储：可以让「卡片本机恢复副本」这一路写入失败（其余照常）。 */
const storageReal = globalThis.localStorage as Storage;
function installFailingStorage(cardId: string) {
  const real = storageReal;
  const target = cardLocalDraftStorageKey(cardId);
  let failing = false;
  const stub = {
    clear: () => real.clear(),
    getItem: (key: string) => real.getItem(key),
    setItem: (key: string, value: string) => {
      if (failing && key === target) {
        const err = new Error("本机存储已满，草稿没有保存成功（清理一些空间后可以重试）");
        (err as { name?: string }).name = "QuotaExceededError";
        throw err;
      }
      real.setItem(key, value);
    },
    removeItem: (key: string) => real.removeItem(key),
    get length(): number {
      return real.length;
    },
    key: (index: number) => real.key(index),
  };
  (globalThis as unknown as { localStorage: unknown }).localStorage = stub;
  return {
    arm: () => {
      failing = true;
    },
    disarm: () => {
      failing = false;
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  // 还原真实存储：上一条用例换上的「会失败的本机存储」不许污染后续用例
  (globalThis as unknown as { localStorage: unknown }).localStorage = storageReal;
  localStorage.clear();
});

describe("§12.2 反例 3：清除 → 新输入 → 本地写失败 → 重读板面", () => {
  it("【状态+模拟失败】旧清除记录不许因本机写失败重新取得决定权，新输入不能变空", async () => {
    vi.useFakeTimers();
    fakeServer({ "card:cA": "服务器上的旧草稿" }, "2026-10-09T00:00:00.000Z");
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    // 用户正常输入（本机副本写成功），随后把草稿清掉（清除依据也写成功）
    const failing = installFailingStorage("cA");
    store.setDraft("card:cA", "清除前的正常输入");
    await flushPromises();
    store.clearDraft("card:cA");
    await flushPromises();
    const clearedRecord = localStorage.getItem(cardLocalDraftStorageKey("cA"));
    expect(clearedRecord, "清除依据应当写进本机存储").toBeTruthy();
    expect(JSON.parse(clearedRecord as string).kind, "清除依据应当是 cleared 记录").toBe("cleared");

    // 之后用户又输入了新文字，但这一次本机写入失败
    failing.arm();
    store.setDraft("card:cA", "清除后的新输入");
    await flushPromises();
    expect(store.draftFor("card:cA"), "内存里的新输入先要真的保留").toBe("清除后的新输入");
    expect(store.draftLocalStates["card:cA"]?.ok, "本机写失败必须留下失败状态（不许假装存过）").toBe(false);

    // 防抖还没有到（没有服务器写、也没有清除同步），用户重读板面
    await store.refreshBoardFromServer();
    await flushPromises();

    expect(
      store.cardDraftText("cA"),
      "重读之后新输入变空了：旧的本机清除记录（写失败导致它还是唯一存档）重新取得了决定权（§12.2）",
    ).toBe("清除后的新输入");
    expect(
      store.draftLocalStates["card:cA"]?.ok,
      "本机写失败的失败状态不许在重读后被静默洗掉",
    ).toBe(false);
    wrapperlessHint(store);
  });

  function wrapperlessHint(store: ReturnType<typeof useInteractiveStore>) {
    // 附加口径：重读期间不许把这条失败悄悄升级成清除动作并重发（旧清除不许作用于新输入之后）
    expect(
      store.draftRemovalStates["card:cA"]?.status,
      "清除依据被旧记录重新拉起（pending）又作用于新输入之后的键（§12.2）",
    ).not.toBe("pending");
  }
});

describe("§12.1 反例 2：冲突未选择 → 仅打开 A 编辑器", () => {
  it("【组件】打开编辑器不许悄悄替用户选本机版本：冲突提示仍在、服务器版本不改", async () => {
    vi.useFakeTimers();
    const server = fakeServer({ "card:cA": "服务器上的版本" }, "2026-10-09T00:00:00.000Z");
    // 旧格式本机记录（没有版本号，无法判定与服务器谁新）→ 恢复时登记冲突
    writeDraft(cardLocalDraftStorageKey("cA"), "本机上的版本", 0);

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("cA"), "装载后应当已登记 A 卡冲突").not.toBeNull();

    const card = {
      id: "cA", kind: "text" as const, content: "正式正文", checked: false, hidden: false,
      folded: false, bookmarked: false, deleted: false, x: 0, y: 0, w: 200, h: 100,
      createdAt: "2026-10-09T00:00:00.000Z", updatedAt: "2026-10-09T00:00:00.000Z", meta: {},
    };
    store.board = {
      boardId: "board_default", seq: 0, updatedAt: "2026-10-09T00:00:00.000Z",
      cards: [card], groups: [], links: [], selection: [card.id],
    } as never;

    const wrapper = mount(BoardCard, {
      props: {
        card, selected: true, highlight: false, dragging: false, x: 0, y: 0,
        groupName: null, groups: [], toolbarLeft: 0, toolbarTop: 120, multi: false, connecting: false,
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-im="card-edit"]').exists(), "未选中时应显示编辑入口").toBe(true);

    // 用户只是打开编辑器（没有点任何一套「用本机的 / 用服务器上的」）
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();

    expect(
      wrapper.find('[data-im="card-draft-conflict"]').exists(),
      "仅打开编辑器就把冲突提示弄没了（§12.1：打开编辑器不等于选择本机版本）",
    ).toBe(true);
    expect(store.draftConflictFor("cA"), "store 里的冲突被打开动作清掉了").not.toBeNull();

    // 等防抖到点：这一动作里绝不能把本机版本自动写上服务器（未决冲突键不许写上去）
    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    for (const call of vi.mocked(imApi.saveDrafts).mock.calls) {
      expect(
        (call[1] as Record<string, string>)["card:cA"],
        "仅打开编辑器就把本机版本保存到服务器（整份替换会覆盖服务器那份）",
      ).toBeUndefined();
    }
    expect(server.drafts["card:cA"], "服务器版本被打开动作改掉了").toBe("服务器上的版本");
    wrapper.unmount();
  });
});

describe("§12.1 反例 1 剩余口径（基线已含首步修复，如实复核）", () => {
  it("【状态】未选择 → 编辑 B、保存 → 刷新：A 的冲突仍在，两份都可处理", async () => {
    vi.useFakeTimers();
    fakeServer({ "card:cA": "服务器 A 版", "card:cB": "服务器 B 版" }, "2026-10-09T00:00:00.000Z");
    writeDraft(cardLocalDraftStorageKey("cA"), "本机 A 版", 0);

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("cA"), "装载后应登记 A 卡冲突").not.toBeNull();

    // 用户编辑另一张卡片 B（这不能解决 A 的冲突），保存成功
    store.setDraft("card:cB", "对 B 的新编辑");
    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    await store.refreshBoardFromServer();
    await flushPromises();

    const conflict = store.draftConflictFor("cA");
    expect(conflict, "编辑另一张卡片就把 A 的冲突解决了（§12.1：编辑别的卡片不等于解决本卡片冲突）").not.toBeNull();
    expect(conflict?.local ?? "", "选择前不许丢掉本机那份").toBe("本机 A 版");
    expect(conflict?.server ?? "", "选择前不许丢掉服务器那份").toBe("服务器 A 版");
  });

  it("【状态】分别选择两种版本后，保存与恢复符合选择", async () => {
    vi.useFakeTimers();
    // 场景一：选服务器上的
    fakeServer({ "card:cA": "服务器 A 版" }, "2026-10-09T00:00:00.000Z");
    writeDraft(cardLocalDraftStorageKey("cA"), "本机 A 版", 0);
    let store = useInteractiveStore();
    await store.load();
    await flushPromises();
    store.resolveDraftConflict("cA", "server");
    await flushPromises();
    expect(store.draftConflictFor("cA"), "选择后冲突应解除").toBeNull();
    expect(store.draftFor("card:cA"), "选服务器版后编辑框内容应跟随服务器版本").toBe("服务器 A 版");
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.draftConflictFor("cA"), "选服务器版刷新后不应再有冲突").toBeNull();

    // 场景二：选本机的（新开一份状态）
    setActivePinia(createPinia());
    fakeServer({ "card:cA": "服务器 A 版" }, "2026-10-09T00:00:00.000Z");
    writeDraft(cardLocalDraftStorageKey("cA"), "本机 A 版", 0);
    store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("cA")).not.toBeNull();
    store.resolveDraftConflict("cA", "local");
    await flushPromises();
    expect(store.draftConflictFor("cA"), "选择后冲突应解除").toBeNull();
    expect(store.draftFor("card:cA"), "选本机版后编辑框应保留本机文字").toBe("本机 A 版");
    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    const payloads = vi.mocked(imApi.saveDrafts).mock.calls.map((call) => call[1] as Record<string, string>);
    expect(
      payloads.some((payload) => payload["card:cA"] === "本机 A 版"),
      "选本机版后应按该选择把本机版本保存（§12.1：用户明确选了才保存）",
    ).toBe(true);
  });

  it("【状态】旧格式无法判定新旧时登记冲突，不许直接覆盖", async () => {
    vi.useFakeTimers();
    fakeServer({ "card:cA": "服务器 A 版" }, "2026-10-09T00:00:00.000Z");
    writeDraft(cardLocalDraftStorageKey("cA"), "本机 A 版", 0);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    const conflict = store.draftConflictFor("cA");
    expect(conflict, "无法判定时应登记冲突（不许静默覆盖任何一份）").not.toBeNull();
    expect(conflict?.local).toBe("本机 A 版");
    expect(conflict?.server).toBe("服务器 A 版");
    expect(
      store.board?.cards?.find((card) => card.id === "cA")?.content,
      "在用户选择之前，正式卡片内容不许被改写",
    ).toBe("正式正文");
  });

  it("【状态】请求在飞期间的新输入不被旧的清理动作误删", async () => {
    vi.useFakeTimers();
    fakeServer({}, null);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    // 一次保存请求挂起在飞，期间用户清掉草稿又写下新输入
    let release!: (value: unknown) => void;
    vi.mocked(imApi.saveDrafts).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    store.setDraft("card:cA", "第一版");
    await vi.advanceTimersByTimeAsync(700);
    await flushPromises();
    expect(vi.mocked(imApi.saveDrafts).mock.calls.length, "防抖到点应已发出保存请求").toBeGreaterThan(0);

    store.clearDraft("card:cA");
    store.setDraft("card:cA", "在飞期间的新输入");
    release({ drafts: {} });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(700);
    await flushPromises();

    expect(
      store.draftFor("card:cA"),
      "在飞请求结束后，用户在请求期间写下的新输入被误删了",
    ).toBe("在飞期间的新输入");
  });
});
