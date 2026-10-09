/**
 * 应用内更新的唯一插件入口（spec 2026-09-22-updater-design）。
 *
 * 这一层只做几件事：把 Tauri 插件包成可替换的小接口、做纯粹的版本比较、
 * 把错误分类成人话、按壳的命令编排「下载 → 校验 → 停后端 → 安装 → 失败恢复」。
 * **网络与安装动作全部发生在 Rust 侧**，所以前端 CSP 不需要放宽。
 *
 * 编排顺序（修复提示词 §2）：
 *   1. 解析**本次操作**的代理（壳里做优先级 + 预算探测），把结果显式交给插件：
 *      检查与下载共用同一个 `Update` 上下文，配置变化不会追溯改已开始的操作；
 *   2. `check()` 只读更新源，不动后端；
 *   3. `download()` 下载并完成插件要求的**签名校验**（校验就在这一步，见
 *      tauri-plugin-updater `Update::download` → `verify_signature`）；
 *   4. 只有校验通过、真正要替换安装文件时，才调用 `qio_prepare_for_update` 结束本实例后端，
 *      并且必须拿到「进程树确已退出」的真实结果，否则放弃安装；
 *   5. `install()` 失败（或结束后端失败）→ 调 `qio_restore_backend` 把后端与连接信息恢复回来，
 *      不把聊天留在不可用状态。
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

/** 下载/安装时的操作约束。`expectedVersion` 是界面上显示的版本：对不上就不装。 */
export interface DownloadInstallOptions {
  expectedVersion?: string;
}

/** 前端需要的全部更新能力；store 依赖这个接口，测试注入假实现。 */
export interface UpdaterApi {
  currentVersion(): Promise<string>;
  /** 有更新返回信息；没有更新返回 null（null 才能显示"已是最新"）。 */
  check(): Promise<UpdateInfo | null>;
  downloadAndInstall(
    onProgress: (progress: UpdateProgress) => void,
    options?: DownloadInstallOptions,
  ): Promise<void>;
  relaunch(): Promise<void>;
}

/** 壳里 `qio_prepare_for_update` 的真实结果。 */
interface UpdatePrepResult {
  /** 后端本来就不在（无需恢复） */
  alreadyStopped: boolean;
  pid: number | null;
  stopped: boolean;
  /** 进程树是否**确认**退出（false 时调用方必须放弃安装） */
  verified: boolean;
  detail: string;
}

/** 壳里 `qio_restore_backend` 的结果。 */
interface RestoreResult {
  /** 本实例后端当前可用 */
  restored: boolean;
  /** 这次是否真的重新拉起了后端（false = 本来就在跑，没有重复启动） */
  started: boolean;
  port: number | null;
  detail: string;
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
  network: "更新源请求失败：可能是网络或代理问题",
  signature: "更新包校验未通过，已放弃安装（不会安装未经签名的包）",
  installer: "安装器启动失败：更新没有装上去，可以重试",
  unknown: "更新失败",
};

/**
 * 把插件抛出的错误分类成人话。
 *
 * 教训（2026-09-22 实测）：Tauri updater 把底层原因统一成
 * "Could not fetch a valid release JSON from the remote" —— 连不上、代理不对、
 * 清单解析失败共用这一句。所以分类只用来决定措辞，**原始文本必须一起显示**，
 * 否则用户看到的是我们猜的原因，而不是真实原因。
 */
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
  const detail = raw.trim().replace(/\s+/g, " ").slice(0, 200);
  return { kind, message: detail ? `${base}（原始信息：${detail}）` : base };
}

function errorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  return raw.trim().replace(/\s+/g, " ").slice(0, 300) || "未知错误";
}

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

/**
 * 一次更新操作里用到的 `Update`：它是 Tauri 的 `Resource`，**用到的每一次都要还**。
 *
 * `close()` 在 SDK 里是 `downloadedBytes?.close()` + `super.close()`（见
 * `@tauri-apps/plugin-updater/dist-js/index.js`），所以：
 * * 下载资源已经被 `install()` 成功消费时，SDK 会把它置空 —— 再调 close 不会二次关闭；
 * * 反过来，重复调用本函数会对已经释放的 rid 再发一次 close，因此必须 only-once。
 */
type ReleasableUpdate = { close?: () => Promise<void> } | null | undefined;

/**
 * 生成「只释放一次」的释放函数。
 *
 * 释放本身永远不能改变这次操作的结果：
 * * 安装成功时应用会在 Windows 上退出、Rust 侧可能已经消费了资源，
 *   这时 close 报「resource not found」是**正常**的，不能因此让「安装成功」变成失败；
 * * 失败路径上 close 异常也**不得掩盖原始失败原因** —— 原始错误才是用户需要看到的东西。
 * 所以这里只记一条 warn，绝不向调用方抛。
 */
function createReleaseOnce(update: ReleasableUpdate): () => Promise<void> {
  let released = false;
  return async function release(): Promise<void> {
    if (released) return;
    released = true;
    if (!update || typeof update.close !== "function") return;
    try {
      await update.close();
    } catch (error) {
      console.warn("[qio] 释放更新资源失败（不影响本次更新结果）：", error);
    }
  };
}

/**
 * 解析**本次更新操作**的代理配置（壳里按优先级 + 总预算探测，可超时）。
 *
 * 返回 null 表示壳明确判定「直连」；命令本身失败时退回系统默认行为并留痕 ——
 * 代理只是提示，不该让一次可用的更新因为探测命令出问题而失败。
 * （注意：与 `qio_prepare_for_update` 不同 —— 结束后端失败**不许**被吞掉。）
 */
async function resolveOperationProxy(invoke: Invoke): Promise<string | undefined> {
  try {
    const proxy = await invoke<string | null>("qio_refresh_updater_proxy");
    const trimmed = typeof proxy === "string" ? proxy.trim() : "";
    return trimmed ? trimmed : undefined;
  } catch (error) {
    console.warn("[qio] 代理探测失败，本次更新按系统默认方式连接：", error);
    return undefined;
  }
}

/**
 * 结束本实例后端进程树（进入安装准备），并校验壳报回来的**真实结果**。
 *
 * 壳必须在超时 / 权限错误 / 退出验证失败时返回错误；这里不吞任何异常 ——
 * 拿不到「已退出」的确认就放弃安装（否则安装器会因为 qio-backend.exe 被占用而失败）。
 */
async function prepareForUpdate(invoke: Invoke): Promise<UpdatePrepResult> {
  let result: UpdatePrepResult;
  try {
    result = await invoke<UpdatePrepResult>("qio_prepare_for_update");
  } catch (error) {
    throw new Error(`结束本实例后端失败，已放弃安装：${errorText(error)}`);
  }
  if (!result || result.stopped !== true || result.verified !== true) {
    const detail = result?.detail ? `：${result.detail}` : "";
    throw new Error(`后端进程树未确认退出，已放弃安装${detail}`);
  }
  return result;
}

/** 后端被停掉之后，把它的连接信息恢复回来（幂等：本来在跑就不重复启动）。 */
async function restoreBackend(invoke: Invoke): Promise<{ restored: boolean; suffix: string }> {
  try {
    const result = await invoke<RestoreResult>("qio_restore_backend");
    if (result?.restored) {
      await resetBackendConnectionCache();
      const restartNote = result.started ? "（已重新启动）" : "（未重复启动）";
      return {
        restored: true,
        suffix: `后端已恢复${restartNote}，聊天可以继续使用。`,
      };
    }
    return {
      restored: false,
      suffix: `后端未能自动恢复${result?.detail ? `：${result.detail}` : ""}；请完全退出 QIO 后重新启动。`,
    };
  } catch (error) {
    return {
      restored: false,
      suffix: `后端未能自动恢复：${errorText(error)}；请完全退出 QIO 后重新启动。`,
    };
  }
}

/**
 * 后端换了端口/令牌之后，连接缓存必须重解析。
 *
 * `services/backend.ts` 的缓存只有它自己知道怎么失效（且 WS1 会给它加代次），
 * 所以这里只调它公开的 `resetBackend()`，不自己复制一份缓存逻辑。
 */
async function resetBackendConnectionCache(): Promise<void> {
  try {
    const mod = await import("./backend");
    mod.resetBackend();
  } catch (error) {
    console.warn("[qio] 重置后端连接缓存失败，下一次请求会重新解析：", error);
  }
}

/**
 * 真实实现：包住 @tauri-apps/plugin-updater 与 plugin-process。
 *
 * 动态 import 是刻意的 —— 在浏览器（开发预览 / 单测）里没有 Tauri 运行时，
 * 静态 import 会让整个模块加载失败，而这一层本可以只做纯函数。
 */
export async function tauriUpdaterApi(): Promise<UpdaterApi> {
  const [{ check }, { relaunch }, { getVersion }, { invoke }] = await Promise.all([
    import("@tauri-apps/plugin-updater"),
    import("@tauri-apps/plugin-process"),
    import("@tauri-apps/api/app"),
    import("@tauri-apps/api/core"),
  ]);
  const invokeCommand = invoke as unknown as Invoke;

  /** 一次操作里下载进度的事件流 → 百分比（Started 给总长，Progress 累加块长）。 */
  const progressReporter = (
    state: { total: number | null; downloaded: number },
    onProgress: (progress: UpdateProgress) => void,
  ) => {
    return (event: { event: string; data?: { contentLength?: number; chunkLength?: number } }) => {
      if (event.event === "Started") {
        state.total = event.data?.contentLength ?? null;
        state.downloaded = 0;
      } else if (event.event === "Progress") {
        state.downloaded += event.data?.chunkLength ?? 0;
      } else if (event.event === "Finished") {
        onProgress({ downloaded: state.downloaded, total: state.total, percent: 100 });
        return;
      } else {
        return;
      }
      onProgress({
        downloaded: state.downloaded,
        total: state.total,
        percent:
          state.total && state.total > 0
            ? Math.min(100, Math.round((state.downloaded / state.total) * 100))
            : null,
      });
    };
  };

  return {
    currentVersion: () => getVersion(),
    check: async () => {
      // 每次检查前重新判断代理（VPN 可能刚开或刚关）——判断在壳里做，
      // 结果作为**这次操作**的配置交给插件（不再改进程级环境变量）。
      const proxy = await resolveOperationProxy(invokeCommand);
      const update = await check(proxy ? { proxy } : undefined);
      if (!update) return null;
      // 取完信息就释放：`check()` 只返回纯数据，Update 的 rid 不该留在进程里
      // （autoCheck 每 24 小时一次、用户手动检查也可能多次，泄漏会一直累积）。
      const release = createReleaseOnce(update);
      try {
        return {
          version: update.version,
          notes: update.body ?? undefined,
          date: update.date ?? undefined,
        };
      } finally {
        await release();
      }
    },
    downloadAndInstall: async (onProgress, options) => {
      // 1) 本次操作的代理上下文：检查得到的 Update 会带着它进入下载/安装
      const proxy = await resolveOperationProxy(invokeCommand);
      const update = await check(proxy ? { proxy } : undefined);
      if (!update) {
        throw new Error("更新源上没有可用的新版本（版本可能已下线），请重新检查更新");
      }
      // 从拿到 Update 起，所有出口（版本不匹配 / 下载失败 / 停止后端失败 / 安装失败 /
      // 安装成功）都必须释放它；只释放一次，且释放异常不掩盖真正的失败原因。
      const release = createReleaseOnce(update);
      try {
        const expected = options?.expectedVersion?.trim();
        if (expected && compareVersions(update.version, expected) !== 0) {
          throw new Error(
            `更新源上的版本已变化（界面显示 ${expected}，实际 ${update.version}），已放弃安装；请重新检查更新`,
          );
        }

        // 2) 下载 + 签名校验（校验在 download 内完成，失败即抛）
        const state: { total: number | null; downloaded: number } = {
          total: null,
          downloaded: 0,
        };
        await update.download(progressReporter(state, onProgress));
        onProgress({ downloaded: state.downloaded, total: state.total, percent: 100 });

        // 3) 校验通过、真要替换安装文件了，才结束后端进程树
        try {
          const prep = await prepareForUpdate(invokeCommand);
          if (prep.alreadyStopped) {
            console.info("[qio] 更新前没有本实例后端在跑，无需结束");
          }
          await update.install();
        } catch (error) {
          // 后端已经（或可能已经）被停掉：必须恢复，不能把聊天留在不可用状态。
          // 恢复状态放在最前面：用户首先要知道「现在能不能继续用」，然后才是真因。
          const recovery = await restoreBackend(invokeCommand);
          throw new Error(`${recovery.suffix} 原因：${errorText(error)}`);
        }
      } finally {
        await release();
      }
    },
    relaunch: () => relaunch(),
  };
}
