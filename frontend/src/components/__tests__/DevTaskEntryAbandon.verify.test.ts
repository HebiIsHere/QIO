/**
 * 独立验证（验证方维护，与实现方的 DevTaskEntry.test.ts 互不覆盖）：
 * 「放弃开发」这一行的界面行为。
 *
 * 断言只依据冻结契约 `_ABANDON-CONTRACT.md` §8.1：
 * - 每行一个「放弃开发」按钮，用已有的 QConfirm（inline / danger /「放弃开发」/「取消」）；
 * - 文案必须说清三件事：从未完成列表移除、工作区文件保留且不删除已注册工具、收回执行授权；
 * - 同一时间只展开一个确认层；提交期间禁用并显示「正在放弃…」，重复点击不发第二个请求；
 * - 成功 → 该行消失、计数变化；移除最后一项 → 面板收起、入口消失；
 * - 失败 → 条目留在列表里、就地显示原因（role="alert" / .dev-task-error）、可重试；
 *   status === "running" 原样显示后端 message，不能说成已放弃。
 *
 * 说明：jsdom 不做真实布局（滚动、横向溢出、max-height 随窗口变化的实测在
 * `scripts/ui-catalog/dev-abandon.mjs` 的 Playwright 采集里做）。这里只断言
 * DOM 结构层面的硬要求（错误区在滚动容器之外、滚动容器可聚焦且有 aria-label）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DevTaskEntry from "../DevTaskEntry.vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({
      ok: true,
      accepted: true,
      turn_id: "t1",
      status: "accepted",
      topic_id: null,
    })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    revokeDevAuthorization: vi.fn(async () => ({ revoked: true })),
    abandonDevTask: vi.fn(),
  },
}));

const abandonDevTask = api.abandonDevTask as unknown as ReturnType<typeof vi.fn>;
const getDevTasks = api.getDevTasks as unknown as ReturnType<typeof vi.fn>;
const listDevAuthorizations = api.listDevAuthorizations as unknown as ReturnType<typeof vi.fn>;

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: "ws_aaaaaaaaaaaa",
    request: "做一个求和工具",
    phase: "testing_failed",
    submitted: false,
    test_passed: false,
    test_evidence_current: true,
    updated_at: "2026-09-30T10:00:00+00:00",
    authorized: false,
    abandoned: false,
    abandoned_at: null,
    ...overrides,
  };
}

function abandoned(overrides: Record<string, unknown> = {}) {
  return task({
    id: "ws_bbbbbbbbbbbb",
    request: "已经放弃的",
    phase: "abandoned",
    abandoned: true,
    abandoned_at: "2026-10-01T00:00:00+00:00",
    ...overrides,
  });
}

function setup(tasks: Record<string, unknown>[] = []) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.devTasks = tasks as never;
  const wrapper = mount(DevTaskEntry, { global: { plugins: [pinia] } });
  return { session, wrapper };
}

async function openPanel(wrapper: ReturnType<typeof setup>["wrapper"]) {
  await wrapper.find(".dev-task-entry").trigger("click");
  await flushPromises();
}

/** 点开某一行的确认层（第 index 行） */
async function openConfirm(wrapper: ReturnType<typeof setup>["wrapper"], index = 0) {
  await wrapper.findAll(".dev-task-abandon")[index].trigger("click");
  await flushPromises();
}

function confirmButton(wrapper: ReturnType<typeof setup>["wrapper"]) {
  return wrapper.find(".qio-confirm button.danger-solid");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  getDevTasks.mockResolvedValue({ tasks: [] });
  listDevAuthorizations.mockResolvedValue({ authorizations: [] });
  abandonDevTask.mockResolvedValue({
    ok: true,
    status: "abandoned",
    message: "已经放弃这项开发。工作区文件与记录都保留着。",
    revoked: true,
    invalidated_approvals: 0,
    can_stop: false,
    task: task({ abandoned: true }),
  });
});

describe("放弃开发（独立验证）", () => {
  it("每一行都有「放弃开发」按钮", async () => {
    const { wrapper } = setup([
      task({ id: "ws_111111111111" }),
      task({ id: "ws_222222222222" }),
    ]);
    await openPanel(wrapper);

    expect(wrapper.findAll(".dev-task-abandon")).toHaveLength(2);
  });

  it("点按钮先出确认层，文案说清三件事（移除 / 文件与工具保留 / 收回授权）", async () => {
    const { wrapper } = setup([task()]);
    await openPanel(wrapper);

    expect(wrapper.find(".qio-confirm").exists()).toBe(false);
    await openConfirm(wrapper);

    const confirm = wrapper.find(".qio-confirm");
    expect(confirm.exists()).toBe(true);
    const text = confirm.text();
    // ① 结束这项开发，并从未完成列表移除
    expect(text).toContain("未完成");
    // ② 工作区文件与记录保留，不删除已注册的工具
    expect(text).toContain("保留");
    expect(text).toContain("不删除已注册的工具");
    // ③ 同时收回执行授权，没回答的确认会失效
    expect(text).toContain("授权");
    expect(text).toContain("失效");
    // 按钮层级：确认是危险实心档，取消在左
    expect(confirm.find("button.danger-solid").text()).toBe("放弃开发");
    expect(confirm.findAll("button")[0].text()).toBe("取消");
  });

  it("同一时间只展开一个确认层", async () => {
    const { wrapper } = setup([
      task({ id: "ws_111111111111", request: "第一个" }),
      task({ id: "ws_222222222222", request: "第二个" }),
    ]);
    await openPanel(wrapper);

    await openConfirm(wrapper, 0);
    await openConfirm(wrapper, 1);

    expect(wrapper.findAll(".qio-confirm")).toHaveLength(1);
    // 展开的是第二次点的那一行：它的确认层里能看到第二个任务的需求
    expect(wrapper.find(".qio-confirm").text()).toContain("第二个");
  });

  it("点「取消」什么也不做：不发请求、条目还在、确认层收起", async () => {
    const { wrapper } = setup([task()]);
    await openPanel(wrapper);
    await openConfirm(wrapper);

    const cancel = wrapper.find(".qio-confirm button:not(.danger-solid)");
    await cancel.trigger("click");
    await flushPromises();

    expect(abandonDevTask).not.toHaveBeenCalled();
    expect(wrapper.find(".qio-confirm").exists()).toBe(false);
    expect(wrapper.find(".dev-task-row").exists()).toBe(true);
  });

  it("确认后只发一次请求；成功后该行消失、计数减一", async () => {
    const { wrapper } = setup([
      task({ id: "ws_111111111111" }),
      task({ id: "ws_222222222222", request: "另一个" }),
    ]);
    await openPanel(wrapper);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 2 个");

    // 契约 §7：成功之后 store 会 await refreshDevTasks()「以后端为准」，
    // 所以替身必须给出放弃之后后端真实的清单（否则测的是替身，不是实现）。
    getDevTasks.mockResolvedValue({ tasks: [task({ id: "ws_222222222222", request: "另一个" })] });

    await openConfirm(wrapper, 0);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(abandonDevTask).toHaveBeenCalledTimes(1);
    expect(abandonDevTask).toHaveBeenCalledWith("ws_111111111111");
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
  });

  it("提交期间按钮禁用并显示「正在放弃…」，重复点击不发第二个请求", async () => {
    const pending = deferred<Record<string, unknown>>();
    abandonDevTask.mockReturnValue(pending.promise);
    const { wrapper } = setup([task()]);
    await openPanel(wrapper);
    await openConfirm(wrapper);

    const confirm = confirmButton(wrapper);
    await confirm.trigger("click");
    await flushPromises();

    // 还没拿到后端结论：按钮禁用 + 文案说明正在放弃
    const pendingBtn = wrapper.find(".dev-task-abandon");
    expect(pendingBtn.attributes("disabled")).toBeDefined();
    expect(pendingBtn.text()).toContain("正在放弃");
    // 再点一次（禁用态点不动，也不该发第二个请求）
    await pendingBtn.trigger("click");
    await confirm.trigger("click");
    await flushPromises();
    expect(abandonDevTask).toHaveBeenCalledTimes(1);

    pending.resolve({
      ok: true,
      status: "abandoned",
      message: "已经放弃",
      revoked: true,
      invalidated_approvals: 0,
      can_stop: false,
      task: task({ abandoned: true }),
    });
    await flushPromises();
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("后端拒绝（running）：条目还在、原样显示后端 message、不说成已放弃", async () => {
    abandonDevTask.mockResolvedValue({
      ok: false,
      status: "running",
      message: "这个任务正在执行（正在跑测试或正在提交）。现在没有只停止这一个任务的能力：请先停止当前执行，再放弃开发。",
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: task(),
    });
    const { wrapper } = setup([task({ id: "ws_111111111111" })]);
    await openPanel(wrapper);
    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    const error = wrapper.find(".dev-task-error");
    expect(error.exists()).toBe(true);
    expect(error.attributes("role")).toBe("alert");
    expect(error.text()).toContain("正在执行");
    expect(error.text()).toContain("先停止当前执行");
    expect(error.text()).not.toContain("已经放弃");
  });

  it("失败之后可以再点一次重试，第二次成功就移除", async () => {
    abandonDevTask
      .mockResolvedValueOnce({
        ok: false,
        status: "submitted",
        message: "这个任务已经做完、工具已经注册。",
        revoked: false,
        invalidated_approvals: 0,
        can_stop: false,
        task: task(),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: "abandoned",
        message: "已经放弃这项开发。",
        revoked: true,
        invalidated_approvals: 0,
        can_stop: false,
        task: task({ abandoned: true }),
      });
    const { wrapper } = setup([task({ id: "ws_111111111111" })]);
    await openPanel(wrapper);

    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();
    expect(wrapper.find(".dev-task-error").text()).toContain("已经做完");

    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(abandonDevTask).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("网络异常：如实说没放弃、条目还在、可以重试", async () => {
    abandonDevTask.mockRejectedValueOnce(new Error("后端没接上"));
    const { wrapper } = setup([task()]);
    await openPanel(wrapper);
    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    const error = wrapper.find(".dev-task-error");
    expect(error.exists()).toBe(true);
    expect(error.text().length).toBeGreaterThan(0);
    expect(error.text()).not.toContain("已经放弃");
  });

  it("移除最后一项时面板收起、入口整行消失", async () => {
    const { wrapper } = setup([task({ id: "ws_111111111111" })]);
    await openPanel(wrapper);
    expect(wrapper.find(".dev-task-panel").exists()).toBe(true);

    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
    expect(wrapper.find(".dev-task-wrap").exists()).toBe(false);
  });

  it("已放弃的任务不再算「没做完」：不计数、不出现", async () => {
    const { wrapper } = setup([abandoned()]);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("接口刷新返回的列表里带 abandoned 的行也不会重新出现", async () => {
    const { wrapper, session } = setup([task({ id: "ws_111111111111" })]);
    await openPanel(wrapper);

    // 放弃成功后 store 重新拉权威列表（契约 §7）：后端把已放弃的行**仍然列出来**
    // 并带 abandoned: true（§3.1），未完成视图必须按 !submitted && !abandoned 过滤掉它。
    session.devTasks = [
      task({ id: "ws_111111111111" }),
      task({ id: "ws_333333333333", request: "还没做的" }),
    ];
    getDevTasks.mockResolvedValue({
      tasks: [
        task({ id: "ws_111111111111", abandoned: true, phase: "abandoned" }),
        task({ id: "ws_333333333333", request: "还没做的" }),
      ],
    });
    await openConfirm(wrapper, 0);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
    expect(wrapper.text()).toContain("还没做的");
    expect(wrapper.text()).not.toContain("做一个求和工具");
  });

  it("错误提示区在滚动容器之外，滚动容器可聚焦且有 aria-label", async () => {
    abandonDevTask.mockResolvedValue({
      ok: false,
      status: "running",
      message: "这个任务正在执行。请先停止当前执行，再放弃开发。",
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: task(),
    });
    const longRequest = `${"很长的需求".repeat(60)}${"x".repeat(400)}`;
    const { wrapper } = setup([task({ id: "ws_111111111111", request: longRequest })]);
    await openPanel(wrapper);
    await openConfirm(wrapper);
    await confirmButton(wrapper).trigger("click");
    await flushPromises();

    const list = wrapper.find(".dev-task-list");
    expect(list.exists()).toBe(true);
    expect(list.attributes("tabindex")).toBe("0");
    expect(list.attributes("aria-label")).toBeTruthy();
    // 错误区必须在滚动容器之外：滚动到任何位置都能看到失败原因
    const error = wrapper.find(".dev-task-error");
    expect(list.element.contains(error.element)).toBe(false);
    expect(wrapper.find(".dev-task-panel").element.contains(error.element)).toBe(true);
    // 长需求文字原样渲染（换行/溢出由 CSS 负责，界面实测见 Playwright 采集）
    expect(list.element.textContent).toContain("x".repeat(400));
  });
});

describe("放弃开发的 store 语义（独立验证，契约 §7）", () => {
  function storeWith(tasks: Record<string, unknown>[]) {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.devTasks = tasks as never;
    return session;
  }

  it("后端确认成功之前不做乐观移除", async () => {
    const pending = deferred<Record<string, unknown>>();
    abandonDevTask.mockReturnValue(pending.promise);
    const session = storeWith([
      task({ id: "ws_111111111111" }),
      task({ id: "ws_222222222222" }),
    ]);

    const inFlight = session.abandonDevTask("ws_111111111111");
    await flushPromises();
    // 请求还在飞：一行都不能少（否则用户会以为已经放弃了）
    expect(session.devTasks.map((item) => item.id)).toEqual([
      "ws_111111111111",
      "ws_222222222222",
    ]);

    getDevTasks.mockResolvedValue({ tasks: [task({ id: "ws_222222222222" })] });
    pending.resolve({
      ok: true,
      status: "abandoned",
      message: "已经放弃",
      revoked: true,
      invalidated_approvals: 0,
      can_stop: false,
      task: task({ abandoned: true }),
    });
    const result = await inFlight;

    expect(result).toEqual({ ok: true, status: "abandoned", message: "已经放弃" });
    expect(session.devTasks.map((item) => item.id)).toEqual(["ws_222222222222"]);
  });

  it("后端拒绝时一行都不动，把 message 原样交回", async () => {
    abandonDevTask.mockResolvedValue({
      ok: false,
      status: "running",
      message: "这个任务正在执行。请先停止当前执行，再放弃开发。",
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: task(),
    });
    const session = storeWith([task({ id: "ws_111111111111" })]);

    const result = await session.abandonDevTask("ws_111111111111");

    expect(result.ok).toBe(false);
    expect(result.status).toBe("running");
    expect(result.message).toContain("先停止当前执行");
    expect(session.devTasks.map((item) => item.id)).toEqual(["ws_111111111111"]);
    expect(getDevTasks).not.toHaveBeenCalled();
  });

  it("网络异常不抛出：返回 ok:false 与一句人话，列表不清空", async () => {
    abandonDevTask.mockRejectedValueOnce(new Error("后端没接上"));
    const session = storeWith([task({ id: "ws_111111111111" })]);

    const result = await session.abandonDevTask("ws_111111111111");

    expect(result.ok).toBe(false);
    expect(result.status).toBe("network_error");
    expect(result.message.length).toBeGreaterThan(0);
    expect(session.devTasks.map((item) => item.id)).toEqual(["ws_111111111111"]);
  });
});
