/**
 * 更新状态机（spec 2026-09-22-updater-design §3）。
 *
 * 规则：只能静默**检查**；下载与安装必须由用户点击触发（`autoCheck` 只影响检查）。
 * 「已是最新」只允许出现在 `up-to-date` —— 检查失败必须落在 `failed`。
 *
 * 定时器与并发（修复提示词 §2 最后一段）：
 * * 首次静默检查与周期检查的句柄都保存，`stopAutoCheck` / 停用 / 重复启动时一并清理；
 * * 手动检查、自动检查、安装之间**同一时刻只有一个有效操作**（`_activeOperation`）；
 * * 每个操作带序号，被停用或被新操作取代之后，旧回调不得再改任何状态；
 * * 自动检查仍然只检查，不下载不安装。
 */
import { defineStore } from "pinia";
import {
  compareVersions,
  describeUpdateError,
  tauriUpdaterApi,
  type UpdateErrorKind,
  type UpdaterApi,
} from "../services/updater";

export type UpdatePhase =
  | "idle"
  | "checking"
  | "up-to-date"
  | "available"
  | "downloading"
  | "ready"
  | "failed";

/** 一次有效操作的种类：自动检查与手动检查在「停用」时的处置不同。 */
export type UpdateOperationKind = "check" | "auto-check" | "install";

interface ActiveOperation {
  seq: number;
  kind: UpdateOperationKind;
  /** 操作开始前的展示状态：被停用取消时恢复，避免停在 checking 上。 */
  phaseBefore: UpdatePhase;
  messageBefore: string;
  errorKindBefore: UpdateErrorKind;
}

/** 启动后多久做第一次静默检查；之后每 24 小时一次。 */
export const FIRST_CHECK_DELAY_MS = 10_000;
export const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;
const AUTO_CHECK_KEY = "qio-updater-auto-check";

export const useUpdaterStore = defineStore("updater", {
  state: () => ({
    phase: "idle" as UpdatePhase,
    currentVersion: "" as string,
    availableVersion: "" as string,
    notes: "" as string,
    progressPercent: null as number | null,
    downloaded: 0,
    total: null as number | null,
    /** 失败原因的人话（分类见 services/updater.describeUpdateError） */
    message: "",
    errorKind: "unknown" as UpdateErrorKind,
    lastCheckedAt: "" as string,
    autoCheck: true,
    _api: null as UpdaterApi | null,
    /** 首次静默检查的 setTimeout 句柄（必须保存，否则停用之后还会触发一次） */
    _firstCheckTimer: null as ReturnType<typeof setTimeout> | null,
    /** 周期检查的 setInterval 句柄 */
    _intervalTimer: null as ReturnType<typeof setInterval> | null,
    /** 操作序号：每次开始操作自增；旧回调拿不到当前序号就被丢弃 */
    _operationSeq: 0,
    _activeOperation: null as ActiveOperation | null,
  }),
  getters: {
    /** 设置入口上的小圆点：只有"确实有新版本"才亮。 */
    hasUpdate: (state): boolean => state.phase === "available" || state.phase === "ready",
    busy: (state): boolean => state.phase === "checking" || state.phase === "downloading",
  },
  actions: {
    configure(api: UpdaterApi) {
      this._api = api;
    },
    async _apiOrLoad(): Promise<UpdaterApi> {
      if (!this._api) this._api = await tauriUpdaterApi();
      return this._api;
    },
    loadAutoCheckPreference() {
      try {
        const raw = localStorage.getItem(AUTO_CHECK_KEY);
        if (raw !== null) this.autoCheck = raw === "true";
      } catch {
        /* 读不到就保持默认开启，不影响功能 */
      }
    },
    setAutoCheck(enabled: boolean) {
      this.autoCheck = enabled;
      try {
        localStorage.setItem(AUTO_CHECK_KEY, enabled ? "true" : "false");
      } catch {
        /* 存不下只影响下次启动的默认值 */
      }
      // 停用 = 立刻停止静默检查：清定时器，并让在飞的静默检查回调失效。
      if (!enabled) this.stopAutoCheck();
    },
    /** 开始一个有效操作；已有有效操作时返回 null（单飞）。 */
    _beginOperation(kind: UpdateOperationKind): number | null {
      if (this._activeOperation) return null;
      const seq = (this._operationSeq += 1);
      this._activeOperation = {
        seq,
        kind,
        phaseBefore: this.phase,
        messageBefore: this.message,
        errorKindBefore: this.errorKind,
      };
      return seq;
    },
    _isOperationActive(seq: number): boolean {
      return this._activeOperation?.seq === seq;
    },
    _finishOperation(seq: number) {
      if (this._activeOperation?.seq === seq) this._activeOperation = null;
    },
    /** 停用静默检查：让在飞的那次自动检查失效并恢复到操作前的展示状态。 */
    _cancelAutoCheckOperation() {
      const active = this._activeOperation;
      if (!active || active.kind !== "auto-check") return;
      this._operationSeq += 1; // 旧回调的序号立刻作废
      this._activeOperation = null;
      this.phase = active.phaseBefore;
      this.message = active.messageBefore;
      this.errorKind = active.errorKindBefore;
    },
    /**
     * 只读当前版本（本地调用，不联网、不改状态机）。
     *
     * 设置页挂载时用它把版本号显示出来；真正联网的检查只由「用户点击」或启动后的
     * autoCheck 触发 —— 打开设置页不该产生一次网络请求。
     */
    async loadCurrentVersion(): Promise<void> {
      if (this.currentVersion) return;
      try {
        const api = await this._apiOrLoad();
        this.currentVersion = await api.currentVersion();
      } catch {
        /* 拿不到版本号只影响显示；不把卡片推进 failed */
      }
    },
    /** 用户点击「检查更新」或启动后的静默检查都走这里（kind 只影响被停用时的处置）。 */
    async check(kind: "check" | "auto-check" = "check"): Promise<void> {
      const seq = this._beginOperation(kind);
      if (seq === null) return; // 单一有效操作：检查/下载期间不再起第二个
      this.phase = "checking";
      this.message = "";
      try {
        const api = await this._apiOrLoad();
        if (!this._isOperationActive(seq)) return;
        if (!this.currentVersion) {
          const version = await api.currentVersion();
          if (!this._isOperationActive(seq)) return;
          this.currentVersion = version;
        }
        const update = await api.check();
        if (!this._isOperationActive(seq)) return;
        this.lastCheckedAt = new Date().toISOString();
        if (!update || compareVersions(update.version, this.currentVersion) <= 0) {
          this.phase = "up-to-date";
          this.availableVersion = "";
          this.notes = "";
          return;
        }
        this.phase = "available";
        this.availableVersion = update.version;
        this.notes = update.notes ?? "";
      } catch (error) {
        if (!this._isOperationActive(seq)) return;
        const described = describeUpdateError(error);
        this.phase = "failed";
        this.errorKind = described.kind;
        this.message = described.message;
      } finally {
        this._finishOperation(seq);
      }
    },
    /** 只有用户点击「下载并安装」才会调用这里。 */
    async download(): Promise<void> {
      if (this.phase !== "available") return;
      const seq = this._beginOperation("install");
      if (seq === null) return;
      this.phase = "downloading";
      this.message = "";
      this.progressPercent = null;
      try {
        const api = await this._apiOrLoad();
        if (!this._isOperationActive(seq)) return;
        await api.downloadAndInstall(
          (progress) => {
            // 迟到的进度回调（失败之后 / 被新操作取代之后）不得改状态
            if (!this._isOperationActive(seq) || this.phase !== "downloading") return;
            this.downloaded = progress.downloaded;
            this.total = progress.total;
            this.progressPercent = progress.percent;
          },
          // 界面显示的是这个版本：安装的必须是同一个，不能无声换成另一个版本
          { expectedVersion: this.availableVersion },
        );
        if (!this._isOperationActive(seq)) return;
        this.phase = "ready";
        this.progressPercent = 100;
      } catch (error) {
        if (!this._isOperationActive(seq)) return;
        const described = describeUpdateError(error);
        this.phase = "failed";
        this.errorKind = described.kind;
        this.message = described.message;
      } finally {
        this._finishOperation(seq);
      }
    },
    async restart(): Promise<void> {
      if (this.phase !== "ready") return;
      const api = await this._apiOrLoad();
      await api.relaunch();
    },
    /** 启动后的静默检查：10 秒首检 + 24 小时周期；用户关掉开关就不再请求更新源。 */
    startAutoCheck() {
      // 只在 Tauri 壳里启动：浏览器里跑开发预览 / 单测时没有更新插件，
      // 静默检查必然失败，那只会制造噪音。
      if (typeof window === "undefined" || !("__TAURI_INTERNALS__" in window)) return;
      this.loadAutoCheckPreference();
      if (!this.autoCheck) return;
      // 重复启动（例如设置页重新挂载）不叠加定时器
      if (this._firstCheckTimer || this._intervalTimer) return;
      this._firstCheckTimer = setTimeout(() => {
        this._firstCheckTimer = null;
        if (this.autoCheck) void this.check("auto-check");
      }, FIRST_CHECK_DELAY_MS);
      this._intervalTimer = setInterval(() => {
        if (this.autoCheck) void this.check("auto-check");
      }, CHECK_INTERVAL_MS);
    },
    stopAutoCheck() {
      if (this._firstCheckTimer) clearTimeout(this._firstCheckTimer);
      if (this._intervalTimer) clearInterval(this._intervalTimer);
      this._firstCheckTimer = null;
      this._intervalTimer = null;
      this._cancelAutoCheckOperation();
    },
  },
});
