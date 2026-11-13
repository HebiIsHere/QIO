/**
 * 保存模型：按组保存 / 取消 / 未保存标记 / 失败保留草稿 / 迟到的旧响应 / 即时保存回滚。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../../composables/floatingState";
import {
  chooseSelectOption,
  flushFrames,
  mountSettings,
  openCategory,
  sectionById,
  setViewportWidth,
} from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});

// 「模型与服务」是真实面板（不是占位）：把它的数据源换成确定性替身，
// 否则每个设置页用例都要跟随网络失败的时序。
vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  document.body.innerHTML = "";
  setViewportWidth(1280);
});

describe("记忆分段：按组保存", () => {
  it("改完先标「未保存」，保存时两个字段一起提交", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    // #16：无改动时不显示保存条（不摆一个常驻的「已保存」操作行）
    expect(memory.find('[data-settings-group="memory.fragment"]').exists()).toBe(false);

    await chooseSelectOption(memory, "自定义");
    const turns = memory.find('input[aria-label="自定义轮数"]');
    await turns.setValue("12");
    await nextTick();
    await memory.find('input[aria-label="单段长度目标"]').setValue("8000");
    await nextTick();

    expect(memory.find('[data-settings-group="memory.fragment"] [data-settings-dirty]').text()).toContain(
      "未保存",
    );

    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    expect(api.updateMemorySettings).toHaveBeenCalledWith({
      fragment_max_turns: 12,
      fragment_max_tokens: 8000,
    });
    // 保存真正成功后：保存条收起，并给一次短反馈
    expect(memory.find('[data-settings-group="memory.fragment"]').exists()).toBe(false);
    expect(memory.find(".set-notice.ok").text()).toContain("记忆分段已保存");
    w.unmount();
  });

  it("取消：草稿回到已保存值，也不发写请求", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    await memory.find('input[aria-label="单段长度目标"]').setValue("9000");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="cancel"]')
      .trigger("click");
    await nextTick();

    expect(
      (memory.find('input[aria-label="单段长度目标"]').element as HTMLInputElement).value,
    ).toBe("4096");
    // 取消后没有改动：保存条收起
    expect(memory.find('[data-settings-group="memory.fragment"]').exists()).toBe(false);
    expect(api.updateMemorySettings).not.toHaveBeenCalled();
    w.unmount();
  });

  it("显式保存失败：保留草稿并给出错误语义", async () => {
    const { api } = await import("../../../services/api");
    (api.updateMemorySettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("PUT /api/settings/memory -> 500: boom"),
    );
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    await memory.find('input[aria-label="单段长度目标"]').setValue("9000");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    const group = memory.find('[data-settings-group="memory.fragment"]');
    expect(group.find(".set-notice.err").text()).toContain("保存记忆分段失败");
    // 草稿保留（用户填的内容不能因为后端 500 就消失）
    expect(
      (memory.find('input[aria-label="单段长度目标"]').element as HTMLInputElement).value,
    ).toBe("9000");
    expect(group.find('[data-settings-dirty]').text()).toContain("未保存");
    w.unmount();
  });

  it("越界输入先被步进器收口到范围内（30），而不是把非法值发出去", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    await chooseSelectOption(memory, "自定义");
    const turns = memory.find('input[aria-label="自定义轮数"]');
    await turns.setValue("99");
    // 失焦提交：QNumber 按 min/max 收口
    await turns.trigger("blur");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    expect(api.updateMemorySettings).toHaveBeenCalledWith({
      fragment_max_turns: 30,
      fragment_max_tokens: 4096,
    });
    w.unmount();
  });

  it("#64 清空只是编辑过程：失焦提示必填并回显当前值，不把空值当 0 / 下限发出去", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    await chooseSelectOption(memory, "自定义");
    const turns = memory.find('input[aria-label="自定义轮数"]');
    // 真实打字只有 input 事件（VTU 的 setValue 会顺带触发 change，等于同时离开输入框）
    (turns.element as HTMLInputElement).value = "";
    await turns.trigger("input");
    await nextTick();
    // 只清空、还没离开：不伪造 0（父级值仍是 10）
    expect((turns.element as HTMLInputElement).value).toBe("");
    await turns.trigger("blur");
    await nextTick();

    // 离开时提示必填，并把显示恢复为当前值（不是 0、也不是下限 1）
    expect(memory.text()).toContain("这项必填");
    expect((turns.element as HTMLInputElement).value).toBe("10");
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    expect(api.updateMemorySettings).toHaveBeenCalledWith({
      fragment_max_turns: 10,
      fragment_max_tokens: 4096,
    });
    w.unmount();
  });
});

describe("搜索配置组：按组保存与「留空不等于清除」", () => {
  it("高级参数与 SearXNG 地址一起提交，且默认不发送 bocha_api_key", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-settings-fold-toggle="models.search.advanced"]').trigger("click");
    await nextTick();

    await models.find('input[aria-label="返回条数"]').setValue("8");
    await nextTick();
    await models.find('input[aria-label="自建 SearXNG 地址"]').setValue("https://s.example");
    await nextTick();

    await models
      .find('[data-settings-group="models.search"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).toEqual({
      top_k_default: 8,
      max_fetch_chars: 15000,
      searxng_url: "https://s.example",
    });
    expect(body).not.toHaveProperty("bocha_api_key");
    expect(models.find(".set-notice.ok").text()).toContain("搜索配置已保存");
    w.unmount();
  });

  it("已配置博查 Key 时留空保存不会清除它", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('input[aria-label="返回条数"]').setValue("6");
    await nextTick();
    await models
      .find('[data-settings-group="models.search"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();
    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).not.toHaveProperty("bocha_api_key");
    w.unmount();
  });

  it("只进入替换态但留空：保存被拦下并说明原因", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    const replace = models.findAll(".qio-btn").find((button) => button.text() === "替换");
    await replace!.trigger("click");
    await nextTick();
    // 让分组进入「脏」状态（否则保存按钮本身就是禁用的，用户不会误以为能提交空 Key）
    await models.find('input[aria-label="返回条数"]').setValue("6");
    await nextTick();
    await models
      .find('[data-settings-group="models.search"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();
    expect(api.updateSearchSettings).not.toHaveBeenCalled();
    expect(models.text()).toContain("请输入新的博查 API Key");
    w.unmount();
  });
});

describe("迟到的旧响应不覆盖新修改（即时保存）", () => {
  it("维护间隔：先发的请求晚回来，不回填覆盖后改的值", async () => {
    const { api } = await import("../../../services/api");
    const update = api.updateMaintenanceSettings as ReturnType<typeof vi.fn>;
    let releaseFirst: (value: unknown) => void = () => {};
    update.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseFirst = resolve;
        }),
    );
    update.mockImplementationOnce(async (body: { enabled?: boolean; interval_hours?: number }) => ({
      enabled: body.enabled ?? true,
      interval_hours: body.interval_hours ?? 24,
    }));

    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    // 第一次修改：后台整理开关（请求挂起）
    await memory.find('button[aria-label="后台整理开关"]').trigger("click");
    await nextTick();
    // 第二次修改：间隔改成 30（后发先至）
    const interval = memory.find('input[aria-label="整理间隔（小时）"]');
    await interval.setValue("30");
    await interval.trigger("blur");
    await flushFrames();
    expect(update).toHaveBeenCalledTimes(2);

    // 旧请求此刻才返回，带着过期值
    releaseFirst({ enabled: true, interval_hours: 24 });
    await flushFrames();

    expect((memory.find('input[aria-label="整理间隔（小时）"]').element as HTMLInputElement).value).toBe(
      "30",
    );
    expect(memory.find('button[aria-label="后台整理开关"]').attributes("aria-checked")).toBe("true");
    w.unmount();
  });
});

describe("即时保存失败回滚", () => {
  it("工具记录开关失败：控件回到真实值并给出分区内联错误", async () => {
    const { api } = await import("../../../services/api");
    (api.updateToolHistorySettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("500 boom"),
    );
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");

    await tools.find('button[aria-label="保存工具输出全文开关"]').trigger("click");
    await flushFrames();

    expect(
      tools.find('button[aria-label="保存工具输出全文开关"]').attributes("aria-checked"),
    ).toBe("true");
    expect(tools.find(".set-notice.err").text()).toContain("保存工具记录设置失败");
    w.unmount();
  });

  it("工具记录有两个保留天数（输出 vs 整条记录），分别提交", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");

    expect(tools.text()).toContain("输出保留天数");
    expect(tools.text()).toContain("记录保留天数");
    expect(tools.text()).toContain("删除整条工具记录");

    await tools.find('input[aria-label="输出保留（天）"]').setValue("30");
    await tools.find('input[aria-label="输出保留（天）"]').trigger("blur");
    await flushFrames();
    expect(api.updateToolHistorySettings).toHaveBeenCalledWith({ output_retention_days: 30 });

    await tools.find('input[aria-label="记录保留（天）"]').setValue("120");
    await tools.find('input[aria-label="记录保留（天）"]').trigger("blur");
    await flushFrames();
    expect(api.updateToolHistorySettings).toHaveBeenCalledWith({ record_retention_days: 120 });
    w.unmount();
  });
});
describe("#63 非法文本不保存 / 显式 0 是合法值", () => {
  /**
   * 真实浏览器的「打字」只有 input 事件；VTU 的 setValue 会顺带触发 change（提交），
   * 会把「编辑过程中」和「离开输入框」两步揉成一步。这里显式分开。
   */
  async function typeOnly(input: { element: Element; trigger: (event: string) => Promise<void> }, value: string) {
    (input.element as HTMLInputElement).value = value;
    await input.trigger("input");
    await nextTick();
  }

  async function mountTools() {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    return { api, w, tools };
  }

  it("输出保留期输入 abc / 1e / -1：不发保存请求、有可读提示、显示保留最近保存值 90", async () => {
    const { api, w, tools } = await mountTools();
    const update = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    const input = tools.find('input[aria-label="输出保留（天）"]');

    for (const bad of ["abc", "1e", " -1"]) {
      await typeOnly(input, bad);
      await input.trigger("blur");
      await flushFrames();
      // 不发保存请求（空白/非法/低于下限都不是用户表达的合法值）
      expect(update, bad).not.toHaveBeenCalled();
      // 可读提示（不是把错误塞进控制台）
      expect(tools.text(), bad).toContain("已恢复为当前值");
      // 显示仍是最近已保存值 90，而不是空白或 0
      expect((input.element as HTMLInputElement).value, bad).toBe("90");
    }
    w.unmount();
  });

  it("显式输入 0：发出保存请求且后端落库为 0，重新进入设置后仍是 0", async () => {
    const { api } = await import("../../../services/api");
    (api.updateToolHistorySettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 0,
      record_retention_days: 0,
      purged: 0,
    });

    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');
    await input.setValue("0");
    await input.trigger("blur");
    await flushFrames();

    expect(api.updateToolHistorySettings).toHaveBeenCalledWith({ output_retention_days: 0 });
    expect((input.element as HTMLInputElement).value).toBe("0");
    w.unmount();

    // 重新进入设置（等价于刷新后的重新读取）：与后端保存的 0 一致，不显示旧的 90
    (api.getToolHistorySettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 0,
      record_retention_days: 0,
    });
    const again = await mountSettings();
    await openCategory(again, "tools");
    expect(
      (sectionById(again, "tools").find('input[aria-label="输出保留（天）"]').element as HTMLInputElement)
        .value,
    ).toBe("0");
    again.unmount();
  });
});

describe("#64 清空只是编辑过程：离开回显 / 刷新后与后端一致，不出现假 0", () => {
  async function typeOnly(input: { element: Element; trigger: (event: string) => Promise<void> }, value: string) {
    (input.element as HTMLInputElement).value = value;
    await input.trigger("input");
    await nextTick();
  }

  it("清空输出保留期后失焦：提示必填、回显 90、不发请求；重新进入仍是 90", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');

    await typeOnly(input, "");
    // 编辑过程中：输入框为空，但父级没被写成 0（不能出现「后端是 90、界面显示 0」）
    expect((input.element as HTMLInputElement).value).toBe("");
    expect(api.updateToolHistorySettings).not.toHaveBeenCalled();

    await input.trigger("blur");
    await flushFrames();
    expect(tools.text()).toContain("这项必填");
    expect((input.element as HTMLInputElement).value).toBe("90");
    expect(api.updateToolHistorySettings).not.toHaveBeenCalled();
    w.unmount();

    const again = await mountSettings();
    await openCategory(again, "tools");
    expect(
      (sectionById(again, "tools").find('input[aria-label="输出保留（天）"]').element as HTMLInputElement)
        .value,
    ).toBe("90");
    again.unmount();
  });
});

describe("#65 保存失败：回到实际生效值、可关闭、可重试、旧回执不覆盖新状态", () => {
  it("503 失败：控件回 90、错误简短可关闭、技术详情可展开；重试成功后显示真正成功", async () => {
    const { api } = await import("../../../services/api");
    const update = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    update.mockRejectedValueOnce(new Error("PUT /api/settings/tool-history -> 503: upstream overloaded"));
    update.mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 30,
      record_retention_days: 0,
      purged: 0,
    });

    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');
    await input.setValue("30");
    await input.trigger("blur");
    await flushFrames();

    expect((input.element as HTMLInputElement).value).toBe("90");
    const error = tools.find(".set-notice.err");
    expect(error.text()).toContain("保存输出保留期失败");
    // 错误简短：不把整段 URL/状态码塞进主提示
    expect(error.text()).not.toContain("503");
    // 技术诊断按需查看
    expect(tools.find("details").text()).toContain("技术详情");
    expect(tools.find("details").text()).toContain("503");
    // 可关闭
    await tools.find('button[aria-label="关闭这项提示"]').trigger("click");
    await nextTick();
    expect(tools.find(".set-notice.err").exists()).toBe(false);

    // 关闭后重新输入同一个值再失焦：仍能真正保存（不是只能靠「重试」按钮）
    await input.setValue("30");
    await input.trigger("blur");
    await flushFrames();
    expect((input.element as HTMLInputElement).value).toBe("30");
    expect(tools.find(".set-notice.ok").text()).toContain("输出保留期已保存");
    w.unmount();
  });

  it("重试按钮重放原操作：失败后点重试，成功后值真正生效", async () => {
    const { api } = await import("../../../services/api");
    const update = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    update.mockRejectedValueOnce(new Error("503 boom"));
    update.mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 40,
      record_retention_days: 0,
      purged: 0,
    });
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');
    await input.setValue("40");
    await input.trigger("blur");
    await flushFrames();
    expect((input.element as HTMLInputElement).value).toBe("90");

    await tools.find('[data-settings-action="retry"]').trigger("click");
    await flushFrames();
    expect(update).toHaveBeenCalledTimes(2);
    // 重试的是原来那次写入（同一字段 40），不是让用户从头再填
    expect(update.mock.calls[1][0]).toEqual({ output_retention_days: 40 });
    expect((input.element as HTMLInputElement).value).toBe("40");
    expect(tools.find(".set-notice.ok").text()).toContain("输出保留期已保存");
    w.unmount();
  });

  it("迟到失败不覆盖新操作：第一次失败晚回、第二次成功 → 显示第二次的 40，无旧错误", async () => {
    const { api } = await import("../../../services/api");
    const update = api.updateToolHistorySettings as ReturnType<typeof vi.fn>;
    let rejectFirst: (e: Error) => void = () => {};
    update.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectFirst = reject; }),
    );
    update.mockResolvedValueOnce({
      record_outputs: true,
      output_retention_days: 40,
      record_retention_days: 0,
      purged: 0,
    });
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="输出保留（天）"]');

    await input.setValue("30");
    await input.trigger("blur");
    await flushFrames();
    await input.setValue("40");
    await input.trigger("blur");
    await flushFrames();
    expect(update).toHaveBeenCalledTimes(1); // 同一设置串行

    rejectFirst(new Error("503 boom"));
    await flushFrames();
    expect(update).toHaveBeenCalledTimes(2);
    expect((input.element as HTMLInputElement).value).toBe("40");
    // 旧失败回执被隔离：既不覆盖新值，也不留下一条指向旧操作的错误
    expect(tools.find(".set-notice.err").exists()).toBe(false);
    expect(tools.find(".set-notice.ok").text()).toContain("输出保留期已保存");
    w.unmount();
  });
});