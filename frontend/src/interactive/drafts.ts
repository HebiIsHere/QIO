/**
 * 草稿持久化的纯逻辑（子智能体 A 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §9.4 / §9.5 / §10.4 / §10.5 / §11.1 / §11.2 / §11.3。
 *
 * 设计要点（为什么需要这一层）：
 * - 聊天草稿与卡片草稿要**分别持久保存**，并且都要能处理「保存回执迟到」：
 *   旧请求的返回值不许覆盖更新的内容，所以每次写入带一个单调递增的 seq。
 * - 存储不可用（隐私模式、配额满、被禁用、内容损坏）时必须**明确失败**，
 *   由调用方显示原因并允许重试 —— 不许静默吞掉。
 * - 卡片草稿有**两种记录身份**（§11.1）：与服务器同步的 `card:<id>` 与本机恢复副本
 *   `card-local:<id>`；恢复必须能发现「只存在于本机」的记录，所以本机记录要能被枚举。
 * - 本机记录要能自己说明：**属于哪个板面**（不给别的板面恢复）、**自己是第几版**（旧版本作用不许
 *   删掉后来新建的版本）、**是不是一份待同步的清除依据**（§11.2：清除在服务器确认前不算已同步）。
 *   判断新旧只看这条记录自己的归属/版本/确认状态，绝不用「服务器整个草稿集合的更新时间」
 *   或别张卡片的保存时间（§11.3）。
 * - 这里只做纯逻辑与存储读写，不碰网络、不碰 Vue、不发消息。
 */

/** 草稿作用域：聊天按会话/话题，卡片按卡片 id */
export type DraftScope = "chat" | "card";

/** 本机记录的种类：编辑副本 / 待确认的清除依据（§11.2） */
export type DraftRecordKind = "draft" | "cleared";

export interface DraftRecord {
  /** 草稿正文（原样保存，包含换行） */
  text: string;
  /** 最后写入时间（毫秒时间戳），用于清理与调试 */
  updatedAt: number;
  /** 单调递增的写入序号：迟到的回执用它判断自己是否已经过期 */
  seq: number;
  /** 这条记录属于哪个板面（卡片草稿用；恢复时不给别的板面恢复内容，§11.1） */
  boardId?: string;
  /** 记录种类：编辑副本 / 待同步的清除依据（§11.2）。旧记录没有这个字段，按「编辑副本」认 */
  kind?: DraftRecordKind;
  /**
   * 该对象上单调递增的本地版本号，**跨刷新、重开仍然递增**。
   *
   * 为什么不能用内存里的 seq：页面重开后 seq 从 0 重新开始，无法判断
   * 「这次清除针对的是哪一版」；旧版本的清除回执晚到时就会删掉后来新建的版本（§11.2）。
   */
  version?: number;
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

/*
 * 卡片草稿的两种记录身份（契约 §11.1）。
 *
 * - `card:<id>`        与服务器同步的草稿（保存成功后服务器也有）
 * - `card-local:<id>`  **本机恢复副本**：输入时同步写的保护记录，可能还没上传
 *
 * 必须分成两种身份：恢复要能发现「只存在于本机」的记录，又不能在成功后误删服务器上还有的草稿。
 * 读取路径与写入路径一律用这两个构造函数，不再手写字符串前缀。
 */
export const CARD_DRAFT_PREFIX = "card:";
export const CARD_LOCAL_DRAFT_PREFIX = "card-local:";

export function cardDraftKey(cardId: string): string {
  return CARD_DRAFT_PREFIX + cardId;
}

export function cardLocalDraftKey(cardId: string): string {
  return CARD_LOCAL_DRAFT_PREFIX + cardId;
}

/** 从任一卡片草稿键里取出卡片 id（两种键都认）；不是卡片草稿键时返回 null。 */
export function cardIdFromDraftKey(key: string): string | null {
  if (key.startsWith(CARD_LOCAL_DRAFT_PREFIX)) return key.slice(CARD_LOCAL_DRAFT_PREFIX.length) || null;
  if (key.startsWith(CARD_DRAFT_PREFIX)) return key.slice(CARD_DRAFT_PREFIX.length) || null;
  return null;
}

export function isCardDraftKey(key: string): boolean {
  return cardIdFromDraftKey(key) !== null;
}

/*
 * 本机记录的实际存储键。
 *
 * 沿用上一版就写下的布局 `qio.draft.card.local-<id>`（历史记录仍能直接读到，不做迁移），
 * 但字符串只在这里拼一次：其它地方一律走 cardLocalDraftStorageKey / readCardLocalDraft 等函数，
 * 不再出现手写的 `"local-" + id`。
 */
const CARD_LOCAL_STORAGE_ID_PREFIX = "local-";

export function cardLocalDraftStorageKey(cardId: string): string {
  return draftStorageKey("card", CARD_LOCAL_STORAGE_ID_PREFIX + cardId);
}

/**
 * 本机存着记录的卡片 id 列表（★恢复必须能发现「只存在于本机」的记录）。
 *
 * 只扫本机存储，不依赖服务器返回了什么，也不依赖内存里有没有对应的键。
 * 「存在」一律按**记录是否存在**判断：正文为空也算存在（§10.4 / §11.1），
 * 待同步的清除依据也在列表里（调用方按 kind 区分，§11.2）。
 */
export function listLocalCardDraftIds(): string[] {
  const { storage } = resolveStorage();
  if (!storage) return [];
  const prefix = cardLocalDraftStorageKey("");
  const ids: string[] = [];
  try {
    const count = typeof storage.length === "number" ? storage.length : 0;
    const readKey = storage.key?.bind(storage);
    if (!readKey) return [];
    for (let index = 0; index < count; index += 1) {
      const key = readKey(index);
      if (!key || !key.startsWith(prefix)) continue;
      const cardId = key.slice(prefix.length);
      if (cardId) ids.push(cardId);
    }
  } catch {
    // 存储不可用（隐私模式 / 被策略禁用）：当作没有本机记录，调用方按「恢复不了」如实说明
    return [];
  }
  return ids;
}

/** 本地存储后端的最小形状（前三个方法必需；枚举键可选，用来发现「只存在于本机」的记录） */
interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  readonly length?: number;
  key?(index: number): string | null;
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
    const record = parsed as {
      text?: unknown;
      updatedAt?: unknown;
      seq?: unknown;
      boardId?: unknown;
      kind?: unknown;
      version?: unknown;
    };
    if (typeof record.text !== "string") return null;
    const result: DraftRecord = {
      text: record.text,
      updatedAt: typeof record.updatedAt === "number" && Number.isFinite(record.updatedAt) ? record.updatedAt : 0,
      seq: typeof record.seq === "number" && Number.isFinite(record.seq) ? record.seq : 0,
    };
    if (typeof record.boardId === "string" && record.boardId) result.boardId = record.boardId;
    if (record.kind === "draft" || record.kind === "cleared") result.kind = record.kind;
    if (typeof record.version === "number" && Number.isFinite(record.version)) result.version = record.version;
    return result;
  } catch {
    return null;
  }
}

/** 写入结果：真实结果 + 失败原因 + 这次写入得到的本机版本号（§11.3 要如实显示，不许静默） */
export interface DraftWriteResult {
  ok: boolean;
  error?: string;
  /** 这次写入针对的本机版本号（写失败时也返回，调用方仍可用它来说明「哪一版没保护上」） */
  version: number;
}

/** 底层写入：只做序列化与存储，不解释语义 */
function writeRecord(key: string, record: DraftRecord): DraftWriteResult {
  const { storage, error } = resolveStorage();
  if (!storage) return { ok: false, error: error ?? "本地存储不可用，草稿无法保存", version: record.version ?? 0 };
  try {
    storage.setItem(key, JSON.stringify(record));
    return { ok: true, version: record.version ?? 0 };
  } catch (err) {
    return { ok: false, error: describeWriteError(err), version: record.version ?? 0 };
  }
}

/** 读一条记录、算出它的下一个版本号（本机单调递增，跨刷新不重复） */
function nextVersion(key: string): number {
  const previous = readDraft(key);
  return (typeof previous?.version === "number" ? previous.version : 0) + 1;
}

/**
 * 写一条草稿；返回真实结果，失败必须带原因（调用方要显示给用户）。
 *
 * 空文本**不写记录**：空草稿就是「没有草稿」。调用方要删记录用 removeDraft，
 * 这里只保证「写空串不会留下一条看着像有草稿的空记录」。
 */
export function writeDraft(key: string, text: string, seq: number): { ok: boolean; error?: string } {
  const record: DraftRecord = {
    text: text ?? "",
    updatedAt: Date.now(),
    seq: Number.isFinite(seq) ? seq : 0,
  };
  const result = writeRecord(key, record);
  return result.ok ? { ok: true } : { ok: false, error: result.error };
}

/*
 * ---------- 卡片的本机恢复记录（§11.1 / §11.2 / §11.3） ----------
 */

/** 读某一卡片的本机记录（编辑副本或待同步的清除依据都算记录） */
export function readCardLocalDraft(cardId: string): DraftRecord | null {
  return readDraft(cardLocalDraftStorageKey(cardId));
}

/** 这条记录是不是一份**编辑草稿**（待同步的清除依据不是草稿，不能恢复成文字） */
export function isDraftRecord(record: DraftRecord | null): boolean {
  return record !== null && record.kind !== "cleared";
}

/**
 * 写下某一卡片的编辑副本（输入时同步调用，不等防抖、不等网络）。
 *
 * 归属（boardId）与版本号都写进记录自己：恢复时**只看这条记录**就能判断
 * 「是不是这个板面的」「是不是比服务器那份新」（§11.3）。
 */
export function writeCardLocalDraft(
  cardId: string,
  text: string,
  options: { boardId?: string; seq?: number } = {},
): DraftWriteResult {
  const key = cardLocalDraftStorageKey(cardId);
  const version = nextVersion(key);
  const record: DraftRecord = {
    text: text ?? "",
    updatedAt: Date.now(),
    seq: Number.isFinite(options.seq) ? (options.seq as number) : 0,
    kind: "draft",
    version,
  };
  if (options.boardId) record.boardId = options.boardId;
  return writeRecord(key, record);
}

/**
 * 写下「这份草稿已被用户清除、等服务器确认」的依据（§11.2）。
 *
 * 删除是一项**待确认的变化**：请求失败或用户正常刷新后，靠这条记录仍然知道要清掉哪一份，
 * 而不是把用户清掉的文字当没清过、刷新后复活。
 */
export function writeCardLocalClear(
  cardId: string,
  options: { boardId?: string; seq?: number } = {},
): DraftWriteResult {
  const key = cardLocalDraftStorageKey(cardId);
  const version = nextVersion(key);
  const record: DraftRecord = {
    text: "",
    updatedAt: Date.now(),
    seq: Number.isFinite(options.seq) ? (options.seq as number) : 0,
    kind: "cleared",
    version,
  };
  if (options.boardId) record.boardId = options.boardId;
  return writeRecord(key, record);
}

/**
 * 删掉某一卡片的本机记录；给了 expectVersion 时**只删这一版**。
 *
 * 版本守卫解决的是 §11.2 的「旧版本的清除不许删掉后来新建的版本」：
 * 用户在清除之后又编辑了新内容，记录已经是新版本，迟到的清除确认不能把它删掉。
 * 记录不存在时返回 true（本来就没有，不需要删）。
 */
export function removeCardLocalDraft(cardId: string, expectVersion?: number): boolean {
  const key = cardLocalDraftStorageKey(cardId);
  const current = readDraft(key);
  if (current && typeof expectVersion === "number" && current.version !== expectVersion) return false;
  removeDraft(key);
  return true;
}

/**
 * 记录是否存在（**正文为空也算存在**）。
 *
 * 为什么需要：`readDraft(key)?.text || card.content` 这种写法会把「用户把正文删空后保存的草稿」
 * 当成「没有草稿」，于是重开编辑器时旧正文又冒出来、把空草稿盖掉。
 * 判断存在性要用这个函数，不要用空字符串的真假值。
 */
export function hasDraftRecord(key: string): boolean {
  return readDraft(key) !== null;
}

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
