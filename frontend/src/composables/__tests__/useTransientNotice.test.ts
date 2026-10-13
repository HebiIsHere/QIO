import { describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import { NOTICE_OK_MS, useTransientNotice, type TransientNotice } from "../useTransientNotice";

function host(): { notice: TransientNotice; unmount: () => void; wrapper: ReturnType<typeof mount> } {
  let api!: TransientNotice;
  const wrapper = mount(
    defineComponent({
      setup() {
        api = useTransientNotice();
        return () => h("div");
      },
    }),
  );
  return { notice: api, unmount: () => wrapper.unmount(), wrapper };
}

describe("useTransientNotice：临时提示生命周期", () => {
  it("成功提示在约 3 秒后自动移除，之前一直显示", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const token = notice.begin();
      expect(notice.succeed(token, "已关联已保存的附件「a.png」")).toBe(true);
      expect(notice.notice.value?.tone).toBe("ok");
      vi.advanceTimersByTime(NOTICE_OK_MS - 1);
      expect(notice.notice.value).not.toBeNull();
      vi.advanceTimersByTime(1);
      await nextTick();
      expect(notice.notice.value).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("进行中的真实进度不自动消失（等待选择 / 上传 / 处理都算）", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const token = notice.begin();
      notice.progress(token, "正在等待选择文件…");
      vi.advanceTimersByTime(NOTICE_OK_MS * 10);
      await nextTick();
      expect(notice.notice.value?.tone).toBe("busy");
      expect(notice.notice.value?.text).toContain("正在等待选择文件");
    } finally {
      vi.useRealTimers();
    }
  });

  it("失败提示不自动消失，保留原因、关闭入口与重新操作方式", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const token = notice.begin();
      const run = vi.fn();
      notice.fail(token, "附件保存失败：网络中断", { label: "重新选择文件", run });
      vi.advanceTimersByTime(NOTICE_OK_MS * 10);
      await nextTick();
      expect(notice.notice.value?.tone).toBe("error");
      expect(notice.notice.value?.retry?.label).toBe("重新选择文件");
      notice.dismiss();
      expect(notice.notice.value).toBeNull();
      notice.fail(notice.begin(), "附件保存失败：网络中断", { label: "重新选择文件", run });
      notice.notice.value?.retry?.run();
      expect(run).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("新操作开始会清掉旧计时器：旧的成功计时器不会清掉新提示", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const first = notice.begin();
      notice.succeed(first, "第一条成功");
      vi.advanceTimersByTime(1000);
      const second = notice.begin();
      notice.progress(second, "正在保存附件「b.png」…");
      vi.advanceTimersByTime(NOTICE_OK_MS * 2);
      await nextTick();
      expect(notice.notice.value?.tone).toBe("busy");
      expect(notice.notice.value?.text).toContain("b.png");
    } finally {
      vi.useRealTimers();
    }
  });

  it("旧操作的迟到结果不能覆盖新操作的提示", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const stale = notice.begin();
      const fresh = notice.begin();
      notice.progress(fresh, "正在保存附件「new.png」…");
      expect(notice.isCurrent(stale)).toBe(false);
      expect(notice.succeed(stale, "旧操作成功")).toBe(false);
      expect(notice.fail(stale, "旧操作失败")).toBe(false);
      expect(notice.progress(stale, "旧操作进度")).toBe(false);
      expect(notice.notice.value?.text).toContain("new.png");
      expect(notice.notice.value?.tone).toBe("busy");
    } finally {
      vi.useRealTimers();
    }
  });

  it("新的成功提示有自己的存活时间：不会被上一条的计时器提前清掉", async () => {
    vi.useFakeTimers();
    try {
      const { notice } = host();
      const first = notice.begin();
      notice.succeed(first, "旧成功");
      vi.advanceTimersByTime(1000);
      // 上一条还没到期就被新成功替换：新提示必须从头计时，不能被旧计时器提前清掉
      notice.succeed(first, "新成功");
      vi.advanceTimersByTime(NOTICE_OK_MS - 500); // t = 3500ms，已越过旧计时器的到期点
      await nextTick();
      expect(notice.notice.value?.text).toBe("新成功");
      vi.advanceTimersByTime(500); // t = 4000ms = 新提示自己的 3 秒
      await nextTick();
      expect(notice.notice.value).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("组件卸载清理计时器，且迟到的成功结果不再恢复已失效的提示", async () => {
    vi.useFakeTimers();
    try {
      const { notice, unmount } = host();
      const token = notice.begin();
      notice.progress(token, "正在保存附件「c.png」…");
      unmount();
      expect(notice.notice.value).toBeNull();
      expect(notice.isCurrent(token)).toBe(false);
      expect(notice.succeed(token, "卸载后才到")).toBe(false);
      vi.advanceTimersByTime(NOTICE_OK_MS * 2);
      await nextTick();
      expect(notice.notice.value).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
