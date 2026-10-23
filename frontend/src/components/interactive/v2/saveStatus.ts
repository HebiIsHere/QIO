/**
 * 互动页保存状态文案（Phase 1-B，纯函数）。
 *
 * 契约 §2.1 / §2.3：互动页只有**一处**保存状态，挂在全局栏里；
 * 这里把 store 的真实状态翻成一句话，并给出可排查的详情：
 * - 「保存」只落库，不等于 QIO 已收到（提交是否成功由提交区单独说明）；
 * - 失败原因的传输层外壳（/api/… 与状态码）不铺在脸上，完整原文放 detail/title。
 */
import { humanizeFailure } from "../displayText";

export type SaveStatusValue = "idle" | "saving" | "saved" | "error";
export type SaveTone = "ok" | "busy" | "err";

export interface SaveStatusView {
  text: string;
  tone: SaveTone;
  /** 悬停时展示的完整说明（已去掉接口路径与内部代码）；没有则为空串。 */
  detail: string;
}

export function saveStatusView(input: {
  status: SaveStatusValue;
  dirty: boolean;
  error?: string | null;
}): SaveStatusView {
  if (input.status === "saving") {
    return { text: "正在保存…", tone: "busy", detail: "" };
  }
  if (input.status === "error") {
    const full = humanizeFailure(input.error ?? null);
    const short = humanizeFailure(input.error ?? null, { maxShort: 40 });
    return { text: `保存失败：${short.short}`, tone: "err", detail: full.detail };
  }
  if (input.dirty) {
    return { text: "有改动尚未保存", tone: "busy", detail: "" };
  }
  if (input.status === "saved") {
    return { text: "已保存", tone: "ok", detail: "" };
  }
  return { text: "尚未保存过", tone: "ok", detail: "" };
}
