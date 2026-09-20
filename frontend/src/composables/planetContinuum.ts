/**
 * Planet 连续体：入口小球 ↔ 全屏 Planet 的共享状态（第四阶段）。
 *
 * 为什么需要它：入口小球在 `ConversationView` 里（`PlanetDock`），全屏 Planet 是
 * `PlanetView`。两者若各自跑计时器、各自淡入淡出，用户看到的就是「小球淡出 + 大球淡入」
 * 这种伪连续。真正连续只有一个办法：让它们共享同一份「现在是哪一个阶段」。
 *
 * 阶段（进入）：activating(A) → expanding(B) → ready(C)
 * 阶段（退出）：collapsing(交互层降密度 + 回到抽象态) → returning(体量收缩回入口) → idle
 *
 * 入口几何在**打开那一刻**读取（起点）与在**关闭那一刻重新读取**（终点）：
 * 悬浮球可以被拖到任意位置、贴边后还会半隐藏，所以终点不能沿用打开时的坐标。
 */
import { reactive } from "vue";

export type PlanetPhase =
  | "idle"
  | "activating"
  | "expanding"
  | "ready"
  | "collapsing"
  | "returning";

/** 入口小球在视口里的几何（CSS 像素）。r 用来对齐球体尺度 */
export interface EntryOrigin {
  cx: number;
  cy: number;
  r: number;
}

export const planetContinuum = reactive({
  phase: "idle" as PlanetPhase,
  /**
   * 本次打开/关闭的代号。收起动画期间用户再次打开时，迟到的那次收尾不能改回状态
   * （与 PlanetView 里既有的 `seq` 是同一类防护，这里管的是跨组件的阶段状态）。
   */
  epoch: 0,
  /** 打开时的入口几何：进入动画的起点 */
  origin: null as EntryOrigin | null,
  /** 关闭时的入口几何：收缩动画的终点（关闭那一刻重新读） */
  target: null as EntryOrigin | null,
  /**
   * 入口小球是否已经由**真实星球场景**渲染。
   *
   * 为什么要有这个标记：小球现在是同一个 WebGL 场景缩到入口尺度渲染的（同一个对象，
   * 连渲染器都是同一个）。但 three.js 是懒加载的 —— 在它准备好之前，入口先显示
   * `PlanetOrb`（2D 压缩态）顶着首屏，准备好之后再把位置让给真实渲染。
   * 这个标记就是「什么时候把位置让出去」，两边都读它，不靠各自的计时器猜。
   */
  ballLive: false,
});

/** 入口元素：由 PlanetDock 挂上 `data-planet-entry` */
export const ENTRY_SELECTOR = "[data-planet-entry]";

export function readEntryOrigin(): EntryOrigin | null {
  if (typeof document === "undefined") return null;
  const el = document.querySelector(ENTRY_SELECTOR) as HTMLElement | null;
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    cx: rect.left + rect.width / 2,
    cy: rect.top + rect.height / 2,
    r: Math.max(4, Math.min(rect.width, rect.height) / 2),
  };
}

/** 进入：开始一次新的连续体（返回这次的代号） */
export function beginOpen(): number {
  planetContinuum.epoch += 1;
  planetContinuum.origin = readEntryOrigin();
  planetContinuum.target = null;
  planetContinuum.phase = "activating";
  return planetContinuum.epoch;
}

/** 退出：终点在关闭那一刻重新读取（悬浮球可能已经被拖走/贴边） */
export function beginClose(): number {
  planetContinuum.epoch += 1;
  planetContinuum.target = readEntryOrigin();
  planetContinuum.phase = "collapsing";
  return planetContinuum.epoch;
}

export function isCurrent(epoch: number): boolean {
  return planetContinuum.epoch === epoch;
}

/** 推进阶段：过期的代号一律忽略，避免迟到回调把新一次打开的状态改回去 */
export function advanceTo(epoch: number, phase: PlanetPhase): boolean {
  if (!isCurrent(epoch)) return false;
  planetContinuum.phase = phase;
  return true;
}

/** 回到常态（小球就位、Planet 卸载） */
export function resetContinuum(epoch?: number): boolean {
  if (epoch !== undefined && !isCurrent(epoch)) return false;
  planetContinuum.phase = "idle";
  planetContinuum.origin = null;
  planetContinuum.target = null;
  return true;
}

/** 星球是否已经可以被操作（只有 C 阶段才是「场景接管」） */
export function isInteractive(): boolean {
  return planetContinuum.phase === "ready";
}
