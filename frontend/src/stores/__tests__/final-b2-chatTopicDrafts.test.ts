/**
 * 收尾轮 B2 批 2：话题导航与首次话题迁移的草稿保护（契约 M2/M3，反例 10、11a/b/c）。
 *
 * 正确行为期望：
 * - 10：话题 A 有旧稿，新输入的本机写入失败 —— 切到 B 再回 A 时，那份**尚未持久化成功的内存候选**
 *       与失败状态都要在 A 看到；恢复写入能力后只重试保存（不发送）。
 * - 11(a)：未绑定新输入与 A 既有草稿同时存在 —— 两份都保留（既有那份进「被让位草稿」并持久化）、
 *       输入框里是当前正在编辑的那份、不拼接、不自动发送、不静默覆盖。
 * - 11(b)：迁移写入 A 失败时，原先已保存的未绑定副本继续存在；目标确实保存成功后才按版本清理源。
 * - 11(c)：本机恢复出来、与本次发送无关的未绑定失败记录，绑定时不被错误吸收。
 *
 * 标注：【状态】只依赖 stores/session.ts 的公开状态与动作；【模拟】受控本机写入结果替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { draftStorageKey, readDraft, UNBOUND_DRAFT_ID, writeDraft, removeDraft } from "../../interactive/drafts";
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
  return {
    ...actual,
    writeDraft: vi.fn(actual.writeDraft),
    removeDraft: vi.fn(actual.removeDraft),
  };
});

function chatKey(topicId: string | null): string {
  return draftStorageKey("chat", topicId || UNBOUND_DRAFT_ID);
}

function storedText(key: string): string | null {
  return readDraft(key)?.text ?? null;
}

/** 本机写入失败（模拟存储被禁用 / 配额满） */
function failWrites(error = "本机存储已满"): void {
  vi.mocked(writeDraft).mockImplementation(() => ({ ok: false, error }));
}

let real: typeof import("../../interactive/drafts");

beforeEach(async () => {
  real = await vi.importActual<typeof import("../../interactive/drafts")>("../../interactive/drafts");
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(writeDraft).mockImplementation(real.writeDraft);
  vi.mocked(removeDraft).mockImplementation(real.removeDraft);
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("10 聊天保存失败：切话题后新稿不许被旧稿替换", () => {
  it("【状态】A 的新稿写本机失败：切 B 再回 A 仍在，且失败状态可见；重试只保存不发送", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 里原来的旧稿";
    session.flushDraft();
    expect(storedText(chatKey("A")), "A 的旧稿没有存好（这是本反例的前提）").toBe("A 里原来的旧稿");

    // 新输入的本机写入失败：内存里是「写不进本机的新稿」，磁盘上仍是旧稿
    failWrites();
    session.draft = "写不进本机的新稿";
    session.flushDraft();
    expect(session.draftSaveStatus, "写入失败必须如实显示").toBe("error");
    expect(storedText(chatKey("A")), "写失败却已经把旧稿覆盖掉了").toBe("A 里原来的旧稿");

    // 切到 B，再切回 A
    session.currentTopicId = "B";
    session.currentTopicId = "A";

    expect(
      session.draft,
      "回到 A 时新稿被磁盘上的旧稿替换了（未持久化成功的内存候选被丢掉）",
    ).toBe("写不进本机的新稿");
    expect(session.draftSaveStatus, "回到 A 时看不到这次的保存失败").toBe("error");
    expect(session.draftSaveError ?? "").toContain("本机存储已满");

    // 存储恢复：只能重试保存，不发送
    const sendsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    vi.mocked(writeDraft).mockImplementation(real.writeDraft);
    session.retryDraftSave();
    expect(session.draftSaveStatus, "重试保存后仍显示失败").toBe("saved");
    expect(storedText(chatKey("A")), "重试没有把新稿写进本机").toBe("写不进本机的新稿");
    expect(
      vi.mocked(api.sendTurn).mock.calls.length - sendsBefore,
      "重试保存不是发送：不许打出发送请求",
    ).toBe(0);
    expect(session.draft, "重试保存不该改动输入框内容").toBe("写不进本机的新稿");
  });
});

describe("11(a) 首次话题迁移：未绑定新输入与 A 既有草稿两份都保留", () => {
  it("【状态】当前编辑的是新输入，既有那份进「被让位草稿」；不拼接、不自动发送、不静默覆盖", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    expect(session.currentTopicId).toBeNull();
    // A 里已经有一份存好的草稿（既有事实）
    real.writeDraft(chatKey("A"), "A 里原有的草稿", 3);

    session.draft = "未绑定位置的新输入";
    session.flushDraft();
    expect(storedText(chatKey(null))).toBe("未绑定位置的新输入");

    session.currentTopicId = "A";

    expect(session.draft, "当前正在编辑的应当是新输入").toBe("未绑定位置的新输入");
    expect(session.draft.includes("A 里原有的草稿"), "两份正文被拼接了").toBe(false);
    expect(
      session.chatDraftDisplaced.map((record) => record.text),
      "A 既有的那份草稿被静默覆盖/丢弃了（两份必须都保留）",
    ).toEqual(["A 里原有的草稿"]);
    expect(storedText(chatKey("A")), "当前编辑的这份没有落在 A 的草稿键上").toBe("未绑定位置的新输入");
    expect(vi.mocked(api.sendTurn).mock.calls.length, "迁移不许自动发送").toBe(0);
  });

  it("【状态】被让位的那份可以取回/放弃：取回时与当前输入互换，两份仍都保留", () => {
    const session = useSessionStore();
    real.writeDraft(chatKey("A"), "A 里原有的草稿", 3);
    session.draft = "未绑定位置的新输入";
    session.flushDraft();
    session.currentTopicId = "A";

    const displaced = session.chatDraftDisplaced[0];
    expect(displaced, "没有登记被让位的草稿").toBeTruthy();
    const restored = session.restoreDisplacedDraft(displaced.id);
    expect(restored.ok, "取回被让位的草稿失败").toBe(true);
    expect(session.draft, "取回后输入框里应当是被让位的那份").toBe("A 里原有的草稿");
    expect(
      session.chatDraftDisplaced.map((record) => record.text),
      "取回时把当前编辑的那份弄丢了（互换语义：两份都保留）",
    ).toEqual(["未绑定位置的新输入"]);
    expect(vi.mocked(api.sendTurn).mock.calls.length, "取回不是发送").toBe(0);

    session.discardDisplacedDraft(session.chatDraftDisplaced[0].id);
    expect(session.chatDraftDisplaced).toEqual([]);
    expect(session.draft, "放弃被让位的那份不该改动输入框").toBe("A 里原有的草稿");
    expect(vi.mocked(api.sendTurn).mock.calls.length, "放弃不是发送").toBe(0);
  });

  it("【状态】关闭重开后两份都还在", () => {
    vi.useFakeTimers();
    let session = useSessionStore();
    real.writeDraft(chatKey("A"), "A 里原有的草稿", 3);
    session.draft = "未绑定位置的新输入";
    session.flushDraft();
    session.currentTopicId = "A";
    expect(session.chatDraftDisplaced.length).toBe(1);

    // 关闭重开（新 pinia，同一份本机存储）
    setActivePinia(createPinia());
    session = useSessionStore();
    session.currentTopicId = "A";
    expect(
      session.chatDraftDisplaced.map((record) => record.text),
      "重开后「被让位」的那份草稿不见了",
    ).toEqual(["A 里原有的草稿"]);
    expect(session.draft, "重开后当前编辑的那份不见了").toBe("未绑定位置的新输入");
  });
});

describe("11(b) 迁移写入失败：源副本继续存在，目标保存成功后才按版本清理源", () => {
  it("【状态】写 A 失败时未绑定副本不删；恢复后重试保存才写目标并清理源（不发送）", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    expect(session.currentTopicId).toBeNull();
    session.draft = "未绑定位置已保存的稿";
    session.flushDraft();
    expect(storedText(chatKey(null))).toBe("未绑定位置已保存的稿");

    failWrites();
    session.currentTopicId = "A";

    expect(storedText(chatKey("A")), "写 A 失败却已经在 A 留了记录").toBeNull();
    expect(
      storedText(chatKey(null)),
      "迁移写入失败时把原先已保存的未绑定副本删掉了（双输）",
    ).toBe("未绑定位置已保存的稿");
    expect(session.draftSaveStatus, "迁移保存失败必须如实显示").toBe("error");

    // 存储恢复：重试保存 → 目标写成功，源才按版本清理
    const sendsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    vi.mocked(writeDraft).mockImplementation(real.writeDraft);
    session.retryDraftSave();
    expect(storedText(chatKey("A")), "重试没有把这份草稿写进真实话题").toBe("未绑定位置已保存的稿");
    expect(
      storedText(chatKey(null)),
      "目标已保存成功后，未绑定位置的旧副本没有被清理（换个话题还会重复出现）",
    ).toBeNull();
    expect(
      vi.mocked(api.sendTurn).mock.calls.length - sendsBefore,
      "重试保存不是发送",
    ).toBe(0);
  });
});

describe("11(c) 与本次发送无关的未绑定失败记录不许被吸收", () => {
  it("【状态】本机恢复出来的历史未绑定失败：绑定 A 时不搬进 A", async () => {
    // 第一次会话：未绑定话题下发一条失败（本机已持久化）
    const first = useSessionStore();
    first.draft = "上次会话没发出去的话";
    first.flushDraft();
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    first.sendAttribution();
    first.draft = "";
    await first.send("上次会话没发出去的话");
    expect(first.failedSendsForTopic(null).map((r) => r.text)).toEqual(["上次会话没发出去的话"]);

    // 关闭重开：这份记录只来自本机，与本次会话的发送无关
    setActivePinia(createPinia());
    const session = useSessionStore();
    expect(session.failedSendsForTopic(null).map((r) => r.text)).toEqual(["上次会话没发出去的话"]);

    // 本次会话绑定真实话题 A
    session.currentTopicId = "A";
    expect(
      session.failedSendsForTopic("A"),
      "与本次发送无关的历史未绑定失败记录被无条件搬进了 A",
    ).toEqual([]);
    expect(
      session.failedSendsForTopic(null).map((r) => r.text),
      "记录应当留在未绑定位置，等用户自己处理",
    ).toEqual(["上次会话没发出去的话"]);
  });

  it("【状态】本次未绑定失败（有本机发送身份）仍然跟着绑定进入真实话题", async () => {
    const session = useSessionStore();
    session.draft = "本次没发出去的话";
    session.flushDraft();
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.sendAttribution();
    session.draft = "";
    await session.send("本次没发出去的话");
    expect(session.failedSendsForTopic(null).length).toBe(1);

    session.currentTopicId = "A";
    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "本次会话里由本机发送产生的失败记录应当归属真实话题（回到 A 要能找到）",
    ).toEqual(["本次没发出去的话"]);
  });
});
