/**
 * 旋转 → 话题流的节流器。
 *
 * 星球不是老虎机：持续出现新话题不能变成快速随机刷新，展示变化要和用户
 * 真实的拖动量对应（spec 第 49~50 条）。所以这里做两件事：

 * 1. 把相机的方位角增量累计起来，够一步才推动一格展示流；
 * 2. 程序性移动（打开/关闭/聚焦的相机补间）不参与流动，也不留残量，
 *    否则补间一结束就会突然涌出一串新话题。
 */

export interface BrowseFlowOptions {
  /** 推动一格展示流需要的方位角（弧度）。0.4 rad ≈ 23°，约一个槽位间距。 */
  stepRad?: number;
  /** 一次最多允许的残量（防止甩动后连续多格）。 */
  maxBacklogSteps?: number;
}

export class BrowseFlowDriver {
  private readonly stepRad: number;
  private readonly maxBacklog: number;
  private pending = 0;

  constructor(options: BrowseFlowOptions = {}) {
    this.stepRad = Math.max(0.01, options.stepRad ?? 0.4);
    // 默认最多攒一格：甩动只推动一格，真正的节奏由浏览会话的 minLifetimeMs 兜底。
    this.maxBacklog = Math.max(1, options.maxBacklogSteps ?? 1) * this.stepRad;
  }

  /**
   * 喂入一次方位角增量。
   *
   * @param azimuthDelta 相对上一帧的方位角变化（弧度）
   * @param nowMs 当前时间（毫秒）
   * @param interacting 用户此刻是否在直接操作镜头（拖动/滚轮/惯性）
   * @returns 这次应该流动的步数：-1 反向、0 不动、1 前进
   */
  feed(azimuthDelta: number, nowMs: number, interacting: boolean): number {
    void nowMs;
    if (!interacting) {
      // 程序性移动：清空残量，避免补间结束后突然涌出一批新话题
      this.pending = 0;
      return 0;
    }
    this.pending += azimuthDelta;
    if (Math.abs(this.pending) > this.maxBacklog) {
      this.pending = Math.sign(this.pending) * this.maxBacklog;
    }
    if (Math.abs(this.pending) < this.stepRad) return 0;
    const step = this.pending > 0 ? 1 : -1;
    this.pending -= step * this.stepRad;
    return step;
  }

  reset(): void {
    this.pending = 0;
  }
}
