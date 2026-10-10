/**
 * 外链白名单与打开方式。
 *
 * 模型输出里的链接一律**不可信**：`javascript:`、`data:`、`file:`、`vbscript:`
 * 这类 scheme 被点击时会执行代码或在本地文件系统上跳转。这里只放行
 * https / http / mailto，其余一律当作纯文本处理。
 *
 * 打开方式：桌面壳里走 Tauri 官方 open（`plugin:shell|open`，对应
 * capabilities 里的 `shell:allow-open`），不让 WebView 自己导航；浏览器开发
 * 模式下退回 `window.open(..., "noopener,noreferrer")`。
 */

const SAFE_SCHEMES = ["http", "https", "mailto"];
// 控制字符与空白：`java\nscript:`、` javascript:` 这类伪装必须在解析前就拒绝
const CONTROL_OR_SPACE = /[\u0000-\u0020\u007f\u00a0\u2028\u2029]/;

/** 只接受白名单 scheme 的绝对 URL。 */
export function isSafeExternalUrl(raw: unknown): boolean {
  const url = typeof raw === "string" ? raw.trim() : "";
  if (!url) return false;
  if (CONTROL_OR_SPACE.test(url)) return false;
  if (url.startsWith("//")) return false; // 协议相对：无法判断 scheme
  const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(url);
  if (!match) return false; // 相对路径不作为外链
  return SAFE_SCHEMES.includes(match[1].toLowerCase());
}

function tauriInternals(): Record<string, unknown> | undefined {
  return (globalThis as unknown as { __TAURI_INTERNALS__?: Record<string, unknown> })
    .__TAURI_INTERNALS__;
}

/**
 * 打开一个外部链接。返回是否**成功发起打开尝试**（不是「新窗口一定出现了」）。
 *
 * 诚实规则（F03）：`window.open(..., "noopener,noreferrer")` 在 noopener 下
 * **成功打开也会返回 null**，因此不能用返回值判断成败 —— 用 null 当失败会把成功打开
 * 报成「打开失败」，并在界面上给出错误提示。保留 opener 隔离，不为了拿句柄去掉安全参数。
 * 只有可验证的失败（环境没有 window.open / 调用抛错 / 危险 scheme）才返回 false，
 * 调用方据此给出可见反馈。
 */
export async function openExternal(url: string): Promise<boolean> {
  if (!isSafeExternalUrl(url)) return false;
  const target = url.trim();
  if (tauriInternals()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("plugin:shell|open", { path: target });
      return true;
    } catch {
      return false;
    }
  }
  const open = globalThis.open;
  if (typeof open !== "function") return false;
  try {
    // 返回值丢弃：noopener 下 null 无法同步区分「被拦截」与「已打开」。
    open.call(globalThis, target, "_blank", "noopener,noreferrer");
    return true;
  } catch {
    return false;
  }
}
