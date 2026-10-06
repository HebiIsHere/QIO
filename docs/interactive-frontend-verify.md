# 互动板前端改版：独立验收报告（子智能体 D）

- 验收对象：**集成提交 `18736a2d930b40453f6b07a7cbee7325244b6964`**（分支 `feat/interactive-frontend-layout`）。
- 独立工作区：`D:\qio-dev\qio-fe-d-verify`（分支 `wt/fe-d-verify`，从上面那个提交新建，**不是**开发者的工作区）。
- 验收方式：亲自读源码 + 亲自起真实应用（后端 8896 / 前端 5396，独立数据目录 `%TEMP%\qio-d-verify`）
  + 真实 Chrome 探针（`scripts/visual_probe_d.mjs`，独立 CDP 端口 9444 与独立 profile）。
- **验收阶段没有改动产品代码**：本报告里的所有结论都来自未修改的 `18736a2`；为定位问题做过一次**本地实验补丁**，
  已在实验后 `git checkout` 还原（见「问题 1」）。
- 证据目录：`%TEMP%\qio-visual\shots`（本报告引用的截图都带 `dv-` 前缀）；探针步骤：`scripts/interactive-verify/steps-dv-*.json`。

## 0. 结论速览

| 场景 | 结果 | 证据类型 |
| --- | --- | --- |
| 同批 3 项不出现批量列表 | 通过 | 亲自复现 |
| 同批 4 项出现且默认收起 | 通过 | 亲自复现 |
| 不同批次 3+1 不误触发 | 通过 | 亲自复现 |
| 部分批量只影响选中项（未选中的继续等待） | 通过 | 亲自复现 |
| 影响确认「取消」：改动不生效 + 任务继续 | 通过 | 亲自复现 |
| 影响确认「确认」：改动生效 + 相关任务暂停 | 通过 | 亲自复现 |
| 确认框焦点限制（打开聚焦取消、Tab 回卷、Escape 取消） | 通过 | 亲自复现 |
| 未勾选注释不进入允许查看范围（勾选前 / 勾选后 / 再取消） | 通过 | 亲自复现 |
| 未完成拖动不成为正式改动 | 通过 | 亲自复现 |
| 平移后坐标仍准确（板面坐标不变、屏幕位移与滚动一致） | 通过 | 亲自复现 |
| 重叠「松开后合并成组」提示 + 松手才成组 + 默认组名 | 通过 | 亲自复现 |
| 组名留空保留默认名 | 通过 | 亲自复现 |
| 聊天只发文字、不带板面、不调提交接口；草稿保留 | 通过 | 亲自复现 |
| 窄窗口 800×600：主要入口与提交区可见可点 | 通过 | 亲自复现 |
| 两种主题（深色 / 浅色） | 通过（只验证可用与对比，未做像素级审美判断） | 亲自复现 |
| 恢复：重开不自动继续、暂停保持暂停 | 通过（暂停态；running→paused 的**跨进程**降级未复现） | 亲自复现 + 部分未验证 |
| **批量列表面板在 800×600 / 1024×768 遮挡底部工具栏** | **不通过（阻断）** | 亲自复现 |
| 单项审批浮条「贴着预览」定位 | 部分未验证（当前数据下无 running 意图 → 走兜底居中；兜底位置实测在窗口内） | 亲自复现 + 未能验证 |
| 空格框选 / 滚轮缩放后坐标准确 | 未验证（本轮只复现了平移；缩放与框选留给 B 的专项验收） | 没能验证 |

### 阻断（必须修）

1. **批量列表面板会盖住底部工具栏（含提交区）**——800×600 重叠 143px、1024×768 重叠 392px。
   违反契约 §8.1「入口与提交区必须始终可见可点」与 Lead 的窄窗口要求。复现步骤与定位见「问题 1」。

### 重要（应修）

2. **同一个「容器高度」写法在集成版里两处不一致**：`IntentBatchTray` 面板用
   `max-height: calc(100% - Npx)`（浮层高度由内容决定 → 百分比解析不成高度，等于没生效）；
   而 `ChatDock` 用的是「自己量高度 + 行内 px」。建议统一成后者（问题 1 给了可直接用的补丁方向）。
3. **`--im-toolbar-clearance` 在集成版里没有生产者**：`interactive-shell.css` 定义了 `--im-toolbar-*` 变量，
   但没有 `--im-toolbar-clearance`；`IntentBatchTray` 的兜底变量因此永远是默认值 120px。
   （不影响功能，但让「避让」看起来像生效了，实际没有。）

### 次要

4. **两个 `role="toolbar"` 元素**（`data-im="card-toolbar"`「代码操作」与 `data-im="board-toolbar"`「板面操作」）
   都在页面上；按契约 §8.4.1 卡片局部工具栏属于设计的一部分，这里只是提醒：任何「取工具栏顶边」的逻辑
   都必须像本报告这样**取最下面那条**，否则会取到卡片工具栏（实测它在 1440×900 时 top=77）。
5. **1024×768 下底部工具栏会变成近满高（top=47 → bottom=756）**：由 `interactive-shell.css`
   `@media (max-width: 1024px)` 的 `max-height: 70vh` + 内容换行造成。不是本次改版新增的缺陷，
   但它让「工具栏上方」的可用空间变得很小（约 47px），修问题 1 时要把这个档位一起测。

## 1. 怎么跑的（可复现）

```powershell
# 独立工作区
git -C D:\qio-dev\qio-fe-d worktree add -b wt/fe-d-verify D:\qio-dev\qio-fe-d-verify 18736a2
cmd /c mklink /J D:\qio-dev\qio-fe-d-verify\frontend\node_modules D:\qio-dev\qio-wt-fixes\frontend\node_modules
cmd /c mklink /J D:\qio-dev\qio-fe-d-verify\backend\.venv D:\qio-dev\qio-wt-fixes\backend\.venv

# 后端（独立数据目录）
$env:QIO_DEV_INSECURE="1"; $env:QIO_DATA_DIR="$env:TEMP\qio-d-verify"
$env:PYTHONPATH="D:\qio-dev\qio-fe-d-verify\backend\src"
cd D:\qio-dev\qio-fe-d-verify\backend
uv run --frozen python -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8896

# 前端（共享验收配置，已设 root）
$env:VITE_QIO_BACKEND_URL="http://127.0.0.1:8896"
cd D:\qio-dev\qio-fe-d-verify\frontend
node node_modules/vite/bin/vite.js --config vite.e2e.config.ts --port 5396 --host 127.0.0.1

# 探针（独立端口与 profile，不抢别人的 9333）
cd D:\qio-dev\qio-fe-d-verify
node scripts/visual_probe_d.mjs "@scripts/interactive-verify/steps-dv-2.json"
```

**测试基线（我自己在这个提交上跑的）**
- `cd frontend; npx vue-tsc --noEmit` → 无输出（TSC_EXIT=0）。
- `cd frontend; npx vitest run` → **112 文件 / 1162 用例全绿**。
- `cd backend; uv run --frozen pytest` → **2107 passed, 9 skipped**（退出码 0）；
  其中 `tests/test_trace_phases.py` 单跑 3 次全绿——首次全量跑时它失败过一次
  （`test_phase_timer_tiles_the_timeline_with_explicit_other`），单独复跑不复现，
  按「疑似与本次前端改版无关的偶发」记录，没有深挖。

---
## 2. 问题清单（按严重度）

### 问题 1（阻断）批量列表面板遮挡底部工具栏

**我做了什么**：在集成提交上，用探针把批量入口展开，然后分别把视口设成 800×600 与 1024×768，
量面板底边与 `[data-im="board-toolbar"]` 顶边。

**实际输出**（原始值）

    viewport 800×600 : panel {top:97, bottom:431}   toolbar {top:288}   overlap = 143px
    viewport 1024×768: panel {top:105, bottom:439}  toolbar {top:47}    overlap = 392px
    entryVisible=true, submitVisible=true, overflowX=false

**结论**：面板与工具栏重叠，工具栏中部（含提交区所在的右端）被面板盖住；点得到不等于看得见。
截图：`dv-20-integrated-overlap-800.png`、`dv-21-integrated-overlap-1024.png`。

**根因（读源码 + 实测确认）**：`IntentBatchTray.vue` 里

    .batch-tray { position: absolute; top: var(--sp-4); … }        /* 高度由内容决定，没有确定高度 */
    .panel { max-height: min(60vh, calc(100% - 204px - var(--sp-4))); }

行内 `max-height` 里的 `100%` 相对的是 `.batch-tray` 的高度，而它是绝对定位、没有 `height`/`bottom`，
百分比解析不成确定高度 → 这个约束**等于没生效**；剩下的 `60vh` 在 600px 高窗口下是 360px，
面板自然高度 334px < 360px，所以面板根本不会被压到工具栏上方。

**为什么 `onMounted` 量的 clearance 没救回来**：量是量对了（800×600 时算出的 328px 写进了行内样式，
实测 `style="max-height: min(60vh, calc(100% - 328px - var(--sp-4)))"`），但公式本身解析不成高度。

**我试过、且**部分验证**的修法（本地实验补丁，已还原，未提交）**：给浮层一个确定高度，让面板在浮层里收缩滚动——

    .batch-tray { position: absolute; top: var(--sp-4);
                  bottom: calc(<量到的 clearance>px + var(--sp-4)); overflow: hidden; }
    .panel { max-height: 100%; flex: 0 1 auto; min-height: 0; overflow: auto; }

实验后 800×600 实测：`panel {top:97, bottom:256}`、`toolbar {top:288}`、**overlap = 0**、
`entryVisible=true`、`submitVisible=true`、`panelScrollable=true`、`overflowX=false`
（截图 `dv-17-narrow-fixed-800.png`）。1024×768 档位在实验里还出现面板被压得过矮（约 26px）的情况，
需要配合「工具栏 70vh 档位」一起调，**我没有把它调到满意就还原了**，所以这一条只给出方向，不声称已修好。

**复现步骤**

1. 起 8896/5396，打开 `http://127.0.0.1:5396/#/interactive`。
2. 用演示入口生成 4 项待审批意图（同一次创建 → 同一批）。
3. 点右上「批量审批」入口展开列表。
4. 把窗口缩到 800×600（或 1024×768），观察面板底部与底部工具栏：面板盖住工具栏。
5. 自动化等价步骤：`scripts/interactive-verify/steps-dv-15.json`（原始值见上）。

### 问题 2（重要）`--im-toolbar-clearance` 没有生产者

**我做了什么**：在集成版全仓搜这个变量。

**实际输出**：`IntentBatchTray.vue` 里用了 `var(--im-toolbar-clearance, 120px)`；
`frontend/src/styles/interactive-shell.css` 里只有 `--im-toolbar-bottom` / `--im-toolbar-max-width`，
没有 `--im-toolbar-clearance`。

**结论**：兜底变量永远取默认 120px。只读代码推断（不是复现出来的故障），但会让后来的人误以为避让已生效。

### 问题 3（次要）取工具栏顶边时必须取「最下面那条」

**实际输出**（1440×900）：`[role="toolbar"]` 有两个——`card-toolbar`「代码操作」top=77、
`board-toolbar`「板面操作」top=712。**结论**：任何「面板不许越过工具栏」的实现都必须取 top 最大的那条，
否则会以卡片局部工具栏为界（会把面板压得极小）。只读代码推断 + 实测数据。

---
## 3. 逐场景记录（我做了什么 + 实际输出 + 结论）

### 3.1 同批 3 项不出现批量列表（亲自复现）

**做了什么**：先造 4 项同批待审批（演示入口，同一次创建 → 批次记录来自 store 的 `recordIntentBatch`），
再用界面批量入口只勾第 4 项、点「批量拒绝」，让这一批只剩 3 项。

**实际输出**：`{"entry":false,"list":false}`，批量入口从 DOM 里消失。

**结论**：通过。阈值按「同一批等待审批 ≥4」算，3 项不出列表。
截图 `dv-8-three-no-entry.png`；步骤 `steps-dv-6.json`。

### 3.2 同批 4 项出现且默认收起（亲自复现）

**实际输出**：

    收起态：{"entry":true,"list":false,"entryText":"批量审批这一批待审批 4 项展开"}
    展开后：{"list":true,"items":4,"approve":"批量批准（0）","reject":"批量拒绝（0）"}

**结论**：通过。入口默认收起（列表不在 DOM 里），点击才展开；展开前两个批量按钮都是 0 项、不可点。
截图 `dv-1-four-collapsed.png`、`dv-2-four-expanded.png`。

### 3.3 不同批次 3+1 不误触发（亲自复现）

**做了什么**：同一份 4 项待审批数据，把批次记录改成 `session:a`×3 + `session:b`×1，刷新页面。

**实际输出**：改前（同一批 4 项）`{"fourEntry":true,"text":"批量审批这一批待审批 4 项展开"}`；
改后（3+1 两批）`{"entry":false,"list":false}`。

**结论**：通过。不同批次不累加。
截图 `dv-9-3plus1-no-entry.png`；步骤 `steps-dv-7.json`。

### 3.4 部分批量只影响选中项（亲自复现）

**做了什么**：展开批量列表，只勾第 1、3 项，点「批量批准」。

**实际输出**：

    勾选后：{"checked":[true,false,true,false],"approve":"批量批准（2）","reject":"批量拒绝（2）",
             "summary":"已选 2 项；其中可直接批准 2 项；未选中的 2 项继续等待。"}
    提交后：{"notice":"批量批准：批准 1 项，拒绝 0 项；未选中的继续等待。
             未成功 1 项（与已批准且尚未撤回的「［演示］把材料归为一组并给出对比摘要」互不相容：不能同时批准。）。"}
    服务端状态（该批 4 项）：["running","pending","waiting_dependency","pending"]

**结论**：通过。未选中的 2 项仍是 `pending`；选中项里不能批准的 1 项被服务端按「互不相容」挡下并原样显示原因，
前端没有自己下结论。截图 `dv-3-partial-selection.png`、`dv-4-after-partial-approve.png`。

### 3.5 影响确认框：取消路径（亲自复现）

**做了什么**：先造一个执行中任务（它的 `materialRefs` 指向板面上的 file / code 卡片），
再**通过界面**选中那张材料卡片 → 卡片局部工具栏「编辑」→ 改文字 → 「完成编辑」，等自动保存触发预判。

**实际输出**：

    弹框：{"dialog":true,"title":"这次改动还没有生效：它会影响正在执行的任务",
          "items":["任务：［演示］把材料归为一组并给出对比摘要 | 受影响材料：文件「（D 独立验收）改过的材料 A」 |
                    后果：继续保存会让这项任务暂停并保留当前进度；取消则不改动板面，任务继续。暂停后不会自动继续，需要你确认。"],
          "focused":"impact-cancel"}
    Tab：{"afterTab":"impact-continue"} → {"afterTab2":"impact-cancel"}   （焦点在框内回卷）
    Escape 之后：{"dialog":false,"savedContent":"待补充文件说明","running":1}

**结论**：通过。改动在确认前**没有落库**（服务端仍是旧内容），取消后任务继续执行，其他独立任务不受影响。
截图 `dv-5-impact-shown.png`、`dv-6-impact-cancelled.png`；步骤 `steps-dv-4.json`。

### 3.6 影响确认框：确认路径（亲自复现）

**做了什么**：同样改一次材料，弹框后点「继续：改动生效，相关任务暂停并保留进度」。

**实际输出**：

    {"dialog":false,"savedContent":"（D 独立验收）改过的材料 B",
     "statuses":[…,"paused","pending","pending","pending"]}

**结论**：通过。改动生效（服务端内容变成新值），相关任务从 `running` 变 `paused`，
同一时刻另外三项待审批意图仍是 `pending`（独立任务不受影响）。
截图 `dv-7-impact-confirmed.png`；步骤 `steps-dv-5.json`。

### 3.7 未勾选注释不进入允许查看范围（亲自复现）

**做了什么**：加两条文字注释，只给第一条写文字（不勾选），读服务端 `GET /visible-range`；
再通过界面勾选第一条，重读；再取消勾选，重读。

**实际输出**：

    未勾选：{"cards":[file, code],"notVisibleCount":2}            （注释文字完全不在返回里）
    勾选后：{"cards":[file, code, text「（D 独立验收）未勾选的注释文字」],"notVisibleCount":1}
    再取消：{"cards":[file, code],"notVisibleCount":2,"leak":false}

**结论**：通过。可见范围由服务端从已保存状态推导，前端塞不进未勾选卡片；
`leak=false` 表示整个返回体里搜不到那条注释文字。
截图 `dv-10-unchecked-comment.png`、`dv-11-checked-comment-visible.png`；步骤 `steps-dv-8.json`、`steps-dv-9.json`。
**未验证**：「删除路径」（删除未勾选注释后是否仍然不泄露）本轮没做，留给 B 的专项。

### 3.8 未完成拖动不成为正式改动（亲自复现）

**做了什么**：对 code 卡片发 `pointerdown` → 连续 `pointermove`（不松手），读服务端状态；
再按 Escape 中断，再读。

**实际输出**：

    拖动中：{"savedDuringDrag":{"x":344,"y":60},"hint":"拖动"}     （板面状态未变）
    Escape 后：{"afterEscape":{"x":344,"y":60}}                    （回到操作前位置）

**结论**：通过。拖动期间只有预演，中断后位置与操作前一致。
截图 `dv-12-dragging.png`、`dv-13-drag-cancelled.png`；步骤 `steps-dv-10.json`。

### 3.9 平移后坐标仍准确（亲自复现）

**做了什么**：记录卡片板面坐标与屏幕坐标；在空白处按住拖动 180×120 做平移；再读两者。

**实际输出**：

    平移前：{"board":{"x":60,"y":60},"screen":{"x":60,"y":137}}
    平移后：{"boardUnchanged":{"x":60,"y":60},"screenAfter":{"x":-120,"y":17},"scroll":{"left":180,"top":120}}

**结论**：通过。屏幕位移 = 滚动位移（60−180=−120、137−120=17），板面坐标不变；
平移只改变查看方式，不形成改动。截图 `dv-22-panned.png`；步骤 `steps-dv-16.json`。
**未验证**：空格框选与滚轮缩放后的坐标（本轮没有复现），留给 B 的专项验收。

### 3.10 重叠成组：提示、松手才成组、默认组名（亲自复现）

**做了什么**：把第一张卡拖到第二张卡上（不松手）→ 读页面提示与服务端组列表 → 松手 → 再读。

**实际输出**：

    拖动中：{"hint":"松开后合并成组"}   服务端：{"groupsBefore":[]}
    松手后：{"groups":[{"name":"组 1","defaultName":true,"members":2,"ordered":false}]}

**结论**：通过。拖动期间只提示；松手才成组；组名是默认名「组 1」且可提交。
截图 `dv-23-merge-hint.png`、`dv-24-merged.png`；步骤 `steps-dv-17.json`。

### 3.11 组名留空保留默认名（亲自复现）

**做了什么**：点组名进入编辑，清空输入框、回车、失焦。

**实际输出**：`{"groups":[{"name":"组 1","defaultName":true,"members":2}]}`（组仍成立，名字保持默认）

**结论**：通过。截图 `dv-26-group-name-blank.png`；步骤 `steps-dv-19.json`。

### 3.12 聊天只发文字、不带板面、不调提交接口；草稿保留（亲自复现）

**做了什么**：在页面里给 `window.fetch` 装了记录器，只记 `/api/interactive` 的调用；
打开右下聊天、输入一句中文、点发送；然后输入不发送、收起面板、再展开。

**实际输出**：

    发送期间记录到的板面接口调用：["GET /boards/board_default/state"]      （没有任何 submissions / state 写入）
    seqBefore=14 → seqAfter=14
    面板自述：「QIO 对板面的理解与工具执行尚未接入；这里和对话页共用同一个会话，只发送输入的文字；
              不带板面、未提交改动、注释或选择范围，也不会调用板面提交接口」
    草稿：收起再展开后 {"draftAfterReopen":"（D 验收草稿）未发送的文字"}

**结论**：通过。发送只走会话，不碰板面提交；收起/展开不丢草稿。
截图 `dv-27-chat-send.png`、`dv-28-chat-draft.png`；步骤 `steps-dv-20.json`。
**未验证**：真实模型回复（第一阶段没有接入，界面自己也这么写）；断网发送的失败文案（Lead 还在查，我没重复做）。

### 3.13 窄窗口与两种主题（亲自复现）

**实际输出**（800×600，浅色主题 `data-theme="light"`）：

    {"theme":"light","bodyBg":"rgb(252, 252, 251)","overflowX":false,
     "toolbarVisible":true,"submitVisible":true}

**结论**：通过——主要入口（批量入口、工具栏、提交按钮）在 800×600 下都可见可点，无横向溢出；
浅色主题生效（背景与面板取到浅色值）。截图 `dv-30-narrow-light.png`。
**但**：批量列表展开时仍然会遮挡工具栏 → 见「问题 1」（阻断）。

### 3.14 恢复行为（亲自复现 + 部分未验证）

**做了什么**：让一项意图进入 `running`（改材料会让它暂停，所以另起一次），刷新页面，比较前后状态。

**实际输出**：

    刷新前：{"running":0,"paused":1}
    刷新后：{"afterReload":{"running":0,"paused":1}}

**结论**：通过（暂停保持暂停，重开不自动继续、不自动提交）。
**未验证**：跨进程的 `running → paused` 降级（需要「另一个实例遗留的 running」，本轮没有构造），
以及草稿/快照在重启后的恢复——这两条属于 B 的专项验收范围，我这边不声称结论。
截图 `dv-29-recovery.png`；步骤 `steps-dv-21.json`。

### 3.15 单项审批浮条（部分未验证）

**做了什么**：打开「任务」浮层，量浮条（`data-im="intent"][data-docked="1"]`）与任务浮层的屏幕矩形，
并尝试找它对应的板面预览节点。

**实际输出**：

    {"popover":{"x":1044,"y":114,"w":380,"h":258},
     "strip":{"x":540,"y":708,"w":360,"h":168},"preview":null,"within":true}

**结论**：浮条在窗口内、不与任务浮层重叠（这是兜底居中的结果）。
**没能验证**：「贴着预览定位」——当时板面上没有 `running` 意图（唯一一项被材料变更暂停了，
而契约 §8.1 只把 pending/needs_update/waiting_dependency/waiting_confirm/running 画成板面预览），
所以预览节点取不到，浮条按设计退回板面下沿居中。锚定分支没有在集成提交上复现成功。
截图 `dv-31-docked-strip.png`；步骤 `steps-dv-22.json`。

---
## 4. 明确区分：亲自复现 / 只读代码推断 / 没能验证

**亲自复现（有原始输出与截图）**
- 3.1–3.13 全部；3.14 的「暂停保持暂停」；问题 1 的重叠数值；问题 3 的工具栏实测数值。

**只读代码推断（没有构造出对应故障）**
- 问题 2（`--im-toolbar-clearance` 没有生产者）。
- `max-height: calc(100% - Npx)` 在绝对定位浮层里解析不成高度——这条既是推断也在问题 1 里用实测数值印证。
- 契约 §8.5 的三条来源优先级：我用单元测试与 localStorage 直接构造验证了「会话记录 > submissionId > 创建秒 > 自成一批」，
  但没有在真实界面里构造出「服务端 submissionId 相同、前端没有会话记录」的场景。

**没能验证**
- 空格框选 / 滚轮缩放后的坐标准确性（只复现了平移）。
- 未勾选注释的**删除路径**。
- 跨进程 `running → paused` 降级、草稿/快照的重启恢复。
- 单项审批浮条的「贴着预览」锚定分支（数据条件不具备）。
- 真实模型回复与工具执行（第一阶段本就没有接入）。
- 像素级视觉质量（间距、字体三声部是否符合设计规范）——我只核对了「颜色只用 var(--*)、
  状态有文字说明、无横向溢出」这些可量化的部分，审美判断不在这里下结论。

## 5. 给 Lead 的建议（不含改动）

1. 先修问题 1（阻断）。修的时候把三个档位一起测：1440×900 / 1024×768（工具栏 70vh 档）/ 800×600（工具栏 60vh 档）；
   建议直接沿用 `ChatDock` 那套「量高度 + 行内 px + 自己滚动」，两个浮层保持一致，别再引入百分比。
2. 要么给 `--im-toolbar-clearance` 一个生产者（页面壳量一次写进 `:root`），要么把变量删掉只留 JS 兜底，
   避免「看起来有避让、实际没有」。
3. 修完问题 1 后建议重跑一次本报告的 `steps-dv-15.json`（重叠数值应变成 0）与 `steps-dv-13.json`（窄窗口三档）。

## 6. 复现材料清单

| 文件 | 用途 |
| --- | --- |
| `scripts/interactive-verify/steps-dv-1.json` … `steps-dv-38.json` | 本报告各场景的探针步骤 |
| `scripts/visual_probe_d.mjs` | D 的独立探针（Chrome）：CDP 9444 + 独立 profile |
| `scripts/visual_probe_d3.mjs` | 第 7–8 节用的探针（**Edge 驱动**）：CDP 9666 + 独立 profile |
| `%TEMP%\qio-visual\shots\dv-*.png` | 截图证据（dv-1…dv-55） |

关键截图对照：

| 截图 | 说明 |
| --- | --- |
| `dv-1-four-collapsed.png` / `dv-2-four-expanded.png` | 同批 4 项：入口默认收起 → 展开 |
| `dv-3-partial-selection.png` / `dv-4-after-partial-approve.png` | 只勾 2 项 → 批量批准只影响选中项 |
| `dv-8-three-no-entry.png` / `dv-9-3plus1-no-entry.png` | 同批 3 项、不同批 3+1：都不出现入口 |
| `dv-5-impact-shown.png` / `dv-6-impact-cancelled.png` / `dv-7-impact-confirmed.png` | 影响确认的弹出、取消、确认 |
| `dv-10-unchecked-comment.png` / `dv-11-checked-comment-visible.png` | 未勾选不进可见范围 / 勾选后才进 |
| `dv-12-dragging.png` / `dv-13-drag-cancelled.png` | 拖动只预演 / 中断回到原位 |
| `dv-22-panned.png` | 平移后板面坐标不变 |
| `dv-23-merge-hint.png` / `dv-24-merged.png` | 松开后合并成组提示 / 松手才成组 |
| `dv-27-chat-send.png` / `dv-28-chat-draft.png` | 聊天只发文字 / 草稿保留 |
| `dv-20-integrated-overlap-800.png` / `dv-21-integrated-overlap-1024.png` | 问题 1：批量面板遮挡底部工具栏（18736a2） |
| `dv-32/33/34-fix-*.png` | b005a45 复验三档 |
| `dv-43-1024-after-fix.png` / `dv-45-1024-toolbar.png` | 2deb3d6：1024 工具栏恢复正常 |
| `dv-46-800-chat-after-fix.png` | 2deb3d6：800 聊天面板仍被挤出视口（已由 72ef426 修好） |
| `dv-51-800-chat-final.png` / `dv-53-1024-chat-final.png` / `dv-54-1440-final.png` | 72ef426 三档聊天面板 |
| `dv-52-800-both-final.png` / `dv-55-800-overlap-detail.png` | 800 同时打开两浮层的重叠观察 |

---

**报告人**：子智能体 D（`fe-d-approval`）。
**第 1–5 节验收对象**：集成提交 `18736a2`；**第 6–7 节**：修复 `b005a45` / `2deb3d6`；**第 8 节**：修复 `72ef426`。
**本报告只报告、未改产品代码**；验收工作区 `wt/fe-d-verify` 上除了本文件与探针步骤，没有其它改动。

## 7. 窄窗口两处修复复验（提交 `2deb3d6`）

**复验对象**：`2deb3d6`（基线 `b005a45`）。**工作区**：`D:\qio-dev\qio-fe-d-verify`（`wt/fe-d-verify` 已前移到该提交）。
**探针**：`scripts/visual_probe_d3.mjs`（**用 Edge 驱动**，独立 CDP 端口 9666 与独立 profile；
原因：本轮复验时本机 Chrome 已被大量 headless 实例拖垮——12 个 chrome 进程、CPU 91%，
`Emulation.setDeviceMetricsOverride` / `Runtime.evaluate` 全部超时，about:blank 也不可达；
Edge 与 Chrome 同为 Chromium 内核、同一套 CDP，几何测量口径一致）。

### 7.1 1024×768：工具栏被提交区撑高（已修好）

    指标                     修复前（b005a45 实测）   修复后（2deb3d6 实测）   结论
    board-toolbar 高度        709px                    236px                   已修好
    .texts 宽度               68px（中文逐字竖排）      220px                   已修好
    .submit-cluster 宽度      533px                    546px（独占一行）        已修好
    提交按钮                  {w:116,h:41}             {w:116,h:41} 可见        正常
    批量面板 maxHeight        140px                    398px                   恢复正常
    批量面板矩形              {105,245,140}            {105,439,334}            恢复正常
    批量面板与工具栏重叠       —                        0                        无重叠
    横向溢出                  无                       无                       正常

截图 `dv-43-1024-after-fix.png`、`dv-45-1024-toolbar.png`；步骤 `steps-dv-32.json`、`steps-dv-33.json`。

### 7.2 800×600：提交区与批量面板（正常）

    提交按钮 {w:106,h:65,top:518,bottom:583} 可见；横向溢出 无；
    批量面板 {top:97,bottom:266} 与工具栏 {top:283} 不重叠；与聊天面板也不重叠。

截图 `dv-46-800-chat-after-fix.png`、`dv-47-800-both-after-fix.png`。

### 7.3 800×600：聊天面板仍被挤出视口（**未修好**，重要）

**实测**（`steps-dv-34.json` / `steps-dv-35.json`）：

    .chat-dock   {top:-141, bottom:259, h:400, bottomCss:"341px"}   --chat-dock-clearance: 325px
    .panel       {top:-141, bottom:22,  h:163, margin:"0px 0px 190px"}   --chat-panel-max-h: 163px
    .toggle      {top:220, bottom:259, h:39}
    toolbar      {top:283}
    面板顶部可见高度 = 22px（其余 141px 在视口上方）

**Lead 的改法生效了一半**：面板高度确实从 220px 降到了 163px（`max(160, 600−325−112)=163`），
但**面板仍被顶出视口**，因为面板上还挂着一条 **190px 的下外边距**：

    frontend/src/styles/interactive-shell.css:110   --im-chat-lift: 190px;      （@media (max-width: 800px)）
    frontend/src/styles/interactive-shell.css:73    margin-bottom: var(--im-chat-lift);

于是容器高度 = 163（面板）+ 190（margin）+ 39（入口按钮）+ 8（gap）= 400px，
而工具栏上方只剩 283px → 按 bottom 对齐后顶部到 −141px。
**这是「两套避让叠加」**：ChatDock 自己已经按实测工具栏高度定位（`--chat-dock-clearance`），
A 的 shell 又用 `--im-chat-lift` 把面板整体抬高一次；1440×900 下 `--im-chat-lift: 0px` 所以看不到问题
（实测 1440：panel{top:121,bottom:641}、margin:0px、完整可见、与工具栏不重叠，截图 `dv-50-1440-chat.png`）。

**建议的修法（一行）**：在 `interactive-shell.css` 的窄窗口断点里把 `--im-chat-lift` 设成 `0px`
（文件注释第 43 行本来就写了「C 若已在自己的面板里按实测工具栏高度避让，可以把 --im-chat-lift 设为 0」），
即 `@media (max-width: 800px)` 与 `@media (max-width: 1024px)` 两处都改成 0；改完容器高度会回到约 210px，可完整放进 283px。
我没有改产品代码（只报告）。

### 7.4 1440×900 回归（通过）

    board-toolbar {w:786,h:172,top:712,bottom:884}     与修复前一致，没有变高
    .texts {w:282,h:99}  提交按钮 {w:116,h:41,top:805} 可见
    聊天面板 {top:121,bottom:641,h:520} 完整可见、与工具栏不重叠、无横向溢出

截图 `dv-44-1440-regression.png`。

### 7.5 三档汇总（本轮）

| 档位 | 提交按钮 | 横向溢出 | 批量面板 | 聊天面板 |
| --- | --- | --- | --- | --- |
| 1440×900 | 可见可点 | 无 | 正常（overlap 0） | 完整可见（top 121） |
| 1024×768 | 可见可点 | 无 | 正常（maxHeight 398px） | 未单独复测（本轮重点在 800） |
| 800×600 | 可见可点 | 无 | 正常（overlap 0） | **仍被挤出视口（top −141）** |

### 7.6 结论与仍未解决项

- `2deb3d6` **修好了 1024×768 工具栏被撑高**（709px → 236px，文字列 68px → 220px，批量面板 140px → 398px），
  且 **1440×900 无回归**。这两条可以确认。
- **800×600 聊天面板仍未修好**：面板高度已经降下来，但 `--im-chat-lift: 190px` 把它又抬出了视口。
  根因与可复现步骤见 7.3，建议按「窄窗口把 --im-chat-lift 设为 0」处理；改完我可以再复验一次。
- 复验环境说明：本轮因为本机 Chrome 不可用改用 Edge 驱动探针（同 Chromium / 同 CDP），
  所有几何数值与 Chrome 时代的口径一致；截图仍写在 `%TEMP%\qio-visual\shots`。

---

## 8. 聊天面板修复复验（提交 `72ef426`）

**复验对象**：`72ef426`（基线 `2deb3d6`，改动只有 `frontend/src/styles/interactive-shell.css`：
1024 与 800 两个断点的 `--im-chat-lift` 改为 `0px`）。
**探针**：`scripts/visual_probe_d3.mjs`（Edge 驱动，CDP 9666 + 独立 profile；原因见第 7 节环境说明）。
**命令**：`node scripts/visual_probe_d3.mjs "@scripts/interactive-verify/steps-dv-37.json"`（步骤也含 `steps-dv-38.json` 的重叠面积测量）。

### 8.1 三档实测（原始值）

    800×600
      .chat-dock   {top:49,  bottom:259, h:210}  margin 0
      .panel       {top:49,  bottom:212, h:163}  margin 0
      .toggle      {top:220, bottom:259, h:39}
      toolbar      {top:283, bottom:592, h:309}   提交按钮 {top:518,bottom:583} 可见可点
      panelTopOk=true  panelVisible=true  panelScrollable=true
      chatOverToolbar=false  chatOverSubmit=false  横向溢出=false

    1024×768
      .chat-dock   {top:49,  bottom:496, h:447}
      .panel       {top:49,  bottom:449, h:400}   margin 0
      .toggle      {top:457, bottom:496, h:39}
      toolbar      {top:520, bottom:756, h:236}   提交按钮 {top:661,bottom:702} 可见可点
      panelTopOk=true  panelVisible=true  panelScrollable=false（内容装得下）
      chatOverToolbar=false  chatOverSubmit=false  横向溢出=false

    1440×900（回归）
      .chat-dock   {top:121, bottom:688, h:567}
      .panel       {top:121, bottom:641, h:520}   margin 0
      toolbar      {top:712, bottom:884, h:172}   提交按钮 {top:805,bottom:846} 可见可点
      panelTopOk=true  panelVisible=true  chatOverToolbar=false  横向溢出=false

### 8.2 结论

- **800×600 聊天面板已修好**：修复前 `.panel{top:-141,bottom:22}`（顶部可见 22px），
  现在 `.panel{top:49,bottom:212}`、`.chat-dock{top:49,bottom:259}`、margin 归零，
  顶部 ≥ 0、内容完整可见、面板可滚动、与工具栏和提交按钮都不重叠。截图 `dv-51-800-chat-final.png`。
- **1024×768 聊天面板正常**（上一轮没单独复测的这一档）：`{top:49,bottom:449}`，
  完整可见、不需要滚动、与工具栏不重叠、提交按钮可见可点。截图 `dv-53-1024-chat-final.png`。
- **1440×900 无回归**：`.panel{top:121,bottom:641,h:520}` 与修复前量级完全一致，
  工具栏高度 172px 未变。截图 `dv-54-1440-final.png`。
- **三档提交按钮都可见可点、都没有横向溢出**。

### 8.3 一个次要观察：800×600 同时打开聊天与批量列表时，批量面板压在聊天面板上方（次要）

**实测**（`steps-dv-38.json`，两浮层都打开）：

    panel（批量列表）{top:97,  bottom:266, left:392, right:792}  z-index 40
    chat（聊天面板） {top:49,  bottom:212, left:364, right:784}  z-index 25
    两面板重叠面积 = 45080 px²
    但批量列表的第一条 {top:258,bottom:286} 与「批量批准」按钮 {top:389,bottom:418}
      与聊天面板的重叠面积都是 0
    聊天面板的输入区/发送按钮位于面板下部，没有被覆盖

**结论**：这是**次要**问题——两处交互入口都还能点到（重叠区主要是批量列表的说明文字与聊天面板的头部/正文区），
但视觉上批量面板盖住了聊天面板的上半部分（截图 `dv-52-800-both-final.png`、`dv-55-800-overlap-detail.png`）。
窄窗口同时打开两个浮层本来就很挤，是否需要处理由 Lead 决定；我没有改产品代码。

### 8.4 仍未解决 / 没能验证的项（累计）

1. 800×600 同时打开两个浮层时的视觉遮挡（8.3，次要）。
2. 第 3 节列出的 6 项「没能验证」没有变化（空格框选与滚轮缩放后的坐标引用的是 Lead 的脚本数据、
   未勾选注释的删除路径、跨进程 running→paused 降级、草稿/快照重启恢复、浮条贴预览的锚定分支、像素级视觉质量）。
3. 第 7 节 7.3 的「聊天面板被挤出视口」已由 `72ef426` 修好，本节确认关闭。

---

## 9. 交付前最后复验（提交 `b68d1f7`，含 B 的连接点/局部工具栏修正）

**复验对象**：`b68d1f7`（并入 B 的 `e2109a2` + `d92af26`：卡片局部工具栏移到卡片外侧、连接点移到卡片内侧边缘、坐标以 DOM 真实滚动量为准）。
**探针**：`scripts/visual_probe_d3.mjs`（Edge 驱动，CDP 9666 + 独立 profile）；**步骤**：`steps-dv-39.json` … `steps-dv-46.json`。

### 9.1 三档回归（通过）

    1440×900
      toolbar    {top:712, bottom:884, h:172}   提交按钮 {top:805,bottom:846} 可见可点
      批量面板   {top:105, bottom:439}   panelOverToolbar=false
      聊天面板   {top:121, bottom:641, h:520}  chatTopOk=true  chatOverToolbar=false   横向溢出=false
    1024×768
      toolbar    {top:520, bottom:756, h:236}   提交按钮 {top:661,bottom:702} 可见可点
      批量面板   {top:105, bottom:439}   panelOverToolbar=false
      聊天面板   {top:49,  bottom:449, h:400}  chatTopOk=true  chatOverToolbar=false   横向溢出=false
    800×600
      toolbar    {top:283, bottom:592, h:309}   提交按钮 {top:518,bottom:583} 可见可点
      批量面板   {top:97,  bottom:266}   panelOverToolbar=false
      聊天面板   {top:49,  bottom:212, h:163}  chatTopOk=true  chatOverToolbar=false   横向溢出=false

**结论**：三档的工具栏高度、提交按钮可见可点、聊天面板 top ≥ 0、批量面板不压工具栏、无横向溢出，
与第 8 节数值完全一致 → B 的改动**没有回归**。截图 `dv-65-final-1440.png`、`dv-66-final-1024.png`、`dv-67-final-800.png`。

### 9.2 板面交互：连接点与局部工具栏（**发现一处阻断**）

**选中卡片后的实测矩形**（1440×900，卡片 x=912,y=60,w=260,h=170）：

    卡片       {top:165, bottom:335, left:912, right:1172}
    局部工具栏 {top:149, bottom:181, left:912, right:1332, h:32}   （贴在卡片上方外侧）
    连接点 top {top:166, bottom:178, left:1036, right:1048}  ← 与工具栏重叠 12px（整块被盖住）
    连接点 right  {top:244, bottom:256, left:1159, right:1171}  未被覆盖
    连接点 bottom {top:322, bottom:334, left:1036, right:1048}  未被覆盖
    连接点 left   {top:244, bottom:256, left:913, right:925}    未被覆盖

**命中测试（决定性证据）**：取 top 连接点中心 (1042,172) 调 `document.elementFromPoint`：

    {"hitTag":"BUTTON","hitIm":"card-edit","hitCls":"tb","isConnectPoint":false}

→ 该点命中局部工具栏的「编辑」按钮，**不是连接点**，用户从 top 连接点按不下去。
换一张位于板面中部的卡片（c_mux78xlb2ff1m，卡片 {top:165,bottom:335}）结果相同：`covered=["top"]`。
截图 `dv-56-connect-points.png`、`dv-57-toolbar-mid-card.png`、`dv-58-hit-test.png`。

**根因（读源码推断 + 实测印证）**：`BoardCanvas.vue` 里

    const TOOLBAR_H = 34;  const TOOLBAR_GAP = 10;
    top = cardTop − TOOLBAR_H − TOOLBAR_GAP   （上方放得下时）

按公式工具栏底边应在 `cardTop − 10`；实测工具栏 {top:149,bottom:181}、卡片 top=165，
底边只到 `cardTop + 16`、实际间距 +3px —— 工具栏落在「卡片上方 3px」而不是「上方 10px」。
而连接点 top 画在卡片**内侧**边缘（`.connect-point.top { top:0; transform:translateX(-50%) }`），
落在 [cardTop+1, cardTop+13]，因此只要工具栏底边越到 `cardTop` 以下就会盖住它。
公式里的 `TOOLBAR_H = 34` 与实测工具栏高度 32px 也不一致，两者建议一起核。

**其余三项（通过）**：
- **从连接点拖到另一张卡片能建链**：从 right 连接点拖到另一张卡，服务端 links 0 → 1，
  新链接 `{src:"c_mux78xlb2ff1m", dst:"c_mux6xxyv13ny8", direction:false, meaning:""}`；
  拖动期间 `data-im="link-draft"` 存在（`draftVisibleDuringDrag=true`）。截图 `dv-59-link-created.png`。
- **无效位置不建链**：从 bottom 连接点拖到空白板面，links 1 → 1（`noLinkCreated=true`）。截图 `dv-60-invalid-no-link.png`。
- **连接点在卡片内侧**：四个连接点矩形都完全落在卡片矩形内（`pointsInsideCard=[true,true,true,true]`）。

### 9.3 重叠成组与组名（通过）

- 先把两张卡移出组（界面「移出组」→ 组消失、groups=[]），再把一张拖到另一张上：
  拖动中提示 `松开后合并成组`、服务端 `groupsDuring=0`（松手前没成组）；
  松手后 `groupsAfter=1`、组名 `组 1`、`defaultName=true`、members=[两张卡的 id]。截图 `dv-64-merge-hint-and-group.png`。
- 组名留空：清空 `input.group-name` 并回车/失焦后，组名仍是 `组 1`、`defaultName=true`、组仍成立（2 名成员）。截图 `dv-62-group-default-name.png`。

### 9.4 结论与未解决项（累计）

1. **阻断（新发现，需修）**：卡片顶部连接点被局部工具栏盖住，命中测试落到工具栏按钮上，用户无法从 top 连接点建链；其余三个方向可用。数值与根因见 9.2。
2. 次要：800×600 同时打开聊天与批量列表时批量面板压住聊天面板上半部分（第 8.3 节，两处交互入口仍可点）。
3. 第 3 节的 6 项「没能验证」没有变化；第 7 节 7.3 的聊天面板溢出已由 `72ef426` 关闭。
4. 本轮探针控制台出现的 `Could not establish connection. Receiving end does not exist.` 是浏览器扩展报错，与应用无关（应用侧 httpFails 为空）。

---

## 10. 连接点遮挡修复复验（提交 `9fd905a`）—— **未修好**

**复验对象**：`9fd905a`（基线 `b68d1f7`）。**探针**：`scripts/visual_probe_d3.mjs`（Edge，CDP 9666）。
**步骤**：`steps-dv-47.json`（四张卡命中测试）、`steps-dv-50.json`（top 连接点拖线）、`steps-dv-52.json` / `steps-dv-53.json`（重复复测）、`steps-dv-51.json`（三档回归）。

### 10.1 连接点命中测试：**仍然失败**（阻断未消除）

**四张卡片各测一次**（1440×900，选中后取 top 连接点中心调 `elementFromPoint`）：

    卡片                          卡片 top   工具栏 {top,bottom}   top 连接点中心   命中元素         isPoint
    c_mux6xxyv13ny8 (x454,y125)     230      {215,247}             (584,237)       card-fold        false  ← 盖住
    c_mux6xxyw2r7ts (x564,y190)     295      {307,340}             (694,302)       connect-point    true
    c_mux78x2q1k49x (x628,y60)      165      {177,210}             (758,172)       connect-point    true
    c_mux78xlb2ff1m (x912,y60)      165      {177,210}             (1042,172)      connect-point    true

**重复复测（同一张卡、每次刷新后重新选中，`steps-dv-53.json`）**：

    card3（c_mux78xlb2ff1m，卡片 top=165）
      第一次/第二次/第三次：工具栏 {150,182}、inline top=44.6875px、命中 card-edit、isPoint=false
    card0（c_mux6xxyv13ny8，卡片 top=230）
      第一次/第二次：工具栏 {215,247}、inline top=109.688px、命中 card-fold、isPoint=false

**结论**：五次重复复测**全部失败**（工具栏底边分别落在卡片顶边下方 17px）。
上一轮 `steps-dv-47.json` 里 card1/card2/card3 曾出现 `isPoint=true`，但独立单卡复测（`steps-dv-52.json`：card3 命中 card-edit）
与重复复测都稳定失败 → 那 3 次通过**不可复现**，按「修复不生效」记录。截图 `dv-68-hit-card0.png`、`dv-78-card3-fresh.png`。

### 10.2 从 top 连接点拖线：**建不了链**

    取 top 连接点中心 → elementFromPoint = card-edit（不是连接点）
    在该点 pointerdown 后拖到另一张卡片：links 1 → 1（没有新建）
    （`steps-dv-50.json` 原始输出：hitIm="card-edit", linksBefore=1, linksAfter=1）

### 10.3 根因：工具栏坐标系与 `placeOverlay` 坐标系不是同一个（读源码 + 实测印证）

**实测（选中 card0）**：

    .board-shell    屏幕 {top:50,  left:0}
    .board-surface  屏幕 {top:105, left:0}      ← 工具栏的 offsetParent
    卡片            板面坐标 {x:454,y:125}，屏幕 {top:230,left:454}
    工具栏          行内 style="left: 454px; top: 109.688px"，屏幕 {top:215}
    工具栏 offsetParent = board-surface（position:relative）

`BoardCanvas.placeOverlay()` 把工具栏位置算成**相对 shell** 的坐标：

    const shellRect = shellElement.getBoundingClientRect();   // top=50
    const offsetY = viewRect.top - shellRect.top;            // 55
    const screen = toScreenPoint(...);  const cardTop = screen.y - shellRect.top;  // 卡片相对 shell ≈ 55
    const limitTop = Math.max(minTop, offsetY + 2);          // ≈ 57

但工具栏是 `position:absolute` 挂在 `.board-surface` 里的（offsetParent=board-surface），行内 `top` 是**相对 board-surface** 的。
于是「卡片相对 shell ≈ 55」被拿去和 `limitTop ≈ 57` 比：`aboveTop = 55 − 33 − 10 = 12 < 57` → 判「上方放不下」→ 走下方分支；
`belowTop = min(maxTop, 卡片底 + 10) = 190`；最后 `top = Math.max(limitTop, 190)` 再夹一次；
真正渲染出来就是「卡片上方 15px、底边落在卡片顶边下方 17px」——**工具栏始终压在卡片顶边上**。

**为什么上一轮时好时坏**：`placeOverlay` 每次都会去量 `[data-im="card-toolbar"]` 的真实高度，
但首次渲染时工具栏可能还不存在（用估算 34），随后才用真实高度 33 重算；叠加坐标系偏差，
最终位置在几像素量级抖动，命中元素就在「连接点」与「工具栏按钮」之间跳。
**这说明修法要先把坐标系统一**，否则量多少次真实高度都不稳。

### 10.4 三档回归：**通过**（`9fd905a` 没有引入回归）

    1440×900：toolbar{712,884,h:172}  提交按钮{805,846} 可见可点  批量面板{105,439} 不压工具栏  聊天面板{121,641,h:520} top≥0  无横向溢出
    1024×768：toolbar{520,756,h:236}  提交按钮{661,702} 可见可点  批量面板{105,439}            聊天面板{49,449,h:400}     无横向溢出
    800×600 ：toolbar{283,592,h:309}  提交按钮{518,583} 可见可点  批量面板{97,266}             聊天面板{49,212,h:163}     无横向溢出

与第 8/9 节完全一致。截图 `dv-75-reg-1440.png`、`dv-76-reg-1024.png`、`dv-77-reg-800.png`。

### 10.5 局部工具栏本身可见可点：**通过**

选中卡片后工具栏 7/7 个按钮都在视口内（`toolbarButtonsVisible=7`、`toolbarButtonsTotal=7`、`toolbarVisible=true`），没有被挪出视口；只是它压在了卡片顶边上。

### 10.6 结论

1. **阻断未消除**：连接点命中测试 5/5 失败，从 top 连接点建链失败（10.1 / 10.2）。
2. **根因是坐标系不统一**（10.3）：工具栏在 `.board-surface` 里绝对定位，`placeOverlay` 用 shell 坐标计算并夹取，两者相差 `.board-surface.top − .board-shell.top`（本例 55px）。
   建议把 `placeOverlay` 的坐标系与工具栏的 offsetParent 对齐（统一用 board-surface 坐标，或把工具栏移到 shell 下），再保留「量真实高度 + 夹取后复验」的逻辑。
3. 三档回归与工具栏自身可见性没有回归（10.4 / 10.5）。
4. 判定标准建议：`steps-dv-53.json` 五次重复都要 `isPoint=true`，且 `steps-dv-50.json` 要 `links 0→1`。

---
