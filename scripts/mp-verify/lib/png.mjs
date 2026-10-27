/**
 * 极简 PNG 编解码与像素统计（只依赖 node:zlib，无第三方依赖）。
 *
 * 为什么自己写：验收脚本要能独立运行在任意 worktree 上，不能依赖 Playwright 之类的
 * 全局缓存路径（基线里的 planet-frames.mjs 就把 Playwright 的绝对路径写死了）。
 * CDP 截图给的是 PNG 字节，比对与测量都得先把像素解出来。
 */
import { deflateSync, inflateSync } from "node:zlib";

/** PNG 解码 → { width, height, data }（data 为 RGBA，每像素 4 字节）。 */
export function decodePng(buffer) {
  if (buffer.readUInt32BE(0) !== 0x89504e47) throw new Error("不是 PNG");
  let offset = 8;
  let width = 0;
  let height = 0;
  let colorType = 6;
  let bitDepth = 8;
  const idat = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString("ascii", offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      if (data[12] !== 0) throw new Error("不支持隔行 PNG");
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
    offset += 12 + length;
  }
  if (bitDepth !== 8) throw new Error("只支持 8 位 PNG，收到 " + bitDepth);
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 0 ? 1 : -1;
  if (channels < 0) throw new Error("不支持的 PNG 颜色类型 " + colorType);
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const out = Buffer.alloc(width * height * 4, 255);
  let prev = Buffer.alloc(stride, 0);
  let pos = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = raw[pos];
    pos += 1;
    const line = Buffer.from(raw.subarray(pos, pos + stride));
    pos += stride;
    for (let i = 0; i < stride; i += 1) {
      const a = i >= channels ? line[i - channels] : 0;
      const b = prev[i];
      const c = i >= channels ? prev[i - channels] : 0;
      let value = line[i];
      if (filter === 1) value += a;
      else if (filter === 2) value += b;
      else if (filter === 3) value += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      line[i] = value & 0xff;
    }
    for (let x = 0; x < width; x += 1) {
      const src = x * channels;
      const dst = (y * width + x) * 4;
      out[dst] = line[src];
      out[dst + 1] = channels === 1 ? line[src] : line[src + 1];
      out[dst + 2] = channels === 1 ? line[src] : line[src + 2];
      out[dst + 3] = channels === 4 ? line[src + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buffer) {
  let c = 0xffffffff;
  for (let i = 0; i < buffer.length; i += 1) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, crc]);
}

/** PNG 编码（8 位 RGBA，无过滤）：用于写出差异热图，人眼可核。 */
export function encodePng({ width, height, data }) {
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (stride + 1)] = 0;
    data.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 6 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** 裁一块区域（越界自动夹取）。 */
export function crop(image, rect) {
  const x0 = Math.max(0, Math.round(rect.x));
  const y0 = Math.max(0, Math.round(rect.y));
  const x1 = Math.min(image.width, Math.round(rect.x + rect.width));
  const y1 = Math.min(image.height, Math.round(rect.y + rect.height));
  const width = Math.max(1, x1 - x0);
  const height = Math.max(1, y1 - y0);
  const data = Buffer.alloc(width * height * 4, 255);
  for (let y = 0; y < height; y += 1) {
    image.data.copy(
      data,
      y * width * 4,
      ((y0 + y) * image.width + x0) * 4,
      ((y0 + y) * image.width + x0 + width) * 4,
    );
  }
  return { width, height, data };
}

export function pixelAt(image, x, y) {
  const px = Math.max(0, Math.min(image.width - 1, Math.round(x)));
  const py = Math.max(0, Math.min(image.height - 1, Math.round(y)));
  const i = (py * image.width + px) * 4;
  return [image.data[i], image.data[i + 1], image.data[i + 2], image.data[i + 3]];
}

/**
 * 两张同尺寸图的像素差异统计。
 * tolerance：单通道差值 ≤ tolerance 视为未变化（抗锯齿/编码噪声）。
 */
export function diffStats(a, b, { tolerance = 2, limit = 240 } = {}) {
  if (a.width !== b.width || a.height !== b.height) {
    throw new Error("尺寸不同，无法逐像素比对：" + a.width + "x" + a.height + " vs " + b.width + "x" + b.height);
  }
  let changed = 0;
  let maxDelta = 0;
  let sumDelta = 0;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let over = 0;
  const channels = [];
  for (let y = 0; y < a.height; y += 1) {
    for (let x = 0; x < a.width; x += 1) {
      const i = (y * a.width + x) * 4;
      let d = 0;
      for (let c = 0; c < 3; c += 1) {
        const delta = Math.abs(a.data[i + c] - b.data[i + c]);
        if (delta > d) d = delta;
        channels.push(delta);
      }
      if (d > tolerance) {
        changed += 1;
        if (x < minX) minX = x;
        if (y < minY) minY = y;
        if (x > maxX) maxX = x;
        if (y > maxY) maxY = y;
      }
      if (d > limit) over += 1;
      if (d > maxDelta) maxDelta = d;
      sumDelta += d;
    }
  }
  const total = a.width * a.height;
  channels.sort((m, n) => m - n);
  const p = (q) => channels[Math.min(channels.length - 1, Math.floor(q * channels.length))] ?? 0;
  return {
    width: a.width,
    height: a.height,
    totalPixels: total,
    changedPixels: changed,
    changedRatio: Number((changed / total).toFixed(6)),
    diffRatio: Number((changed / total).toFixed(6)),
    maxDelta,
    meanDelta: Number((sumDelta / total).toFixed(4)),
    overLimitPixels: over,
    percentile: { p50: p(0.5), p90: p(0.9), p99: p(0.99), p999: p(0.999) },
    changedBox: changed ? { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 } : null,
  };
}

/** 差异热图：未变化处压暗，变化处按幅度偏红（给人看的那张图）。 */
export function diffImage(a, b, { tolerance = 2 } = {}) {
  const data = Buffer.alloc(a.width * a.height * 4, 255);
  for (let y = 0; y < a.height; y += 1) {
    for (let x = 0; x < a.width; x += 1) {
      const i = (y * a.width + x) * 4;
      const dl = Math.abs(a.data[i] - b.data[i]);
      const dg = Math.abs(a.data[i + 1] - b.data[i + 1]);
      const db = Math.abs(a.data[i + 2] - b.data[i + 2]);
      const d = Math.max(dl, dg, db);
      if (d <= tolerance) {
        // 未变化：保留 18% 灰度底（能看出比对的是哪一帧）
        const g = Math.round((a.data[i] * 0.3 + a.data[i + 1] * 0.6 + a.data[i + 2] * 0.1) * 0.18);
        data[i] = g;
        data[i + 1] = g;
        data[i + 2] = g;
      } else {
        const k = Math.min(1, d / 64);
        data[i] = 255;
        data[i + 1] = Math.round(255 * (1 - k));
        data[i + 2] = Math.round(255 * (1 - k));
      }
      data[i + 3] = 255;
    }
  }
  return { width: a.width, height: a.height, data };
}

/**
 * 「粉色程度」：记忆星球的轮廓 / 融合环 / 话题点 / 经纬线全部是同一族玫红
 * （暗色主题 0xe878bd，亮色主题 0xb0136a）。用 r-g 与 b-g 的联合阈值取它，
 * 与背景（--planet-void 的暗紫渐变）区分开。
 */
export function pinkScore(image, x, y) {
  const [r, g, b] = pixelAt(image, x, y);
  const rg = r - g;
  const bg = b - g;
  if (rg <= 0) return 0;
  return Math.min(1, rg / 90) * Math.min(1, Math.max(0, bg) / 40);
}

/**
 * 用像素测量球体轮廓（圆拟合）：整幅图**最外圈**的连续玫红细线就是轮廓环
 * （`contour.renderOrder = 4`，opacity 0.8，随相机朝向），它是唯一完整的圆。
 *
 * 关键几何（必读，否则会把「环凸出一点」当成直径变化）：
 * 轮廓环是画在球心平面上、半径 = 球半径 R 的圆，它投影出来的半径是 atan(R/d)，
 * 而球体真正的剪影是 asin(R/d) —— 轮廓环比剪影**略小**（1440×900 时约小 7%）。
 * 融合环画在 1.006R 的球面上，靠近剪影处可能比轮廓环更靠外一点点。
 * 所以不能拿「最外一个玫红像素」当直径：要拟合**那个圆**本身。
 *
 * 做法：
 * 1. 用玫红得分的质心当球心初值；
 * 2. 从初值向 N 个方向径向扫描，取该方向**最外侧**的玫红像素作为圆周候选点；
 * 3. 用中位数剔掉明显偏离的点（被融合环/话题点顶出去的那些），
 * 4. 对剩下的点做最小二乘圆拟合（Kasa 代数拟合）→ 圆心 + 半径。
 *
 * 被视口/布局裁掉的部分自然没有候选点，只要剩下的点覆盖足够宽的弧段就成立。
 */
export function measureContour(image, opts = {}) {
  const threshold = opts.threshold ?? 0.32;
  const angles = opts.angles ?? 720;
  const { width, height } = image;
  let sumW = 0;
  let sumX = 0;
  let sumY = 0;
  for (let y = 0; y < height; y += 2) {
    for (let x = 0; x < width; x += 2) {
      const s = pinkScore(image, x, y);
      if (s <= 0.02) continue;
      const w = s * s;
      sumW += w;
      sumX += w * x;
      sumY += w * y;
    }
  }
  if (sumW <= 0) return { ok: false, reason: "画面里没有玫红结构（WebGL 未渲染？）" };
  const cx0 = sumX / sumW;
  const cy0 = sumY / sumW;
  const maxR = Math.max(width, height);

  /**
   * 沿一个方向找「最外侧的**细**玫红峰」。
   *
   * 为什么必须挑「细」的：球面上还有融合环（内 10 / 中 6.5 / 外 4 px 全宽）和话题点，
   * 它们在靠近剪影的位置会伸到轮廓环外面去 —— 直接取「最外一个玫红像素」会被它们带偏。
   * 轮廓环是一条 1px 细线（半高宽 ≤ 3px），用「径向半高宽」就能把它和环区分开。
   */
  const edgePoint = (theta) => {
    const cos = Math.cos(theta);
    const sin = Math.sin(theta);
    const samples = [];
    for (let r = maxR * 0.05; r <= maxR * 1.2; r += 0.25) {
      const px = cx0 + cos * r;
      const py = cy0 + sin * r;
      if (px < 0 || py < 0 || px >= width - 1 || py >= height - 1) break;
      samples.push({ r, s: pinkScore(image, px, py) });
    }
    for (let i = samples.length - 1; i >= 1; i -= 1) {
      if (samples[i].s < threshold) continue;
      // 找这个峰的左右边界（< 半高即断开）
      const peak = samples[i].s;
      let lo = i;
      let hi = i;
      while (lo > 0 && samples[lo - 1].s >= peak * 0.5) lo -= 1;
      while (hi < samples.length - 1 && samples[hi + 1].s >= peak * 0.5) hi += 1;
      const fwhm = samples[hi].r - samples[lo].r;
      if (fwhm <= 3.5) {
        let w = 0;
        let wr = 0;
        for (let k = lo; k <= hi; k += 1) {
          w += samples[k].s;
          wr += samples[k].s * samples[k].r;
        }
        return w > 0 ? wr / w : samples[i].r;
      }
      i = lo; // 这个峰太宽（是融合环/话题点）：跳过整段，继续往外找
    }
    return null;
  };

  const rough = [];
  for (let a = 0; a < angles; a += 1) {
    const theta = (a / angles) * Math.PI * 2;
    const r = edgePoint(theta);
    if (r !== null) rough.push({ theta, r });
  }
  if (rough.length < 60) return { ok: false, reason: "可用径向采样太少：" + rough.length };
  const radii = rough.map((item) => item.r).sort((m, n) => m - n);
  const median = radii[Math.floor(radii.length / 2)];
  // 剔掉被融合环/话题点顶出去的点（> 中位数 8% 以上），再剔掉明显在内部的结构
  const kept = rough.filter((item) => item.r >= median * 0.75 && item.r <= median * 1.08);
  if (kept.length < 40) return { ok: false, reason: "内点太少：" + kept.length };
  const mean = kept.reduce((sum, item) => sum + item.r, 0) / kept.length;
  const points = kept.map((item) => ({
    x: cx0 + Math.cos(item.theta) * mean,
    y: cy0 + Math.sin(item.theta) * mean,
  }));

  // Kasa 最小二乘圆拟合：x² + y² + D x + E y + F = 0
  let sxx = 0;
  let sxy = 0;
  let syy = 0;
  let sx = 0;
  let sy = 0;
  let sxz = 0;
  let syz = 0;
  let sz = 0;
  const n = points.length;
  for (const point of points) {
    const z = point.x * point.x + point.y * point.y;
    sxx += point.x * point.x;
    sxy += point.x * point.y;
    syy += point.y * point.y;
    sx += point.x;
    sy += point.y;
    sxz += point.x * z;
    syz += point.y * z;
    sz += z;
  }
  // 解 3x3 线性方程（克拉默法则）
  const a11 = sxx;
  const a12 = sxy;
  const a13 = sx;
  const a21 = sxy;
  const a22 = syy;
  const a23 = sy;
  const a31 = sx;
  const a32 = sy;
  const a33 = n;
  const b1 = -sxz;
  const b2 = -syz;
  const b3 = -sz;
  const det =
    a11 * (a22 * a33 - a23 * a32) - a12 * (a21 * a33 - a23 * a31) + a13 * (a21 * a32 - a22 * a31);
  if (Math.abs(det) < 1e-9) return { ok: false, reason: "圆拟合退化（点共线）" };
  const dx =
    (b1 * (a22 * a33 - a23 * a32) - a12 * (b2 * a33 - a23 * b3) + a13 * (b2 * a32 - a22 * b3)) / det;
  const dy =
    (a11 * (b2 * a33 - a23 * b3) - b1 * (a21 * a33 - a23 * a31) + a13 * (a21 * b3 - b2 * a31)) / det;
  const df =
    (a11 * (a22 * b3 - b2 * a32) - a12 * (a21 * b3 - b2 * a31) + b1 * (a21 * a32 - a22 * a31)) / det;
  const cx = -dx / 2;
  const cy = -dy / 2;
  const radius = Math.sqrt(Math.max(0, cx * cx + cy * cy - df));
  if (!Number.isFinite(radius) || radius <= 1) return { ok: false, reason: "拟合半径不合理：" + radius };
  // 残差（像素）：拟合得好的话应当 < 1px
  let residual = 0;
  for (const point of points) {
    residual += Math.abs(Math.hypot(point.x - cx, point.y - cy) - radius);
  }
  residual /= points.length;
  const arcSpanDeg = ((kept.length / angles) * 360).toFixed(1);
  return {
    ok: true,
    cx: Number(cx.toFixed(2)),
    cy: Number(cy.toFixed(2)),
    radius: Number(radius.toFixed(2)),
    diameter: Number((radius * 2).toFixed(2)),
    residualPx: Number(residual.toFixed(3)),
    arcSpanDeg: Number(arcSpanDeg),
    points: kept.length,
    /** 竖直/水平方向的极值（球体在视口里被裁掉时用它核对） */
    extremes: {
      left: Number((cx - radius).toFixed(2)),
      right: Number((cx + radius).toFixed(2)),
      top: Number((cy - radius).toFixed(2)),
      bottom: Number((cy + radius).toFixed(2)),
    },
  };
}

/**
 * 横向扫描线上的「玫红笔画」宽度直方图。
 *
 * 用途：SDF 融合环是**恒定像素宽**描边（内 10 / 中 6.5 / 外 4 全宽）。
 * 一条水平扫描线横切一段近似竖直的圆弧时，命中长度 ≈ 该圈的全宽。
 * 全图统计后取直方图的峰，就是这三档宽度的像素证据（不是靠肉眼看）。
 */
export function strokeRunHistogram(image, opts = {}) {
  const threshold = opts.threshold ?? 0.3;
  const maxRun = opts.maxRun ?? 16;
  const bins = new Array(maxRun + 1).fill(0);
  const runs = [];
  for (let y = 0; y < image.height; y += 1) {
    let run = 0;
    for (let x = 0; x < image.width; x += 1) {
      if (pinkScore(image, x, y) >= threshold) {
        run += 1;
      } else if (run > 0) {
        if (run <= maxRun) {
          bins[run] += 1;
          runs.push(run);
        }
        run = 0;
      }
    }
    if (run > 0 && run <= maxRun) {
      bins[run] += 1;
      runs.push(run);
    }
  }
  // 取出现最多的三档
  const ranked = bins
    .map((count, width) => ({ width, count }))
    .filter((item) => item.width >= 2 && item.count > 0)
    .sort((a, b) => b.count - a.count)
    .slice(0, 4);
  return { histogram: bins, top: ranked, sampled: runs.length };
}
/**
 * 玫红结构的包围盒（用于「展开态只剩右半球」这类**被裁切**的画面）。
 *
 * 为什么另给一个：展开后球体左半被内容区裁掉，完整圆已经不存在，
 * 径向拟合没有意义；但**竖直方向没有被裁**，包围盒高度仍然是球体直径 ——
 * 这正好是「直径不变 ±1px」要量的那个数。
 */
export function measureSilhouette(image, opts = {}) {
  const threshold = opts.threshold ?? 0.3;
  const minX = opts.minX ?? 0;
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  let count = 0;
  for (let y = 0; y < image.height; y += 1) {
    for (let x = Math.max(0, Math.round(minX)); x < image.width; x += 1) {
      if (pinkScore(image, x, y) < threshold) continue;
      count += 1;
      if (x < left) left = x;
      if (x > right) right = x;
      if (y < top) top = y;
      if (y > bottom) bottom = y;
    }
  }
  if (count === 0) return { ok: false, reason: "画面里没有玫红结构" };
  return {
    ok: true,
    left,
    right,
    top,
    bottom,
    width: right - left + 1,
    height: bottom - top + 1,
    cx: Number(((left + right) / 2).toFixed(2)),
    cy: Number(((top + bottom) / 2).toFixed(2)),
    pixels: count,
  };
}

/** 某一行/列上玫红像素的起止（判断「左边缘之内还有没有球体」用）。 */
export function pinkExtentOnRow(image, y, threshold = 0.3) {
  let left = Infinity;
  let right = -Infinity;
  for (let x = 0; x < image.width; x += 1) {
    if (pinkScore(image, x, y) < threshold) continue;
    if (x < left) left = x;
    if (x > right) right = x;
  }
  return Number.isFinite(left) ? { left, right } : null;
}
/**
 * 沿「球心所在的那一行/几行」横向扫描，量球轮廓的最左/最右列。
 *
 * 为什么这样量最稳：球心所在行上，轮廓环几乎是**竖直**的，水平扫描线横切它
 * 得到 1–2px 的干净玫瑰色笔画；而这一行同时穿过球体最宽的地方 —— 左半径 + 右半径
 * 就是球体在这个高度上的直径。球心位置不需要拟合：它就在画布中心（DOM 已经告诉我们）。
 *
 * 展开态左半被裁掉时，左边界会落在裁剪边界上：这时 leftRadius 没有意义，
 * 但 rightRadius（右半球完整程度）仍然可靠 —— 正好是验收要的那个数。
 */
export function scanSphereRow(image, opts = {}) {
  const threshold = opts.threshold ?? 0.3;
  const centerX = opts.centerX;
  const rows = opts.rows ?? [];
  const results = [];
  for (const row of rows) {
    const y = Math.round(row);
    if (y < 0 || y >= image.height) continue;
    let leftEdge = null;
    for (let x = 0; x < image.width; x += 1) {
      if (pinkScore(image, x, y) >= threshold) {
        leftEdge = x;
        break;
      }
    }
    let rightEdge = null;
    for (let x = image.width - 1; x >= 0; x -= 1) {
      if (pinkScore(image, x, y) >= threshold) {
        rightEdge = x;
        break;
      }
    }
    if (leftEdge === null || rightEdge === null) continue;
    results.push({
      y,
      leftEdge,
      rightEdge,
      leftRadius: centerX === undefined || centerX === null ? null : Number((centerX - leftEdge).toFixed(1)),
      rightRadius: centerX === undefined || centerX === null ? null : Number((rightEdge - centerX).toFixed(1)),
      width: rightEdge - leftEdge + 1,
    });
  }
  if (!results.length) return { ok: false, reason: "这一行上没有玫红像素" };
  const median = (values) => {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  };
  return {
    ok: true,
    rows: results,
    width: median(results.map((item) => item.width)),
    leftRadius: median(results.filter((item) => item.leftRadius !== null).map((item) => item.leftRadius)),
    rightRadius: median(results.filter((item) => item.rightRadius !== null).map((item) => item.rightRadius)),
  };
}
/**
 * 颜色调色板（颜色 → 像素数），用于**旋转无关**的材质/透明度对比。
 *
 * 为什么需要它：基线自身的「最终朝向」是不可复现的（聚焦 slerp 在相机补间结束时被截断，
 * 帧时序一变，停下来的角度就变）——两次基线截图互相之间都有差异。
 * 但调色板不受朝向影响：球体填充、轮廓、经纬线、融合环各档透明度的像素颜色是固定的，
 * 因此「基线 vs 改版」的调色板必须一致；不一致就说明颜色/透明度/材质真的动了。
 */
export function colorPalette(image, opts = {}) {
  const limit = opts.limit ?? 24;
  const minCount = opts.minCount ?? 40;
  const counts = new Map();
  for (let i = 0; i < image.data.length; i += 4) {
    const key = (image.data[i] << 16) | (image.data[i + 1] << 8) | image.data[i + 2];
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const total = image.width * image.height;
  return {
    distinct: counts.size,
    total,
    top: sorted.slice(0, limit).map(([key, count]) => ({
      rgb: [(key >> 16) & 255, (key >> 8) & 255, key & 255].join(","),
      count,
      share: Number((count / total).toFixed(5)),
    })),
    /** 像素数 ≥ minCount 的颜色（用于「两边集合是否一致」的严格比较） */
    significant: sorted.filter(([, count]) => count >= minCount).map(([key]) => [(key >> 16) & 255, (key >> 8) & 255, key & 255].join(",")),
  };
}
/**
 * 结构色探针（旋转无关的材质证据）。
 *
 * 为什么不用「调色板集合」：球体表面是渐变 + 半透明叠加，抗锯齿会产出成百上千种中间色，
 * 朝向一变这些中间色的分布就变 —— 集合比较只会得到「都不一致」的无用结论。
 * 真正能证明「颜色/透明度/材质没变」的是**结构位置上的颜色**：
 * - 轮廓环：球心所在行、球体左极值处的那条细线（opacity 0.8，颜色随主题）；
 * - 融合环/话题点：玫红强度最高那批像素的颜色（内圈 0.62 / 中 0.44 / 外 0.28 叠加后的实色）；
 * - 球体内部：排除玫红结构后的众数色（填充 0.05 + 经纬线 + 虚空渐变）。
 * 这三组在任意朝向下都由令牌决定，因此可以直接在「基线 vs 改版」之间比对。
 */
export function probeMaterials(image, opts = {}) {
  const centerX = opts.centerX ?? image.width / 2;
  const centerY = opts.centerY ?? image.height / 2;
  const row = Math.round(centerY);

  const strongPink = new Map();
  const interior = new Map();
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const score = pinkScore(image, x, y);
      const i = (y * image.width + x) * 4;
      const key = (image.data[i] << 16) | (image.data[i + 1] << 8) | image.data[i + 2];
      if (score >= 0.6) strongPink.set(key, (strongPink.get(key) ?? 0) + 1);
      else if (Math.hypot(x - centerX, y - centerY) < Math.min(image.width, image.height) * 0.35) {
        interior.set(key, (interior.get(key) ?? 0) + 1);
      }
    }
  }
  const toRgb = (key) => [(key >> 16) & 255, (key >> 8) & 255, key & 255].join(",");
  const rank = (map, limit) =>
    [...map.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, limit)
      .map(([key, count]) => ({ rgb: toRgb(key), count }));

  /**
   * 轮廓环颜色：取球心行**最左侧**的玫红像素（= 球体左极值处的那条细线）。
   * 注意不能取「这一行最强的玫红」—— 那一带常常是融合环或话题点，颜色对但位置不稳，
   * 换个朝向采样点就跑到别的结构上去了（实测就踩过这个坑）。
   */
  let contour = null;
  for (let x = 0; x < image.width - 2; x += 1) {
    const score = pinkScore(image, x, row);
    if (score >= 0.15) {
      // 往内 1px 取更实的一笔（1px 细线在抗锯齿下最外侧往往只有部分覆盖）
      const i = (row * image.width + Math.min(image.width - 1, x + 1)) * 4;
      contour = {
        rgb: [image.data[i], image.data[i + 1], image.data[i + 2]].join(","),
        x,
        score: Number(score.toFixed(3)),
      };
      break;
    }
  }
  return {
    contourOnRow: contour,
    strongPinkTop: rank(strongPink, 6),
    strongPinkDistinct: strongPink.size,
    interiorTop: rank(interior, 4),
  };
}
/**
 * 轮廓环颜色（按圆采样，朝向无关）。
 *
 * 轮廓环是一个完整的圆：沿整个圆周采样后取**众数色**，个别角度上被融合环/话题点
 * 干扰的采样点会被自动投掉。这样得到的颜色就是轮廓线本身的实色（opacity 0.8）。
 */
export function contourRingColor(image, opts = {}) {
  const centerX = opts.centerX;
  const centerY = opts.centerY;
  const radius = opts.radius;
  if (!Number.isFinite(centerX) || !Number.isFinite(centerY) || !Number.isFinite(radius) || radius <= 1) {
    return { ok: false, reason: "缺少圆心/半径" };
  }
  const counts = new Map();
  let sampled = 0;
  const angles = 720;
  for (let a = 0; a < angles; a += 1) {
    const theta = (a / angles) * Math.PI * 2;
    let best = 0;
    let bestKey = null;
    for (let dr = -1.5; dr <= 1.5; dr += 0.5) {
      const x = Math.round(centerX + Math.cos(theta) * (radius + dr));
      const y = Math.round(centerY + Math.sin(theta) * (radius + dr));
      if (x < 1 || y < 1 || x >= image.width - 1 || y >= image.height - 1) continue;
      const score = pinkScore(image, x, y);
      if (score > best) {
        best = score;
        bestKey = (image.data[(y * image.width + x) * 4] << 16) |
          (image.data[(y * image.width + x) * 4 + 1] << 8) |
          image.data[(y * image.width + x) * 4 + 2];
      }
    }
    if (bestKey !== null && best >= 0.15) {
      counts.set(bestKey, (counts.get(bestKey) ?? 0) + 1);
      sampled += 1;
    }
  }
  if (!sampled) return { ok: false, reason: "圆周上没有采样到玫红像素" };
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  const toRgb = (key) => [(key >> 16) & 255, (key >> 8) & 255, key & 255].join(",");
  return {
    ok: true,
    sampled,
    mode: { rgb: toRgb(top[0][0]), count: top[0][1] },
    top: top.map(([key, count]) => ({ rgb: toRgb(key), count })),
  };
}
/**
 * 竖直中带内「每行最右侧玫红像素」的中位数。
 *
 * 用途：管理展开后要判断「右半球完整可见」——右边界应当 ≈ 球心 + 半径。
 * 为什么要按行取中位数：画面里还有界面层的玫红元素（选中话题的标签胶囊、页面工具行、
 * 按钮描边），它们在竖直方向只占少数行；球轮廓是连续弧线，跨几百行都成立。
 * 中位数因此对界面元素免疫。
 */
export function medianRightmost(image, opts = {}) {
  const centerY = opts.centerY ?? image.height / 2;
  const band = opts.band ?? 150;
  const threshold = opts.threshold ?? 0.25;
  const rights = [];
  for (let y = Math.max(0, Math.round(centerY - band)); y <= Math.min(image.height - 1, Math.round(centerY + band)); y += 2) {
    for (let x = image.width - 1; x >= 0; x -= 1) {
      if (pinkScore(image, x, y) >= threshold) {
        rights.push(x);
        break;
      }
    }
  }
  if (!rights.length) return { ok: false, reason: "中带内没有玫红像素" };
  rights.sort((a, b) => a - b);
  const median = rights[Math.floor(rights.length / 2)];
  return {
    ok: true,
    rows: rights.length,
    rightmost: median,
    p10: rights[Math.floor(rights.length * 0.1)],
    p90: rights[Math.floor(rights.length * 0.9)],
  };
}
/**
 * 绕球心单轴（视线轴）旋转后的平均绝对差。
 *
 * 为什么需要它：基线球体的**最终滚转角**不确定 —— 入口球态的空闲自转（rotation.y += dt·0.1）
 * 累积量取决于加载时长，聚焦 slerp 之后残留的滚转因此每跑一次都不一样（实测同一份代码两次
 * 截图差 11–17% 像素、滚转 40–60°）。直接逐像素 diff 只会得到「大量假差异」。
 * 正确做法：在 -180°..+180° 内搜一个最佳对齐角，在该角度下再比 —— 这时比的是**同一朝向**。
 */
export function alignedMeanAbsDiff(a, b, angleDeg, opts = {}) {
  const cx = opts.cx ?? a.width / 2;
  const cy = opts.cy ?? a.height / 2;
  const step = opts.step ?? 1;
  const rad = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  let sum = 0;
  let count = 0;
  for (let y = 0; y < a.height; y += step) {
    for (let x = 0; x < a.width; x += step) {
      const dx = x - cx;
      const dy = y - cy;
      const sx = Math.round(cx + dx * cos - dy * sin);
      const sy = Math.round(cy + dx * sin + dy * cos);
      if (sx < 0 || sy < 0 || sx >= b.width || sy >= b.height) continue;
      const i = (y * a.width + x) * 4;
      const j = (sy * b.width + sx) * 4;
      sum +=
        Math.abs(a.data[i] - b.data[j]) +
        Math.abs(a.data[i + 1] - b.data[j + 1]) +
        Math.abs(a.data[i + 2] - b.data[j + 2]);
      count += 1;
    }
  }
  return { angle: angleDeg, meanAbs: count ? Number((sum / (count * 3)).toFixed(3)) : null, samples: count };
}

/**
 * 单轴对齐搜索：先 2° 粗搜（隔像素采样），再在最佳角附近 0.25° 细化。
 * 返回最佳角与残余差异；残余差异就是「对齐到同一朝向后，美术还剩多少不同」。
 */
export function bestRotationAlignment(a, b, opts = {}) {
  if (a.width !== b.width || a.height !== b.height) {
    return { ok: false, reason: "尺寸不同，无法旋转对齐" };
  }
  const cx = opts.cx ?? a.width / 2;
  const cy = opts.cy ?? a.height / 2;
  const coarseStep = opts.coarseStep ?? 2;
  const fineStep = opts.fineStep ?? 0.25;
  const coarse = [];
  for (let angle = -180; angle < 180; angle += coarseStep) {
    const result = alignedMeanAbsDiff(a, b, angle, { cx, cy, step: 2 });
    coarse.push(result);
  }
  coarse.sort((m, n) => m.meanAbs - n.meanAbs);
  const bestCoarse = coarse[0];
  const fine = [];
  for (let angle = bestCoarse.angle - coarseStep; angle <= bestCoarse.angle + coarseStep; angle += fineStep) {
    fine.push(alignedMeanAbsDiff(a, b, angle, { cx, cy, step: 1 }));
  }
  fine.sort((m, n) => m.meanAbs - n.meanAbs);
  const best = fine[0];
  return {
    ok: true,
    angle: Number(best.angle.toFixed(2)),
    meanAbs: best.meanAbs,
    unalignedMeanAbs: alignedMeanAbsDiff(a, b, 0, { cx, cy, step: 1 }).meanAbs,
    coarseBest: Number(bestCoarse.angle.toFixed(1)),
    candidates: fine.length,
  };
}

/** 按最佳对齐角旋转一张图（写差异热图用）。 */
export function rotateAbout(image, angleDeg, opts = {}) {
  const cx = opts.cx ?? image.width / 2;
  const cy = opts.cy ?? image.height / 2;
  const rad = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const out = Buffer.alloc(image.width * image.height * 4, 255);
  for (let y = 0; y < image.height; y += 1) {
    for (let x = 0; x < image.width; x += 1) {
      const dx = x - cx;
      const dy = y - cy;
      const sx = Math.round(cx + dx * cos - dy * sin);
      const sy = Math.round(cy + dx * sin + dy * cos);
      const dst = (y * image.width + x) * 4;
      if (sx < 0 || sy < 0 || sx >= image.width || sy >= image.height) {
        out[dst + 3] = 0;
        continue;
      }
      const src = (sy * image.width + sx) * 4;
      out[dst] = image.data[src];
      out[dst + 1] = image.data[src + 1];
      out[dst + 2] = image.data[src + 2];
      out[dst + 3] = 255;
    }
  }
  return { width: image.width, height: image.height, data: out };
}
