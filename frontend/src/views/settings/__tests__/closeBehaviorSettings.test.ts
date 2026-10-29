/**
 * A（F05）设置入口：关闭行为连续切换后，页面显示必须与最新有效选择一致；
 * 保存失败要有明确反馈，并且控件回到真实生效值（不是「看起来改了」）。
 *
 * 基线（f5d0e744）上第一条是红的：旧请求晚返回会把已生效的选择盖回去。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  chooseSelectLabel,
  flushFrames,
  mountSettings,
  sectionById,
  setViewportWidth,
} from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});

vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function pressedLabels(root: ReturnType<typeof sectionById>): string[] {
  return root.findAll('.set-seg__opt[aria-pressed="true"]').map((option) => option.text());
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
  setViewportWidth(1280);
});

describe("关闭行为设置入口：显示与实际生效值一致", () => {
  it("连续切换（直接退出 → 最小化到托盘）后，旧的慢响应不会把界面盖回旧选择", async () => {
    const { api } = await import("../../../services/api");
    const slower = deferred<{ ok: boolean; typewriter_cps: number; close_behavior: string }>();
    const faster = deferred<{ ok: boolean; typewriter_cps: number; close_behavior: string }>();
    (api.updateCloseBehavior as unknown as { mockImplementationOnce: (fn: () => unknown) => void })
      .mockImplementationOnce(() => slower.promise);
    (api.updateCloseBehavior as unknown as { mockImplementationOnce: (fn: () => unknown) => void })
      .mockImplementationOnce(() => faster.promise);

    const w = await mountSettings();
    const regular = sectionById(w, "regular");

    await chooseSelectLabel(regular, "直接退出");
    await chooseSelectLabel(regular, "最小化到托盘");

    faster.resolve({ ok: true, typewriter_cps: 50, close_behavior: "tray" });
    await flushFrames();
    expect(pressedLabels(regular)).toContain("最小化到托盘");

    // 较早的请求这时才返回：它代表的不是最新选择，不能覆盖界面
    slower.resolve({ ok: true, typewriter_cps: 50, close_behavior: "quit" });
    await flushFrames();

    expect(pressedLabels(regular)).toContain("最小化到托盘");
    expect(pressedLabels(regular)).not.toContain("直接退出");
    w.unmount();
  });

  it("保存失败：提示明确原因，控件回到真实生效值", async () => {
    const { api } = await import("../../../services/api");
    (api.updateCloseBehavior as unknown as { mockRejectedValueOnce: (e: unknown) => void })
      .mockRejectedValueOnce(new Error("后端不可用"));

    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    const before = pressedLabels(regular);

    await chooseSelectLabel(regular, "直接退出");
    await flushFrames();

    const notice = regular.find(".set-notice").text();
    expect(notice).toContain("保存关闭行为失败");
    expect(notice).toContain("后端不可用");
    // 控件停在「看起来改了、其实没保存」的值上是不可接受的
    expect(pressedLabels(regular)).toEqual(before);
    expect(pressedLabels(regular)).toContain("每次询问");
    w.unmount();
  });
});
