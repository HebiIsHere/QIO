/**
 * 统一 Esc 层级栈（composables/useEscapeLayer.ts）。
 *
 * 守住的规则：
 * - 一次 Esc 只交给最内层（最后注册且 active）的层；内层返回 false 才轮到外层；
 * - 处理过的 Esc 会阻止默认行为并停止继续冒泡（页面上的其它 Esc 处理不会重复触发）；
 * - 输入法组合输入期间的 Esc 不算关闭操作；
 * - 注销后不再参与；同一 id 重复注册不叠加。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { isComposingKeyEvent, registerEscapeLayer } from "../useEscapeLayer";

const disposers: (() => void)[] = [];

afterEach(() => {
  while (disposers.length) disposers.pop()?.();
});

function layer(id: string, onEscape: (event: KeyboardEvent) => boolean, active?: () => boolean) {
  const dispose = registerEscapeLayer({ id, onEscape, active });
  disposers.push(dispose);
  return dispose;
}

function pressEscape(init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "Escape",
    bubbles: true,
    cancelable: true,
    ...init,
  });
  window.dispatchEvent(event);
  return event;
}

describe("useEscapeLayer 层级仲裁", () => {
  it("后注册的内层先处理，一次 Esc 只处理最内层", () => {
    const outer = vi.fn(() => true);
    const inner = vi.fn(() => true);
    layer("outer", outer);
    layer("inner", inner);

    const event = pressEscape();
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
  });

  it("内层返回 false 时交给外层（例如下拉没有可选项）", () => {
    const outer = vi.fn(() => true);
    const inner = vi.fn(() => false);
    layer("outer", outer);
    layer("inner", inner);

    const event = pressEscape();
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
  });

  it("active=false 的层被跳过（关闭状态不参与层级）", () => {
    const outer = vi.fn(() => true);
    const inner = vi.fn(() => true);
    let innerOpen = false;
    layer("outer", outer);
    layer("inner", inner, () => innerOpen);

    pressEscape();
    expect(inner).not.toHaveBeenCalled();
    expect(outer).toHaveBeenCalledTimes(1);

    innerOpen = true;
    pressEscape();
    expect(inner).toHaveBeenCalledTimes(1);
    expect(outer).toHaveBeenCalledTimes(1);
  });

  it("处理过的 Esc 不再冒泡给页面上的其它 Esc 处理（问题 6 的根因）", () => {
    const pageHandler = vi.fn();
    const host = document.createElement("div");
    host.setAttribute("tabindex", "0");
    document.body.appendChild(host);
    host.addEventListener("keydown", pageHandler);
    layer("inner", () => true);

    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    host.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(pageHandler).not.toHaveBeenCalled();
    host.remove();
  });

  it("输入法组合输入（isComposing / keyCode 229）期间的 Esc 不处理任何层", () => {
    const handler = vi.fn(() => true);
    layer("l1", handler);

    pressEscape({ isComposing: true });
    expect(handler).not.toHaveBeenCalled();

    pressEscape({ keyCode: 229 });
    expect(handler).not.toHaveBeenCalled();

    pressEscape();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("isComposingKeyEvent：标准字段与 229 兜底都识别", () => {
    expect(isComposingKeyEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true }))).toBe(true);
    expect(isComposingKeyEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229 }))).toBe(true);
    expect(isComposingKeyEvent(new KeyboardEvent("keydown", { key: "Enter" }))).toBe(false);
  });

  it("注销后不再处理；注销幂等", () => {
    const handler = vi.fn(() => true);
    const dispose = layer("only", handler);
    dispose();
    dispose();
    pressEscape();
    expect(handler).not.toHaveBeenCalled();
  });

  it("同一 id 重复注册以最新一次为准，不叠加成两层", () => {
    const first = vi.fn(() => true);
    const second = vi.fn(() => true);
    layer("same", first);
    layer("same", second);
    pressEscape();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("非 Esc 键不参与仲裁", () => {
    const handler = vi.fn(() => true);
    layer("l1", handler);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    expect(handler).not.toHaveBeenCalled();
  });
});
