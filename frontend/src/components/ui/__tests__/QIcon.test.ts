/**
 * QIcon / 图标名集合（2026-10-26 统一改造的基础件）。
 *
 * 守三件事：
 * 1) 图标只从 icons.ts 来，全部是可描边路径（没有用 emoji/Unicode 冒充）；
 * 2) 渲染出的 svg 是装饰性的（aria-hidden），不抢可访问名称；
 * 3) 未知名字不会让界面炸掉（开发期告警，渲染空 svg 占位）。
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import QIcon from "../QIcon.vue";
import { ICON_NAMES, ICON_PATHS } from "../icons";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("图标名集合", () => {
  it("覆盖本轮需要的图标名", () => {
    const required = [
      "close", "chevron-down", "chevron-up", "chevron-left", "chevron-right",
      "copy", "retry", "check", "info", "warning", "error", "success",
      "edit", "trash", "archive", "pin", "locate", "external", "more",
      "search", "refresh", "download", "eye", "link", "unlink",
      "collapse", "expand", "settings", "plus", "minus",
      "window-minimize", "window-maximize", "window-restore", "window-close",
    ];
    for (const name of required) expect(ICON_NAMES, name).toContain(name);
  });

  it("每个图标都有非空路径，且不含 emoji/Unicode 图形字符", () => {
    for (const name of ICON_NAMES) {
      const paths = ICON_PATHS[name];
      expect(paths.length, name).toBeGreaterThan(0);
      for (const d of paths) {
        expect(d.trim().length, name).toBeGreaterThan(0);
        expect(d, name).not.toMatch(/[\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\u2700-\u27bf]/);
      }
    }
  });
});

describe("QIcon 渲染", () => {
  it("svg 是装饰性的：aria-hidden、无 role 干扰", () => {
    const w = mount(QIcon, { props: { name: "close" } });
    const svg = w.find("svg");
    expect(svg.attributes("aria-hidden")).toBe("true");
    expect(svg.attributes("viewBox")).toBe("0 0 24 24");
    expect(svg.attributes("stroke")).toBe("currentColor");
    expect(svg.attributes("stroke-width")).toBe("1.6");
    expect(svg.attributes("fill")).toBe("none");
    expect(svg.attributes("data-icon")).toBe("close");
    expect(svg.findAll("path").length).toBe(ICON_PATHS.close.length);
  });

  it("默认 16px，尺寸可按传入值变化", () => {
    const w = mount(QIcon, { props: { name: "check" } });
    expect(w.find("svg").attributes("width")).toBe("16");
    const big = mount(QIcon, { props: { name: "check", size: 20 } });
    expect(big.find("svg").attributes("width")).toBe("20");
  });

  it("未知图标名：不崩溃，开发环境告警", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const w = mount(QIcon, { props: { name: "not-an-icon" } });
    expect(w.find("svg").exists()).toBe(true);
    expect(w.findAll("path").length).toBe(0);
    expect(warn).toHaveBeenCalled();
  });
});
