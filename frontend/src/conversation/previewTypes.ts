/**
 * 只读预览的身份与阅读位置（Lead 定义的共享形状；C 实现渲染与标签管理）。
 *
 * 身份规则写在 docs/conversation-page-contract-20261022.md §4.3：
 * 不能用文件名当身份；同名不同文件必须是两个标签。
 */

export type PreviewKind =
  | "image"
  | "pdf"
  | "docx"
  | "xlsx"
  | "pptx"
  | "text"
  | "code"
  | "markdown"
  | "audio"
  | "video"
  | "unsupported";

export type PreviewSource = "attachment" | "reply_code" | "task_output";

export interface PreviewTarget {
  /** 标签唯一身份（见契约 §4.3）。 */
  id: string;
  kind: PreviewKind;
  title: string;
  source: PreviewSource;
  attachmentId?: string;
  /** 附件/文件内容版本：文件更新后旧请求不得覆盖新版本。 */
  version?: string;
  messageId?: string;
  blockIndex?: number;
  taskId?: string;
  topicId?: string;
  filePath?: string;
  mime?: string;
  size?: number;
  /** 最近一次在界面出现/更新的时间（ISO）。 */
  updatedAt?: string;
}

/** 每个标签各自的轻量阅读位置；只存数字与短字符串，不存正文。 */
export interface PreviewPosition {
  scrollTop?: number;
  page?: number;
  sheet?: string;
  zoom?: number;
  search?: string;
  line?: number;
}

export interface PreviewTabState {
  /** 与 target.id 相同；单列出来方便持久化与去重。 */
  id: string;
  target: PreviewTarget;
  position: PreviewPosition;
}

/** 生成附件标签身份：同一附件同一版本只能有一个标签。 */
export function attachmentPreviewId(attachmentId: string, version?: string | null): string {
  return version ? `attachment:${attachmentId}@${version}` : `attachment:${attachmentId}`;
}

/** 回复里代码块的稳定身份：同一条回复的不同代码块各自独立。 */
export function replyCodePreviewId(messageId: string, blockIndex: number): string {
  return `replycode:${messageId}:${blockIndex}`;
}

/** 任务成果文件的稳定身份：任务 + 附件 id（或规范路径）。 */
export function taskOutputPreviewId(taskId: string, ref: string): string {
  return `task:${taskId}:${ref}`;
}
