/**
 * [P3 多媒体] Composer 的附件能力提示（契约 §4.1 / §4.2，**只加不减**）。
 *
 * 判据是「说真话」：图片 / 音频 / 视频各自的真实状态都要看得见，
 * 而且不许承诺「一定能看见 / 一定听得见」——交付与否由本次真实链路的能力决定。
 * 这里不碰发送主流程：只断言提示存在且措辞不越界。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  prepareAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  waitUntilSettled: vi.fn(),
  pickLocalPath: vi.fn(),
}));

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return { ...actual, api: { ...actual.api, sendTurn: mocks.sendTurn } };
});

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    prepareAttachment: mocks.prepareAttachment,
    uploadAttachment: mocks.uploadAttachment,
    waitUntilSettled: mocks.waitUntilSettled,
    pickLocalPath: mocks.pickLocalPath,
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";

beforeEach(() => {
  localStorage.clear();
  for (const key of Object.keys(mocks) as (keyof typeof mocks)[]) mocks[key].mockReset();
  mocks.pickLocalPath.mockResolvedValue(null);
});

describe("Composer：附件能力提示", () => {
  it("提示存在，图片 / 音频 / 视频各自一句真实状态", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.draft = "带附件的一条";
    const w = mount(Composer, { global: { plugins: [pinia] } });
    await flushPromises();

    // v3：回形针菜单 → 从路径添加，让附件的说明区显示出来
    await w.find(".attach-circle").trigger("click");
    await flushPromises();
    const pathItem = w.findAll(".attach-menu-item").find((b) => b.text().includes("从路径添加"));
    expect(pathItem, "回形针菜单里没有「从路径添加」").toBeTruthy();
    await pathItem!.trigger("click");
    await flushPromises();
    expect(w.find(".path-row").exists(), "路径入口没有打开，说明区也没出现").toBe(true);

    const box = w.find('[data-test="attach-capability"]');
    expect(box.exists(), "附件能力提示没有渲染出来").toBe(true);
    const image = w.find('[data-test="attach-capability-image"]').text();
    const audio = w.find('[data-test="attach-capability-audio"]').text();
    const video = w.find('[data-test="attach-capability-video"]').text();

    expect(image).toContain("图片");
    expect(audio).toContain("音频");
    expect(video).toContain("视频");

    // 音频：说清靠本次链路的能力或转写通道，不承诺一定能听
    expect(audio).toMatch(/链路|转写/);
    expect(audio).not.toContain("一定");

    // 视频：抽样帧，不是逐帧；没有转换程序就如实说不能
    expect(video).toContain("抽样");
    expect(video).toContain("不是逐帧");
    expect(video).toMatch(/转换程序|ffmpeg/);
  });
});
