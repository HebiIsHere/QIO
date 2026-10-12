/**
 * 多媒体附件的**读取事实**（契约 §4.4、§5.1）在界面上的呈现口径。
 *
 * 这里只做「把事实翻成人话」，不判断任何交付结果：
 *   * 五态必须可区分：附件已保存 → 预览可用 → 内容已处理 → 已交付模型 → 回答已完成；
 *   * 没有能力 / 没有转换程序时如实降级，原因照原样给人看，绝不改写成「不支持」；
 *   * 抽样帧必须写明时间点与「不是逐帧」；转写文字只代表实际得到的文字。
 */
import { describe, expect, it } from "vitest";
import {
  MEDIA_CAPABILITY_HINT,
  MEDIA_SOURCE_FACTS,
  mediaFactsOf,
  mediaKindOfName,
} from "../mediaFacts";

describe("mediaKindOfName：按扩展名认音视频（不认识就不猜）", () => {
  it("认出常见的音频与视频扩展名（大小写无关）", () => {
    expect(mediaKindOfName("会议录音.MP3")).toBe("audio");
    expect(mediaKindOfName("tone.wav")).toBe("audio");
    expect(mediaKindOfName("clip.mp4")).toBe("video");
    expect(mediaKindOfName("demo.MKV")).toBe("video");
  });

  it("图片与非媒体文件返回 null（不把它们当成音视频）", () => {
    expect(mediaKindOfName("shot.png")).toBeNull();
    expect(mediaKindOfName("report.pdf")).toBeNull();
    expect(mediaKindOfName("noext")).toBeNull();
    expect(mediaKindOfName("")).toBeNull();
  });
});

describe("MEDIA_CAPABILITY_HINT：能力提示必须说真话", () => {
  it("图片 / 音频 / 视频各有各的口径，且都点明「由本次链路决定」", () => {
    expect(MEDIA_CAPABILITY_HINT.image).toContain("图片");
    expect(MEDIA_CAPABILITY_HINT.audio).toContain("音频");
    expect(MEDIA_CAPABILITY_HINT.video).toContain("视频");
  });

  it("音频提示不许承诺「一定能听」：说清要靠本次链路的能力或转写通道", () => {
    const hint = MEDIA_CAPABILITY_HINT.audio;
    expect(hint).toMatch(/链路|转写/);
    expect(hint).not.toContain("一定");
  });

  it("视频提示必须写明「抽样帧，不是逐帧」与「没有转换程序就如实说不能」", () => {
    const hint = MEDIA_CAPABILITY_HINT.video;
    expect(hint).toContain("抽样");
    expect(hint).toContain("不是逐帧");
    expect(hint).toMatch(/转换程序|ffmpeg/);
  });

  it("每种媒体都有「五态」词表（附件已保存 / 预览可用 / 内容已处理 / 已交付模型 / 回答已完成）", () => {
    expect(MEDIA_SOURCE_FACTS.states).toEqual([
      "附件已保存",
      "预览可用",
      "内容已处理",
      "已交付模型",
      "回答已完成",
    ]);
  });
});

describe("mediaFactsOf：source → 人话事实", () => {
  it("八种 source 都能翻成事实，且都带 read 与 kind", () => {
    const sources = [
      "image_delivered",
      "image_unsupported",
      "audio_delivered",
      "audio_transcribed",
      "audio_unsupported",
      "video_delivered",
      "video_sampled",
      "video_unsupported",
    ] as const;
    for (const source of sources) {
      const fact = mediaFactsOf({ source });
      expect(fact.label, source).toBeTruthy();
      expect(typeof fact.read, source).toBe("boolean");
      expect(fact.kind, source).toMatch(/image|audio|video/);
    }
  });

  it("已交付的三种事实 read=true，且措辞不越过「模型理解正确」这条线", () => {
    for (const source of ["audio_delivered", "video_delivered", "image_delivered"] as const) {
      const fact = mediaFactsOf({ source });
      expect(fact.read).toBe(true);
      expect(fact.label).toContain("已交付");
      expect(fact.label).not.toContain("已理解");
    }
  });

  it("video_sampled：label 写抽样、detail 带真实时间点，并明说不是逐帧", () => {
    const fact = mediaFactsOf({
      source: "video_sampled",
      frames: [{ at: 0.5 }, { at: 1.5 }],
      range: { start: 0.5, end: 1.5 },
      engine: { provider: "ffmpeg", model: "ffmpeg.exe" },
    });
    expect(fact.label).toContain("抽样");
    expect(fact.detail).toContain("0.5s");
    expect(fact.detail).toContain("1.5s");
    expect(fact.detail).toContain("不是逐帧");
    expect(fact.engine).toContain("ffmpeg");
  });

  it("audio_transcribed：文字只代表实际得到的文字，不声称听懂语气", () => {
    const fact = mediaFactsOf({
      source: "audio_transcribed",
      transcript: "大家好，今天我们讲三件事。",
      range: { start: 0, end: 12.5 },
      engine: { provider: "openai.audio.transcriptions", model: "whisper-x" },
    });
    expect(fact.read).toBe(true);
    expect(fact.transcript).toContain("三件事");
    expect(fact.detail).toContain("0s");
    expect(fact.detail).toContain("12.5s");
    expect(fact.detail).toMatch(/只代表实际得到的文字/);
    expect(fact.detail).not.toMatch(/语气|情绪|音乐/);
  });

  it("不可用的三种事实：read=false，原因原样带出来（含「这台机器上没有可用的转换程序」）", () => {
    const ffmpegReason =
      "视频抽样：这台机器上没有可用的转换程序，也没有随包提供：已按顺序查过 QIO_FFMPEG 环境变量、应用资源目录、PATH 与常见安装位置，都没有可用的 ffmpeg";
    const fact = mediaFactsOf({ source: "video_unsupported", reason: ffmpegReason });
    expect(fact.read).toBe(false);
    expect(fact.detail).toContain("这台机器上没有可用的转换程序");
    expect(fact.detail).toContain("也没有随包提供");
    expect(fact.detail).not.toBe("不支持");
  });

  it("未知 source 不编造：当作「本次没有交付事实」如实呈现", () => {
    const fact = mediaFactsOf({ source: "something_new" });
    expect(fact.read).toBe(false);
    expect(fact.label).toContain("未交付");
    expect(fact.detail).toContain("something_new");
  });

  it("没有 source 时也安全（历史消息 / 只加载了元数据）", () => {
    expect(mediaFactsOf({}).label).toBeTruthy();
    expect(mediaFactsOf(undefined).read).toBe(false);
  });
});
