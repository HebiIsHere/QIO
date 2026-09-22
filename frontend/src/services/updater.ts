/**
 * 应用内更新的唯一插件入口（spec 2026-09-22-updater-design）。
 *
 * 这一层只做三件事：把 Tauri 插件包成可替换的小接口、做纯粹的版本比较、
 * 把错误分类成人话。**网络与安装动作全部发生在 Rust 侧**，所以前端 CSP 不需要放宽。
 *
 * 测试不依赖 Tauri 运行时：纯函数直接测，插件实现用动态 import，只在使用时才加载。
 */

export type UpdateErrorKind = "network" | "signature" | "installer" | "unknown";

export interface UpdateInfo {
  version: string;
  notes?: string;
  date?: string;
}

export interface UpdateProgress {
  downloaded: number;
  total: number | null;
  percent: number | null;
}

/** 前端需要的全部更新能力；store 依赖这个接口，测试注入假实现。 */
export interface UpdaterApi {
  currentVersion(): Promise<string>;
  /** 有更新返回信息；没有更新返回 null（null 才能显示"已是最新"）。 */
  check(): Promise<UpdateInfo | null>;
  downloadAndInstall(onProgress: (progress: UpdateProgress) => void): Promise<void>;
  relaunch(): Promise<void>;
}

/** 语义化版本比较：只按数字段比，避免 "0.1.10" < "0.1.2" 这种字符串陷阱。 */
export function compareVersions(a: string, b: string): number {
  const parts = (raw: string): number[] =>
    String(raw || "")
      .trim()
      .replace(/^v/i, "")
      .split(/[.\-+]/)
      .map((piece) => Number.parseInt(piece, 10))
      .map((value) => (Number.isFinite(value) ? value : 0));
  const left = parts(a);
  const right = parts(b);
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    const x = left[i] ?? 0;
    const y = right[i] ?? 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}

const ERROR_TEXT: Record<UpdateErrorKind, string> = {
  network: "网络不可达：连不上更新源，请检查网络或稍后重试",
  signature: "更新包校验未通过，已放弃安装（不会安装未经签名的包）",
  installer: "安装器启动失败：更新没有装上去，可以重试",
  unknown: "更新失败",
};

/** 把插件抛出的错误分类成人话。分类错了会骗用户，所以宁可落入 unknown 也不猜。 */
export function describeUpdateError(error: unknown): { kind: UpdateErrorKind; message: string } {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const text = raw.toLowerCase();
  let kind: UpdateErrorKind = "unknown";
  if (text.includes("signature") || text.includes("verify") || text.includes("minisign")) {
    kind = "signature";
  } else if (
    text.includes("fetch") ||
    text.includes("network") ||
    text.includes("connect") ||
    text.includes("timeout") ||
    text.includes("dns") ||
    text.includes("404")
  ) {
    kind = "network";
  } else if (text.includes("install") || text.includes("exit code") || text.includes("nsis")) {
    kind = "installer";
  }
  const base = ERROR_TEXT[kind];
  return {
    kind,
    message: kind === "unknown" && raw ? `${base}：${raw}` : base,
  };
}

/**
 * 真实实现：包住 @tauri-apps/plugin-updater 与 plugin-process。
 *
 * 动态 import 是刻意的 —— 在浏览器（开发预览 / 单测）里没有 Tauri 运行时，
 * 静态 import 会让整个模块加载失败，而这一层本可以只做纯函数。
 */
export async function tauriUpdaterApi(): Promise<UpdaterApi> {
  const [{ check }, { relaunch }, { getVersion }] = await Promise.all([
    import("@tauri-apps/plugin-updater"),
    import("@tauri-apps/plugin-process"),
    import("@tauri-apps/api/app"),
  ]);
  return {
    currentVersion: () => getVersion(),
    check: async () => {
      const update = await check();
      if (!update) return null;
      return {
        version: update.version,
        notes: update.body ?? undefined,
        date: update.date ?? undefined,
      };
    },
    downloadAndInstall: async (onProgress) => {
      const update = await check();
      if (!update) return;
      let total: number | null = null;
      let downloaded = 0;
      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? null;
          downloaded = 0;
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength ?? 0;
        }
        onProgress({
          downloaded,
          total,
          percent: total && total > 0 ? Math.min(100, Math.round((downloaded / total) * 100)) : null,
        });
      });
      onProgress({ downloaded, total, percent: 100 });
    },
    relaunch: () => relaunch(),
  };
}
