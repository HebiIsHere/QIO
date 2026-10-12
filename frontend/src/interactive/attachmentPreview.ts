/**
 * 板面卡片的「附件预览计划」（P2）。
 *
 * 契约：docs/interactive-mode-contract.md §14.1（拿到文件名 ≠ 读到正文）。
 * 这里只做两件事：
 * 1. **计划是纯的**：把卡片 meta（meta.file / meta.image = {name, attachmentId, size}）
 *    翻成「能看见什么、点一下会发生什么、为什么不能」，可单测、不碰 DOM；
 * 2. **执行辅助不 import vue**：缩略图的 blob URL 生命周期（建 → 卸载 / 换附件时 revoke）
 *    与「点一下」到底走哪条既有链路（桌面 open / reveal，浏览器带认证 fetch + blob 新标签页）
 *    收在一处 —— 组件只负责渲染与接线，判断只有一份。
 *
 * 复用而不是重造（services/attachments 是唯一来源）：
 *   fetchAttachmentContent(id)    带认证 fetch 出 Blob
 *   openPlanFor(ref, {desktop})   这个附件在桌面 / 浏览器会怎么打开
 *   openAttachment(ref, opts)     真的去执行那个计划（可执行类降级为 reveal）
 *   openBlobInNewTab / humanSize / isDesktopShell / isExecutableName / isViewableName
 *
 * 诚实规则：
 *   * 没有 attachmentId → 没有预览、没有假按钮（unlinked）；
 *   * 可执行 / 脚本类 → 不自动运行，桌面端只「在文件夹中显示」并说明原因；
 *   * 浏览器 + 引用型（没有 QIO 副本）→ openAttachment 抛错，界面如实显示那句话；
 *   * 「已交给浏览器」不等于「已经打开」（noopener 下 window.open 返回 null），文案不谎报成功；
 *   * 超出缩略图上限时**不发起**请求，直接说清原因（预算是显示性能，不是不让你看）。
 */
import { humanSize, openPlanFor } from "../services/attachments";
import type { AttachmentRef } from "../services/attachments";
import {
  attachmentCardState,
  attachmentPreviewActionLabel,
  attachmentPreviewUnavailableReason,
} from "./materials";
import { isExecutableName, isViewableName } from "../services/attachments";
import type { CardKind } from "./types";

/**
 * 缩略图字节上限：超过就不发起 fetch。
 * 目的是显示性能与内存（渲染位图的内存约等于宽 × 高 × 4），不是「不让你看」——
 * 超限时如实显示原因，并保留「查看 / 打开」入口。
 */
export const IMAGE_PREVIEW_MAX_BYTES = 24_000_000;

/** 一次「点一下会发生什么」的类型（动作串与 AttachmentOpenAction 同名，便于对照）。 */
export type AttachmentPreviewAction =
  | "open"
  | "reveal"
  | "view"
  | "download"
  | "open_in_folder"
  | "blocked";

export interface AttachmentPreviewPlan {
  state: "linked" | "unlinked";
  /** meta 里的真实附件 id；没有就是空串 */
  attachmentId: string;
  displayName: string;
  sizeBytes: number | null;
  /** 卡片是图片类，且附件能安全渲染成缩略图（位图扩展名） */
  imagePreviewable: boolean;
  /** 缩略图的字节上限（拿不到 content 端点时，用卡片 meta 的 size 做预算） */
  imageSizeLimitBytes: number;
  /** 计划动作（"blocked" = 这个环境里没有可用的打开方式，例如浏览器里的引用型附件） */
  action: AttachmentPreviewAction | null;
  /** 点一下会发生什么（可读，含原因） */
  openNote: string;
  /** 缩略图不可用的原因；可预览时为空 */
  unavailableReason: string;
  /** 关联状态一行（meta 里有名字但没 id 时要说清「名字不等于正文」） */
  statusText: string;
  /** 按钮的可读标签（可执行类明确「在文件夹中显示」/「不自动运行」） */
  actionLabel: string;
}

/**
 * 可执行 / 脚本类与「可安全内联查看」这两份名单**只有一处来源**：services/attachments。
 * 这里显式绑定一次，保证预览计划与真正的打开计划（openPlanFor）用的是同一份判断 ——
 * 否则会出现「按钮说查看、点下去是下载」这种两套说法。
 */
const AVAILABILITY_LOOKUPS = { executable: isExecutableName, viewable: isViewableName };

/**
 * 从卡片 meta 算出预览计划（纯函数；desktop 显式传入 ——
 * 组件传 isDesktopShell() 的真实结果，测试传 true / false）。
 */
export function attachmentPreviewPlan(
  kind: CardKind | string,
  meta: Record<string, unknown> | null | undefined,
  options: { desktop: boolean },
): AttachmentPreviewPlan {
  const availability = attachmentCardState(meta, kind, AVAILABILITY_LOOKUPS);
  const actionLabel = attachmentPreviewActionLabel(availability, options.desktop);
  const unavailableReason = attachmentPreviewUnavailableReason(availability);

  if (availability.state !== "linked") {
    return {
      state: "unlinked",
      attachmentId: "",
      displayName: availability.name,
      sizeBytes: availability.size,
      imagePreviewable: false,
      imageSizeLimitBytes: IMAGE_PREVIEW_MAX_BYTES,
      action: null,
      openNote: "",
      unavailableReason,
      statusText: attachmentUnlinkedStatus(availability),
      actionLabel,
    };
  }

  const ref = attachmentRefForPreview(availability);
  const detail = previewActionDetail(ref, options.desktop);
  return {
    state: "linked",
    attachmentId: availability.attachmentId,
    displayName: ref.name,
    sizeBytes: availability.size,
    imagePreviewable: availability.imagePreviewable,
    imageSizeLimitBytes: IMAGE_PREVIEW_MAX_BYTES,
    action: detail.action,
    openNote: detail.note,
    unavailableReason,
    statusText:
      "已关联已保存的附件：" + (availability.name || "未命名附件") + sizeSuffix(availability.size),
    actionLabel,
  };
}

function attachmentUnlinkedStatus(availability: { name: string }): string {
  const name = availability.name;
  return name
    ? "还没有关联已保存的附件：卡片上只记着文件名「" + name + "」，QIO 读不到它的正文，也打不开它"
    : "还没有关联已保存的附件：QIO 读不到它的正文，也打不开它";
}

function sizeSuffix(size: number | null): string {
  return size && size > 0 ? "（" + humanSize(size) + "）" : "";
}

/**
 * 这个附件在这台机器上「点一下」会怎么打开 —— **判断复用 openPlanFor**，
 * 这里只补两句界面必须说清楚的话：
 *
 * * 桌面端 + 卡片没有本地路径：meta 里本来就没有 storedPath / sourcePath，
 *   不猜路径也不要一个点了会报错的按钮 —— 改为查看 QIO 保存的副本字节（带认证 fetch），
 *   并把「这条链路到底是什么」写进提示；
 * * 浏览器 + 引用型（没有 QIO 副本）：openPlanFor 给的是 blocked，界面如实显示原因，
 *   不给假入口。
 */
function previewActionDetail(
  ref: AttachmentRef,
  desktop: boolean,
): { action: AttachmentPreviewAction; note: string } {
  const plan = openPlanFor(ref, { desktop });
  if (plan.action === "reveal" || plan.action === "open") {
    return {
      action: plan.action,
      note: plan.action === "reveal" ? plan.reason : "已交给系统默认程序打开：「" + ref.name + "」",
    };
  }
  /**
   * 可执行 / 脚本类**优先**判断，而且不依赖本地路径：
   * 卡片 meta 里本来就没有 storedPath（那是附件行上的事实），
   * 如果照抄 openPlanFor 的「没有路径 → 按副本处理」，一个 .exe 会被说成「查看 / 下载」——
   * 与「绝不自动运行」的规则不一致。所以：
   *   桌面：没有路径就给 blocked，并让用户用卡片上的「重新关联」补上路径；
   *   浏览器：只能下载（要打开就在桌面端点）。
   */
  if (isExecutableName(ref.name)) {
    if (desktop) {
      return {
        action: "open_in_folder",
        note:
          "可执行 / 脚本类文件不自动运行；这张卡片上没有可定位的本地路径（meta 只记了附件 id）：" +
          "先在卡片上重新关联这个文件，就能用「在文件夹中显示」定位到它",
      };
    }
    return {
      action: "download",
      note: "可执行 / 脚本类文件不自动运行：浏览器里只能下载副本，要打开请在桌面端点「在文件夹中显示」",
    };
  }
  if (plan.action === "blocked") {
    return { action: "blocked", note: plan.reason };
  }
  if (plan.action === "download") {
    return { action: "download", note: plan.reason || "这种类型不内联查看：改为下载副本" };
  }
  return {
    action: "view",
    note:
      (desktop ? "卡片上没有本地路径：" : "") +
      "查看的是 QIO 保存的副本字节（带认证 fetch 后在浏览器新标签页打开；没有出现可能是弹窗被拦截，可改用下载）",
  };
}

/** 卡片 meta → 最小 AttachmentRef（只带上 meta 里**真实有**的事实，不伪造路径 / 状态）。 */
export function attachmentRefForPreview(availability: {
  attachmentId: string;
  name: string;
  size: number | null;
}): AttachmentRef {
  return {
    id: availability.attachmentId,
    name: availability.name || "未命名附件",
    sizeBytes: availability.size ?? 0,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  };
}

/**
 * 「点一下」本身**没有第二套实现**：组件直接调用既有 openAttachment（services/attachments），
 * 失败原样抛出、界面如实显示 —— 绝不把「打不开」说成「已打开」。
 * 这里不包一层转发函数：多一层只会多一种说法。
 */

// ---------------------------------------------------------------------------
// 执行辅助（不 import vue，可在纯函数测试里直接测）
// ---------------------------------------------------------------------------

/** 缩略图字节的取用口（组件把带认证 fetch 传进来，测试里换成假 Blob）。 */
export interface PreviewContentFetcher {
  (id: string): Promise<Blob>;
}

export interface PreviewBlobResult {
  url: string;
  /** 真实字节数（后端实际返回了多少，不是 meta 里记的） */
  bytes: number;
}

export interface PreviewBlobHost {
  /** 取字节并建 blob URL（并发 / 重复调用只取一次；按附件 id 缓存并复用）。 */
  load(ref: AttachmentRef): Promise<PreviewBlobResult | null>;
  /** 失败原因（成功后清空）。 */
  failure(): string | null;
  /** 释放当前 blob URL（重复调用安全）。 */
  revoke(): void;
}

/**
 * blob URL 的生命周期宿主：按附件 id 缓存，切换附件时释放上一个，卸载时释放当前的。
 *
 * 为什么要有它：板面上可能同时挂很多卡片，每次重渲染都新建 URL 会泄漏；
 * 这里把「什么时候建、什么时候 revoke」收成一个可测对象，组件只负责调用。
 */
export function createPreviewBlobHost(fetcher: PreviewContentFetcher): PreviewBlobHost {
  let objectUrl: string | null = null;
  let loadedFor: string | null = null;
  let loadedBytes = 0;
  let failureReason: string | null = null;
  /** 在途请求按附件 id 记：不同附件的请求不会互相顶掉（组件还有一层自己那代作废） */
  const inflight = new Map<string, Promise<PreviewBlobResult | null>>();

  function revoke() {
    if (!objectUrl) return;
    const url = objectUrl;
    objectUrl = null;
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* 已经释放过就算了：释放失败不该影响界面 */
    }
  }

  return {
    async load(ref: AttachmentRef): Promise<PreviewBlobResult | null> {
      // 同一个附件已经有 URL：直接复用，不重复取字节、不重复建 URL
      if (ref.id && ref.id === loadedFor && objectUrl) {
        return { url: objectUrl, bytes: loadedBytes };
      }
      // 换了附件：先把上一个释放掉（同一时刻只持有一个 URL）
      if (loadedFor && ref.id !== loadedFor) {
        revoke();
        loadedFor = null;
        loadedBytes = 0;
      }
      const pending = inflight.get(ref.id);
      if (pending) return pending;
      const task = (async (): Promise<PreviewBlobResult | null> => {
        try {
          const blob = await fetcher(ref.id);
          const create = (URL as unknown as { createObjectURL?: (value: Blob) => string })
            .createObjectURL;
          if (typeof create !== "function") {
            failureReason = "这个环境不支持 Blob 缩略图（可以打开或下载副本）";
            return null;
          }
          revoke();
          objectUrl = create(blob);
          loadedFor = ref.id;
          loadedBytes = blob.size;
          failureReason = null;
          return { url: objectUrl, bytes: blob.size };
        } catch (err) {
          failureReason = err instanceof Error ? err.message : String(err);
          return null;
        } finally {
          inflight.delete(ref.id);
        }
      })();
      inflight.set(ref.id, task);
      return task;
    },
    failure() {
      return failureReason;
    },
    revoke,
  };
}

export interface PreviewImageState {
  /** 正在取字节 */
  loading(): boolean;
  /** 已就绪的 blob URL（未就绪时 null） */
  url(): string | null;
  /** 失败 / 不可用的原因（可读一句话；可用时 null） */
  error(): string | null;
  /** 释放（组件卸载、或同一个组件换了另一个附件时） */
  release(): void;
}

/**
 * 图片缩略图状态机（不 import vue）：超限**不请求**，失败如实报错，释放是显式的。
 *
 * 组件在 watch 里 re-run 时先 release 再 start，所以同一时刻只持有一个 blob URL。
 */
export function createPreviewImageState(deps: {
  fetchContent: PreviewContentFetcher;
  /**
   * 清掉 img.src 的引用（组件里是 removeAttribute）。
   * **url 不做成回调**：界面用的是 v-if + ref，元素要等这一版渲染之后才存在；
   * 由消费方 watch(url) 在渲染后 setAttribute，避免「回调时元素还不存在」的竞态。
   */
  clearUrl?(): void;
  /** 状态变化时通知一次（组件用它刷新模板） */
  onUpdate?(): void;
}): PreviewImageState & {
  start(ref: AttachmentRef, budget: { sizeBytes?: number | null; sizeLimitBytes: number }): void;
} {
  const host = createPreviewBlobHost(deps.fetchContent);
  let loading = false;
  let url: string | null = null;
  let error: string | null = null;
  let seq = 0;

  /** 静默停掉当前这一代（交给 start 用）：不通知，避免一次 start 触发两轮渲染。 */
  function stop() {
    seq += 1;
    loading = false;
    url = null;
    error = null;
    deps.clearUrl?.();
    host.revoke();
  }

  function release() {
    stop();
    deps.onUpdate?.();
  }

  function start(
    ref: AttachmentRef,
    budget: { sizeBytes?: number | null; sizeLimitBytes: number },
  ): void {
    stop();
    const mine = seq;
    const size = typeof budget.sizeBytes === "number" ? budget.sizeBytes : null;
    if (size !== null && size > budget.sizeLimitBytes) {
      // 超限不发请求：如实说原因（不是失败，是预算）
      error =
        "这个文件 " +
        humanSize(size) +
        " 超过缩略图上限 " +
        humanSize(budget.sizeLimitBytes) +
        "：不生成缩略图，避免为了看一眼把整张位图读进内存（可以打开或下载副本）";
      deps.onUpdate?.();
      return;
    }
    loading = true;
    deps.onUpdate?.();
    void host.load(ref).then((next) => {
      if (mine !== seq) return; // 已经换过附件 / 已卸载：这次结果作废（blob 由 host 管）
      loading = false;
      url = null;
      if (!next) {
        error = host.failure() ?? "缩略图加载失败（原因未说明）";
        deps.onUpdate?.();
        return;
      }
      if (next.bytes > budget.sizeLimitBytes) {
        // meta 里的 size 缺了或过期：以**真实字节**为准再判一次，超限就立刻释放已读入的字节
        error =
          "这个文件实际 " +
          humanSize(next.bytes) +
          " 超过缩略图上限 " +
          humanSize(budget.sizeLimitBytes) +
          "：已停止生成缩略图并释放已读入的字节（可以打开或下载副本）";
        deps.clearUrl?.();
        host.revoke();
        deps.onUpdate?.();
        return;
      }
      url = next.url;
      error = null;
      deps.onUpdate?.();
    });
  }

  return {
    loading() {
      return loading;
    },
    url() {
      return url;
    },
    error() {
      return error;
    },
    release,
    start,
  };
}
