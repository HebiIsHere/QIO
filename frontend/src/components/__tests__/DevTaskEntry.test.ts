/**
 * 「有 N 个工具开发任务没做完」这一行。
 *
 * 回归的是这个真实缺口：任务只在模型那一轮的工具调用里出现过，模型不提，
 * 刷新或重启之后界面上就再也找不到它 —— 用户不知道还有一件事没做完。
 *
 * 后半部分回归「放弃开发」：这是一个**终态**动作，界面只以后端确认为准 ——
 * 不许乐观移除、不许把「正在执行」说成已放弃、提交期间不许发第二个请求；
 * 同时清单要能滚（30 项任务 + 长需求文字时仍然好用）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    revokeDevAuthorization: vi.fn(async () => ({ revoked: true })),
    abandonDevTask: vi.fn(async () => ({
      ok: true,
      status: "abandoned",
      message: "已经放弃这项开发。",
      revoked: true,
      invalidated_approvals: 0,
      can_stop: false,
      task: null,
    })),
  },
}));

const sendTurn = api.sendTurn as unknown as ReturnType<typeof vi.fn>;
const getDevTasks = api.getDevTasks as unknown as ReturnType<typeof vi.fn>;
const listDevAuthorizations = api.listDevAuthorizations as unknown as ReturnType<typeof vi.fn>;
const revokeDevAuthorization = api.revokeDevAuthorization as unknown as ReturnType<typeof vi.fn>;
const abandonDevTask = api.abandonDevTask as unknown as ReturnType<typeof vi.fn>;

type Row = Record<string, unknown>;

function task(overrides: Row = {}): Row {
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

function authorization(overrides: Row = {}) {
  return {
    task_id: "ws_aaaaaaaaaaaa",
    request: "做一个求和工具",
    submitted: false,
    executor: "subprocess",
    isolated: false,
    policy_fingerprint: "p1",
    capabilities: ["受限子进程"],
    filesystem: [],
    network: false,
    network_allow: [],
    credentials: ["weather_key"],
    granted_at: "2026-09-30T10:05:00+00:00",
    ...overrides,
  };
}

/**
 * 一个最小但**有状态**的后端替身：
 * - `GET /api/dev/tasks` 返回当前行（放弃成功的行带 `abandoned: true`）；
 * - `POST …/abandon` 成功时真的把那一行标成已放弃。
 *
 * 这样「放弃成功 → 刷新之后仍然不出现」是被真正验证过的，
 * 而不是靠 mock 直接返回一份写死的列表。
 */
function makeBackend(tasks: Row[]) {
  const rows = tasks.map((item) => ({ ...item }));
  const abandon = async (taskId: string) => {
    const row = rows.find((item) => item.id === taskId);
    if (!row) {
      return {
        ok: false,
        status: "not_found",
        message: `找不到这个开发任务：${taskId}`,
        revoked: false,
        invalidated_approvals: 0,
        can_stop: false,
        task: null,
      };
    }
    row.abandoned = true;
    row.abandoned_at = "2026-10-04T00:00:00+00:00";
    row.phase = "abandoned";
    return {
      ok: true,
      status: "abandoned",
      message: "已经放弃这项开发，它不会再执行、也不会被注册。",
      revoked: true,
      invalidated_approvals: 0,
      can_stop: false,
      task: { ...row },
    };
  };
  getDevTasks.mockImplementation(async () => ({ tasks: rows.map((row) => ({ ...row })) }));
  abandonDevTask.mockImplementation(abandon);
  return { rows, abandon };
}

function setup(tasks: Row[] = []) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const { rows, abandon } = makeBackend(tasks);
  session.devTasks = rows as never;
  const wrapper = mount(DevTaskEntry, { global: { plugins: [pinia] } });
  return { session, wrapper, rows, abandon };
}

type Wrapper = ReturnType<typeof setup>["wrapper"];

/** 展开清单（已经展开就不再点，避免把它收起）→ 点某一行的「放弃开发」→ 在确认层里点确认。 */
async function abandonRow(wrapper: Wrapper, rowIndex = 0) {
  if (!wrapper.find(".dev-task-panel").exists()) {
    await wrapper.find(".dev-task-entry").trigger("click");
  }
  await wrapper.findAll(".dev-task-abandon")[rowIndex].trigger("click");
  const buttons = wrapper.findAll(".qio-confirm__actions button");
  await buttons[buttons.length - 1].trigger("click");
  await flushPromises();
}

const SOURCE = () =>
  readFileSync(resolve(process.cwd(), "src/components/DevTaskEntry.vue"), "utf8");

beforeEach(() => {
  vi.clearAllMocks();
  sendTurn.mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "t1",
    status: "accepted",
    topic_id: null,
  });
  listDevAuthorizations.mockResolvedValue({ authorizations: [] });
  revokeDevAuthorization.mockResolvedValue({ revoked: true });
  // reset 先清掉上一个用例可能留下的 once 实现（避免串场）
  abandonDevTask.mockReset();
  abandonDevTask.mockResolvedValue({
    ok: true,
    status: "abandoned",
    message: "已经放弃这项开发。",
    revoked: true,
    invalidated_approvals: 0,
    can_stop: false,
    task: null,
  });
  getDevTasks.mockResolvedValue({ tasks: [] });
});

describe("未完成开发任务的入口", () => {
  it("没有未完成任务时不显示这一行", () => {
    const { wrapper } = setup([]);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("有 1 个未完成任务时只亮出一行，不自动展开清单", () => {
    const { wrapper } = setup([task()]);

    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个工具开发任务没做完");
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
  });

  it("已提交的任务不算「没做完」，不出现也不计数", () => {
    const { wrapper } = setup([
      task({ id: "ws_done", request: "已经注册好的工具", phase: "ready", submitted: true, test_passed: true }),
    ]);

    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("已放弃的任务也不算「没做完」（后端列表会带回它，界面按事实过滤）", () => {
    const { wrapper } = setup([
      task({ id: "ws_gone", request: "已经放弃的任务", abandoned: true, abandoned_at: "2026-10-01T00:00:00+00:00", phase: "abandoned" }),
    ]);

    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
  });

  it("点这一行才展开只读清单，并说清证据对不上当前内容", async () => {
    const { wrapper } = setup([
      task({ id: "ws_stale", request: "把文件转成 CSV", test_passed: true, test_evidence_current: false }),
      task({ id: "ws_none", request: "抓一个网页", test_passed: null, phase: "building" }),
    ]);

    await wrapper.find(".dev-task-entry").trigger("click");

    const panel = wrapper.find(".dev-task-panel");
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("把文件转成 CSV");
    expect(panel.text()).toContain("测试通过，但文件后来改过，结论不算数");
    expect(panel.text()).toContain("还没跑过测试");
    expect(panel.text()).toContain("正在写代码");
    expect(panel.text()).toContain("2026-09-30");
  });

  it("「继续开发」把任务 id 与当前状态交给模型，然后收起清单", async () => {
    const { wrapper } = setup([task({ id: "ws_resume0001", request: "做一个求和工具" })]);
    await wrapper.find(".dev-task-entry").trigger("click");

    await wrapper.find(".dev-task-resume").trigger("click");
    await flushPromises();

    expect(sendTurn).toHaveBeenCalledTimes(1);
    const message = String(sendTurn.mock.calls[0]?.[0] ?? "");
    expect(message).toContain("ws_resume0001");
    expect(message).toContain("做一个求和工具");
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
  });

  it("交给模型失败时如实说明，不假装已经交给它", async () => {
    sendTurn.mockRejectedValueOnce(new Error("后端没接上"));
    const { wrapper } = setup([task()]);
    await wrapper.find(".dev-task-entry").trigger("click");

    await wrapper.find(".dev-task-resume").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-panel").exists()).toBe(true);
    expect(wrapper.find(".dev-task-error").text()).toContain("没能把这件事交给模型");
  });

  it("展开清单时查一次授权范围，并说清「同意了什么」", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    const { wrapper } = setup([task({ authorized: true })]);

    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    expect(listDevAuthorizations).toHaveBeenCalledTimes(1);
    const scope = wrapper.find(".dev-task-scope").text();
    expect(scope).toContain("受限子进程");
    expect(scope).toContain("weather_key");
  });

  it("没有授权时不显示范围那一行", async () => {
    const { wrapper } = setup([task()]);

    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-scope").exists()).toBe(false);
  });

  it("撤销授权：调后端、这一行的授权提示消失", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    const { session, wrapper } = setup([task({ authorized: true })]);
    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    await wrapper.find(".dev-task-revoke").trigger("click");
    await flushPromises();

    expect(revokeDevAuthorization).toHaveBeenCalledWith("ws_aaaaaaaaaaaa");
    expect(wrapper.find(".dev-task-scope").exists()).toBe(false);
    expect(session.devAuthorizations).toEqual([]);
  });

  it("撤销失败时如实说明，不假装已经收回", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    revokeDevAuthorization.mockRejectedValueOnce(new Error("后端没接上"));
    const { wrapper } = setup([task({ authorized: true })]);
    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    await wrapper.find(".dev-task-revoke").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-scope").exists()).toBe(true);
    expect(wrapper.find(".dev-task-error").text()).toContain("撤销");
  });
});

describe("放弃开发：确认层说清后果", () => {
  it("点「放弃开发」先展开 QConfirm：结束开发 / 从未完成列表移除 / 不删已注册工具，确认按钮是危险档", async () => {
    const { wrapper } = setup([task()]);
    await wrapper.find(".dev-task-entry").trigger("click");

    expect(wrapper.find(".qio-confirm").exists()).toBe(false);

    await wrapper.find(".dev-task-abandon").trigger("click");

    const confirm = wrapper.find(".qio-confirm");
    expect(confirm.exists()).toBe(true);
    const text = confirm.text();
    // 先点名是哪一项（清单可能很长，只说「这项开发」对不上行）
    expect(text).toContain("做一个求和工具");
    expect(text).toContain("结束这项开发");
    expect(text).toContain("从未完成列表移除");
    expect(text).toContain("工作区文件与记录保留");
    expect(text).toContain("不删除已注册的工具");
    expect(text).toContain("收回这个任务的执行授权");
    expect(text).toContain("没回答的确认会失效");

    const buttons = confirm.findAll(".qio-confirm__actions button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].text()).toBe("取消");
    expect(buttons[1].text()).toBe("放弃开发");
    // danger 档：中性实心 danger-solid，不是品牌主操作色
    expect(buttons[1].classes()).toContain("danger-solid");

    // 还没确认：一个请求都不许发出去
    expect(abandonDevTask).not.toHaveBeenCalled();
  });

  it("同一时间只有一个确认层：点另一行会换过去，不会叠两个", async () => {
    const { wrapper } = setup([
      task({ id: "ws_a", request: "任务甲" }),
      task({ id: "ws_b", request: "任务乙" }),
    ]);
    await wrapper.find(".dev-task-entry").trigger("click");

    const buttons = wrapper.findAll(".dev-task-abandon");
    await buttons[0].trigger("click");
    await buttons[1].trigger("click");

    expect(wrapper.findAll(".qio-confirm")).toHaveLength(1);
  });

  it("取消确认：不调接口、条目还在", async () => {
    const { wrapper } = setup([task()]);
    await wrapper.find(".dev-task-entry").trigger("click");
    await wrapper.find(".dev-task-abandon").trigger("click");

    await wrapper.findAll(".qio-confirm__actions button")[0].trigger("click");
    await flushPromises();

    expect(abandonDevTask).not.toHaveBeenCalled();
    expect(wrapper.find(".qio-confirm").exists()).toBe(false);
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
  });
});

describe("放弃开发：以后端确认为准", () => {
  it("确认成功：只调一次接口、这一条消失、入口数量从 2 变 1", async () => {
    const { session, wrapper } = setup([
      task({ id: "ws_a", request: "任务甲" }),
      task({ id: "ws_b", request: "任务乙" }),
    ]);
    await wrapper.find(".dev-task-entry").trigger("click");
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 2 个");

    await abandonRow(wrapper, 0);

    expect(abandonDevTask).toHaveBeenCalledTimes(1);
    expect(abandonDevTask).toHaveBeenCalledWith("ws_a");
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-panel").text()).not.toContain("任务甲");
    expect(wrapper.find(".dev-task-panel").text()).toContain("任务乙");
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
    expect(wrapper.find(".dev-task-panel").exists()).toBe(true);
    // 刷新（后端权威列表）之后仍然不再出现：不是靠本地删掉装出来的
    expect(session.unfinishedDevTasks.map((item) => item.id)).toEqual(["ws_b"]);
  });

  it("放弃成功同时去掉这个任务的授权行", async () => {
    const { session, wrapper, rows } = setup([task({ id: "ws_aaaaaaaaaaaa", authorized: true })]);
    // 授权列表也跟着后端状态走：任务一旦放弃，它就不再出现在授权里
    listDevAuthorizations.mockImplementation(async () => ({
      authorizations: rows.some((row) => row.id === "ws_aaaaaaaaaaaa" && row.abandoned)
        ? []
        : [authorization()],
    }));

    await abandonRow(wrapper, 0);

    expect(session.devAuthorizations).toEqual([]);
    expect(wrapper.find(".dev-task-scope").exists()).toBe(false);
  });

  it("最后一项放弃成功：面板收起、入口整行消失，不留空面板", async () => {
    const { wrapper } = setup([task({ id: "ws_only", request: "唯一没做完的任务" })]);

    await abandonRow(wrapper, 0);

    expect(abandonDevTask).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
    expect(wrapper.find(".dev-task-list").exists()).toBe(false);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
    expect(wrapper.find(".dev-task-wrap").exists()).toBe(false);
  });

  it("后端说正在执行（running）：条目留下、原因原样显示、可以重试且重试会再发请求", async () => {
    const runningMessage =
      "这个任务正在执行（正在跑测试或正在提交）。现在没有只停止这一个任务的能力：请先停止当前执行，再放弃开发。";
    const { wrapper } = setup([task({ id: "ws_run", request: "正在跑的任务" })]);
    abandonDevTask.mockResolvedValueOnce({
      ok: false,
      status: "running",
      message: runningMessage,
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: null,
    });

    await abandonRow(wrapper, 0);

    // 条目还在，数量没变，也没有被说成「已放弃」
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
    expect(wrapper.text()).not.toContain("已放弃");

    const alert = wrapper.find(".dev-task-error");
    expect(alert.attributes("role")).toBe("alert");
    // 后端的人话原样透出：说清「没有只停止这一个任务的能力」「先停止当前执行」
    expect(alert.text()).toContain("这个任务正在执行");
    expect(alert.text()).toContain("请先停止当前执行，再放弃开发");
    expect(alert.text()).not.toContain("已经放弃");
    // 失败之后能对上是哪一项没放弃
    expect(wrapper.find(".dev-task-error-which").text()).toContain("正在跑的任务");

    // 按钮恢复可点（不是「正在放弃…」），重试会真的再发一次请求
    const again = wrapper.find(".dev-task-abandon");
    expect(again.attributes("disabled")).toBeUndefined();
    expect(again.text()).toContain("放弃开发");

    await abandonRow(wrapper, 0);

    expect(abandonDevTask).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("网络异常：不假装已放弃 —— 条目还在、原因可见、按钮可点", async () => {
    const { wrapper } = setup([task({ id: "ws_net", request: "网断了的任务" })]);
    abandonDevTask.mockRejectedValueOnce(new Error("后端没接上"));

    await abandonRow(wrapper, 0);

    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个");
    const alert = wrapper.find(".dev-task-error");
    expect(alert.attributes("role")).toBe("alert");
    expect(alert.text()).toContain("没能放弃这项开发");
    expect(alert.text()).toContain("后端没接上");
    expect(alert.text()).toContain("可以再试一次");
    expect(wrapper.find(".dev-task-abandon").attributes("disabled")).toBeUndefined();
    expect(wrapper.find(".dev-task-abandon").text()).toContain("放弃开发");
  });

  it("后端说 not_found：如实显示，不把条目悄悄删掉", async () => {
    const { wrapper } = setup([task({ id: "ws_missing" })]);
    abandonDevTask.mockResolvedValueOnce({
      ok: false,
      status: "not_found",
      message: "找不到这个开发任务：ws_missing",
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: null,
    });

    await abandonRow(wrapper, 0);

    expect(wrapper.find(".dev-task-error").text()).toContain("找不到这个开发任务");
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(1);
  });

  it("提交期间重复点击：接口只被调用一次，按钮禁用并显示「正在放弃…」", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { wrapper, abandon } = setup([task({ id: "ws_slow", request: "慢慢放弃的任务" })]);
    // 第一次请求卡在「后端还没回」的状态：真正的状态变更仍然由有状态后端做，
    // 所以请求放行之后「刷新即消失」也是真验证过的。
    abandonDevTask.mockImplementationOnce(async (taskId: string) => {
      await gate;
      return abandon(taskId);
    });
    await wrapper.find(".dev-task-entry").trigger("click");
    await wrapper.find(".dev-task-abandon").trigger("click");

    const confirmButton = wrapper.findAll(".qio-confirm__actions button")[1];
    await confirmButton.trigger("click");

    // 三路重复点击：确认按钮再点一次、这一行按钮再点一次（此时已禁用）
    await confirmButton.trigger("click");
    await wrapper.find(".dev-task-abandon").trigger("click");
    await nextTick();

    expect(abandonDevTask).toHaveBeenCalledTimes(1);

    const rowButton = wrapper.find(".dev-task-abandon");
    expect(rowButton.text()).toContain("正在放弃…");
    expect(rowButton.attributes("disabled")).toBeDefined();

    release();
    await flushPromises();

    // 请求结束后条目确实被移除了（按钮不会永久停在「正在放弃…」）
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });
});

describe("放弃开发：列表滚动与窄窗口", () => {
  it("30 项任务都渲染出来，列表是键盘可达的滚动容器", async () => {
    const many = Array.from({ length: 30 }, (_, i) =>
      task({ id: `ws_${String(i).padStart(3, "0")}`, request: `第 ${i + 1} 项任务` }),
    );
    const { wrapper } = setup(many);
    await wrapper.find(".dev-task-entry").trigger("click");

    const list = wrapper.find(".dev-task-list");
    expect(list.exists()).toBe(true);
    // 高度上限不丢条目：30 项都在 DOM 里，靠滚动看到最后一项
    expect(wrapper.findAll(".dev-task-row")).toHaveLength(30);
    expect(list.attributes("tabindex")).toBe("0");
    expect(list.attributes("aria-label")).toContain("未完成的工具开发任务");
    expect(list.attributes("aria-label")).toContain("30");
    // 最后一项的按钮完整存在（滚动到即可点）
    const lastRow = wrapper.findAll(".dev-task-row")[29];
    expect(lastRow.find(".dev-task-abandon").exists()).toBe(true);
    expect(lastRow.find(".dev-task-resume").exists()).toBe(true);

    const src = SOURCE();
    expect(src).toMatch(/\.dev-task-list\s*\{[^}]*overflow-y:\s*auto/s);
    expect(src).toMatch(/\.dev-task-list\s*\{[^}]*overscroll-behavior:\s*contain/s);
    // 列表底部留白：滚到底时最后一项的按钮完整可点
    expect(src).toMatch(/\.dev-task-list\s*\{[^}]*padding:[^;]*12px[^;]*;/s);
  });

  it("失败原因在滚动容器之外：任何滚动位置都看得见（DOM 相对位置断言）", async () => {
    const many = Array.from({ length: 30 }, (_, i) =>
      task({ id: `ws_${String(i).padStart(3, "0")}`, request: `第 ${i + 1} 项任务` }),
    );
    const { wrapper } = setup(many);
    abandonDevTask.mockResolvedValueOnce({
      ok: false,
      status: "running",
      message: "这个任务正在执行（正在跑测试或正在提交）。现在没有只停止这一个任务的能力：请先停止当前执行，再放弃开发。",
      revoked: false,
      invalidated_approvals: 0,
      can_stop: false,
      task: null,
    });

    await abandonRow(wrapper, 0);

    const list = wrapper.find(".dev-task-list").element;
    const panel = wrapper.find(".dev-task-panel").element;
    const error = wrapper.find(".dev-task-error").element;

    // 不在滚动容器里，但在同一个面板里
    expect(list.contains(error)).toBe(false);
    expect(panel.contains(error)).toBe(true);
    // 文档顺序：错误区排在列表之后（滚列表不会把它滚出视野）
    expect(list.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("长需求文字：换行不掉字、面板高度上限随窗口变化（样式契约）", () => {
    const src = SOURCE();

    // 长需求文字 / 授权范围都不撑出横向滚动
    expect(src).toMatch(/\.dev-task-request\s*\{[^}]*overflow-wrap:\s*anywhere/s);
    expect(src).toMatch(/word-break:\s*break-word/);
    // 确认层的说明里也会出现需求原文：它一样要能断行，且不许把面板撑宽
    expect(src).toMatch(
      /\.qio-confirm\s+:deep\(\.qio-confirm__detail\)\s*\{[^}]*overflow-wrap:\s*anywhere/s,
    );
    expect(src).toMatch(/\.qio-confirm\s*\{\s*min-width:\s*0/s);
    // 动作行会换行，窄窗口下按钮不会被挤出可见区
    expect(src).toMatch(/\.dev-task-actions\s*\{[^}]*flex-wrap:\s*wrap/s);

    // 面板高度上限：vh + calc + 顶部提示条让位变量，不是写死的像素
    const panel = src.match(/\.dev-task-panel\s*\{[\s\S]*?\n\}/);
    expect(panel).not.toBeNull();
    const block = panel![0];
    expect(block).toContain("--qio-top-notes-offset");
    expect(block).toMatch(/max-height:\s*calc\(\s*100vh/);
    expect(block).toMatch(/--dev-panel-reserve/);
    expect(block).not.toMatch(/max-height:\s*\d+px/);

    // 颜色一律走 token，不允许硬编码色值
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(src).not.toMatch(/\brgba?\(/);
  });

  it("窄窗口下长需求文字不产生横向溢出：行是 min-width:0 的纵向列，靠换行而不是撑宽", () => {
    const src = SOURCE();
    expect(src).toMatch(/\.dev-task-row\s*\{[^}]*min-width:\s*0/s);
    expect(src).toMatch(/\.dev-task-list\s*\{[^}]*min-height:\s*0/s);
    // 面板宽度跟随可用宽度，不写死
    expect(src).toMatch(/width:\s*min\(560px,\s*calc\(100vw\s*-\s*32px\)\)/);
  });
});
