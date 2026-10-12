/**
 * [P3 多媒体] 消息上的**读取事实**呈现（契约 §4.4 / §5.1）。
 *
 * 五态必须可区分，而且**分开报告**：
 *   附件已保存 → 预览可用 → 内容已处理 → 已交付模型 → 回答已完成。
 *
 * 这一条用例锁死三件事：
 *   1. 八种 source 各自显示自己的事实（已交付 / 抽样 / 转写 / 未支持）与真实原因；
 *   2. 时间位置如实显示（后端给才显示），附件名来自真的那条附件元数据；
 *   3. **不许越界**：已交付不等于「模型理解正确」；抽样必须写「不是逐帧」；
 *      转写只代表实际得到的文字；不支持的原因照原样带出来（不缩写成「不支持」）。
 *
 * 模拟边界：附件服务只在打开/重定位这类动作上，本用例不需要 —— 全是渲染事实。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import type { StreamMessage } from "../../stores/session";
import type { AttachmentRef } from "../../services/attachments";

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "会议录音.mp3",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function message(over: Partial<StreamMessage> = {}): StreamMessage {
  return {
    id: "m1",
    role: "user",
    content: "听听这段录音",
    contentType: "text",
    createdAt: "2026-10-12T08:00:00+00:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MessageItem, { props: { message: msg }, global: { plugins: [pinia] } });
}

beforeEach(() => {
  localStorage.clear();
});

describe("MessageItem：多媒体读取事实", () => {
  it("audio_transcribed：显示真实转写文字、时段与引擎，且不声称听懂了声音本身", () => {
    const w = mountItem(
      message({
        attachments: [attachment({ id: "att_audio", name: "会议录音.mp3" })],
        mediaFacts: [
          {
            id: "att_audio",
            label: "会议录音.mp3",
            source: "audio_transcribed",
            read: true,
            transcript: "大家好，今天我们讲三件事",
            range: { start: 0, end: 12.5 },
            engine: { provider: "openai.audio.transcriptions", model: "whisper-x" },
            at: "2026-10-12T08:00:03+00:00",
          },
        ],
      }),
    );
    const row = w.find('[data-test="media-fact"]');
    expect(row.exists(), "转写事实没有渲染出来").toBe(true);
    expect(row.attributes("data-source")).toBe("audio_transcribed");
    expect(row.attributes("data-read")).toBe("true");
    expect(w.find('[data-test="media-fact-name"]').text()).toBe("会议录音.mp3");
    expect(w.find('[data-test="media-fact-label"]').text()).toContain("转写");
    const detail = w.find('[data-test="media-fact-detail"]').text();
    expect(detail).toContain("0s");
    expect(detail).toContain("12.5s");
    expect(detail).toContain("只代表实际得到的文字");
    expect(detail).not.toMatch(/语气|情绪|音乐/);
    expect(w.find('[data-test="media-fact-transcript"]').text()).toContain("三件事");
    expect(w.find('[data-test="media-fact-at"]').exists(), "时间位置没有显示").toBe(true);
  });

  it("video_sampled：写明抽样时间点，并明说不是逐帧", () => {
    const w = mountItem(
      message({
        attachments: [attachment({ id: "att_video", name: "screen.mp4" })],
        mediaFacts: [
          {
            id: "att_video",
            source: "video_sampled",
            read: true,
            frames: [{ at: 0.5 }, { at: 1.5 }],
            range: { start: 0.5, end: 1.5 },
            engine: { provider: "ffmpeg", model: "ffmpeg.exe" },
          },
        ],
      }),
    );
    const detail = w.find('[data-test="media-fact-detail"]').text();
    expect(detail).toContain("0.5s");
    expect(detail).toContain("1.5s");
    expect(detail).toContain("不是逐帧");
    expect(w.find('[data-test="media-fact-label"]').text()).toContain("抽样");
  });

  it("未支持：原因原样带出来，不缩写成「不支持」，也不显示转写文字", () => {
    const reason =
      "视频抽样：这台机器上没有可用的转换程序，也没有随包提供：已按顺序查过 QIO_FFMPEG 环境变量、应用资源目录、PATH 与常见安装位置，都没有可用的 ffmpeg";
    const w = mountItem(
      message({
        attachments: [attachment({ id: "att_video", name: "screen.mp4" })],
        mediaFacts: [{ id: "att_video", source: "video_unsupported", read: false, reason }],
      }),
    );
    const row = w.find('[data-test="media-fact"]');
    expect(row.attributes("data-read")).toBe("false");
    const detail = w.find('[data-test="media-fact-detail"]').text();
    expect(detail).toContain("这台机器上没有可用的转换程序");
    expect(detail).toContain("也没有随包提供");
    expect(detail).not.toBe("不支持");
    expect(w.find('[data-test="media-fact-transcript"]').exists()).toBe(false);
  });

  it("已交付：只说「真实字节已交给模型」，绝不说「已理解」", () => {
    const w = mountItem(
      message({
        attachments: [attachment({ id: "att_img", name: "示意图.png" })],
        mediaFacts: [{ id: "att_img", source: "image_delivered", read: true, bytes: 4096 }],
      }),
    );
    const label = w.find('[data-test="media-fact-label"]').text();
    const detail = w.find('[data-test="media-fact-detail"]').text();
    expect(label).toContain("已交付");
    expect(label + detail).not.toContain("已理解");
    expect(detail).toContain("请求侧的事实");
  });

  it("没有媒体事实时一个字都不显示（不编造、不留空壳）", () => {
    const w = mountItem(message({ attachments: [attachment()] }));
    expect(w.find('[data-test="message-media-facts"]').exists()).toBe(false);
  });

  it("事实指向的附件不在这一条消息里（卡片 id）：仍然如实列出，不冒充附件名", () => {
    const w = mountItem(
      message({
        mediaFacts: [
          { id: "card_media_1", label: "板面卡片上的录音", source: "audio_unsupported", read: false, reason: "没有转写凭据" },
        ],
      }),
    );
    expect(w.find('[data-test="media-fact-name"]').text()).toBe("板面卡片上的录音");
    expect(w.find('[data-test="media-fact-detail"]').text()).toContain("没有转写凭据");
  });
});
