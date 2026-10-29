/**
 * 设置页保存模型（唯一实现）—— §5 契约。
 *
 * 为什么是组合式而不是每个分区各写一遍：
 * 1) 「迟到的旧响应不得覆盖新修改」和「失败回滚到真实值」是**安全语义**，
 *    每个分区抄一遍必然出现某处漏了 seq 判断，旧响应就会把用户刚改的值盖回去；
 * 2) 「未保存」标记、按组保存/取消、分区内联反馈必须是同一套口径，
 *    否则用户看到「保存中…」却不知道它属于哪个设置组。
 *
 * 两种保存模式：
 * - 即时保存（`saveNow`）：单个开关/分段/下拉。先乐观更新，失败回滚到真实值，
 *   错误只落在本分区（`notice` 属于当前分区，不共用）。
 * - 按组保存（`useGroupDraft`）：一组字段共用一个草稿与一次显式提交；
 *   失败**保留草稿**（用户填的内容不能因为后端 500 就消失），取消才回滚。
 *
 * 序号规则：每次写请求领一个递增 token，只有 `token === 最新` 才允许把服务端返回值
 * 落回界面、也只有它才允许写成功/失败反馈。旧请求的结果被丢弃 —— 不是覆盖，是忽略。
 *
 * 离开设置页不做任何额外动作：设置页是整页路由，卸载即丢弃未保存草稿，
 * 不自动保存、不弹询问、不发额外写请求（见 §5）。本文件不读 localStorage。
 */
import { computed, onScopeDispose, reactive, ref, watch, type Ref } from "vue";

export type SettingsNoticeKind = "ok" | "err" | "warn" | "info";

export interface SettingsNotice {
  kind: SettingsNoticeKind;
  text: string;
}

/** 成功提示的存活时长：成功只要「看见它发生了」，不该长期占位（失败更持久）。 */
export const NOTICE_OK_MS = 4000;

export function settingsErrorText(e: unknown): string {
  const err = e as { message?: unknown } | null;
  const message = err && typeof err.message === "string" ? err.message : "";
  return message || String(e ?? "未知错误");
}

export interface ImmediateSaveOptions<T> {
  /**
   * 这个设置在分区里的**身份**（F08）。
   *
   * 同一分区有多个独立设置（后台整理开关 / 整理间隔 / 输出速度 / 工具记录…），
   * 它们各自的保存在途互不作废：A 失败时 A 必须回滚并留下错误，B 成功仍是成功。
   * 只有同一个 key 的后发请求才作废先发请求（同一设置仍须防止旧响应覆盖新选择）。
   * 省略 = 分区级（旧调用方式，行为不变）。
   */
  key?: string;
  /** 请求进行中的中性提示（默认「保存中…」） */
  pending?: string;
  /** 乐观更新：先把控件切到用户刚选的值 */
  optimistic?: () => void;
  /** 回滚：失败时把控件恢复到**真实值**（服务端/本地已生效的那个值） */
  rollback?: () => void;
  commit: () => Promise<T>;
  /** 服务端真值回填（只在响应仍是最新时执行） */
  apply?: (server: T) => void;
  /**
   * 迟到成功、但该设置最新一次请求已经失败时的收敛（默认用 apply）。
   * 场景（返修 1）：连续提交 30、40，40 先失败、30 后成功 —— 服务端实际保存的是 30，
   * 界面必须收敛到 30，而不是停在回滚用的更早值；较新成功仍然不允许被旧成功覆盖。
   */
  reconcile?: (server: T) => void;
  success: string | ((server: T) => string);
  /** 失败文案前缀，最终形如「保存输出速度失败：<原因>」 */
  failure: string;
}

export interface QioSettingsSave {
  notice: Ref<SettingsNotice | null>;
  setNotice: (kind: SettingsNoticeKind, text: string) => void;
  clearNotice: () => void;
  beginWrite: (key?: string) => number;
  isLatest: (token: number, key?: string) => boolean;
  saveNow: <T>(options: ImmediateSaveOptions<T>) => Promise<boolean>;
}

/**
 * 一个分区的保存助手。
 * `section` 只用于调试标记（data-settings-section），反馈本身留在本实例里 ——
 * 这样两个分区同时保存时不会互相盖掉提示。
 */
export function useQioSettingsSave(section: string): QioSettingsSave {
  const notice = ref<SettingsNotice | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;
  /**
   * 单调整体写序号（无 key 的旧调用方式）。
   * 带 key 的调用各有一份序号：不同设置在途互不作废，只有同一设置的后发才作废先发。
   */
  let seq = 0;
  const keySeq = new Map<string, number>();
  /** 当前提示属于哪个设置（key）；null = 分区级提示。 */
  let noticeKey: string | null = null;
  /**
   * 按设置保留的未解决失败（F08 返修 2）：两个不同设置先后失败后，只修好第二个，
   * 第一个的错误必须继续可见；成功只清掉它自己那个设置的错误。
   */
  const errorNotices = new Map<string, SettingsNotice>();
  /** 每个设置「最新一次请求」的结果：只有最新请求失败时，迟到的成功才允许收敛真实值。 */
  const latestOutcome = new Map<string, "success" | "failure">();
  /** 每个设置在途请求数：还有更新请求在跑时，旧成功不收敛，避免打断在途选择。 */
  const inflightWrites = new Map<string, number>();

  function writeId(key?: string): string {
    return key ?? "";
  }

  function inflightOf(id: string): number {
    return inflightWrites.get(id) ?? 0;
  }

  function clearTimer(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function showLatestUnresolvedError(): void {
    const entries = Array.from(errorNotices.entries());
    if (!entries.length) return;
    const last = entries[entries.length - 1];
    notice.value = last[1];
    noticeKey = last[0];
  }

  function setNotice(kind: SettingsNoticeKind, text: string, key?: string): void {
    const owner = key ?? null;
    if (kind === "ok") {
      // 成功只清除「这个设置自己」的错误；其它设置未解决的失败继续显示在原反馈区。
      if (owner !== null) errorNotices.delete(owner);
      clearTimer();
      if (errorNotices.size) {
        showLatestUnresolvedError();
        return;
      }
      notice.value = { kind, text };
      noticeKey = owner;
      timer = setTimeout(() => {
        timer = null;
        if (notice.value?.kind === "ok") {
          notice.value = null;
          noticeKey = null;
        }
      }, NOTICE_OK_MS);
      return;
    }
    if (kind === "err") {
      clearTimer();
      const error: SettingsNotice = { kind, text };
      notice.value = error;
      noticeKey = owner;
      if (owner !== null) errorNotices.set(owner, error);
      return;
    }
    // 中性 / 成功态提示：有未解决的错误时不动（错误优先保留）。
    if (errorNotices.size) return;
    clearTimer();
    notice.value = { kind, text };
    noticeKey = owner;
  }

  function clearNotice(): void {
    clearTimer();
    notice.value = null;
    noticeKey = null;
    errorNotices.clear();
  }

  function beginWrite(key?: string): number {
    if (key) {
      const value = (keySeq.get(key) ?? 0) + 1;
      keySeq.set(key, value);
      return value;
    }
    seq += 1;
    return seq;
  }

  function isLatest(token: number, key?: string): boolean {
    return key ? (keySeq.get(key) ?? 0) === token : token === seq;
  }

  async function saveNow<T>(options: ImmediateSaveOptions<T>): Promise<boolean> {
    const key = options.key;
    const id = writeId(key);
    const token = beginWrite(key);
    inflightWrites.set(id, inflightOf(id) + 1);
    options.optimistic?.();
    setNotice("info", options.pending ?? "保存中…", key);
    try {
      const server = await options.commit();
      if (!isLatest(token, key)) {
        // 迟到成功：默认不落 UI（更新的那次修改才是事实）。但如果该设置最新一次请求已经
        // 失败、且已没有在途请求，这次成功携带的是服务端真实保存值 —— 界面必须收敛过去，
        // 否则「前一次成功、后一次失败」会退回到更早的值（返修 1）。
        if (latestOutcome.get(id) === "failure" && inflightOf(id) - 1 <= 0) {
          (options.reconcile ?? options.apply)?.(server);
        }
        return false;
      }
      latestOutcome.set(id, "success");
      options.apply?.(server);
      const text =
        typeof options.success === "function" ? options.success(server) : options.success;
      setNotice("ok", text, key);
      return true;
    } catch (e) {
      if (!isLatest(token, key)) return false;
      latestOutcome.set(id, "failure");
      // 失败必须让控件回到真实值：停在「看起来改了、其实没保存」比报错更危险。
      options.rollback?.();
      setNotice("err", `${options.failure}失败：${settingsErrorText(e)}`, key);
      return false;
    } finally {
      inflightWrites.set(id, Math.max(0, inflightOf(id) - 1));
    }
  }

  // 卸载后计时器不能再写状态（例如成功提示的 4s 计时器跨越了页面卸载）。
  onScopeDispose(clearTimer);

  // section 参与 data 属性时有用，但不应产生任何全局副作用；保留为调试标签。
  void section;
  return { notice, setNotice, clearNotice, beginWrite, isLatest, saveNow };
}

/** 草稿值只允许可 JSON 化的普通值：这里也顺带挡住误传 Ref/组件实例。 */
export type SettingsDraftValue = string | number | boolean | null;

export type SettingsDraftShape = Record<string, SettingsDraftValue>;

export interface GroupSaveOptions<T extends SettingsDraftShape> {
  /** 初始值（加载完成前的占位值） */
  initial: T;
  /** 提交前的本地校验；返回文案即校验失败原因（不发明细时返回 null） */
  validate?: (values: T) => string | null;
  /** 真的提交；返回服务端真值（用于重置基线） */
  commit: (values: T) => Promise<T>;
  success?: (server: T) => string;
  failure: string;
  pending?: string;
}

export interface GroupDraft<T extends SettingsDraftShape> {
  /** 可变的草稿值（绑定到控件） */
  values: T;
  /** 最近一次已保存（或刚加载）的真值，用于 cancel 与「未保存」判断 */
  baseline: Ref<T>;
  dirty: Ref<boolean>;
  saving: Ref<boolean>;
  error: Ref<string | null>;
  /**
   * 草稿修订号：任何字段变化（含 reset / cancel / 保存成功回填）都会 +1。
   * 分区加载迟到时用它判断「用户在等待期间有没有改过」——改过就不能拿旧加载值覆盖。
   */
  revision: Ref<number>;
  /** 保存成功次数：初始读取迟到时用它判断「是否已有更新的保存结果」 */
  savedRevision: Ref<number>;
  /** 用服务端/初始真值重置草稿并刷新基线（加载完成时调用） */
  reset: (next: T) => void;
  /**
   * 只把「服务端实际保存值」记为取消基线，不动用户正在编辑的草稿（返修 4）。
   * 初始读取迟到且用户已经编辑过时用它：保留输入，但取消回到真实保存值，
   * 而不是回到加载前的占位默认值。
   */
  adoptBaseline: (next: T) => void;
  /** 取消：丢弃草稿，回到基线 */
  cancel: () => void;
  /** 保存：成功返回 true；失败保留草稿并写入 error */
  save: () => Promise<boolean>;
}

function cloneDraft<T extends SettingsDraftShape>(value: T): T {
  return { ...value };
}

function sameDraft(a: SettingsDraftShape, b: SettingsDraftShape): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((key) => a[key] === b[key]);
}

/**
 * 按组草稿。
 *
 * 迟到的响应规则比即时保存更细：用户可能在等待期间继续改字段。
 * 因此成功时先把「已保存真值」写成新基线，**只有在等待期间没有新修改**才把
 * 服务端返回值抄回草稿；否则保留用户的新输入（它相对新基线仍是未保存状态）。
 */
export function useGroupDraft<T extends SettingsDraftShape>(options: GroupSaveOptions<T>): GroupDraft<T> {
  const values = reactive(cloneDraft(options.initial)) as T;
  const baseline = ref(cloneDraft(options.initial)) as Ref<T>;
  const saving = ref(false);
  const error = ref<string | null>(null);
  /** 草稿修订号：任何字段变化都会 +1，用于识别「等待期间又改了」。 */
  const revision = ref(0);
  const savedRevision = ref(0);
  let writeSeq = 0;
  /** 当前正在等响应的保存（用于「被 reset/新保存作废后仍要正确结束 saving」）。 */
  let activeSaveToken: number | null = null;

  watch(
    () => ({ ...values }),
    () => {
      revision.value += 1;
    },
    { deep: true, flush: "sync" },
  );

  const dirty = computed(() => !sameDraft(values, baseline.value));

  function reset(next: T): void {
    const copy = cloneDraft(next);
    baseline.value = copy;
    Object.assign(values, copy);
    error.value = null;
    revision.value += 1;
    writeSeq += 1; // 加载完成：作废此前所有在途响应
  }

  function adoptBaseline(next: T): void {
    // 只更新「取消回到哪」的基线：不动草稿、不动在途保存（否则保存成功会被误作废），
    // 也不清除尚未解决的保存错误（返修 3：失败反馈不能被随后返回的读取清除）。
    baseline.value = cloneDraft(next);
  }

  function cancel(): void {
    Object.assign(values, cloneDraft(baseline.value));
    error.value = null;
    revision.value += 1;
  }

  async function save(): Promise<boolean> {
    if (saving.value) return false;
    const invalid = options.validate?.(values);
    if (invalid) {
      error.value = invalid;
      return false;
    }
    const token = ++writeSeq;
    const startRevision = revision.value;
    activeSaveToken = token;
    saving.value = true;
    error.value = null;
    try {
      const server = await options.commit(cloneDraft(values));
      if (token !== writeSeq) return false; // 已有更新的保存/加载：旧响应不落 UI
      const next = cloneDraft(server);
      baseline.value = next;
      savedRevision.value += 1;
      // 等待期间用户没再改 → 用服务端规范化后的真值回填；否则保留新输入
      if (revision.value === startRevision) {
        Object.assign(values, next);
        revision.value += 1;
      }
      return true;
    } catch (e) {
      if (token !== writeSeq) return false;
      // 显式保存失败保留草稿：用户刚填的内容不能因为后端错误被清空
      error.value = `${options.failure}失败：${settingsErrorText(e)}`;
      return false;
    } finally {
      // 关键（F14）：reset / 更新的保存会作废这次响应（token !== writeSeq），
      // 但「这次保存」必须自己结束 saving —— 否则界面会永远停在「保存中」，
      // 后续 save() 也会被入口的 saving 判断永久拒绝。
      if (activeSaveToken === token) {
        activeSaveToken = null;
        saving.value = false;
      }
    }
  }

  return { values, baseline, dirty, saving, error, revision, savedRevision, reset, adoptBaseline, cancel, save };
}