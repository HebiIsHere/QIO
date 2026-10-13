import { describe, expect, it } from "vitest";
import {
  decideQioTableMode,
  qioColumnLabel,
  QIO_TABLE_ENTER_COLUMN_PX,
  QIO_TABLE_COMFORT_COLUMN_PX,
} from "../qioTable";

/**
 * 对话表格适配的判定（用户规则：表格不得超出正文边界；列数/内容让每列过窄时
 * 重排为纵向记录；不能靠横向滚动或隐藏溢出；切换依据实际可用宽度与列结构）。
 */
describe("decideQioTableMode", () => {
  it("可用宽度充足、内容最小宽度放得下时用表格", () => {
    expect(
      decideQioTableMode({ availablePx: 800, columnCount: 2, naturalPx: 700, current: "table" }),
    ).toBe("table");
  });

  it("列数多导致每列过窄时切到记录模式", () => {
    // 12 列 / 720px → 每列 60px，明显低于可读下限
    expect(
      decideQioTableMode({ availablePx: 720, columnCount: 12, naturalPx: 0, current: "table" }),
    ).toBe("records");
    // 几十列更不可能是可读表格
    expect(
      decideQioTableMode({ availablePx: 900, columnCount: 40, naturalPx: 0, current: "table" }),
    ).toBe("records");
  });

  it("内容真实最小宽度超过可用宽度时切到记录模式（不是靠隐藏溢出）", () => {
    // 2 列、每列名义上 400px，但单元格里有无法断开的超长内容
    expect(
      decideQioTableMode({ availablePx: 800, columnCount: 2, naturalPx: 1240, current: "table" }),
    ).toBe("records");
  });

  it("滞回：记录模式在 88–112px 区间内不来回振荡", () => {
    const perColumn = (QIO_TABLE_ENTER_COLUMN_PX + QIO_TABLE_COMFORT_COLUMN_PX) / 2; // 100px
    const available = perColumn * 6;
    expect(
      decideQioTableMode({ availablePx: available, columnCount: 6, naturalPx: 0, current: "records" }),
    ).toBe("records");
    expect(
      decideQioTableMode({ availablePx: available, columnCount: 6, naturalPx: 0, current: "table" }),
    ).toBe("table");
  });

  it("退回表格需要同时满足「够宽」与「真实最小宽度留有余量」", () => {
    const wide = QIO_TABLE_COMFORT_COLUMN_PX * 4 + 40; // 每列 > 舒适阈值
    // 真实最小宽度几乎等于可用宽度（只差 4px）：留在记录模式，避免临界抖动
    expect(
      decideQioTableMode({ availablePx: wide, columnCount: 4, naturalPx: wide - 4, current: "records" }),
    ).toBe("records");
    expect(
      decideQioTableMode({ availablePx: wide, columnCount: 4, naturalPx: wide - 40, current: "records" }),
    ).toBe("table");
  });

  it("只有一列或没有可用宽度时不重排（单列没有「列名 + 值」的意义）", () => {
    expect(
      decideQioTableMode({ availablePx: 100, columnCount: 1, naturalPx: 400, current: "table" }),
    ).toBe("table");
    expect(
      decideQioTableMode({ availablePx: 0, columnCount: 8, naturalPx: 0, current: "table" }),
    ).toBe("table");
  });

  it("阈值本身是「每列可读宽度」，不是窗口宽度或固定列数", () => {
    // 同样的 6 列：宽窗口里是表格，窄容器里是记录 —— 决定因素是可用宽度
    expect(
      decideQioTableMode({ availablePx: 1200, columnCount: 6, naturalPx: 0, current: "table" }),
    ).toBe("table");
    expect(
      decideQioTableMode({ availablePx: 420, columnCount: 6, naturalPx: 0, current: "table" }),
    ).toBe("records");
  });
});

describe("qioColumnLabel", () => {
  it("空表头用位置身份，不拿空字符串当列名", () => {
    expect(qioColumnLabel("", 0, false)).toBe("第 1 列");
    expect(qioColumnLabel("   ", 4, false)).toBe("第 5 列");
  });

  it("重复表头带上位置，仍是可区分的备用名称", () => {
    expect(qioColumnLabel("值", 2, true)).toBe("值（第 3 列）");
    expect(qioColumnLabel("值", 2, false)).toBe("值");
  });

  it("普通表头归一化空白后原样使用", () => {
    expect(qioColumnLabel("  工具   名称 ", 0, false)).toBe("工具 名称");
  });
});
