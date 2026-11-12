/**
 * 工具失败的「可读事实」纯映射（#12）。
 *
 * 界面只消费后端给的**程序字段**（toolCode / toolCategory / toolFileRead），
 * 绝不从 toolError 的中文文本里猜「是不是文件读取失败」：
 *
 *   * 新事件：toolCode === "file_read_failed"，附带 file_read = { name / reason /
 *     next_step / diagnostic } —— 直接展示给用户的安全事实；
 *   * 旧事件 / 历史预览：没有 file_read 对象，回退到
 *     toolName === "read_attachment" 且 category ∈ io_error / invalid_data / unavailable；
 *   * 有别的稳定 code（权限 / 审批 / 限额 / 超时…）时一律不归成文件读取。
 *
 * 这里只做映射，不发请求、不读写 store，方便单测与两个组件（MessageItem / TurnProcess）共用。
 */
import type { StreamMessage } from "../stores/session";

export const FILE_READ_TOOL = "read_attachment";
export const FILE_READ_FAILED_CODE = "file_read_failed";

/** 旧数据回退：只有这三类「内容真的读不出来」才算文件读取失败。 */
export const FILE_READ_FALLBACK_CATEGORIES = new Set(["io_error", "invalid_data", "unavailable"]);

const CATEGORY_REASONS: Record<string, string> = {
  io_error: "读取文件时出错了（文件可能被移动、占用，或没有读取权限）。",
  invalid_data: "文件内容无法按当前格式读取（文件可能已损坏）。",
  unavailable: "这份文件的内容现在不可用。",
};

const CATEGORY_NEXT_STEPS: Record<string, string> = {
  io_error: "可以重新发送这个文件，或打开右侧预览确认内容。",
  invalid_data: "可以换一份文件，或把内容贴成文本再发。",
  unavailable: "可以重新上传这个文件，或打开右侧预览确认。",
};

const CODE_REASON_FALLBACK = "读取文件时发生了错误。";
const CODE_NEXT_STEP_FALLBACK = "可以重新发送这个文件，或打开右侧预览确认内容。";

/** 工具的终态语义（与卡片一致；旧数据按 toolOk 兜底）。 */
export function toolTerminalState(m: StreamMessage): string {
  return m.toolStatus ?? (m.toolOk === false ? "failed" : "success");
}

function toolIdentity(m: StreamMessage): string {
  return String(m.toolName ?? m.presentation?.tool ?? "").trim();
}

/**
 * 这次失败是不是「文件真的没读到」。
 *
 * 有稳定 code 时只认 code（其它 code 一律不算）；没有 code 的旧数据才用
 * toolName + category 回退 —— 取消 / 等待审批 / 超时 / 权限都不算文件读取失败。
 */
export function isFileReadFailureTool(m: StreamMessage): boolean {
  if (toolTerminalState(m) !== "failed") return false;
  if (m.toolResultStatus === "awaiting_approval") return false;
  const code = String(m.toolCode ?? "").trim();
  if (code) return code === FILE_READ_FAILED_CODE;
  if (toolIdentity(m) !== FILE_READ_TOOL) return false;
  return FILE_READ_FALLBACK_CATEGORIES.has(String(m.toolCategory ?? "").trim().toLowerCase());
}

export interface FileReadFailureDiagnosticRow {
  label: string;
  value: string;
}

export interface FileReadFailureCopy {
  /** 卡面结论：「这次没读到文件内容」+（有名称时「：名称」）。 */
  headline: string;
  /** 真实原因（后端 file_read.reason 优先，缺失才用类别回退）。 */
  reason: string;
  /** 下一步（后端 file_read.next_step 优先，缺失才用类别回退）。 */
  nextStep: string;
  name: string;
  attachmentId: string;
  code: string;
  category: string;
  categoryLabel: string;
  diagnostic: string;
  toolName: string;
  /** 展开诊断区逐行显示的事实（工具名 / 附件 id / code / 类别 / diagnostic）。 */
  rows: FileReadFailureDiagnosticRow[];
}

/** 把一条文件读取失败映射成可读事实；不是文件读取失败时返回 null。 */
export function fileReadFailureOf(m: StreamMessage): FileReadFailureCopy | null {
  if (!isFileReadFailureTool(m)) return null;
  const fact = m.toolFileRead ?? null;
  const name = String(fact?.name ?? "").trim();
  const attachmentId = String(fact?.attachmentId ?? "").trim();
  const category = String(m.toolCategory ?? "").trim().toLowerCase();
  const categoryLabel = String(m.toolCategoryLabel ?? "").trim();
  const code = String(m.toolCode ?? "").trim();
  const diagnostic = String(fact?.diagnostic ?? "").trim();
  const toolName = String(m.toolName ?? "").trim() || FILE_READ_TOOL;
  const reason =
    String(fact?.reason ?? "").trim() ||
    CATEGORY_REASONS[category] ||
    (code === FILE_READ_FAILED_CODE ? CODE_REASON_FALLBACK : CATEGORY_REASONS.io_error);
  const nextStep =
    String(fact?.nextStep ?? "").trim() ||
    CATEGORY_NEXT_STEPS[category] ||
    CODE_NEXT_STEP_FALLBACK;
  const rows: FileReadFailureDiagnosticRow[] = [{ label: "工具", value: toolName }];
  if (attachmentId) rows.push({ label: "附件", value: attachmentId });
  if (code) rows.push({ label: "结果码", value: code });
  if (category) {
    rows.push({
      label: "类别",
      value: categoryLabel ? category + "（" + categoryLabel + "）" : category,
    });
  }
  if (diagnostic) rows.push({ label: "诊断", value: diagnostic });
  return {
    headline: name ? "这次没读到文件内容：" + name : "这次没读到文件内容",
    reason,
    nextStep,
    name,
    attachmentId,
    code,
    category,
    categoryLabel,
    diagnostic,
    toolName,
    rows,
  };
}

/** 文件读取失败涉及的真实文件名（没有 name 时为空串，不编造）。 */
export function fileReadFailureName(m: StreamMessage): string {
  return String(m.toolFileRead?.name ?? "").trim();
}
