/**
 * 权限模式 store（R9 契约 §1）。
 *
 * 常驻显示 + 设置页共用同一份状态；模式以服务端为准：
 * * 挂载时 GET /api/security/mode（断线/刷新后恢复）；
 * * 切换只走「用户操作 → PUT /api/security/mode（user_confirmed=true）」；
 * * 后端本分支不新增模式变更事件（Lead 裁决），所以不靠事件更新。
 */
import { defineStore } from "pinia";
import {
  DEFAULT_PERMISSION_MODE,
  PERMISSION_MODE_LABELS,
  getSecurityMode,
  normalizePermissionMode,
  setSecurityMode,
  type PermissionMode,
} from "../services/security";

export const useSecurityStore = defineStore("security", {
  state: () => ({
    mode: DEFAULT_PERMISSION_MODE as PermissionMode,
    /** 后端给的中文标签（缺失时用本地映射，不显示英文枚举） */
    label: PERMISSION_MODE_LABELS[DEFAULT_PERMISSION_MODE],
    /** 旧客户端可用的等价旧值（用于「旧值映射展示」） */
    legacyValue: null as string | null,
    /** true = 新接口不存在，读的是旧设置接口（兼容期事实） */
    compat: false,
    /** 是否已经从服务端读到过真实模式：没读到之前不许把本地默认值说成「当前模式」 */
    loaded: false,
    loading: false,
    /** 最近一次操作失败原因（成功后清空；失败 ≠ 已切换） */
    error: null as string | null,
    saving: false,
  }),
  getters: {
    /** 当前模式的展示标签 */
    currentLabel: (state): string => state.label || PERMISSION_MODE_LABELS[state.mode] || "受限模式",
    /** 还没读到真实模式（顶栏显示「读取中」，不冒充默认值） */
    pending: (state): boolean => !state.loaded && !state.error,
  },
  actions: {
    applyMode(input: {
      mode: unknown;
      label?: string | null;
      legacyValue?: string | null;
      compat?: boolean;
    }) {
      const mode = normalizePermissionMode(input.mode);
      this.mode = mode;
      this.label = input.label || PERMISSION_MODE_LABELS[mode];
      this.legacyValue = input.legacyValue ?? null;
      this.compat = Boolean(input.compat);
      this.loaded = true;
      this.error = null;
    },
    /** 挂载时恢复（失败不伪造：保留 pending/error，界面说「暂时无法确认」） */
    async load() {
      if (this.loading) return;
      this.loading = true;
      this.error = null;
      try {
        const state = await getSecurityMode();
        this.applyMode({
          mode: state.mode,
          label: state.label,
          legacyValue: state.legacyValue,
          compat: state.compat,
        });
      } catch (err) {
        this.error = "读取权限模式失败：" + ((err as Error)?.message || "未知原因");
      } finally {
        this.loading = false;
      }
    },
    /**
     * 用户主动切换。唯一入口是设置页的按钮事件；这里不接受来自模型/工具的调用。
     * 失败时**保持原模式**（不能看起来已经切换成功）。
     */
    async changeMode(next: PermissionMode) {
      if (this.saving) return;
      const previous = this.mode;
      this.saving = true;
      this.error = null;
      try {
        const state = await setSecurityMode(next);
        this.applyMode({
          mode: state.mode,
          label: state.label,
          legacyValue: state.legacyValue,
          compat: state.compat,
        });
      } catch (err) {
        // 失败保留原模式，并把服务端/网络给的原因原样显示
        this.mode = previous;
        this.error = "切换权限模式失败，当前模式未改变：" + ((err as Error)?.message || "未知原因");
        throw err;
      } finally {
        this.saving = false;
      }
    },
  },
});
