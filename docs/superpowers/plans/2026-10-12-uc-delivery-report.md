# QIO 本轮交付报告：统一文字卡 · 持续许可 · 卡片内预览 · 多媒体对话（2026-10-12）

## 0. 基线与分支（可核对）

| 项 | 值 |
| --- | --- |
| 基线标签 | `v0.2.1` |
| 基线 SHA | `65cdd7e3759fe44e12c6802a0cf41777bfc2e086`（`git ls-remote --tags origin v0.2.1` 与本地一致，**未**改用 main）|
| 契约基提交 | `dacc7cb`（分支 `feat/unified-cards-media-preview-021-base`）|
| 集成分支 | `feat/unified-cards-media-preview-021` |
| 四路子分支 | `feat/uc-01-card-core` · `feat/uc-02-preview-core` · `feat/uc-03-media-chat` · `feat/uc-04-layout-preview-ui` |
| worktree | 集成 `D:/qio-dev/q021`；子路 `D:/qio-dev/qL1` · `qL2` · `qL3` · `qL4` |

远端基线与本地一致；本轮**没有**合并 main、**没有**发布版本、**只追加迁移 41 / 42 / 43**（历史迁移一字未改，
append-only 守卫测试通过）。

## 1. 用户使用流程（非技术语言）

### 1.1 写一张文字卡

1. 在互动板面的添加菜单里点「文字卡」（菜单里只有文字卡、文件、图片三种）。
2. 卡片上是一个编辑区。直接写字；选中一段文字后按工具栏，把它变成**加粗 / 斜体 / 行内代码 /
   删除线 / 标题 / 列表 / 引用**；点「插入链接」把你选中的文字变成链接并让你填地址；
   点「插入代码块」插入一对围栏，你在中间写代码（缩进与换行原样保留）。
3. 文字、链接、代码在**同一份内容**里交替出现：写完一段文字、插入一段代码、再回到上面的文字继续改。
   光标移动、选择文字、撤销重做都用编辑器原生能力，不会被代码区困住。
4. 卡片显示时链接显示地址并可点开（只有 http / https / mailto 会真的打开），代码块显示语言并可复制。
5. 整张卡片一起保存、复制、移动、折叠、加书签、连线；草稿会保留，保存失败会说明原因。

### 1.2 许可（这张卡能不能被 QIO 看到）

1. 新建的文字卡**默认没有许可**：这时 QIO 看不到这张卡的任何内容，包括里面的链接地址与代码。
2. 勾选「许可」= 允许 QIO 看这张卡。
3. **许可不会因为提交而失效**：提交成功、失败、重复点、超时、刷新、重启都还在；只有你自己取消（或隐藏卡片）才失效。
4. 提交交给 QIO 的，是这一次确定的那份内容与范围。

### 1.3 在原卡片里看真实文件

1. 文件卡片收起来时显示文件名、类型、状态；图片显示缩略图。
2. 点卡片上的「展开预览」——**这张卡片自己变大**，在卡片里面看；点「收起预览」收回。
3. 能看什么：图片（完整比例，可放大 / 缩小 / 适应）；PDF（一页一页看，保留原页面外观）；
   Word / Excel / PowerPoint（**结构化正文**：段落、标题、列表、表格；Excel 有工作表与单元格地址、
   公式与结果分开；PPT 有每页文字与表格）；代码 / 文本文件（行号 + 语言，只读）。
   旧的 `.doc` / `.xls` / `.ppt` 不能在卡片里看，界面说明原因并保留打开 / 另存入口。
4. 可以同时展开多张；旁边的卡片**临时让开**阅读空间，收起后回到原位置；你在这期间挪过的卡片停在**你挪到的位置**。
5. **打开预览不会让 QIO 去读它**；要 QIO 读，仍然需要许可 + 提交。

### 1.4 在聊天里发图片、视频、音频

1. 对话模式聊天与互动悬浮聊天都可以加附件。
2. 图片：链路能看图就把**真实图片**交给它；不能看就如实说明，不编造画面。
3. 音频：链路能直接听就交给它；否则**尝试转写**成文字（需要你配置了带转写能力的凭据，并且这台机器上有可用的转换程序），
   转写结果带时间段与用了哪个服务。
4. 视频：链路能直接看就交给它；否则**按时间点抽几张画面**（写明抽了哪几个时间点，并明确这是抽样、不是逐帧看完）。
5. 悬浮聊天只发送当前消息与明确选中的附件，不会自动附带板面内容、未提交改动或预览内容。

## 2. 已完成的效果与实现落点

### 2.1 统一文字卡

* 卡片身份不变（同一张 `kind="text"` 卡片，id 不变），内容改为**块序列**（段落 / 标题 / 列表 / 引用 / 代码 / 链接），
  存在 `card.meta.doc`；`meta.docText` 是编辑区文本；`content` 是纯文本派生。
* 前后端同一套规则：`backend/src/agent/interactive/text_card.py` 与 `frontend/src/interactive/textCard.ts`；
  两侧契约测试锁死三条性质：**代码逐字保真**、`serialize(parse(t)) === t`、块序稳定。
* 编辑区：`frontend/src/interactive/TextCardEditor.vue`（单个文本域 + 9 个格式动作，全部走同一份纯函数，
  改完把选区还给用户；不自建撤销栈）。
* 卡片按块渲染；代码块带语言与复制按钮；链接可打开（沿用外链白名单）、可编辑。
* 添加菜单与识别规则收敛为三种：文字卡 / 文件 / 图片；代码**文件**仍属于文件材料（只读预览）。

### 2.2 历史迁移

* `backend/src/agent/interactive/text_migration.py`：旧代码卡 → 一个代码块（正文逐字、语言保留）；
  旧网址卡 → 一个链接块（地址与标题保留）；旧文字注释卡补齐块结构。**逐卡迁移，绝不合并**。
* id / 位置 / 分组 / 关系 / 书签 / 折叠 / 草稿键 / 任务引用保留；迁移在板面状态加载 / 保存层执行一次、**幂等**。
* 可见范围不变：未许可的旧注释迁移后仍然不可见；旧代码 / 网址卡迁移后带许可（它们此前默认在材料范围内）。

### 2.3 持续许可

* `checked` = 整卡许可（覆盖文字、链接、代码）；未许可时**整体不交付**。
* 提交不再清许可：`checkedCleared` 恒为空数组（字段保留以兼容旧前端），直连与幂等重放路径一致。
* 遗留 `code` / `url` 卡在迁移完成前一律不可见（没有默认可见通道）。

### 2.4 卡片内预览

* 后端 `backend/src/agent/services/preview.py` + `backend/src/agent/api/preview_routes.py`（docx / xlsx / pptx / code / pdf 事实）。
* 前端 `frontend/src/components/interactive/CardInlinePreview.vue` + `frontend/src/interactive/renderers/`（动态加载）；PDF 用 pdf.js 逐页。
* 缓存按「附件 + 文件版本 + 配方」；版本变化重算、旧结果不复用；工作线程 + 可取消；超限标截断；失败给原因、不 500。
* 阅读位置按附件 + 版本落库（迁移 42 的 `board_preview_state`）。
* **预览 ≠ 模型已读**：`backend/tests/test_preview_real_files.py` 用同一份真实 .docx 同时给出两条链路的结论。

### 2.5 临时布局

* `frontend/src/interactive/cardLayout.ts` 纯函数：展开卡原位放大、相交卡按最短方向让开（最多 8 轮；仍相交记 displaced）。
* 只影响显示：展开 / 让位 / 收起前后板面坐标与版本号逐字节不变，不产生表达、不触发模型、不暂停任务。
* 减少动画下完全不做过渡动画；展开状态下 Esc 只收起预览。

### 2.6 多媒体对话

* 两处入口共用同一套会话、附件与多媒体处理；悬浮聊天只发当前消息 + 明确选定的附件。
* 能力位 `supports_images` / `supports_audio` / `supports_video` / `audio_transcription`，按**本次运行**的真实链路解析。
* 八种事实 source（见 `docs/interactive-vision-contract.md` §8）；转写带时段与提供方，视频抽样帧带时间位置。
* 转写文字与本轮媒体事实**进入提示词**（`turn_orchestrator.media_facts_note`），不只是第一次理解时带一次。

## 3. 各模型连接方式的真实支持范围

| 链路 | 图片 | 音频（原生） | 视频（原生） | 派生转写 |
| --- | --- | --- | --- | --- |
| OpenAI 兼容 native | **支持**（真实字节 `image_url`） | 不支持 | 不支持 | 配了带 `transcription` 用途标签的凭据才有（`POST /audio/transcriptions`，模型名取自凭据、不硬编码） |
| Anthropic | **支持**（图片内容块） | 不支持 | 不支持 | 无 |
| 文本协议 | 不支持 | 不支持 | 不支持 | 无 |
| 假适配器（测试） | 可声明 | 可声明 | 可声明 | 可声明 |

判定规则：「接口能接受某种消息」**不等于**「所选模型一定能理解」；能力位是唯一事实来源，没有能力就如实降级。

## 4. 各文件格式的实际预览范围

| 格式 | 卡片内预览 | 保真口径 |
| --- | --- | --- |
| png / jpg / jpeg / gif / webp / bmp | 支持 | 完整比例，可放大 / 缩小 / 适应 |
| pdf | 支持 | 原页面外观（pdf.js 逐页渲染），含图片与表格 |
| docx | 支持 | 结构化正文（段落 / 标题 / 列表 / 表格），**不是**逐页原版面 |
| xlsx / xlsm | 支持 | 工作表 + 单元格地址 + 公式与缓存值分开；不计算公式 |
| pptx | 支持 | 幻灯片文本 / 表格 / 图片清单，**不是**原版面渲染 |
| 代码 / 文本 | 支持 | 行号 + 语言 + 等宽，只读，不执行 |
| doc / xls / ppt（旧版） | **不支持** | 说明原因 + 打开 / 另存入口 |
| 其它 | 不支持 | 如实原因 + 打开 / 下载入口 |

## 5. 验证记录

### 5.1 唯一一次全量本地验证

命令：`python scripts/uc_full_verify.py --run`（执行器保证每一项各跑一次并把输出落盘）

| 项 | 命令 | 结果（真实退出码）|
| --- | --- | --- |
| backend-full | `backend/.venv/Scripts/python.exe -m pytest` | **exit 1**：10 failed / 3977 passed / 16 skipped（21 分 04 秒）|
| frontend-typecheck | `npx vue-tsc --noEmit` | exit 0（16.5 秒）|
| frontend-tests | `npx vitest run` | exit 0（137.0 秒）|
| frontend-build | `npm run build` | exit 0（24.8 秒）|
| docs-consistency | `python scripts/check_docs.py` | exit 0（0.3 秒）|

执行 SHA：`8795875`。完整产物（每项的命令、退出码、stdout/stderr 全文）：
`docs/superpowers/plans/uc-full-verify-20261012-235634/`（`summary.json` + 5 个逐项日志）。

口径说明（避免把过程写成结论）：

* 在此之前还有两次启动**没有跑完**，都不计入唯一一次全量（产物逐项可查）：
  * `uc-full-verify-20261012-232528/` 与 `uc-full-verify-20261013-003412/`：我发现工作树在全量期间又被改过，
    主动中止（产物为空）—— 记录 SHA 与工作树不一致的验证不作数；
  * `uc-full-verify-20261012-233536/`：跑完第一项（后端，exit 1：就是下面那 10 条）后**在第二项崩了** ——
    执行器直接 `CreateProcess("npx")`，而 Windows 上 npx 是 `npx.cmd`（WinError 2），**第二项根本没被执行**。
    修好执行器（提交 `8795875` 的后半部分）后才跑出上表那一轮完整记录。
* 因此**唯一一次完整全量** = `uc-full-verify-20261012-235634/`；上面两次只作为过程记录保留。
* 后端那 10 条失败是**本轮契约变更的真实后果**，逐条定性与定向补验见 §5.2；它们不是被放宽掉的。

### 5.2 失败项定向补验（只重跑失败项，不重跑整套）

只重跑**失败的那 5 个文件**（不重跑整套）：

```
cd backend
.venv/Scripts/python.exe -m pytest tests/test_irm_b_migration_39.py tests/test_frozen_materials_packaging.py \
  tests/test_interactive_integration.py tests/test_irm_b_acceptance_api.py \
  tests/test_irm_c_understanding_lifecycle.py -q
```

结果：**5 个文件全绿**（11 / 7 / 12 / 全部 / 14 条）。逐条定性：

| 失败用例 | 定性（为什么原来的断言不再成立）| 怎么改的 |
| --- | --- | --- |
| `test_irm_b_migration_39.py`（5 条）| 把「补偿入口固定 40」钉死；本轮追加 41/42/43 后必然过期 | 改成守住真正不变的性质：编号有序不重复、**39 仍在**、补偿 = 最新版本、补偿内容包含 39 的对象 |
| `test_frozen_materials_packaging.py`（2 条）| 用**代码卡**当材料；契约 §2.2 之后链接与代码没有默认可见通道 | 改成**许可过的文字卡**（内含代码块），诚实口径的断言一条没减 |
| `test_interactive_integration.py`（1 条）| 断言「提交成功后自动取消勾选」——这条规则已被用户明确作废 | 改成断言**许可持续有效** + `checkedCleared` 恒空 |
| `test_irm_b_acceptance_api.py`（1 条）| 场景前提是「提交成功后板面版本自然前进」，而提交不再改板面，前提消失 | 前提改成「提交之后用户又改了一次板面」；并修 `_prepare` 的并发安全（按当前版本保存）|
| `test_irm_c_understanding_lifecycle.py`（1 条）| 快照里的代码卡不再可见 → 模型输出的对象引用校验判 failed | 快照改成许可过的文字卡 |

全量后又有两类**由 CI 暴露**的问题，修完后同样定向补验（见 §5.3）：

1. 两个 e2e 文件跨测试文件 `from tests.test_media_service import ...` → 抽成 `backend/tests/media_shared.py`；
2. 后端全量项在最终树上**重跑一次**（它是上面失败的那一项），真实退出码记录在
   `docs/superpowers/plans/uc-full-verify-*/` 的后端项里。

### 5.3 唯一一次全量 CI

受控触发方式：开发期四路子分支**全程不 push**（`.github/workflows/ci.yml` 对 push 与 PR 都触发，
从源头避免重复）；集成完成后只 push 集成分支一次。

| 运行 | SHA | 结果 |
| --- | --- | --- |
| CI run **38279449869**（唯一一次完整全量）| `194e574b881ba8c747e9e4dd3f719d531293bed4` | **9/10 job 绿**：rust(ubuntu/windows)、docs consistency、frontend、ci dedup preflight、frozen worker、install e2e、backend(py3.11)、backend(py3.12) 全过；`backend (windows-latest)` 一条**负载敏感的计时**用例判红（见下）|

过程与归因（如实记录，不掩盖）：

* 更早的 run **38278269851** @ `96c57e8`：三个后端 job **在收集期**就失败 ——
  `ModuleNotFoundError: No module named 'tests'`（两个 e2e 文件 `from tests.test_media_service import ...`；
  本机把 `tests` 当命名空间包而侥幸通过，Linux runner 不是）。这是**代码/测试缺陷**，不是环境问题；
  修复提交 `194e574`（抽 `tests/media_shared.py`，用 try/except 兼容两种 rootdir 行为）。
* run 38279449869 的那一条红：`tests/test_interactive_during_heavy_work.py::test_a_cancel_request_is_served_while_slow_prediction_runs`
  断言「取消回调延迟 < 100ms」，Windows runner 实测 219ms。本机同类路径正常；同一文件里其余同类用例**早已**按项目既定口径
  （`loop_stall_budget_ms`：安静机器 100ms 硬线，噪声机器按刺激比例）改成缩放预算，只有这一条还是硬线。
  按「环境失败优先只重试受影响项」的规定，只重跑这一个 job（`gh run rerun --job …`），不重跑其它成功 job。

唯一一次全量 CI 的**结论**（job 级）：除上面列出的 windows 后端单条计时用例外，全部通过；
该条为负载敏感的环境性假红还是真实回归，以单 job 重跑结果为准（下方回填）。


#### 失败归因与修复（含证据链，不是猜的）

1. **重跑同一个 job**（按「环境失败优先只重试受影响项」只派发 `backend (windows-latest)`）：
   这次换了一条计时用例判红 —— `test_a_slow_retrieval_does_not_stall_the_turn_loop`，实测 **249.7ms**，预算 150ms。
2. **该文件本轮一字未改**：`git diff 65cdd7e..HEAD -- backend/tests/test_interactive_during_heavy_work.py` 为空；
   `heavy.py` / `predictor.py` 也未改。
3. **本机同一文件的诊断运行**（把预算临时压到 1ms，让每条用例把自己的实测值打出来）：
   健康探测 50ms、直接慢检索 19ms、**整轮慢检索 61ms**、维护 19ms、维护期取消 11ms。
   这些数字**已经包含**本轮新增的媒体收集代码（它就挂在这一轮的 `build_context` 上），
   而 CI 同一条测到 219ms / 249.7ms —— 差 4 倍，差异在 runner 环境。
4. **该文件自己的文档**记着 windows-latest 的**环境地板 266ms**（2026-10-12 实测），
   而当时的预算只有 150ms —— **预算低于已知地板**，必然假红：这是既有的阈值误标定，不是本轮的功能回归。

修复（`a7d4c95`）：让预算回到「地板 < 预算 < 回归签名」：

* 环境地板上限 100ms → **300ms**（实测 266ms / 250ms；回归签名 = 整轮慢检索 446ms，仍能抓住）；
* 取消延迟那条仍在写死 100ms，一并纳入同一规则；并把它前面的慢推理刺激 0.3s → 0.6s（`SLOW_STIMULUS_SECONDS`），
  **提高信噪比而不是放宽阈值**（回归签名随之从 ~300ms 升到 ~600ms）；
* **确定性判据一条未改**：结果 ok 标志、`slow.searches >= 1`、取消原因 "cancelled" 原样保留。
* 本机复跑该文件：**13 passed / 6 skipped**。

定向验证：`targeted-verify.yml`（dispatch-only，按 SHA 只跑指定单项）派发 `windows` 单项 +
`pytest_node_ids=tests/test_interactive_during_heavy_work.py`，结果回填在下方。

**结果：success。** 派发 run **38287353953**（`--ref feat/unified-cards-media-preview-021`，只跑 `windows` 单项 +
`pytest_node_ids=tests/test_interactive_during_heavy_work.py`）：

* `resolve + assert SHA`：success（工作区实际 HEAD == 派发 SHA `a3dc8ad`，由本地 action 自己断言）；
* `backend (windows-latest)`：**success**（就是全量里判红的那条与那个文件）；
* `summary`：`ran=1 failed=0 cancelled=0`，其余检查项一律 **SKIPPED**，并明确打印
  「SKIPPED 不等于通过，它们根本没被执行」——不是拿跳过凑绿。

这个入口本身在过程中暴露了两个缺陷（都是**入口的**，不是测试的），一并修好：

1. `5c065e1`：每个 job 的第一步就是 `uses: ./.github/actions/verify-checkout`，
   而本地 action 必须先签出仓库才能被加载（鸡生蛋）→ 报 `Can't find 'action.yml'`。
   加一步 `actions/checkout@v5` 引导签出；**SHA 断言逻辑一行未改**。
   （该缺陷不是本分支特有：`targeted-verify.yml` 与 `action.yml` 在 tag 与 main 上逐字相同，
   2026-10-11 的那次派发同样是 failure。建议把这条修复同步回 main。）
2. `a3dc8ad`：`scripts/targeted-verify/verify_sha.py` 在 windows-latest 上打印中文证据行时
   `UnicodeEncodeError`（cp1252）崩溃 —— `ci.yml` 各 job 早就设了 `PYTHONIOENCODING: utf-8`，这个本地 action 没设。已补。

还有一个使用陷阱值得写下来：`gh workflow run … -f ref=<branch>` 里的 `-f ref` 只是**输入**，
工作流文件本身取自 `--ref`（默认 default branch）。少了 `--ref`，跑的仍是 main 上的旧工作流。

#### 后端全量项的重跑（它是上面失败的那一项）

* 命令：`python scripts/uc_full_verify.py --run --only backend-full`（执行器捕获**真实退出码**，不是管道的）
* 结果：**exit 0；3987 passed / 16 skipped（22:00）**
* 产物：`docs/superpowers/plans/uc-full-verify-20261013-014023/01-backend-full.txt`（含真实退出码与耗时）
* 这证实了：全量那一轮后端那 10 条失败**已经全部修好**，且没有把别的用例带红。
* 之后又改动的文件都属于「已单独复验」的三类（见下节表格）：一个测试文件（单独复跑 13 passed / 6 skipped）、
  `.github/` 入口（后端测试不读 `.github`，`grep` 0 命中）、文档与验证产物（`check_docs.py` 复跑通过）。

## 6. 故障与取消的处置（验收情境之一）

* 预览：收起 / 换附件 / 卸载时中止在途 fetch 与渲染（AbortController + 代次），释放 blob URL、canvas、pdf 文档；
  迟到结果丢弃（组件测试覆盖两个不同卡片的连续切换 + 旧卡响应后到）。
* 媒体：准备失败保留用户输入与可重试入口；已移除附件用 tombstone 挡住晚到结果（不复活）。
* 转换：可取消、按版本缓存，旧版本结果绝不写到新版本。
* 许可：旧回执不可能把用户更晚的手动取消恢复成开启（store 层测试锁死）。

## 7. 尚未通过真实模型验证的部分（不粉饰）

1. **真实模型 API 的联网验收不在自动化范围内**：图片 / 音频 / 视频链路的协议形状、降级路径、交付事实与
   「转写文字进请求」都由 fake / mock provider 覆盖；具体某个模型看图 / 听音的效果本轮没有实测。
2. **没有真实转写凭据的实机验收**：转写通道的凭据解析与请求形状有测试（假 HTTP 客户端），但没有用真实凭据跑过一次真转写。
3. **派生处理依赖本机转换程序**：本开发机 PATH 上有 ffmpeg（媒体用例真跑）；干净安装版若没有随包提供 ffmpeg，会如实降级。
4. **没有真实浏览器 / 安装版界面的视觉走查**：本轮按任务书只做定向测试 + 类型检查 + 构建（组件与布局用 jsdom 覆盖）。
5. **原生撤销重做**只在真实文本域的行为层面断言（jsdom 没有撤销栈），没有端到端验证。
6. 旧版 Office 不内嵌预览；docx / xlsx / pptx 是结构化预览而非原版面渲染。

## 8. 四个结论必须分开看（用户明确要求）

| 结论 | 由什么决定 | 界面 / 接口怎么表达 |
| --- | --- | --- |
| 附件已保存 | 附件服务的 state | 已保存 / 准备中 / 失败 + 原因 |
| 预览可用 | `GET /api/attachments/{id}/preview` 的 status | ready / failed / unsupported + 原因 |
| 内容已处理 | 材料条目的 `read` + `source` | 八种 source + 时段 / 时间点 / 引擎 |
| 已交付模型 | 内容块或提示词里真的带上了 | 图片 / 音频 / 视频字节；转写文字；抽样帧 |
| 回答已完成 | 模型返回并落库 | 回答文本；**不代表理解正确** |
