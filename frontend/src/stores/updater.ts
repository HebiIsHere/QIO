/**
 * 更新状态机（spec 2026-09-22-updater-design §3）。
 *
 * 规则：只能静默**检查**；下载与安装必须由用户点击触发（`autoCheck` 只影响检查）。
 * 「已是最新」只允许出现在 `up-to-date` —— 检查失败必须落在 `failed`。
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
    _timer: null as ReturnType<typeof setInterval> | null,
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
    /** 用户点击「检查更新」或启动后的静默检查都走这里。 */
    async check(): Promise<void> {
      if (this.busy) return;
      this.phase = "checking";
      this.message = "";
      try {
        const api = await this._apiOrLoad();
        if (!this.currentVersion) this.currentVersion = await api.currentVersion();
        const update = await api.check();
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
        const { kind, message } = describeUpdateError(error);
        this.phase = "failed";
        this.errorKind = kind;
        this.message = message;
      }
    },
    /** 只有用户点击「下载并安装」才会调用这里。 */
    async download(): Promise<void> {
      if (this.phase !== "available") return;
      this.phase = "downloading";
      this.message = "";
      this.progressPercent = null;
      try {
        const api = await this._apiOrLoad();
        await api.downloadAndInstall((progress) => {
          this.downloaded = progress.downloaded;
          this.total = progress.total;
          this.progressPercent = progress.percent;
        });
        this.phase = "ready";
        this.progressPercent = 100;
      } catch (error) {
        const { kind, message } = describeUpdateError(error);
        this.phase = "failed";
        this.errorKind = kind;
        this.message = message;
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
      if (this._timer) return;
      setTimeout(() => {
        if (this.autoCheck) void this.check();
      }, FIRST_CHECK_DELAY_MS);
      this._timer = setInterval(() => {
        if (this.autoCheck) void this.check();
      }, CHECK_INTERVAL_MS);
    },
    stopAutoCheck() {
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
    },
  },
});
