import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import CredentialCard from "../CredentialCard.vue";
import type { CredentialMeta } from "../../../services/api";

vi.mock("../../../services/api", () => ({
  api: { getCredentialAudit: vi.fn(async () => ({ ok: true, audit: [] })) },
}));

const base: CredentialMeta = {
  key_id: "key_demo",
  version: 2,
  tags: ["main-loop"],
  endpoint: "https://api.deepseek.com/v1",
  default_model: "deepseek-v4-flash",
  budget: 40_000,
  budget_used: 12_000,
  status: "active",
  enabled: true,
  note: null,
  kind: "openai",
  verify_state: "verified",
  is_default: true,
  provider_id: "deepseek",
  provider_name: "DeepSeek",
};

describe("CredentialCard 阅读态", () => {
  it("默认展示：名称/厂商、模型、中文用途、当前默认与状态", () => {
    const w = mount(CredentialCard, { props: { credential: base, isDefault: true } });
    expect(w.find(".name").text()).toBe("DeepSeek");
    expect(w.find(".meta").text()).toContain("deepseek-v4-flash");
    // 用途用中文，不暴露 main-loop 这种内部标签
    expect(w.find(".purpose").text()).toBe("主对话");
    expect(w.find(".default-badge").text()).toBe("当前默认");
    expect(w.find(".status").text()).toContain("已启用");
    expect(w.text()).toContain("已验证可用");
  });

  it("「已启用」与「已验证可用」是两件事，分开显示", () => {
    const w = mount(CredentialCard, {
      props: { credential: { ...base, verify_state: "unverified" } },
    });
    expect(w.text()).toContain("已启用");
    expect(w.text()).toContain("尚未验证");

    const failed = mount(CredentialCard, {
      props: { credential: { ...base, verify_state: "failed" } },
    });
    expect(failed.text()).toContain("验证未通过");
  });

  it("用量按 token 显示，不出现人民币符号", () => {
    const w = mount(CredentialCard, { props: { credential: base } });
    expect(w.find(".meta").text()).toContain("12,000 / 40,000 token");
    expect(w.text()).not.toContain("¥");
  });

  it("没有用量上限时显示「用量不限」且不渲染进度条", () => {
    const w = mount(CredentialCard, { props: { credential: { ...base, budget: null } } });
    expect(w.find(".meta").text()).toContain("用量不限");
    expect(w.find(".budget").exists()).toBe(false);
  });

  it("地址、内部标识、版本与审计都收进详情", async () => {
    const w = mount(CredentialCard, { props: { credential: base } });
    expect(w.find(".detail").exists()).toBe(false);
    await w.find(".btn-manage").trigger("click");
    await w.find(".btn-detail").trigger("click");
    const detail = w.find(".detail");
    expect(detail.text()).toContain("https://api.deepseek.com/v1");
    expect(detail.text()).toContain("key_demo");
    expect(detail.text()).toContain("v2");
  });

  it("更多操作默认收起且 inert，展开后可再次收起", async () => {
    const w = mount(CredentialCard, { props: { credential: base } });
    const manage = w.find(".manage");
    expect(manage.classes()).not.toContain("open");
    expect(manage.attributes("inert")).toBeDefined();
    // 常用入口：编辑 + 更多操作
    expect(w.find(".btn-edit").exists()).toBe(true);
    expect(w.find(".btn-manage").text()).toContain("更多操作");
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".manage").classes()).toContain("open");
    expect(w.find(".manage").attributes("inert")).toBeUndefined();
  });

  it("管理动作发出对应事件：更换 API Key / 重新验证 / 启停 / 撤销 / 删除", async () => {
    const w = mount(CredentialCard, { props: { credential: base } });
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".btn-rotate").text()).toBe("更换 API Key");
    await w.find(".btn-rotate").trigger("click");
    await w.find(".btn-verify").trigger("click");
    await w.find(".btn-toggle").trigger("click");
    await w.find(".btn-revoke").trigger("click");
    await w.find(".btn-danger").trigger("click");
    await w.find(".btn-edit").trigger("click");
    expect(w.emitted("rotate")).toHaveLength(1);
    expect(w.emitted("verify")).toHaveLength(1);
    expect(w.emitted("toggle-enabled")).toHaveLength(1);
    expect(w.emitted("revoke")).toHaveLength(1);
    expect(w.emitted("remove")).toHaveLength(1);
    expect(w.emitted("edit-meta")).toHaveLength(1);
  });

  it("不是默认项时给出「设为默认」，已经是默认项时不给", async () => {
    const w = mount(CredentialCard, { props: { credential: base, isDefault: false } });
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".btn-default").exists()).toBe(true);
    await w.find(".btn-default").trigger("click");
    expect(w.emitted("set-default")).toHaveLength(1);

    const current = mount(CredentialCard, { props: { credential: base, isDefault: true } });
    await current.find(".btn-manage").trigger("click");
    expect(current.find(".btn-default").exists()).toBe(false);
  });

  it("没通过验证/没有主对话用途的凭据不能被设为默认", async () => {
    const w = mount(CredentialCard, {
      props: { credential: { ...base, verify_state: "failed" }, isDefault: false },
    });
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".btn-default").exists()).toBe(false);

    const purposeOnly = mount(CredentialCard, {
      props: { credential: { ...base, tags: ["vision"] }, isDefault: false },
    });
    await purposeOnly.find(".btn-manage").trigger("click");
    expect(purposeOnly.find(".btn-default").exists()).toBe(false);
  });

  it("已撤销的凭据不再显示撤销入口", async () => {
    const w = mount(CredentialCard, {
      props: { credential: { ...base, status: "revoked" } },
    });
    await w.find(".btn-manage").trigger("click");
    expect(w.find(".btn-revoke").exists()).toBe(false);
    expect(w.find(".status").text()).toContain("已撤销");
  });
});
