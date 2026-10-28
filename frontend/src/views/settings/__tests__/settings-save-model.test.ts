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
  const { createSettingsApiMock } = await import("./settingsApiMock");
  return { api: createSettingsApiMock() };
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

    expect(memory.find('[data-settings-group="memory.fragment"] [data-settings-dirty]').text()).toContain(
      "已保存",
    );

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
    const group = memory.find('[data-settings-group="memory.fragment"]');
    expect(group.find('[data-settings-dirty]').text()).toContain("已保存");
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
    expect(memory.find('[data-settings-dirty]').text()).toContain("已保存");
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

  it("清空必填数字后按最小边界收口（1 轮），不会把空值/null 发出去", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");

    await chooseSelectOption(memory, "自定义");
    const turns = memory.find('input[aria-label="自定义轮数"]');
    await turns.setValue("");
    await turns.trigger("blur");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    expect(api.updateMemorySettings).toHaveBeenCalledWith({
      fragment_max_turns: 1,
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
