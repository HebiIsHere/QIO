# QIO 0.3.2 五分支集成 · 三项返修 · 文案清理 —— 交付报告

> 交付对象：下一轮独立核查。**以本报告末尾的最终固定 SHA 为准**。
> 本轮范围：把五个已交付分支合入同一审查分支、解决合并冲突、返修 #28 / #46 / #47、清理仍会显示给用户的冗余文案；只做本地定向验收（不跑 CI、不跑全量 pytest、不跑无筛选 npm test、不跑完整安装包矩阵）。

## 0. 身份

| 项 | 值 |
| --- | --- |
| 共同基线 | `7445170d0f51756b2740eb7209d798540d8042a2` |
| 集成分支 | `fix/pm-qio032-integration` |
| 集成 worktree | `D:\qio-dev\pm-qio032-integration` |
| 最终固定 SHA | `acf323a3669010b7ae99f5d9c1989bf4f58340fd` |
| 远端可核查状态 | `见 §9（推送后回填）` |
| 合并方式 | 从基线建 worktree/分支，逐支按固定 SHA `git merge --no-ff`（保留历史），每次解决冲突并核对两侧行为后再合下一支 |

### 0.1 冻结输入核对（fetch 后）

fetch 后核对五个远端分支当前 HEAD = 任务给的已审查 SHA，**均无更新的提交需要纳入**：

| 顺序 | 来源分支 | 已审查 SHA = 远端 HEAD | 与基线关系 |
| --- | --- | --- | --- |
| 1 | fix/pm-qio032-shell | `a8c9222b1525ed0e5e6f8874487e7748f070cafb` | merge-base = 基线，ahead 33 |
| 2 | fix/pm-qio032-interactive | `08d08955549d3843603dc160b4da2fdee1cb799d` | merge-base = 基线，ahead 85 |
| 3 | fix/pm-qio032-planet | `0db3521dba6e8b38ba2d4a6017fdd1d7bd49c1fa` | merge-base = 基线，ahead 9 |
| 4 | fix/pm-qio032-settings | `baf2a1f9ce4c2ef333e73ed68bba7f56b7afb2ab` | merge-base = 基线，ahead 20 |
| 5 | fix/pm-qio032-records | `30575cebcf942b7c0a4f137996554c962d35a8de` | merge-base = 基线，ahead 18 |

（「原 SHA → 新 SHA」的纳入情形本轮不存在：远端 HEAD 就是已审查 SHA，没有需要额外纳入的提交。）

### 0.2 本轮新增提交（按可审查粒度）

1. `bb0fd72b` merge(pm-qio032-shell)
2. `b2796785` merge(pm-qio032-interactive)
3. `bc58cdfd` merge(pm-qio032-planet)
4. `543ee77a` merge(pm-qio032-settings)
5. `e324e392` merge(pm-qio032-records)
6. `2461e5d5` test(integration)：合并后按两侧同时成立的实际行为修正两处旧断言
7. `8c2009ff` #47 恢复「还原默认布局」
8. `66bd9636` #46 离开互动页后继续上传与关联
9. `79a4b748` test(integration)：解除组级连线的确认框改查窗口层（#82 × #60）
10. `13f54eb0` 文案清理（第五节）
11. `d3f659e1` #28 正式提交只读提交时的版本
12. `57e38057` docs(status) + 证据索引
13. `acf323a3` test(copy)：未完成事项禁用原因只显示一次（S07 收尾）
14. 本轮无其它提交（最终 SHA 见 §0）

每题改动与理由见各提交信息；无无关格式化、无依赖升级、无版本号调整、无迁移历史改写。

### 0.3 合并后校验

- 五个来源 SHA 逐一 `git merge-base --is-ancestor <sha> HEAD` → 全部 True。
- 祖先关系正确但实现被覆盖的防线：① 对每支改动过的文件做「HEAD 内容 == 基线内容」扫描（等价于整文件被回退）→ 0 命中；② 冲突文件逐项人工核对两侧行为（见 §2）；③ 各分支自己的定向用例在集成结果上重跑（见 §5）；④ `git grep -E "^(<<<<<<< |>>>>>>> )"` → 0 行冲突标记。

## 1. 合并冲突处理（每个冲突保留了什么行为）

### 1.1 `frontend/src/views/settings/searchIndex.ts`
- 保留**外壳分支的简短文案**：`memory.fragment.max-tokens` 的 `description` 用「单段长度上限（按估算字符数计）」，不再出现「到点分段不代表阶段完成」。
- 保留**设置分支的分类 / 定位键 / `fold`**：该条目 `category: "memory"`、`key: "memory.fragment.max-tokens"`、`fold: "memory.advanced"`。
- 搜「单段长度」→ 先自动展开「记忆 · 高级选项」再定位到可操作控件（真实界面已验，见 §4 #47-13；`settings-search.test.ts` 断言 fold 仍在）。
- 旧星球/设置入口的**贴边隐藏选项继续移除**（索引与设置区均无「入口贴靠后自动隐藏」条目，`settings-content.test.ts` 有守卫）。
- **恢复 #47 的搜索项**：`regular.window.reset-layout`（还原默认布局），与常规区真实按钮的 `data-settings-key` 对齐。
- 附带修正：嵌入用途的搜索行说明原为「更换外部嵌入模型会影响既有索引维度」（与现在「使用 QIO 当前内置配置」的真实行为矛盾）→ 改为「使用 QIO 当前内置配置」。

### 1.2 `frontend/src/components/conversation/PreviewPanel.vue`
两侧行为同时保留并由测试同时断言：
- 外壳组：#52 按**实际焦点**决定删除/方向键作用于哪个标签、删完焦点交给新当前标签；#53 切标签/收起/全窗口前 `flushReader()` 写回搜索词与阅读位置、旧标签迟到写入不覆盖；`autoFull` 区分「窄窗自动全窗口」与「手动全窗口」（前者「返回对话」=收起预览，后者=回分栏）；全窗口 Esc 走共用层级栈并归还焦点。
- 记录组：#57 面板内 `.qio-sr-only` 读屏播报 `openStatus` **随当前标签更新**，并且最后一个标签关闭后变成空串（清掉旧播报）。
- 两组合并后同时成立：`cv-c-preview-50-53-state.test.ts`（外壳行为 + 「可见文字里只读只出现一次」）、`d-preview-52-tab-focus.test.ts`（焦点标签快捷键）、`cv-c-preview-feedback.test.ts`（读屏播报随标签更新/清空）三文件全绿。

### 1.3 `docs/status.md`
- 五组记录全部保留（按日期倒序：互动 2 条 2026-11-13、星球 2026-11-13、记录 2026-11-13、外壳 2026-11-12、设置 2026-11-12），并新增本条集成记录；顶部「最后核对：2026-11-14」。
- 明确区分来源分支历史验收（各条里写的是各自分支/SHA 上跑的）与本轮固定 SHA 上的验收（本条 + §4/§5）。
- 原报告里 A/B 工具读取、Windows 原生窗口、更新安装等未验证项**继续保留**；其中 A/B 工具读取本轮新增了 fake provider 证据（见 §3.1），但「真实厂商」仍标未验证。

### 1.4 合并交互另外暴露的 4 处「两侧都成立后旧断言失真」（均已按实际行为修正，未删除/跳过任何测试）
| 位置 | 现象 | 处理 | 保留的行为 |
| --- | --- | --- | --- |
| `cv-c-preview-50-53-state.test.ts` | 外壳「可见只读一次」vs 记录组读屏播报里也含「只读」 | 计数改为去掉 `.qio-sr-only` 后的可见文字，并断言读屏播报仍在 | 视觉只读一次 + 读屏播报 |
| `uf2-notice-navigation.test.ts` | 外壳 #83 去掉固定断点后，1000px 已能并排左栏，旧断言用 1000 代表「窄窗」 | 改用 600px（< CENTER_MIN 460 + 分隔条 12 + LEFT_MIN 232 = 704） | 左栏确实不可见时不得声称任务列表可见 |
| `pm032-canvas-group-ops.test.ts` | 设置 #82 把 QConfirm 的 layer 档 Teleport 到 body（窗口层），互动 #60 的用例仍在菜单子树里找确认框 | 改在 `document.body` 上查 `.qio-confirm` 与 confirm/cancel 动作 | 解除组级连线前仍列受影响关系、确认前不提交、取消不解除 |
| `fu-w5-RecoveryInbox.spec.ts` | 文案清理按 S07 删掉禁用按钮上重复原因的悬停说明，旧断言仍要求存在 | 断言改为「不再重复挂悬停」+「原因在 .blocked 显示一次」 | 禁用原因仍可读、按钮仍点不动 |

## 2. 三项返修

### 2.1 #28 · 正式提交只能读取提交时的版本

**复现（修复前）**：① 保存板面 A；② 直接插一条只有 id/board_id/seq/status 的提交行（无快照）并绑定该提交；③ `board_inspect` / `board_read_cards`（latest 与 `source=submission`）读到**当前板面**（B 版）；显式提交路径在「提交行存在但没有快照」时返回成功空结果。
**预期（定稿）**：绑定提交的轮次只用提交当时版本；快照缺失/损坏/归属不匹配 → 可识别失败 + 「读不到这次提交的内容，请重新提交。」，结果里没有当前卡片/关系；材料只交付能确认属于提交时的内容，同名同大小不足以证明未变。
**实际（修复后）**：
- `board_tools._submission_basis`：有 `submission_id` 就**不再回退**当前板面；不可用时返回 `failed / submission_snapshot_unavailable`，短文案如上，诊断进 `data.diagnostics`；`board_inspect`、`board_read_cards`(latest)、`board_read_cards`(source=submission) 三路同口径，失败结果不含 cards/links/counts。
- `materials.py`：旧记录没有冻结 sha256 时不再按「同名同大小」放行（不交付 + 重新提交提示）；新增「读一次 → 校验 → 用同一份字节解析/交付图片」（`content_overrides` + 临时副本），避免「校验 A 又读到 B」。
- `attachment_tools.read_attachment`：提交范围失败带结构化 code/data；读取前后各核对一次内容哈希，读期间变化 → `material_source_changed` 且不交付。
- `turn_orchestrator`：首轮/追问材料入口不可用 → `materials_status=unavailable` + 短文案 + 原因（不是空材料）。
- 普通互动聊天与未绑定提交的读取规则不变；三态共用原授权；隐藏/排除/提交后新增材料不进入绑定轮次。
**证据**（`_lead-logs/pm-qio032/integration-032/evidence-28.md`、本机 `evidence-28-summary.txt` / `evidence-28-pytest-s.txt`）：
- fake provider 在**同一绑定追问轮**真实执行 8 次工具调用（`board_inspect`、`board_read_cards` latest/submission、`read_attachment` TXT/PNG/B 版/隐藏版、按 card_ids 的 submission 读取），台账含**参数、返回、提交身份 sub_b4dfe5fb727e**、以及**最终模型请求**：A 版正文/关系出现（模型请求 index 1 命中 `A_TEXT_BODY`、`A_LINK_MEANING`），B 版正文、B 替换后 TXT、B-only 材料、排除/隐藏正文**全部 false**，A 版 TXT 原内容已被替换 → 结果与请求里都没有它，并出现「重新提交」提示。
- 快照缺失场景（`sub_no_snapshot_28`）：工具返回 `submission_snapshot_unavailable` + 短文案；模型请求有 `materials_status: unavailable` 与「读不到这次提交的内容，请重新提交。」，**没有当前板面正文**。
- 定向用例：`backend/tests/pm_qio032_material_scope_test.py`（含改写后的失败断言：无当前正文/关系/卡片计数）、新增 `pm_qio032_submit_version_basis.py`（5 例，含 fake provider 绑定追问轮）、`pm_qio032_submit_material_versions.py`、`pm_qio032_submit_first_round_materials.py`。
- 改写断言：`test_latest_read_falls_back_to_current_when_submission_snapshot_missing` → `test_bound_turn_with_missing_snapshot_fails_and_asks_resubmit`（旧断言固化的正是 #28 禁止的回退，新断言更严）；三个既有夹具补真实提交快照（`test_p3_b_tools`、`test_p3_d_tools_registry`、`test_p2_d_participation_endpoints_display_version` 的用例改为未绑定轮），**断言内容未删减**。

### 2.2 #46 · 离开互动页后继续上传和关联

**复现（修复前）**：在互动页从 AddMenu 选文件上传 → 立刻切到设置 → 上传成功；返回互动页：卡片标「结果未知」，成功回执没有完成关联（旧测试 `pm032-canvas-import-batch.test.ts` 明确断言了「卸载即结果未知」）。
**预期**：上传/等待附件可用/最终关联在生命周期长于页面的位置；回执按原板面/原卡/原关联目标/本次尝试核对；返回时显示真实进度或终态，只有真的需要用户重选时才要求重选。
**实际**：
- `stores/interactive.ts` 新增导入执行器（`runImportItem`/`runImportBatch`/`startCardAttachment`）与单卡导入终态 `cardImportJobs`（含 acknowledged）；回执核对原板面/原卡/原关联目标/尝试序号，原卡删除、换文件、已有更新尝试 → 丢弃迟到回执（`supersededBy=item|card`）；导入写板面统一走公开 `commit`。
- `AddMenu.vue` 只剩选文件与显示；回挂时恢复批次真实进度，并按**既有提示入口**显示单卡终态（不新增第二个常驻通知条）；成功沿用既有约 3 秒自动消失，失败保留到关闭/重试；已展示过的终态 ack 后不再重复弹。
- 单批五份上限、未接收项文件名与重新导入入口、单项失败只重试该项——原行为保留（`importBatchSummaryText` 是唯一的批次进度文案入口）。
**证据**：`_lead-logs/pm-qio032/integration-032/evidence-46.md`；用例 `pm032-canvas-import-batch.test.ts`（基线 8 passed → 21 passed，卸载场景改为「发起上传→去设置→上传成功→回互动页」并断言原卡已关联、只关联一次、批次成功、没有要求重选）、`fix025-attach-notice.test.ts`（1 条断言按定稿反转）。
**真实界面往返（浏览器证据）**：见 §5 UI 表 `#46-1..4`（延迟 4.5s 的上传在途切到设置 → 返回后卡只 +1 且稳定、工具栏变「重新关联附件」、无未关联/重新选择入口、成功提示可读、批次在结算后显示已接收）。
**Windows 原生拖放**：未验证（需真实 Tauri 壳）；桌面原路径登记只有 jsdom/mock 证据（`registerSourceAttachment` 调用与落定）。

### 2.3 #47 · 恢复可用的「还原默认布局」

**复现（修复前）**：设置 > 常规 > 窗口行为里没有「还原默认布局」入口，搜索也搜不到（#39 定稿 A 时期删除）。
**预期**：恢复设置操作与搜索项；只重置用户可调布局字段（左右栏宽、左栏收起、右侧预览开合、手动全窗口）；保留预览标签及顺序/当前标签/各标签搜索词与阅读位置/聊天内容/草稿/当前话题；窄窗自动折叠/全窗口仍由宽度推导；竞态安全；成功一次短反馈，失败明确 + 重试。
**实际**：
- `RegularSection.vue`：窗口行为新增「布局 · 还原默认布局」，保存成功后才给「已还原默认布局（预览标签与阅读位置保留）」，失败显示真实原因 + 技术详情 + 重试（重放同一次还原）。
- `conversationShell.ts`：保存改**串行链**（服务端最后一次写入一定是最后状态）；回执仅在「写出的快照 === 当前界面状态」时回填 → 迟到旧回执不覆盖还原结果、也不覆盖回到对话后新开的标签/新的阅读位置；新增 `resetLayout()`（未读过服务端布局时先读再还原，避免用空标签清掉已保存的标签/位置）。
- `layoutPrefs.ts`：新增 `restoreDefaultLayout`（只改 5 个字段）、`cloneLayoutPrefs`、`layoutPrefsFingerprint`；`resolveConversationLayout`/`sanitizeLayoutPrefs` 未改（窄窗临时收起/临时全窗口仍按实际宽度推导，不写回偏好）。
- 搜索项 `regular.window.reset-layout` 恢复并定位到真实按钮；贴边隐藏继续删除；#39 的固定星球/设置入口与 Windows 应用窗口贴靠未改；双击分隔线仍是单侧快捷操作。
**证据**：`_lead-logs/pm-qio032/integration-032/evidence-47.md`；新增 `settings-reset-layout.test.ts`（7 例：只改布局字段、保留标签/位置/任务页签、默认收起预览后重开回原标签原位置、启动直接进设置先读再还原、两条竞态、失败重试、窄窗不写回偏好）、`layoutPrefs.test.ts`(+4)、`settings-search.test.ts`/`settings-content.test.ts` 旧断言更新。
**真实界面**：见 §5 UI 表（宽窗还原+返回+刷新、窄窗推导、搜索定位、[模拟] 500 失败+重试）。

## 3. 文案清理（第五节）

- 逐项核对表（页面与触发条件 / 改动前 / 改动后 / 删减或保留的理由 / 位置）**已提交**：`_lead-logs/pm-qio032/integration-032/copy-checklist.md`（含待判项裁决、表 B 措辞逐条判断、转交项、既有断言改写说明）。
- 可逐条追证的定稿文案共 **58 处**（外壳 18 + 互动 14 + 星球 6 + 设置 20）；任务里说的「约 60 处」在仓库追不到 60 条出处——records 组没有独立文案清单（其文案改动内嵌在 #12/#57/#68/#69/#91 里），差 2 处无逐条原文。
- 本轮实际新增改动：**14 个前端源文件 / 22 处**（21 处可见文字 + 1 处读屏名称）+ 转交后由 Lead 在 AddMenu / InteractiveView / stores/interactive / searchIndex 落地的 5 处；另有 **11 项**（6 待判 + 5 部分）经核对**判定已达标**，注明「已保留短版」，不重写。
- 代表性场景（完整见核对表）：
  - 互动上传/替换：常态进度只写「正在保存附件「X」…」，删「首次落盘为工作区材料源文件」附注；失败提示补文件名「附件保存失败「X」：…」并重试该份。
  - 互动提交/查证/意图：删「也不排除没有到达服务端」「这不代表没有上次结果/本次没有改动」「已确认不等于已完成」「可逐项重试或查看原因」；保留最短事实句「确认后由服务端续作。」（同时满足后端静态边界守卫）。
  - 执行记录：读不到记录/成果不再补「不等于没有…」判据句；`operation_key` 等原始标识收进既有「诊断」区。
  - 设置搜索/记忆：删「到点分段不代表阶段完成」；嵌入用途说明与真实行为对齐；搜索行保留字段含义、单位与范围。
  - 预览：「只读」在对话侧标题徽标出现一次；卡片内预览删掉事实行尾「· 只读」；关闭提示用「关闭预览」。
  - 星球/引导/全局：删空态里的内部判断说明、重复搜索范围说明；引导按钮旁同义说明删除。
- 保留项（有意保留、写明保护什么）：权限影响/不可撤销动作/未保存数据/安装时序等会改变决定的说明各讲一次；「有序组/普通组：摆放顺序不代表先后」保留（改变用户对顺序的预期）；「已配置 ≠ 检查通过」保留（决定是否要再点检查）。

## 4. 文案与视觉（真实界面）

真实界面验收用既有 harness `scripts/verify-pm-qio032/run.ps1`（后端 8734 / vite 5199 / 本机假厂商 8807 / 系统 Edge + CDP 9333，隔离数据域 `ui-data-final2`），驱动脚本是本轮新写的验收资产 `_lead-logs/pm-qio032/e2e-integration-032/ui-verify.mjs` + `layout-only.mjs`（不改产品代码）。

**总账：36 PASS / 1 FAIL / 5 UNVERIFIED（42 条）** = 主验收 22/1/5（28 条）+ #47 宽窗定向 14/0/0（14 条）。

### #47 还原默认布局（宽窗四步，全部 PASS）
| 断言 | 事实 |
| --- | --- |
| 两个预览标签各自搜索词与滚动位置 | `replycode:msg_f89b84e33d4b:0` search=`alpha-…` scrollTop=420；`:1` search=`beta-…` scrollTop=260 |
| 写入服务端 conversation_layout | 服务端真值同上一行（GET /api/settings/ui 读回） |
| 拖动左右分隔线 + 收起左栏 | leftWidth 292→340（键盘兜底 420）、rightWidth 420→372、leftCollapsed=true |
| 点「还原默认布局」 | 提示「已还原默认布局（预览标签与阅读位置保留）」；prefs 回 leftWidth=292 / rightWidth=420 / leftCollapsed=false / rightOpen=false / previewFull=false；**两个标签与搜索词/滚动位置原样保留** |
| 返回对话 | 左栏 292 展开、预览收起、预览入口显示「打开文件预览，2 个标签」 |
| 重新打开预览 | 两标签、当前标签（`:1`）、各自搜索词与滚动位置都在 |
| 整页刷新 | 布局字段仍默认（292/420/展开/收起预览），标签/当前标签/搜索词/位置仍在 |
| 窄窗 800×600（预览占位） | `data-left-auto=true`、`data-preview-full=true`（自动收起左栏 + 临时全窗口），**服务端 prefs 仍是 leftCollapsed=false / previewFull=false**（临时状态没被写成用户偏好） |
| 拉宽到 1440×900 | 回到三栏分栏，偏好仍默认 |

### #46 跨页面上传（全部 PASS）
| 断言 | 事实 |
| --- | --- |
| 上传在途切到设置 | CDP 把 `POST /api/attachments/upload` 延迟 4.5s（**[模拟]**），上传中截图两张（互动页 / 设置页） |
| 返回后卡片数量 | before=0 → after=1，稳定 1（无重复卡） |
| 原卡已关联 | 工具栏变「重新关联附件」、出现「查看」、无「未关联」提示、无重试入口 |
| 没有要求重选文件 | 无「重新选择 / 重新导入」入口；成功提示可读：「已关联附件「crosspage-….txt」：已保存为工作区材料源文件（来源：浏览器上传）；板面关联是本地改动，提交后 QIO 才可见」 |
| 批次（补充，非跨页） | 单文件拖放批次结算后显示「本批已接收 1 份 / 未接收 0 份（单批上限 5 份）」、item 状态「已接收」 |

### 设置搜索（全部 PASS）
- 搜「单段长度」→ 自动展开 `[data-settings-fold="memory.advanced"]`，定位到「单段长度目标」输入框且**可聚焦**（inputFocused=true）。
- 搜「还原默认布局」→ 命中 `regular.window.reset-layout`、分类「常规」、定位到真实按钮「还原默认布局」（命中后搜索面板按设计关闭，不算失败）。

### 保存失败与重试（[模拟]，PASS）
- CDP 让 `PUT /api/settings/ui` 返回 **HTTP 500** → 设置行显示 `.set-notice.err`「还原默认布局失败」（role=alert）+ 技术详情 + 重试。
- 点重试 → 成功提示「已还原默认布局（预览标签与阅读位置保留）」（role=status）。

### 文案与视觉矩阵（2 主题 × 2 尺寸，全部 PASS）
- 净白 / 暗紫晶 × 1440×900 / 800×600：对话底部、预览标题行、互动底部、设置搜索态各截图；四组合均「控件可达（hitIsSelf）、无横向溢出、无重复提示、无文档级横向滚动」。
- 真实失败态（设置 → 关于与更新 → 检查更新）：`.line.err[role=alert]` 显示「更新失败（原始信息：…）／更新没有安装。可以点「重试」重新检查…」——**只有内联失败提示，没有浮层形态**，故「失败浮层」一项记 UNVERIFIED（截图 `matrix-failure-inline`）。

### 唯一 FAIL（产品问题，非 fixture）
从**刚流式产出、尚未持久化**的消息打开代码预览，阅读器停在「正在读取这个文件…」：
- tab id `replycode:local_1794591004179_2:0`（`local_` 临时消息 id），`readerKind=code`、`tools=false`、`search=false`、`scroll=false`、`stateText="正在读取这个文件…"`，120s 不进入 ready。
- 对照：**已持久化**消息（`replycode:msg_f89b84e33d4b:0`）同一入口 `search=true`/`scroll=true`，内容正常渲染。
- 影响面：仅「刚产出未落库」这一瞬间；刷新后或历史消息无影响。
- 归属判断：预览加载链路（`stores/conversationPreview.ts`、`conversation/preview.ts`、`previewBridge.ts`）本轮**一行未改**（`git diff 7445170d..HEAD` 为空），`PreviewReader.vue` 只改了搜索词写回与 watch 键；`MessageItem.vue` 的 reply-code 入口在基线就存在。因此这是**基线既有行为**，不是本轮引入；本轮按「发现即如实记录」列入未修项，未擅自扩大改动面。

### UNVERIFIED（5 条）
1-4. 主验收的 #47-9..12（窄窗四条）：主验收脚本的 s1 前置因上面的 FAIL 不成立，已由 #47 定向复跑的 L11/L12 覆盖为 PASS（同一 HEAD、同一数据域）。
5. 视觉-失败浮层：真实入口只有内联失败提示，无浮层形态。

## 5. 本地定向验收（命令与结果，最终 SHA 上）

```
# 身份与结构
git rev-parse HEAD                                  # 见 §0
git merge-base --is-ancestor <5 个来源 SHA> HEAD     # 5/5 True
git grep -n -E "^(<<<<<<< |>>>>>>> )"               # 0 行
git status --porcelain                              # 空

# 后端定向（不跑全量）
cd backend; QIO_DATA_DIR=<隔离目录> QIO_DISABLE_DB_CHECK=1 \
  .venv\Scripts\python.exe -m pytest \
  tests\pm_qio032_material_scope_test.py tests\pm_qio032_submit_version_basis.py \
  tests\pm_qio032_submit_material_versions.py tests\pm_qio032_submit_first_round_materials.py \
  tests\test_p3_b_tools.py tests\test_p3_d_tools_registry.py \
  tests\test_p2_d_participation_endpoints_display_version.py \
  tests\test_p3_d_static_boundaries.py tests\test_p2_d_static_boundaries.py -q
# → 109 passed / 0 failed / 0 skipped（22.7s，junit 计数）

# 前端定向（受影响入口，不跑无筛选 npm test）
cd frontend; npx --no-install vitest run \
  src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts \
  src/components/interactive/__tests__/pm032-canvas-group-ops.test.ts \
  src/conversation/layoutPrefs.test.ts \
  src/views/settings/__tests__/settings-search.test.ts \
  src/views/settings/__tests__/settings-content.test.ts \
  src/views/settings/__tests__/settings-reset-layout.test.ts \
  src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts \
  src/components/conversation/__tests__/d-preview-52-tab-focus.test.ts \
  src/components/conversation/__tests__/cv-c-preview-feedback.test.ts \
  src/views/__tests__/i2v2-a-recovery-entry.test.ts \
  src/components/__tests__/fu-w5-RecoveryInbox.spec.ts \
  src/services/__tests__/uf2-notice-navigation.test.ts \
  src/components/interactive/__tests__/fix025-attach-notice.test.ts
# → 13 files / 149 tests passed

# 类型检查 / 文档 / workflow 契约
cd frontend; npx --no-install vue-tsc --noEmit            # exit 0
python scripts/check_docs.py                              # 通过（121 个里程碑条目）
python scripts/targeted-verify/verify_workflow_contract.py # 171 assertions passed（含 fix/pm-* 不触发 CI 的断言）

# 受影响区域加宽回归（不是全量；用于发现合并交互）
# → src/views/settings + src/components/conversation + src/conversation + src/views/__tests__ + src/stores/__tests__ + src/components/__tests__ + src/components/approval → 303 files / 2398 tests，1 failed（fu-w5-RecoveryInbox 旧断言，已按 S07 修正后 19 passed）；src/components/{planet,models,onboarding,ui,interactive,records} → 142 files / 1314 tests，1 failed（recovery-a-integration-n6 并行批次波动，单独复跑 6 passed）
```

固定 SHA 上的完整命令与原始日志：`_lead-logs/pm-qio032/integration-032/verify-final.ps1`（本机日志在 `D:\qio-dev\_lead-logs\integration-032\final\`）。

## 6. 仍失败 / 未验证 / 证据局限

**仍失败**：1 条（产品问题，基线既有、本轮未修）：**从刚流式产出、尚未持久化的消息打开代码预览，阅读器停在「正在读取这个文件…」**（tab id `replycode:local_…`，120s 不 ready；已持久化消息正常）。

复现：发一条会产出代码块的消息 → 在回复流式结束后**不刷新**直接点该代码块的「预览」→ 右侧停在读取态。
影响面窄（刷新后正常），但用户会看到「打不开」。归属：预览加载链路本轮未改（见 §4 归属判断），建议下一轮单独修（例如：本地临时 id 的消息在持久化后把标签身份迁移到服务端 id，或在正文已在内存时直接进入 ready 而不等附件读取）。

**未验证（本轮无对应新证据）**：
- Windows 原生 Tauri 窗口：#36 普通/最大化/系统贴靠窗口下星球按钮边界与点击、DPI 与原生拖放——无实机与原生壳环境，只有网页/CDP 证据。
- #25「下载并安装」退出、重新打开时序——需要在安装版验证，本轮未做。
- 真实厂商模型：所有模型调用都是本机假厂商；A/B 工具读取本轮有 fake provider 证据，但真实厂商仍为未验证。
- Windows 原生拖放；桌面原路径登记只有 mock 证据。
- 完整安装包 E2E / 更新器验签 / 发布闸门——本轮按纪律不跑。

**证据局限**：
- 设置组 20 项定稿文案只有名目、没有逐条原文，只能按「用户能否知道状态/影响/下一步」的口径核对，不是逐字对照。
- `git diff --check 7445170d..HEAD` 报 3 处空白问题（2 处 EOF 空行、1 处行尾空格），**均来自来源分支既有内容**（对应 blob 与来源分支尖端的 blob 完全一致），本轮未新增、按「避免无关格式化」未改；无参数/--cached 形式为 0。
- `recovery-a-integration-n6-store-input.test.ts` 在 300 文件级并行批次里失败 1 次、单独复跑通过（6 passed）；首跑与复跑结果都保留，判为并行批次下的顺序/污染波动，不是产品失败。
- 真实界面验收的 CDP 场景脚本是本轮新写的验收资产（`_lead-logs/pm-qio032/integration-032/ui-verify/`），其「等待/截图/断言」本身也有假设；凡脚本假设错误处都在报告里按「fixture 问题」与「产品问题」分开标注。
- 全量 pytest / 无筛选 npm test / CI 按用户纪律**未跑**；受影响区域加宽回归只覆盖相关目录。

## 7. 自主设计决定（影响原体验的都列在这里）

1. **#46 单卡导入终态用既有提示入口恢复**，不新增常驻通知条：终态存 store 并保留到用户「看过或关闭」（ack），成功提示仍约 3 秒自动消失（只消失提示，卡片关联保留），失败保留到关闭/重试。保护的是「离开期间完成/失败的结果不会因为 3 秒提示过期而丢失」。
2. **#46 重试复用 store 内的文件源**，只有整页刷新丢源时才要求重新选择（`needsReselect`）；关闭批次提示不取消在飞上传，但会释放该批内存文件源（在飞回执自带源）。
3. **#46 导入写板面统一走公开 `commit`**，无预建卡的导入把「建卡 + 关联」合并为一次 commit（不产生中间空卡、不产生两条撤销）。
4. **#47 保存失败保留内存里的还原结果**（界面已生效）但如实显示失败并可重试，刷新后以服务端值回填（不假装已保存）。
5. **#47 保存串行 + 内容指纹守卫**：宁可多一次串行等待也不让服务端最终值倒退；回执只在内容未变时回填。
6. **#28 旧提交缺 sha256 时宁可不交付也不猜**：会降低「很老的提交仍能读到材料正文」的可用性，但符合「同名同大小不足以证明未变」的定稿；旧提交仍可查看可用的历史记录，缺失材料提示重新提交。
7. **#28 快照失败不给当前板面**：界面上表现为读不到（含重试/重新提交提示），而不是「空板面」。
8. **文案清理删掉「不代表/不等于」类判据句时，保留了最短事实句**（例如「确认后由服务端续作。」），既满足后端静态边界守卫，也不让用户失去「确认后会发生什么」的信息。
9. **组级连线解除的确认框跟随 #82 改为窗口层**（Teleport 到 body）：窄窗不再被顶部导航遮挡；互动组的「先确认」语义保持不变，只改用例的查找位置。
10. **未完成的窄窗宽度断言修正**（1000px → 600px）：这是外壳 #83「按真实可用宽度解析」的必然结果，不是放宽断言。

## 8. 下一轮重点复查建议

1. 在 Windows 实机上补 #36（普通/最大化/贴靠窗口星球按钮边界与点击）与 #25（下载并安装→退出→重开时序）——本轮 UNVERIFIED。
2. 用真实厂商复验 #28 的 A/B 材料链路（本轮只有 fake provider 证据），特别是「图片从核验字节交付」在真实视觉链路上的表现。
3. 设置组 20 项文案需要拿到逐条原稿后逐字对照；本轮只能按口径核对。
4. 主流程全量回归（pytest 全量 + 无筛选 npm test + CI）本轮按纪律未跑；合并涉及 426 个文件，建议下一轮在候选 SHA 上跑一次完整矩阵。
5. 预览在「代码片段」类型的搜索/滚动行为、以及 800×600 下预览占位后的自动全窗口，建议在实机再确认一遍（本轮见 §4 的 UNVERIFIED 项）。
6. 合并交互类问题（两侧都成立后旧断言失真）建议在下一轮 CI 全量里兜底，本轮已发现并处理 4 处。

---

### 附：证据索引

| 内容 | 位置 |
| --- | --- |
| 文案逐项核对表 | `_lead-logs/pm-qio032/integration-032/copy-checklist.md` |
| #28 返修证据 | `_lead-logs/pm-qio032/integration-032/evidence-28.md` |
| #46 返修证据 | `_lead-logs/pm-qio032/integration-032/evidence-46.md` |
| #47 返修证据 | `_lead-logs/pm-qio032/integration-032/evidence-47.md` |
| 定向验收命令清单 | `_lead-logs/pm-qio032/integration-032/accept-inventory.md` |
| 本机定向验收日志 | `_lead-logs/pm-qio032/integration-032/final/`（verify-final.ps1 产物） |
| 真实界面验收 | `_lead-logs/pm-qio032/e2e-integration-032/`（summary.json / REPORT.md / shots） |
| 假厂商与 E2E harness | `scripts/verify-pm-qio032/`（既有资产，本轮未改产品代码） |
