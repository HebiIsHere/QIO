# 逐帧曲线（自动生成，数据来自 frames/*.json 与 probe/*.json）

横轴时间（ms），纵轴 `--mp`（0..1）。**灰色竖条 = 长帧（dt > 60ms）**，玫红虚线 = 目标进度，细竖线 = 反向触发时刻。
本机 headless WebGL 走 SwiftShader，帧间隔是**双模态**（约 7ms 与 100–180ms 交替），所以：
**动画质量用「几何不变式 + 归一化步进 + 长帧单列」判断，不看原始 |Δmp|**。

## rev · 1440x970

### expand

<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180" viewBox="0 0 720 180"><rect width="100%" height="100%" fill="#fbfaf9" /><rect x="122.0" y="26" width="39.4" height="128" fill="#000" fill-opacity="0.08" /><rect x="255.4" y="26" width="63.6" height="128" fill="#000" fill-opacity="0.08" /><rect x="319.0" y="26" width="99.7" height="128" fill="#000" fill-opacity="0.08" /><rect x="418.8" y="26" width="69.1" height="128" fill="#000" fill-opacity="0.08" /><rect x="487.9" y="26" width="82.3" height="128" fill="#000" fill-opacity="0.08" /><rect x="570.3" y="26" width="56.6" height="128" fill="#000" fill-opacity="0.08" /><rect x="626.9" y="26" width="67.1" height="128" fill="#000" fill-opacity="0.08" /><line x1="26" y1="26.0" x2="694" y2="26.0" stroke="#c51b7d" stroke-dasharray="4 3" stroke-width="1" /><polyline points="26.6,154.0 30.5,154.0 32.7,154.0 38.2,154.0 43.2,154.0 52.9,154.0 54.3,154.0 59.8,154.0 66.0,154.0 69.4,154.0 76.2,154.0 77.1,154.0 89.3,154.0 92.8,154.0 106.0,154.0 109.5,154.0 110.7,154.0 115.0,154.0 118.5,154.0 122.0,154.0 161.4,154.0 167.2,154.0 174.6,154.0 193.8,154.0 202.7,154.0 225.0,154.0 243.8,154.0 252.5,131.0 255.4,117.4 319.0,110.9 418.8,43.2 487.9,26.4 570.3,26.0 626.9,26.0 694.0,26.0" fill="none" stroke="#c51b7d" stroke-width="2" /><text x="26" y="174" font-size="11" fill="#666">0ms</text><text x="650" y="174" font-size="11" fill="#666">1236ms</text><text x="4" y="30" font-size="11" fill="#666">1.0</text><text x="4" y="158" font-size="11" fill="#666">0.0</text></svg>

- 帧数 35，时长 1234ms，fps≈27.5
- 归一化基准 520ms（令牌动画时长），长帧 7 个
- 球心 x：720 → 0；选中标签 x：732 → 363
- **teleports（归一化步进 > 1.6）：2 处**：t=419ms dt=16ms step=0.18 norm=5.85；t=424ms dt=5.4ms step=0.106 norm=10.207

### collapse

<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180" viewBox="0 0 720 180"><rect width="100%" height="100%" fill="#fbfaf9" /><rect x="208.3" y="26" width="37.5" height="128" fill="#000" fill-opacity="0.08" /><rect x="252.1" y="26" width="132.6" height="128" fill="#000" fill-opacity="0.08" /><rect x="397.9" y="26" width="35.2" height="128" fill="#000" fill-opacity="0.08" /><rect x="440.7" y="26" width="53.0" height="128" fill="#000" fill-opacity="0.08" /><rect x="493.7" y="26" width="43.3" height="128" fill="#000" fill-opacity="0.08" /><rect x="537.0" y="26" width="43.6" height="128" fill="#000" fill-opacity="0.08" /><rect x="580.5" y="26" width="45.9" height="128" fill="#000" fill-opacity="0.08" /><rect x="626.4" y="26" width="34.1" height="128" fill="#000" fill-opacity="0.08" /><rect x="660.5" y="26" width="33.3" height="128" fill="#000" fill-opacity="0.08" /><line x1="26" y1="154.0" x2="694" y2="154.0" stroke="#c51b7d" stroke-dasharray="4 3" stroke-width="1" /><polyline points="26.1,26.0 29.2,26.0 31.3,26.0 40.7,26.0 41.0,26.0 43.7,26.0 52.3,26.0 55.0,26.0 55.6,26.0 58.2,26.0 63.2,26.0 67.4,26.0 67.6,26.0 74.6,26.0 78.5,26.0 78.8,26.0 85.2,26.0 89.8,26.0 94.6,26.0 100.5,26.0 105.1,26.0 109.5,26.0 118.8,26.0 123.4,26.0 132.5,26.0 144.1,26.0 152.6,26.0 157.3,26.0 170.0,26.0 181.6,26.0 193.5,26.0 206.1,27.7 208.3,30.5 245.8,35.1 252.1,102.2 384.6,115.5 397.9,154.0 433.1,154.0 440.7,154.0 493.7,154.0 537.0,154.0 580.5,154.0 626.4,154.0 660.5,154.0 693.8,154.0 694.0,154.0" fill="none" stroke="#c51b7d" stroke-width="2" /><text x="26" y="174" font-size="11" fill="#666">0ms</text><text x="650" y="174" font-size="11" fill="#666">1918ms</text><text x="4" y="30" font-size="11" fill="#666">1.0</text><text x="4" y="158" font-size="11" fill="#666">0.0</text></svg>

- 帧数 46，时长 1918ms，fps≈23.5
- 归一化基准 420ms（令牌动画时长），长帧 9 个
- 球心 x：0 → 720；选中标签 x：363 → 730
- **teleports（归一化步进 > 1.6）：2 处**：t=649ms dt=18ms step=-0.524 norm=-12.227；t=1068ms dt=38.1ms step=-0.301 norm=-3.318

### reverse

<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180" viewBox="0 0 720 180"><rect width="100%" height="100%" fill="#fbfaf9" /><rect x="141.2" y="26" width="22.2" height="128" fill="#000" fill-opacity="0.08" /><rect x="163.4" y="26" width="46.0" height="128" fill="#000" fill-opacity="0.08" /><rect x="209.4" y="26" width="45.8" height="128" fill="#000" fill-opacity="0.08" /><rect x="255.3" y="26" width="34.7" height="128" fill="#000" fill-opacity="0.08" /><rect x="290.0" y="26" width="27.6" height="128" fill="#000" fill-opacity="0.08" /><rect x="317.6" y="26" width="27.5" height="128" fill="#000" fill-opacity="0.08" /><rect x="345.1" y="26" width="29.1" height="128" fill="#000" fill-opacity="0.08" /><rect x="374.2" y="26" width="51.5" height="128" fill="#000" fill-opacity="0.08" /><rect x="425.7" y="26" width="34.9" height="128" fill="#000" fill-opacity="0.08" /><rect x="460.6" y="26" width="29.4" height="128" fill="#000" fill-opacity="0.08" /><rect x="490.0" y="26" width="64.0" height="128" fill="#000" fill-opacity="0.08" /><rect x="554.0" y="26" width="46.2" height="128" fill="#000" fill-opacity="0.08" /><rect x="600.8" y="26" width="64.2" height="128" fill="#000" fill-opacity="0.08" /><line x1="26" y1="154.0" x2="694" y2="154.0" stroke="#c51b7d" stroke-dasharray="4 3" stroke-width="1" /><line x1="209.4" y1="26" x2="209.4" y2="154" stroke="#b0136a" stroke-width="1" stroke-opacity="0.6" /><polyline points="26.1,154.0 30.3,154.0 32.1,154.0 33.9,154.0 36.0,154.0 39.4,154.0 39.5,154.0 40.6,154.0 42.5,154.0 44.5,154.0 46.0,154.0 47.6,154.0 49.2,154.0 51.2,154.0 52.9,154.0 55.2,154.0 56.3,154.0 57.9,154.0 60.0,154.0 61.4,154.0 63.3,154.0 65.1,154.0 66.8,154.0 68.3,154.0 70.1,154.0 71.9,154.0 73.4,154.0 75.4,154.0 77.0,154.0 78.9,154.0 83.0,154.0 83.1,154.0 87.0,154.0 87.3,154.0 89.3,154.0 91.1,154.0 92.6,154.0 96.1,154.0 97.7,154.0 107.8,154.0 107.9,154.0 107.9,154.0 109.8,154.0 112.1,154.0 113.2,154.0 117.4,154.0 117.5,154.0 118.7,154.0 120.1,154.0 122.0,154.0 123.8,154.0 125.3,154.0 127.2,154.0 129.0,154.0 131.9,154.0 132.2,154.0 136.7,154.0 137.7,148.8 141.2,134.3 163.4,120.5 209.4,56.3 255.3,89.7 290.0,150.7 317.6,154.0 345.1,154.0 374.2,154.0 425.7,154.0 460.6,154.0 490.0,154.0 554.0,154.0 600.2,154.0 600.8,154.0 665.0,154.0 665.1,154.0 666.7,154.0 668.2,154.0 669.7,154.0 671.5,154.0 673.2,154.0 675.0,154.0 679.9,154.0 680.1,154.0 682.0,154.0 685.5,154.0 687.3,154.0 689.0,154.0 690.6,154.0 692.2,154.0 694.0,154.0" fill="none" stroke="#c51b7d" stroke-width="2" /><text x="26" y="174" font-size="11" fill="#666">0ms</text><text x="650" y="174" font-size="11" fill="#666">2684ms</text><text x="4" y="30" font-size="11" fill="#666">1.0</text><text x="4" y="158" font-size="11" fill="#666">0.0</text></svg>

- 帧数 89，时长 2684ms，fps≈32.8
- 归一化基准 520ms（令牌动画时长），长帧 13 个
- 球心 x：720 → 720；选中标签 x：730 → 730
- **teleports（归一化步进 > 1.6）：3 处**：t=449ms dt=4.3ms step=0.041 norm=4.958；t=463ms dt=14.1ms step=0.113 norm=4.167；t=1061ms dt=139.4ms step=-0.476 norm=-1.776
- 反向触发：t=737ms（此时 --mp=0.763，点的是 [data-mp-close]），过冲 0

## 减少动态效果（data-motion=reduced）

<svg xmlns="http://www.w3.org/2000/svg" width="720" height="180" viewBox="0 0 720 180"><rect width="100%" height="100%" fill="#fbfaf9" /><line x1="26" y1="26.0" x2="694" y2="26.0" stroke="#c51b7d" stroke-dasharray="4 3" stroke-width="1" /><polyline points="28.6,154.0 59.5,154.0 62.1,154.0 82.7,154.0 113.7,154.0 113.7,154.0 134.3,154.0 149.8,154.0 173.0,154.0 193.6,154.0 214.3,154.0 245.2,154.0 265.9,154.0 314.9,154.0 392.2,154.0 397.4,154.0 405.1,154.0 423.2,154.0 456.7,154.0 492.8,154.0 518.6,154.0 611.5,26.0 619.2,26.0 694.0,26.0" fill="none" stroke="#c51b7d" stroke-width="2" /><text x="26" y="174" font-size="11" fill="#666">0ms</text><text x="650" y="174" font-size="11" fill="#666">259ms</text><text x="4" y="30" font-size="11" fill="#666">1.0</text><text x="4" y="158" font-size="11" fill="#666">0.0</text></svg>

- 减少动画下 0 与 1 之间没有任何中间帧（真正一帧到位）；点击派发到生效约 3ms
- 0..1 之间的中间帧数量：0；点击派发到生效：3ms

## 渲染收尾（静置 2.5s）

- 渲染计数 440 → 440（Δ0），CDP Frames Δ0，loopActive=false
- 判定：静置 2.5s 渲染计数不再增长（停了）

