/**
 * 0.2.5 回归修复：**添加附件后的成功提示必须自己消失**（AddMenu，任务书 §二）。
 *
 * 回归现象（修复前的实现）：attachRealFile 成功后把 status 设成 {tone:"ok"}，
 * 模板 v-if="status" 一直挂着 —— 提示永远不会消失；也没有 token / 竞态处理，
 * 旧操作的迟到回调会清掉 / 覆盖新提示；取消与失败的文案还固定声称
 * 「这张卡片还没有关联已保存的附件」，卡片本来就有附件时那是在说谎。
 *
 * 这一份测试按 Lead 冻结的 useTransientNotice 原语验收（假定时器，不依赖真实 3 秒）：
 *   1 成功约 3 秒后自动移除，且只移除提示（卡片 meta 里的 attachmentId 仍在）；
 *     成功文案只陈述「附件已保存」与「板面关联是本地改动」，不声称服务端已保存；
 *   2 等待选择 / 上传 / 处理期间是真实进度，不会被成功计时器提前藏掉；
 *   3 失败不自动消失：简短原因 + 关闭入口 + 「重新选择文件」，点了真的重走一遍；
 *   4 连续两次添加：旧计时器清不掉新提示，旧操作的迟到结果也覆盖不了新提示
 *     （各自的附件仍然只绑到各自的卡片上）；
 *   5 卸载后没有残留计时器，迟到结果既不复活提示、也不再写板面；
 *   6 取消选择的文案按卡片当前事实区分（没有附件 / 已有附件）；
 *   7 长文件名能看完整名称（title）且允许换行 / 断词；
 *   8 提示与菜单在同一悬浮列里各占一行，且整层不吃指针事件 —— 不遮挡
 *     「添加」按钮与工具栏上的其它操作。
 *
 * 模拟边界：附件服务（系统对话框 / 网络）全 mock；pinia store 是真的，
 * 但 commit 只保留「本地板面写入」这一段语义（不引入落盘 debounce 计时器 ——
 * 否则 getTimerCount 会把落盘计时器算成提示计时器）。做法与 irl-c-new-card.test.ts 一致。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  pickBrowserFile: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    pickBrowserFile: mocks.pickBrowserFile,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
  };
});

import AddMenu from "../AddMenu.vue";
// 样式契约按源码断言（jsdom 不做布局）：Vite 的 ?raw 在 vitest 里可用，与既有测试同一做法
import sfcSource from "../AddMenu.vue?raw";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, updateCard } from "../../../interactive/board";
import { publishBoardArea } from "../../../interactive/viewport";
import type { AttachmentRef } from "../../../services/attachments";
import type { BoardState } from "../../../interactive/types";

const SFC_SOURCE: string = sfcSource;

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

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

function mountMenu(): VueWrapper {
  wrapper = mount(AddMenu, { attachTo: document.body });
  return wrapper;
}

function statusEl() {
  return wrapper!.find('[data-im="add-material-status"]');
}

function statusText(): string {
  const el = wrapper!.find('[data-im="add-material-status-text"]');
  return el.exists() ? el.text() : "";
}

/** 从「添加」菜单里点一个材料入口；返回新建卡片的 id（附件要绑到它上面）。 */
// 2026-10-14：文件与图片合并成同一种材料 —— 添加菜单只剩「文件」一项，
// 选中的文件是不是图片由文件本身决定，所以这里统一走 add-file。
async function clickAdd(kind: "file"): Promise<string> {
  await wrapper!.get('[data-im="add-menu"]').trigger("click");
  await wrapper!.get('[data-im="add-' + kind + '"]').trigger("click");
  await flush();
  const cards = store.board!.cards;
  return cards[cards.length - 1].id;
}

function cardById(cardId: string) {
  const card = store.board!.cards.find((item) => item.id === cardId);
  if (!card) throw new Error("测试里的卡片不见了：" + cardId);
  return card;
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
  store = useInteractiveStore();
  store.board = emptyState("board_t");
  publishBoardArea(null);
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    void label;
    store.board = { ...next, boardId: store.boardId };
  });
  mocks.pickBrowserFile.mockResolvedValue(null);
  mocks.uploadAttachment.mockResolvedValue(attachment());
  mocks.waitUntilSettled.mockImplementation(async (item: AttachmentRef) => item);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  publishBoardArea(null);
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// 1. 成功：约 3 秒后只移除提示
// ---------------------------------------------------------------------------

describe("成功提示的生命周期", () => {
  it("成功约 3 秒后自动移除；卡片与 meta 里的附件关联全部保留", async () => {
    mountMenu();
    const base = vi.getTimerCount();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "报告.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_1", name: "报告.pdf" }));
    const cardId = await clickAdd("file");

    const el = statusEl();
    expect(el.exists(), "关联成功后没有任何提示").toBe(true);
    expect(el.classes(), "成功提示的色调不对").toContain("is-ok");
    expect(statusText()).toContain("报告.pdf");
    // 成功文案的诚实口径：附件已保存 + 板面关联还是本地改动（commit 不等于服务端已保存）
    expect(statusText(), "成功文案没有说清「提交后 QIO 才可见」").toContain("提交");
    expect(statusText(), "成功文案没有说清这是本地板面改动").toContain("本地");
    expect(
      statusText(),
      "成功文案声称服务端已经保存 / 同步了板面关联",
    ).not.toMatch(/服务端|已同步|已保存到/);
    // 恰好一个「成功自动移除」计时器
    expect(vi.getTimerCount(), "成功提示没有挂上自动移除计时器").toBe(base + 1);
    // 附件已经真的写进卡片 meta
    expect((cardById(cardId).meta.file as { attachmentId: string }).attachmentId).toBe("att_1");

    // 3 秒之内还在（成功更短更局部，但不是一闪而过）
    await vi.advanceTimersByTimeAsync(2900);
    expect(statusEl().exists(), "成功提示提前消失了").toBe(true);

    // 约 3 秒后自动移除
    await vi.advanceTimersByTimeAsync(200);
    expect(statusEl().exists(), "成功提示没有自动移除（0.2.5 回归）").toBe(false);
    expect(vi.getTimerCount(), "自动移除后计时器没有回收").toBe(base);

    // 移除的只是提示：卡片、meta 与文件名都还在
    expect(cardById(cardId).meta.name).toBe("报告.pdf");
    expect((cardById(cardId).meta.file as { attachmentId: string }).attachmentId).toBe("att_1");
    expect(cardById(cardId).deleted).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. 进行中：真实进度不被成功计时器藏掉
// ---------------------------------------------------------------------------

describe("进行中的真实进度", () => {
  it("等待选择 / 上传 / 处理三段的提示都一直可见，不会被任何计时器提前藏掉", async () => {
    mountMenu();
    const picking = deferred<File | null>();
    mocks.pickBrowserFile.mockReturnValue(picking.promise);

    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await flush();
    expect(statusText()).toContain("正在等待选择文件");
    // 把成功提示的时长推过去：进行中的提示也不能消失
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl().exists(), "等待用户选择时提示被藏掉了").toBe(true);

    const uploading = deferred<AttachmentRef>();
    mocks.uploadAttachment.mockReturnValue(uploading.promise);
    picking.resolve(new File(["x"], "很慢的季度报告.bin"));
    await flush();
    expect(statusText()).toContain("正在保存附件");
    expect(statusText()).toContain("很慢的季度报告.bin");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl().exists(), "上传期间提示被藏掉了").toBe(true);

    const settling = deferred<AttachmentRef>();
    mocks.waitUntilSettled.mockReturnValue(settling.promise);
    uploading.resolve(attachment({ id: "att_slow", name: "很慢的季度报告.bin", state: "prepared" }));
    await flush();
    expect(statusText(), "上传完成后的「处理中」没有真实进度").toContain("正在处理附件");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl().exists(), "服务端处理期间提示被藏掉了").toBe(true);

    settling.resolve(attachment({ id: "att_slow", name: "很慢的季度报告.bin" }));
    await flush();
    expect(statusText()).toContain("已关联附件");
  });
});

// ---------------------------------------------------------------------------
// 3. 失败：不自动消失 + 可关闭 + 明确重新操作
// ---------------------------------------------------------------------------

describe("失败提示", () => {
  it("不自动消失：有原因、有关闭入口、有真的会重跑的「重新选择文件」", async () => {
    mountMenu();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "大文件.bin"));
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("磁盘写满了"));
    await clickAdd("file");

    const el = statusEl();
    expect(el.exists(), "上传失败时没有任何提示").toBe(true);
    expect(el.classes(), "失败提示的色调不对").toContain("is-error");
    expect(el.text()).toContain("磁盘写满了");
    expect(statusText()).toContain("附件保存失败");
    expect(wrapper!.find('[data-im="add-material-dismiss"]').exists(), "失败提示没有关闭入口").toBe(true);
    const retry = wrapper!.find('[data-im="add-material-retry"]');
    expect(retry.exists(), "失败提示没有明确的重新操作入口").toBe(true);
    expect(retry.text()).toContain("重新选择文件");

    // 失败不会被任何计时器清掉（推过成功提示的 3 秒也不消失）
    await vi.advanceTimersByTimeAsync(30_000);
    expect(statusEl().exists(), "失败提示被自动清掉了").toBe(true);

    // 点「重新选择文件」：同一张卡片真的重新走 pickBrowserFile → 上传 → 关联
    const picksBefore = mocks.pickBrowserFile.mock.calls.length;
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "大文件.bin"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_retry", name: "大文件.bin" }));
    await retry.trigger("click");
    await flush();
    expect(mocks.pickBrowserFile.mock.calls.length, "重新选择没有重新发起选择").toBe(picksBefore + 1);
    expect(mocks.uploadAttachment, "重新选择没有真的再上传一次").toHaveBeenCalledTimes(2);
    expect(statusText()).toContain("已关联附件");
    expect(statusText()).toContain("大文件.bin");

    // 关闭入口真的关得掉（再来一次失败）
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("这次是没有权限"));
    await clickAdd("file");
    expect(statusEl().classes()).toContain("is-error");
    expect(statusText()).toContain("这次是没有权限");
    await wrapper!.find('[data-im="add-material-dismiss"]').trigger("click");
    await flush();
    expect(statusEl().exists(), "点了关闭提示还在").toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4. 连续操作：旧计时器 / 旧迟到结果都不许影响新提示
// ---------------------------------------------------------------------------

describe("连续两次添加", () => {
  it("旧的成功计时器不会清掉新提示", async () => {
    mountMenu();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "第一张.png"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_a", name: "第一张.png" }));
    await clickAdd("file");
    expect(statusText()).toContain("第一张.png");

    // 第一张的成功计时器 3000ms 后到期：先走 2000ms，再开始第二次添加
    await vi.advanceTimersByTimeAsync(2000);
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "第二张.png"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_b", name: "第二张.png" }));
    await clickAdd("file");
    expect(statusText(), "新提示没有出现").toContain("第二张.png");

    // 再走 1500ms：距第一次成功已 3500ms > 3000ms —— 旧计时器若没被清掉，新提示已经被它清掉了
    await vi.advanceTimersByTimeAsync(1500);
    expect(statusEl().exists(), "旧计时器把新提示清掉了").toBe(true);
    expect(statusText()).toContain("第二张.png");

    // 新提示仍按自己的 3 秒走完（从它出现起算）
    await vi.advanceTimersByTimeAsync(1600);
    expect(statusEl().exists(), "新提示没有自己的 3 秒生命周期").toBe(false);
  });

  it("旧操作的迟到结果不会覆盖 / 清掉新提示，但附件仍然绑到各自的卡片上", async () => {
    mountMenu();
    // 第一次：选好了文件，但上传一直不返回
    const latePick = deferred<File | null>();
    const lateUpload = deferred<AttachmentRef>();
    mocks.pickBrowserFile.mockReturnValueOnce(latePick.promise);
    mocks.uploadAttachment.mockReturnValueOnce(lateUpload.promise);
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await flush();
    const firstCardId = store.board!.cards[store.board!.cards.length - 1].id;
    latePick.resolve(new File(["x"], "迟到.pdf"));
    await flush();
    expect(statusText()).toContain("正在保存附件");

    // 第二次：完整走完并成功，提示归第二次所有
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "新的.png"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_new", name: "新的.png" }));
    const secondCardId = await clickAdd("file");
    expect(statusText()).toContain("新的.png");

    // 第一次的上传这才返回：迟到结果不许覆盖 / 清掉新提示
    lateUpload.resolve(attachment({ id: "att_late", name: "迟到.pdf" }));
    await flush();
    expect(statusText(), "旧操作的迟到结果覆盖了新提示").toContain("新的.png");
    expect(statusText(), "迟到文件名出现在新提示里").not.toContain("迟到.pdf");

    // 新提示仍按自己的 3 秒走完（迟到回调没有重置 / 取消它）
    await vi.advanceTimersByTimeAsync(2900);
    expect(statusEl().exists()).toBe(true);
    await vi.advanceTimersByTimeAsync(200);
    expect(statusEl().exists(), "迟到回调干扰了新提示的计时器").toBe(false);

    // 附件与目标卡片的绑定没有串：各自的 attachmentId 在自己的卡片上
    expect((cardById(firstCardId).meta.file as { attachmentId: string }).attachmentId).toBe("att_late");
    // 文件与图片合并后统一写 meta.file（第九个问题）
    expect((cardById(secondCardId).meta.file as { attachmentId: string }).attachmentId).toBe("att_new");
  });
});

// ---------------------------------------------------------------------------
// 5. 卸载：不残留，也不复活
// ---------------------------------------------------------------------------

describe("组件卸载", () => {
  it("卸载时把成功提示的计时器一起清掉", async () => {
    mountMenu();
    const base = vi.getTimerCount();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "报告.png"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_ok", name: "报告.png" }));
    await clickAdd("file");
    expect(vi.getTimerCount(), "成功提示应当恰好有一个自动移除计时器").toBe(base + 1);

    wrapper!.unmount();
    wrapper = null;
    expect(vi.getTimerCount(), "卸载后仍有残留计时器").toBe(base);
    await vi.advanceTimersByTimeAsync(3000);
    expect(vi.getTimerCount(), "卸载后仍被计时器唤醒").toBe(base);
  });

  it("卸载后迟到的选择结果：不复活提示、也不写板面", async () => {
    mountMenu();
    const base = vi.getTimerCount();
    const latePick = deferred<File | null>();
    mocks.pickBrowserFile.mockReturnValueOnce(latePick.promise);
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    await wrapper!.get('[data-im="add-file"]').trigger("click");
    await flush();
    const cardId = store.board!.cards[store.board!.cards.length - 1].id;

    wrapper!.unmount();
    wrapper = null;

    latePick.resolve(new File(["x"], "迟到.pdf"));
    await flush();
    await vi.advanceTimersByTimeAsync(3000);
    expect(vi.getTimerCount(), "迟到的结果又挂上了计时器（提示复活）").toBe(base);
    expect(mocks.uploadAttachment, "卸载后还去上传用户选的文件").not.toHaveBeenCalled();
    expect(cardById(cardId).meta.file, "卸载后的迟到回调还改了板面").toBeUndefined();
  });

  it("卸载后才落定的成功结果：不写板面、不复活提示", async () => {
    mountMenu();
    const base = vi.getTimerCount();
    const lateUpload = deferred<AttachmentRef>();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "迟到.png"));
    mocks.uploadAttachment.mockReturnValueOnce(lateUpload.promise);
    const cardId = await clickAdd("file");
    expect(statusText()).toContain("正在保存附件");

    wrapper!.unmount();
    wrapper = null;

    lateUpload.resolve(attachment({ id: "att_late", name: "迟到.png" }));
    await flush();
    expect(vi.getTimerCount(), "迟到的成功结果又挂了计时器（提示复活）").toBe(base);
    expect(mocks.waitUntilSettled, "卸载后还继续跟进附件状态").not.toHaveBeenCalled();
    expect(cardById(cardId).meta.file, "卸载后的迟到结果还写了板面").toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 6. 取消选择：文案必须符合卡片当前事实
// ---------------------------------------------------------------------------

describe("用户取消选择", () => {
  it("卡片没有附件：如实说「还没有关联已保存的附件，读不到正文」", async () => {
    mountMenu();
    mocks.pickBrowserFile.mockResolvedValue(null);
    const cardId = await clickAdd("file");

    const el = statusEl();
    expect(el.exists(), "取消选择后没有任何说明").toBe(true);
    expect(el.classes()).toContain("is-error");
    expect(statusText()).toContain("没有选择文件");
    expect(statusText()).toContain("还没有关联已保存的附件");
    expect(statusText()).toContain("读不到它的正文");
    // 取消不是失败：卡片没有被改，也没有凭空多出附件
    expect(cardById(cardId).meta.file).toBeUndefined();
    // 取消也不该被计时器清掉（用户要能看见自己取消了什么）
    await vi.advanceTimersByTimeAsync(10_000);
    expect(statusEl().exists(), "取消说明被自动清掉了").toBe(true);
  });

  it("卡片已有附件：如实说「仍然关联着原有附件」，不许说没有", async () => {
    mountMenu();
    // 第一次：先失败，拿到「重新选择文件」入口（同一张卡片的重跑入口）
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "原有的.png"));
    mocks.uploadAttachment.mockRejectedValueOnce(new Error("第一次没成功"));
    const cardId = await clickAdd("file");
    expect(statusEl().classes()).toContain("is-error");

    /**
     * 这张卡片在进入本次关联之前就已经有附件（板面自带 / 卡片自己的关联入口留下的真实状态）。
     * 「添加」入口只对刚建的卡片走第一次关联，所以这个前置状态在这里直接写进板面。
     */
    store.board = updateCard(store.board!, cardId, {
      meta: { ...cardById(cardId).meta, file: { name: "原有的.png", attachmentId: "att_old" } },
    });

    // 同一张卡片再走一次：这次用户取消
    mocks.pickBrowserFile.mockResolvedValue(null);
    await wrapper!.find('[data-im="add-material-retry"]').trigger("click");
    await flush();

    expect(statusText()).toContain("没有选择文件");
    expect(statusText(), "原始附件被说成不存在").toContain("仍然关联着原有附件");
    expect(statusText()).toContain("原有的.png");
    expect(statusText(), "卡片已有附件却还在说「还没有关联」").not.toContain("还没有关联");
    // 原有附件事实没被取消操作删掉
    expect((cardById(cardId).meta.file as { attachmentId: string }).attachmentId).toBe("att_old");
  });
});

// ---------------------------------------------------------------------------
// 7. 长文件名 + 不遮挡主要操作
// ---------------------------------------------------------------------------

describe("长文件名与布局", () => {
  it("长文件名：完整名称可查看（title）且允许换行 / 断词", async () => {
    mountMenu();
    const longName = "2026年第三季度华东区供应链风险与合规审计报告最终版（含附件清单、批注与修订说明）.pdf";
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], longName));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_long", name: longName }));
    await clickAdd("file");

    const el = statusEl();
    expect(el.exists()).toBe(true);
    expect(el.attributes("title") ?? "", "提示上没有可查看完整名称的 title").toContain(longName);
    expect(statusText(), "提示里没有完整文件名").toContain(longName);
    // 样式护栏（jsdom 不做布局）：提示必须允许换行 / 断词，而不是把长名字裁掉
    expect(cssRule(".add-status"), "提示缺少长文件名换行样式").toContain("overflow-wrap: anywhere");
    expect(cssRule(".add-status"), "提示缺少断词样式").toContain("word-break: break-word");
    expect(cssRule(".status-text"), "文本区不能换行 / 断词").toContain("overflow-wrap: anywhere");
  });

  it("提示与菜单在同一悬浮列里各占一行，整层不吃指针事件（不遮挡「添加」按钮与其它操作）", async () => {
    mountMenu();
    mocks.pickBrowserFile.mockResolvedValue(new File(["x"], "报告.pdf"));
    mocks.uploadAttachment.mockResolvedValue(attachment({ id: "att_1", name: "报告.pdf" }));
    await clickAdd("file");
    // 菜单与提示同时在：两者必须排开（不是互相覆盖的绝对定位）
    await wrapper!.get('[data-im="add-menu"]').trigger("click");
    const float = wrapper!.get('[data-im="add-float"]');
    expect(float.find('[data-im="add-material-status"]').exists(), "菜单展开时提示消失了").toBe(true);
    expect(float.find('[data-im="add-menu-list"]').exists()).toBe(true);
    const status = float.get('[data-im="add-material-status"]');
    const menu = float.get('[data-im="add-menu-list"]');
    const children = Array.from(float.element.children);
    const statusIndex = children.indexOf(status.element);
    const menuIndex = children.indexOf(menu.element);
    expect(statusIndex, "提示没有和菜单排在同一列里").toBeGreaterThanOrEqual(0);
    expect(menuIndex, "提示与菜单互相覆盖（菜单不是独立的兄弟行）").toBeGreaterThan(statusIndex);
    expect(menu.find('[data-im="add-material-status"]').exists(), "提示被嵌进了菜单里").toBe(false);

    // 样式契约：悬浮层整体 pointer-events: none，菜单与「重新选择 / 关闭」各自打开 ——
    // 提示因此不可能挡住「添加」按钮或工具栏上的其它操作
    expect(cssRule(".add-float"), "悬浮层会挡住下面的操作").toContain("pointer-events: none");
    expect(cssRule(".add-pop"), "菜单点不动了").toContain("pointer-events: auto");
    expect(cssRule(".status-action"), "失败提示上的按钮点不动").toContain("pointer-events: auto");
  });
});
