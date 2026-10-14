/**
 * 对话表格适配的**判定**部分（纯函数，可单测；DOM 部分在 composables/useQioTables.ts）。
 *
 * 用户规则（高于旧规范里「代码块可以横向滚动」的约定）：
 * - 表格必须落在它实际所属的**消息正文可用宽度**内，不得横向滚动、不得裁切；
 * - 列数/内容让每列过窄、明显降低可读性时，重排为纵向记录（列名 + 值），
 *   保留原记录顺序、列顺序、表头与内容；
 * - 切换依据**组件实际可用宽度 + 列结构 + 真实内容最小宽度**，不是整机窗口宽度或固定列数；
 * - 临界宽度用滞回（进入阈值 < 退出阈值）避免来回振荡。
 */

export type QioTableMode = "table" | "records";

/** 每列窄于该宽度即认为「明显降低可读性」→ 进入记录模式。 */
export const QIO_TABLE_ENTER_COLUMN_PX = 88;
/** 每列宽于该宽度才允许从记录模式退回表格模式（滞回带 = 88–112px）。 */
export const QIO_TABLE_COMFORT_COLUMN_PX = 112;

/** 从记录模式退回表格时，真实最小宽度要留出的余量（px）。 */
export const QIO_TABLE_RESTORE_SLACK_PX = 12;

export interface QioTableDecisionInput {
  /** 所属正文的**实际**可用宽度（测量值，不是窗口宽度）。 */
  availablePx: number;
  /** 列数（表头列数，含空/重复表头）。 */
  columnCount: number;
  /** 表格内容的真实最小宽度（min-content，0 表示未测量）。 */
  naturalPx: number;
  /** 当前模式（用于滞回）。 */
  current: QioTableMode;
}

/**
 * 判定单个表格应处于哪种模式。
 * 只有一列时不做记录重排（单列表格没有「列名 + 值」的意义，且不会过窄）。
 */
export function decideQioTableMode(input: QioTableDecisionInput): QioTableMode {
  const available = Math.max(0, input.availablePx);
  const cols = Math.max(0, Math.floor(input.columnCount));
  if (cols <= 1 || available <= 0) return "table";

  const perColumn = available / cols;
  const naturalOverflows = input.naturalPx > 0 && input.naturalPx > available;

  if (input.current === "records") {
    const comfortable = perColumn >= QIO_TABLE_COMFORT_COLUMN_PX;
    const fits = input.naturalPx <= 0 || input.naturalPx <= available - QIO_TABLE_RESTORE_SLACK_PX;
    return comfortable && fits ? "table" : "records";
  }

  if (perColumn < QIO_TABLE_ENTER_COLUMN_PX) return "records";
  if (naturalOverflows) return "records";
  return "table";
}

/** 记录模式下的备用列名：空表头/重复表头用稳定位置身份，不拿列名当唯一键。 */
export function qioColumnLabel(header: string, index: number, duplicated: boolean): string {
  const text = (header ?? "").replace(/\s+/g, " ").trim();
  if (!text) return `第 ${index + 1} 列`;
  if (duplicated) return `${text}（第 ${index + 1} 列）`;
  return text;
}
