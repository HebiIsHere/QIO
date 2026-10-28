# scripts/settings-verify —— 设置页改版验收取证工具（D）

一套「起隔离环境 → 截图 + 脚本自检 → 交互取证 → 录屏 → 汇总」的可重复验收装置。
只给验收用，不参与产品打包；产品代码里没有任何 fixture 或演示数据。

产物落在两处（简报指定）：

- `docs/verification-shots-settings/` —— 截图 + `report.json` / `report.md`
- `docs/verification-video-settings/` —— 关键流程录屏（mp4）+ `record-report.json`

运行期产物（报告副本、日志、按次证据）落 `scripts/settings-verify/evidence/`（已 gitignore）。

---

## 1. 依赖与环境

| 依赖 | 说明 |
| --- | --- |
| Node.js ≥ 20 | 只用内置能力（fetch / WebSocket / zlib / child_process），无额外 npm 包 |
| Edge 或 Chrome | CDP 无头浏览器；用 `QIO_SETTINGS_BROWSER` 指定可执行文件 |
| ffmpeg（可选） | 录屏编码 mp4；没有时只落 JPEG 帧序列，并在报告里明说没有视频 |
| `frontend/node_modules` | 是指向基线的 **junction**，不要删除 / 重建 / `npm install` |
| `backend/.venv` | 同上，是指向基线的 junction |

端口与数据目录（与其它工作区错开，互不抢）：

| 角色 | 前端 | 后端 | CDP | 数据目录 |
| --- | --- | --- | --- | --- |
| D（本 worktree） | 5106 | 8756 | 9806 | `%TEMP%\qio-settings-verify\data\d` |
| A | 5107 | 8757 | 9807 | `%TEMP%\qio-settings-verify\data\a` |

数据目录固定落 `%TEMP%`，**绝不**继承用户真实数据目录；后端以 `QIO_DEV_INSECURE=1` +
`QIO_DISABLE_DB_CHECK=1` 启动，不要求任何 Key，也不做真实模型调用。

---

## 2. 怎么起页面（选定的那条路径）

**选的是「隔离后的真实后端 + 浏览器侧 fixture 拦截」，不是纯前端 mock。**

理由（与 Lead 的优先建议对账过）：

1. `App.vue` 启动时先 `waitForBackend()`（`/api/health` 应答前不渲染主界面），
   纯前端 mock 要连 SSE `/api/events` 的长连接一起挡；挡不好会把启动状态卡在
   「正在连接后端」，截出来的不是设置页；
2. 真实后端能给出**真的**保存成功/失败语义（写入 SQLite、重读一致），
   把「已保存」建立在 mock 成功上会让结论不可信；
3. 需要控制的地方（Key / 凭据 / 模型目录 / 搜索通道状态、失败注入、迟到响应）
   全部用 **CDP Fetch 域拦截**在浏览器侧替换，不发给后端；
4. 全程只连 `127.0.0.1`，**不联网**、不用真实 Key。

复现命令（从本 worktree 根目录执行）：

```powershell
# 1) 起隔离环境（后端 + 前端 dev server；状态写 scripts/settings-verify/.state/<label>.json）
node scripts/settings-verify/serve.mjs up --label d
# 复用 A 的 worktree（第一批视觉/结构核查用）：
node scripts/settings-verify/serve.mjs up --label a --worktree D:\qio-dev\qio-stg-a --app-port 5107 --backend-port 8757

# 2) 一键取证（视觉 + 交互 + 录屏），跑完自动停环境
node scripts/settings-verify/run-all.mjs --url http://127.0.0.1:5106 --api http://127.0.0.1:8756 --report-label baseline

# 3) 也可分开跑
node scripts/settings-verify/visual.mjs      --url http://127.0.0.1:5106 --api http://127.0.0.1:8756 --report-label r1
node scripts/settings-verify/interaction.mjs --url http://127.0.0.1:5106 --api http://127.0.0.1:8756 --label r1
node scripts/settings-verify/record.mjs      --url http://127.0.0.1:5106 --api http://127.0.0.1:8756 --label r1

# 4) 停
node scripts/settings-verify/serve.mjs down --label d
```

PowerShell 入口：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/settings-verify/run-all.ps1 -ReportLabel r1`。

---

## 3. 文件

| 文件 | 作用 |
| --- | --- |
| `serve.mjs` | 起/停隔离环境（`up` / `status` / `down`） |
| `visual.mjs` | 四种尺寸 × 深/浅主题 × 全场景截图 + 脚本内自检 → `report.json` / `report.md` |
| `interaction.mjs` | 草稿保留 / 离开丢弃 / 分组保存成功与失败 / 迟到响应 / 搜索定位 → `evidence/interaction-<label>.json` |
| `record.mjs` | 关键流程真实录屏（CDP `Page.startScreencast` → mp4）→ `docs/verification-video-settings/` |
| `run-all.mjs` / `run-all.ps1` | 一条命令串起上面三步并汇总 |
| `lib/env.mjs` | 端口、路径、五类分类与别名（唯一来源） |
| `lib/stack.mjs` | 环境启停实现（端口探测、隔离数据目录、vite.e2e.config.ts） |
| `lib/browser.mjs` | 在 `scripts/mp-verify/lib/cdp.mjs` 之上补：Fetch 替身、键盘、偏好、原生对话框守卫 |
| `lib/page-helpers.page.js` | 注入页面的探针（读事实 / 点可见控件），单独源文件、运行时读取 |
| `lib/probe.mjs` | 探针的 Node 侧封装（导航 / 一次采集全部事实 / 分类比对） |
| `lib/fixtures.mjs` | 读取类固定响应（凭据卡 / 模型目录 / 搜索通道状态），不含任何真实 Key |

---

## 4. 检查项与判定口径（`visual.mjs`）

每个（尺寸 × 主题）组合都会截图并跑同一批检查：

| 检查 | 判定 |
| --- | --- |
| `settings-root-present` | 设置根存在（`[data-settings-page] / .qio-settings / .settings`） |
| `theme-applied` | `html[data-theme]` 与目标主题一致 |
| `radius-token-is-14px` | `--qio-ui-radius` 计算值为 `14px` |
| `global-bar-single-and-fixed` | `.window-chrome` 恰好 1 个、不在设置根内、`position: fixed` |
| `global-bar-not-replaced` | 设置根内没有 `position: fixed/sticky` 的 header/banner（不自绘标题栏） |
| `global-bar-no-overlap` | 设置根顶部不压住全局栏 |
| `five-categories` | 导航恰好五类，标签与约定一致（缺 / 多都报出来） |
| `category-<id>-reachable` | 五类都能点开 |
| `advanced-<id>-expanded` | 常规 / 记忆与整理 / 模型与服务 都有可展开的高级选项并真的展开 |
| `no-horizontal-scroll` | 设置页内逐容器 `scrollWidth <= clientWidth + 1`（装饰层与输入框内部文字滚动除外） |
| `no-native-select-number-checkbox` | 设置子树内可见的 `select` / `input[type=number]` / `input[type=checkbox]` / `input[type=radio]` 计数为 0 |
| `radius-uniform-token` | 可见界面组件的圆角要么 14px，要么是胶囊/圆形（按高度一半识别）；其余记为违规 |
| `radius-edge-attached-reported` | 「一侧直角 + 其余角 14px」的贴边形状单列（当前导航标签就是这种），**不混进违规**，供产品裁定 |
| `nav-layout-side` / `nav-layout-top` | 1024+ 为侧栏、≤900 为顶部标签（优先读 `data-nav-layout`，并交叉核对矩形关系） |
| `single-vertical-scroll` | 设置页内只有一个「实际溢出且可纵向滚动」的元素且是设置根；文档无横向溢出；代码/许可全文这类限高查看器单列 `bounded` |
| `no-native-dialogs` | `window.confirm/alert/prompt` 调用计数为 0（`Page.addScriptToEvaluateOnNewDocument` 守卫计数） |
| `no-runtime-errors` | 页面无未捕获 JS 异常（网络失败单列 `httpFails`） |
| `search-panel` / `search-results` / `search-locate-highlight` | 搜索入口可达、有命中、点结果后切分类并出现 `.is-revealed` 高亮 |
| `long-form-opens` | 能打开 Key 接入表单（B 的占位期跳过，见下） |
| `confirm-layer` | 危险操作出现中央确认层（QConfirm layer），Esc 可取消 |
| `long-text-readable` | 关于页展开许可 / 第三方声明后可见文本 > 4000 字 |
| `keyboard-reaches-controls` / `keyboard-focus-visible` | 真实 Tab 键序列能到达分类/控件，且每个焦点都有可见指示（outline / box-shadow） |
| `reduced-motion-*` | `html[data-motion=reduced]` 下运行中的位移/缩放动画为 0（只允许透明度类动画） |
| `same-dom-order-<主题>-<宽>vs<宽>` | 同主题不同宽度下 `data-settings-key` **集合与文档顺序完全一致**（A 声明 1） |
| `structure-hook-present` | 签名来源必须是 `data-settings-key`（不是回退的文案推断） |

数据类检查一律汇总「全部五个分类」的结果再判，不只看当前分类。

### 占位期规则（不判红、也不冒充通过）

当前分支 `ModelServicesSection` 里是 B 的占位（`data-testid="models-panel-placeholder"`）。
检测到占位时，`long-form-opens` / `confirm-layer` 进入 **跳过** 列表（报告单列「跳过」小节），
既不算失败也不算通过。其余检查照常判定。

---

## 5. 交互取证（`interaction.mjs`）

| 场景 | 做法 | 判定 |
| --- | --- | --- |
| `draft-retention` | 改「自建 SearXNG 地址」不保存 → 切分类再回来 → 宽窄切换再回来 | 值仍是草稿 |
| `draft-discard-on-leave` | 改草稿 → 应用内 hash 路由去对话页 → 回设置页 | 草稿被丢弃；离开期间**没有**设置类写请求 |
| `group-save-success` | 改「返回条数」→ 点按组保存（真实后端） | 有写请求、有成功反馈、重载后仍是新值 |
| `group-save-failure` | 写请求注入 500 | 分区内联错误出现、草稿保留、无成功文案 |
| `late-response` | 第一次保存延迟 1.8s、第二次立即返回；旧响应晚到 | 最终显示第二次的值 |
| `search-locate` | 搜「主题 / 返回条数 / 记忆整理」→ 点命中 | 切到正确分类并出现高亮 |
| `no-confirm-alert-prompt` | 全程对话框守卫计数 | 0 |

真实性边界：读取类数据是 fixture；写请求（除明确注入失败/延迟的用例外）打**真实隔离后端**；
「离开设置页」用应用内路由切换（整页刷新会连内存 store 一起清掉，测的就不是「离开页面」）。
假 Key 只出现在录屏的 Key 接入流程里，且该流程的写请求全部由替身接住，不落库、不联网。

---

## 6. 录屏（`record.mjs`）

CDP `Page.startScreencast` 抓浏览器真实合成帧 → ffmpeg 编 mp4（**不是**逐帧截图拼接）。

流程：`categories`（切换五类）/ `resize`（1440→1024→800→480→1440）/ `search`（输入→结果→定位）/
`key-flow`（打开 Key 表单 + 假 Key + 替身响应）/ `group-save`（改值→保存→成功反馈）。

产物：`docs/verification-video-settings/<flow>/capture.mp4` + `record.json`；
`record-report.json` 汇总每条的 `status`（recorded / flow-failed / prepare-failed）与替身边界说明。
默认不保留 JPEG 原始帧（`--keep-frames` 可留）。

---

## 7. 基线负对照（2026-10-28，基线 `ac0a83fd`）

在**基线**上跑这套脚本的目的只有一个：证明脚本能抓到「新要求 vs 基线」的差距，
不是拿基线当通过证据。

- `visual.mjs --quick`（1440×900 / dark）：23 条检查，通过 18、失败 5。
  失败项正是本轮要求：五类 IA（基线 8 tab）、圆角统一（基线 10px 输入框/权限模式卡片）、
  搜索（基线没有）、Key 长表单与确认层入口（基线在「凭据」独立分类，流程不同）。
  通过项包含：无横向滚动、无原生控件、全局栏未被替代、键盘焦点可见、减少动画无位移。
- `interaction.mjs`：19 条检查，通过 17、失败 2。
  失败项符合预期：基线**保留**离开设置页的草稿（本轮要求丢弃）、基线**没有**设置搜索。
  通过项里有真实后端写入 + 重载后一致的「分组保存成功」，以及失败注入、迟到响应。

## 8. 已知限制

- 无 GPU 时用 SwiftShader 软件光栅，帧率与视频观感不代表真机；截图排版不受影响（字体已确认实际加载）。
- 录屏帧时间戳不等间隔，mp4 按平均帧率编码；**不要**用视频帧数反推动画帧数。
- 不覆盖真实联网、真实模型调用、真实 Key（按项目约定不做）；这三类证据一律标注「未做」。
- 不覆盖手感（拖拽跟手程度、滚动惯性）与声音。
- 贴边直角（导航标签左缘 0 + 其余角 14px）是否算「圆角不统一」需产品裁定；脚本单列为
  `radius-edge-attached-reported`，不计入 `radius-uniform-token` 违规。
- 部分检查依赖产品提供稳定钩子（`data-settings-key`、`data-settings-action`、
  `data-settings-fold-toggle`、`data-nav-layout`）；缺失时按回退选择器尽力读取，
  并在 `structure-hook-present` 里明确报出，不会假装通过。

---

## 9. 集成分支复跑（2026-10-28，qio-stg @ 06ee492f）

- 目标：D 的脚本对集成分支（A 壳 + B 模型面板 + C 调用/搜索）重跑，验证占位消失后的真实面板。
- 证据：docs/verification-shots-settings/integration-06ee492f/（截图 + report.json / report.md + interaction-report.json）、
  docs/verification-video-settings/integration-06ee492f/（录屏 + record-report.json）。
- 与 A 壳批次的差异：模型与服务里是 B 的真实面板（Key 接入 / 默认模型 / 12 行用途分配），
  长表单（KeyForm）与中央确认弹窗（撤销密钥）不再跳过；深浅主题各跑四尺寸。
- 新增检查：models-panel-content（Key 接入+默认模型+用途分配齐备、用途行 >= 8）、
  models-ui 交互（真实面板内容 / Key 表单打开与关闭 / 撤销确认层 / 用途候选）。
- 已知边界：读取类接口仍是浏览器侧 fixture；写请求除注入失败/延迟用例外打真实隔离后端；
  Key 表单只打开与断言，不提交；不联网、不用真实 Key。
