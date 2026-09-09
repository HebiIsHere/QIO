import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import CredentialCard from "../CredentialCard.vue";
import type { CredentialMeta } from "../../../services/api";

const base: CredentialMeta = {
  key_id: "key_demo",
  version: 2,
  tags: ["chat", "code"],
  endpoint: "https://api.openai.com/v1",
  default_model: "gpt-4o-mini",
  budget: 40,
  budget_used: 12,
  status: "active",
  enabled: true,
  note: null,
};

describe("CredentialCard", () => {
  it("渲染名称与类别标签", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, note: "GPT-5 Studio" } } });
    expect(w.find(".name").text()).toBe("GPT-5 Studio");
    const badges = w.findAll(".qio-badge").map((b) => b.text());
    expect(badges).toContain("chat");
    expect(badges).toContain("code");
  });
  it("active+enabled 显示 ✓ 已启用（ok），停用显示 已停用（paused），revoked 显示 ✕ 已撤销（err）", () => {
    const ok = mount(CredentialCard, { props: { credential: { ...base } } });
    expect(ok.find(".status").text()).toContain("✓");
    expect(ok.find(".status").classes()).toContain("ok");
    const paused = mount(CredentialCard, { props: { credential: { ...base, enabled: false } } });
    expect(paused.find(".status").text()).toBe("已停用");
    expect(paused.find(".status").classes()).toContain("paused");
    const err = mount(CredentialCard, { props: { credential: { ...base, status: "revoked" } } });
    expect(err.find(".status").text()).toContain("✕");
    expect(err.find(".status").classes()).toContain("err");
  });
  it("掩码密钥行只读显示（不包含明文）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    expect(w.find(".key").text()).toContain("••••");
    expect(w.find(".key").text()).not.toContain("sk-");
  });
  it("预算进度条填充宽度与警告态（>=80% 变 warning）", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, budget: 40, budget_used: 12 } } });
    expect(w.find(".fill").attributes("style")).toContain("30%");
    expect(w.find(".fill").classes()).not.toContain("warn");
    const warn = mount(CredentialCard, { props: { credential: { ...base, budget: 20, budget_used: 18 } } });
    expect(warn.find(".fill").classes()).toContain("warn");
    expect(warn.find(".meta").text()).toContain("¥ 18 / 20");
  });
  it("无预算时显示 预算 ∞ 且不渲染进度条", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, budget: null } } });
    expect(w.find(".meta").text()).toContain("预算 ∞");
    expect(w.find(".budget").exists()).toBe(false);
  });
  it("测试/编辑/换钥/启停/删除按钮发出对应事件", async () => {
    const w = mount(CredentialCard, { props: { credential: { ...base } } });
    await w.find(".btn-test").trigger("click");
    await w.find(".btn-meta").trigger("click");
    await w.find(".btn-rotate").trigger("click");
    await w.find(".btn-toggle").trigger("click");
    await w.find(".btn-danger").trigger("click");
    expect(w.emitted("test")).toHaveLength(1);
    expect(w.emitted("edit-meta")).toHaveLength(1);
    expect(w.emitted("rotate")).toHaveLength(1);
    expect(w.emitted("toggle-enabled")).toHaveLength(1);
    expect(w.emitted("remove")).toHaveLength(1);
  });
});
