/**
 * 权限模式客户端（R9 契约 §1）。
 *
 * 唯一事实源是后端：
 * * 读：GET /api/security/mode → {mode, label, legacy_value}；
 * * 写：**只有用户操作**才能切换 —— PUT /api/security/mode 必须带 user_confirmed=true，
 *   模型 / 工具 / 子智能体没有任何调用路径（前端这里也只是按钮事件触发）。
 *
 * 兼容：W1 后端合入之前，老后端只有 GET /api/settings/computer。
 * 读不到新接口时**退回旧接口并做旧值映射展示**（不静默提高模式、不伪造「已确认」）；
 * 写路径**不退回旧接口** —— 旧接口不要求 user_confirmed，不能拿它当权限模式入口。
 */
import { authHeaders, resetBackend, resolveBackend } from "./backend";

/** 权限接口错误：带状态码，调用方才能区分「没有这个接口（老后端）」与真的坏了。 */
export class SecurityRequestError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
  ) {
    super(path + " -> " + status + "：" + detail);
    this.name = "SecurityRequestError";
  }
}

/**
 * 本模块自带一个极小的请求层（与 attachments.ts 同一套语义：读超时 15s / 写 20s、写不重试）。
 * 为什么不复用 api.ts：设置页测试会把 services/api 整个 mock 掉；权限模式是常驻 UI 的事实来源，
 * 不应因为别的模块被 mock 就无法工作。
 */
async function request<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { timeoutMs = 15_000, ...rest } = init;
  const { base, token } = await resolveBackend();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(base + path, {
      ...rest,
      signal: controller.signal,
      headers: {
        ...authHeaders(token),
        ...((rest.headers as Record<string, string> | undefined) ?? {}),
      },
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      if (resp.status === 401 || resp.status === 403) resetBackend();
      throw new SecurityRequestError(resp.status, path, text.slice(0, 200) || resp.statusText);
    }
    return (await resp.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

export type PermissionMode = "read_only" | "restricted" | "unrestricted";

/** 三种模式的中文名（与后端 MODE_LABELS 逐字一致）。 */
export const PERMISSION_MODE_LABELS: Record<PermissionMode, string> = {
  read_only: "只读模式",
  restricted: "受限模式",
  unrestricted: "完全无限制模式",
};

/** 首次安装默认：受限模式（契约 §1 DEFAULT_MODE）。 */
export const DEFAULT_PERMISSION_MODE: PermissionMode = "restricted";

export const PERMISSION_MODES: readonly PermissionMode[] = [
  "read_only",
  "restricted",
  "unrestricted",
];

/** 每种模式对用户意味着什么（契约 §2.3；文案必须诚实，不把受限说成安全沙箱）。 */
export const PERMISSION_MODE_DESCRIPTIONS: Record<PermissionMode, string> = {
  read_only:
    "QIO 可以读取文件；不能修改、创建或删除你的业务文件，也不能执行会改动文件的脚本。应用自身的运行数据（日志、审批状态）与你主动导入材料时的落盘不受影响。",
  restricted:
    "首次安装的默认模式。读取默认允许；修改或删除你的原有文件需要你逐项审批，QIO 自主生成的工作文件可以直接写。",
  unrestricted:
    "普通操作不再逐项审批，但仍受操作系统账户权限约束（QIO 的受限子进程不是安全沙箱）。直接改写 QIO 内部运行数据仍然需要连续三次确认。",
};

/** 旧值 → 新模式的映射（与后端 LEGACY_MODE_MAP 逐字一致）。 */
export const LEGACY_MODE_MAP: Record<string, PermissionMode> = {
  default: "restricted",
  plan: "read_only",
  "accept-edits": "restricted",
  bypass: "unrestricted",
};

/** 旧值的中文名（设置页在旧数据上显示「旧值：…」时用）。 */
export const LEGACY_VALUE_LABELS: Record<string, string> = {
  default: "默认（低危自动，高危审批）",
  plan: "只读规划（不写不执行）",
  "accept-edits": "接受编辑（文件编辑自动，命令仍审批）",
  bypass: "全放行（危险，仅信任环境）",
};

export interface SecurityModeState {
  mode: PermissionMode;
  label: string;
  /** 旧客户端可用的等价旧值（后端给；缺失时按映射算） */
  legacyValue: string | null;
  /** true = 老后端兼容读取（来自 /api/settings/computer），不是新接口 */
  compat: boolean;
}

/** 未知值/旧值 → 新模式；未知值不静默提高（与后端 normalize_mode 同口径）。 */
export function normalizePermissionMode(value: unknown): PermissionMode {
  const raw = typeof value === "string" ? value.trim() : "";
  if ((PERMISSION_MODES as readonly string[]).includes(raw)) return raw as PermissionMode;
  return LEGACY_MODE_MAP[raw] ?? DEFAULT_PERMISSION_MODE;
}

/** 新模式 → 等价旧值（后端没给 legacy_value 时用）。 */
export function legacyValueFor(mode: PermissionMode): string {
  switch (mode) {
    case "read_only":
      return "plan";
    case "unrestricted":
      return "bypass";
    default:
      return "default";
  }
}

/** 旧值说明；未知旧值如实显示原值，不假装认识。 */
export function legacyValueLabel(value: string | null | undefined): string {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  return LEGACY_VALUE_LABELS[raw] ?? "旧值：" + raw;
}

/** 读当前模式（新接口优先；新接口不存在时退回旧设置接口，明确标记 compat）。 */
export async function getSecurityMode(): Promise<SecurityModeState> {
  try {
    const res = await request<{ mode?: unknown; label?: unknown; legacy_value?: unknown }>(
      "/api/security/mode",
      { timeoutMs: 15_000 },
    );
    const mode = normalizePermissionMode(res.mode);
    const legacyRaw = typeof res.legacy_value === "string" ? res.legacy_value : "";
    return {
      mode,
      label: typeof res.label === "string" && res.label ? res.label : PERMISSION_MODE_LABELS[mode],
      legacyValue: legacyRaw || legacyValueFor(mode),
      compat: false,
    };
  } catch (err) {
    if (!(err instanceof SecurityRequestError) || err.status !== 404) throw err;
    const old = await request<{ permission_mode?: unknown }>("/api/settings/computer", {
      timeoutMs: 15_000,
    });
    const mode = normalizePermissionMode(old.permission_mode);
    return {
      mode,
      label: PERMISSION_MODE_LABELS[mode],
      // 旧接口给的是旧值：原样展示「旧值：default → 受限模式」的映射事实
      legacyValue: typeof old.permission_mode === "string" ? old.permission_mode : legacyValueFor(mode),
      compat: true,
    };
  }
}

/**
 * 用户**主动**切换模式。`userConfirmed` 固定为 true 由调用方语义保证：
 * 这个函数只从设置页按钮事件调用，不接受模型/工具传入的 mode。
 */
export async function setSecurityMode(mode: PermissionMode): Promise<SecurityModeState> {
  if (!(PERMISSION_MODES as readonly string[]).includes(mode)) {
    throw new Error("权限模式只能是 read_only / restricted / unrestricted");
  }
  const res = await request<{ mode?: unknown; label?: unknown; legacy_value?: unknown }>(
    "/api/security/mode",
    {
      method: "PUT",
      timeoutMs: 20_000,
      // 必须显式声明 JSON：FastAPI 的 `body: dict` 在没有 Content-Type 时会把
      // 原始字节当字符串校验，返回 422「Input should be a valid dictionary」
      // （用户实测：/api/security/mode -> 422）。
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, user_confirmed: true }),
    },
  );
  const next = normalizePermissionMode(res.mode);
  return {
    mode: next,
    label: typeof res.label === "string" && res.label ? res.label : PERMISSION_MODE_LABELS[next],
    legacyValue:
      typeof res.legacy_value === "string" && res.legacy_value
        ? res.legacy_value
        : legacyValueFor(next),
    compat: false,
  };
}

/** 模式切换的生效说明：两种方向都必须诚实（新操作立即生效；已开始的执行沿用开始权限）。 */
export function permissionModeSwitchNote(from: PermissionMode, to: PermissionMode): string {
  if (from === to) return "";
  const base = "新操作立即生效；已经开始的执行沿用开始时的权限，不会中途被改变。";
  if (to === "read_only") {
    return "收紧到只读模式：" + base + "等待中的审批不会自动批准，仍需你逐项决定。";
  }
  if (to === "restricted" && from === "read_only") {
    return "放宽到受限模式：" + base + "原来被拒绝的路径会重新按受限规则判定；等待中的审批不会自动批准。";
  }
  if (to === "unrestricted") {
    return (
      "放宽到完全无限制模式：" +
      base +
      "等待中的审批不会因此自动通过；直接改写 QIO 内部运行数据仍需连续三次确认。"
    );
  }
  return "收紧到受限模式：" + base + "等待中的审批不会自动批准，仍需你逐项决定。";
}

/** 设置页的安全边界说明（不能只靠颜色表达）。 */
export const PERMISSION_MODE_SAFETY_NOTE =
  "只有你能修改权限模式：QIO 的模型、工具、脚本与子智能体都没有修改入口。收紧会立即生效；放宽不会自动批准任何等待中的审批，也不会把已拒绝/已过期的审批复活。";
