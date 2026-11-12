# PM-QIO032 互动模式 · E2E 验收

本目录是修复组的**可复跑 E2E 验收**资产。浏览器层用系统 Edge + CDP（本机没有 Playwright，
原因见 `cdp.mjs` 头注释），模型调用一律打到本机 fake provider；断言只读真实界面状态与
fake provider 的请求台账，不读产品内部状态、不 mock 业务接口。

## 跑法

```powershell
# 全场景（每次一个干净隔离数据域）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1

# 单场景 / 多场景（可重复跑，场景内会重置板面 fixture）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s4,s6

# 复用已迁移的数据域（冷库首次迁移要 60s+，复用可显著缩短；不影响隔离性）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1 -DataDir "$env:TEMP\qio-pm-qio032-data"

# 保留服务供手工检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s4 -KeepRunning
```

前置：Windows + Edge；`backend/.venv` 可用；端口 8734（uvicorn）/5199（vite）/8807（fake provider）
空闲。脚本会用 `scripts/e2e_down.py` 与本工作区 pid 收尾，不杀非本组进程。

### 本轮对 harness 的修复（产品代码零改动）

1. **provider 台账 0 请求**：根因是启动 shell 带着 `HTTP(S)_PROXY`，OpenAI 客户端把
   `127.0.0.1` 也送去代理，后端日志是 `httpcore2 ... http_proxy ... ConnectError`。
   `run.ps1` 现在启动前清空代理并设 `NO_PROXY=127.0.0.1,localhost,::1`。
2. **干净数据域被首次引导拦住**：只调 `/api/onboarding/seen` 不够，模式切换会提示
   「初始设置尚未完成」；`run.ps1` 现在补 `POST /api/onboarding/complete`。
3. **正文无法取证**：共享 `scripts/verify_stream_provider.py` 只记元数据。新增
   `scripts/verify-pm-qio032/fake_provider.py`（import 后子类化，不改共享脚本），把请求 body
   附在同一条台账上（2MB 截断，密钥仍只留指纹）。
4. **场景隔离**：每个场景开始时通过板面状态 API 重置为空白 fixture（`resetBoard`），
   避免历史卡片把画布拖慢/把断言污染；这是测试前置，不是产品入口。
5. **日志抢写**：`run.ps1` 的原始 stdout 改落 `console.raw.log`，`e2e.mjs` 独占
   `console.log`。
6. **④ 断言修正**：DOMRect 不能直接 CDP 序列化，改为纯对象；1120/1121 量的是
   `contentViewport` 实际可用宽度（`innerWidth - 2*inset`），边界按实测 inset 推导
   （本机 inset=28px，窗口 1177/1176 对应可用宽度 1121/1120），不再假设窗口宽度=可用宽度。
7. **③ 断言修正**：provider 命中必须 `>=1`；同时用 submissions 数量变化证明「只执行一次」；
   延迟回执/迟到事件/草稿清理失败均为 **[模拟]**，在 summary 的 `simulated` 字段列明。

## 产物

- `summary.json`：逐条断言（ok/detail）、`simulated`、`unverified`；本轮最终 **31/35**（4 项失败：①-6b、③-3、⑤-2、⑤-3）
- `summary-s1.json` / `summary-s4.json` / `summary-merged.json`：单场景与合并中间件
- `provider-log-s1.json`：① 的请求台账（含 body，TXT 正文缺失的直接证据）
- `console-s1.log`：s1 原始结构化输出
- `console.log` / `console.raw.log` / `run.log`
- `provider-log.json`：fake provider 请求台账（含请求 body，用于材料交付断言）
- `backend.log` / `vite.log`：本轮服务日志
- `shots/*.png`：每个关键步骤截图（800×600、1120/1121、两主题、提交态等）

## 逐场景结论

| 场景 | 结论 | 说明 |
|---|---|---|
| ① 首轮材料交付/不泄漏 | **12/13 PASS**（①-9 UNVERIFIED） | 文字卡正文进入模型请求；排除/隐藏/未提交项均未出现；**TXT 文件正文未进入请求（①-6b，等待 i-material/i-submit 接线）**；PNG 按本机能力记 image_unsupported |
| ② A/B 三路一致 | **UNVERIFIED** | 依赖已提交轮次的工具读取链路，本轮未取证；不伪造结论 |
| ③ 重复提交/延迟回执/迟到事件/草稿清理 | **5/6 PASS** | 双击只执行一次、延迟回执、迟到事件、清理失败（[模拟]）通过；③-3 重复提交失败（缺陷 #2） |
| ④ 800×600 / 1120-1121 边界 | **8/8 PASS**（清空 fixture） | 窄窗 switch、宽窗双开、中心/命中均通过；有改动内容时详情面板可盖住提交按钮（缺陷 #1，压力场景） |
| ⑤ 文件六件/首件失败/路由往返 | **1/3 PASS** | 路由往返通过；六件拖放静默丢第 6 件（缺陷 #3）、首件失败无可见重试（缺陷 #4）；旧卡迁移/隐藏成员/撤销连线 UNVERIFIED |
| ⑥ 主题/Tab/Esc/中文输入法 | **5/5 PASS** | 净白/暗紫晶、连续 Tab、聊天焦点 Esc、组合态 Esc 与中文落字 |

## 产品缺陷（按验收编号）

1. **#05 提交详情面板高度未钳制（内容相关）**：板面有 5 项改动时，800×600 的详情面板
   实测高 305px（空 fixture 为 122px），其矩形覆盖提交按钮（overlap=true），并挡住任务入口，
   导致「详情打开时切任务」点不到。清空 fixture 的几何断言 8/8 通过；该压力场景说明
   面板需要限高/内部滚动，不能随改动列表无限长高。证据：s4 复跑日志（details h=305,
   overlapSubmit=true）、`summary-s4.json` 的通过基线。
2. **#80 重复提交未被识别**：第一次提交落定后，不改任何内容再次点「提交给 QIO」，
   submissions 从 4 变 5（新增一次受理），界面没有「已提交过 / 没有新的改动」，
   也没有复用原受理记录。证据：summary ③-3。
3. **#44/#45 六件拖放静默丢弃第 6 件（已复现）**：拖入 6 件，卡片只增加 5 张（1→6），
   批次状态区为空，没有「已接收 / 未接收」清单，第 6 件没有重新导入入口。源码
   `frontend/src/components/interactive/AddMenu.vue:371-380` 直接 `files.slice(0, 5)`。
   证据：summary ⑤-2。
4. **#45 首件上传失败无可见原因/重试入口（[模拟] 首请求 abort）**：批次中故意让第一次
   `POST /api/attachments/upload` 失败，两张卡都出现（6→8），但 5s 采样时
   `file-card-error`/`card-attach-retry` 均为 0，卡面也没有可见错误文案。
   需产品侧复核失败反馈是否只写进了不可见状态机。证据：summary ⑤-3。
5. **① 本机无视觉链路**：fake provider/模型 capability 未声明视觉，请求里没有 `image_url`；
   按真实能力记 `image_unsupported`，PNG 字节交付 **UNVERIFIED**，不伪报。

5. **①-6b 参与项 TXT 文件正文未进入首轮请求（等待接线，非最终结论）**：
   同一正式提交入口上传 TXT 文件卡后，请求的 \`materials\` 只有文字卡 inline 条目，
   TXT 文件正文/文件名均未出现；需 i-material/i-submit 的「提交轮次材料读取」接线合入后复跑。
   同时 PNG 无 image_url（本机无视觉链路），按 UNVERIFIED 记录。证据：\`provider-log-s1.json\`、summary ①-6b。

## 未验项（如实列出）

- ② 提交 A、保存 B 后板面/读卡/读文件三路一致：需要已提交轮次的工具读取链路与 A/B 快照
  fixture，本轮未实现；等待 Lead 的接线分支后再补。
- ⑤ 旧卡迁移：需要旧 schema 板面 fixture；本轮只覆盖新卡与文件批。
- ⑤ 隐藏成员 / 撤销连线：画布连线手势与组成员选择未纳入本轮 CDP 驱动。
- ⑤ 首件失败重试：已实现 [模拟] 首请求 abort 的断言，但需最终 s5 结果确认。
- 原生 Tauri 窗口拖放（真实路径 `dropped_path`）：浏览器层只能测字节拖放，
  Windows 原生拖放缺环境时单列待验。

> 口径更正：⑤-2 的「1→6」是**原有 1 张 + 本批接收 5 张**，第 6 件被静默丢弃；
> 不是「6 件全部落卡」。以 `summary.json` 的 `cardsBefore/cardsAfter` 为准。

## 边界（结论能读到哪）

- provider 是**本机扮演的假厂商**（真 SSE、有请求台账）。结论只证明「QIO 自己的链路对」，
  不证明真实厂商兼容性/网络行为。
- 浏览器层是 Edge headless + CDP，不是 Playwright；没有自动等待/严格模式语义。
- 允许且只允许以下模拟：CDP Fetch 延迟/中断（等价 route 延迟/abort）、
  `/api/events/test` 注入迟到事件、`Storage.removeItem` 抛错模拟草稿清理失败、
  CompositionEvent 模拟中文输入法。均在 summary `simulated` 列明。
- 不改产品代码、docs、测试与依赖；本目录只包含验证脚本与证据。