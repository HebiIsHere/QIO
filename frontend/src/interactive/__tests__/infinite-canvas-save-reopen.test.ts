/**
 * 无限画布 · 保存重开（store → api.saveBoardState() → 重开读取）的定向回归。
 *
 * 用户可见契约：
 * - 在负坐标区建的卡片、连的关系，保存后重开必须**逐字段**还在：
 *   卡片的有符号 x/y、卡片 ID、关系两端 ID、关系文字与已有曲线参数；
 * - 重开远坐标板面时要能看到内容（视野可恢复；缺少有效视野时用「查看全部」初始化）；
 * - 缓存视野按 boardId 隔离，并与板面内容的保存状态分开。
 *
 * 这里走的是**正式保存入口**（store.commit → 自动保存 → api.saveBoardState），
 * 只把网络换成受控的「服务端」替身；真实后端联调由本地窗口验收承担。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../stores/interactive";
import * as imApi from "../../services/interactive";
import { addCard, addLink, emptyState, updateCard, updateLink } from "../board";
import type { BoardState, BoardStateResponse } from "../types";

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
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const BOARD_ID = "board_neg";
const STAMP = "2026-11-25T00:00:00.000Z";

/** 受控「服务端」：保存写进内存，读取原样返回（与真实接口语义一致）。 */
function fakeServer(initial: BoardState | null = null) {
  let stored: BoardState | null = initial;
  let seq = initial ? 1 : 0;
  vi.mocked(imApi.fetchBoardState).mockImplementation(async (): Promise<BoardStateResponse> => ({
    board: { id: BOARD_ID, title: "负坐标板面" },
    state: stored ?? emptyState(BOARD_ID),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: STAMP },
  }));
  vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, state: BoardState) => {
    stored = JSON.parse(JSON.stringify(state)) as BoardState;
    seq += 1;
    return { ok: true, seq, savedAt: STAMP, state: stored };
  });
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: STAMP } as never);
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  return {
    get stored() {
      return stored;
    },
  };
}

/**
 * 等自动保存真的落库。
 *
 * store 的保存是「450ms 防抖 + 排队 + 一次 PUT」：这里用真实定时器等过防抖窗口，
 * 再 flush 微任务，让 PUT 与回执都落地（不 mock 定时器 —— 那会把防抖与排队一起跳过）。
 */
async function flushSave(): Promise<void> {
  for (let i = 0; i < 8; i += 1) {
    await flushPromises();
    await new Promise<void>((resolve) => setTimeout(resolve, 120));
  }
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("负坐标板面：保存 → 重开逐字段一致", () => {
  it("卡片的有符号 x/y、卡片 ID、关系两端 ID、关系文字与曲线参数都原样回来", async () => {
    const server = fakeServer();
    const store = useInteractiveStore();
    store.boardId = BOARD_ID;
    await store.load();
    await flushPromises();

    // 在负坐标区建两张卡（远离原点）
    let next = addCard(store.board ?? emptyState(BOARD_ID), {
      kind: "text",
      content: "左上远卡",
      x: -8000,
      y: -6000,
      w: 260,
      h: 170,
    });
    next = addCard(next, { kind: "text", content: "原点附近", x: 120, y: 90, w: 260, h: 170 });
    const [farCard, nearCard] = next.cards;
    // 连一条关系并写含义 + 曲线
    next = addLink(next, farCard.id, nearCard.id, true, "属于");
    const linkId = next.links[0].id;
    next = updateLink(next, linkId, { curve: { x: 42, y: -18 } });
    store.commit(next, "在负坐标区建卡并连线");
    await flushSave();

    // 服务端真的收到了负坐标与关系
    expect(server.stored).not.toBeNull();
    const savedFar = server.stored!.cards.find((card) => card.id === farCard.id)!;
    expect({ x: savedFar.x, y: savedFar.y }).toEqual({ x: -8000, y: -6000 });
    const savedLink = server.stored!.links.find((link) => link.id === linkId)!;
    expect({ src: savedLink.src, dst: savedLink.dst }).toEqual({ src: farCard.id, dst: nearCard.id });
    expect(savedLink.meaning).toBe("属于");
    expect(savedLink.curve).toEqual({ x: 42, y: -18 });

    // 重开：新建 store（模拟关闭重开），从同一份「服务端」读回
    setActivePinia(createPinia());
    const reopened = useInteractiveStore();
    reopened.boardId = BOARD_ID;
    await reopened.load();
    await flushPromises();

    const cards = reopened.board!.cards;
    const reopenedFar = cards.find((card) => card.id === farCard.id)!;
    expect(reopenedFar).toBeTruthy();
    expect({ x: reopenedFar.x, y: reopenedFar.y }).toEqual({ x: -8000, y: -6000 });
    expect(reopenedFar.content).toBe("左上远卡");
    const reopenedNear = cards.find((card) => card.id === nearCard.id)!;
    expect({ x: reopenedNear.x, y: reopenedNear.y }).toEqual({ x: 120, y: 90 });

    const reopenedLink = reopened.board!.links.find((link) => link.id === linkId)!;
    expect(reopenedLink).toBeTruthy();
    expect({ src: reopenedLink.src, dst: reopenedLink.dst }).toEqual({ src: farCard.id, dst: nearCard.id });
    expect(reopenedLink.meaning).toBe("属于");
    expect(reopenedLink.direction).toBe(true);
    expect(reopenedLink.curve).toEqual({ x: 42, y: -18 });
  });

  it("重开远坐标板面：内容真的在板面上（卡片坐标不被夹回 0）", async () => {
    const seed = addCard(emptyState(BOARD_ID), {
      kind: "text",
      content: "远卡",
      x: -12000,
      y: -9000,
      w: 260,
      h: 170,
    });
    fakeServer(seed);
    const store = useInteractiveStore();
    store.boardId = BOARD_ID;
    await store.load();
    await flushPromises();
    const card = store.board!.cards[0];
    expect({ x: card.x, y: card.y }).toEqual({ x: -12000, y: -9000 });
    // 归一化不会把负坐标夹回 0（clampCardRect 只挡离谱值）
    expect(card.x).toBeLessThan(0);
    expect(card.y).toBeLessThan(0);
  });

  it("已有正坐标板面按原值打开（无需迁移历史卡片）", async () => {
    const seed = addCard(emptyState(BOARD_ID), { kind: "text", content: "老卡", x: 320, y: 240 });
    fakeServer(seed);
    const store = useInteractiveStore();
    store.boardId = BOARD_ID;
    await store.load();
    await flushPromises();
    const card = store.board!.cards[0];
    expect({ x: card.x, y: card.y }).toEqual({ x: 320, y: 240 });
  });

  it("拖动跨过原点后保存：只有被拖的那张卡坐标变了，另一张原样", async () => {
    const server = fakeServer();
    const store = useInteractiveStore();
    store.boardId = BOARD_ID;
    await store.load();
    await flushPromises();

    let next = addCard(store.board ?? emptyState(BOARD_ID), { kind: "text", content: "A", x: 100, y: 100 });
    next = addCard(next, { kind: "text", content: "B", x: 600, y: 400 });
    const [cardA, cardB] = next.cards;
    store.commit(next, "建两张卡");
    await flushSave();

    // 把 A 拖到负坐标（只改 A）
    const moved = updateCard(store.board!, cardA.id, { x: -400, y: -300 });
    store.commit(moved, "移动卡片");
    await flushSave();

    const savedA = server.stored!.cards.find((card) => card.id === cardA.id)!;
    const savedB = server.stored!.cards.find((card) => card.id === cardB.id)!;
    expect({ x: savedA.x, y: savedA.y }).toEqual({ x: -400, y: -300 });
    expect({ x: savedB.x, y: savedB.y }).toEqual({ x: 600, y: 400 });
  });
});