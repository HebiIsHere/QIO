/**
 * 附件异步操作的**共享守卫**（冻结契约 K1，Lead 冻结接口；Composer 与 MessageItem 共用）。
 *
 * 它解决的是同一类竞态：附件的上传 / 轮询 / 校验 / 重新定位 / 历史重传 / 恢复都是异步的，
 * 而「用户已经移除了它」「已经随发送受理失效」「已经切到别的话题」「组件已经重挂载」
 * 都可能发生在结果返回之前。归属与写权限必须由**发起时刻冻结的身份**决定，
 * 绝不能等结果回来再读 currentTopicId。
 *
 * 四件事：
 * 1. `beginAttachmentOp`：发起时捕获 {kind, topicId, attachmentIds, opToken, topicEpoch}；
 * 2. `registerAttachmentIdentity` / `invalidateAttachmentIdentity` / `restoreAttachmentIdentity`：
 *    附件级 opToken（注册/替换递增；移除使其失效；删除失败则原样恢复，保留身份）；
 * 3. topic 级 topicEpoch（发送受理 / 话题清空 / 重挂载恢复基线时前进）与 sent 失效集；
 * 4. `decideAttachmentWrite`：ui / persistence / drop 三态判定 + `mergeRestorePatch` 只新增的合并。
 *
 * 判定口径（K1.3）：
 *   * 已移除（tombstone）或已发送（sent）→ **drop**（静默丢弃，绝不 upsert 回来）；
 *   * token 已被替换 / 条目在操作发起后重新登记（世代前进）→ **persistence**
 *     （结果仍然真实：落到**原话题**持久化，不写当前 UI，也不制造孤儿）；
 *   * 结果属于其它话题 → **persistence**；只有当前话题 + 身份仍然有效 → **ui**。
 *
 * 关于 topicEpoch 的精确含义：话题级世代在「发送被受理 / 话题清空 / 组件重挂载」时前进。
 * 对**整表快照类**操作（restore）它必须完全一致才算新鲜；对**条目级**操作（轮询/校验/重新定位）
 * 判据是「这条附件的列表条目没有在这次操作发起之后被重新登记」—— 否则「发送受理时把话题世代
 * 推进一步」会冻结同一话题里另一条仍在准备中的附件的状态显示，制造假象。
 */
import {
  attachmentRemovedSeq,
  clearRemovedAttachmentMemory,
  forgetAttachmentRemoved,
  forgetSentAttachments,
  isAttachmentRemoved,
  isAttachmentSent,
  pendingRevision,
  type AttachmentRef,
} from "../services/attachments";

/** 操作种类：新建类（附件身份由本次操作分配）/ 条目类（作用于已存在的附件）/ 整表快照类。 */
export type AttachmentOpKind =
  | "prepare"
  | "upload"
  | "reupload"
  | "history"
  | "poll"
  | "verify"
  | "relocate"
  | "restore"
  | "send";

const CREATION_KINDS: ReadonlySet<AttachmentOpKind> = new Set<AttachmentOpKind>([
  "prepare",
  "upload",
  "reupload",
  "history",
]);

/** 在发起时刻冻结的操作身份（冻结对象：后续函数只能读，不能改写归属）。 */
export interface AttachmentOpCapture {
  readonly kind: AttachmentOpKind;
  /** 发起时刻的话题：之后切话题也不改这一事实 */
  readonly topicId: string | null;
  readonly attachmentIds: readonly string[];
  /** 主附件（新建类 = 本次操作将要分配的身份）的 token */
  readonly opToken: number;
  /** 逐附件捕获的 token（多附件操作各自判定） */
  readonly opTokens: Readonly<Record<string, number>>;
  readonly topicEpoch: number;
  /**
   * 逐附件的**操作声明序号**（R4）：条目级操作（poll / verify / relocate …）每次发起都声明一次。
   *
   * 与 opToken 的区别：token 表达「这条身份还在不在」，声明序号表达「这条上**最新**的操作是哪一次」。
   * 同一个附件上后发起的操作会拿走最新的声明，先发起、后返回的旧结果据此判为过期
   * —— 过期结果既不写界面也不落盘（`decideAttachmentWrite` → drop）。
   */
  readonly claims: Readonly<Record<string, number>>;
}

export type AttachmentWriteTarget = "ui" | "persistence" | "drop";

export interface AttachmentWriteVerdict {
  target: AttachmentWriteTarget;
  reason:
    | "ok"
    | "other-topic"
    | "removed"
    | "sent"
    | "token-stale"
    | "epoch-passed"
    | "not-in-list"
    | "topic-gone"
    /** R4：这条上已经有**更晚发起**的操作（它的结果才是当前事实） */
    | "superseded";
}

export interface PendingRestorePatch {
  topicId: string | null;
  /** 补丁对应的待发列表修订号（Composer 用来判断恢复期间有没有人写过） */
  revision: number;
  /** 确认可用（含「暂时无法确认」带 unconfirmed 标记）的条目 */
  restored: AttachmentRef[];
  /** 确认永久无效的名字（给人看） */
  missing: string[];
  /** 确认永久无效的 id（用来从当前列表剔除） */
  missingIds: string[];
}

export interface MergeRestoreResult {
  list: AttachmentRef[];
  added: string[];
  skipped: { id: string; reason: "removed" | "sent" | "missing" }[];
}

let opSeq = 0;
/** 附件级 token：通过导出函数读写 */
const tokens = new Map<string, number>();
/** 附件条目进入列表时的世代：晚到的操作不得覆盖更晚的条目 */
const entryEpochs = new Map<string, number>();
/** topic 级世代 */
const topicEpochs = new Map<string, number>();
/** 条目级操作的声明序号：这条上最新发起的那次操作才是当前事实（R4） */
const claims = new Map<string, number>();
let claimSeq = 0;

function topicKey(topicId: string | null | undefined): string {
  return String(topicId ?? "");
}

/** 仅供测试：清掉守卫的内存状态（磁盘上的 tombstone 不受影响，重挂载后依然有效）。 */
export function resetAttachmentOpState(): void {
  opSeq = 0;
  tokens.clear();
  entryEpochs.clear();
  topicEpochs.clear();
  claims.clear();
  claimSeq = 0;
  forgetSentAttachments(undefined);
  // 注意：tombstone 的**持久化**内容不清（重挂载语义）；只清存储不可用时的内存镜像。
  clearRemovedAttachmentMemory();
}

export function topicEpochOf(topicId: string | null | undefined): number {
  return topicEpochs.get(topicKey(topicId)) ?? 0;
}

/** 话题级世代前进：发送被受理 / 话题被清空 / 组件重挂载后恢复基线。 */
export function bumpTopicEpoch(topicId: string | null | undefined): number {
  const key = topicKey(topicId);
  const next = (topicEpochs.get(key) ?? 0) + 1;
  topicEpochs.set(key, next);
  return next;
}

/**
 * 注册 / 重新添加一个附件的身份（显式重新选择文件时调用）。
 *
 * 传 `token` = 某个在飞操作发起时分配的身份（新建类操作：结果落地后把身份登记上去）；
 * 不传 = 分配一个新身份（替换/重新添加）。注册会清掉这条的移除痕迹与 sent 标记 ——
 * 这是**用户显式重新添加**的唯一入口；恢复合并绝不走这里。
 */
export function registerAttachmentIdentity(
  topicId: string | null | undefined,
  id: string,
  token?: number,
): number {
  const next = token ?? opSeq + 1;
  opSeq = Math.max(opSeq, next);
  tokens.set(id, next);
  entryEpochs.set(id, topicEpochOf(topicId));
  forgetAttachmentRemoved(topicId, id);
  forgetSentAttachments(topicId, [id]);
  return next;
}

export function attachmentOpToken(id: string): number {
  return tokens.get(id) ?? 0;
}

/** 这条附件进入列表时的世代（>= 发起时刻世代 = 操作之后被重新登记过）。 */
export function attachmentEntryEpoch(id: string): number {
  return entryEpochs.get(id) ?? 0;
}

/** 这条附件当前的操作声明序号（0 = 还没有条目级操作声明过它）。 */
export function attachmentOpClaim(id: string): number {
  return claims.get(id) ?? 0;
}

/** 移除 / 替换一个附件：使它的当前 token 失效（旧操作晚到不得再写）。 */
export function invalidateAttachmentIdentity(id: string): number {
  const next = opSeq + 1;
  opSeq = next;
  tokens.set(id, next);
  return next;
}

/** 删除失败等「什么都没变」的情形：把身份原样恢复（暂时失败保留身份不变）。 */
export function restoreAttachmentIdentity(id: string, token: number): void {
  opSeq = Math.max(opSeq, token);
  tokens.set(id, token);
}

/** 开始一次附件异步操作：捕获话题、附件、逐附件 token 与话题世代。 */
export function beginAttachmentOp(input: {
  kind: AttachmentOpKind;
  topicId: string | null | undefined;
  attachmentIds?: readonly string[];
}): AttachmentOpCapture {
  const topicId = input.topicId ?? null;
  const attachmentIds = Object.freeze([...(input.attachmentIds ?? [])]);
  const creating = CREATION_KINDS.has(input.kind);
  const opToken = creating ? opSeq + 1 : attachmentIds.length ? attachmentOpToken(attachmentIds[0]) : 0;
  if (creating) opSeq = opToken;
  const opTokens: Record<string, number> = {};
  const opClaims: Record<string, number> = {};
  for (const id of attachmentIds) {
    opTokens[id] = attachmentOpToken(id);
    // 条目级操作：发起即声明「这一条上最新的操作是我」——之前发起、之后返回的旧结果一律过期（R4）。
    // 新建类操作不声明：它还没有 id（身份由结果登记），别人的在飞结果不受影响。
    if (!creating) {
      claimSeq += 1;
      claims.set(id, claimSeq);
      opClaims[id] = claimSeq;
    }
  }
  return Object.freeze({
    kind: input.kind,
    topicId,
    attachmentIds,
    opToken,
    opTokens: Object.freeze(opTokens),
    topicEpoch: topicEpochOf(topicId),
    claims: Object.freeze(opClaims),
  });
}

export function isAttachmentDead(topicId: string | null | undefined, id: string): boolean {
  return isAttachmentRemoved(topicId, id) || isAttachmentSent(topicId, id);
}

export { markAttachmentsSent, isAttachmentSent, forgetSentAttachments } from "../services/attachments";

/**
 * 这次结果该写哪里（K1.3）。
 *
 * `isCurrentTopic` 由调用方给出（组件存活且捕获话题仍是当前话题），守卫本身不读
 * currentTopicId —— 归属只来自 capture。
 */
export function decideAttachmentWrite(
  capture: AttachmentOpCapture,
  context: {
    id?: string | null;
    isCurrentTopic: boolean;
    inCurrentList?: boolean;
    topicExists?: boolean;
  },
): AttachmentWriteVerdict {
  const id = context.id ?? null;
  if (id) {
    if (isAttachmentRemoved(capture.topicId, id)) return { target: "drop", reason: "removed" };
    if (isAttachmentSent(capture.topicId, id)) return { target: "drop", reason: "sent" };
    /**
     * R4：这条上已经有**更晚发起**的操作 → 本次结果是过期事实。
     * 既不写界面也不落盘（写进持久化同样是拿旧事实覆盖新结果），静默丢弃。
     */
    const claimed = capture.claims[id];
    if (claimed !== undefined && attachmentOpClaim(id) !== claimed) {
      return { target: "drop", reason: "superseded" };
    }
    const expected = capture.opTokens[id] ?? capture.opToken;
    if (expected !== attachmentOpToken(id)) return { target: "persistence", reason: "token-stale" };
  }
  // 整表快照类（restore）要求世代完全一致；条目类只看「这条条目有没有被重新登记」。
  const stale =
    capture.kind === "restore"
      ? capture.topicEpoch !== topicEpochOf(capture.topicId)
      : Boolean(id) && attachmentEntryEpoch(id as string) > capture.topicEpoch;
  if (stale) return { target: "persistence", reason: "epoch-passed" };
  if (id && context.inCurrentList === false) return { target: "persistence", reason: "not-in-list" };
  if (context.topicExists === false) return { target: "drop", reason: "topic-gone" };
  if (!context.isCurrentTopic) return { target: "persistence", reason: "other-topic" };
  return { target: "ui", reason: "ok" };
}

/**
 * 合并恢复补丁（K1.6）：**只新增**，绝不复活 removed/sent，绝不覆盖更晚的列表状态。
 *
 *   * 当前列表里已有的条目保持原样（唯一例外：补丁说它「暂时无法确认」→ 只加标记；
 *     这是 F10 的既有语义 —— 状态未知就不能当成就绪发送，身份与其它事实都不动）；
 *   * 补丁里确认永久无效的 id 从列表剔除（`dropMissing: false` 时保留，交给下一次恢复清理）；
 *   * 补丁里新增的条目只有在不在 removed/sent 时才追加。
 *
 * `removedBarrier`（可选）：发起这次核对时捕获的**移除水位**（attachmentRemovalBarrier）。
 * 补丁里出现某个候选，说明服务核对该候选时它还没有 tombstone；只有水位**之后**才被移除的
 * 候选才在这里被挡（这正是「恢复在途时移除」的窗口）。水位之前就存在的 tombstone 不可能
 * 出现在真实补丁里 —— 服务在核对阶段就会直接跳过它；不传水位 = 一律按 tombstone 挡。
 *
 * R3（应用边界）：补丁的 `revision` 是**发起核对时捕获**的修订号。合并这一刻再比一次
 * 「当前本地修订号」—— 更大了说明期间有人写过，这份补丁的 `missingIds` 已经是旧事实，
 * 一律不应用剔除（保留当前记录，交给下一次恢复）。补丁生成与合并之间还有窗口，
 * 所以这一层判定不能只放在生成侧。
 *
 * 比较用「当前 > 补丁」：修订号按话题单调递增（更大 = 期间有人写过）；
 * 当前为 0 只表示本地没有这个话题的修订记录，不能据此声称有人写过。
 */
export function mergeRestorePatch(
  patch: PendingRestorePatch,
  current: readonly AttachmentRef[],
  options: { dropMissing?: boolean; removedBarrier?: number } = {},
): MergeRestoreResult {
  const topicId = patch.topicId ?? null;
  const revisionMoved = pendingRevision(topicId) > patch.revision;
  const dropMissing = options.dropMissing !== false && !revisionMoved;
  const missingIds = new Set(dropMissing ? patch.missingIds : []);
  const restoredById = new Map(patch.restored.map((item) => [item.id, item]));
  const list: AttachmentRef[] = [];
  const skipped: MergeRestoreResult["skipped"] = [];

  for (const item of current) {
    if (missingIds.has(item.id)) {
      skipped.push({ id: item.id, reason: "missing" });
      continue;
    }
    const fresh = restoredById.get(item.id);
    if (fresh?.unconfirmed && !item.unconfirmed) {
      // 暂时失败：保留身份与其它事实，只加「暂时无法确认」标记（不能当成就绪发送）
      list.push({ ...item, unconfirmed: true });
      continue;
    }
    if (fresh && !fresh.unconfirmed && item.unconfirmed) {
      // 这次核对确认它还在：用后端新事实，并清掉「暂时无法确认」标记
      list.push({ ...fresh, name: fresh.name || item.name, error: fresh.error ?? item.error ?? null });
      continue;
    }
    list.push(item);
  }

  const present = new Set(list.map((item) => item.id));
  const added: string[] = [];
  for (const item of patch.restored) {
    if (present.has(item.id)) continue;
    const removedAt = attachmentRemovedSeq(topicId, item.id);
    if (removedAt && (options.removedBarrier === undefined || removedAt > options.removedBarrier)) {
      skipped.push({ id: item.id, reason: "removed" });
      continue;
    }
    if (isAttachmentSent(topicId, item.id)) {
      skipped.push({ id: item.id, reason: "sent" });
      continue;
    }
    list.push(item);
    present.add(item.id);
    added.push(item.id);
  }
  return { list, added, skipped };
}
