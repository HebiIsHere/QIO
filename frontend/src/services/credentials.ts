/**
 * 凭据表单的共用逻辑：首次引导与设置页用**同一套**默认值、校验与提交路径。
 *
 * 以前两处各写一份：引导页自己识别厂商、自己拼 payload、自己决定错误文案，
 * 设置页又是另一套，于是默认值、验证方式和提示语经常不一致。这里把「怎么填、
 * 怎么校验、怎么保存、怎么验证」收敛到一处，组件只负责渲染。
 *
 * 几条硬约束：
 * - 厂商由用户选；Key 前缀只做**本地**提示（不发网络请求）；
 * - 保存与验证分开：保存成功 ≠ 验证通过；
 * - 关闭表单/切换厂商/改 Key 之后，旧请求的结果不能覆盖新状态（版本号 + 中断）；
 * - 已保存但没验证通过的记录，重试作用在**同一条**记录上。
 */

import { computed, reactive, ref, type ComputedRef, type Ref } from "vue";
import {
  api,
  ApiError,
  type CredentialMeta,
  type ProviderPreset,
  type VerifyReport,
} from "./api";

export type CredentialMode = "create" | "edit" | "rotate";

/** 用途标签：对外一律用中文，内部值保持不变（main-loop 仍是主循环的标识）。 */
export const USAGE_OPTIONS: { value: string; label: string }[] = [
  { value: "main-loop", label: "主对话" },
  { value: "chat", label: "通用对话" },
  { value: "code", label: "写代码" },
  { value: "subagent", label: "子任务" },
  { value: "vision", label: "看图/视觉" },
  { value: "video", label: "视频" },
  { value: "audio", label: "语音" },
  { value: "research", label: "研究" },
  { value: "embedding", label: "向量/嵌入" },
];

export const DEFAULT_USAGE = "main-loop";

export function usageLabel(tag: string): string {
  return USAGE_OPTIONS.find((o) => o.value === tag)?.label ?? tag;
}

export function usageSummary(tags: string[]): string {
  const labels = tags.map(usageLabel);
  return labels.length ? labels.join("、") : "未选择";
}

/** 验证状态 → 界面文案。`legacy` 是本次改动之前就在用的老数据，按可用处理。 */
export function verifyLabel(state: string | null | undefined): string {
  switch (state) {
    case "verified":
      return "已验证可用";
    case "failed":
      return "验证未通过";
    case "unverified":
      return "尚未验证";
    default:
      return "";
  }
}

/** 厂商类别 → 一句「Key 会发到哪里」。避免用户误判发送目标。 */
export function destinationLabel(preset: ProviderPreset | null): string {
  if (!preset) return "";
  if (preset.category === "aggregator") {
    return `第三方转发：Key 会发送到 ${preset.base_url}，由它再转发给多家模型服务`;
  }
  if (preset.category === "custom") {
    return "自定义服务：Key 会发送到你填写的服务地址";
  }
  return `官方服务：Key 只发送到 ${preset.base_url}`;
}

/**
 * 这条凭据**当前实际上**用什么协议。
 *
 * 历史数据没有存协议（`kind` 为空），运行时按地址判断（见后端 `uses_anthropic`）。
 * 表单必须用同一个判断：否则编辑一条老凭据时会把「空 → openai」当成一次协议变更，
 * 于是要么被后端要求重新确认，要么在用户什么都没改的情况下报错。
 */
export function resolvedKind(meta: CredentialMeta | null | undefined): string {
  const kind = (meta?.kind || "").trim();
  if (kind) return kind;
  return (meta?.endpoint || "").toLowerCase().includes("anthropic.com") ? "anthropic" : "openai";
}

/**
 * 本地前缀提示：**不联网**，只回答「这个 Key 看起来像谁家的」。
 *
 * 只认足够长的前缀（`sk-proj-` / `sk-ant-` / `gsk_` / `sk-or-` / `AIza` / `xai-`）；
 * `sk-` 这种多家共用的前缀只作为提示，不据此替用户选厂商。
 */
export function keyHint(
  secret: string,
  providers: ProviderPreset[],
): { preset: ProviderPreset; confident: boolean } | null {
  const value = secret.trim();
  if (!value) return null;
  const hints: { prefix: string; presetId: string }[] = [
    { prefix: "sk-proj-", presetId: "openai" },
    { prefix: "sk-ant-", presetId: "anthropic" },
    { prefix: "sk-or-", presetId: "openrouter" },
    { prefix: "gsk_", presetId: "groq" },
    { prefix: "xai-", presetId: "xai" },
    { prefix: "AIza", presetId: "gemini" },
    { prefix: "sk-", presetId: "deepseek" },
  ];
  const hit = hints.find((h) => value.startsWith(h.prefix));
  if (!hit) return null;
  const preset = providers.find((p) => p.id === hit.presetId) ?? null;
  if (!preset) return null;
  return { preset, confident: hit.prefix.length >= 6 };
}

export interface FormStatus {
  kind: "ok" | "warn" | "err" | "info";
  title: string;
  message: string;
  detail?: string;
  /** 有值时说明这条记录已经落库，可以直接重试验证（不会重复创建） */
  retryKeyId?: string;
}

export interface CredentialFormOptions {
  mode: CredentialMode;
  initial?: CredentialMeta | null;
  /** 保存成功（无论验证是否通过）后回调；由调用方决定是关闭还是继续 */
  onSaved?: (payload: {
    credential: CredentialMeta | null;
    report: VerifyReport | null;
    mode: CredentialMode;
  }) => void;
}

export interface CredentialFormApi {
  providers: Ref<ProviderPreset[]>;
  providersError: Ref<string>;
  form: {
    providerId: string;
    secret: string;
    endpoint: string;
    kind: string;
    model: string;
    note: string;
    budget: number | null;
    confirmTarget: boolean;
  };
  tags: Ref<string[]>;
  customTag: Ref<string>;
  usageOpen: Ref<boolean>;
  advanced: Ref<boolean>;
  showSecret: Ref<boolean>;
  busy: Ref<boolean>;
  progress: Ref<string>;
  status: Ref<FormStatus | null>;
  fieldError: Ref<{ field: string; message: string } | null>;
  models: Ref<string[]>;
  modelsNote: Ref<string>;
  hintText: Ref<string>;
  providerOptions: ComputedRef<{ value: string; label: string; hint: string }[]>;
  selectedProvider: ComputedRef<ProviderPreset | null>;
  destination: ComputedRef<string>;
  targetChanged: ComputedRef<boolean>;
  needsSecret: ComputedRef<boolean>;
  primaryLabel: ComputedRef<string>;
  savedKeyId: Ref<string | null>;
  /** 用户是不是已经开始填了（选过厂商或敲过内容）—— 引导页据此决定主按钮说什么 */
  touched: ComputedRef<boolean>;
  chooseProvider: (id: string) => void;
  onSecretInput: () => void;
  toggleTag: (tag: string) => void;
  addCustomTag: () => void;
  refreshModels: () => void;
  submit: () => Promise<void>;
  retry: () => Promise<void>;
  cancel: () => void;
  dispose: () => void;
}

/** 把后端/网络错误翻译成普通用户能读的中文。 */
export function readableError(error: unknown): string {
  if (error instanceof ApiError) {
    const text = error.message;
    const detail = text.includes(": ") ? text.slice(text.indexOf(": ") + 2) : text;
    if (error.status === 500) return "系统安全存储写入失败，密钥没有保存下来";
    return detail.slice(0, 200);
  }
  if (error instanceof DOMException && error.name === "AbortError") return "请求已取消";
  return error instanceof Error ? error.message.slice(0, 200) : String(error);
}

export function useCredentialForm(options: CredentialFormOptions): CredentialFormApi {
  const mode = options.mode;
  const initial = options.initial ?? null;

  const providers = ref<ProviderPreset[]>([]);
  const providersError = ref("");
  /**
   * 已经写进库的那条记录（新建成功后才有值）。
   *
   * 它的作用不是「再展示一次」，而是让同一个表单接着编辑**同一条**记录：
   * 保存 → 验证没过 → 用户改 Key 再点保存，必须作用在原记录上，不能新建第二条。
   * 也因为它，写库之后「服务地址 / 协议」的比较基准才是真实的落库值。
   */
  const savedMeta = ref<CredentialMeta | null>(null);
  /** 上一次提交时用的 Key（只在内存里，用来判断「只是重试」还是「换了钥匙」） */
  let submittedSecret = "";
  const form = reactive({
    providerId: mode === "create" ? "" : (initial?.provider_id ?? ""),
    secret: "",
    endpoint: mode === "create" ? "" : (initial?.endpoint ?? ""),
    kind: mode === "create" ? "openai" : resolvedKind(initial),
    model: mode === "create" ? "" : (initial?.default_model ?? ""),
    note: mode === "create" ? "" : (initial?.note ?? ""),
    budget: mode === "create" ? null : (initial?.budget ?? null),
    confirmTarget: false,
  });
  // 新建默认就是「主对话」；编辑保留原有用途，绝不重新套用新建默认值。
  const tags = ref<string[]>(
    mode === "create" ? [DEFAULT_USAGE] : [...(initial?.tags ?? [])],
  );
  const customTag = ref("");
  const usageOpen = ref(false);
  const advanced = ref(false);
  const showSecret = ref(false);
  const busy = ref(false);
  const progress = ref("");
  const status = ref<FormStatus | null>(null);
  const fieldError = ref<{ field: string; message: string } | null>(null);
  const models = ref<string[]>([]);
  const modelsNote = ref("");
  const hintText = ref("");
  const savedKeyId = ref<string | null>(null);

  // 同一次「新建」的重试标识：请求超时后用户再点一次保存，后端据此识别为同一次提交。
  const clientRequestId = `cred_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  // 两个独立的序号：保存/验证的序号绝不能被后台的「取模型列表」顶掉，
  // 否则一次后台刷新就会让正在进行的保存被判成过期请求，界面永远停在「保存中…」。
  let seq = 0;
  let modelSeq = 0;
  let controller: AbortController | null = null;
  let hintTimer: ReturnType<typeof setTimeout> | null = null;
  let modelTimer: ReturnType<typeof setTimeout> | null = null;

  const selectedProvider = computed(
    () => providers.value.find((p) => p.id === form.providerId) ?? null,
  );
  const providerOptions = computed(() =>
    providers.value.map((p) => ({
      value: p.id,
      label: p.category === "custom" ? p.name : `${p.name}· ${p.category_label}`,
      hint: p.category === "aggregator" ? "第三方转发" : p.note || p.base_url,
    })),
  );
  const destination = computed(() => destinationLabel(selectedProvider.value));
  const baselineKind = computed(() =>
    mode === "create"
      ? savedMeta.value
        ? resolvedKind(savedMeta.value)
        : "openai"
      : resolvedKind(initial),
  );
  const targetChanged = computed(() => {
    const baseMeta = mode === "create" ? savedMeta.value : initial;
    if (!baseMeta) return false;
    const endpointChanged = (baseMeta.endpoint ?? "") !== form.endpoint.trim();
    const kindChanged = baselineKind.value !== form.kind;
    return endpointChanged || kindChanged;
  });
  const touched = computed(() =>
    Boolean(
      form.providerId ||
        form.secret.trim() ||
        form.model.trim() ||
        form.note.trim() ||
        (mode === "create" && form.endpoint.trim()),
    ),
  );
  const needsSecret = computed(() => {
    if (mode === "create" || mode === "rotate") return true;
    return targetChanged.value;
  });
  const primaryLabel = computed(() => {
    if (busy.value) return "保存中…";
    return "保存";
  });

  async function loadProviders() {
    try {
      const result = await api.listProviders();
      providers.value = result.providers;
      providersError.value = "";
    } catch (error) {
      providersError.value = readableError(error);
    }
  }

  function chooseProvider(id: string, opts: { keepKey?: boolean } = {}) {
    form.providerId = id;
    const preset = providers.value.find((p) => p.id === id);
    if (!preset) return;
    // 自定义服务没有默认地址/模型：必须填的东西不能藏在收起的区域里。
    if (preset.category === "custom") advanced.value = true;
    if (mode === "create") {
      form.endpoint = preset.base_url;
      form.kind = preset.kind;
      form.model = preset.suggested_model;
      form.note = preset.name;
    }
    if (!opts.keepKey) refreshModels();
  }

  function onSecretInput() {
    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      hintTimer = null;
      if (mode !== "create") return;
      const hint = keyHint(form.secret, providers.value);
      if (!hint) {
        hintText.value = "";
        return;
      }
      if (hint.confident && !form.providerId) {
        chooseProvider(hint.preset.id, { keepKey: true });
        hintText.value = `根据 Key 的前缀预选了「${hint.preset.name}」；如果不对，可以在这里改。`;
        return;
      }
      if (form.providerId && form.providerId !== hint.preset.id) {
        hintText.value = `这个 Key 看起来也可能是「${hint.preset.name}」的；当前仍按你选择的厂商发送。`;
        return;
      }
      hintText.value = `Key 前缀看起来像「${hint.preset.name}」。`;
    }, 400);
  }

  function toggleTag(tag: string) {
    const next = new Set(tags.value);
    if (next.has(tag)) next.delete(tag);
    else next.add(tag);
    tags.value = Array.from(next);
    if (fieldError.value?.field === "tags") fieldError.value = null;
  }

  function addCustomTag() {
    const value = customTag.value.trim();
    if (value && !tags.value.includes(value)) tags.value = [...tags.value, value];
    customTag.value = "";
  }

  function refreshModels() {
    if (modelTimer) clearTimeout(modelTimer);
    modelTimer = setTimeout(async () => {
      modelTimer = null;
      const endpoint = form.endpoint.trim();
      if (!endpoint) return;
      const newSecret = form.secret.trim();
      // 已存的 Key 只能问**它自己那个地址**要模型列表。
      //
      // 改了服务地址（或协议）之后再用已存的 Key 去请求，等于在用户点「保存」、
      // 勾选「确认发送到新地址」之前就把这把钥匙交给了新地址 —— 这里必须拦住：
      // 没有新 Key 就干脆不取候选列表，让用户手填模型名，等保存之后再取。
      const storedKeyUsable = Boolean(initial?.key_id) && !targetChanged.value;
      if (!newSecret && !storedKeyUsable) {
        models.value = [];
        modelsNote.value = initial?.key_id
          ? "地址和这把 Key 原来的地址不一致，这里不会自动去取模型列表；填好 Key 并保存后即可取回。"
          : "";
        return;
      }
      const token = ++modelSeq;
      try {
        const params: { endpoint: string; kind?: string; keyId?: string; secret?: string } = {
          endpoint,
          kind: form.kind,
        };
        if (newSecret) params.secret = newSecret;
        else if (initial?.key_id) params.keyId = initial.key_id;
        const result = await api.listCredentialModels(params);
        if (token !== modelSeq) return;
        models.value = result.models;
        modelsNote.value = result.models.length
          ? ""
          : "没有取到模型列表；可以直接手动填写模型名称。";
      } catch {
        if (token !== modelSeq) return;
        models.value = [];
        modelsNote.value = "没有取到模型列表；可以直接手动填写模型名称。";
      }
    }, 450);
  }

  function currentTarget() {
    return {
      endpoint: form.endpoint.trim(),
      kind: form.kind,
      model: form.model.trim(),
    };
  }

  function validate(): boolean {
    const target = currentTarget();
    if (mode === "create" && !form.providerId) {
      // 先说最靠前的一件事：没选厂商时地址/模型都是空的，
      // 直接报「请填写服务地址」会让用户以为要自己去改高级设置。
      fieldError.value = { field: "provider", message: "请先选择服务厂商" };
      return false;
    }
    if (!target.endpoint) {
      fieldError.value = { field: "endpoint", message: "请填写服务地址" };
      advanced.value = true;
      return false;
    }
    if (!target.model) {
      fieldError.value = { field: "model", message: "请选择或填写模型名称" };
      advanced.value = true;
      return false;
    }
    if (!tags.value.length) {
      fieldError.value = { field: "tags", message: "请至少选择一种用途（例如「主对话」）" };
      usageOpen.value = true;
      return false;
    }
    if (needsSecret.value && !form.secret.trim()) {
      fieldError.value = { field: "secret", message: "请填写 API Key" };
      return false;
    }
    if (targetChanged.value && !form.confirmTarget) {
      fieldError.value = {
        field: "target",
        message: "服务地址或连接协议变了，这把 Key 会被发到新的地址；请勾选确认后再保存",
      };
      return false;
    }
    fieldError.value = null;
    return true;
  }

  function setVerifyStatus(report: VerifyReport, keyId: string) {
    savedKeyId.value = keyId;
    if (report.ok) {
      status.value = {
        kind: "ok",
        title: "已保存，模型可用",
        message: report.message,
        detail: report.detail,
      };
      return;
    }
    status.value = {
      kind: "warn",
      title: "已保存，尚未通过验证",
      message: report.message,
      detail: report.detail,
      retryKeyId: keyId,
    };
  }

  async function submit() {
    if (busy.value) return;
    status.value = null;
    if (!validate()) return;
    busy.value = true;
    progress.value = mode === "create" ? "正在保存……" : "正在更新……";
    const token = ++seq;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    try {
      if (mode === "create") {
        const payload: Record<string, unknown> = {
          secret: form.secret.trim(),
          tags: tags.value,
          endpoint: currentTarget().endpoint,
          default_model: currentTarget().model,
          kind: form.kind,
          client_request_id: clientRequestId,
        };
        if (form.providerId) payload.provider = form.providerId;
        if (form.note.trim()) payload.note = form.note.trim();
        if (form.budget && form.budget > 0) payload.budget = form.budget;

        // 已经写过库了：后续的「保存」都作用在**同一条**记录上，不新建。
        // - 钥匙没变 → 只是重试验证（不写库、不涨版本）；
        // - 钥匙换了 → 原子替换同一把凭据（失败时原凭据仍可用）；
        //   改地址/协议还要求显式确认，与编辑一致。
        if (savedMeta.value) {
          const keyId = savedMeta.value.key_id;
          if (form.secret.trim() === submittedSecret) {
            progress.value = "正在重新验证……";
            const retried = await api.verifyCredential(keyId, signal);
            if (token !== seq) return;
            progress.value = retried.verify.ok ? "验证通过" : "验证没有通过";
            setVerifyStatus(retried.verify, keyId);
            options.onSaved?.({ credential: null, report: retried.verify, mode });
            return;
          }
          progress.value = "正在更新这条凭据……";
          const update: Record<string, unknown> = { ...payload, secret: form.secret.trim() };
          delete update.client_request_id;
          delete update.provider;
          if (form.kind === baselineKind.value) delete update.kind;
          if (targetChanged.value) update.confirm_reconfigure = true;
          const changed = await api.updateCredentialMeta(keyId, update, signal);
          if (token !== seq) return;
          submittedSecret = form.secret.trim();
          if (changed.credential) savedMeta.value = changed.credential;
          if (changed.verify) {
            progress.value = changed.verify.ok ? "验证通过" : "验证没有通过";
            setVerifyStatus(changed.verify, keyId);
          } else {
            status.value = { kind: "ok", title: "已保存", message: "设置已更新" };
          }
          options.onSaved?.({ credential: changed.credential, report: changed.verify, mode });
          return;
        }

        progress.value = "正在保存……";
        const result = await api.createCredential(payload, signal);
        if (token !== seq) return;
        progress.value = result.verify.ok ? "验证通过" : "验证没有通过";
        submittedSecret = form.secret.trim();
        if (result.credential) savedMeta.value = result.credential;
        setVerifyStatus(result.verify, result.key_id);
        options.onSaved?.({ credential: result.credential, report: result.verify, mode });
        return;
      }

      const keyId = initial?.key_id ?? "";
      if (!keyId) throw new Error("缺少要编辑的凭据");
      const payload: Record<string, unknown> = {
        tags: tags.value,
        endpoint: currentTarget().endpoint,
        default_model: currentTarget().model,
        note: form.note.trim() ? form.note.trim() : null,
        budget: form.budget && form.budget > 0 ? form.budget : null,
      };
      // 协议没变就不提交这个字段：老凭据的 kind 是空的，写回 "openai" 会被后端
      // 当成一次「改了发送目标」的重新配置（要重填 Key + 显式确认）。
      if (form.kind !== baselineKind.value) payload.kind = form.kind;
      if (form.secret.trim()) payload.secret = form.secret.trim();
      if (targetChanged.value) payload.confirm_reconfigure = true;

      if (mode === "rotate") {
        // 先验证新钥匙，再原子替换：验证不过就完全不碰原凭据。
        progress.value = "正在验证新的 API Key……";
        const draft = await api.verifyCredentialDraft(
          {
            secret: form.secret.trim(),
            endpoint: currentTarget().endpoint,
            default_model: currentTarget().model,
            kind: form.kind,
          },
          signal,
        );
        if (token !== seq) return;
        if (!draft.verify.ok) {
          status.value = {
            kind: "err",
            title: "新的 API Key 没有通过验证，原凭据仍然可用",
            message: draft.verify.message,
            detail: draft.verify.detail,
          };
          return;
        }
      }

      progress.value = "正在写入……";
      const updated = await api.updateCredentialMeta(keyId, payload, signal);
      if (token !== seq) return;
      if (form.secret.trim()) submittedSecret = form.secret.trim();
      if (updated.credential) savedMeta.value = updated.credential;
      const report = updated.verify;
      if (report) {
        progress.value = report.ok ? "验证通过" : "验证没有通过";
        setVerifyStatus(report, keyId);
      } else {
        status.value = {
          kind: "ok",
          title: "已保存",
          message: "设置已更新",
        };
      }
      options.onSaved?.({ credential: updated.credential, report, mode });
    } catch (error) {
      if (token !== seq) return;
      progress.value = "";
      const aborted = error instanceof DOMException && error.name === "AbortError";
      if (aborted) {
        status.value = null;
        return;
      }
      // 写入失败绝不能显示成功，也不能假装有一条记录：如实说「保存失败 + 为什么」。
      status.value = { kind: "err", title: "保存失败", message: readableError(error) };
    } finally {
      if (token === seq) {
        busy.value = false;
        if (progress.value.startsWith("正在")) progress.value = "";
      }
    }
  }

  async function retry() {
    const keyId = savedKeyId.value ?? status.value?.retryKeyId ?? null;
    if (!keyId || busy.value) return;
    busy.value = true;
    progress.value = "正在重新验证……";
    const token = ++seq;
    try {
      const result = await api.verifyCredential(keyId);
      if (token !== seq) return;
      setVerifyStatus(result.verify, keyId);
      progress.value = result.verify.ok ? "验证通过" : "验证没有通过";
      options.onSaved?.({ credential: null, report: result.verify, mode });
    } catch (error) {
      if (token !== seq) return;
      status.value = { kind: "err", title: "验证失败", message: readableError(error) };
    } finally {
      if (token === seq) busy.value = false;
    }
  }

  function cancel() {
    seq += 1;
    controller?.abort();
    controller = null;
    form.secret = "";
    savedKeyId.value = null;
    status.value = null;
    if (hintTimer) clearTimeout(hintTimer);
    if (modelTimer) clearTimeout(modelTimer);
  }

  function dispose() {
    cancel();
  }

  void loadProviders();

  return {
    providers,
    providersError,
    form,
    tags,
    customTag,
    usageOpen,
    advanced,
    showSecret,
    busy,
    progress,
    status,
    fieldError,
    models,
    modelsNote,
    hintText,
    providerOptions,
    selectedProvider,
    destination,
    targetChanged,
    needsSecret,
    primaryLabel,
    savedKeyId,
    touched,
    chooseProvider,
    onSecretInput,
    toggleTag,
    addCustomTag,
    refreshModels,
    submit,
    retry,
    cancel,
    dispose,
  };
}


