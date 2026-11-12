import { describe, expect, it } from "vitest";
import { entityKindLabel } from "../entityKind";

describe("entityKindLabel（实体类型显示名）", () => {
  it("已知内部 slug 映射为可读中文名", () => {
    expect(entityKindLabel("person")).toBe("人物");
    expect(entityKindLabel("animal")).toBe("动物");
    expect(entityKindLabel("project")).toBe("项目");
    expect(entityKindLabel("PERSON")).toBe("人物");
  });

  it("未知类型原样保留（不臆造）", () => {
    expect(entityKindLabel("家人")).toBe("家人");
    expect(entityKindLabel("custom_kind")).toBe("custom_kind");
  });

  it("空值 → 空串（界面据此隐藏类型徽章）", () => {
    expect(entityKindLabel("")).toBe("");
    expect(entityKindLabel("   ")).toBe("");
    expect(entityKindLabel(null)).toBe("");
    expect(entityKindLabel(undefined)).toBe("");
  });
});
