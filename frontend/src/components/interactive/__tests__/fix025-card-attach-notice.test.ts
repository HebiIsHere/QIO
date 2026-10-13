/**
 * 0.2.5 回归修复（任务书 §二.8）：**卡片内**的附件反馈必须和 AddMenu 用同一套规则。
 *
 * 修复前的实现：BoardCard 的 pickAttachment 成功后把 attachStatus 设成 ok，
 * 模板 v-if="attachStatus" 一直挂着 —— 提示永远不消失；没有 token / 竞态处理，
 * 旧操作的迟到回调会清掉或覆盖新提示、甚至用旧操作的附件把新关联覆盖回去；
 * 取消文案固定说「这张卡片还没有关联已保存的附件」，卡片本来就有附件时那是错的。
 *
 * 规则（与 AddMenu 的 fix025-attach-notice.test.ts 逐条对齐）：
 *   1 成功约 3 秒后自动移除，移除的只是提示：emit 出去的 patch、卡片 meta 里的
 *     attachmentId、卡片内的关联状态行都还在；成功文案不许声称服务端已保存
 *     （组件只 emit patch 给父级 commit）；
 *   2 等待选择 / 上传 / 处理三段是真实进度，不会被成功计时器提前藏掉；
 *   3 失败不自动消失：原因 + 关闭入口 + 「重新选择文件」（真的重跑一遍上传）；点了关闭就消失；
 *   4 连续两次关联：旧计时器清不掉新提示；
 *   5 旧操作的迟到结果既不覆盖 / 清掉新提示，也不再把旧附件覆盖回板面；
 *   6 卸载后没有残留计时器，迟到结果不复活提示、不 emit patch；
 *   7 取消文案按卡片当前事实区分（没有附件 / 已有附件）；
 *   8 长文件名可查看完整名称（title）且允许换行 / 断词。
 *
 * 模拟边界：附件服务（系统对话框 / 网络）全 mock；pinia store 是真的；
 * 草稿写通道（setCardDraftInput）在本用例里点掉 —— 它自带保存 debounce 计时器，
 * 会让「提示还剩几个计时器」的断言失真（与本轮修复无关）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick, reactive } from "vue";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  pickBrowserFile: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  fetchAttachmentContent: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    pickBrowserFile: mocks.pickBrowserFile,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    fetchAttachmentContent: mocks.fetchAttachmentContent,
  };
});

import BoardCard from "../BoardCard.vue";
// 样式契约按源码断言（jsdom 不做布局）：Vite 的 ?raw 在 vitest 里可用，与既有测试同一做法
import sfcSource from "../BoardCard.vue?raw";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, setSelection } from "../../../interactive/board";
import type { AttachmentRef } from "../../../services/attachments";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const SFC_SOURCE: string = sfcSource;
const STAMP = "2026-10-13T00:00:00.000Z";
const CARD_ID = "card_attach";

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "报告.pdf",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function mkCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id: CARD_ID,
    kind: "file",
    content: "待补充文件说明",
    meta: { name: "未命名文件" },
    x: 0,
    y: 0,
    w: 260,
    h: 170,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * 假定时器下**不能**用 @vue/test-utils 的 flushPromises：它走 setImmediate，
 * 会被 vitest 的假定时器一起假掉（永远不 resolve）。用推进 0ms + 排空微任务代替。
 */
async function flush(times = 3): Promise<void> {
  for (let i = 0; i < times; i += 1) await vi.advanceTimersByTimeAsync(0);
}

interface Harness {
  w: VueWrapper;
  card: BoardCardModel;
}

let wrappers: VueWrapper[] = [];

/** 挂一张 file 卡片、选中、并走真实入口进入编辑态（关联入口只在编辑态）。 */
async function mountEditing(over: Partial<BoardCardModel> = {}): Promise<Harness> {
  const store = useInteractiveStore();
  // 草稿通道自带 debounce 计时器：点掉它，让计时器断言只反映附件提示
  vi.spyOn(store, "setCardDraftInput").mockImplementation(() => undefined);
  const card = reactive(mkCard(over));
  store.board = setSelection({ ...emptyState("board_t"), cards: [card] }, [card.id]);
  const w = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card,
      selected: true,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: card.y,
      multi: false,
      connecting: false,
    },
  });
  wrappers.push(w);
  await w.get('[data-im="card-edit"]').trigger("click");
  await flush();
  expect(w.find('[data-im="card-attach-file"]').exists(), "编辑态没有关联附件的入口").toBe(true);
  return { w, card };
}

function statusEl(w: VueWrapper) {
  return w.find('[data-im="card-attach-status"]');
}

function statusText(w: VueWrapper): string {
  const el = w.find('[data-im="card-attach-status-text"]');
  return el.exists() ? el.text() : "";
}

async function clickAttach(w: VueWrapper): Promise<void> {
  await w.get('[data-im="card-attach-file"]').trigger("click");
  await flush();
}

function patches(w: VueWrapper): unknown[][] {
  return (w.emitted("patch") ?? []) as unknown[][];
}

function lastPatchMeta(w: VueWrapper): Record<string, unknown> | null {
  const list = patches(w);
  const last = list[list.length - 1];
  if (!last) return null;
  const patch = last[1] as Partial<BoardCardModel> | undefined;
  return (patch?.meta as Record<string, unknown>) ?? null;
}

/** 父级把 patch 落回卡片（真实父组件就是这么做的）。 */
async function applyLastPatch(h: Harness): Promise<void> {
  const meta = lastPatchMeta(h.w);
  if (meta) h.card.meta = meta;
  await nextTick();
}

/** 取 SFC 里某条样式规则（jsdom 不做布局，样式契约只能按源码断言）。 */
function cssRule(selector: string): string {
  const start = SFC_SOURCE.indexOf(selector + " {");
  expect(start, "样式里没有找到 " + selector).toBeGreaterThan(-1);
  return SFC_SOURCE.slice(start, SFC_SOURCE.indexOf("}", start));
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  setActivePinia(createPinia());
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.uploadAttachment.mockResolvedValue(attachment());
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
  mocks.fetchAttachmentContent.mockResolvedValue(new Blob(["x"]));
});

afterEach(() => {
  for (const w of wrappers.splice(0)) w.unmount();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// 1. 成功：约 3 秒后只移除提示
// ---------------------------------------------------------------------------

describe("卡片内成功提示的生命周期", () => {
  it("成功约 3 秒后自动移除；emit 的附件关联与卡片状态全部保留", async () => {
    const h = await mountEditing();
    const { w } = h;
    const base = vi.getTimerCount();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "报告.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_1", name: "报告.pdf" }));
    await clickAttach(w);

    const el = statusEl(w);
    expect(el.exists(), "关联成功后没有提示").toBe(true);
    expect(el.classes(), "成功提示的色调不对").toContain("is-ok");
    expect(statusText(w)).toContain("报告.pdf");
    // 成功文案的诚实口径：附件已保存 + 板面关联还是本地改动（组件只 emit patch）
    expect(statusText(w), "成功文案没有说清「提交后 QIO 才可见」").toContain("提交");
    expect(statusText(w), "成功文案没有说清这是本地板面改动").toContain("本地");
    expect(statusText(w), "成功文案声称服务端已保存 / 同步了板面关联").not.toMatch(/服务端|已同步|已保存到/);
    expect(vi.getTimerCount(), "成功提示没有挂上自动移除计时器").toBe(base + 1);

    // 关联通过 patch 交给父级（组件自己不直接改板面状态）
    const meta = lastPatchMeta(w);
    expect((meta?.file as { attachmentId: string } | undefined)?.attachmentId).toBe("att_1");
    expect(meta?.name).toBe("报告.pdf");

    await applyLastPatch(h);
    expect(w.get('[data-im="card-attachment-state"]').text()).toContain("报告.pdf");

    // 3 秒之内还在
    await vi.advanceTimersByTimeAsync(2900);
    expect(statusEl(w).exists(), "成功提示提前消失了").toBe(true);

    // 约 3 秒后自动移除
    await vi.advanceTimersByTimeAsync(200);
    expect(statusEl(w).exists(), "成功提示没有自动移除（0.2.5 回归 §二.8）").toBe(false);
    expect(vi.getTimerCount(), "自动移除后计时器没有回收").toBe(base);

    // 移除的只是提示：卡片 meta、关联状态行与 emit 出去的 patch 都还在
    expect((h.card.meta.file as { attachmentId: string }).attachmentId).toBe("att_1");
    expect(w.get('[data-im="card-attachment-state"]').text()).toContain("报告.pdf");
    expect(w.find('[data-im="card-attach-file"]').exists()).toBe(true);
    expect(patches(w).length, "自动移除提示时又发了一次 patch").toBe(1);
  });
});

// ---------------------------------------------------------------------------
// 2. 进行中：真实进度不被藏掉
// ---------------------------------------------------------------------------

describe("卡片内进行中的真实进度", () => {
  it("等待选择 / 上传 / 处理三段的提示都一直可见", async () => {
    const { w } = await mountEditing();
    const picking = deferred<File | null>();
    mocks.pickBrowserFile.mockReturnValue(picking.promise);
    await w.get('[data-im="card-attach-file"]').trigger("click");
    await flush();
    expect(statusText(w)).toContain("正在等待选择文件");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl(w).exists(), "等待选择时提示被藏掉了").toBe(true);

    const uploading = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValue(uploading.promise);
    picking.resolve(new File(["x"], "很慢的季度报告.bin"));
    await flush();
    expect(statusText(w)).toContain("正在保存附件");
    expect(statusText(w)).toContain("很慢的季度报告.bin");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl(w).exists(), "上传期间提示被藏掉了").toBe(true);

    const settling = deferred<AttachmentRef>();
    mocks.waitUntilSettled.mockReturnValue(settling.promise);
    uploading.resolve(attachment({ id: "att_slow", name: "很慢的季度报告.bin", state: "prepared" }));
    await flush();
    expect(statusText(w), "上传完成后的「处理中」没有真实进度").toContain("正在处理附件");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl(w).exists(), "服务端处理期间提示被藏掉了").toBe(true);

    settling.resolve(attachment({ id: "att_slow", name: "很慢的季度报告.bin" }));
    await flush();
    expect(statusText(w)).toContain("已关联附件");
  });
});

// ---------------------------------------------------------------------------
// 3. 失败：不自动消失 + 关闭 + 重新操作
// ---------------------------------------------------------------------------

describe("卡片内失败提示", () => {
  it("不自动消失：有原因、有关闭入口、有真的会重跑的「重新选择文件」", async () => {
    const { w } = await mountEditing();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "大文件.bin"));
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("磁盘写满了"));
    await clickAttach(w);

    const el = statusEl(w);
    expect(el.exists(), "上传失败时没有提示").toBe(true);
    expect(el.classes(), "失败提示的色调不对").toContain("is-error");
    expect(el.text()).toContain("磁盘写满了");
    expect(statusText(w)).toContain("附件保存失败");
    expect(w.find('[data-im="card-attach-dismiss"]').exists(), "失败提示没有关闭入口").toBe(true);
    const retry = w.find('[data-im="card-attach-retry"]');
    expect(retry.exists(), "失败提示没有明确的重新操作入口").toBe(true);
    expect(retry.text()).toContain("重新选择文件");

    await vi.advanceTimersByTimeAsync(30_000);
    expect(statusEl(w).exists(), "失败提示被自动清掉了").toBe(true);

    // 点「重新选择文件」：真的重新走 pickBrowserFile → 上传 → 关联
    const picksBefore = mocks.pickBrowserFile.mock.calls.length;
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "大文件.bin"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_retry", name: "大文件.bin" }));
    await retry.trigger("click");
    await flush();
    expect(mocks.pickBrowserFile.mock.calls.length, "重新选择没有重新发起选择").toBe(picksBefore + 1);
    expect(mocks.uploadAttachment, "重新选择没有真的再上传一次").toHaveBeenCalledTimes(2);
    expect(statusText(w)).toContain("已关联附件");

    // 关闭入口真的关得掉（再来一次失败）
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("这次是没有权限"));
    await clickAttach(w);
    expect(statusEl(w).classes()).toContain("is-error");
    expect(statusText(w)).toContain("这次是没有权限");
    await w.get('[data-im="card-attach-dismiss"]').trigger("click");
    await flush();
    expect(statusEl(w).exists(), "点了关闭提示还在").toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4/5. 连续两次：旧计时器与迟到结果
// ---------------------------------------------------------------------------

describe("卡片内连续两次关联", () => {
  it("旧的成功计时器不会清掉新提示", async () => {
    const { w } = await mountEditing();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "第一份.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_a", name: "第一份.pdf" }));
    await clickAttach(w);
    expect(statusText(w)).toContain("第一份.pdf");

    await vi.advanceTimersByTimeAsync(2000);
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "第二份.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_b", name: "第二份.pdf" }));
    await clickAttach(w);
    expect(statusText(w), "新提示没有出现").toContain("第二份.pdf");

    // 距第一次成功已 3500ms > 3000ms：旧计时器若没被清掉，新提示已经被它清掉了
    await vi.advanceTimersByTimeAsync(1500);
    expect(statusEl(w).exists(), "旧计时器把新提示清掉了").toBe(true);
    expect(statusText(w)).toContain("第二份.pdf");

    await vi.advanceTimersByTimeAsync(1600);
    expect(statusEl(w).exists(), "新提示没有自己的 3 秒生命周期").toBe(false);
  });

  it("旧操作的迟到结果不覆盖新提示，也不再 emit 过期的 patch", async () => {
    const { w } = await mountEditing();
    // 第一次：选好了文件，但上传一直不返回
    const latePick = deferred<File | null>();
    const lateUpload = deferred<AttachmentRef>();
    mocks.pickBrowserFile.mockReturnValueOnce(latePick.promise);
    mocks.uploadAttachment.mockReturnValueOnce(lateUpload.promise);
    await w.get('[data-im="card-attach-file"]').trigger("click");
    await flush();
    latePick.resolve(new File(["x"], "迟到.pdf"));
    await flush();
    expect(statusText(w)).toContain("正在保存附件");

    // 第二次：完整走完并成功，提示与板面都归第二次所有
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_new", name: "新的.pdf" }));
    await clickAttach(w);
    expect(statusText(w)).toContain("新的.pdf");
    const afterSecond = patches(w).length;
    expect((lastPatchMeta(w)?.file as { attachmentId: string }).attachmentId).toBe("att_new");

    // 第一次的上传这才返回：迟到结果不许覆盖 / 清掉新提示，也不许把旧附件写回板面
    const settlesBefore = mocks.waitUntilSettled.mock.calls.length; // 第二次操作正常跟进的那一次
    lateUpload.resolve(attachment({ id: "att_late", name: "迟到.pdf" }));
    await flush();
    expect(statusText(w), "旧操作的迟到结果覆盖了新提示").toContain("新的.pdf");
    expect(statusText(w), "迟到文件名出现在新提示里").not.toContain("迟到.pdf");
    expect(patches(w).length, "迟到的旧操作又把旧附件 emit 回板面了").toBe(afterSecond);
    expect(
      mocks.waitUntilSettled.mock.calls.length,
      "迟到的旧操作还在继续跟进附件状态",
    ).toBe(settlesBefore);

    // 新提示仍按自己的 3 秒走完
    await vi.advanceTimersByTimeAsync(2900);
    expect(statusEl(w).exists()).toBe(true);
    await vi.advanceTimersByTimeAsync(200);
    expect(statusEl(w).exists(), "迟到回调干扰了新提示的计时器").toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 6. 卸载：不残留，也不复活
// ---------------------------------------------------------------------------

describe("卡片卸载", () => {
  it("卸载时把成功提示的计时器一起清掉", async () => {
    const { w } = await mountEditing();
    const base = vi.getTimerCount();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "报告.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_ok", name: "报告.pdf" }));
    await clickAttach(w);
    expect(vi.getTimerCount(), "成功提示应当恰好有一个自动移除计时器").toBe(base + 1);

    w.unmount();
    wrappers = wrappers.filter((item) => item !== w);
    expect(vi.getTimerCount(), "卸载后仍有残留计时器").toBe(base);
    await vi.advanceTimersByTimeAsync(3000);
    expect(vi.getTimerCount(), "卸载后仍被计时器唤醒").toBe(base);
  });

  it("卸载后迟到的结果：不复活提示、不 emit patch", async () => {
    const { w } = await mountEditing();
    const base = vi.getTimerCount();
    const lateUpload = deferred<AttachmentRef>();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "迟到.pdf"));
    mocks.uploadAttachment.mockReturnValueOnce(lateUpload.promise);
    await clickAttach(w);
    expect(statusText(w)).toContain("正在保存附件");

    w.unmount();
    wrappers = wrappers.filter((item) => item !== w);

    lateUpload.resolve(attachment({ id: "att_late", name: "迟到.pdf" }));
    await flush();
    expect(vi.getTimerCount(), "迟到的成功结果又挂了计时器（提示复活）").toBe(base);
    expect(mocks.waitUntilSettled, "卸载后还继续跟进附件状态").not.toHaveBeenCalled();
    expect(patches(w).length, "卸载后迟到回调还 emit 了 patch").toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 7. 取消选择：文案必须符合卡片当前事实
// ---------------------------------------------------------------------------

describe("卡片内取消选择", () => {
  it("卡片没有附件：如实说「还没有关联已保存的附件，读不到正文」", async () => {
    const { w, card } = await mountEditing();
    mocks.pickBrowserFile.mockResolvedValue(null);
    await clickAttach(w);

    expect(statusEl(w).exists(), "取消选择后没有任何说明").toBe(true);
    expect(statusEl(w).classes()).toContain("is-error");
    expect(statusText(w)).toContain("没有选择文件");
    expect(statusText(w)).toContain("还没有关联已保存的附件");
    expect(statusText(w)).toContain("读不到它的正文");
    expect(card.meta.file, "取消不该凭空多出附件").toBeUndefined();
    // 取消不是一闪而过：用户要能看见自己取消了什么
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl(w).exists(), "取消说明被自动清掉了").toBe(true);
  });

  it("卡片已有附件：如实说「仍然关联着原有附件」，不许说没有", async () => {
    const { w, card } = await mountEditing({
      meta: { name: "原有的.pdf", file: { name: "原有的.pdf", attachmentId: "att_old", size: 10 } },
    });
    // 这张卡片本来就有关联：按钮文案是「重新关联」而不是「关联」
    expect(w.get('[data-im="card-attach-file"]').text()).toContain("重新关联");
    mocks.pickBrowserFile.mockResolvedValue(null);
    await clickAttach(w);

    expect(statusText(w)).toContain("没有选择文件");
    expect(statusText(w), "原始附件被说成不存在").toContain("仍然关联着原有附件");
    expect(statusText(w)).toContain("原有的.pdf");
    expect(statusText(w), "卡片已有附件却还在说「还没有关联」").not.toContain("还没有关联");
    expect((card.meta.file as { attachmentId: string }).attachmentId, "取消不该动原有附件").toBe("att_old");
  });
});

// ---------------------------------------------------------------------------
// 8. 长文件名
// ---------------------------------------------------------------------------

describe("卡片内长文件名", () => {
  it("完整名称可查看（title）且允许换行 / 断词", async () => {
    const { w } = await mountEditing();
    const longName = "2026年第三季度华东区供应链风险与合规审计报告最终版（含附件清单、批注与修订说明）.pdf";
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], longName));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_long", name: longName }));
    await clickAttach(w);

    const el = statusEl(w);
    expect(el.attributes("title") ?? "", "提示上没有可查看完整名称的 title").toContain(longName);
    expect(statusText(w), "提示里没有完整文件名").toContain(longName);
    // 样式护栏（jsdom 不做布局）：状态区允许换行，文本区允许断词
    expect(cssRule(".attach-status"), "状态区不能换行").toContain("flex-wrap: wrap");
    expect(cssRule(".attach-status-text"), "文本区缺少长文件名换行样式").toContain("overflow-wrap: anywhere");
    expect(cssRule(".attach-status-text"), "文本区缺少断词样式").toContain("word-break: break-word");
  });
});
