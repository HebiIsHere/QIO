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

/**
 * 一张卡片**未完成输入**里的附加字段（N6）。
 *
 * 为什么必须跟正文一起存：用户编辑的是「这一张卡片这次要怎么改」，正文与网址/标题/文件名/
 * 图片名/代码语言是**同一份未完成输入**。只存正文的话，刷新或正常关闭重开之后，
 * 网址、标题、名称、语言会悄悄回退成正式卡片上的旧值 —— 用户以为自己改过的都还在。
 *
 * 取值必须原样保留**空串**：空串代表「用户明确清空了这个字段」，恢复时绝不能用正式值回填
 * （那等于把用户删掉的旧值又塞回来）。
 */
export interface CardDraftMetaInput {
  /** file / image 的名称 */
  name?: string;
  /** code 的语言 */
  language?: string;
  /** url 的网址 */
  href?: string;
  /** url 的标题 */
  title?: string;
}

/** 一张卡片的完整未完成输入：正文 + 适用附加字段（可缺省 = 这条记录只改了正文） */
export interface CardDraftInput {
  /** 正文（可以为空串：空草稿是有效编辑状态，§10.4） */
  text: string;
  /** 附加字段；缺省表示这条记录没有附加快照（旧记录或只改了正文） */
  meta?: CardDraftMetaInput;
}

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
  /**
   * 未完成输入的附加字段（N6）。旧记录没有这个字段：读取时按「没有附加快照」处理，
   * 恢复时回退到正式卡片上的值（兼容行为，不是把空值当成清空）。
   */
  meta?: CardDraftMetaInput;
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

/** 存储容量/配额类错误单独识别：用户能采取行动（清理空间或改用其它浏览器） */
function isQuotaError(err: unknown): boolean {
  const name = (err as { name?: string } | null)?.name ?? "";
  const code = (err as { code?: number } | null)?.code ?? 0;
  return name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED" || code === 22 || code === 1014;
}

/** 存储容量/配额类错误单独说清楚：用户能采取行动（清理一些空间或改用其它浏览器） */
function describeWriteError(err: unknown): string {
  if (isQuotaError(err)) return "本机存储已满，草稿没有保存成功（清理一些空间后可以重试）";
  return "草稿写入失败：" + describeError(err);
}

/*
 * 删除失败的措辞与写入分开（契约 M2 / 条目 13）：
 * 「没保存上」和「旧记录没删掉」对用户是两件事 —— 前者代表这次输入暂时没有保护，
 * 后者代表下次打开可能又看到一份本该消失的内容。混成一句话会让用户判断错该做什么。
 */
function describeRemoveError(err: unknown): string {
  if (isQuotaError(err)) return "本机存储已满，这条记录没有删掉（清理一些空间后可以重试）";
  return "删除本机记录失败：" + describeError(err);
}

/** 删除本身没抛错、但结果无法核实（读记录就抛）时的说明：宁可说「没确认」，不报成功 */
function describeRemoveUnverified(err: unknown): string {
  return "没能确认本机记录是否已经删除：" + describeError(err);
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
/** 附加字段的固定顺序：写入、读取与展示都按这个顺序，保证同一份输入可复跑 */
export const CARD_DRAFT_META_FIELDS = ["name", "language", "href", "title"] as const;

/** 附加字段名 */
export type CardDraftMetaField = (typeof CARD_DRAFT_META_FIELDS)[number];

/**
 * 归一化未完成输入的附加字段（N6）。
 *
 * - 只认四个已知字段，且必须是字符串：**空串保留**（明确清空），未知字段与非法类型一律丢掉；
 * - 一个可用字段都没有时返回 null（不制造空的 meta 对象：旧调用写出的记录必须保持原样）。
 */
export function normalizeCardDraftMeta(meta: CardDraftMetaInput | null | undefined): CardDraftMetaInput | null {
  if (!meta || typeof meta !== "object") return null;
  const result: CardDraftMetaInput = {};
  let any = false;
  for (const field of CARD_DRAFT_META_FIELDS) {
    const value = meta[field];
    if (typeof value === "string") {
      result[field] = value;
      any = true;
    }
  }
  return any ? result : null;
}

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
      meta?: unknown;
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
    const meta = normalizeCardDraftMeta(record.meta as CardDraftMetaInput | null | undefined);
    if (meta) result.meta = meta;
    return result;
  } catch {
    return null;
  }
}

/**
 * 写入结果：真实结果 + 失败原因 + 本机版本号（§11.3 要如实显示，不许静默）。
 *
 * 版本号分两个口径，调用方**必须**分清（条目 12 / 契约 M2）：
 * - `version`：这次写入**计划**写下的版本号。写入成功时它就是真实落盘的版本；
 *   写入失败时它只是一次没有实现的计划 —— 调用方不得把它登记成「已确认版本」，
 *   否则版本守卫会对不上，磁盘上的旧记录删不掉、重开后又复活。
 * - `committedVersion`：这次**真的写进存储**的版本号（存储里现在的版本）。
 *   写入失败时为 undefined：这次没有推进本机版本，磁盘上仍是旧记录。
 */
export interface DraftWriteResult {
  ok: boolean;
  error?: string;
  /** 计划写入的本机版本号；写入成功时等于 committedVersion */
  version: number;
  /** 真实落盘的版本号（存储里现在的版本）；写入失败时为 undefined */
  committedVersion?: number;
}

/**
 * 删除没达成期望时的原因分类（调用方据此决定界面说什么，契约 M2 / 条目 13）。
 *
 * - `missing-record`：本来就没有这条记录。**不是失败**（ok=true），调用方不做多余提示。
 * - `version-guard`：记录已经是别的版本，按版本守卫**有意保留**（ok=false）。
 *   这是 §11.2 的正常分支，不是故障 —— 界面不能显示成「删除失败」。
 * - `storage-failure`：存储不可用或 removeItem 抛错，删除**真的失败了**（ok=false）。
 *   调用方必须显示真实原因并提供重试，也不得把这次操作标成完成。
 */
export type DraftRemoveReason = "missing-record" | "version-guard" | "storage-failure";

/**
 * 底层删除的真实结果。
 *
 * 为什么专门要有这个返回值：删除是「让旧内容不再出现」的唯一手段，静默失败
 * （返回 void，或把 false 混着用）会让界面显示成已完成，而用户重开后又看到那份
 * 本该消失的稿子（条目 13 / 条目 12）。所以删除一律返回：**期望是否达成**（ok）、
 * **是否真的删掉了一条**（removed）、**没达成时的原因分类**（reason）、
 * 以及**可直接显示的真实原因**（error）。
 */
export interface DraftRemoveResult {
  /** 期望是否达成：记录已经不在了就是 true（本来就没有也算） */
  ok: boolean;
  /** 这次是否真的从存储里删掉了一条记录 */
  removed: boolean;
  /** 没达成期望时的原因分类；成功时为 undefined */
  reason?: DraftRemoveReason;
  /** 失败时的真实原因（可直接显示给用户）；version-guard 是有意保留，不带错误 */
  error?: string;
}

/** 底层写入：只做序列化与存储，不解释语义 */
function writeRecord(key: string, record: DraftRecord): DraftWriteResult {
  const { storage, error } = resolveStorage();
  const planned = typeof record.version === "number" && Number.isFinite(record.version) ? record.version : 0;
  if (!storage) return { ok: false, error: error ?? "本地存储不可用，草稿无法保存", version: planned };
  try {
    storage.setItem(key, JSON.stringify(record));
    // 只有真的落盘才给 committedVersion：失败时调用方不能把计划版本当成已确认版本（M2 / 条目 12）
    return typeof record.version === "number" ? { ok: true, version: planned, committedVersion: planned } : { ok: true, version: planned };
  } catch (err) {
    return { ok: false, error: describeWriteError(err), version: planned };
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

/**
 * 读某一卡片的本机记录（编辑副本或待同步的清除依据都算记录）。
 *
 * 兼容**最早期**写下的「纯文本」值（当时这个键直接存正文，没有 JSON 外壳）：
 * 只在值根本不是合法 JSON 时按正文认。JSON 解析成功但形状不对（损坏、字段类型错）
 * 仍然按「没有记录」处理 —— 不把损坏内容或 JSON 字面量塞进输入框。
 */
export function readCardLocalDraft(cardId: string): DraftRecord | null {
  const key = cardLocalDraftStorageKey(cardId);
  const record = readDraft(key);
  if (record) return record;
  return readLegacyPlainTextCardLocalRecord(key);
}

/** 旧版纯文本本机记录的回退读取（只在值不是合法 JSON、且不像 JSON 结构时生效） */
function readLegacyPlainTextCardLocalRecord(key: string): DraftRecord | null {
  const { storage } = resolveStorage();
  if (!storage) return null;
  let raw: string | null = null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (typeof raw !== "string" || raw === "") return null;
  const trimmed = raw.trim();
  // 以对象/数组开头的一律当成「损坏的 JSON」，不当作正文（避免把乱码恢复进编辑器）
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) return null;
  let parsed = true;
  try {
    JSON.parse(raw);
  } catch {
    parsed = false;
  }
  if (parsed) return null;
  // 旧纯文本记录没有 kind、没有版本、没有时戳：如实按「未知来源的编辑候选」返回，
  // 由调用方按 legacy 规则要求用户选择（不猜成清除依据）。
  return { text: raw, updatedAt: 0, seq: 0 };
}

/** 这条记录是不是一份**编辑草稿**（待同步的清除依据不是草稿，不能恢复成文字） */
export function isDraftRecord(record: DraftRecord | null): boolean {
  return record !== null && record.kind !== "cleared";
}

/**
 * 写下某一卡片的**完整未完成输入**（正文 + 适用附加字段，N6）。
 *
 * 一次写入同一条记录（不是正文一条、附加字段另一条）：这是「同一份未完成输入」的
 * 唯一落点，不新增第二个写者，也就不会出现「正文恢复了、网址还是旧的」这种半份恢复。
 * 归属（boardId）与版本号都写进记录自己：恢复时**只看这条记录**就能判断
 * 「是不是这个板面的」「是不是比服务器那份新」（§11.3）。
 * 附加字段为空/没有 → 不写 meta 字段，与旧格式逐字节兼容。
 */
export function writeCardDraftInput(
  cardId: string,
  input: CardDraftInput,
  options: { boardId?: string; seq?: number } = {},
): DraftWriteResult {
  const key = cardLocalDraftStorageKey(cardId);
  const version = nextVersion(key);
  const record: DraftRecord = {
    text: input?.text ?? "",
    updatedAt: Date.now(),
    seq: Number.isFinite(options.seq) ? (options.seq as number) : 0,
    kind: "draft",
    version,
  };
  if (options.boardId) record.boardId = options.boardId;
  const meta = normalizeCardDraftMeta(input?.meta);
  if (meta) record.meta = meta;
  return writeRecord(key, record);
}

/**
 * 写下某一卡片的编辑副本正文（输入时同步调用，不等防抖、不等网络）。
 *
 * 保持既有签名与行为不变：它就是 writeCardDraftInput 的薄包装（只带正文、不带附加字段），
 * 老调用写出的记录格式与原实现逐字节一致。
 */
export function writeCardLocalDraft(
  cardId: string,
  text: string,
  options: { boardId?: string; seq?: number } = {},
): DraftWriteResult {
  return writeCardDraftInput(cardId, { text }, options);
}

/** 读某一卡片的完整未完成输入（正文 + 附加字段）；没有记录时返回 null（旧记录没有 meta） */
export function readCardDraftInput(cardId: string): CardDraftInput | null {
  const record = readCardLocalDraft(cardId);
  if (!record) return null;
  const input: CardDraftInput = { text: record.text };
  if (record.meta) input.meta = record.meta;
  return input;
}

/**
 * 这条记录是不是**当前板面**的（记录自己写了归属才判断；没写归属的旧记录不拦，§11.1）。
 */
export function cardDraftInputBelongsToBoard(
  record: DraftRecord | null,
  boardId: string | null | undefined,
): boolean {
  if (!record) return false;
  if (!record.boardId) return true;
  return Boolean(boardId) && record.boardId === boardId;
}

/**
 * 读某一卡片属于**指定板面**的未完成输入：待同步的清除依据不是草稿（不恢复成文字），
 * 别板面的记录也不给恢复（§11.1）。恢复来源只允许从这里取。
 */
export function readCardDraftInputForBoard(
  cardId: string,
  boardId: string | null | undefined,
): CardDraftInput | null {
  const record = readCardLocalDraft(cardId);
  if (!record || !isDraftRecord(record)) return null;
  if (!cardDraftInputBelongsToBoard(record, boardId)) return null;
  const input: CardDraftInput = { text: record.text };
  if (record.meta) input.meta = record.meta;
  return input;
}

/** 某一卡片种类适用的附加字段（文字注释/reply 没有附加字段） */
export function cardDraftMetaFieldsForKind(kind: string): CardDraftMetaField[] {
  if (kind === "file" || kind === "image") return ["name"];
  if (kind === "code") return ["language"];
  if (kind === "url") return ["href", "title"];
  return [];
}

/** 重建恢复来源的输入：正式内容 + 本机未完成输入（两者分开给，避免「谁覆盖谁」靠猜） */
export interface CardDraftRestoreSources {
  /** 卡片种类（决定哪些附加字段适用） */
  kind: string;
  /** 正式卡片正文 */
  content: string;
  /** 正式卡片的附加字段（原始 meta） */
  meta?: Record<string, unknown> | null;
  /** 是否存在未完成输入（由 store 判定：内存候选或属于本板面的本机记录） */
  hasUnfinishedInput: boolean;
  /** 未完成输入的正文；空串是有效值（空草稿是有效编辑状态） */
  draftText?: string;
  /** 本机记录里的附加字段；null / 缺省 = 这条记录没有附加快照（旧记录，回退正式值） */
  draftMeta?: CardDraftMetaInput | null;
}

/** 重建后的编辑态内容：正文 + 适用附加字段（附加字段一定给出，缺省为空串） */
export interface RestoredCardDraftInput {
  hasUnfinishedInput: boolean;
  text: string;
  meta: CardDraftMetaInput;
}

/**
 * 重建「打开编辑器时该显示什么」（N6 的恢复来源）。
 *
 * 规则（与既有草稿/正式内容口径一致，不新增第二套判断）：
 * - 正文：有未完成输入就用它（**空串也算**，§10.4），否则用正式正文；
 * - 附加字段：先取正式卡片的适用字段，本机记录里**写了**的字段（含空串）覆盖它；
 *   旧记录没有附加快照 → 全部用正式值（兼容行为，不是把空值当成清空）。
 */
export function restoreCardDraftInput(sources: CardDraftRestoreSources): RestoredCardDraftInput {
  const meta: CardDraftMetaInput = {};
  for (const field of cardDraftMetaFieldsForKind(sources.kind)) {
    const formal = sources.meta?.[field];
    const drafted = sources.draftMeta?.[field];
    // 未完成输入里写了这个字段（包括空串）就以它为准；否则回退正式值
    meta[field] = typeof drafted === "string" ? drafted : typeof formal === "string" ? formal : "";
  }
  return {
    hasUnfinishedInput: sources.hasUnfinishedInput,
    text: sources.hasUnfinishedInput ? sources.draftText ?? "" : sources.content,
    meta,
  };
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

/** 重建本机清除保护的结果：真实写入结果 + 是否本来就已经有保护 */
export interface DraftClearProtectionResult extends DraftWriteResult {
  /**
   * 磁盘上本来就已经是一份「待确认清除」记录：这次只确认，**没有新写**（版本不推进）。
   * 幂等很重要：每重写一版都会让之前登记的确认版本失效，清理反而删不掉（M2 / 条目 12）。
   */
  alreadyProtected: boolean;
  /**
   * 补写被**版本守卫**拒绝时给出 `"version-guard"`（有意保留更新的一版，不是故障）。
   * 与 `removeCardLocalDraft` 的 reason 同口径，调用方据此决定「静默保留」还是「显示失败」。
   */
  reason?: "version-guard" | "storage-failure" | "missing-record";
}

/**
 * 确认/重建「这份草稿已被用户清除、等服务器确认」的本机依据（M2 / 条目 12）。
 *
 * 为什么要单独有这个入口：清除是两步事实 —— 先在本机留下依据（planCleared），再去和
 * 服务器同步。本机那一步失败过（配额满、存储被禁用）时，重试**必须先补写本机依据**
 * 再重发网络清除；只重发网络的话，网络在飞期间关闭重开，旧稿会因为本机依据不存在而复活。
 * 已经写过（磁盘上就是一条 cleared 记录）时不重复写，避免把版本推掉。
 */
export function ensureCardLocalClear(
  cardId: string,
  options: { boardId?: string; seq?: number; expectVersion?: number | null } = {},
): DraftClearProtectionResult {
  const current = readCardLocalDraft(cardId);
  if (current && current.kind === "cleared") {
    const version = typeof current.version === "number" ? current.version : 0;
    return {
      ok: true,
      version,
      ...(typeof current.version === "number" ? { committedVersion: current.version } : {}),
      alreadyProtected: true,
    };
  }
  /**
   * 版本守卫（独立复核 12b）：另一页面在本页面清除写失败期间写入了**更新的一版**编辑草稿时，
   * 补写 cleared 会把那份新输入覆盖掉 —— 随后网络确认流程还会把它删掉，用户的新输入就没了。
   * 契约 M2 明文禁止「为清除旧稿误删后来输入的更新版本」，所以这里也要按记录版本校验，
   * 与 removeCardLocalDraft 的守卫同口径：记录版本与预期不符 → 有意保留，不写、不删。
   */
  if (current && typeof options.expectVersion !== "undefined" && options.expectVersion !== null) {
    if (typeof current.version === "number" && current.version !== options.expectVersion) {
      return {
        ok: false,
        version: current.version,
        committedVersion: current.version,
        alreadyProtected: false,
        error: "这份本机草稿已经被更晚的输入更新过：保留更新的那一版，不执行这次清除",
        reason: "version-guard",
      };
    }
  }
  // 没给预期版本时不做无法证明的判断：按现有口径写入（调用方负责给出版本）
  return { ...writeCardLocalClear(cardId, options), alreadyProtected: false };
}

/** 本机此刻是否真的有一份待确认清除记录（以磁盘为准，不依赖内存里的状态） */
export function hasCardLocalClear(cardId: string): boolean {
  return readCardLocalDraft(cardId)?.kind === "cleared";
}

/**
 * 删掉某一卡片的本机记录；给了 expectVersion 时**只删这一版**。
 *
 * 版本守卫解决的是 §11.2 的「旧版本的清除不许删掉后来新建的版本」：
 * 用户在清除之后又编辑了新内容，记录已经是新版本，迟到的清除确认不能把它删掉。
 * 记录不存在时 ok=true（本来就没有，不需要删）。
 *
 * 返回**真实结果**（条目 13）：版本守卫拒绝是 `version-guard`（有意保留，界面不说失败），
 * 存储层删不掉是 `storage-failure`（界面必须说清原因并留重试入口）。
 */
export function removeCardLocalDraft(cardId: string, expectVersion?: number): DraftRemoveResult {
  const key = cardLocalDraftStorageKey(cardId);
  const current = readDraft(key);
  if (!current) return { ok: true, removed: false, reason: "missing-record" };
  if (typeof expectVersion === "number" && current.version !== expectVersion) {
    return { ok: false, removed: false, reason: "version-guard" };
  }
  return removeDraft(key);
}

/*
 * ---------- 旧格式本机记录的内容身份依据（F2） ----------
 *
 * 无 version（旧版本写下的记录）或 version === 0 的记录**没有版本号可校验**：
 * 「这次要删的是当时那条记录」这句话无法用版本证明。只按对象 id 删就会误删
 * 后来新建的那条（反例 F2：删除失败期间另一页面为同一张卡片写下新稿，重试把它删掉了）。
 *
 * 所以旧格式记录在登记决定时必须留下一份**可验证的原始身份依据**：由记录自己的内容算出的指纹。
 * 重试前用同一依据复核，证明不了「当前仍是原记录」就保留新稿。
 * 指纹必须确定性、可复跑、不含随机数（同一记录多次调用、以及重新读盘后调用都必须一致），
 * 否则重试会永远证明不了自己，旧副本反而清不掉。
 */
const LOCAL_RECORD_FINGERPRINT_VERSION = "fp1";

/** 32 位 FNV-1a（用 Math.imul 保证 32 位溢出行为确定，不依赖平台数学实现） */
function fnv1a32Hex(input: string, seed: number): string {
  let hash = seed >>> 0;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

/**
 * 记录时戳在历史格式里可能叫 updatedAt / ts / createdAt：逐个归一（不是有限数字的记 null），
 * **全部**进身份依据 —— 少算任何一个都会把两条不同的旧记录认成同一条。
 */
function normalizeStamps(...values: unknown[]): (number | null)[] {
  return values.map((value) => (typeof value === "number" && Number.isFinite(value) ? value : null));
}

/**
 * 一条本机记录的内容身份依据（F2：无版本 / version 0 记录的替换保护）。
 *
 * - 记录不存在（null）时返回 null：调用方据此表达「当时就没有记录」，
 *   之后冒出来的记录不许被这次决定删掉；
 * - 有记录时返回稳定字符串，覆盖 text、记录种类（draft/cleared/legacy）、
 *   记录时戳（updatedAt / ts / createdAt）、seq、version、boardId —— 任一字段变了，
 *   身份依据就变，调用方据此判定「当前已经不是当时那条记录」。
 *
 * 正版本号的记录同样能算出依据（多一道独立校验不削弱既有版本守卫），
 * 但旧格式记录是**唯一**能证明身份的来源。
 */
export function localRecordFingerprint(record: DraftRecord | null): string | null {
  if (!record) return null;
  const kind = record.kind === "draft" || record.kind === "cleared" ? record.kind : "legacy";
  const raw = record as DraftRecord & { ts?: unknown; createdAt?: unknown };
  const stamps = normalizeStamps(record.updatedAt, raw.ts, raw.createdAt);
  const seq = typeof record.seq === "number" && Number.isFinite(record.seq) ? record.seq : 0;
  const version = typeof record.version === "number" && Number.isFinite(record.version) ? record.version : null;
  const boardId = typeof record.boardId === "string" && record.boardId ? record.boardId : null;
  const payload = JSON.stringify([LOCAL_RECORD_FINGERPRINT_VERSION, record.text ?? "", kind, ...stamps, seq, version, boardId]);
  return LOCAL_RECORD_FINGERPRINT_VERSION + ":" + fnv1a32Hex(payload, 0x811c9dc5) + fnv1a32Hex(payload, 0x9e3779b1);
}

/** 读某一卡片当前的本机记录并算身份依据（登记删除决定时用） */
export function cardLocalDraftFingerprint(cardId: string): string | null {
  return localRecordFingerprint(readCardLocalDraft(cardId));
}

/**
 * 按「记录仍是当时那一版／那一条」守卫的本机副本清理（§12.2：同浏览器多页面按记录身份校验）。
 *
 * 在飞的保存/清除回执处理本机副本之前，必须确认副本还是**请求发出时看到的那一条**：
 * 同一浏览器的另一个页面可能在请求期间写入了更新的副本，直接删会把别的页面还没同步的
 * 新输入一起删掉。所以：
 * - expectVersion 是正数：只删版本号仍等于它的记录（若另外给了指纹，指纹也须一致）；
 * - expectVersion 为 0 / null（旧格式记录：没有版本可校验，或登记时本来就没有记录）：
 *   **必须**提供 expectFingerprint，且与当前记录的指纹一致才删。没有这份可验证依据
 *   就一律保留（version-guard）—— 绝不允许退化成「按对象 id 删」；
 * - expectFingerprint === null：表达「当时就没有记录」，只有现在仍然没有记录才算无事可做，
 *   之后冒出来的记录一律保留；
 * - 不传 expectFingerprint（第三参缺省）：**完全保持既有两参行为**（向后兼容）。
 *
 * 版本/身份对不上就**拒绝清理**（ok=false / reason="version-guard"），让调用方保留这条（可能更新的）副本；
 * 身份对得上却删不掉（存储失败）是 **storage-failure**：调用方必须显示原因并可重试。
 * 两者混在一个 false 里就无法区分「有意保留」与「真的删失败」（条目 13）。
 */
export function removeCardLocalDraftIfUnchanged(
  cardId: string,
  expectVersion: number | null,
  expectFingerprint?: string | null,
): DraftRemoveResult {
  const key = cardLocalDraftStorageKey(cardId);
  const current = readDraft(key);
  const currentVersion = current ? (typeof current.version === "number" ? current.version : 0) : null;

  // 第三参缺省：既有两参语义一字不变（旧调用仍然可用）
  if (expectFingerprint === undefined) {
    if (currentVersion !== expectVersion) return { ok: false, removed: false, reason: "version-guard" };
    // 当时没有记录、现在仍然没有：本来就没有可删的东西（不是失败）
    if (!current) return { ok: true, removed: false, reason: "missing-record" };
    return removeDraft(key);
  }

  // 正版本号：版本相等（且给了指纹时指纹一致）才删
  if (typeof expectVersion === "number" && expectVersion > 0) {
    if (currentVersion !== expectVersion) return { ok: false, removed: false, reason: "version-guard" };
    if (expectFingerprint && localRecordFingerprint(current) !== expectFingerprint) {
      return { ok: false, removed: false, reason: "version-guard" };
    }
    return removeDraft(key);
  }

  // 无版本 / version 0：记录已经不在了就是「本来就没有」
  if (!current) return { ok: true, removed: false, reason: "missing-record" };
  /**
   * 没有可验证依据（expectFingerprint 为 null：登记时本来就没有记录）时，
   * 现在却冒出一条记录 —— 那一定是后来新建的稿，不许按对象 id 删。
   */
  if (typeof expectFingerprint !== "string") return { ok: false, removed: false, reason: "version-guard" };
  // 身份对不上：当前已经不是当时那条记录（另一页面重写、种类变化、内容变化）→ 保留新稿
  if (localRecordFingerprint(current) !== expectFingerprint) {
    return { ok: false, removed: false, reason: "version-guard" };
  }
  return removeDraft(key);
}

/*
 * ---------- 本机记录的处理目的（反例 R4） ----------
 *
 * 「本机记录被我处理掉」这句话有两种完全不同的目的，混成一个就会把用户的决定做反：
 * - `remove-local-copy`：只删本机这份**冗余副本**（用户明确选了服务器那份、或服务器
 *   已经拿到同样内容、或这份记录已经没有可归属的对象）。它**绝不**写 cleared 依据 ——
 *   写下去就等于用户要求清掉整份草稿。
 * - `clear-draft`：用户把整份草稿清掉了，需要在服务器确认前留下「待确认清除」的依据。
 *   只有这个目的才允许调用 ensureCardLocalClear 补写 cleared 依据。
 *
 * 为什么必须显式：反例 R4 里 store 只登记了「版本」，把用户决定的真实目的丢了，
 * 重试时一律按「补写 cleared 依据」（整份草稿清除）处理，于是「选择服务器稿」
 * 变成了「删除这份服务器稿的依据」，重开后用户明确保留的那份稿子变空、随后被删。
 */
export type LocalRemovalPurpose = "remove-local-copy" | "clear-draft";

/**
 * 本机记录删除/清理的守卫方式（四种语义必须分开，混成一种就会削弱版本守卫）：
 * - `version`：只删版本号仍等于 expectVersion 的那条（跨页面更新的记录不许被删，§12.2）；
 * - `absent`：登记时本来就没有记录，只在**现在仍然没有**记录时才算无事可做（flushDrafts 回执口径）；
 * - `fingerprint`（F2）：旧格式记录（无 version / version 0）用**内容身份依据**复核，
 *   证明不了「当前仍是原记录」就保留 —— 旧格式记录不许退化成按对象 id 删；
 * - `object`：按对象删（只用于**确实没有**任何版本或身份依据可比的历史调用；显式给了
 *   expectFingerprint 时自动升级为指纹守卫，不会退化成裸删）。
 */
export type LocalRemovalGuard = "version" | "absent" | "fingerprint" | "object";

/**
 * store 的 pendingLocalRemovals 应当存的形状（替换现在的 `number | null`）。
 *
 * `purpose` 决定重试时「删副本」还是「补写清除依据」，`expectVersion` + `guard` 决定
 * 版本守卫用哪一种口径。`guard` 可省略（默认 `object`，即「按对象删」，
 * 与既有 removeCardLocalDraft(cardId, expectVersion) 调用口径一致）；
 * 只有「登记时本来就没有记录」的清理（flushDrafts 成功回执）需要显式给 `absent`，
 * 否则会把请求期间别的页面新建的记录一起删掉（版本守卫被削弱）。
 */
export interface LocalRemovalIntent {
  purpose: LocalRemovalPurpose;
  /** 登记时那条记录的本机版本；null = 登记时没有可比的版本 */
  expectVersion: number | null;
  /** 缺省 `object`：按对象删；显式给了 expectFingerprint 时自动按指纹守卫 */
  guard?: LocalRemovalGuard;
  /**
   * 登记时那条记录的**内容身份依据**（F2）：无 version / version 0 的记录只有它能证明
   * 「当前仍是原记录」。字符串 = 当时那条记录的指纹；null = 登记时本来就没有记录
   * （之后冒出来的记录一律不许删）；缺省（字段不存在）= 没有依据，按旧口径处理。
   */
  expectFingerprint?: string | null;
}

/** 兼容旧登记形状：旧版本存的是裸版本号（或 null），目的不明确 */
export type LocalRemovalIntentInput = LocalRemovalIntent | number | null | undefined;

/** 目的不明时的可操作说明：不许猜成任何一种破坏性动作 */
export const UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE =
  "这条本机记录的处理目的无法判定（旧登记只记了版本号）：本机副本与服务器草稿都先保留，" +
  "请打开这张卡确认要保留哪一份（不要把它当成整份草稿清除）。";

/** 旧恢复记录（没有 kind 字段）的可操作说明：来源/种类不可判定时保留两份候选 */
export const LEGACY_LOCAL_RECORD_ADVICE =
  "这条本机记录是旧版本写下的，无法判断它是编辑副本还是待同步的清除依据：" +
  "本机候选与服务器草稿都保留，请打开这张卡确认要保留哪一份。";

/** 本机记录的角色：显式两类之外一律是 `unknown`（旧记录没有 kind，不许猜） */
export type LocalRecordRole = "draft" | "cleared" | "unknown" | "missing";

/** 读一条本机记录的原始 kind（readDraft 会丢掉「没有 kind」这个事实，判定来源必须看原始字段） */
function readRawCardLocalRecordKind(cardId: string): { exists: boolean; kind: unknown } {
  const { storage } = resolveStorage();
  if (!storage) return { exists: false, kind: undefined };
  let raw: string | null = null;
  try {
    raw = storage.getItem(cardLocalDraftStorageKey(cardId));
  } catch {
    return { exists: false, kind: undefined };
  }
  if (typeof raw !== "string" || raw === "") return { exists: false, kind: undefined };
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { exists: true, kind: undefined };
    return { exists: true, kind: (parsed as { kind?: unknown }).kind };
  } catch {
    return { exists: true, kind: undefined };
  }
}

/**
 * 判定本机记录的来源/种类（反例 R4 第 7 条）：
 * 旧版本写下的记录没有 kind，**无法判断**它是编辑副本还是清除依据，一律返回 `unknown`，
 * 调用方不得把 `unknown` 当成「整份清除」处理（要保留两份候选并给出可操作说明）。
 */
export function cardLocalRecordRole(cardId: string): LocalRecordRole {
  const raw = readRawCardLocalRecordKind(cardId);
  if (!raw.exists) return "missing";
  if (raw.kind === "cleared") return "cleared";
  if (raw.kind === "draft") return "draft";
  return "unknown";
}

/** 旧记录是否需要用户明确选择（无法判定来源/种类时） */
export function inspectLegacyCardLocalRecord(cardId: string): {
  role: LocalRecordRole;
  needsUserChoice: boolean;
  advice?: string;
} {
  const role = cardLocalRecordRole(cardId);
  return role === "unknown" ? { role, needsUserChoice: true, advice: LEGACY_LOCAL_RECORD_ADVICE } : { role, needsUserChoice: false };
}

/** 归一化后的登记意图：目的不明时给 `unknown` + 可操作说明，绝不当成整份清除 */
export interface NormalizedLocalRemovalIntent {
  purpose: LocalRemovalPurpose | "unknown";
  expectVersion: number | null;
  guard: LocalRemovalGuard;
  /** 登记时的内容身份依据：字符串 = 指纹；null = 当时没有记录；undefined = 没有这份依据 */
  expectFingerprint?: string | null;
  advice?: string;
}

/** 把登记意图归一化（含旧形状）：不认识的一律标注目的不明，不猜 */
export function normalizeLocalRemovalIntent(raw: LocalRemovalIntentInput): NormalizedLocalRemovalIntent {
  if (raw && typeof raw === "object") {
    /**
     * 指纹字段只在**登记时确实带了**的时候保留（undefined 与 null 语义不同：
     * undefined = 没有依据、按旧口径；null = 当时没有记录，后来冒出来的不许删）。
     */
    const hasFingerprint = Object.prototype.hasOwnProperty.call(raw, "expectFingerprint");
    const expectFingerprint = typeof raw.expectFingerprint === "string" ? raw.expectFingerprint : null;
    const explicitGuard =
      raw.guard === "version" || raw.guard === "absent" || raw.guard === "fingerprint" || raw.guard === "object"
        ? raw.guard
        : null;
    return {
      purpose: raw.purpose === "clear-draft" ? "clear-draft" : "remove-local-copy",
      expectVersion: typeof raw.expectVersion === "number" && Number.isFinite(raw.expectVersion) ? raw.expectVersion : null,
      /**
       * 缺省 object：与既有 removeCardLocalDraft(cardId, expectVersion) 口径一致，不改变已落地的接线行为；
       * 但**显式带了身份依据**时默认按指纹守卫 —— 有依据就不许退化成按对象 id 裸删（F2）。
       */
      guard: explicitGuard ?? (hasFingerprint ? "fingerprint" : "object"),
      ...(hasFingerprint ? { expectFingerprint } : {}),
    };
  }
  // 旧形状（裸版本号 / null）：只知道版本，不知道用户当时要做什么 —— 不许猜成整份清除
  return {
    purpose: "unknown",
    expectVersion: typeof raw === "number" && Number.isFinite(raw) ? raw : null,
    guard: typeof raw === "number" && Number.isFinite(raw) ? "version" : "absent",
    advice: UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE,
  };
}

/** 重试本机记录处理的结果：目的 + 实际动作 + 真实原因（调用方据此决定界面说什么） */
export interface LocalRemovalRetryOutcome {
  /** 这一次的期望是否达成（protected / removed / nothing 都算达成） */
  ok: boolean;
  purpose: LocalRemovalPurpose | "unknown";
  action: "removed" | "protected" | "kept-newer" | "failed" | "needs-choice" | "nothing";
  /** 底层原因分类（storage-failure / version-guard / unknown-purpose 等） */
  reason?: string;
  /** 失败时可直接显示的真实原因 */
  error?: string;
  /** kept-newer / needs-choice 时给用户的可操作说明 */
  advice?: string;
  /** protected 时真实落盘的版本号（调用方只能用这个登记「已确认版本」） */
  committedVersion?: number;
  /** 涉及的记录版本（底层函数回报的那一个） */
  version?: number;
  /** clear-draft 且磁盘上本来就已是一份 cleared 记录（幂等，没有新写） */
  alreadyProtected?: boolean;
  /** 底层结果原样返回，调用方需要更细的分类时用 */
  removeResult?: DraftRemoveResult;
  clearResult?: DraftClearProtectionResult;
}

/**
 * 按**登记时的目的**重试本机记录处理（反例 R4 的修复入口）。
 *
 * - `remove-local-copy`：只删本机副本（按登记的守卫口径；旧格式记录按登记的内容身份依据
 *   复核，F2：证明不了「当前仍是原记录」就保留新稿），**绝不写 cleared 依据**；
 * - `clear-draft`：调用 ensureCardLocalClear 补写/确认 cleared 依据（幂等，保留版本守卫）；
 * - 目的不明（旧登记形状）：两件破坏性动作都不做，保留两份候选 + 可操作说明。
 *
 * 版本守卫绝不削弱：`guard: "version"` 走 removeCardLocalDraftIfUnchanged /
 * ensureCardLocalClear 的 expectVersion，另一页面写入的更新版本一律保留。
 */
export function retryLocalRemovalByPurpose(
  cardId: string,
  intent: LocalRemovalIntentInput,
  options: { boardId?: string; seq?: number } = {},
): LocalRemovalRetryOutcome {
  const normalized = normalizeLocalRemovalIntent(intent);
  if (normalized.purpose === "unknown") {
    /**
     * 旧登记只记了版本、没记目的：删掉本机记录与补写 cleared 依据都可能做反用户的决定，
     * 所以什么都不做；两份候选（本机记录 + 服务器草稿）都留着，把说明交回调用方显示。
     */
    return {
      ok: false,
      purpose: "unknown",
      action: "needs-choice",
      reason: "unknown-purpose",
      error: UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE,
      advice: UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE,
    };
  }

  if (normalized.purpose === "clear-draft") {
    // 只有这个目的才允许补写 cleared 依据
    const protection = ensureCardLocalClear(cardId, {
      ...(options.boardId ? { boardId: options.boardId } : {}),
      ...(typeof options.seq === "number" && Number.isFinite(options.seq) ? { seq: options.seq } : {}),
      expectVersion: normalized.expectVersion,
    });
    if (protection.reason === "version-guard") {
      return {
        ok: false,
        purpose: "clear-draft",
        action: "kept-newer",
        reason: "version-guard",
        error: protection.error,
        advice: "这份本机记录已经被更晚的输入更新过：保留更新的那一版，不执行这次清除。",
        version: protection.version,
        clearResult: protection,
      };
    }
    if (!protection.ok) {
      return {
        ok: false,
        purpose: "clear-draft",
        action: "failed",
        reason: "storage-failure",
        error: protection.error ?? "本机没能记下这次清除，重开后这份旧稿可能重新出现",
        version: protection.version,
        clearResult: protection,
      };
    }
    return {
      ok: true,
      purpose: "clear-draft",
      action: "protected",
      committedVersion: protection.committedVersion ?? protection.version,
      version: protection.version,
      alreadyProtected: protection.alreadyProtected,
      clearResult: protection,
    };
  }

  // remove-local-copy：只删本机冗余副本，绝不写 cleared 依据
  const expectVersion = normalized.expectVersion;
  const expectFingerprint = normalized.expectFingerprint;
  const hasFingerprint = expectFingerprint !== undefined;
  /**
   * 守卫选择（F2 后）：
   * - fingerprint：旧格式记录按内容身份依据复核，没有依据就不许按对象 id 删；
   * - object 且带了（哪怕为 null 的）身份依据：同样走指纹守卫，不裸删；
   * - absent：只在现在仍然没有记录时无事可做（version 参为 null）；
   * - version：只删那一个版本（若同时带指纹，一并复核）。
   */
  let result: DraftRemoveResult;
  if (normalized.guard === "fingerprint") {
    result = removeCardLocalDraftIfUnchanged(cardId, expectVersion, expectFingerprint ?? null);
  } else if (normalized.guard === "object") {
    result = hasFingerprint
      ? removeCardLocalDraftIfUnchanged(cardId, expectVersion, expectFingerprint ?? null)
      : removeCardLocalDraft(cardId, typeof expectVersion === "number" ? expectVersion : undefined);
  } else {
    result = removeCardLocalDraftIfUnchanged(
      cardId,
      normalized.guard === "absent" ? null : expectVersion,
      hasFingerprint ? (expectFingerprint ?? null) : undefined,
    );
  }
  if (result.ok) {
    return {
      ok: true,
      purpose: "remove-local-copy",
      action: result.removed ? "removed" : "nothing",
      reason: result.reason,
      removeResult: result,
    };
  }
  if (result.reason === "version-guard") {
    return {
      ok: false,
      purpose: "remove-local-copy",
      action: "kept-newer",
      reason: "version-guard",
      advice: "这份本机记录已经被更晚的输入更新过：保留更新的那一版，不执行这次清理。",
      removeResult: result,
    };
  }
  return {
    ok: false,
    purpose: "remove-local-copy",
    action: "failed",
    reason: "storage-failure",
    error: result.error ?? "这份本机副本没能删掉，暂时还留在本机",
    advice: "这份本机副本没能删掉，重开后可能又出现。",
    removeResult: result,
  };
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

/**
 * 删掉一条本机记录，返回**真实结果**（条目 13 / 契约 M2：失败不得静默报成功/完成）。
 *
 * 三种结局分得开：
 * - 删成功 / 本来就没有 → ok=true；
 * - 存储不可用、removeItem 抛错 → ok=false / storage-failure + 真实原因（删除失败时记录仍在，内容没丢）；
 * - removeItem 不抛错却没删掉（只读环境、被别的实现吞掉、多页面竞争）→ 复核后判为 storage-failure，不报成功。
 */
export function removeDraft(key: string): DraftRemoveResult {
  const { storage, error } = resolveStorage();
  if (!storage) {
    return {
      ok: false,
      removed: false,
      reason: "storage-failure",
      error: error ?? "本地存储不可用，这条本机记录没有删掉",
    };
  }
  // 先看有没有：本来就没有不叫失败，调用方不必为它提示错误
  let existed: boolean;
  try {
    existed = storage.getItem(key) !== null;
  } catch (err) {
    return { ok: false, removed: false, reason: "storage-failure", error: describeRemoveUnverified(err) };
  }
  if (!existed) return { ok: true, removed: false, reason: "missing-record" };
  try {
    storage.removeItem(key);
  } catch (err) {
    return { ok: false, removed: false, reason: "storage-failure", error: describeRemoveError(err) };
  }
  /*
   * 删完必须复核：removeItem 不抛异常并不等于记录真的没了
   * （只读环境、被别的实现吞掉、多页面竞争都可能留下旧记录）。
   * 不复核就会把「没删掉」报成完成 —— 正是条目 13 要消掉的行为。
   */
  try {
    if (storage.getItem(key) !== null) {
      return {
        ok: false,
        removed: false,
        reason: "storage-failure",
        error: "本机存储没有真正删掉这条记录（可能被浏览器策略或其它页面阻止）",
      };
    }
  } catch (err) {
    return { ok: false, removed: false, reason: "storage-failure", error: describeRemoveUnverified(err) };
  }
  return { ok: true, removed: true };
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
