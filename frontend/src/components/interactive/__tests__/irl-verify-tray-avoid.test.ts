/**
 * IRL-E 独立复核 · 审批批量列表让开对话窗口（集成边界）对抗式用例。
 *
 * 与 Lead 的 irl-lead-tray-avoid.test.ts 换角度：
 * 1) 「只在原本真的会叠上时才改几何」—— 用二分法找出竖直方向的**精确分界**，成对断言；
 * 2) 让开之后批量列表的**上界矩形**必须落在 widestFreeBand 给出的可用带内（窄带 + 160 下限的反例）；
 * 3) 带被吃光进入切换显示时，切换条的现状（记录，不是缺陷判定）。
 *
 * 断言口径：不比死像素，只比「与基线一致 / 与基线不同」「矩形是否相交」。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { OVERLAY_BATCH_FOOTER, OVERLAY_EDGE, OVERLAY_GAP } from "../../../interactive/overlayLayout";
import { publishChatFrame, resetChatFrameState } from "../../../interactive/chatFrameState";
import { widestFreeBand, type Band } from "../../../interactive/overlayAvoid";
import { INTENT_BATCH_STORAGE_KEY } from "../../../interactive/approval";
import type { BoardCard, Intent } from "../../../interactive/types";

const W = 1440;
const H = 900;
const STAGE_TOP = 52;
const TOOLBAR_TOP = 833;

let stage: HTMLElement;
let originalRect: typeof Element.prototype.getBoundingClientRect;

interface ChatRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Geo {
  right: number;
  width: number;
  mode: string;
  bar: { right: number; maxWidth: number } | null;
}

function card(id: string): BoardCard {
  return {
    id,
    kind: "reply",
    content: "预览内容",
    meta: {},
    x: 40,
    y: 40,
    w: 240,
    h: 96,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  };
}

function intent(id: string): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    status: "pending",
    title: "演示任务 " + id,
    summary: "",
    preview: { cards: [card("c_" + id)], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  };
}

/** 有真实批次，几何层才会把 batchAvailable 当作可用（否则不会进入切换布局）。 */
function recordBatches(ids: string[]): void {
  localStorage.setItem(
    INTENT_BATCH_STORAGE_KEY,
    JSON.stringify({ version: 1, records: ids.map((id) => ({ id, key: "session:demo" })) }),
  );
}

function fakeRect(top: number, height: number, left = 0, width = W): DOMRect {
  return {
    x: left,
    y: top,
    top,
    left,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

async function flush(): Promise<void> {
  await new Promise((done) => setTimeout(done, 5));
  await new Promise((done) => setTimeout(done, 5));
}

/** 挂载一次、读几何、卸载：每次都用同一套环境，读到的差异只可能来自让开逻辑。 */
async function readGeometry(chat: ChatRect | null, collapsed = false): Promise<Geo> {
  resetChatFrameState();
  if (chat) publishChatFrame(chat, collapsed);
  const wrapper = mount(IntentBatchTray, { attachTo: stage });
  await flush();
  const tray = document.querySelector('[data-im="batch-area"]') as HTMLElement | null;
  const barEl = document.querySelector('[data-im="overlay-switch"]') as HTMLElement | null;
  const geo: Geo = {
    right: tray ? Number.parseFloat(tray.style.right || "NaN") : Number.NaN,
    width: tray ? Number.parseFloat(tray.style.maxWidth || "NaN") : Number.NaN,
    mode: tray?.parentElement?.getAttribute("im-geo-mode") ?? "",
    bar:
      barEl && barEl.style.right
        ? { right: Number.parseFloat(barEl.style.right), maxWidth: Number.parseFloat(barEl.style.maxWidth) }
        : null,
  };
  wrapper.unmount();
  await flush();
  return geo;
}

const sameGeo = (a: Geo, b: Geo): boolean => a.right === b.right && a.width === b.width && a.mode === b.mode;

/** 批量列表的上界矩形（right 贴视口右边，宽 = maxWidth） */
function trayRect(geo: Geo): Band {
  return { left: W - geo.right - geo.width, right: W - geo.right };
}

function clearOf(rect: Band, chat: ChatRect): boolean {
  return (
    rect.right <= chat.x - OVERLAY_GAP + 1 || rect.left >= chat.x + chat.w + OVERLAY_GAP - 1
  );
}

beforeEach(() => {
  setActivePinia(createPinia());
  resetChatFrameState();
  document.body.innerHTML = "";
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { value: W, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: H, configurable: true, writable: true });
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const hook = this.getAttribute("data-im");
    if (hook === "board-toolbar") return fakeRect(TOOLBAR_TOP, 96, 0, W);
    // 组件的「舞台」取 tray.parentElement：jsdom 下 Vue Test Utils 会在 attachTo 的元素
    // 与组件根之间插一个 data-v-app 包装层，若不把它当成舞台，量到的可用区就是 0×0，
    // 所有几何断言都会退化成「空间不足」的假绿。
    if (this.classList.contains("im-stage") || this.hasAttribute("data-v-app")) {
      return fakeRect(STAGE_TOP, H - STAGE_TOP, 0, W);
    }
    return fakeRect(0, 0);
  };
  stage = document.createElement("div");
  stage.className = "im-stage";
  document.body.appendChild(stage);
  const toolbar = document.createElement("div");
  toolbar.setAttribute("data-im", "board-toolbar");
  document.body.appendChild(toolbar);
  const store = useInteractiveStore();
  store.intents = [intent("i0"), intent("i1"), intent("i2"), intent("i3")];
  store.chatOpen = true;
  store.batchOpen = true;
  recordBatches(["i0", "i1", "i2", "i3"]);
});

afterEach(() => {
  Element.prototype.getBoundingClientRect = originalRect;
  resetChatFrameState();
  document.body.innerHTML = "";
});

describe("只在原本真的会叠上时才改几何", () => {
  it("窗口贴顶（底边 ≤ 上界之上一个 gap）：几何与基线逐字节一致", async () => {
    const base = await readGeometry(null);
    // 判据用的是批量列表的**最大**矩形：它的上沿 = stageTop + EDGE，再让一个 gap，
    // 所以只有底边不超过 52 + 16 - 12 = 56 的窗口才完全不参与让开。
    const boundary = STAGE_TOP + OVERLAY_EDGE - OVERLAY_GAP;
    const chat: ChatRect = { x: 400, y: 0, w: 600, h: boundary };
    const withChat = await readGeometry(chat);
    expect(sameGeo(withChat, base)).toBe(true);
  });

  it("窗口横向完全错开（在批量列表原始矩形右侧）：与基线一致", async () => {
    const base = await readGeometry(null);
    const chat: ChatRect = { x: 1300, y: 400, w: 130, h: 300 };
    const withChat = await readGeometry(chat);
    expect(sameGeo(withChat, base)).toBe(true);
  });

  it("竖直分界成对用例：底边 56 不改、57 改（阈值 = stageTop + EDGE - GAP）", async () => {
    const base = await readGeometry(null);
    const boundary = STAGE_TOP + OVERLAY_EDGE - OVERLAY_GAP;
    const x = 400;
    const width = 600;
    const height = 40;
    expect(boundary).toBe(56);

    const below = await readGeometry({ x, y: boundary - height, w: width, h: height });
    expect(sameGeo(below, base)).toBe(true);
    const above = await readGeometry({ x, y: boundary - height + 1, w: width, h: height });
    expect(sameGeo(above, base)).toBe(false);
    expect(clearOf(trayRect(above), { x, y: boundary - height + 1, w: width, h: height })).toBe(true);
  });

  it("行为说明：判据用最大高度矩形，窗口只要没跑到顶边 56px 以内就会触发横向让位", async () => {
    // 窗口落在屏幕上半部、其底边（352）远在批量列表当前可见内容之上，
    // 但只要它进入了「批量列表可能占到的竖直区间」[68, batchBottom]，就会让位。
    const chat: ChatRect = { x: 400, y: STAGE_TOP, w: 600, h: 300 };
    const geo = await readGeometry(chat);
    expect(geo.right).not.toBe(448); // 448 = 并排布局下批量列表原始右偏移
    expect(clearOf(trayRect(geo), chat)).toBe(true);
  });

  it("分界处批量列表仍落在视口内（右贴边不越界）", async () => {
    const geo = await readGeometry({ x: 400, y: 500, w: 600, h: 200 });
    const rect = trayRect(geo);
    expect(rect.right).toBeLessThanOrEqual(W - OVERLAY_EDGE + 0.5);
    expect(rect.left).toBeGreaterThanOrEqual(0);
  });

  it("窗口收起（collapsed）：空间视为可用，与基线一致", async () => {
    const base = await readGeometry(null);
    const geo = await readGeometry({ x: 400, y: 400, w: 600, h: 340 }, true);
    expect(sameGeo(geo, base)).toBe(true);
  });
});

describe("让开之后的上界矩形必须落在可用带内", () => {
  it("宽裕的带：上界矩形落在 widestFreeBand 给出的带内，且不与窗口相交", async () => {
    const chat: ChatRect = { x: 500, y: 400, w: 400, h: 340 };
    const geo = await readGeometry(chat);
    const band = widestFreeBand({ left: OVERLAY_EDGE, right: W - OVERLAY_EDGE }, [chat], OVERLAY_GAP);
    expect(band).not.toBeNull();
    const rect = trayRect(geo);
    expect(rect.right).toBeLessThanOrEqual(band!.right + 0.5);
    expect(rect.left).toBeGreaterThanOrEqual(band!.left - 0.5);
    expect(clearOf(rect, chat)).toBe(true);
  });

  it("现状（窄带 + 160px 下限）：可用带只有 88px，上界矩形被抬到 160px，重新压住窗口", async () => {
    // 窗口几乎占满宽度，只剩右侧 88px 的带（< 160）。
    const chat: ChatRect = { x: OVERLAY_EDGE, y: 400, w: W - OVERLAY_EDGE - 100 - OVERLAY_EDGE, h: 340 };
    const geo = await readGeometry(chat);
    const band = widestFreeBand({ left: OVERLAY_EDGE, right: W - OVERLAY_EDGE }, [chat], OVERLAY_GAP);
    expect(band).not.toBeNull();
    expect(band!.right - band!.left).toBeLessThan(160);
    expect(geo.width).toBe(160); // Math.max(160, ...) 抬上来的下限
    expect(clearOf(trayRect(geo), chat)).toBe(false);
  });

  it.fails("窄带时上界矩形也不该超过可用带（否则「让开」会重新制造碰撞）", async () => {
    const chat: ChatRect = { x: OVERLAY_EDGE, y: 400, w: W - OVERLAY_EDGE - 100 - OVERLAY_EDGE, h: 340 };
    const geo = await readGeometry(chat);
    const band = widestFreeBand({ left: OVERLAY_EDGE, right: W - OVERLAY_EDGE }, [chat], OVERLAY_GAP)!;
    const rect = trayRect(geo);
    expect(rect.right).toBeLessThanOrEqual(band.right + 0.5);
    expect(rect.left).toBeGreaterThanOrEqual(band.left - 0.5);
  });
});

describe("带被吃光 → 切换显示时的现状", () => {
  it("记录：窗口覆盖整条带时进入 switched，切换条回落到右边缘（仍压在窗口范围内）", async () => {
    const chat: ChatRect = { x: OVERLAY_EDGE, y: 120, w: W - OVERLAY_EDGE * 2, h: H - 200 };
    const geo = await readGeometry(chat);
    expect(geo.mode).toBe("switched");
    expect(geo.bar).not.toBeNull();
    const barRect: Band = { left: W - geo.bar!.right - geo.bar!.maxWidth, right: W - geo.bar!.right };
    // 现状：band 为 null 时切换条不再按窗口让开，直接贴右边缘 —— 会压住窗口。
    expect(clearOf(barRect, chat)).toBe(false);
  });

  it("切换条的高度行仍由几何计划预留（不越过工具栏）", async () => {
    const chat: ChatRect = { x: OVERLAY_EDGE, y: 120, w: W - OVERLAY_EDGE * 2, h: H - 200 };
    const geo = await readGeometry(chat);
    expect(geo.bar).not.toBeNull();
    expect(geo.bar!.right).toBeGreaterThanOrEqual(OVERLAY_EDGE);
  });
});
