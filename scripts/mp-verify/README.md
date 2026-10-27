# scripts/mp-verify —— 记忆星球页改版的验收工具（A / B / C / Lead 共用）

一套「起隔离环境 → 造真实数据 → 截图 / 逐帧采样 / 录屏 → 对照」的最小工具集。
只依赖 Node 内置能力（fetch / WebSocket / zlib）+ 本机 Edge 或 Chrome；录屏另需 ffmpeg（有就用）。

> 这些脚本**只给验收用**，不参与产品打包；产品代码里没有任何演示数据。

---

## 0. 端口约定（Lead 统一分配，避免互抢）

| 角色 | vite dev | 说明 |
| --- | --- | --- |
| base | 5101 | 只读基线 worktree（视觉对照用） |
| int | 5102 | Lead 集成 |
| C | 5103 | 本工具的作者分支 |
| A | 5104 | 页面与导航 |
| B | 5105 | 管理流程 |

后端：共享开发后端 `127.0.0.1:8734`；需要自己的后端（基线对照、改后端）用 `127.0.0.1:8751`。
CDP 调试端口也要分开：`--cdp-port 9801`（C 默认）/ 9802 / 9803 …

**不要在这些脚本里跑 npm ci / uv sync**：依赖由 Lead 统一复制到各 worktree。

---

## 1. 起一个隔离环境（后端 + 指定 worktree 的前端）

```powershell
# C 自己的分支（数据目录默认 %TEMP%\qio-mp-verify\data\c，绝不碰用户真实数据）
node scripts/mp-verify/serve.mjs up --label c --worktree D:\qio-dev\qio-mp-c --app-port 5103 --backend-port 8751 --seed

# 只读基线（对照用）：另起一套，端口与数据目录都不同
node scripts/mp-verify/serve.mjs up --label base --worktree D:\qio-dev\qio-mp-base --app-port 5101 --backend-port 8752 \
  --python D:\qio-dev\qio-mp-int\backend\.venv\Scripts\python.exe --seed

node scripts/mp-verify/serve.mjs status --label c
node scripts/mp-verify/serve.mjs down --label c
```

要点：
- `--python` 可以借别的 worktree 的 venv（`PYTHONPATH` 仍指向**你自己 worktree 的 backend/src**，跑的一定是这份代码）；
  不传时依次试 `<worktree>\backend\.venv` → 环境变量 `QIO_MP_PYTHON` → `--fallback-python`。
- 数据目录默认 `%TEMP%\qio-mp-verify\data\<label>`，一个 label 一份，互不干扰。
- 状态写 `scripts/mp-verify/.state/<label>.json`（已 gitignore），`down` 按它停进程。
- 前端固定用 `frontend/vite.e2e.config.ts`（补 `server.fs.allow`，避免 @fontsource 中文字体 403）。

## 2. 造数据（真实接口能读到的形状）

```powershell
& D:\qio-dev\qio-mp-int\backend\.venv\Scripts\python.exe scripts/mp-verify/seed.py \
  --data-dir "$env:TEMP\qio-mp-verify\data\c" --worktree D:\qio-dev\qio-mp-c --reset
```

- 话题 / 知识 / 实体 / 凭据复用仓库既有的 `scripts/baseline/seed_fixtures.py`（基线 vs 改版同一份确定性数据）。
- 待处理：知识 `state=pending_review`（真实接口 `/api/memory/pending` 会读到）+ 维护提问
  `maintenance_items(kind=ask, status=awaiting_answer)`。
- 整理记录：`maintenance_batches` / `maintenance_items` / `maintenance_operations`。
- 实体更新候选：写进 `entity_cards.field_meta.pending`（A04 候选的真实形状）。
- 迁移 47（维护表）在基线上还没接进 `schema.py`：验收库里由 `seed.py` 用模块自带 DDL 补建，
  **只是验收数据目录里的补丁**，不改产品迁移顺序；补不上时脚本在 `unavailable` 里如实写明，
  对应场景标记为「未验证」而不是假装通过。

## 3. 截图 + 页面度量

```powershell
node scripts/mp-verify/shots.mjs --url http://127.0.0.1:5103 --label rev \
  --out docs/mp-verify/c-planet/shots --viewports 1440x970 --viewport-tag 1440x900 \
  --scenarios planet,expanded,collapsed --cdp-port 9801
```

每个（视口 × 场景）产出：
- `<label>-<场景标签>-<场景>.png` 整页（人看）；
- `…-stage.png` 舞台区域裁剪（含布局，逐像素对比用）；
- `…-sphere.png` **以画布中心为中心裁剪**（边长 = 画布短边 × 1.02），所以「球被布局挪到左边」不会造成假差异；
- `report.json`：视口、内容区/画布/面板矩形、`--mp`、`data-mp`、`managing`、
  横向滚动判据（`scrollWidth > clientWidth+1` 的容器清单）、DEV 钩子诊断。
- **像素测量**：玫红结构包围盒（`height` = 球体直径，竖直方向不被裁切）、径向拟合（完整圆时）、
  横向扫描线笔画宽度直方图（SDF 融合环是恒定像素宽描边，用它量环宽）。

> **同一画布高度**是美术对照的前提：基线窗口 1440×900（画布高 900）↔ 改版窗口 1440×970
> （内容区在全局栏下方，画布高 900）。球体半径 ≈ 画布高 × 0.5025，两边必须换成同一画布高，
> 报告里用 `--viewport-tag` 把它们记成同一个场景键。

所有截图都在「朝向 + 渲染计数连续两次不变」之后拍（`waitSceneStatic`）；否则空闲自转
会让两次截图本来就不可能一样。

## 4. 基线 vs 改版对照

```powershell
node scripts/mp-verify/compare.mjs \
  --base docs/mp-verify/c-planet/shots/base --rev docs/mp-verify/c-planet/shots/rev \
  --out docs/mp-verify/c-planet/shots/compare
```

输出 `compare.md` + 差异热图 `*-sphere-diff.png` / `*-stage-diff.png`。判读口径：
1. **美术有没有变** → 球体裁剪图的逐像素差异（容差 ≤ 2/255）。颜色/透明度/材质/轮廓/环宽/话题点一变，这张图立刻变。
2. **球体直径** → 包围盒高度 + DEV 钩子半径（基线与改版必须 ±1px 内）。
3. **环宽** → 扫描线笔画宽度直方图的峰（内 10 / 中 6.5 / 外 4 全宽）。
4. **位置/布局**（本轮要改对的行为）→ 单独一栏，不混进美术结论。

## 5. 逐帧采样（展开 / 收起 / 中途反向）

```powershell
node scripts/mp-verify/frames.mjs --url http://127.0.0.1:5103 --label rev \
  --out docs/mp-verify/c-planet/frames --actions expand,collapse,reverse \
  --viewport 1440x970 --strip        # --strip：另跑一遍 ?planetdemo=slow 截图条，仅目视
```

- 页面内 rAF 采样器逐帧记：`t / --mp / data-mp / managing / 画布矩形（含 CSS 变换）/ 内容区矩形 / 选中标签位置 / 渲染计数`。
- 中途反向由**页面内按帧判定**触发（`--mp` 跨过 `--reverse-at`（默认 0.5）那一帧就点收起），不靠 Node 侧 sleep 猜。
- 输出 `…-<action>.json`：`durationMs / frames / fps≈ / maxStep / jumps（|Δmp|>0.16 的瞬移）/ reversals / reverseOvershoot / centerX 起止 / labelX 起止 / 完整 curve`。
- **时间线只有一条**：数值曲线一律来自真实时长那一遍。`--strip` 的慢放截图是**另一遍**、只为目视阶段顺序，
  报告里会注明，两遍数据不混用。

## 6. 录屏（真录屏，不是逐帧拼接）

```powershell
node scripts/mp-verify/record.mjs --url http://127.0.0.1:5103 --label rev \
  --out docs/mp-verify/c-planet/record --action expand --viewport 1440x970
```

用 CDP `Page.startScreencast` 抓浏览器真实合成帧 → `frame-*.jpg` → ffmpeg 编 `capture.mp4`。
帧时间戳不等间隔，mp4 按平均帧率编码；**不要**拿视频帧数反推动画帧数，帧级数据看 `frames.mjs`。

## 7. 渲染收尾 + 减少动态效果探针

```powershell
node scripts/mp-verify/probe.mjs --url http://127.0.0.1:5103 --label rev \
  --out docs/mp-verify/c-planet/probe --viewport 1440x970
```

- 静置 2.5s 前后两次渲染计数（`__qioPlanetScene().renders` / CDP `Frames`）：不增长 = 动画结束确实停帧。
- 点一下画布后再读：必须恢复增长（停帧不能把交互冻住）。
- 减少动态效果：写 `localStorage(qio-motion=reduced)` + 重载（与设置页同一份偏好），
  再展开，逐帧看 `--mp` 是否 ≤1 帧到达目标；结束时删掉这个偏好。

## 8. 已知边界

- 依赖 `usePlanetScene` 在 DEV 构建下暴露的只读钩子 `window.__qioPlanetScene()`
  （`webglOK / renders / loopActive / paused / quaternion`）。钩子不存在时脚本自动退化：
  `waitSceneStatic` 只看 `--mp`，渲染计数改用 CDP `Frames` 指标，其余度量一律用 DOM 与像素。
- `window.__qioPlanetStage()`（`sphere` / `selectedLabel` / 阶段）来自 PlanetView，属于 A 的文件；
  不存在时 `radiusHook` / `labelX` 一栏为 `null`，不影响其它结论。
- 无 GPU 的机器用 SwiftShader 跑 WebGL（`--use-angle=swiftshader`）：帧率偏低是环境特性，不是产品问题。
- 截图里的中文字体必须真的加载（`vite.e2e.config.ts` 已放行），否则排版结论不可信。
