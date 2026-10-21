/**
 * 意图预演 → 批准落地（Stage 1）的定向测试。
 *
 * 前三组是纯逻辑（不发请求、不碰 DOM）：
 * 1) 哪些意图该画虚线预演（含 0.2.10 新状态，落地后必须消失）；
 * 2) 批准走哪条接口（新状态走 /confirm，旧状态才走 /approve）；
 * 3) 拖动 / 删除幽灵卡时 operations 怎么改、依赖操作怎么清理；
 * 4) PATCH plan 失败时本地预演回滚到修改前（不假成功）。
 * 最后一组是**轻量组件测试**：只挂 BoardPreviewLayer，验证指针手势真的接上了
 * 「改 operations → PATCH plan」，以及失败时真的回滚 + 如实提示（不重型挂载）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

import BoardPreviewLayer from "../../components/interactive/BoardPreviewLayer.vue";
import { useInteractiveStore } from "../../stores/interactive";

import {
  appliedForIntent,
  appliedSummaryText,
  approvalRoute,
  isPlanEditable,
  movePreviewCardInPlan,
  persistPlanEdit,
  previewCardOperationIndex,
  removePreviewCardFromPlan,
  shouldPreviewIntent,
} from "../intentPreviewApply";
import type { PreviewIntentLike } from "../intentPreviewApply";
import type { BoardCard, Intent, IntentOperation, IntentPreview } from "../types";

function previewCard(id: string, overrides: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: "reply",
    content: "结果 " + id,
    meta: {},
    x: 100,
    y: 100,
    w: 320,
    h: 240,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
    ...overrides,
  };
}

function preview(cards: BoardCard[] = [previewCard("a")]): IntentPreview {
  return { cards, groups: [], links: [], note: "" };
}

function intentLike(status: PreviewIntentLike["status"], overrides: Partial<PreviewIntentLike> = {}): PreviewIntentLike {
  return { status, preview: preview(), ...overrides };
}

const PLAN: IntentOperation[] = [
  { op: "add_reply_card", ref: "a", content: "结果 A", x: 100, y: 100 },
  { op: "add_text_card", ref: "b", content: "文字 B" },
  {
    op: "group_cards",
    ref: "g",
    name: "一组",
    ordered: true,
    cardIds: ["a", "card:b"],
  },
  { op: "order_group", groupId: "g", memberOrder: ["a", "b"] },
  { op: "add_link", src: "card:a", dst: "b", direction: true, meaning: "A 支撑 B" },
  { op: "move_card", cardId: "existing-1", x: 10, y: 20 },
];

describe("预演集合：新状态进入、落地后消失", () => {
  it("proposed / awaiting_confirmation / approved 都画预演", () => {
    expect(shouldPreviewIntent(intentLike("proposed"))).toBe(true);
    expect(shouldPreviewIntent(intentLike("awaiting_confirmation"))).toBe(true);
    expect(shouldPreviewIntent(intentLike("approved"))).toBe(true);
  });

  it("旧状态照旧画预演（不回归）", () => {
    for (const status of ["pending", "needs_update", "waiting_dependency", "waiting_confirm", "running"] as const) {
      expect(shouldPreviewIntent(intentLike(status)), status).toBe(true);
    }
  });

  it("已落地 / 已结束的状态不再画预演", () => {
    for (const status of ["completed", "done", "rejected", "failed", "cancelled"] as const) {
      expect(shouldPreviewIntent(intentLike(status)), status).toBe(false);
    }
  });

  it("带 applied 的意图即使状态还是 running 也不再画预演（真卡已经出现）", () => {
    expect(
      shouldPreviewIntent(intentLike("running", { applied: { cardIds: ["c1"], groupIds: [], linkIds: [] } })),
    ).toBe(false);
    expect(
      shouldPreviewIntent(intentLike("running", { applied: { cardIds: [], groupIds: [], linkIds: [] } })),
    ).toBe(true);
  });

  it("没有 preview 内容时不画空预演", () => {
    expect(shouldPreviewIntent(intentLike("proposed", { preview: null }))).toBe(false);
    expect(shouldPreviewIntent(intentLike("proposed", { preview: preview([]) }))).toBe(false);
  });
});

describe("批准路由：新状态走 /confirm，旧状态才走 /approve", () => {
  it("proposed / awaiting_confirmation → confirm", () => {
    expect(approvalRoute({ status: "proposed" })).toBe("confirm");
    expect(approvalRoute({ status: "awaiting_confirmation" })).toBe("confirm");
  });

  it("旧的可批准状态 → legacy-approve", () => {
    expect(approvalRoute({ status: "pending" })).toBe("legacy-approve");
    expect(approvalRoute({ status: "waiting_confirm" })).toBe("legacy-approve");
    expect(approvalRoute({ status: "paused" })).toBe("legacy-approve");
  });

  it("已确认 / 执行中 / 已结束 → 不再批准", () => {
    for (const status of ["approved", "running", "completed", "done", "failed", "cancelled", "rejected"] as const) {
      expect(approvalRoute({ status }), status).toBe("none");
    }
  });
});

describe("预演可编辑状态", () => {
  it("只有未批准且带 operations 的意图能改预演", () => {
    expect(isPlanEditable({ status: "proposed", operations: PLAN })).toBe(true);
    expect(isPlanEditable({ status: "awaiting_confirmation", operations: PLAN })).toBe(true);
    expect(isPlanEditable({ status: "approved", operations: PLAN })).toBe(false);
    expect(isPlanEditable({ status: "proposed", operations: [] })).toBe(false);
    expect(isPlanEditable({ status: "proposed" })).toBe(false);
  });
});

describe("幽灵卡 ↔ add 操作映射（与服务端 preview_from_operations 同口径）", () => {
  it("有 ref / cardId 时预览 id 就是它；没有时是 p_<下标>", () => {
    expect(previewCardOperationIndex(PLAN, "a")).toBe(0);
    expect(previewCardOperationIndex(PLAN, "b")).toBe(1);
    expect(
      previewCardOperationIndex([{ op: "add_reply_card", content: "没有符号" }], "p_0"),
    ).toBe(0);
    expect(previewCardOperationIndex(PLAN, "missing")).toBe(-1);
  });
});

describe("拖动幽灵卡：只改对应 add 操作的 x/y", () => {
  it("找到对应 add 操作并写入新坐标，不修改入参", () => {
    const result = movePreviewCardInPlan(PLAN, "a", { x: 480.4, y: 260.6 });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.operations[0]).toMatchObject({ op: "add_reply_card", x: 480, y: 261 });
    // 其他操作原样保留
    expect(result.operations.slice(1)).toEqual(PLAN.slice(1));
    // 入参没有被改
    expect(PLAN[0].x).toBe(100);
    expect(PLAN[0].y).toBe(100);
  });

  it("显式坐标必须赢过旧 place：拖动后去掉 place", () => {
    const withPlace: IntentOperation[] = [
      { op: "add_text_card", ref: "t", content: "文字", place: { relativeTo: "card:x", side: "right" } },
    ];
    const result = movePreviewCardInPlan(withPlace, "t", { x: 300, y: 400 });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.operations[0].x).toBe(300);
    expect(result.operations[0].y).toBe(400);
    expect(result.operations[0].place).toBeUndefined();
  });

  it("坐标无效或找不到卡片时如实失败（不改任何操作）", () => {
    expect(movePreviewCardInPlan(PLAN, "missing", { x: 1, y: 2 }).ok).toBe(false);
    const bad = movePreviewCardInPlan(PLAN, "a", { x: Number.NaN, y: 2 });
    expect(bad.ok).toBe(false);
  });
});

describe("删除幽灵卡：移除 add 操作与引用它的依赖操作", () => {
  it("删除后 group_cards / add_link 等引用它的操作一并移除，其余保留", () => {
    const result = removePreviewCardFromPlan(PLAN, "a");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const kinds = result.operations.map((op) => op.op);
    // 删除 a 对应的 add_reply_card、依赖 a/b 的组与排序、引用 a 的关系
    expect(kinds).toEqual(["add_text_card", "move_card"]);
    expect(result.operations[0].ref).toBe("b");
    expect(result.operations[1].cardId).toBe("existing-1");
  });

  it("typed 别名 card:a / 裸符号都能识别", () => {
    const byTyped = removePreviewCardFromPlan(PLAN, "a");
    expect(byTyped.ok).toBe(true);
    if (byTyped.ok) {
      expect(byTyped.operations.some((op) => op.op === "add_link")).toBe(false);
    }
  });

  it("只影响被删卡的引用：其他卡之间的组与关系保留", () => {
    const ops: IntentOperation[] = [
      { op: "add_text_card", ref: "b", content: "B" },
      { op: "add_text_card", ref: "c", content: "C" },
      { op: "group_cards", ref: "g", name: "BC", cardIds: ["b", "c"] },
      { op: "add_link", src: "b", dst: "c", meaning: "关系" },
    ];
    const result = removePreviewCardFromPlan(ops, "b");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.operations.map((op) => op.op)).toEqual(["add_text_card"]);
    expect(result.operations[0].ref).toBe("c");
  });

  it("order_group 的成员顺序里去掉被删卡，而不是留下悬空成员", () => {
    const ops: IntentOperation[] = [
      { op: "add_text_card", ref: "b", content: "B" },
      { op: "add_text_card", ref: "c", content: "C" },
      { op: "order_group", groupId: "existing-group", memberOrder: ["b", "c"] },
    ];
    const result = removePreviewCardFromPlan(ops, "b", { b: "pb" });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const order = result.operations.find((op) => op.op === "order_group");
    expect(order?.memberOrder).toEqual(["c"]);
  });

  it("找不到对应 add 操作时如实失败（不删任何东西）", () => {
    const result = removePreviewCardFromPlan(PLAN, "missing");
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason.length).toBeGreaterThan(0);
  });
});

describe("PATCH 失败：本地预演回滚，不假成功", () => {
  const baseline: { preview: IntentPreview; operations: IntentOperation[] } = {
    preview: preview(),
    operations: PLAN,
  };
  const nextOperations: IntentOperation[] = [{ op: "add_reply_card", ref: "a", content: "结果 A", x: 900, y: 900 }];

  it("服务端返回 ok:false 时回滚到修改前的预览与操作", async () => {
    const result = await persistPlanEdit({
      baseline,
      nextOperations,
      patch: async () => ({ ok: false, error: "not_editable", detail: "已批准的意图不能再改预演" }),
    });
    expect(result.ok).toBe(false);
    expect(result.preview).toEqual(baseline.preview);
    expect(result.operations).toEqual(baseline.operations);
    // 首选服务端给用户看的原因（detail）；error 码只是结构，不拿它当提示文案
    expect(result.error).toContain("已批准");
  });

  it("网络 / 抛错时同样回滚，并把真实原因交回界面", async () => {
    const result = await persistPlanEdit({
      baseline,
      nextOperations,
      patch: async () => {
        throw new Error("PATCH /plan -> 409: not_editable");
      },
    });
    expect(result.ok).toBe(false);
    expect(result.preview).toEqual(baseline.preview);
    expect(result.operations).toEqual(baseline.operations);
    expect(result.error).toContain("409");
  });

  it("成功时采用服务端返回的 preview / operations（以服务端事实为准）", async () => {
    const serverPreview = preview([previewCard("a", { x: 900, y: 900 })]);
    const result = await persistPlanEdit({
      baseline,
      nextOperations,
      patch: async (sent) => {
        expect(sent).toEqual(nextOperations);
        return { ok: true, intent: { preview: serverPreview, operations: nextOperations } };
      },
    });
    expect(result.ok).toBe(true);
    expect(result.preview).toEqual(serverPreview);
    expect(result.operations).toEqual(nextOperations);
    expect(result.error).toBe("");
  });
});

// --- 轻量组件测试：预演层的手势真的接上 PATCH plan，失败真的回滚 -----------------

let pinia: ReturnType<typeof createPinia>;
const mounted: ReturnType<typeof mount>[] = [];

function editableIntent(status: Intent["status"] = "proposed"): Intent {
  const a = previewCard("a", { kind: "reply", content: "结果 A" });
  const b = previewCard("b", { kind: "text", content: "文字 B", x: 460, w: 260, h: 170 });
  return {
    id: "in_1",
    boardId: "board_1",
    submissionId: null,
    title: "预演任务",
    summary: "",
    status,
    preview: { cards: [a, b], groups: [], links: [], note: "" },
    operations: [
      { op: "add_reply_card", ref: "a", content: "结果 A" },
      { op: "add_text_card", ref: "b", content: "文字 B" },
    ],
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-21T00:00:00+00:00",
    updatedAt: "2026-10-21T00:00:00+00:00",
  } as Intent;
}

beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
});

afterEach(() => {
  for (const wrapper of mounted.splice(0)) wrapper.unmount();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

function mountLayer(intent: Intent) {
  const wrapper = mount(BoardPreviewLayer, {
    props: {
      intents: [intent],
      locatedIntentId: null,
      width: 2400,
      height: 1600,
      // jsdom 没有真实缩放：用恒等换算验证「手势 → 板面坐标」这一段接线
      toBoardPoint: (x: number, y: number) => ({ x, y }),
    },
    attachTo: document.body,
    global: { plugins: [pinia] },
  });
  mounted.push(wrapper);
  return wrapper;
}

function pressPointer(el: Element, x: number, y: number): void {
  el.dispatchEvent(
    new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y }),
  );
}

function movePointer(x: number, y: number): void {
  window.dispatchEvent(new MouseEvent("pointermove", { bubbles: true, clientX: x, clientY: y }));
}

function releasePointer(): void {
  window.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }));
}

describe("预演层组件：拖动 / 删除真的接上 PATCH plan", () => {
  it("拖动幽灵卡：松手后只改对应 add 操作的 x/y（本地跟手 → 服务端保存）", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    const card = wrapper.find('[data-preview-card-id="a"]');
    expect(card.exists()).toBe(true);
    pressPointer(card.element, 100, 100);
    movePointer(180, 160);
    releasePointer();
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const [intentId, operations] = patch.mock.calls[0];
    expect(intentId).toBe("in_1");
    expect(operations[0]).toMatchObject({ op: "add_reply_card", x: 180, y: 160 });
    // 其他操作原样带回去（不整份重写语义）
    expect(operations[1]).toMatchObject({ op: "add_text_card", ref: "b" });
  });

  it("删除幽灵卡：点击 × 后 PATCH 去掉该 add 操作", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent());

    const remove = wrapper.find('[data-im="preview-delete"][data-preview-card-id="a"]');
    expect(remove.exists()).toBe(true);
    await remove.trigger("click");
    await flushPromises();

    expect(patch).toHaveBeenCalledTimes(1);
    const operations = patch.mock.calls[0][1];
    expect(operations.map((operation) => operation.ref)).toEqual(["b"]);
  });

  it("PATCH 失败：本地位置回滚、emit 失败原因（不假成功）", async () => {
    const store = useInteractiveStore();
    vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: false, error: "not_editable" });
    const wrapper = mountLayer(editableIntent());

    pressPointer(wrapper.find('[data-preview-card-id="a"]').element, 100, 100);
    movePointer(240, 220);
    releasePointer();
    await flushPromises();

    const errors = wrapper.emitted("edit-error") as string[][] | undefined;
    const message = (errors && errors.length ? errors[errors.length - 1][0] : "") ?? "";
    expect(message).toContain("已回到修改前");
    expect(message).toContain("not_editable");
    // 回滚：回到服务端预览的坐标（100/100），不是拖动后的 240/220
    const style = (wrapper.find('[data-preview-card-id="a"]').element as HTMLElement).style;
    expect(style.left).toBe("100px");
    expect(style.top).toBe("100px");
  });

  it("已批准（approved）的预演不可编辑：没有删除按钮，拖动不提交任何修改", async () => {
    const store = useInteractiveStore();
    const patch = vi.spyOn(store, "updateIntentPlan").mockResolvedValue({ ok: true, error: "" });
    const wrapper = mountLayer(editableIntent("approved"));

    expect(wrapper.find('[data-im="preview-delete"]').exists()).toBe(false);
    const card = wrapper.find('[data-preview-card-id="a"]');
    pressPointer(card.element, 100, 100);
    movePointer(200, 200);
    releasePointer();
    await flushPromises();
    expect(patch).not.toHaveBeenCalled();
  });
});

// --- 确认响应的 applied 解析（契约文字与后端实现形状不同，两种都要认） ---------

describe("确认即落地的 applied 解析", () => {
  it("按键分组形状（服务端实际返回）：只取当前意图的落地对象", () => {
    const applied = {
      in_1: { cardIds: ["c1", "c2"], groupIds: ["g1"], linkIds: [] },
      in_2: { cardIds: ["c3"] },
    };
    expect(appliedForIntent(applied, "in_1")).toEqual({
      cardIds: ["c1", "c2"],
      groupIds: ["g1"],
      linkIds: [],
    });
    expect(appliedSummaryText(appliedForIntent(applied, "in_1"))).toBe("2 张卡片、1 个组");
    expect(appliedForIntent(applied, "in_2")?.cardIds).toEqual(["c3"]);
    // 别的意图的落地不算到当前这一条头上
    expect(appliedForIntent(applied, "in_missing")).toBeNull();
  });

  it("扁平形状（契约文字描述）也认", () => {
    const flat = { cardIds: ["c1"], groupIds: [], linkIds: ["l1"] };
    expect(appliedForIntent(flat, "in_1")).toEqual(flat);
    expect(appliedSummaryText(flat)).toBe("1 张卡片、1 条关系");
  });

  it("没有落地时如实为空（不编造新增对象）", () => {
    expect(appliedForIntent(undefined, "in_1")).toBeNull();
    expect(appliedForIntent({}, "in_1")).toBeNull();
    expect(appliedForIntent({ in_1: {} }, "in_1")).toBeNull();
    expect(appliedSummaryText(null)).toBe("");
    expect(appliedSummaryText({})).toBe("");
  });
});
