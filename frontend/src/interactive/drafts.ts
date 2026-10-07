/**
 * 草稿持久化的纯逻辑（子智能体 C 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §9.4 / §9.5。
 *
 * 设计要点（为什么需要这一层）：
 * - 聊天草稿与卡片草稿要**分别持久保存**，并且都要能处理「保存回执迟到」：
 *   旧请求的返回值不许覆盖更新的内容，所以每次写入带一个单调递增的 seq。
 * - 存储不可用（隐私模式、配额满、被禁用、内容损坏）时必须**明确失败**，
 *   由调用方显示原因并允许重试 —— 不许静默吞掉。
 * - 这里只做纯逻辑与存储读写，不碰网络、不碰 Vue、不发消息。
 */

/** 草稿作用域：聊天按会话/话题，卡片按卡片 id */
export type DraftScope = "chat" | "card";

export interface DraftRecord {
  /** 草稿正文（原样保存，包含换行） */
  text: string;
  /** 最后写入时间（毫秒时间戳），用于清理与调试 */
  updatedAt: number;
  /** 单调递增的写入序号：迟到的回执用它判断自己是否已经过期 */
  seq: number;
}

/**
 * 会话上下文还没加载出来（`currentTopicId` 还是 null）时，聊天草稿先放在这个占位 id 下。
 *
 * 为什么需要它：应用刚启动的一小段时间里用户就可能开始打字，如果这段时间的字没有
 * 归宿，等话题确定时就只能丢掉。占位记录的正文会在话题确定后**迁移**到真正的话题键，
 * 因此这一段输入既不会丢，也不会串到别的话题。
 */
export const UNBOUND_DRAFT_ID = "__unbound__";

/** 存储键前缀：与卡片/聊天之外的其它本地键（主题、设置）分开命名空间 */
const STORAGE_PREFIX = "qio.draft.";

/** 存储键：不同作用域、不同对象互不干扰 */
export function draftStorageKey(scope: DraftScope, id: string): string {
  return STORAGE_PREFIX + scope + "." + (id || "default");
}

/** 本地存储后端的最小形状（只要这三个方法，方便测试注入替身） */
interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * 取当前可用的本地存储。
 *
 * 每次调用都重新解析，不把结果缓存在模块变量里：
 * - 部分浏览器（隐私模式 / 企业策略）连**读取** `localStorage` 属性都会抛异常，
 *   所以访问本身也要包在 try 里；
 * - 测试可以替换 `globalThis.localStorage` 来验证「存储不可用」这条路径。
 *
 * 用 localStorage 而不是 sessionStorage：需求是「关闭后重开仍能继续编辑」，
 * sessionStorage 活不过关闭标签页/窗口。
 */
function resolveStorage(): { storage: DraftStorage | null; error?: string } {
  try {
    const backend = (globalThis as { localStorage?: DraftStorage | null }).localStorage;
    if (!backend) return { storage: null, error: "当前环境没有本地存储，草稿无法保存在本机" };
    return { storage: backend };
  } catch (err) {
    return { storage: null, error: "浏览器不允许使用本地存储：" + describeError(err) };
  }
}

function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  const text = String(err ?? "").trim();
  return text || "原因未知";
}

/** 存储容量/配额类错误单独说清楚：用户能采取行动（清空间或改用其它浏览器） */
function describeWriteError(err: unknown): string {
  const name = (err as { name?: string } | null)?.name ?? "";
  const code = (err as { code?: number } | null)?.code ?? 0;
  if (name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED" || code === 22 || code === 1014) {
    return "本机存储已满，草稿没有保存成功（清理一些空间后可以重试）";
  }
  return "草稿写入失败：" + describeError(err);
}

/**
 * 本地存储是否可用（界面据此解释「为什么草稿没有保存」）。
 *
 * 只探测、不写入：探测写入会在只读环境里制造一条假数据。
 */
export function draftStorageAvailable(): { ok: boolean; error?: string } {
  const { storage, error } = resolveStorage();
  if (!storage) return { ok: false, error };
  return { ok: true };
}

/**
 * 读一条草稿；不存在、损坏或存储不可用时返回 null（调用方据此走「没有草稿」路径）。
 *
 * 损坏（不是本模块写的 JSON、字段类型不对）也按「没有草稿」处理：
 * 与其把一段乱码塞进输入框，不如当作没有，用户重打一遍即可。
 */
export function readDraft(key: string): DraftRecord | null {
  const { storage } = resolveStorage();
  if (!storage) return null;
  let raw: string | null = null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (typeof raw !== "string" || raw === "") return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const record = parsed as { text?: unknown; updatedAt?: unknown; seq?: unknown };
    if (typeof record.text !== "string") return null;
    return {
      text: record.text,
      updatedAt: typeof record.updatedAt === "number" && Number.isFinite(record.updatedAt) ? record.updatedAt : 0,
      seq: typeof record.seq === "number" && Number.isFinite(record.seq) ? record.seq : 0,
    };
  } catch {
    return null;
  }
}

/**
 * 写一条草稿；返回真实结果，失败必须带原因（调用方要显示给用户）。
 *
 * 空文本**不写记录**：空草稿就是「没有草稿」。调用方要删记录用 removeDraft，
 * 这里只保证「写空串不会留下一条看着像有草稿的空记录」。
 */
export function writeDraft(key: string, text: string, seq: number): { ok: boolean; error?: string } {
  const { storage, error } = resolveStorage();
  if (!storage) return { ok: false, error: error ?? "本地存储不可用，草稿无法保存" };
  const record: DraftRecord = {
    text: text ?? "",
    updatedAt: Date.now(),
    seq: Number.isFinite(seq) ? seq : 0,
  };
  try {
    storage.setItem(key, JSON.stringify(record));
    return { ok: true };
  } catch (err) {
    return { ok: false, error: describeWriteError(err) };
  }
}

/** 删除一条草稿（例如发送成功后）。删不掉时不能假装删掉了，但也没有更好的补救。 */
export function removeDraft(key: string): void {
  const { storage } = resolveStorage();
  if (!storage) return;
  try {
    storage.removeItem(key);
  } catch {
    /* 删不掉：下一次读取还会看到旧草稿，至少内容没有丢 */
  }
}

/**
 * 迟到的保存回执是否应当被忽略。
 *
 * 序号是单调递增的：任何**小于**当前序号的回执都对应一份已经被更新内容取代的旧稿，
 * 应用它就会把用户后来输入的新文字覆盖掉。等于当前序号的回执是最新的，必须接受。
 */
export function isStaleReceipt(receiptSeq: number, currentSeq: number): boolean {
  return receiptSeq < currentSeq;
}
