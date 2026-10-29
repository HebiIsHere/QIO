# QIO 项目状态

**本文只回答一个问题：现在真的做到哪了。**

- 设计意图与分层 → `docs/architecture.md`
- 安装与运行 → `docs/SETUP.md`
- 协作约定 → `AGENTS.md`

最后核对：2026-10-29（**发布前第五轮合并前集成验证完成；未合并 main、未打标签、未发布、未触发 CI**：候选代码 SHA `dc97b25ea3817c1ad5445959a7b758d2adf9ad61`（第四轮交付尖端），分支 `fix/release-step5-integration-validation-20261029`。main 关系：`origin/main` = `1b03541eedd2dcaa9b82d6102108d717c71c6fb4`，候选是 main 的严格后代（313 ahead / 0 behind，merge-base = main），main 无独立新增提交，完整修复链保留、无需重放或合并提交。六项清单：①启动与入口 PASS（隔离空数据完成现有 7 步引导进主页、对话/互动/设置切换、真实 main 旧数据样本 schema 70→74 升级保留与二次重开幂等）；②普通对话 PASS（流式首个正文早于假厂商结束 3.542 秒、界面停止 cancelled/user_stopped、500 失败后界面重试成功、历史重开内容与终态一致）；③模型组合 PASS（同一连接 4 个模型：主对话与三种后台用途各自命中，操作期间改配置只影响后续批次）；④互动意图：确认→执行→记录自动更新与重试/恢复 PASS，发现并**最小修复**两个集成阻塞（确认路径结果入口不可达 → 读取侧回退权威账本 `board_operations`；提交轮内部绑定文字作为普通用户消息 → 提交轮标记 `content_type=board_submission` 且前端不渲染，模型可见正文一字不改），修复提交 `d4de01a40552425a5ead9d422af5fd6a467d60d0`，只重跑第 4 项与直接受影响项后 PASS；⑤设置与凭据 PASS（保存成功/失败/连续修改显示值与实际一致、输出速度固定清单通过、凭据未改内容重试不重复建连接、获取模型失败结束等待并可单独重试、刷新重开一致）；⑥搜索与候选 PASS（同类 18 条命中、总数正确、第 9 条与最后一条可定位、无看图能力模型禁作看图候选、未接通用途只读）。定向测试：后端受影响 10 文件 142 项、前端受影响 11 文件 111 项，其余各项定向文件均 exit 0；前端生产构建在两个代码状态各一次（`dc97b25e` 与 `d4de01a4`）均 exit 0；`check_docs`、第三方声明检查与 `release_gate --selftest` 通过（自检只代表检查机制本身，不代表已有安装包验证）。真实厂商调用与 Windows 安装包/发布操作**未验证**（后续原生发布验证）；本轮提交均带 `[skip ci]`，未运行全量测试、未触发 CI。详见 `docs/release-step5-integration-validation-20261029.md`。）
最后核对：2026-10-29（**发布前第四轮死代码清理完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `04c7df1ecf2aedd45d54b0b9d06ee47a97df3538`（第三轮返修五交付尖端，前三轮成果全部保留），分支 `fix/release-step4-dead-code-cleanup-20261029`。七项已无生产入口的死代码删除：旧设置页 `CredentialCard` / `CredentialModal`（连同三个专属测试）、`KnowledgeCandidateCard`（知识候选已由统一审批层直接 ingest）、`useComposerClearance`（输入区底部让位已由 MessageStream 按共享 composerMetrics 统一处理）、`usePresence`（浮层进出场已由 QModal/QConfirm 承担）、ui store 未使用的 `settingsDraft` 与已被 `saveCps` 取代的 `setCps`；同步清理滚动策略清单、ChatColumn 旧占位引用与设置页 tab 待办里的失效指向，旧测试/mock 不保留。受保护项未动：`frontend/src/components/credentials/CredentialForm.vue`（现仅首次引导使用）、`frontend/src/services/credentials.ts`、当前模型面板（KeyForm / KeyAccessSection）、统一审批链路、前三轮保存与恢复逻辑、历史迁移/兼容/恢复重试与历史验收记录。定向验证：前端定向 vitest（51 文件 / 423 项用例）、`npx vue-tsc --noEmit`、`python scripts/check_docs.py` 全部通过；**仅完成定向验证，未运行全量测试、构建或 CI**，提交使用 `[skip ci]`。详见 `docs/release-step4-dead-code-cleanup-20261029.md`。
最后核对：2026-10-29（**发布前第三轮返修五完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `966703f53d2153aa63e8e3ebe63f79bcdb2e5095`，分支 `fix/release-step3-settings-reliability-20261029`。输出速度保存：分别维护「用户最新选择」（ui.typewriterCps）与「实际已保存值」（加载确认值 + 本次接口成功返回，不从界面读取）；新增 ui.saveCps 返回本次接口真实结果且不写界面；沿用共享串行保存（同一设置依次、不同设置独立、关闭行为保持原处理）；等待期间保持最新选择，最新失败恢复最后实际成功值并保留错误，全部失败恢复最初确认值。固定清单六项（单次失败、25 成功/75 失败、25 失败/75 成功、两次失败含非默认初值、25/75/50 等待期间不被较早请求改回、最终成功/最终失败）均通过，另核对请求依次执行。提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第三轮返修四完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `2e902d637b4e75d3fc7fc1ec13a87ed809114a16`，分支 `fix/release-step3-settings-reliability-20261029`。① 提交顺序 ≠ 实际写入顺序：同一设置改为依次保存（后一次在前一次结算后才写入），实际写入顺序确定，回滚依据来自最后实际写入成功的响应；较新操作仍优先，不同设置独立并行；② 最终校准同步生效摘要：记录服务端返回的真实保存值，校准/失败回滚优先用 apply 同时刷新控件与 effectiveFacts（后台整理开关、间隔、对话深度同原则）；③ 免密钥开关适配共享规则：新增真实保存值与 trackSaved，回滚/校准不再用点击瞬间的界面值，并核对记忆/工具/搜索/常规全部 saveNow 调用方；开启先失败、关闭随后成功时最终显示关闭且错误保留。提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 定向 pytest + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第三轮返修三完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `15bbffb55c274cb2ec9b104f95bfdbe7b47fa330`，分支 `fix/release-step3-settings-reliability-20261029`。① 连续保存回滚：`trackSaved` 按写入序号记录真实保存值（迟到旧成功不回退更新的真实值），失败回滚读已记录真实值，并在全部在途结算、最新请求失败后执行最终校准；30、40 都成功（30 迟到）后再提交 50 失败恢复 40；30/40/50 中 50 先失败、40 成功、30 失败最终显示 40；记忆、对话深度与工具设置统一同一逻辑、不同设置独立。② 旧格式模型归属：发现状态新增模型来源签名，只有成功取回才更新，失败 / 空列表尝试不改变来源；旧格式优先用来源签名、其次用无错误的尝试签名兜底；更换地址或 Key 后新配置超时 / 空列表时，重新读取与缓存仍把旧模型列为历史，手动模型、历史记录与用途引用保留。提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 定向 pytest + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第三轮返修二完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `92438384037d609d99363c3780130fc50cf1465c`，分支 `fix/release-step3-settings-reliability-20261029`。① 连续保存回滚：每个设置记录最新请求结果与在途数，成功响应（含迟到成功）写回真实保存值，失败回滚读失败时刻的真实值；整理间隔 24→30 成功、40 失败最终显示 30（两种到达顺序），较新成功仍不会被旧成功覆盖；② 保存期间返回的初始读取：adoptBaseline 不作废在途保存、不清错误，仅更新取消基线，保存失败后取消回服务端保存值（记忆 12000 / 搜索条数 9）；③ 失败反馈不被读取清除：保存失败后迟到的初始读取不再清掉未解决错误（记忆 / 搜索两组）；④ 旧格式目录兼容：没有集合字段但已有发现来源签名时按签名区分当前 / 历史，换地址或 Key 后旧模型进入历史，历史行、手动模型与用途引用保留。提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 定向 pytest + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第三轮设置可靠性返修完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线 `b4c8afe868079b967090270589956cc31e9f3518`，分支 `fix/release-step3-settings-reliability-20261029`。① 失败回滚：工具记录/后台整理/对话深度分别维护已保存真值，保存失败回到真实值（90 改 30 失败仍 90、24 改 30 失败仍 24），不再拿输入值当基线；② 错误保留：按设置保留未解决失败，两个设置先后失败只修好第二个时第一个错误仍在原反馈区可见；③ 输入保护：工作区、迭代上限等入口在实际输入（update:modelValue）时就建立保护，迟到的初始读取不覆盖未提交输入；④ 取消恢复：组草稿新增 adoptBaseline / savedRevision，初始读取期间已编辑时保留输入但取消回到服务端实际保存值（记忆回 12000 而非 4096、搜索条数回 9 而非 5），并保护更新的保存结果；⑤ 模型获取归属与顺序：失败/空结果/成功在写库与写状态前统一校验「最新请求 + 当前配置」，旧配置超时与同配置较早请求都不能覆盖较新的成功结果，后端 stale=superseded/config_changed、前端静默退场；⑥ 当前目录与历史分开：记录当前配置的发现集合，获取结果/数量/缓存/重载后一致，旧地址模型进入 historical_models 保留（历史、手动模型、用途引用都不删除）。提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 定向 pytest + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第三轮设置可靠性修复完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：基线为已验收提交 `69c44558487aff3957140dab6a4b7f6bf36a2a92`（上一轮分支尖端，前两轮成果全部保留），工作分支 `fix/release-step3-settings-reliability-20261029`。A 独立设置保存（F08）：同一分区每个即时设置各领写序号，不同设置的保存在途互不作废；A 失败回滚自己的控件并保留错误，B 的成功不被静默覆盖；同一设置仍只认最新响应，且单字段保存只回写自己拥有的字段与生效摘要。B 初始读取不覆盖（F14）：记忆分段 / 后台整理 / 对话深度 / 联网搜索 / 工作区 / 工具记录的迟到初始读取，在用户已编辑或保存后跳过重置与生效摘要写入；组保存被作废后正确结束 saving，不再永久停在「保存中」。C 模型获取归属（F09）：表单与已保存连接按「配置签名（服务商 / 地址 / 协议 / Key）+ 请求身份」丢弃旧响应，不写模型列表、默认选择与获取状态；后端发现按配置指纹校验，重配期间的旧结果不写库、不改写发现状态，缓存按当前配置归属，手动模型 / 用途引用 / 历史保留。D 模型获取反馈（F12）：保存后的自动获取分别处理成功 / 空列表 / 业务失败 / 网络异常，全部结束「正在同步」并明确「列表未验证可用」；失败可重试获取且不重复创建连接。E Key 保存重试（F13）：未改内容的重试复用同一 request 身份、改内容才换；提交中重复点击 / 回车只发一次；后端同身份同内容幂等返回同一 key、同身份不同内容 409 且不改原记录。F 搜索完整性（F11）：话题 / 知识 / 实体显示真实命中总数，分批「查看更多」，第 9 条与最后一条都能打开定位；换词保护、失败重试与返回搜索上下文保留。本轮提交使用 `[skip ci]`：**仅完成定向验证（前端定向 vitest + 类型检查 + 定向 pytest + 文档一致性），未运行全量测试、构建或 CI**。详见 `docs/release-step3-settings-reliability-20261029.md`。）
最后核对：2026-10-29（**发布前第二轮返修三完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：分支 `fix/release-step2-ui-consistency-20261029`（基线 `08ac8aa3` 不变）。A 关闭行为：连续两次保存「后一次先失败并完成回读、前一次随后才写入成功」时，还有请求在途不把回读当最终结果，全部结算后补读服务端真实值作为终态，实际保存值/界面/桌面壳最终一致，不回退更早旧值，保留对较新选择的序号保护；B 执行记录更新：过程状态读取在途时收到 TURN_END 不再丢弃刷新要求，当前读取结束后自动补读最新状态（同话题才补读，话题变了丢弃以防旧响应覆盖），不轮询、不并发打同一接口。提交使用 `[skip ci]`：**本轮新提交仅完成定向验证（前端类型检查 + 定向 vitest），未运行 CI**。详见 `docs/release-step2-ui-consistency-20261029.md`。）
最后核对：2026-10-29（**发布前第二轮返修（验收遗漏）完成实现与定向验证；未合并 main、未打标签、未发布、未触发 CI**：分支 `fix/release-step2-ui-consistency-20261029`（基线 `cc52b150` 不变）。A 关闭行为：每次成功都采纳服务端持久值作为基线、最新失败后校准服务端真实值，连续保存「前成功后失败」不再回退更早旧值，界面/桌面壳与实际保存值一致；B 批量接口允许清除 `custom:<tag>` 历史覆盖（新增无效分配仍 422、整批仍原子）；C 用途判定读取中/失败时按用途必备能力 + 模型已声明能力本地兜底（明确缺能力仍不可选并说明、能力真正未知沿用既定规则），判定到达后以服务端为准；D 意图执行记录：确认版本进入 record_key（重新确认分开、自动重试合并）、写入确认时的名称快照、分页改为页内 head 且前端同 key 只渲染一次、确认/恢复/重试后无需手动刷新即出现并按 TURN_END 更新真实状态、板面结果入口可定位真实板面（失败有一句实话）。本轮提交使用 `[skip ci]`：**新提交仅完成定向验证（前端类型检查 + 定向 vitest + 定向 pytest），未运行 CI**。详见 `docs/release-step2-ui-consistency-20261029.md`。）
最后核对：2026-10-29（**发布前第二轮 UI 一致性修复完成实现与本地定向验证；未合并 main、未打标签、未发布、未开始下一轮**：基线 `fix/release-step1-model-reliability-20261028` @ `f5d0e744`（远端尖端即该提交，含第一轮已验收提交），工作分支 `fix/release-step2-ui-consistency-20261029`。A 关闭行为（F05）：`setCloseBehavior` / `load` 在真正写状态与同步桌面壳的位置领单调序号，旧响应（成功或失败）一律忽略；保存失败回到最近一次确认生效值并给出明确反馈；壳同步串行化，最后发起的那次最后到达。B 用途生效性（F06）：按真实调用点逐项核对后，code / research / video / audio / embedding / `custom:<tag>` 不再提供独立模型选择（界面只读说明 + 后端两个保存入口结构化 422 `usage_not_effective`），历史覆盖可读且标「历史配置（未生效）」、可清除；获得确认的生效用途（日常对话合并行 / 子任务 / 看图 / 记忆整理 / 知识提炼 / 实体提炼）照旧可选。C 候选按用途（F07）：catalog 按 `usage_key` 给服务端算好的 `usage_usable` / `capability_state`（supported / missing / unknown / not_required），明确缺能力禁选并说明、能力未知仍可选并标「能力未确认」、连接不可用沿用第一轮原因；保存校验复用同一 `model_usable(..., usage_key=...)`，批量仍整体成功/失败。D 意图执行记录（F10）：续作 Turn 的 user 消息落 `content_type="intent_execution"` + 程序事实（content 原样保留，模型上下文与可信绑定不动），服务端读取时派生 7 档真实状态、`record_head` 合并、attempts 时间线、逐项状态、结果入口与诊断编号；前端渲染为默认收起的「意图执行记录」，与用户消息区分，旧记录原样兼容。详见 `docs/release-step2-ui-consistency-20261029.md`。）
最后核对：2026-10-29（**发布前第一轮模型可靠性修复完成实现与本地验证；未合并 main、未打标签、未发布**：基线 `fix/settings-visual-20261028` @ `3fefb59e`，工作分支 `fix/release-step1-model-reliability-20261028`。四项：A 同一凭据下不同模型的 adapter 缓存不再互相关闭（按 `(key_id, model)` 分槽位；运行期不关闭仍可能被使用的连接，缓存与暂留均有界，应用退出统一释放）；B 并行后台派生工作改为任务上下文隔离（contextvars + drain 入口快照，不再共享实例可变配置），且 `build_derived_adapters` 在首次异步等待前冻结整批目标/不可用结果（准备第一种连接期间改设置只影响下一批）；C 模型目录、用途候选与真实调用对连接可用性同源（未验证、验证失败、停用、撤销、过期、预算耗尽、密钥不可读一致；legacy 与「连接已验证、模型未单独验证」不误禁）；D 「日常对话」合并行改为后端单事务原子保存，目录用与运行时同一 `resolve_shared_target` 的 `effective` 显示真实生效目标。详见 `docs/release-step1-model-reliability-20261029.md`。）
最后核对：2026-10-28（**设置页视觉修复（P97）完成实现与定向验证，未合并基线分支、未打包、未发布**：去掉设置页常驻灰底与药丸描边（含玫红外框、底边内阴影、单侧竖条），改为「标题 + 留白 + 对齐 + 中性细线」+ 单层轻玻璃承载面，选中态统一为品牌色文字 + 4px 圆点；开关改为线性轨道 + 滑块（保留同尺寸命中区）；下拉/候选/确认层改轻玻璃浮层。同时删除设置页「没有必要」的解释文字（安全提示、错误信息、限额/单位、状态词、许可与第三方声明全部保留），并把对话页话题详情卡的纵向节奏统一为一套固定间距。所有共享原语的覆盖都限定在 \`.qio-settings\` 作用域，其它页面不受影响；未改接口、保存与草稿机制、权限规则、搜索定位、首次引导与模型调用。详见 \`docs/settings-visual-fix-20261028.md\`。）
最后核对：2026-10-28（**设置页改版与模型/用途接入（P96）完成实现、集成与定向验收，未合并基线分支、未打包、未发布**：基线 `HebiIsHere/QIO` @ `ac0a83fde31f9f56edae68ca53a2a79e89a08e45`（分支 `feat/mp-20261027-integration`，开工核对该 SHA 即分支尖端，未改用更新 HEAD），集成分支 `feat/mp-20261027-settings-integration`，四条子分支各自独立 worktree 并行后由 Lead 合并。内容：设置页 8 个 tab 并为**五类**（常规 / 模型与服务 / 记忆与整理 / 工具与权限 / 关于与更新），宽窗左侧导航、窄窗顶部标签且**只换导航容器布局**（内容同一份 DOM）；去掉设置页自绘页头，保留应用全局栏；新增设置搜索（静态索引，结果给名称+分类+说明，点击切分类、展开折叠组、定位高亮、返回保留关键词与位置）；混合保存（即时 + 按组，分区独立反馈、递增 seq 丢弃迟到响应，离开页面直接丢弃草稿且不发额外写请求）；—— 模型侧改为「**一把 Key 一条连接 / 一条连接多个模型 / 用途分别分配**」（追加迁移 73/74，新增 `credential_models` 与 `model_usages`，默认模型落 `settings['model.default']`；用量账本仍只挂在连接上，不按模型复制额度）；新增模型目录发现（分页/刷新/缓存/错误分类，只有显式提交才发认证请求）、按模型验证（按连接配置指纹失效）、Key 详情四页签与引用保护；用途解析接进真实调用链（主对话/通用对话/子任务/看图/记忆整理/知识提炼/实体提炼），并把此前「只存不读」的搜索返回条数与正文字符预算真正接到工具默认值；联网搜索保持免密钥通道 + 博查 + SearXNG + 必应/百度回退顺序，新增通道状态与真实连接检查（区分「已配置」与「检查通过」）。**没有**改对话页、互动页、星球美术、权限规则、首次引导机制、后台整理算法与触发规则、更新与重启决定方式、许可证与第三方声明的离线内容来源；**没有**改其它页面圆角（设置页统一用既有 `--qio-ui-radius` = 14px）。未合并到基线分支、未打包、未发布；未接通能力与边界见 P96 与 `docs/settings-redesign-delivery-20261028.md`。）
最后核对：2026-10-27（**记忆星球页改版（P95）完成实现、集成与验收**：基线 `fix/ui-feedback-followup-20261027` @ `d8cdcc41`（开工核对：本地 = origin、远端无后续提交），集成分支 `feat/mp-20261027-integration`，三条子分支各自独立 worktree 并行后由 Lead 合并。内容：内容区在全局栏下方、星球默认居中、管理展开时球心移到内容区左边缘只露右半球（直径不变）；新增一套可往返的星球展开/收起过渡（独立姿态组 + 令牌曲线 + 中途反向速度衔接 + 减少动画一帧到位 + 静帧停绘）；管理区三标签 + 待处理/整理记录；记忆页话题概览与完整讨论记录；知识/实体列表详情与草稿化编辑（原子提交、409 冲突保留草稿、未保存先确认）；待处理三类真实事项；整理记录前后对比默认折叠、撤回复用中央弹窗；全局搜索覆盖三类。后端仅补实体修订的 relations 原子增删与 expected_revision CAS，无 schema / 依赖变更。美术未变：DEV 半径与基线逐位相同、结构色与环宽笔画峰相同、同姿态逐像素差 3.35%（未对齐 19.4%）、像素扫描直径 Δ-1px（基线自跑噪声地板 Δ37px）。未合并到基线分支、未打包、未发布；已知边界见 `docs/memory-planet-redesign-20261027.md`。）
最后核对：2026-10-27（**全应用提示/通知/弹窗统一改造完成实现与集成，未合并 main、未发布、未打包**：基线 `fix/post-merge-followups-20261026` @ `e5c80253`（远端未推进）；分支 `fix/ui-feedback-unification-20261026`，四个独立 worktree 并行完成后合入。唯一圆角令牌 14px、QIcon/QIconButton/500ms 用途说明、QModal/QConfirm 对称标题与按钮、自定义控件状态、通知调度（500ms 合并/约 5s/暂停恢复/事件身份去重/历史不重放）、全局异常单入口与根因去重、弹窗仲裁与关闭询问让位、审批输入法/失焦/覆盖层/收起不连弹/处理完回列表/真实期限、记忆星球本地处理与整批撤回中央弹窗、启动/更新/关闭真实阶段与可见失败、一次性初始设置提醒。验证：基线类型检查与定向抽样通过；全量前端轮（功能 SHA `bb3b7896`）477 文件 470 通过/7 失败，6 个为旧断言/测试隔离问题已修并定向复跑通过，1 个为基线既有并行 flaky；真实应用场景脚本、ui-catalog 71 项几何、场景展示页几何全部通过；完整 CI 在最终功能 SHA 上运行（唯一一次完成的完整运行；首个 push 的运行被最终视觉审计修复提交取代，未计为完成，详见最终交付记录）。详见 `docs/ui-feedback-unification-20261026.md`。**）
最后核对：2026-10-26（**后续修复轮（P91）已完成**：本地全量与完整 CI run 40257319813（9/10，唯一失败为测试跨线程 sqlite 问题，已修测试并 targeted-verify py3.12/3.11 双通过）、F 独立验收五项反例转绿与真实界面 480 证据见 docs/post-merge-followups-20261026.md；基线 `origin/fix/post-merge-integration-cleanup-20261025` = 373fda40（P90 分支 tip，未合入 main）、`origin/main` 仍为 1b03541e；本轮分支 fix/post-merge-followups-20261026（未合入 main、未打包、未发布）。范围：互动续作话题归属（原提交 binding.resumeState.topicId 优先）、旧 request 审批的 legacy 身份与恢复、/decide 拒绝 legacy 而 /respond 继续、执行结果写 approval_history 并发 APPROVAL_RESULT、预算 overrides 真实生效或显式拒绝、子任务通知持久化可靠投递（只追加迁移 71/72）、文档与第三方许可概括更正。收口：受测产品代码 8cb18828、完整 CI SHA 48b01f0c（run 40257319813，9/10；唯一失败为测试跨线程使用 sqlite，已修测试 0c47e11b 并 targeted-verify py3.12/3.11 双通过）、最终 HEAD 为本文件所在提交（docs-only 回填，见 docs/post-merge-followups-20261026.md）；未验证边界见 P91 与 `docs/post-merge-integration-cleanup-20261025.md` §12。）
最后核对：2026-10-26（**合并后前后端漏接修复与旧代码清理完成**：基线 origin/main 1b03541e，修复分支 fix/post-merge-integration-cleanup-20261025（未合入 main、未打包、未发布）。互动页权限模式徽标收起/展开常驻；审批最终结果与历史按 approval_id 去重持久可回看、两个聊天路由与非聊天路由均可达（0 待办也可查）；批量逐项结果、恢复失败可见可重试、480 窄窗队列可区分且键盘可达、子智能体预算旧创建路径真实生效且只读生命周期无假控件。删除 17 个已不挂载的旧前端文件及其旧测试/mock，旧浮层样式收敛为 styles/layers.css 唯一公共来源，batchOpen 死状态统一到 tasksOpen；保留 /respond、/demo/advance、历史迁移等真实消费者能力。D01/D02 更正上一轮 CI 台账与回退说明；真机桌面壳 7 场景通过。详见 docs/post-merge-integration-cleanup-20261025.md。）
最后核对：2026-10-25（**三分支整合（对话页 v2 / 互动页 v2 / R9-H13）完成实现、合并与验证**：冻结来源 origin/main 249a6344、feat/conversation-page-v2 13ec0935、feat/interactive-page-v2 9b236348、r9h13/main 6f56d815；开工后互动页线又交付 fix/interactive-v2-frontend-flow 28813463（F01–F10 前端操作逻辑修复），按「远端推进取最新完成交付」并入。19 个冲突文件逐一裁决（启动恢复三段并存、设置接口字段并集、WindowChrome 外观取 v2 + 关闭协议取 R9、board_tools 快照合并 display 与源文件事实）；数据库撞号解决为页面线迁移改号到 66 之后（67 task_feed / 69 P3-D，68/70 统一补偿，SCHEMA_VERSION=COMPENSATION_VERSION=70）+ 分支线前置兼容修复，五类来源库（空/main51/cv55/i2_57/r9_66）升级验证通过。整合说明与台账见 docs/integration-3way-20261025.md。**)
最后核对：2026-10-25（**互动页 v2 前端操作逻辑修复（F01–F10）完成并集成**：基线 `9b236348`，修复分支 `fix/interactive-v2-frontend-flow`，未合并 main、未打包、未发布。材料影响确认回到页面（真实受影响任务 / 材料 / 后果、「等待确认，尚未保存」、确认 / 取消、过期与保存失败处理，与工具审批同一时刻只有一个阻塞面）；意图视图收进 store 成为唯一维护位置（延迟理解、刷新、重试、确认 / 恢复后统一刷新，旧链重复预览让位，旧失败不覆盖新状态）；「任务」与批量入口接到真实任务面板（暂停 / 继续 / 取消、执行记录、聊天停止与任务取消分离）；恢复事项摘要独立于面板（稳定去重、失败不伪装为空、处理后立即减少、对话收起仍可打开）；意图读取失败保留旧内容并可重试；重复拖线只打开原关系、不覆盖含义 / 方向 / 曲线；菜单移出组给卡片找组外空位（不与剩余成员 / 组框重叠）；收起组内卡片定位先展开再按实际可用视野定位；关系胶囊拖动按缩放换算并支持 Esc / 中断 / 失焦取消；意图位置保存失败回到操作前位置、服务器位置优先。前端定向 / 集成测试与类型检查通过；真实页面（1440×900、700×800、480×760）逐流程验收与完整 CI 记录见 `docs/interactive-v2/frontend-flow-fixes-20261025.md` 与 `docs/interactive-v2/delivery.md` §9。）
最后核对：2026-10-25（**互动页 v2 交互修复第三批完成**：组内成员固定纵向排列（加入 / 重排 / 合并 / 拖出自动收拢，间距 12px，组框由排列结果重算），拖动成组不再重叠；生产局部工具栏改为卡片左上角定位，按上边容量逐个把按钮分配到上边/左边：先超过端点的按钮滑到左边，后续按钮压在上面并把前一个继续下压；其他按钮在上边向左补位；两边可同时存在按钮，按钮固定 30px、无托板。**本轮追加：端点命中区只保留卡片外侧半区（边框内不再抢点击），短条和悬停半圆都以贴边端为基准向外展开；缩小视野时短条不再以中心为基准侵入边框。组内成员按顺序显示 1..n 圆形编号；组被选中时边框与阴影高亮；组拖入时显示“插入这里”虚线占位，位置按放下后的插入下标落在两个成员之间；组内固定纵向排列、拖入热区按指针、拖组同帧、组头收紧和组表面统一均已完成。工具栏按钮位置改为按卡片实时显示宽高逐帧计算，拖动缩放时即时换边、无跳变；上边和左边容量都用尽后自动进入第二列；端口半圆的基座偏移修正，悬停前后中心误差 ≤1px。剩余：组拖入插入占位线和组视觉细节；工具栏锚点旧测试已更新为卡片左上角 + 逐按钮布局；前端全量 447 files / 4133 tests 全部通过。** 类型检查与构建通过。）
最后核对：2026-10-25（**互动页 v2 交互修复第二批完成**：卡片端点统一为固定 6px 外距，选中/未选中不再两套位置；选中卡片四边短条、未选中已连接空心点、悬停卡片四边短条、端口悬停向外半圆；关系线改走四边中点并停在外距后的端点中心，拖线预览同步偏移；旧 BoardResizeLayer 连接点只保留 DOM 兼容、视觉隐藏。图片重复加载、组落点按指针、组拖拽成员同帧、工具栏去托板/单卡五按钮、多选整理菜单、双击关系编辑、删除拖线横幅、组头收紧均已在上一批完成。**剩余：生产工具栏逐个按钮被端点挤出/两边分担、组内固定排列与插入占位、组缩放/拖出重排和组视觉最后细节。** 前端全量与构建通过，完整 CI 未重跑。）
最后核对：2026-10-25（**互动页 v2 交互修复第一批完成**：图片卡移动不再重复读取原图；组落点改为按指针位置判断，不再随拖动卡片左上角偏移；拖整组时成员卡片禁用 left/top 过渡，与组框同帧移动；卡片局部工具栏去掉白色托板，单卡只保留五个圆形按钮，多选时才显示“整理”菜单且修复面板被压成 30px 竖条；关系编辑改为双击说明打开并锚定在点击位置，单击只选中关系，编辑关系不再禁用缩放把手；删除开始拖线时的常驻提示横幅；组框改为与卡片同档圆角/边框/阴影，组头收紧。**剩余：卡片端点统一（外距/空心点/短条/半圆/连线接点）、工具栏被端点挤压后两边分担与补位、组内成员固定排列与插入占位、组成员拖出/缩放后的重排、组视觉细调。** 前端全量与构建通过，完整 CI 未重跑。）
最后核对：2026-10-24（**互动页 v2 卡片交互最终稿完成**：关系线为曲线，方向只用浅→深颜色表达、无箭头；拖动含义标签改变弯曲，`curve` 控制点随后端板面状态持久化，刷新 / 撤销 / 重做不丢；卡片边框为单条 SVG 环带路径（右下角 1→3px 连续加粗）；互动板矩形组件统一 14px 圆角；右下角缩放热区为贴合加粗边框的 L 形透明命中带；卡片只保留参与/不参与，许可徽章与勾选 UI 已移除；局部工具栏改为六个圆形图标，竖卡自动贴左边、宽卡贴上边；卡片端点已连接时空心点、悬停短条、悬停端口向外半圆；组端点只显示被关系使用的边缘锚点，拖到成员卡/组框分别连接卡片/组；对话输入区聚焦展开消息、右上角圆形收起。**前端全量与后端全量已通过，类型检查与构建通过；完整 CI 未重跑。**）
最后核对：2026-10-24（**互动页 v2 卡片交互与板面背景收口**：卡片取消四边/四角缩放热区，只在右下角用边框自身从 1px 短距离加粗到约 3px，保持圆角连续；连线改为四边外侧短条，悬停平滑变为圆点后拖出，线吸附卡片边缘；点阵移到整个视野背景，板面任意可见区域都有点阵且可平移/操作；工具栏仍为独立按钮行。**前端全量 443 文件 / 4088 项通过，类型检查与构建通过**。完整 CI 未在本轮重跑；旧组件文件仍未物理删除。细节见 docs/interactive-v2/delivery.md。）
最后核对：2026-10-24（**互动页 v2 第二迭代实现完成**：悬浮对话固定在底部居中，板面保持完整高度，消息向上展开、工具栏与输入区不动；工具栏去掉重复搜索，提交状态改为图标按钮并可展开详情；卡片取消“展开预览”按钮，按真实尺寸 420×280 进入、360×240 退出，图片方向/面积自动判断、极端比例保留小眼睛；板面改为白底主题色 24px/1px 点阵纸；原意图详情新增执行记录；互动页移除旧 BatchTray/StatusPopover/Workbench/ImpactConfirmDialog 挂载并删除 DemoIntentEntry。**前端全量 443 文件 / 4095 项通过，类型检查与构建通过**。旧组件文件与旧测试仍在仓库，尚未物理删除；执行记录暂只从原意图打开；完整 CI 未在本轮重跑。细节见 `docs/interactive-v2/delivery.md`。）
最后核对：2026-10-24（**互动页 v2 已完成四阶段开发与本地集成**：基线 `13ec0935`；共用全局栏/双向模式切换/唯一审批门控；InteractiveWorkspace 分层板面、底部工具栏与向上市聊、桌面侧栏/窄屏下侧书签；工具栏含添加/搜索/撤销/重做/标注占位/书签/提交；图片仍为文件卡附件并按原图比例与详情态渲染；参与讨论为唯一状态并与后端 visible_range 一致；组固定槽位/收起概览/拖整组/组缩放/组端点连线；虚拟意图层按 intent_id 保存 display，单条/批量显式 ids 确认；确认/恢复后服务端真实创建互动 Turn，重试系列持久化（首次失败 + 最多 3 次自动、手动一次一次、重启不重置）；固定提示词集中在 prompts.py。**前端全量 436 文件 / 4072 项通过**；后端定向 105 项通过；启动恢复与展示/重试 HTTP 接口已补测；**完整后端全量已在最新代码上通过（exit 0）**；**最终完整 CI run 40041195250 全部 job 成功（SHA 627d0670）**，包含前端、三套后端、两套 Rust、安装 e2e、文档一致性、冻结 worker 与 dedup。分支已推送至 origin/feat/interactive-page-v2；未合并 main、未打包、未发布；细节与已知边界见 `docs/interactive-v2/delivery.md`。）
最后核对：2026-10-22（**对话页 v6 品牌与反馈修订完成**：全局栏纵向收窄 62px → 52px（标题栏预留 80px → 70px），左侧品牌改为轨道标志 + PERSONAL AGENT；删除代码预览成功提示“已在右侧打开这段代码的只读预览”（失败原因仍保留）；logo 使用裁剪后的透明 PNG 资产，是否改 SVG 待确认。按用户要求**未跑全量测试**，只做定向测试、类型检查、构建与多宽度截图检查；未推送、未合并、未打包。）
最后核对：2026-10-22（**对话页 v5 视觉细节修订完成**：删除输入框下方的可见快捷键说明，并把 composer dock 118px → 104px、消息区 +14px（快捷键保留在 textarea 的 aria-label / title）；话题栏与模式切换的字体字符“⌄”改为 18px SVG 折线图标，旋转 SVG 自身并以几何中心为轴（28/24px 圆钮）；代码块“在右侧查看…代码”文字药丸改为代码预览胶囊（代码符号 + 语言 + 眼睛，文字只留 tooltip / aria-label）。按用户要求**未跑全量测试**，只做定向测试、类型检查、构建与多宽度截图检查；未推送、未合并、未打包。）
最后核对：2026-10-22（**对话页 v4 视觉修订完成**：背景 wash 提升为整窗 fixed 图层，覆盖全局栏四周；卡片之间统一 12px（顶栏→卡片、左栏→话题、话题→消息、左右预览；收起轨道保留同样槽位；外沿仍 8px）；全窗口预览改为与全局栏同一条 8px 卡片网格并修正 preview-panel.full 覆盖父层 inset 的问题；「回到最新消息」改为 38px 圆形下箭头按钮 + 未读 badge，中心对齐输入胶囊；附件 chip 改为单胶囊（文件符号 + 文件名/大小/状态 + 眼睛/⋯ 菜单，破坏性动作保留文字）；自绘滚动条保留。按用户要求**未跑全量测试**，只做定向测试、类型检查、构建与多宽度截图检查；未推送、未合并、未打包。）
最后核对：2026-10-22（**对话页 v3 视觉修订完成**：全局栏改为 8px 外缩 / 62px 高的圆角卡片（QIO + PERSONAL AGENT + 齿轮 / 窗口控制）；左栏、话题栏、消息框统一圆角并共用同一条顶线，去掉中央对话列的直角不透明底；「回到最新消息」改为锚定整个输入区外壳，不再压住入口行；输入区改三列 grid，星球槽与胶囊之间保证 ≥12px；输入胶囊去掉阴影；自绘滚动条扩大热区并支持按住轨道拖动。本轮按用户要求**未跑全量测试**，只做定向测试、类型检查、构建与多宽度截图检查；未推送、未合并、未打包。）
最后核对：2026-10-22（**对话页 v2（对话工作台）已完成实现、集成、本地验收与完整 CI**：顶部全局栏 / 左侧任务栏 / 中央消息流与输入区五入口 / 右侧只读多标签预览 / 统一审批与知识原子保存 / 记忆问题提醒与星球定位；任务栏有后端权威快照、分页与持久展示记录（迁移 55，号段避开并行任务的 52–54）；布局偏好走 `/api/settings/ui` 的 `conversation_layout`。**完整 CI（run 39762585293）全部 job 通过；前端全量 3745 项、后端全量 4610 通过 / 51 跳过、离线评估 sha16=d1a8ff768a8afad6、文档一致性均通过**。首轮完整 CI 曾因一个时区相关用例（本地 +08 通过、CI UTC 失败）红过一次，已修成时区无关断言后重跑全绿；细节见 P73 与 `docs/conversation-page-v2-merge-notes.md`。**未合并 main、未打包、未发布**。R9（统一授权 / 源文件引用 / 桌面退出）仍是并行任务，本任务只按共用边界对接、未重复实现。）
最后核对：2026-10-25（**R9-H13 审批与授权统一补修已完成实现与真实流程回归**：要求继承/恢复/去重按保存事实（A1-A3），对象判定过严纠正（B1-B3），读取先于内容（C1-C2），内容绑定与影响完整性（D1-D3），附件执行一致性与批量（E1-E3），内部维护三次确认统一强制（§8），链接语义（§10），子智能体范围（F1-F6），模式变化按当前规则（§12）；同时修正 H12 交付记录（三个绿色定向运行对应 245f76aa）。完整 CI 未新增（历史 2/2 已用尽），只做受影响测试与三平台定向 CI；未合并 main、未发布安装包。细节见 P88、docs/r9h13-report.md。）
最后核对：2026-10-25（**R9-H12 审批与授权补修已完成实现与真实流程回归**：冻结 UNRELATED 证据白名单（名称差异/查找失败/当前解析到别处/身份不可读/无完整绑定不再能证明无关）；保存候选优先的绑定恢复，当前 PATH 只作候选事实；限制路径身份不可读不再放行；三项原始复现（硬链接 alternate 绕过、恢复转移对象、显式路径身份不可读）修复；错误规则测试按契约修订并补可靠无关邻例。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P87、docs/r9h12-root-causes.md。）
最后核对：2026-10-25（**R9-H11 程序对象状态判定与跨操作一致性修复已完成实现与真实流程回归**：共同五态对象判定（有效匹配/可靠无关/绑定缺失/绑定失效/事实不足）接入读取/摘要/修改/删除/执行/审批恢复；历史无绑定不再经绝对路径执行或文件正文读取绕过；原路径替换后旧对象硬链接仍按保存身份受保护；编号复用/生命周期变化进入绑定失效或恢复；定向 CI 完成；**未合并 main、未发布安装包**。细节见 P86、docs/r9h11-root-causes.md。）
最后核对：2026-10-24（**R9-H10 程序限制绑定生命周期修复已完成实现与真实流程回归**：纯规范化/反序列化无绑定副作用、显式可信绑定创建、名称绑定跨文件操作、路径对象身份硬链接、绑定替换失效状态。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P85、docs/r9h10-root-causes.md。）
最后核对：2026-10-24（**R9-H9 共享规则修复已完成实现与真实流程回归**：目录完整性独立门禁、硬链接对象身份、程序绑定持久化、程序限制操作范围、只读固定查询共同检查。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P84、docs/r9h9-root-causes.md。）
最后核对：2026-10-24（**R9-H8 根因修复已完成实现与真实流程回归**：整体限制空对象语义恢复、程序名称按真实对象绑定、内部维护确认前不读正文且检测等待变化、批量禁读逐项、内部目录延迟读取、脚本摘要含义统一。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P83、docs/r9h8-root-causes.md。）
最后核对：2026-10-24（**R9-H7 四类审批授权补修已完成实现与真实流程回归**：程序入口与解析目标分离、内部维护确认前不读正文、删除摘要读取前核读取限制、执行前重核对先核读取权限。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P82、docs/r9h7-contracts.md。）
最后核对：2026-10-24（**R9-H6 五项审批授权补修已完成实现与真实流程回归**：补丁正文与前置摘要同源快照、目录逐项完整子项影响、补丁审批恢复读取限制、历史程序名称结构化恢复、普通写入读取限制前置。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P81、docs/r9h6-contracts.md。）
最后核对：2026-10-24（**R9-H5 六项审批授权补修已完成实现与真实流程回归**：fs_write/fs_patch 等待期间真实文件变化不覆盖、脚本审查事实绑定、patch 先读授权、批量审批逐项结果、结构化决策码替代中文 reason、程序限制只按明确路径/对象匹配。完整 CI 未新增（历史 2/2 已用尽），只做定向 CI；未合并 main、未发布安装包。细节见 P80、docs/r9h5-contracts.md。）
最后核对：2026-10-24（**R9-H4 审批与授权大修已完成实现、集成与定向验证**：真实操作多影响/限制继承精确解除/撤回与内容一致性/目录批量/内部身份 birth/审批并发/子智能体范围。完整 CI 未新增（累计 2/2 已用尽），最终代码只完成定向 CI；未合并 main、未发布安装包。细节见 P79、docs/r9h4-contracts.md。）
最后核对：2026-10-23（**R9-H3 第三轮后续补修已完成实现、集成与定向验证**：①已保存的用户限制先于模型审查/旧授权/人工批准/内部数据确认生效，命中即拒绝；②移除完全无限制模式的 targeted 独立授权审查，恢复仅受限模式审查；③正常可信写入提交后自动登记内部文件新身份，硬链接等访问路径立即延续保护。完整 CI 未新增（累计额度 2/2 已用尽）；最终代码已在 py3.11/py3.12/windows 定向 CI 通过；**未合并 main、未发布安装包**。细节见 P78、docs/r9h3-contracts.md。）
最后核对：2026-10-23（**R9-H2 第三轮后续补修已完成实现、集成与定向验证**：①历史 legacy 要求补充后按真实父子链重新理解完整有效限制，不再只喂补充句；②解析失败/超时/缺失/非法输出保存 unresolved，不再落成 structured/[]，旧错误记录按审计证据追加迁移纠正；③内部运行数据按真实对象身份（volume:file_id）识别硬链接/移动/改名，路径与身份双重保护。完整 CI 39958699070 最终 success（10/10 jobs）；**未合并 main、未发布安装包**。细节见 P77、docs/r9h2-contracts.md。）
最后核对：2026-10-23（**R9-H 审批与授权第三轮修复已完成实现与集成**：①要求解析成功后主流程从 AppliedResolution.requirement 取得真实有效绑定（new/amend 版本、withdraw 失效），原话/来源真实保存；②内容摘要/前置状态/启动规格/删除方式/条件贯通真实工具→引擎审查对象→授权记录/审批单/执行前重核对，内容变化不再复用旧授权；③名称/目录名/后缀不再直接决定权限（internal_data 与 computer 红线清理，内部数据只按真实注册路径与对象身份）；④结构化限制迁移 59/补偿 60 持久化、继承与恢复，后台/审批恢复/重启后仍生效；历史 legacy 要求由模型重新理解并记录依据。完整 CI 结果见 docs/r9h-merge-notes.md；**未合并 main、未发布安装包**。细节见 P76、docs/r9h-contracts.md。）
最后核对：2026-10-23（**R9-G 审批与授权机制修复已完成实现与集成**：①删除关键词/句式授权判断（关键词提取模块整体删除，审查守卫/引擎禁止表/命令删词扫描移除）；②批量操作逐项核对、逐项状态与单次合并审批；③操作绑定具体用户要求及版本（模型理解 new/amend/withdraw，后端校验，迁移 57/补偿 58），不再取「最近一条 active」；④审查/授权复用/人工批准/实际执行内容一致（独立操作摘要防篡改）；⑤每个实际操作开始前统一重核对模式/要求/授权/审批/确认/对象/内容/前置状态/条件。全量 pytest 与完整 CI 结果见 docs/r9g-merge-notes.md；**未合并 main、未发布安装包**。细节见 P75、docs/r9g-contracts.md。）
最后核对：2026-10-22（**R9-F 授权与执行修复 / 受限模式模型审查已完成实现与定向验证**：A 普通程序/shell/自建工具统一受控启动（spec_digest 前后核对、审批后重放重核对 payload/对象/程序，变化即 invalidated）；B 删除关键词授权、受限模式改为用户主模型单独授权审查（后端组装输入+结构/对象/操作/limits 校验+有界授权记录复用，模型不可用不默认放行）；C 文件来源持久化（迁移 55）与工作区 QIO 生成物自主；D 项目执行记录绑定 program/argv/cwd、不再靠目录命中；用户明确禁止三模式均生效。仅跑失败项/直接受影响定向测试，未跑全量 pytest/完整 CI；安装版 A-075 路径未复验。细节见 P74、`docs/r9f-contracts.md`、`docs/r9f-merge-notes.md`。**未合并 main、未发布安装包**。）
最后核对：2026-10-22（**R9 统一授权 / 源文件引用 / 桌面关闭与退出已完成实现、集成与定向/真机验证**：三种权限模式（只读/受限/完全无限制，默认受限）与统一 AuthorizationGateway、指令授权不跨要求继承、内部运行数据三连确认（全模式）、审批 v2（600 秒原始期限/持久化状态机/原子领取/重启恢复/非阻塞 deferred/执行前重核对）、本地附件改为源文件引用（不复制、文件身份与移动修复、旧数据迁移）、文件卡"作为互动材料"与引用信息、用户文件删除走系统回收站、桌面关闭行为（每次询问/最小化到托盘/直接退出）与有界真正退出。全量测试/完整 CI/安装版验收记录见 `docs/r9-merge-notes.md` 与 `docs/r9-acceptance-matrix.md`；**未合并 main、未发布安装包**。细节见 P73。）
最后核对：2026-10-21（**v0.2.10 已从 main 打包并发布：tag/release `v0.2.10` 已创建（更新签名，离线发布闸门 18/18 PASS，资产已上传）；未做真机安装验收**。**QIO 整体改造第四阶段（P72，最终阶段）已完成实现与本地集中验证；CI 崩溃根因已定位并修复，本地全量 4640 项通过，等待获准后的新一轮完整 CI 验收**：子任务保留控制字段与凭据自主选择、真实终态与原话题续作、工具开发完整版本证据与可用声明分离、放弃任务只读复用、自动化建议结构化与首次引导、旧 ToolCreator/旧理解链退出、提示词单源与显示名补齐。前三阶段 P69–P71 保持冻结。**未合并 main、未发版**；完整 CI 作为第一轮完整验收，结果以最终运行记录为准。细节见 P72、契约 §14 与台账阶段四部分。
最后核对：2026-10-21（**QIO 整体改造第三阶段（P71）已完成实现与定向验证**：互动模式接入完整主循环，12 个板面专属工具（意图提案/确认/创建/更新/移动/分组/排序/链接/等待/完成），授权范围与权威操作账本、版本 CAS 与幂等、提交—等待—续作持久关联与自动续作、问题一次呈现、普通模式不暴露板面工具、前端意图工作台/结果卡/局部失败与恢复。第二阶段 P70 的独立后台维护与 P69 的提示词单文件/统一组装保持冻结。本阶段仍只做定向验证与真实界面检查，**未推送、未合并 main、未启动完整 CI**；第四阶段从阶段三整合提交继续。细节见 P71、契约 §13 与台账阶段三部分。
最后核对：2026-10-21（**QIO 整体改造第二阶段（P70）已完成实现与定向验证**：独立后台知识维护（变化计数 / 20 条或 30 轮触发 / 快照推进 / 失败退避 / 单轮互斥）、维护动作（merge / normalize / ask，保留条件、归属、确定程度与多来源关系）、独立回答（revise / revoke / needs_clarification / deferred / no_change + 版本保护）、「记忆待确认」入口与维护记录、安全恢复（预览 / 逐条 / 整批 / 保护后续修改）、来源标签统一为「后台整理」。第一阶段（P69）的提示词单文件、统一消息组装与记忆基础保持冻结。本阶段仍只做定向验证与真实界面检查，**未推送、未合并 main、未启动完整 CI**；后续阶段从阶段二整合提交继续。细节见 P70、`docs/qio-refactor-contract.md` §12 与 `docs/qio-verification-ledger.md` 阶段二部分。
最后核对：2026-10-21（**QIO 整体改造第一阶段（P69）已完成实现与定向验证**：提示词统一到单文件 `backend/src/agent/prompts.py`、统一消息组装、结构化工具结果、摘要完整覆盖与滚动摘要覆盖位置（迁移 44）、知识新增审批与实体决策（`propose_knowledge` / `create_entity` / 证据目录 / 拒绝墓碑 / 稳定 ID 纠正）、`continue_from_fragment` 当轮上下文。本阶段按四阶段共同验证预算只做定向验证，**未推送、未合并 main、未启动完整 CI**；后续阶段从整合提交继续。第一阶段细节见 P69 与 `docs/qio-refactor-contract.md`、`docs/qio-verification-ledger.md`。）
最后核对：2026-10-17（本轮补齐**第三方组件声明与分发合规**：新增根目录 LICENSE（MIT）、THIRD-PARTY-NOTICES.md、licenses/ 与 CI 覆盖率门禁，设置页新增「关于」分区，见 P68；**v0.2.9 已打包**（提交 `22a4651a`，安装包 `QIO_0.2.9_x64-setup.exe` sha256 `c732ca5b…`，发布闸门 18/18 PASS，更新签名 key id `5af7e96c8e24bf8b`；**已发布**，tag `v0.2.9`），内容为**互动模式第三十八轮集中修复**（结果卡 Markdown、卡片几何权威化、统一落点与尺寸约束、结果卡许可续用、批次内类型化引用、几何上下文与空间排序），见 `docs/releases/v0.2.9.md` 与 P66。**v0.2.8 已发布**（tag `v0.2.8` → `a9e88a2d`，发布闸门 18/18 PASS，安装包 `QIO_0.2.8_x64-setup.exe` sha256 `60da302a…`，更新签名 key id `5af7e96c8e24bf8b`），内容为**底部功能栏布局重构**（第三十六轮，见 P64）与**图片展示简化**（第三十七轮，见 P65），见 `docs/releases/v0.2.8.md`。**v0.2.7 已发布**（tag `v0.2.7` → `2da1f5ee`，发布闸门 18/18 PASS，安装包 sha256 `2d428936…`），内容为**互动模式界面收敛与回归修复**：卡片表面收敛（文字卡编辑面 / 文件卡）、.md 文件卡做解析、西 / 北把手拉不动修复、图片卡封面回归修复，见 P63 与 `docs/releases/v0.2.7.md`；上一版 v0.2.6（tag `v0.2.6` → `18ea6e59`）是互动模式十个实机问题的修复与界面文案精简；v0.2.5（tag `fa9d0cfb`）是互动空间结构与自由尺寸调整，见 P36；更早的 v0.2.4（tag `1755c105`）是附件上传 CORS 预检热修，统一文字卡 / 持续许可 / 卡片内真实预览 / 多媒体对话见 P37）。**2026-10-14 第十二轮：统一滚动体验 / 对话不横向滚动 / 表格适配已完成（见 P39）**，并已合并进 main（`3a4f256e`，覆盖清单见 `docs/global-scroll-coverage.md`，合并材料见 `docs/global-scroll-merge-prep.md`）；同一轮还并入了 **0.2.5 互动模式两处回归修复**（附件成功提示常驻 / 连接点被尺寸热区遮挡，见 P40）。
**2026-10-14 第十三轮：板面操作提示不再常驻**（改为「本机没用过查看手势时显示一次」，说明并入帮助，见 P41）；
**第十四轮：对话窗口在短窗口下不再把输入框裁掉**（见 P42）；
**第十五轮：卡片局部工具栏不再压住卡片**（见 P43）；
**第十六轮：星球不再为空槽画融合环**（见 P44）；
**第十七轮：图片卡改为以图为主的封面式呈现**（见 P45）；
**第十八轮：卡片坐标夹在画布范围内**（页眉不再吃掉卡片顶部，见 P46）；
**第十九轮：对话窗口随窗口恢复 + 底部工具栏左对齐让出右下角**（见 P47）；
**第二十轮：文件与图片合并成同一种材料卡**（样式与标签按文件类型决定，见 P48）；
**第二十一轮：详情默认收起 + 新建文字卡默认提交给 QIO**（见 P49）；
**第二十二轮：板面 chrome 不再启动原生选择拖拽**（画布不再被「Ctrl+拖拽」滚走，见 P50）；
**第二十三轮：窄窗口下工具栏不再被压塌**（「详情压住查看过程」的收口，见 P51）；
**第二十四轮：按用户要求删除一批界面解释文案**（见 P52）；
**第二十五轮：修回归——合并文件/图片后图片预览消失**（见 P53）；
**第二十六轮：展开卡片拖动不再留白框**（偏移 122px → 46px，未归零，见 P54）；
**第二十七轮：卡片不允许叠放**（方案 A：落点被占就挪到最近空位，见 P55）；
**第二十八轮：展开卡片拖动白框归零 + 工具栏在平移/缩放后跟随**（见 P56）；
**第二十九轮：只移动位置不再弹提示**（见 P57）；
**第三十轮：宽高按钮不再出现 + 提示条浮在板面上 + 对话按钮固定右下角**（见 P58）；
**第三十一轮：工具栏准确跟随卡片**（拖动中与恢复选中都不再漂移，见 P59）。核对方法见文末。
**第三十二轮：画布随内容增长**（方案 B 第一步，见 P60）；**第三十三轮：画布向左上也无限**（原点重定位，见 P61）；
**第三十四轮：局部工具栏不再与卡片分离**（独立不缩放浮层 + 真实 DOM 外框锚点 + 单帧合并调度，见 P62）；
**第三十五轮：卡片表面收敛**（文字卡编辑面只剩工具栏 + 吃满卡片的输入框、完成编辑改为退出选中 / 再点编辑按钮、
换行修复、文件卡只显示文件名且按钮全在工具栏）与**西 / 北把手拉不动修复**（见 P63）。核对方法见文末。
**第三十六轮：底部功能栏布局重构**（长说明不再参与主栏横向分配；行列关系按功能栏**实际可用宽度**分档而不是视口宽度；「上次提交结果 / 本次提交范围与改动 / 上次提交过程」三类面板互斥展开，见 P64）。核对方法见文末。
**第三十七轮：图片展示简化**（图片统一为文件卡附件、常态即完整展示、取消卡片内展开预览与旧 `image` 卡片类型，
见 P65）。核对方法见文末。
**第三十八轮：结果卡 Markdown、几何权威化与续用协作**（卡片外框唯一权威、展开不再让卡片移位、统一落点与尺寸约束、结果卡 Markdown 与许可续用、批次内类型化临时引用、几何上下文、空间排序，见 P66）。核对方法见文末。

---

## 状态口径

| 状态 | 含义 |
| --- | --- |
| `completed` | 有实现、有自动化测试覆盖主路径，后续可以依赖 |
| `partial` | 主路径可用，但存在本节列出的已知限制 |
| `active` | 正在改，行为或接口可能变化 |
| `planned` | 只有设计，没有实现 |

**给后续编码 Agent 的硬性规则**

1. 不要在本文件写测试数量、事件数量、表数量这类会迅速过期的硬编码数字。要真实数字就去跑命令。
2. 一个能力只有在「代码 + 测试」都在仓库里时才允许标 `completed`。
3. 改完实现要同步改本文件。状态与代码冲突时，以代码为准，并立刻修正本文件。
4. 本文件与 `architecture.md`、`README.md` 的里程碑状态必须一致；`scripts/check_docs.py` 会检查这一点。

---

## 产品里程碑

### M0 — 骨架与事件协议

- **Status：** completed
- **Implementation：** `backend/src/agent/main.py`（`create_app` 工厂）、`api/server.py`（路由）、`api/events.py`（事件信封与类型）、`api/bus.py`（订阅扇出 + 重放缓冲）
- **Tests：** `backend/tests/test_events.py`、`test_events_bus.py`、`test_api_routes.py`
- **Known limitations：** `POST /api/events/test` 只在开发模式（`QIO_DEV_INSECURE=1` 或 `QIO_ENABLE_TEST_EVENTS=1`）注册，生产构建里这条路由根本不存在，且同样要求会话认证；事件类型集合会随功能增长，数量不写死在文档里。
- **后续依赖：** 无（其余里程碑都建立在这一层上）。

### M1 — 存储层

- **Status：** completed
- **Implementation：** `storage/schema.py`（顺序迁移，当前 schema 版本见 `SCHEMA_VERSION`）、`storage/migrate.py`、`storage/db.py`（WAL）、`storage/archive.py`（冷归档）、`storage/settings.py`
- **Tests：** `backend/tests/test_storage.py`、`test_settings.py`
- **Known limitations：** 归档与维护由后台任务触发，默认不是常驻高频；SQLite 单写者，写入串行化由后端负责。
- **后续依赖：** M2/M6/M7/M8 的表都通过迁移追加；改动 schema 必须新增一条迁移，不能改历史迁移。

### M2 — 凭据层（BYOK）

- **Status：** completed
- **Implementation：** `credentials/store.py`（密钥进 keyring，元数据进 SQLite）、`credentials/policy.py`（按标签解析 + 快照）、`credentials/providers.py`（厂商预设的唯一来源：名称 / 协议 / 地址 / 建议模型 / 类别）、`services/verify.py`（只对**用户选定的地址**做一次真实调用验证）
- **Implementation（2026-09-15 身份边界）：** `endpoint` 视为凭据的**安全身份**而不是普通元数据：变化必须重新输入 secret 并显式确认（`confirm_reconfigure=true`），否则 HTTP 层与 store 双层拒绝；默认只允许 HTTPS，明文 HTTP 仅限 loopback 本地 provider。主 Agent Loop 的凭据解析改为 **`main-loop` 标签优先**（以前排序把专项凭据排在前面，一个 `vision` Key 会被主循环静默拿去用），专项标签只在没有 main-loop 可用时回落。
- **Implementation（2026-09-21 后端可用性）：** 凭据后端的解析改为**惰性**：`CredentialStore` 构造期不再探测系统 keyring，
  第一次真正读写密钥时才解析并缓存。语义上读写不对称是有意的 ——
  **读路径**（`get_secret` / `get_default_secret`）在这台机器没有可用后端时如实返回 `None`
  （应用本来就有「当前没有可用凭据」的降级路径），**写路径**（存 / 轮换 / 删除）仍然大声抛错，
  绝不静默降级到 no-op 后端。动机：headless 环境（CI 的 ubuntu runner、容器、无 SecretService 的机器）
  没有任何可用后端，而旧的构造期抛错会让应用工厂与大量测试在启动阶段直接失败。
- **Implementation（2026-09-28 保存与验证分离）：** 删除「把同一把 Key 依次探测十几家候选厂商」的自动识别模块：
  厂商由用户明确选择，Key 前缀只做**本地**提示，验证请求只发往选定的那一个地址，
  失败也不会转投别家。`credentials` 表新增 `kind` / `verify_state` / `verified_at` / `verify_error` / `is_default`
  （迁移 21，只追加）：历史凭据回填 `legacy`（按老行为视为可用，不误标未验证、不停止使用），并把第一条可用的
  主对话凭据回填为默认项。新建凭据缺省用途 = 主对话；显式提交空用途返回可理解的中文错误（不静默覆盖）。
  保存 = 必要校验 + 安全写入 + 一次自动可用性验证，两者分别反馈（「已保存，模型可用」/「已保存，尚未通过验证」）；
  写入失败不留半条记录。未通过验证的凭据不进自动选择（`CredentialPolicy.resolve` 只认 `verified` / `legacy`），
  因此首次引导也不会误判为已配置完成。显式默认项只影响**合法候选之间**的排序，不绕过停用、撤销、预算与用途限制；
  第一条验证可用的主对话凭据自动成为默认，后续新增不替换（重试与换钥都作用在同一条记录上，带 `client_request_id`
  的重复提交按确定性标识去重）。验证与正式对话共用同一套协议判断（OpenAI 兼容走 `probe_adapter`，Anthropic 走
  `probe_anthropic`），「拿到模型列表」不再被当作「模型可用」。
- **Tests：** `backend/tests/test_credentials.py`、`test_identify.py`、`test_credential_verify.py`、`test_tool_credentials.py`
- **Tests（2026-09-15 追加）：** `test_credential_identity.py`（只改 endpoint 必须被拒、https 默认、loopback 例外）、`test_credential_routing.py`（main-loop 优先、专项凭据只作回落、无匹配用途不得拿别的标签顶上）、`test_credential_endpoint_api.py`（HTTP 层同一套规则）
- **Known limitations：** 真实凭据读写只在 Windows 凭据库上验证过（headless 环境没有可用后端时，
  读路径返回「无凭据」、写路径报错）；预算以 token 计数为主（界面也按 token 显示，不再出现人民币符号）；
  预设里的「建议模型」只是推荐值，是否真的可用由保存后的一次实际调用决定，因此没有可靠默认模型的服务
  （聚合/自定义）要求用户自己选一个模型；`用量上限` 按「进 + 出」合计与真实计量对齐，运行期每次模型调用
  都会累计（主循环、子 agent、后台维护共用 `credentials/usage.py` 的同一份归因），界面把进 / 出分开显示。
- **后续依赖：** M3 适配层、M10 子 agent、embedding 选档都从这里取 Key。

### M3 — 模型适配层

- **Status：** completed
- **Implementation：** `adapters/base.py`（内部 `Completion` / `ChatMessage` / `ToolCall` 契约）、`adapters/native.py`、`adapters/text.py`、`adapters/anthropic.py`、`adapters/probe.py`、`adapters/model_context.py`、`adapters/errors.py`（内部错误分类）
- **Tests：** `backend/tests/test_adapters.py`、`test_anthropic_adapter.py`、`test_adapter_contract.py`、`test_adapter_errors.py`、`test_model_context.py`
- **Known limitations：** `unsupported` 档默认拒绝启动，需要用户显式打开强制继续；`text` 档的能力弱于 `native`（不切换话题）。
- **后续依赖：** M4 主循环、M5 选择器、M9 上下文组装都消费这一层的内部模型，不直接接触任何厂商 SDK 对象。

### M4 — 主循环

- **Status：** completed
- **Implementation：** `core/loop.py`（PLANNING → TOOL_EXEC → OBSERVING → DONE）、`core/budget.py`（迭代 + token 双预算，native 128 / text 64）、`core/guard.py`（重复失败护栏）
- **Tests：** `backend/tests/test_loop.py`、`test_loop_continue.py`、`test_runaway_guard.py`、`test_budget_defaults.py`
- **Known limitations：** 迭代上限可在设置中覆盖，但不会无限；护栏按「同一调用重复失败」计数，命中后 WARN/BLOCK/HALT。
- **后续依赖：** M9 上下文组装与 M10 工具执行都挂在主循环的规划步上。

### M5 — 选择器与工具路由

- **Status：** completed
- **Implementation：** `selector/`（规则层、BM25、ONNX 本地嵌入、远程嵌入）、`services/tool_router.py`（查询只嵌入一次 + 工具描述批量缓存 + 条件暴露）
- **Tests：** `backend/tests/test_selector.py`、`test_tool_router.py`、`test_tool_router_cache.py`、`test_embedding_identity.py`、`test_onnx_backend.py`、`test_remote_embedding.py`
- **Known limitations：** 远程嵌入与本地 ONNX 都需要额外配置或模型文件；缺失时降到 BM25 / 规则层，功能可用但召回质量下降。精排层默认关闭。
- **后续依赖：** M9 检索排序、M10 工具候选展示都复用这一层。

### M6 — 记忆域

- **Status：** completed
- **Implementation：** `memory/ingest.py`、`memory/fragment.py`、`memory/summary.py`、`memory/index.py`、`services/memory_lifecycle.py`（封块、滚动摘要、预算压力整理）
- **Implementation（2026-09-15 第三阶段 · 按轮封块）：** 封块阈值以前数的是**消息条数**，而设置页一直写「标准（10 轮）」—— 一轮 = 用户 + 助手两条消息，所以「10 轮」实际只有 5 轮。现在 `FragmentManager(max_turns=...)` 只数 `role='user'` 的消息（工具消息不计入），并且**最后一条是 user 消息时不封块**（否则第 N 轮的助手回答会被写进下一个片段）。设置键正名为 `fragment.max_turns`（读写与运行时共用 `memory/fragment.py::resolve_max_turns`，旧键 `fragment.max_messages` 只作为一次性迁移回退并写回新键）。
- **Tests：** `backend/tests/test_memory.py`、`test_injection_short_term.py`、`test_turn_short_term.py`
- **Tests（2026-09-15 追加）：** `backend/tests/test_fragment_turn_semantics.py`（10 轮 = 20 条消息才封块、工具消息不计轮、半轮不封块）
- **Known limitations：** 摘要与知识提炼依赖主模型调用，失败时降级为原文直引；索引是机械生成，模型不可写。
- **后续依赖：** M9 注入以此为素材；M12 维护任务读取片段做整理。

### M7 — 知识域

- **Status：** completed
- **Implementation：** `knowledge/lifecycle.py`（状态机 + supersedes 版本链）、`knowledge/verify.py`（分层验证）、`knowledge/inject.py`、`tools/knowledge_correction.py`（对话式纠错）
- **Implementation（2026-09-15 第三阶段 · 高影响候选进对话）：** 高影响候选（`user_profile` / `agent_self` / `goal`）不再只留在 `pending_review` 等用户主动去 Knowledge Panel 发现 —— `services/memory_lifecycle.py` 把本轮新建的高影响候选登记下来，turn 收尾（回答完成之后）由 `AppContext.emit_knowledge_candidates()` 发 `KNOWLEDGE_CANDIDATE`，前端在**回答完成后**以低干扰卡片给出「保存 / 修改 / 忽略」。新增 `POST /api/knowledge/{id}/ignore`：状态转 `revoked` 并在 `provenance` 记 `ignored_at`，同一内容不再重复提示；低影响候选仍自动生效、不进对话。Knowledge Panel 保留浏览 / 修正 / 归档 / 审核历史，但不再是高影响候选唯一的确认入口。
- **Tests：** `backend/tests/test_knowledge.py`、`test_knowledge_correction.py`、`test_knowledge_mgmt_api.py`
- **Tests（2026-09-15 追加）：** `backend/tests/test_knowledge_candidate_event.py`；前端 `stores/__tests__/phase3Cards.test.ts`（候选只在 `TURN_END` 之后出现、保存 / 修改 / 忽略与失败可重试）
- **Known limitations：** 高影响类别必须用户确认；隐式反馈与自动整理属后续项，见本文末「尚未完成」。
- **后续依赖：** M9 只把 `active` 条目纳入注入面。

### M8 — 图导航层

- **Status：** completed
- **Implementation：** `graph/nodes.py`、`graph/edges.py`、`graph/anchors.py`（Anchor 生命周期：位置校验/恢复/推进）、`graph/topics.py`、`graph/layout.py`、`entities/`（识别、抽取、卡片）、`tools/topic_tools.py`、`tools/continue_tool.py`（Agent 显式 `continue_from_fragment`）、`tools/entity_tools.py`
- **Implementation（2026-09-15 第二阶段 · 话题导航与 Planet 浏览景观）：** 新增 `services/navigation.py::TopicNavigationService` 作为 Anchor 的**唯一写入者**（进入话题 / 创建话题 / 确认切换 / 从历史继续），`/api/anchor`、`switch_topic` / `create_topic` / `continue_from_fragment` 与 turn 编排全部改走它；`tests/test_topic_navigation.py` 里有一条源码扫描守卫测试，任何绕过 Navigator 直接写 anchor 的模块都会让它失败。
  「从历史继续」语义修正为**新建接续片段**（迁移 11 增加 `fragments.source_fragment_id`），旧片段零改动，Focus 读来源片段；新增「待确认切换」：用户明确说「切到 X」直接执行，预测器推测只发 `TOPIC_SWITCH_SUGGESTED` 事件并等用户表态。Planet 侧新增 `services/planet.py`（轻量概览 + 确定性浏览序列 / 可前进可后退的游标）与三层数据接口 `GET /api/planet/overview`、`POST /api/planet/browse`、`GET /api/fragments/{id}/messages`；`GET /api/graph/topics/{id}` 不再内联 Message 原文，`message_count` 改为真实计数。星球不再是「固定球面坐标 + 前 16 个话题」，而是「数据层无上限、视觉层固定 16 个槽位、旋转推动话题流」的浏览景观（前端 `planet/browseSession.ts`、`planet/layoutSlots.ts`、`planet/dotPool.ts`、`planet/browseFlow.ts`）。
- **Implementation（2026-09-22 · 工具导航归属）：** `services/navigation.py` 增加工具导航登记（`note_tool_navigation` / `take_tool_navigation`，turn 标记走 ContextVar、回传走共享表，因为子 task 的写入不会传回父上下文）；`tools/topic_tools.py` 的 `create_topic` / `switch_topic` 成功后登记，`services/turn_orchestrator.py::persist` 在写回答前落实；`continue_from_fragment` 刻意不登记（只影响后续提交）。用户导航（另一个请求）读不到本轮 turn 标记，登记不上，因此行为不变。
- **Tests：** `backend/tests/test_graph.py`、`test_anchor_event.py`、`test_anchor_lifecycle.py`、`test_continue_fragment.py`、`test_entities_recognizer.py`、`test_entity_cards.py`、`test_entity_correct.py`、`test_entity_extract.py`、`test_entity_inject.py`、`test_entity_retrieval.py`、`test_topic_tools.py`、`test_topic_dedup.py`、`test_tool_nav_rebind.py`
- **Tests（2026-09-15 追加）：** `test_planet_browse.py`、`test_planet_api_layers.py`、`test_topic_navigation.py`、`test_topic_switch_policy.py`、`test_anchor_no_advance.py`；前端 `planet/__tests__/browseSession.test.ts`、`layoutSlots.test.ts`、`dotPool.test.ts`、`browseFlow.test.ts`、`stores/__tests__/topicSwitch.test.ts`
- **Known limitations：** 实体懒创建依赖提及计数阈值；星球视图的**当前展示布局**在前端按稳定种子临时生成（后端不再为渲染提供永久坐标，`nodes.meta.layout` 仅保留兼容）；浏览排序（哈希 + 近期活跃加权 + 曝光抑制）没有做过体验评估与调参；触控板手势与真机 GPU 帧率未验证。详见 `docs/release-planet-phase2.md`。
- **后续依赖：** M9 的亲和度与 M10 的 `switch_topic` / `create_topic` 都依赖锚点。

### M9 — 注入与检索

- **Status：** completed
- **Implementation：** `services/context.py`（ContextAssembler：Focus 块 `标题 + 摘要 + 开头 2 条 + 省略标记 + 结尾 3 条`，受 `FOCUS.max_tokens` 硬上限）、`services/injection.py`（三面聚合 + **按稳定身份去重**：Focus / 短期记忆已给的片段不再从检索重复注入）、`services/retrieval.py`（命中携带 `fragment_id`）、`services/affinity.py`、`services/token_budget.py`（TokenBudgetPlanner + completion reserve + 预算分解）、`services/decay.py`（分类型时间衰减）、`services/params.py`（集中阈值，含 `FOCUS`）
- **Tests：** `backend/tests/test_service_injection.py`、`test_injection_short_term.py`、`test_token_budget.py`、`test_decay.py`、`test_affinity.py`、`test_focus.py`（含 Focus 尾部结论、Token 上限、开放片段、去重）、`test_turn_no_duplicate_query.py`
- **Known limitations：** 注入上限是硬约束，强制项超预算时走确定性截断；阈值集中在 `services/params.py`，改动需要 eval 支撑（见 P3）。
- **后续依赖：** 无下游；被 P1/P2 的 turn 流水线调用。

### M10 — 工具创建生命周期

- **Status：** completed
- **Implementation：** `tools/creator.py`、`tools/lifecycle.py`、`tools/dev_tools.py`、`tools/dev_workspace.py`、`tools/tester.py`、`tools/sandbox.py`、`tools/policy.py`、`tools/approval.py`、`tools/subagent_tool.py`、`tools/task_manager.py`、`storage/tool_store.py`
- **Implementation（2026-09-15 第三阶段 · 创建进度与审批表达）：** 工具创建以前在界面上是一串彼此无关的工具卡（`create_tool` → `dev_write_file` → `dev_run_tests` → `dev_submit_tool`），用户看不出走到哪一步。现在 `tools/dev_tools.py` 的 `ToolCreateStatus` 出口按 **`group_id`（开发工作区）** 发 `TOOL_CREATE_STATUS`，phase 为 `proposal / building / testing / testing_passed / testing_failed / waiting_approval / registering / ready / failed`，前端同一张卡原地推进；失败（测试没过 / 用户拒绝 / 超时 / 凭据不可用 / 注册冲突）都带可理解的中文原因。同时 `tools/approval_present.py::describe_tool_call` 把每次工具调用翻译成「想做什么 / 会访问什么 / 影响 / 一次性还是长期」（`description` / `access` / `capabilities` / `scope`），工具执行的审批不再是 `fs_write path=...` 这种只有工程师能读的形式。
- **Tests（2026-09-15 追加）：** `backend/tests/test_tool_create_events.py`、`test_approval_present.py`；前端 `components/__tests__/ApprovalModal.test.ts`（授权范围与访问清单）、`stores/__tests__/phase3Cards.test.ts`（同一 group_id 一张卡）
- **Tests：** `backend/tests/test_tool_lifecycle.py`、`test_dev_tools.py`、`test_dev_workflow_integration.py`、`test_tool_policy.py`、`test_computer_sandbox.py`、`test_subagent.py`、`test_subagent_integration.py`、`test_tool_parallel_cancel.py`、`test_tool_registry_reversible.py`、`test_tool_restore.py`、`test_tool_store.py`、`test_tool_schema_present.py`、`test_tool_pipeline.py`、`test_tool_event_isolation.py`
- **Known limitations：** 受限子进程不是强安全隔离，而且**不强制**文件/网络隔离——实测声明为 PURE 的工具仍可读取用户目录。当前强制力只来自「按声明拒绝高风险」+「剥离环境变量」+「凭据不进工具结果」，谎报能力的工具拦不住。高风险能力在没有可用 Docker 时直接拒绝执行，不做静默降级。子 agent 异步并行上限见 `tools/task_manager.py`。
  隔离说法与「不保护什么」只有一个来源（`tools/policy.py` 的 `isolation_label` / `unprotected_surfaces`，按**真实执行器**推导）；威胁模型与分阶段方案见 `docs/security/tool-execution-isolation.md`。**强制隔离未实现**，不要把它读成已实现。
- **Implementation（2026-09-14 任务04 确认调度）：** 后台任务请求确认不再无条件抢焦点——用户正在输入（输入框/文本域/可编辑区）时到达的审批
  只入队并亮出常驻入口「⚠ 有 N 项操作等待确认」（`components/ApprovalEntry.vue`，顶部居中，避开右下角浮动组件），用户主动点开才显示窗口；
  没在输入时仍立即弹出（直接相关的确认不延迟）。窗口新增「稍后处理」＝只收起窗口、保留待审批任务（不批准也不拒绝）；**Esc 改为按最上层处理**：
  收起窗口、不做决定、入口重新亮出（此前 Esc 完全无效，用户只能被迫做决定）。收起/关闭后焦点归还被打断的输入框（实测回到凭据表单 `INPUT.qio-input`，
  未提交内容不丢）。设置页把两个浮动开关改名为「星球入口贴边后自动隐藏」「设置入口贴边后自动隐藏」，并把取值文案写成「贴边后自动隐藏：开/关」，
  明确**关闭＝入口常显，不是禁用**。
- **后续依赖：** 无下游。
- **性能测量（任务06，dev 与正式构建各一遍，2026-09-15 补）：** 脚本 `scripts/baseline/t06-perf.mjs`（只测量，不改产品源码）。条件：headless Edge/Chromium + ANGLE/SwiftShader 软件渲染、1440×900、DPR 1、估算 123Hz、隔离数据 23 个话题、当前话题 20+ 条消息；dev = Vite 5199，prod = `npm run build` 后 vite preview 5299。七场景实测（dev / prod）：设置往返 **110.4 / 110 fps**（p95 8.5 / 8.4ms，>50ms 0 / 1；点击到设置出现 80 / 67ms、返回 38 / 33ms）；长回答真流式期间切页 **89.6 fps**（p95 16.7ms，>50ms 5）；**生成中打开星球+选话题+展开侧栏 12.9 / 12.9 fps**（p95 250 / 233ms，>50ms 28 / 27；点击到覆盖层出现 212 / 181ms）；**多话题+管理列表旋转与连续选择 11 / 12.1 fps**（p95 166.7 / 99.9ms，>50ms 52 / 49）；大代码与表格滚动 **120.8 / 120.8 fps**（p95 8.4ms，>50ms 0）；连续开关页面与菜单 **114.4 fps**（菜单反馈 1ms）；后台确认到来时输入 **120.2 fps**（>50ms 0）；连续缩放窗口 117 fps；减少动画运行时切换 121.1 fps。**结论：正式构建与 dev 几乎一致 → 瓶颈不在打包方式，而在星球 3D 场景（软件渲染下）；文字与布局场景全部稳在 p95 ≤16.7ms。**
- **性能测量暴露的未完成项（有数字支撑）：** ① 页面标记为隐藏后星球仍在渲染（实测隐藏前后各 1s 的 rAF 帧数 11 → 12），没有「不可见时暂停装饰动画」；② 反复开关后监听器/观察器计数仍在增长（net listeners 88 → 星球×10 后 809 → 设置×10 后 2119；ResizeObserver 4 → 14 → 54），**是否挂在长生命周期目标上并真正泄漏尚未定论**（当前只统计创建/移除次数，未逐目标核对）；③ 关闭后中心点 `elementFromPoint` 命中的是悬浮球内部图形（`ellipse`），无覆盖层残留；④ 星球每次打开都重建整个 three.js 实例（冷启动实测 1.5–3.4s）、侧栏过渡期间逐帧 resize+render，仍未优化。
- **性能优化第一批（2026-09-15，按上面证据做）：** ① **不可见时暂停渲染**：`usePlanetScene` 监听 `visibilitychange`，隐藏时 `cancelAnimationFrame` 停掉渲染循环、恢复时重置时间基准（避免暂停时长被当成一帧 dt 造成跳变），并暴露 `setPaused()` 供调用方使用；实测把页面标记为隐藏后，同一页面 1s 采样从星球在绘时的 **12 帧** 变为 **102 帧**（重负载消失 → 渲染循环确实停了）。② **resize 去重**：画布尺寸没变化（或 `display:none` 导致 0×0）时不再 `setSize` + 补帧，省掉侧栏过渡期间的重复清缓冲/重绘。③ 复测：生成中打开星球/选话题/展开侧栏 **12.9 → 15.1 fps**、管理列表旋转与连续选择 **11 → 14.6 fps**（软件渲染下星球仍是最重场景，属于填充率/着色器成本，真机 GPU 结论待测）。④ **监听器净增长已定性为「不是泄漏」**：按目标类型细分后，窗口与文档的净监听器反而下降（window net 14 → −25、document net −7 → −218）；元素监听器净增 2451 个，但 1743 个曾绑定监听器的元素里 **只有 40 个仍在文档中**，其余 1703 个已随 DOM 移除（可回收）。ResizeObserver 计数增长只反映构造次数，各处在 `onUnmounted`/`onScopeDispose` 里都有 disconnect（按代码检查，未在运行时验证 disconnect 调用次数）。⑤ 仍未做：星球**实例复用**（现在每次打开仍重建 WebGL 场景，冷启动 1.5–3.4s；ConversationView 目前用 `:key` 保证「旧回调不关新页面」，改成常驻复用需要单独一轮验证）。
- **Implementation（2026-09-14 任务04 收尾 · 设置页移动与数字控件）：** 进入设置改成整页只做短淡入、不再整体位移（去掉 `translateY(2px)`）；
  切分类只让右侧内容短淡入（`panel-in`，左栏与页头保持不动，面板仍用 `v-show` 保留，切分类不重建表单、不丢未提交草稿）。
  `QNumber` 新增 `unit` 属性，维护间隔直接显示「24 小时」；加减按钮宽度 22px→30px 并加 `touch-action: manipulation`。
  实测：入场期间 8 次采样 transform 全为 `none`，左栏位移 0.0/0.0、页头位移 0.0，右栏动画名 `panel-in`，加减宽 30px。

### M11 — 前端

- **Status：** completed
- **Implementation：** `frontend/src/views/`（对话页、星球页、设置页、调试页）、`frontend/src/components/`、`frontend/src/stores/`、`frontend/src/planet/`、`frontend/src-tauri/`（桌面壳）。2026-09-12 稳定化：审批失败保留待审批项并可重试（失败 ≠ 已授权）、锚点切换以后端成功为准、token 用量按 `turn_id` 归属、流式 Markdown 增量渲染、流式自动跟随（上翻即停）、QNumber 统一 commit 语义、设置页分区反馈与凭据留空不清除、星球详情竞态防护、浮动组件单击不贴靠且 resize 保持贴靠关系。
- **Implementation（2026-09-15 第三阶段 · 状态表达与事件收口）：** 事件集合与后端 `EventType` 完全一致（守卫测试 `backend/tests/test_event_protocol.py`：集合相等、每个事件都有生产者、都必须被前端消费）——删掉 `MEMORY_INJECT`（前端写了 case、后端从来不发；普通用户不需要知道「注入了 4 条记忆」，开发者改看 `/debug` 的单轮 `injection`），补上 `TOOL_START` / `TOOL_CREATE_STATUS` / `KNOWLEDGE_CANDIDATE` / `CREDENTIAL_STATUS` / `FALLBACK` / `APPROVAL_RESULT` 的消费分支。工具卡变成「开始时立刻出现运行中、结束时按 `call_id` 原地更新」（不再等结束才可见、不再出现重复卡，并显示耗时与失败结论）；`SUBAGENT_STATUS` 独立成「独立任务」卡（不再混进普通工具卡，按 `task_id` 原地更新）；工具创建是一张卡（`ToolCreationCard.vue`）；高影响知识候选在**回答完成之后**以低干扰卡片出现（`KnowledgeCandidateCard.vue`，保存 / 修改 / 忽略，修改是很轻的内联编辑）；全局只保留一句整体状态（正在处理 / 正在使用工具 / 等待你确认 / 正在处理独立任务 / 正在整理独立任务的结果），不再暴露内部事件名；审批弹窗新增「授权范围：仅这一次 / 长期生效」并优先显示具体访问清单。
- **Tests（2026-09-15 追加）：** `stores/__tests__/phase3Cards.test.ts`（TOOL_START→TOOL_END 同一张卡、独立任务按 task_id 更新、工具创建按 group_id 推进、候选只在 TURN_END 后出现、凭据/降级提示不含内部标识、notify 轮不清排队标记）、`components/__tests__/MessageStream.test.ts`（整体状态文案与「不出现内部事件名」）
- **Implementation（2026-09-28 凭据表单统一）：** 首次引导的「连接模型」与设置页的凭据弹窗改为**同一个组件**
  （`frontend/src/components/credentials/CredentialForm.vue` + `services/credentials.ts` 的 `useCredentialForm`），
  默认值、校验与提示语不再有第二份实现。基础区只保留厂商（可搜索的 `QCombo`）、API Key（可显隐）、用途
  （默认「主对话」，可点「修改」多选）与「取消 / 保存」；地址、协议、模型、显示名称、用量上限、自定义标签与
  只读的内部标识都收进默认收起的「高级设置」。主按钮固定「保存」（执行中「保存中…」+ 独立的进度/结果区，
  提交期间禁用按钮并中断旧请求：关闭表单、切换厂商或改 Key 之后，旧结果不会覆盖新状态）。选厂商即补齐地址、
  协议与建议模型；「其他 / 自定义服务」直接展开必填项；拿不到模型列表时可手填。凭据卡默认只给结论
  （名称/厂商、模型、中文用途、当前默认标记、启用状态与验证状态），地址/内部标识/版本/审计收进详情，
  常用入口是「编辑」与「更多操作」，「换钥」改名「更换 API Key」（先验证新 Key 再原子替换，失败保留原凭据）。
  文案明确：在 QIO 里撤销或删除凭据**不会**吊销厂商账户里的 API Key。
- **Implementation（2026-09-28 续 · 引导页合并按钮）：** 首次引导「连接模型」这一步只有一个主按钮：
  该保存时它是「保存」，保存并验证通过后自动进入下一步；已经有可用凭据又没在填新的时它是「下一步」。
  「跳过」只在它比主按钮多做一件事时才出现（确实有一份没保存的填写）：已有可用凭据且没在填新的时，
  「跳过」与主按钮是同一件事，因此只保留主按钮；没有任何可用凭据时它仍是「先不配，往下走」的出口。
  表单自己那一行「取消 / 保存」在这一步隐藏（`showActions=false`，由引导页调用表单的 `submit()`）。
  验证没通过就停在原地，原因与「重试验证」显示在表单里。同一个表单里如果**已经写过库**，
  后续的保存一律作用在那条记录上：钥匙没变 = 只重试验证（不写库、不涨版本），钥匙变了 = 原子替换
  （失败保留原凭据），改地址/协议仍要求显式确认 —— 避免「改掉打错的 Key 再点保存」意外新建第二条凭据。
- **Implementation（续 2026-09-14 体验轮）：** 复制的诚实反馈（剪贴板不存在/写入失败一律显示「复制失败」，不再假装成功）、草稿连续（输入草稿存 `session.draft`，切页不丢；发送失败回填草稿并撤掉未获受理的乐观消息）、排队请求失败不再清除仍在运行的任务状态、只有本机发送才把消息流拉回底部（后台任务开始不打断向上阅读）、星球详情加载失败独立可见且可重试（与「从这里继续」的错误分开）、QNumber 手输在真实父组件绑定下也会在失焦/回车时提交一次、知识页筛选框有可见标签与「全部」回退项。动效：设置与星球出现/消失 170–260ms、星球相机 420–460ms、边栏重新居中和宽度过渡同时发生、脚本相机补间遵守 `prefers-reduced-motion`。设计规则同步在 `docs/superpowers/specs/2026-08-09-qio-frontend-design.md`。
- **Implementation（2026-09-14 状态一致性补齐）：** 阅读位置随会话保留（上翻阅读 → 设置/星球 → 返回恢复到原位置，恢复期间的程序性 scroll 不参与跟随判定；容器未完成布局时重试有上限）；设置页记住上次分类与分类列表滚动位置（存 ui store，仅同次运行期）；星球首次数据加载失败有可见失败条与重试；四个设置保存路径（记忆/对话深度/搜索/维护）加请求归属序号，旧响应不回填、不用陈旧成功盖住新的失败；起点请求在页面已关闭时失败会回到对话页可见；代码复制按钮每个独立计时（连续复制不同代码块各自复位）、失败时选中代码作为手动复制退路、`@media (hover: none)` 下默认可见（触屏可发现）；审批失败后焦点回到对话框。
- **Implementation（2026-09-14 动画与反馈统一）：** 动画参数按用途分层落到令牌（按下 80ms / 开关选中 150ms / 菜单弹窗 170ms / 设置打开 220ms / 返回 170ms / 侧栏 210ms / 星球打开 300ms、关闭 200ms / 聚焦 320ms 且短距离 0.6×，曲线 `--ease-out` 打开、`--ease-in` 关闭），并去掉唯一的 `transition: all`；菜单与弹窗补齐出现与退出（退出结束从 DOM 移除，菜单退出期间不拦截点击、模态退出期间保留遮罩拦截），折叠的队列列表也补了短过渡；新增「跟随系统 / 标准 / 减少动画」偏好（`localStorage(qio-motion)` + `html[data-motion]`），CSS 过渡与星球相机补间读同一份结果、运行中切换立即生效，减少动画或时长为零时跳过退出阶段；按钮按下反馈立即开始（`.qio-btn/.qio-select/.opt/.tag-chip/.qio-btn` 独立 80ms 位移），处理中用 `min-width` 固定尺寸避免周围跳动，开关「关闭」与「禁用」视觉上区分；运行中任务与排队项分别用「停止」「取消排队」，取消请求期间显示等待确认、失败留下可见错误；队列项的旧 `TURN_END` 不再结束当前运行状态；流式正文标记 `aria-busy` 且不设 live 区域（不逐字播报）。
- **Implementation（2026-09-14 聊天布局与阅读连续性）：** 对话内容改为**一条居中内容列**（860px）——回答与用户消息同列左右对齐，不再分别贴窗口两端，长文仍受 68ch 行宽限制；输入区固定在内容列正下方（中等窗口为星球入口留通道，≤899px 整宽贴底），默认中性边框 + 较轻阴影，聚焦只加强外框一层（内层 textarea 不再叠边框与光晕）；底部留白仍按输入区高度动态预留，实测滚到底时最后一条内容在输入区上方。滚动：上翻阅读时出现「回到最新消息（N）」，点击才滚动并恢复跟随；自动滚动改用可被滚轮/触屏中断的 rAF 短滚动；贴底前**再次确认用户意图**（布局等待期间用户上翻就不滚）；等待布局与恢复位置都不再依赖固定延时。生成状态只表达可观察阶段：已提交且无助手内容时「已提交，等待模型响应…」，有增量后由流式气泡表达「正在生成」，等待动画只在回答附近播一次（任务列表改静态「运行中」标记）；新消息最多一次 170ms 入场（历史消息不带），长回答积压超过 200 字时该段显示压到 600ms 内追上已到达内容；已取消条目提升到 opacity .75 保持可读。
- **Implementation（2026-09-14 阅读位置收尾）：** 两处「阅读位置」缺口修掉：（1）**本机发送被后端拒绝时把阅读位置放回发送前**
  （新增 `session.sendRejectedSeq` 信号 + 发送前快照；被拒时不进入跟随、「回到最新消息」的虚假未读数作废；用户在请求途中自己滚过则不强行放回。
  实现上必须从判定那一刻起就忽略 `scroll` 事件，否则贴底补送的迟到 `scroll` 会把状态重新判成跟随、把位置冲回底部）；
  （2）**「回到最新消息」在长对话里不再停在半路**（虚拟列表滚动中逐条测量使总高度持续变大，平滑滚动改为每帧重算目标，实测从「距底部 5346px」变为 0）。
  验收脚本同步校准：`A5` 断言恢复为「阅读位置不变」，测试事件的 `turn_id` 每次运行唯一（后端会重放最近事件，固定 id 会让上一轮的 `TURN_END` 混进来），
  `A3/A5` 发送前 `Esc` 收起重放出来的「等待确认」窗口。综合验收 19/19 通过。
- **Tests：** `frontend/src/**/__tests__/*.test.ts`、`frontend/src/smoke.test.ts`、`frontend/src/styles/tokens.test.ts`
- **Implementation（2026-09-15 状态真实性）：** 最终回答的唯一权威来源是 `TURN_END.final_content`：最后一条是「工具前说的中间话」时不再被当成最终答案，`TURN_END` 没有内容时也不会把中间话升级成答案（`stores/events.ts` + `stores/session.ts::applyFinalAnswer` / `markLastAssistantInterim`，中间话始终带 `interim` 并以「◈ 过程」呈现）。`ERROR` 只显示错误、不再结束界面上的 turn（结束只认 `TURN_END`），同一 turn 的重复 `TURN_END`（重连重放）只生效一次。历史读取有 `idle/loading/ready/error` 状态：失败时保留已加载的消息并在对话顶部给「历史记录暂时无法读取 + 重试」（安静的一行，不是错误横幅）。提交后立即用 POST 返回的 `turn_id` 打开停止能力。取消的结局在顶部安静地写「已停止」，不当错误。
- **Implementation（2026-09-15 桌面边界）：** Markdown 链接按协议白名单渲染（只允许 `https/http/mailto`；`javascript:`、`data:`、`file:`、`vbscript:`、协议相对地址、含控制字符的伪装一律退化成纯文本，见 `frontend/src/utils/externalLink.ts`），点击外链不再让 WebView 自己导航而是走 Tauri 官方 open（`plugin:shell|open`），打开失败有可见反馈；`tauri.conf.json` 的 `csp` 从 `null` 换成生产最小权限策略 + 独立的 `devCsp`（字体/图标是 `self` + `data:`，本机后端与 SSE 走 `connect-src http://127.0.0.1:*`）。
- **Known limitations：** 桌面壳只在 Windows 上验证过；应用内浏览器有模块缓存，改前端后需带 `?fresh=N` 强刷。斜杠命令体系未实现。代码块复制仍依赖 `navigator.clipboard`：受限 WebView 下写不进去时现在会显示「复制失败」并选中代码留出手动复制退路，但不保证写入。草稿、阅读位置、设置分类与**非敏感表单草稿**只活在**当前运行期内存**里（刷新页面不保留，也不跨设备）；凭据密钥一类敏感输入不进这份草稿。星球帧率未做过真实设备采样（只测量了状态变化与过渡时长）；聊天区的流畅度同样只有状态与时长测量，没有帧率曲线。后台审批的调度已按任务 04 重做：用户正在输入时不自动弹出（只亮出「有 N 项操作等待确认」入口，由用户主动打开），打开后可按 Esc／「稍后处理」收起并保留待办，焦点回到被打断的表单（实测凭据表单 `INPUT.qio-input`，未提交内容不丢）。仍未验证的是**后端确认成功出队**的端到端路径：假 `approval_id` 不被后端受理，成功出队后的焦点归还只有前端单测覆盖。「停止当前回答 / 取消运行条目」已统一为同一个对象（当前运行的任务），「取消排队」是不同对象、文案保持区分；三种取消的**事件顺序**仍未逐条实测。迟到 `TURN_END` 已加防护并有单测，但真实的取消与完成同时到达仍未在运行中的应用里复现。输入区改为与内容列对齐属于**默认布局变化**：输入区本身不可拖动、没有用户保存位置（星球/设置入口的浮动位置与贴靠未被覆盖，「还原默认布局」照旧）。
- **性能测量口径（2026-09-14 补测）：** 用页面内 rAF 采样记录帧间隔，环境是 **headless Edge + SwiftShader 软件渲染**，只用于发现卡顿、不代表真机 GPU。三段实测：滚动长对话 120.7fps（p95 8.4ms、>50ms 的帧 0）、长回复增量到达 105fps（p95 16.7ms、>50ms 的帧 0）、**星球打开/拖动/关闭 40.5fps（p95 91.8ms、>50ms 的帧 28）**。结论：目前唯一出现长帧的是星球 3D 场景，且处于软件渲染下；真机结论待任务 06 复测（可先评估把环球网格 128×96 降到 96×64，环宽按像素算，不受影响）。
- **触屏与键盘（2026-09-14 补测）：** 触屏用 CDP 合成触摸事件验证（非真机）：直接点复制入口得到「已复制」；向下滑动消息区后停止跟随并出现「回到最新消息」。消息区加了 `tabindex` + `aria-label`，键盘 PageUp 能滚动并停止跟随、End 回到最新；真机手势未验证。
- **星球展开/收起（2026-09-14 演示版）：** 用户反馈原时长「过快、过渡不连贯」，改为两段式：展开＝140ms 铺底 + 420ms 内容淡入/相机收敛（相机一开始就在接近最终构图，不再从远景小球放大）；收起＝180ms 收势（沿视线轻微后撤 + 星球降到 25% + 面板退场）+ 280ms 整体淡出后卸载。实测关闭 531–541ms（原 225ms）、减少动画下 9ms 直接卸载。演示开关 `?planetdemo=slow`（时间线 ×3）用于逐帧观察；**这组时长比任务 02 记录的动画参数更长，是按用户直接反馈调整的（规则同步在设计规范里）**。
- **Implementation（2026-09-14 任务05 星球：加载、选择与管理）：** 打开时按「起点未变就回到上次浏览的话题、起点变了以起点为准」定位（浏览记忆只在运行期内存，`composables/planetSession.ts`）；数据未回来显示「正在加载话题…」、失败显示原因 + 重试，懒加载 chunk 期间 `PlanetBoot` 立即铺底显示「正在打开星球…」，入口按下即 `scale(0.96)`。详情区改为三段式：顶部固定（页签/标题/搜索）、中部可滚动（话题列表自身滚动且高度封顶 42%，详情正文与它各自滚动）、底部固定操作区，浏览器实测操作区不随滚动移动、不覆盖正文（y 793.8 → 793.8，中部底边 794）。面板固定显示「浏览的话题 / 选中的片段 / 已生效的起点」三行，起点取自服务端返回的会话状态。话题点：悬停显示简短名称（移开即消失）、选中话题有持续可见的选中环（修掉了「用世界坐标写局部位置，环跑到球面别处」的坐标错误）、命中范围放大到屏幕 16px/14px、列表项可聚焦并用 Enter/Space 选择、Esc 先收边栏再收星球；辅助网格减弱（经线 45°→60°，透明度 0.35/0.18→0.22/0.1、0.13→0.07），球体结构/融合环/聚焦距离 2.25 未动。收起阶段整层 `pointer-events: none`，父级用「打开序号 + key + seq」保证关闭动画中的迟到回调不会关掉新打开的一层；知识/实体面板把「读取失败」「暂无记录」「没有匹配」分开，并提供重试与清除筛选。
- **Implementation（2026-09-15 第二阶段 · Planet 浏览景观）：** 星球改成窗口驱动：`VISIBLE_CAPACITY = 16`（正面可见约 8 个），数据层不再有「只显示前 16 个话题」的上限；`loadTopics()`（把全部话题聚簇后切前 16 个）被 `attachBrowse(session)` 取代，话题点来自固定容量对象池 `planet/dotPool.ts`，进出只改数据、不 new / dispose。旋转按方位角累计（0.4 rad 一步）推动话题流；**位置规则 = 打开时在整个球面随机铺开（`spreadPositions`）+ 拖动摇动时新话题在球体背面随机落点（`randomBackPosition`）+ 静止时窗口内位置完全不动**。真的掉头才把刚离开的话题连同原位置放回，继续同向旋转一律引入新话题。选中话题在查看期间锁定、不会被回收；搜索或列表命中的话题会注入展示窗口。相机后撤（planet 2.6→2.9、focus 2.25→2.6）并加**纵向限位 35°~145°**（`POLAR_LIMIT`，程序性相机移动同样受限），球体完整落在画面内、四边留白。开发构建里提供只读调试钩子 `window.__qioPlanetWindow()`（生产构建不注册）。
- **后续依赖：** 无下游。

- **任务 04/05 追加（2026-09-14）：** 审批弹窗改为按「它想做什么 → 会访问什么 → 会改变什么 → 为什么需要 → 验证了吗（已验证/未验证）→
  折叠的高级详情（策略指纹、逐条测试结果、raw params）」组织；高风险操作的批准按钮降调为中性实心（不再像品牌色在推荐你点），
  低风险才用主操作色；失败文案先给结论「未做出任何授权」。设置页补上保存模型标注（自动保存分区「修改后自动保存」、
  搜索分区「开关即时保存、数字参数点保存生效」）与「保存中…」等待态；输出速度保存失败不再只写控制台；
  关闭免密钥搜索且无博查/SearXNG 时说明「当前没有可用的联网搜索通道」。净白主题的 `--success/--danger/--warning`
  与 `--text-faint` 按实测调深（旧值在小字上只有 3.16–4.14:1，达不到 4.5:1）；`scripts/baseline/qa/contrast.mjs`
  客观审计现为 **0 项不达标**，另有 2 项「非文本边界」建议项未改（描边对页面底色约 1.6–1.7:1；输入框另有高对比聚焦态，
  加深全部描边会改动整体视觉语言，属待定取舍）。修掉两个状态真实性缺陷：①「从这里继续」切换话题后对话页不再停留在旧话题
  （新增 `session.setAnchorAndSync`：锚点真的变化才 `loadHistory()`；SSE `ANCHOR` 在 `turnRunning` 时不重载以免擦掉流式内容）；
  ②停止按钮在「已提交、服务端运行标识未到」期间不再显示为灰的「停止」（改为「准备中…」，拿到 `turn_id` 后才叫「停止」）。
  聊天页新增 `.skip-link`「跳到输入框」作为第一个键盘焦点位——实测每条消息的复制按钮会占满 Tab 顺序（前 8 个停靠点全是它）。

### M12 — 离线维护任务

- **Status：** completed
- **Implementation：** `services/maintenance.py`（矛盾扫描、Dreaming 整理、轨迹→工具候选）、`prompts.py`
- **Tests：** `backend/tests/test_m12_maintenance.py`、`test_m12_trace.py`
- **Known limitations：** 属离线任务，不在运行时关键路径上；失败被隔离，不影响主循环；需要模型调用时走主循环凭据。
- **后续依赖：** 无下游。

---

## 工程强化

以下不是产品里程碑，而是对已实现能力的重构与加固，按引入顺序编号。

### P1 — Turn Runtime 边界与并发契约

- **Status：** completed
- **Implementation：** `core/turn.py`（`TurnContext` + `TurnManager`：主 turn single-flight、FIFO 排队、可取消，**并且是 turn 生命周期的唯一事实源**）、`services/turn_orchestrator.py`（单轮流水线）、`POST /api/turns`（受理即返回 `turn_id`）、`POST /api/turns/cancel`
- **Implementation（2026-09-15 生命周期协议）：** 一个被受理的 turn **恰好**产生一次 `TURN_START` 与一次 `TURN_END`（终态 `completed / failed / cancelled / unavailable`，由 `TurnManager` 在 `finally` 里收口），任何异常路径都不会再让界面停在 running；`POST /api/turns` 不再只回 `accepted`，而是同步返回 `{turn_id, status}`，前端不必从 SSE 里猜请求身份。`AgentLoop` 不再发 `TURN_START` / `TURN_END` —— 它同时被 subagent、维护任务、工具开发流水线复用，以前一个子 agent 的 `TURN_END` 会把用户的主 turn 提前结束。取消（`TurnContext.cancelled`）在「模型调用前后 / 工具调用前后 / 下一次迭代前 / 持久化最终回答前 / 收尾记忆处理前」逐点检查：取消后不再发起新的模型或工具调用、不保存后续内容为正常最终回答、不推进锚点、不做记忆整理，`TURN_END.status = cancelled`；底层 HTTP 请求无法物理中断，但返回值会被丢弃。
- **Tests：** `backend/tests/test_turn_manager.py`、`test_turn_concurrency.py`、`test_turn_identity_events.py`、`test_turn_cancel_api.py`、`test_turn_no_duplicate_query.py`、`test_tool_event_isolation.py`、`test_turn_lifecycle_protocol.py`（四种终态各恰好一个 `TURN_END`、无凭据 → `unavailable`、收尾阶段抛错仍收口、取消不落库）
- **Known limitations：** 这是「可靠的单飞主 Agent + 明确排队」，不是多用户并发 Agent Server。契约细节见 `architecture.md` 的并发契约一节。
- **后续依赖：** P2 的 Trace 以 `turn_id` 为主键。

### P2 — Trace 与可观测性

- **Status：** completed
- **Implementation：** `trace/model.py`、`trace/store.py`（`turn_traces` 表）、`trace/recorder.py`、`trace/redact.py`（统一脱敏）、只读 API `/api/traces`、前端 `/debug` 视图
- **Tests：** `backend/tests/test_trace_store.py`、`test_trace_api.py`、`test_trace_redact.py`、`test_trace_turn.py`
- **Known limitations：** Trace 存的是标识、摘要预览与计数，不是完整 prompt；默认脱敏后才是 safe-to-inspect，不要往里塞原文。
- **后续依赖：** 无下游；是调试与未来 eval 的事实来源。

### P3 — Context 预算、衰减策略与 Eval 框架

- **Status：** completed
- **Implementation：** `services/token_budget.py`、`services/decay.py`、`services/params.py`、`agent/eval/`（`topic_eval.py`、`retrieval_eval.py`、`run.py`）、数据 `backend/evals/`、基线 `backend/evals/baseline.json`
- **Tests：** `backend/tests/test_token_budget.py`、`test_decay.py`、`test_eval_regression.py`
- **Known limitations：** 评测集是小型、确定性、离线数据集，指标用于**防退化**，不代表生产质量；它不在运行时关键路径上，跑评测失败不应阻塞对话功能。
- **后续依赖：** 调整 topic/retrieval 阈值前应重跑并对比基线。

### P4 — 工具能力安全、沙箱与执行策略

- **Status：** completed
- **Implementation：** `tools/policy.py`（能力分级 + 指纹）、`tools/sandbox.py`（按策略收紧）、`tools/approval.py`（能力展示；审批绑定 turn/session + 过期 + 摘要 + 单次使用）、`services/tool_router.py`（缓存与条件暴露）
- **Implementation（2026-09-15 命令与文件边界）：** 命令模型拆成两个工具：`run_program`（`program + argv`、`shell=False`、只读程序白名单 + 参数校验，白名单内可自动执行）与 `run_shell`（交给系统 shell 的自由命令，**每次都要审批**，`plan` 模式直接拒绝）。`ComputerSandbox` 新增 `classify_argv` / `shell_verdict` / `command_verdict_for_program`：含 `| > && ; $(` 等元字符的命令至少 DANGER，修掉「按字符串前缀判安全、却把整条字符串交给 shell」的错配。文件工具统一 `resolve → containment(ComputerSandbox.root()) → 权限判定 → 执行`：相对路径与省略参数以工作区根为基准（不再用 `process.cwd()`），`..`、根外绝对路径、指向根外的 symlink/junction（含嵌套）都只能走审批；`fs_find` 不再沿符号链接目录走出根外。
- **Tests：** `backend/tests/test_tool_policy.py`、`test_computer_sandbox.py`、`test_tool_router_cache.py`、`test_tool_router.py`
- **Tests（2026-09-15 追加）：** `test_shell_boundary.py`（链式命令不得判低危自动执行、`run_shell` 永远要审批、`run_program` 参数校验）、`test_fs_containment.py`（默认根不是 cwd、`..`/根外绝对路径/symlink-junction 逃逸）、`test_approval_binding.py`（单次使用、过期、错 turn/session、摘要不符）
- **Known limitations：** 见 `architecture.md` 的沙箱安全契约；扩大能力必须重新审批，不能沿用旧授权。
- **后续依赖：** M10 的工具创建流程依赖这一层的策略判定。

### P5 — Provider 边界、依赖锁定与 CI

- **Status：** completed
- **Implementation：** `adapters/errors.py`、`adapters/base.py`（`finish_reason`）、`backend/uv.lock`、`.github/workflows/ci.yml`、`scripts/setup_env.ps1`
- **Tests：** `backend/tests/test_adapter_contract.py`、`test_adapter_errors.py`、`test_embedding_identity.py`；CI 本身在 push / PR 上执行
- **Known limitations：** 后端本地只在 Python 3.12 验证过，3.11 由 CI 矩阵验证；CI 只对 Rust 做 `cargo check`，不产出完整 Tauri 安装包。
- **后续依赖：** 无下游。

### P7 — 集成验收（Dogfooding / E2E / 视觉）

- **Status：** completed
- **Implementation：** 无新增产品功能；本阶段以验证为主，产出 4 个修复：`agent/tools/sandbox.py`（超时终止子进程 + 清理不再抛异常）、`agent/tools/runtime_tools.py`（输出按策略截断）、`frontend/src/components/MessageStream.vue`（悬浮星球不再遮挡消息）、`scripts/e2e-checklist/run_memory_tests.py`（陈旧 API / 缺判空 / 断言污染 / 陈旧 fixture / 外键清理）。
- **Tests：** `backend/tests/test_p7_*.py`（8 个文件），其中两个沙箱回归做了红绿验证；端到端见 `scripts/e2e-checklist/run_memory_tests.py`。
- **Known limitations：** 完整报告与未修项见 `docs/release-qualification.md`。
- **后续依赖：** 无下游；结论供后续迭代参考。

### P8 — 本机 API 边界与 turn 生命周期协议

- **Status：** completed
- **Implementation：** `api/auth.py`（`SessionAuth`：会话令牌校验 + origin/Host 策略 + 一次性 SSE ticket）、`api/server.py`（认证中间件、CORS 白名单、`/api/instance`、`/api/events/ticket`；`/api/events/test` 只在开发模式注册）、`config.py`（`QIO_SESSION_TOKEN` / `QIO_SESSION_TOKEN_FILE` / `QIO_DEV_INSECURE` / `QIO_ENABLE_TEST_EVENTS` 与 origin 白名单）、`frontend/src-tauri/src/main.rs`（随机空闲端口 + 后端生成会话令牌写入用户私有临时文件 + `qio_backend_info` 命令把 `{port, token}` 交给自己的 WebView）、`frontend/src/services/backend.ts`（地址与令牌解析，不再硬编码端口）、`api/bus.py` + `api/events.py`（线上 `id:` 行与 cursor 重放）
- **Tests：** `backend/tests/test_api_auth.py`、`test_sse_replay.py`、`test_credential_endpoint_api.py`
- **Known limitations：** 桌面壳的随机端口与令牌交接只在 Windows 上验证过；未在真实打包产物里跑过完整验收。开发脚本 `scripts/e2e_up.py` 默认仍以显式开发豁免（`QIO_DEV_INSECURE=1`）启动，并用 `--secure` 提供带令牌的口径；直接 `uvicorn agent.main:create_app --factory` 且不设任何环境变量时后端会自建令牌并拒绝所有未带令牌的请求（fail-closed，日志里只打印提示、不打印令牌）。
- **后续依赖：** 无下游。

### P9 — 第四阶段：视觉统一、交互收口与动效语言

- **Status：** partial
- **Implementation：** `docs/superpowers/specs/2026-09-15-visual-language-phase4-design.md`（规范 v3）；
  `frontend/src/styles/tokens.css`（三层动效 `--mo-1/2/3-*`、曲线集 `--ease-1/2/3-*`、位移 `--shift-*`、晶体玻璃 `--glass-*`）、
  `frontend/src/styles/base.css`（组件原语 + 过渡工具类 + 重写的 reduced-motion 语义）、
  `frontend/src/components/planet/PlanetOrb.vue`（入口小球 = 全屏 Planet 的压缩态）、
  `frontend/src/components/ui/QConfirm.vue`（取代原生确认框的确认层）
- **Implementation（本阶段改了什么）：** 把前三阶段已建立的能力收敛到同一套视觉/交互/动效语言：
  ① 动效分三层（高频简洁、中频柔顺、低频优雅），旧 `--dur-*` 全部降级为别名，页面组件不再散落硬编码值；
  ② Planet 入口小球改成 Planet 本体的压缩态，进入/退出是同一个对象的连续长大与收缩（不再是两个组件淡入淡出）；
  ③ 晶体玻璃材质只用于高层级空间浮层；
  ④ 卡片家族（Approval / Tool Creation / Subagent / 知识候选 / 工具调用）统一骨架与状态表达，状态原位推进；
  ⑤ 原生 `confirm` / `alert` 全部替换为按危险程度分档的确认层；
  ⑥ Knowledge / Entity / Topic Detail 改为「默认阅读、按需编辑」；
  ⑦ reduced-motion 从「全部瞬切」改为「保留淡入淡出、去掉位移与弹性」。
  2026-09-20 追加（用户实测反馈 + 一个死结）：
  ⑧ 星球「变大」的起点改用布局值计算（此前第二次打开会因为残留缩放把起始尺度算成 ≈1，
  表现为「星球直接出现、没有变大」）；⑨ 铺底与长大/收拢改成同一刻起止，对话页随星球渐隐渐显；
  ⑩ 审批在后端已不存在（404）时标记为失效并给一个「知道了」，不再留成永远点不掉的待办
  （其它失败仍保留待办并可重试）；⑪ `.planet-view` 整层背景改为透明（此前它自带不透明底色，
  一挂载就把对话页盖死，导致「对话页随星球渐隐」根本不可见 —— 只有铺底这一层负责遮盖）；
  ⑫ 收起不再做相机后撤（那是旧编排「收势 + 整层淡出」的遗留物，会让收起出现两段缩小：
  相机后撤一段、星球层缩回入口一段；现在收势只降密度、退浮层，体量收缩只有一段）；
  ⑬ **入口小球改为真实星球渲染**（用户要求）：关闭星球时把同一个场景缩到入口尺度常驻渲染
  （慢速自转、低帧率、展开后聚焦当前话题），不再是另画的 2D 压缩态；代价是 three.js 会在对话页
  懒加载并常驻（空闲挂载、正在对话时不抢主线程），WebGL 不可用时仍退回 2D 压缩态；
  ⑭ 打开延迟回到设计值：已预热时不再等数据刷新（点击 → 开始长大 1240ms → 391ms，
  点击 → 场景接管 1891ms → 915ms，设计值约 920ms），展开结束之后才聚焦当前话题；
  ⑮ 收起星球时右侧话题栏**一起退场**（收势阶段宽度收到 0、透明度 0，球态下不出现）——
  此前它只由 open/entered 驱动，会留在对话页上（用户实测反馈）；
  ⑯ 进入星球只剩**一段**动作：聚焦与长大同刻开始、同刻收束，并去掉人为的「激活等待」
  （此前先放大进入、再回正，用户实测反馈为两段动画）；⑰ 入口小球拖动松手后**本体跟着贴边**
  （此前只跟随内联 style 变化，而贴边是 CSS 过渡，本体停在原地再闪现），并按用户要求去掉
  「话题星球」文字标注（保留 title / aria-label）；⑱ 入口小球改为**按自身分辨率渲染**
  （球态画布 = 108px、scale≈1），不再把全屏渲染缩到 ~10% —— 那会让 1 像素宽的轮廓
  被打成断续的点。配套 `renderer.setSize(w, h, false)`：不让 three 写内联尺寸样式；
  ⑲ 球态的**环宽按球体屏幕直径成比例**（0.75%，保底 1.3px）—— 此前为了「看得见」用了固定 1.7px，
  相对粗度是全屏的 2.7 倍，小球看起来是一圈圈加粗的同心环而不是缩小的星球；
 ⑳ 收起与球态共用同一个密度常量 `BALL_REVEAL`（此前收起设 0.12、球态 0.55，
 交接那一帧点阵与网格会突然冒出来，用户实测「最后一帧和缩小状态完全不同」）。
  ㉑ **收缩尾段改在球自己的分辨率下渲染**：屏幕上球直径降到入口球的 1.35 倍时，星球层切到
  入口小球那套布局（画布 108px），并用当前 computed transform 反算球此刻的屏幕几何写回新坐标，
  之后每帧用**同一条令牌曲线**（新增 `frontend/src/utils/easing.ts` 求值）推进到终点 ——
  只有「画布 CSS 尺寸 = 屏幕尺寸」才是真 1:1（把绘图缓冲改小只会更糊）。换布局那一帧实测
  球心偏离应有轨迹 0.01px、半径单帧零回升（新增验收 S2.19）。
  ㉒ **环宽补偿改为「渲染前按当时几何现算」**：视图只交「环在屏幕上的目标宽度」
  （`setRingScreenWidth`），补偿倍数由 `usePlanetScene.applyRingWidthCompensation` 用渲染那一刻的
  `画布 rect 宽 / 布局宽` 现推。起因是用户实测「缩小途中闪一下」——尾段换布局时补偿倍数还是旧约定下的
  ≈2.0（对应整层缩放 0.13），而画布已变成 1:1，那一帧的环被画成 10px 宽的实心斑；
  改成现算后这类「几何与补偿错配」结构上不可能再出现（新增验收 S2.20，逐帧画面复核见
  `scripts/baseline/qa/planet-flash-probe.mjs`）。
  ㉓ **打开时以「当前在聊的话题」为中心**：定位规则由「起点没变就回到上次浏览的话题」改成
  「当前话题优先、浏览记忆只兜底」—— 用户要求「打开前它不在最中心，就在打开的过程中转到中心」。
  旋转本来就在长大过程中发生（实测锚点从偏离 162px 收到 1px，收束点在长大进度 96–99%），
  缺口只是被浏览记忆顶掉了。新增验收 S2.21（对着修复前规则确认会红）。
  ㉔ **收起时也转：转回「打开前的朝向」**：`markReturnOrientation()` 在展开聚焦**之前**记下球体四元数，
  `rotateBack(maxMs)` 在体量收缩段同刻发起，时长按夹角缩放并封顶在 `--mo-3-collapse`
  （超出会和球态自转抢同一个四元数）；曲线用对称的 `easeInOutCubic`，所以收起正好是展开的时间倒放。
  新增验收 S2.22（实测展开 159°、收起首帧仍 159°、末帧 4°），单测 +1。
  代价：大角度会在窗口内转得较快（159°/420ms ≈ 6.6 rad/s）。
- **Tests：** `frontend/src/styles/tokens.test.ts`、`frontend/src/styles/primitives.test.ts`（令牌与降级语义）、
  各组件既有 `__tests__` 的家族契约断言；真实界面验收脚本 `scripts/baseline/qa/phase4.mjs`
- **Known limitations：** 见本文末「尚未完成」里的第四阶段条目。摘要：工具创建卡只做了家族语言统一
  而没有真实端到端跑通；Planet 关键转场只在无头软件渲染（swiftshader）下逐帧验证过，
  真机 GPU 帧率与触控板手势未验证；动效手感未经真人评审；首次打开的 WebGL 冷启动耗时未优化。
  本阶段没有新增后端能力，也没有改变任何状态机。
- **后续依赖：** 后续新增界面必须复用本阶段的令牌与组件原语，不允许再引入新的视觉语法。

---

### P10 — 全工程稳定性、逻辑一致性与性能修复

- **Status：** completed
- **Implementation（turn 状态模型）：** `core/turn.py` 的 `TurnContext.status` 明确区分
  `accepted` / `queued` / `running` 与终态；**只有真正开始执行的 worker 才发 `TURN_START`**。
  终态单向：进入 `TERMINAL_STATUSES` 之后不再变化。被取消的 queued turn 变成 tombstone ——
  仍留在底层 `asyncio.Queue` 里，但 worker 取到它会直接跳过（不占用 active、不发任何 turn 事件），
  并且在取消那一刻就兑现它的等待者。`TurnManager.shutdown()` 按「停止受理 → 处理排队 turn
  → resolve pending futures → 取消 worker → 清理」的顺序收尾，`wait()` 超时也会摘掉 waiter。
- **Implementation（前端 turn 状态）：** `frontend/src/stores/session.ts` 拆出
  `activeTurnId`（**只由 `TURN_START` 或后端 `TURN_QUEUE` 快照里的 running 写入**）与
  `queuedTurnIds`；`send()` 不再把 POST 返回的 `turn_id` 当成 active。`stopActiveTurn()`
  已知 active 时精确取消它，还不知道 turn_id 时改为 `POST /api/turns/cancel`（后端只取消 active），
  绝不误伤排队消息。`TURN_END` 归属规则重新审查为：属于当前 active 的必须生效、属于从未开始的
  queued turn 只清理排队登记、active 未知时按去重表收敛、其余迟到事件忽略。
- **Implementation（用量与预算）：** 新增统一内部模型 `ModelUsage(input_tokens / output_tokens /
  total_tokens)`，供应商差异（OpenAI 的 `prompt_tokens/completion_tokens`、Anthropic 的
  `input_tokens/output_tokens`、文本兼容档）全部在 Adapter 层归一化，`AgentLoop` 不再读供应商字段。
  `USAGE` 事件在保留原有 `tokens`（= 输出 token）的同时给出 input / output / total。
  **默认不再使用整轮累计输出 token 作为强制停止条件**（`DEFAULT_TOKEN_BUDGET = 0` 表示不限），
  用户显式配置的预算仍然严格执行；迭代上限、重复失败护栏、provider 自身窗口与单次输出上限照旧。
- **Implementation（上下文预算）：** Adapter 新增 `system_prompt_text()` /
  `protocol_overhead_tokens()` / `tools_in_prompt`，`services/turn_orchestrator.py` 的
  `adapter_prompt_costs()` 把 system prompt（text 档含全部工具说明）、协议开销与工具定义
  真实计入 `TokenBudgetPlanner`，不再恒为 0，也不会把已经拼进 prompt 的工具说明重复计一遍。
- **Implementation（Adapter 生命周期）：** `AppContext` 缓存 adapter / 底层 HTTP client
  （键为凭据 id + 版本 + 端点 + 模型），同一凭据不再每个 Turn 重建连接池；Anthropic 能力探测
  按凭据缓存（默认 1 小时），凭据轮换立即失效；`create_app` 的 lifespan 在关闭时统一 `aclose()`。
- **Implementation（Subagent / EventBus）：** `tools/task_manager.py` 改为「提交即 queued、
  **拿到执行名额之后**才 running」，`await_result` 所有出口清理 waiter，任务记录有数量与 TTL 上限
  并在回收时释放 `full_content`。`api/bus.py` 的订阅者缓冲改为有界：同一 `(类型, turn_id)` 的
  `ASSISTANT` / `USAGE` 只保留最新（累计语义），溢出时先牺牲这类可合并事件，
  `TURN_START` / `TURN_END` / 审批 / 工具生命周期等关键事件优先保留。
- **Implementation（记忆检索与向量）：** `Selector` 新增 `upsert()` / `remove()`，`BM25Backend`
  实现真正的增量（df / doc_len / avgdl 增量维护，打分公式与排序键一个字未改），
  ONNX / 远程向量后端改为维护可复用矩阵（脏标记驱动重建），不再每次 `search()` 都 `np.stack` 全量重组；
  `services/memory_lifecycle.py` 封块后只 `upsert` 新写入的那一条，
  `turn_orchestrator.post_turn` 不再在增量更新之后又全量重建一次。
- **Implementation（数据与安全边界）：** 迁移 12 追加「同一 topic 最多一个开放 fragment」的部分唯一索引，
  迁移前先做**不删数据**的归一化（只归档关闭较早的开放片段），保证旧库仍能启动；
  `storage/db.py` 新增 `transaction()`，「关闭片段 + 写记忆索引」包成原子操作。
  `POST /api/approvals/{id}/respond` 现在真正接收并校验 `turn_id` / `session_id` / `request_digest`，
  前端应答时原样回传，正常审批体验不变。
- **Implementation（前端体验与性能）：** `MarkdownContent.vue` 把解析与显现动画解耦
  （解析按批次节拍，与动画帧率无关；`unified` processor 单例、hljs 结果按 `(lang, code)` 记忆化、
  落定时做一次完整解析）；会话历史改为渐进加载（首屏最近一页 + 向上滚动按游标加载更早，
  游标是 `created_at|id` 复合键，同一时刻写入的消息也不丢不重，插入旧历史时做滚动锚定保持阅读位置）。
- **Implementation（2026-09-20 收尾轮 · 极端时序）：** 四处「只有在极端时序下才会暴露」的状态缺陷：
  ① `api/bus.py` 的事件分类显式化（可合并 `ASSISTANT`/`USAGE`、关键状态转换、控制事件 `RESYNC`），
  缓冲里**全是关键事件**时不再静默丢最旧的一条：仍然保持有界，但会先发一条 `RESYNC`
  告知客户端「这一路事件流已经不完整」，客户端据此重新拉取权威快照；
  ② `TURN_QUEUE` 成为真正的权威快照：既能**恢复**缺失状态，也能**清除**本地已经过期的 active，
  同时 `core/turn.py` 给快照与 `TURN_START` / `TURN_END` 带上单调递增 `revision`，
  新的 `GET /api/turns/queue` 供 resync 使用 —— 旧的权威快照不能覆盖更新的状态；
  ③ `TurnManager.wait(timeout)` 不再删除 / 取消 turn 的 completion future
  （`asyncio.shield`）：某一次等待超时或调用方被取消，都只结束那一次等待，turn 照常跑完并正常 resolve；
  ④ `TaskManager.await_result` 超时返回**真实状态**（排队中报 `queued`，不报 `running`），
  并且兑现 waiter 时传「结局快照」而不是记录对象 —— 任务刚完成就被 retention 回收时，
  waiter 仍然拿得到结果（否则会拿到「done 但没有内容」的假结论）。
  另外 `Selector.select()` 把规则层时效项用到的时钟暴露成可选参数 `now`（`QueryContext` 本来就支持注入）：
  默认仍是当前时间，行为不变；测试 / 评测钉住它之后，「同一份索引、同一时刻」的两次排序才逐位可比
  —— 否则两次调用相隔几微秒，得分会在第 12 位小数上漂移（曾导致约 1/10 的偶发失败）。
- **Tests：** `backend/tests/test_turn_manager.py`、`test_turn_state_sequences.py`、`test_budget_defaults.py`、
  `test_model_usage.py`、`test_context_budget_accuracy.py`、`test_adapter_lifecycle.py`、
  `test_events_backpressure.py`、`test_selector_incremental.py`、`test_memory_selector_wiring.py`、
  `test_session_pagination.py`、`test_db_invariants.py`、`test_approval_binding.py`、`test_subagent.py`；
  前端 `stores/__tests__/turnSequences.test.ts`、`stores/__tests__/historyPagination.test.ts`、
  `components/__tests__/MarkdownStreaming.test.ts`、`stores/__tests__/approvals.test.ts`
- **Tests（2026-09-20 收尾轮追加）：** `backend/tests/test_turn_queue_snapshot.py`（快照 revision 单调、
  事件携带 revision、`GET /api/turns/queue` 与 SSE 快照同源）；`test_events_backpressure.py` 的反例用例
  （满缓冲全是关键事件时不得静默丢失、可合并事件淘汰不触发 resync、事件分类完备性守卫）；
  `test_turn_manager.py` 的 wait 语义用例（超时/取消不破坏 completion future、第二个 waiter 仍拿得到结果）；
  `test_subagent.py` 的 queued/running 超时语义与 retention 竞态用例；
  前端 `turnSequences.test.ts` 的 `TURN_QUEUE` 权威快照用例（清 stale、恢复 running/queued、旧快照不覆盖新状态、
  RESYNC 后重新同步）
- **Known limitations：** 取消仍不能物理中断已经发出的模型 HTTP 请求（返回值会被丢弃，turn 以
  `cancelled` 结束）；流式 Markdown 的单次解析仍是 O(全文长度)，只是频率不再等于动画帧数；
  增量检索的收益依赖后端支持 `supports_incremental`（不支持时自动回退全量重建，行为正确但没有加速）；
  本轮未启动真实浏览器做人工视觉复核（改动以「渲染仍是 source 前缀、逐状态 DOM 一致」为前提）。
- **后续依赖：** 后续新增 turn 状态、用量字段或索引维护路径都应以本节的契约为准。

---

### P11 — 稳定性第二轮收敛：恢复协议与生命周期

- **Status：** completed
- **Implementation（Event / RESYNC 恢复协议）：** `api/bus.py` 把 RESYNC 从「顺便发一条提示」
  升级成**硬边界**：关键事件过载时清空该订阅者失真区间的全部缓冲（含触发溢出的那一条），
  只标记需要 resync，此后只有新事件进入；客户端据此拉权威快照，不会被旧事件改回错误状态。
  `Last-Event-ID` 现在有明确三分法：**在 history 里** → 从下一条续传；**正好是最新一条** → 安静等新事件；
  **不存在 / 已被挤出 / 来自旧实例** → `RESYNC`（不得补发最近几条假装连续）。
  RESYNC 自身会写进 history，因此它是一个**合法游标** —— 客户端把它存成 Last-Event-ID 后重连
  不会被当成未知游标（否则每次重连都会再 RESYNC）。
- **Implementation（权威运行状态快照）：** 新增 `GET /api/runtime/state`，返回
  `instance_id` + `revision` + `turn_queue` + `approvals`（仍在等待的审批）+ `tasks`（仍在跑 / 排队的独立任务）。
  RESYNC 之后前端用它一次性恢复：turn 队列、断线期间错过的审批、仍在进行的独立任务，
  并把服务器已不存在的「运行中」工具卡收口为「连接中断，未收到执行结果」。
  审批的**过期**（timeout → `APPROVAL_RESULT(decision=timeout)`）与**取消**
  （等待的那一轮被 Stop → `APPROVAL_RESULT(decision=cancelled)`）都会通知客户端，
  界面不会一直留着已经不可能被批准的 Allow / Reject。
- **Implementation（版本基准与单一状态来源）：** 快照与 `TURN_START` / `TURN_END` 都带
  `instance_id`：同一实例内比较 `revision`，实例变化（后端重启，revision 从头计数）
  则重置基准并接受新实例状态。前端 Turn 队列只有**一个** apply 入口
  （`applyTurnQueue`）：先按 revision / instance 校验，再一次性落地 `turnQueue` +
  `activeTurnId` + `queuedTurnIds`；陈旧事件**整条**不生效（不再出现「active 用新数据、
  QueueChip 用旧数据」的半应用）。
- **Implementation（Event Ownership）：** 前端按 `turn_id` 判定归属：`subagent:task_x`
  的 `ASSISTANT` / `TOOL_START` / `TOOL_END` 不进入主对话，没有主 turn 在跑时带 turn_id 的
  助手输出同样不进；`USAGE` 按**事件自带的 turn_id** 归属，子任务用量不再记到主 turn 上。
- **Implementation（凭据写入原子性）：** `create` 改为「先校验 endpoint（与更新同一套规则：
  远端必须 HTTPS、明文 HTTP 仅限 loopback）→ 写密钥 → 事务内写元数据 + 审计」，
  任一失败都把刚写的密钥删掉；`update_secret` 先记住旧密钥、写新密钥成功后才提交
  version + 审计，失败则把旧密钥写回。不再出现「有元数据没密钥」或「version 涨了密钥没换」。
- **Implementation（应用生命周期）：** 维护调度改在 FastAPI lifespan startup 启动
  （构造阶段没有 running loop，旧代码在那里 `except RuntimeError: pass`，等于从未启动且不出声），
  新增 `stop()`；`_loop` 的异常隔离覆盖**整个循环体**（旧代码引用了早已不存在的
  `ctx._active_loop`，一次 AttributeError 就会永久杀死调度器，真正状态来源改为 `ctx.turns`）。
  `AppContext.aclose()` 现在是统一关机入口：停维护 → `TurnManager.shutdown()` →
  `TaskManager.shutdown()`（取消在跑任务、兑现所有 waiter、把记录收口成终态）→ 关 adapter/HTTP；
  真实入口 `main.create_app()` 再最后关 DB（避免后台任务还在写时连接先断）。
  `TurnManager` / `TaskManager` 关闭后 `submit()` 直接拒绝，不再接一个永远不会执行的任务。
- **Tests：** `backend/tests/test_event_recovery.py`（边界 / 过期游标 / 续传 / RESYNC 游标身份）、
  `test_runtime_state.py`（快照内容、待审批恢复、审批过期与取消通知）、
  `test_credential_atomicity.py`（create / update 失败注入、endpoint 校验）、
  `test_app_lifecycle.py`（真实 startup/shutdown、维护错误隔离、submit 拒绝、任务取消）；
  前端 `stores/__tests__/eventOwnership.test.ts`（归属 + RESYNC 恢复），
  `stores/__tests__/turnSequences.test.ts` 扩充（stale TURN_START / stale 快照不半应用 / 实例切换）。
- **Known limitations：** 事件流仍可能丢可合并的累计型事件（设计如此，状态可由快照恢复）；
  RESYNC 之后「失真区间」内被丢掉的关键事件不再回放，其状态由 `/api/runtime/state` 覆盖。
- **后续依赖：** 新增任何「丢一次事件就会让界面永久停在错误状态」的东西，
  都必须同时进 `/api/runtime/state`。

---

### P12 — 稳定性最终收尾：RESYNC 闭环、快照核对与组合更新

- **Status：** completed
- **Implementation（RESYNC 闭环）：** 两条 RESYNC 来源（缓冲过载 / 游标过期）现在共用同一套恢复身份：
  两者产生的 RESYNC 都写进 history，因此客户端把它存成 `Last-Event-ID` 之后重连能被识别，
  不会「每次重连都再 RESYNC」。前端新增 `resyncing` 状态与**同步缓冲**：
  收到 RESYNC 后进入 resyncing → 期间到达的实时事件先缓存 → 拉权威快照并完整应用 →
  再按到达顺序补放缓冲事件 → 回到 normal；同一时间只允许一个同步在跑
  （同步期间再来 RESYNC 只做标记，完成后补一次），既不丢事件也不会被旧 snapshot 覆盖。
  同步失败进入 `failed` 并如实保留错误，不假装已同步。
- **Implementation（快照核对语义）：** 恢复不再「只追加」，而是按状态性质分别处理：
  Turn 队列 **replace**（含 revision / instance 校验）、审批 **reconcile**
  （服务器没列出的 = 已不再 pending，本地移除）、独立任务 **reconcile**
  （不在活动集合里的 running/queued 卡片收口为「结果未收到」）、工具 **reconcile**
  （`/api/runtime/state.tools` 报告此刻真正在跑的工具，不在其中的「运行中」卡片收口）。
  `AgentLoop.active_tools()` 提供该列表，`TOOL_END` 丢失 + 重连后工具卡不会再永久转圈。
- **Implementation（审批路由统一）：** 实时 `APPROVAL_REQUIRED` 与 RESYNC 恢复出来的 pending approval
  走同一个入口 `handleApprovalRequired()`：`kind=continue` 一律进 ContinueBar，
  其余进审批队列（恢复时不抢焦点）。审批身份仍由服务端掌握：应答只需 `approval_id`。
- **Implementation（事件归属补全）：** `WARNING` / `ERROR` 也按 turn_id 判归属：
  `subagent:*` 的警告与错误不再升级成主会话的全局提示 / 错误（它们由任务卡表达）。
- **Implementation（凭据组合更新）：** 新增 `CredentialStore.reconfigure()`，把
  secret + endpoint + 其他元数据当成**一个**操作：校验全部输入（endpoint 变化必须重新输入 secret
  并显式确认）→ 先写密钥 → 事务内写元数据 + 推进 version + 审计；任一步失败都回滚两边。
  如果连密钥回滚都失败，记 **CRITICAL** 并抛 `CredentialRollbackError`（带两个原因），
  绝不让调用方以为「只是没更新」。API 的 `PATCH /api/credentials/{id}` 改为只调用它，
  不再把 `update_secret` 与 `update_metadata` 串起来。
- **Implementation（Maintenance 避让）：** 是否避让主任务改为判断「有没有 active Turn」
  （`ctx.turns.active is not None`），而不是 active AgentLoop —— Turn 处于上下文准备 /
  结果保存 / 收尾阶段时 loop 可能已经不在，但主任务并没有结束。
- **Tests：** `backend/tests/test_event_recovery.py`（过期游标产生的 RESYNC 也是合法游标）、
  `test_active_tools.py`（只有真正在跑的工具被报告）、`test_credential_atomicity.py`
  （组合更新的成功 / 失败回滚 / 回滚失败必须大声报错）、`test_app_lifecycle.py`
  （维护避让任何 active Turn）、`test_runtime_state.py`（应答只需 approval_id）；
  前端 `stores/__tests__/resyncProtocol.test.ts`（同步缓冲、单飞、快照核对、实例切换、失败状态）、
  `approvalRouting.test.ts`（continue 审批两条路径一致）、`eventOwnership.test.ts`（WARNING/ERROR 归属）。
- **Known limitations：** 同步缓冲只在内存里：如果同步期间进程被杀，缓冲事件随之消失
  （下一次连接仍会走 RESYNC + 快照，状态最终一致）。失真区间内被丢弃的关键事件依然回放不了，
  由快照覆盖。
- **后续依赖：** 无下游；这是这一系列稳定性修复的收尾。

---

### P13 — 工具终态可恢复：过程可丢，结论不可丢

- **Status：** completed
- **Implementation（权威状态）：** 新增 `agent/core/tool_state.py`：进程级、纯内存、有界的
  `ToolExecutionState`（active + recent terminal tool executions）。每条只留
  `tool_call_id` / `tool_name` / `turn_id` / `status`（running / success / failed / cancelled）/
  起止时间 / 一行错误摘要 —— 不保存完整工具输出、不落盘、不建事件日志。
  身份是 `(turn_id, tool_call_id)`：同一次调用原位更新，跨 Turn 不会互相污染，
  同一个工具名在一轮里连续调用多次也能区分。
- **Implementation（写入顺序）：** `AgentLoop` 在 `tool/start` / `tool/end` 管线里**先**写权威状态、
  **再**发 `TOOL_START` / `TOOL_END`。所以「`TOOL_END` 丢在失真区间里」不会让服务器已经知道的
  终态一起消失。`TOOL_END` 载荷新增 `status`（success / failed / cancelled）：
  工具注册表的取消路径显式标记 cancelled，前端不必从 `ok=false` 反推「取消还是失败」。
- **Implementation（恢复入口）：** `GET /api/runtime/state` 的 `tools` 字段由这份状态生成
  （活工具 + 最近结束的工具），只包含主 Turn：`subagent:*` 等内部循环的调用既不返回、
  也不进 UI —— 产品上独立任务的最小显示单位仍然是「独立任务」。
  属于 active Turn 的 running 记录如实报 running；所属 Turn 已经不在的记录报 unknown
  （服务器不能替它保证「还在跑」）。
- **Implementation（前端核对）：** 工具卡数据模型区分 running / success / failed / cancelled / unknown
  （`session.reconcileTools()`）。优先级：服务器给出的终态 > 本地过期的「运行中」；
  快照之后到达的实时事件 > 快照（同步缓冲按到达顺序补放）；服务器说还在跑时不把已有终态
  降级回运行中；别的 Turn 的 `tool_call_id` 不会被当成这次调用的事实。`TURN_END` 时残余的
  「运行中」卡片也会收口 —— 但下一次快照只要有真实终态，就会改写回 success / failed / cancelled。
- **接受的限制 1：** RESYNC 期间到达的新事件只临时存在内存里。恢复过程中应用被强制结束，
  下次启动重新获取完整快照（不尝试恢复上一次未完成的 resync buffer），不持久化临时缓冲。
- **接受的限制 2：** Subagent 只恢复 Task 级状态（queued / running / done / failed），
  不恢复、也不展示 subagent 内部单个 Tool 的执行明细。
- **保证：** 主 Turn 中只要服务器仍然知道工具最终状态，即使实时 `TOOL_END` 丢失，
  RESYNC 后仍能恢复成 success / failed / cancelled；只有服务器自己也无法确认
  （记录已按 retention 回收、或进程重启过）时，界面才显示「结果未收到」。
- **Tests：** `backend/tests/test_tool_state.py`（权威状态语义与 retention：active Turn 的终态不被提前清理）、
  `backend/tests/test_tool_recovery.py`（`TOOL_END` 丢失后 snapshot 仍能恢复终态、取消不等于失败、
  stale running 只能报 unknown、子任务内部工具不进主 snapshot）、
  `frontend/src/stores/__tests__/toolRecovery.test.ts`（快照核对、同名多次调用、跨 Turn 不串、
  快照与缓冲事件的顺序、`TURN_END` 收敛）、
  `frontend/src/components/__tests__/MessageItem.test.ts`（「已取消」与「结果未收到」有自己的文案）。
- **Known limitations：** 这份状态是**进程内**的：后端重启后它为空，此时界面显示「结果未收到」
  是诚实答案（不伪造终态）；terminal 记录按 TTL 与最大条数回收，回收之后同样回落为「结果未收到」。
  本轮不建设工具执行历史数据库，也不扩大 Subagent 的展示范围。
- **后续依赖：** 无下游。

---

### P14 — Execution Narrative（执行叙事层，2026-09-22）

- **Status：** completed
- **Implementation（表达与事实分离）：** 新增 `agent/core/narrative.py`：`Narrative(kind, text, explanation, silent)`
  与白名单解析 `parse_narrative`（只认 `kind` / `text` / `explanation`，其它键丢弃、长度截断、
  过 `trace/redact.redact_text`）。模型在工具调用参数里携带保留字段 `_qio`；
  `NativeAdapter` / `TextAdapter` 解析时用 `split_narrative_arguments` **剥离**它，
  所以工具参数、风险判断、沙箱判定、审批摘要都看不到模型文案。
  `ToolRegistry.specs()` 在每份 parameters 副本里声明可选的 `_qio` 属性（不加入 required）。
- **Implementation（一批一条 + 先说明再执行）：** `AgentLoop` 新增 `narrative_sink` / `narrative_settler`；
  每个工具批次在取消检查之后、真正执行之前取**第一条有效叙事**，注入的 sink 先落库再广播
  `NARRATIVE`，批次结束后把真实终态与耗时补写进同一行的 `raw.calls`（系统生成，模型改不了）。
  子 agent / 维护循环不注入 sink，因此不会往主对话写过程说明。
- **Implementation（审批 explanation）：** `ToolRegistry.execute` 用 ContextVar 挂上"当前调用的叙事"
  （并行调用各 task 隔离）；`ApprovalService.request` 只在载荷自己没有 explanation 时补上模型文案。
  `description` / `access` / `capabilities` / `scope` / `detail` / 事件载荷一个字段都不动，
  审批的单次使用、过期、摘要校验、拒绝/超时/取消语义完全不变。
- **Implementation（落库与恢复）：** 叙事写入 `messages`（`role='assistant'`、`content_type='narrative'`、
  `raw={narrative, calls}`），不新增迁移；`GET /api/runtime/state.narratives` 与历史分页
  （`raw` 一起返回）构成恢复路径，前端按 `narrative_id` 三层去重；
  `FragmentManager.content_tokens` 排除叙事行，展示文本不会让片段提前封存。
- **Implementation（前端）：** 新增 `NarrativeStage.vue`：一行叙事 = 一个**默认收起**的抽屉头，
  收纳它之后、下一行叙事之前的调用卡；折叠头由系统状态显示
  `N 次调用 · 耗时` / `N 运行中` / `N 失败` / `N 已取消`（异常不会因为收纳而消失）；
  历史里抽屉内容来自 `raw.calls` 的系统生成调用摘要。机械提示「正在使用工具」已移除，
  `MessageStream` 不再显示它；`ApprovalModal.vue` 把模型说明单独成段并标注「QIO 的说明」。
- **Tests：** `backend/tests/test_execution_narrative.py`（silent / 执行前顺序 / 批量合并 /
  审批 explanation 与事实不变 / 并行不串味 / `_qio` 不进参数 / 批次结算 / runtime 与历史恢复 /
  容量排除 / 完整一轮端到端）、`frontend/src/stores/__tests__/executionNarrative.test.ts`、
  `frontend/src/components/__tests__/ExecutionNarrativeDrawer.test.ts`、
  frontend/src/components/__tests__/ApprovalModal.test.ts（2026-10-26 已随旧审批展示层删除）。
- **Known limitations：**
  - 工具卡本身**不写入 `messages` 表**（沿用既有边界）：历史里抽屉打开看到的是
    系统生成的调用摘要。2026-09-24 起，工具调用另存于 `tool_records` 表，
    历史与实时都能按记录 id 展开看完整参数与输出（见本文末「工具调用历史」一节）。
  - 叙事是展示记录，不参与记忆整理判断；它会计入片段的摘要输入但不计入容量。
  - 模型可以不写叙事（silent 是默认）；此时不会出现任何过程文案，只保留工具卡。
  - 叙事在取消/失败的轮次里会留在历史中（如实反映"说明过、没做完"）。
- **后续依赖：** 无下游。设计见 `docs/superpowers/specs/2026-09-22-execution-narrative-design.md`。

---

### P15 — 应用内联网更新（Updater，2026-09-22）

- **Status：** completed
- **Implementation（真机闭环，2026-09-22）：** 已发布 v0.1.3 → v0.1.6 六个版本，更新源为
  GitHub Releases（`releases/latest/download/latest.json`）。**应用内一键更新已在真机跑通**：
  0.1.5 内点「检查更新」→ 发现 0.1.6 → 下载 → 签名校验通过 → 安装 → 重启后版本为 0.1.6。
  途中修掉三个真实故障：更新插件只读 `TAURI_SIGNING_PRIVATE_KEY`（导致"有安装包、无签名"）、
  后端孤儿进程锁住 `qio-backend.exe` 让安装中止（改用 Windows Job Object 根治）、
  以及残留的失效代理地址让请求一直等（改为读系统代理 + 采用前做连通测试）。
- **Implementation（运行时）：** Tauri 官方 `tauri-plugin-updater`（检查 / 下载 / **签名校验** / 安装）
  + `tauri-plugin-process`（装完重启）；`main.rs` 注册两个插件，`capabilities/default.json` 增加
  `updater:default` 与 `process:allow-restart`，`tauri.conf.json` 开启 `bundle.createUpdaterArtifacts`
  并配置 `plugins.updater`（endpoints = GitHub Releases 的 latest.json，pubkey = 用户生成的 minisign 公钥）。
  更新请求在 Rust 侧发出，**前端 CSP 未放宽**。
- **Implementation（前端）：** `services/updater.ts`（唯一插件入口 + 版本比较 + 错误分类）、
  `stores/updater.ts`（状态机：idle/checking/up-to-date/available/downloading/ready/failed）、
  `components/UpdateCard.vue`（设置 → 数据与维护）；启动后静默检查一次 + 每 24 小时一次，
  设置里可关；**只检查不自动下载**，下载与安装必须用户点击；浏览器开发预览不触发检查。
- **Implementation（发布侧）：** `scripts/build_installer.ps1` 增加第 4 步：校验签名环境变量 →
  产出 `.sig` → 生成 `latest.json` → 复制 exe/.sig/latest.json 到 `dist/` → 汇总 `SHA256SUMS.txt`；
  新增 `scripts/publish_release.ps1`（gh release create + 上传三个资产）。
- **Tests：** `frontend/src/stores/__tests__/updater.test.ts`（版本比较、错误分类、
  状态机正反两条路径、检查失败不得显示成"已是最新"、只有用户点击才 relaunch）、
  `frontend/src/components/__tests__/UpdateCard.test.ts`（各状态按钮文案与可用性）。
- **Known limitations：**
  - 0.1.2 及更早版本没有更新器代码，升级到 0.1.3 需要**手动安装一次**；0.1.5 起已在真机验证；
  - 代理自动适配覆盖"系统代理模式"与 TUN/全局模式的 VPN；**PAC 自动配置脚本、浏览器插件式
    VPN、需要用户名密码的代理不覆盖**（任何非浏览器程序都不行）；
  - 更新包未做 Authenticode 代码签名，安装时 Windows 可能仍提示「已保护你的电脑」；
  - 私钥与口令是信任根：泄露即等于所有已安装实例可被投毒；丢失即无法再发签名更新；
  - 不做差分更新、不做 beta/stable 多通道、不做回滚。
- **后续依赖：** 发布 v0.1.3 时补齐端到端验证。
  设计与实现计划见 `docs/superpowers/specs/2026-09-22-updater-design.md`、
  `docs/superpowers/plans/2026-09-22-updater.md`。

### P16 — 统一执行过程 / 真实流式回答 / 文件附件 / 耗时口径（2026-10-06）

- **Status：** partial
- **Implementation（统一过程区域）：** 一轮 = 一个过程区域（`frontend/src/components/TurnProcess.vue`），
  把过去彼此独立的入口（阶段行、工具卡、`◈ 过程` 中间话气泡、全局运行中提示、耗时面板）收拢成一处；
  状态行只由系统事实（TURN_*/TOOL_*/APPROVAL_*）驱动，完成/失败/停止自动收起，用户阅读历史时不抢滚动位置。
- **Implementation（阶段协议）：** 新增 `STAGE` 事件与 `stage_id`（`st_<turn8>_<n>`）。模型可在工具参数的
  `_qio` 信封里附 `stage:{op,name}`（start / next / update，白名单解析）；缺失或非法一律安全降级
  （没有阶段操作只更新当前说明，当前无阶段才开隐式阶段）——不因每次工具调用或新文本自动开阶段。
  阶段与说明随叙事行落库（`messages.raw.stage`）后再广播，工具按 `stage_id` 归属而非相邻位置；
  旧数据没有 `raw.stage` 时按旧版平铺渲染，不伪造阶段历史。
- **Implementation（真实流式）：** adapter 层新增 `supports_stream` / `stream()`（OpenAI 兼容与 Anthropic 走真 SSE，
  文本兼容档明确降级为一次性输出并提示「不支持实时生成」）；正文增量**一到达就以 `interim=true` 实时发布**
  （进过程区，边生成边显示），**唯一可靠的正式回答判据 = 该次调用结束且没有任何工具调用** → 同一 `delta_id`
  原样提升为正式回答（`streaming=false` 收尾快照），调用结束有工具调用则该段留在过程区；
  **没有时间守卫，也没有「正式回答→过程区」的移动**（旧的 300ms 守卫与移动例外已于 2026-10-06 审计废止）。
  按字符/时间合并发布累计快照，`(delta_id, seq)` 单调去重，`TURN_END.final_content` 只做校准；
  工具参数碎片只在 adapter 内组装，未完成的参数绝不执行。
  `TURN_END` 另带轮次结束事实 `reason_code / reason / stopped_by / actions`（系统事实、过 redact、
  只列确实可用的操作；旧记录为 `none` 不伪造）。
- **Implementation（耗时）：** `TURN_END` 增补 `duration_ms / queue_ms / started_at / ended_at`（来源 turn_traces 台账，
  缺失时退化为单调钟执行窗口）；折叠态直接显示「已完成 · 耗时」，不再无期限显示「读取中」；
  仅真正请求明细时才加载，未请求 / 加载中 / 成功无分项 / 失败 / 旧记录五种显示互不混淆，明细失败不抹掉已知总耗时。
- **Implementation（附件）：** 新增 `attachments` 表（追加迁移）与附件服务/接口/工具。不大于 100,000,000 字节
  （十进制 MB，取等号算副本）存独立副本并标注「已保存副本」，大于阈值只记录真实路径并标注「引用本地文件」
  （写明历史保留的是位置）；路径只来自 Tauri 原生选择/拖放的绝对路径或浏览器上传字节，不把 fakepath 当路径；
  内容不进上下文，由 `read_attachment` 按需分段读取；删除附件只清理 QIO 副本，绝不动用户原文件。
- **Tests：** `backend/tests/test_stage_protocol.py`、`test_streaming_deltas.py`、`test_turn_timing_facts.py`、
  `test_attachment_context.py`、`test_attachments_service.py` / `_tools` / `_api`、
  `frontend/src/components/__tests__/TurnProcess.test.ts`、`stores/__tests__/stageStreaming.test.ts` 等；
  独立验证方另有一组 `*_verify` 用例（`backend/tests/test_*_verify.py`、`frontend/src/**/*.verify.test.ts`）。
  附件边界的真机取证另有一个手工脚本 `scripts/verify_attachment_boundaries.py`（含**真实 100MB 复制**的精确等号边界，
  不进 CI，避免每次全量都写 100MB）。
- **Implementation（远端 CI 抓到的两个真缺陷，2026-10-06 已修）：**
  1) **后台复制不再在工作线程碰共享 sqlite 连接**：原先整个 `run_prepare` 被丢进 `asyncio.to_thread`，
     工作线程既读又写与全应用共享的连接（`check_same_thread=False`），在 CI 的 py3.12 / windows 上
     稳定复现 `sqlite3.InterfaceError` 与「刚 POST 成功、马上 GET 404」的幻影状态（本机 py3.11 全绿只是时序运气）。
     现在工作线程只跑纯文件 I/O（`copy_to_disk`），落库回到事件循环线程（`apply_outcome`），
     并有确定性并发用例（闸门卡住复制 + 复制期间高频 GET）守住「同一个连接只有一个线程碰」这条不变量。
  2) **兼容忽略 `stream: true` 的 OpenAI 兼容服务**：这类服务回整段 `application/json`，
     SDK 会给出 0 个 chunk 且不报错 —— 整轮会「没有工具调用」。现在先看响应 `Content-Type`：
     不是 `text/event-stream` 就直接用整段结果（零额外请求），并对裸客户端保留「零增量则只回退一次」的兜底；
     两种路径都如实告知「这条模型路径不支持实时生成」。
- **Known limitations：**
  - **真实厂商端点的 SSE 未验证**（规则禁止真实 Key / 联网）：只验证了协议形状与假厂商分片；
    「不支持流式」的 provider 路径明确降级，不宣称实时生成。
  - **工具轮的过程旁白在该次模型调用结束、阶段就位后才显示**（保证「同一阶段、不并列两个过程气泡」的取舍）；
    正式回答的实时性不受影响。
  - **原生文件选择与 Tauri 拖放只有编译级验证**（`cargo check --offline` 通过），没有在运行中的桌面进程里
    手工点开对话框/拖入文件；浏览器环境拿不到真实路径，只能上传字节（能力限制如实提示）。
  - **真实大于阈值的超大文件未做端到端复制耗时取证**（阈值分类与引用路径已由测试覆盖）。
  - 阶段与说明历史复用 `messages` 的叙事行，没有独立阶段表；阶段在当前实现里不跨 turn 延续。
- **后续依赖：** 真机桌面端手工验证原生选择/拖放与视觉检查（窄窗口、长回答、代码块、附件准备中）。
  设计与分工见 `docs/plans/2026-10-06-unified-process-attachments-streaming.md`，
  结构契约见 `docs/architecture.md` §12.1.2 ~ §12.1.5；
  独立验证方的取证记录见 `docs/verification-d-phase2.md`（含七组验收结论与截图 `docs/verification-shots/`）。

### P17 — 审计七项修复（统一过程区 / 流式输出 / 附件，2026-10-06）

- **Status：** partial
- **背景：** 对 P16 交付做独立审计，确认七项与产品规则冲突的问题并逐项修复；本轮**废止**了 P16 引入的两处错误规则：
  300ms 输出角色守卫，以及「正式回答→过程区」的文字移动例外。
- **Implementation（问题 2 · 输出角色）：** 删除 `GUARD_MS` 守卫与 answer→interim 移动。正文增量**一到达就以
  `interim=true` 实时发布**（过程区「生成中」说明，边生成边显示）；**唯一可靠判据 = 该次模型调用结束且没有
  任何工具调用** → 同一 `delta_id` 发 `{interim:false, streaming:false, content=累计全文}` 收尾快照，
  文字**原样提升**为正式回答；有工具调用则留在过程区，阶段就位后同 `delta_id` 补 `stage_id`/`call_ids`。
  工具阶段收尾零正文时补**一次** `tools=[]` 的调用专门产出正式回答（每轮最多一次，成本计入迭代/用量）。
  **已进入正式回答区的文字永不移动**；判据不含时间、文案猜测或 `kind` 变化。
- **Implementation（问题 1 · 内联审批）：** 抽出共用 `ApprovalFacts.vue` + `approvalFacts()`，弹窗与内联卡同一份事实；
  内联展示模型 explanation 与系统 description（分别保留）、真实操作事实（命令/路径/工具参数/授权对象/范围/风险）、
  验证与预算入口，长技术明细可折叠；「查看完整信息」打开原弹窗；内联接管期间**抑制自动弹窗**，
  **同一 `approval_id` 任一时刻只有一套有效按钮**；非当前轮/恢复路径仍走全局入口。
- **Implementation（问题 3 · 附件绑定）：** `attachment_ids` 的**存在性即语义**（出现，含 `[]`，表示这条消息就是这些附件；
  只有**缺字段**才走旧客户端兜底）。前端发送路径一律带该字段；待发附件与话题/草稿绑定并在重建/刷新后可见恢复；
  绑定前校验存在、话题归属与状态，已被别的轮绑定的不再重复绑定。
- **Implementation（问题 4 · 默认折叠）：** 运行中**不自动展开**；默认可见区 = 状态行 + 当前阶段名 + 最新一条说明 +
  **一行**工具摘要；旧阶段/旧说明/逐项工具记录默认收起；整轮历史抽屉与本阶段明细**两个独立**展开状态；
  完成/失败/停止自动收起，手动开合或正在阅读时不被抢占。
- **Implementation（问题 5 · 历史附件）：** 新增 `GET /api/attachments/{id}/content`（只读 QIO 管理的副本、需认证、
  路径由 id 反查、不接受任意路径、`nosniff`）；浏览器认证 `fetch` → Blob 查看/下载；桌面原生打开，
  **可执行/脚本类不自动执行**（改「在文件夹中显示」）；引用型在 missing/changed/failed 时提供**重新定位**入口。
- **Implementation（问题 6 · 后台化）：** 上传改 `request.stream()` **有界分块**（无 `Content-Length` 也强制上限），
  写临时文件 + sha256 在**工作线程**；重新定位/复制同理；事件循环只做落库与 O(1) 判断，
  工作线程**不触碰**共享 sqlite 连接；取消后不得提交为 ready。
- **Implementation（问题 7 · 结束事实）：** `TURN_END` 增补 `reason_code / reason / stopped_by / actions`
  （系统事实、过 redact、≤200 字、只列确实可用的操作）；`reason_code` 含 `provider_error`（仅厂商/传输路径失败）/
  `internal_error`（QIO 自身异常，reason 带真实类名）/ `credential_unavailable` / `tool_failed` /
  `budget|no_progress|guard_halt` / `user_stopped` / `interrupted` / `none`（旧记录不伪造）；
  可恢复的单次工具错误**不等于**整轮失败。前端按 `turn_id` 记进 `TurnFacts` 并展示原因与可用操作。
- **Tests：** 独立验证方（D）先建立 **44 条红 / 24 条绿守卫**的基线（按产品规则而非实现文档），修复后逐项转绿；
  实现方补充 `test_streaming_deltas.py` / `test_turn_timing_facts.py` / `test_attachment_explicit_binding.py` /
  `test_attachment_content_and_background.py` 与前端 `TurnProcessCollapse` / `ApprovalFacts` /
  `assistantPromotion` / `turnFactsReason` / `MessageItemAttachments` 等用例。
- **Known limitations：**
  - 实机交互（默认折叠 / 正式回答稳定性 / 内联审批点击 / 历史附件打开与重定位 / 失败入口）的浏览器级证据见阶段二报告；
    原生选择器、拖放与原生打开仍需在运行中的桌面端手工验证（`cargo check` 不能替代）。
  - 工作线程与事件循环共享 GIL 会带来 10–30ms 抖动（最大单次停顿实测 14–21ms，**不随文件大小增长**；
    累计值随负载波动）。已用对照实验归因，未做「每 N 块主动让出 GIL」的优化（吞吐代价不划算）。
  - 真实厂商端点的流式与兼容行为仍未验证（规则禁止真实 Key / 联网）。
  - 与实机取证同批满载跑时，附件后台化的「最大单次停顿」断言出现过一次越线（单独复跑 13–14ms 通过）；
    断言语义与阈值未改，建议该文件单独跑。
  - 历史分页的附件元数据只加在 `/api/session/context` 与 `/api/session/messages` 两条路由；
    `GET /api/fragments/{fragment_id}/messages` 未改动（前端无调用点）。
- **验证（2026-10-07）：** 独立验证方按产品规则先建立 **44 条红 / 24 条绿守卫**基线，修复后逐项转绿；
  Lead 亲自复跑冻结验收套件（后端 26 + 前端 26 全绿）与两条闸门（后端全量 0 失败、前端 1120 用例 + `vue-tsc` 全绿）。
  **实机交互 37/37 通过**：默认折叠（运行中旧说明与逐项工具卡不可见、展开可回看）、正式回答稳定性
  （provider 结束前已可见；1.2s 迟到工具增量不移字）、内联审批真机点击（允许 → `respond` 200 且本轮继续；
  拒绝 → 命令不执行）、失败原因 + 重试真的再跑一轮、历史附件刷新后仍可打开（`GET /content` 200）
  与引用失效后 `missing` + 重新定位。
  取证见 `docs/verification-audit-phase2.md` 与 `docs/verification-shots-phase2/`（含原始 FAIL 对照）。
- **后续依赖：** 真实厂商端点、桌面壳原生交互（当前用最小 Tauri 桩驱动同一段前端代码）、
  大于阈值的超大文件上传与窄窗口视觉检查仍需在对应环境补齐。

### P18 — 三项剩余问题修复（正式回答真流式 / 附件重试复用 / 上传失败收敛，2026-10-07）

- **Status：** partial
- **背景：** 独立复现三项遗留问题：正式回答仍要等调用结束才出现、带附件任务重试静默丢附件、
  分块上传写盘失败后接收端持续等待。本轮同时**取代** P17 的回答角色方案。
- **Implementation（问题一 · 正式回答真流式）：** 角色判据改为「这次调用带不带工具」：
  `tools=[...]` 是**工作调用**（正文进过程区，可多轮/并行调工具）；`tools=[]` 是**回答调用**，
  其正文**从第一个可发布增量起**以 `{interim:false, streaming:true}` **直接进入正式回答区**并持续显示。
  工作调用不再请求工具时进入回答阶段并发起一次回答调用（工作阶段无正文时亦然）；收尾再发同一
  `delta_id` 的累计快照做**校准**（收尾前若有未发布正文先发流式增量，**校准永不成为首次展示来源**）。
  已进正式回答区的文字**永不移动**。成本：每轮固定多一次纯回答调用（最简问答 1→2 次），如实记录。
  前端同步修两处：累计快照被打字机节流导致「文字到了 DOM 不亮」、断流/失败把已发布回答标回过程区。
  另修一处诚实性问题：厂商错误曾被 `openai` SDK 默认 `max_retries=2` 静默重试成「成功」，现按 0 重试并如实失败。
- **Implementation（问题二 · 附件重试复用）：** 新增 `retry_of_turn_id` 显式来源；`bind_for_turn`
  返回 `BindOutcome(bound, rejected)`。**受理前**校验每个 id（存在 / 同话题 / 状态允许 / 未绑定或绑定在
  来源轮），不满足即**结构化 409 且不入队**；`kind=copy` 为新一轮**新建记录并复用已保存副本**
  （`os.link` 硬链接优先、失败退化复制，**绝不重读用户原文件**），新增 `source_attachment_id`（追加迁移）；
  `kind=reference` 克隆**重查**当前可用性与变化；原轮归属与历史不变。响应带**实际绑定回执**，
  前端以回执为准；中断恢复重发同样按此处理。
- **Implementation（问题三 · 上传失败收敛）：** 一次上传 = 一个作业（有界队列 + 终态
  `running|done|failed|cancelled` + 原因 + 工作线程句柄）。接收端排队前看终态，等待空位时**同时**观察终态；
  **结束/中止哨兵不再依赖已无消费者的满队列**；取消能解除工作线程的阻塞读，工作线程失败能解除接收端等待。
  失败/取消后清理临时文件、`prepared` 转 `failed`（带人话原因），**绝不提交 ready**；覆盖建目录/打开/
  写入途中失败、权限、超限、客户端断开、用户取消、服务关闭。仍保持有界内存与字节上限、临时文件 +
  `os.replace` 提交、后台文件 I/O、数据库只在事件循环线程访问。
- **Tests：** 独立验证方按用户可见规则先建立 **26 红 / 12 绿守卫**基线（含三类受控写盘失败、
  分块数超过队列容量、真实重试入口、受控假 provider 暂停在首段正文后），修复后逐项转绿；
  实现方补充流式协议、克隆复用、回执准确性、上传收敛等用例；前端新增正式回答容器/过程容器 DOM 断言。
- **Known limitations：**
  - 真实厂商端点仍未验证（无外网、无真实 Key；全部假 provider / 真 SDK + 假端点）。
  - 原生桌面交互（原生选择器、拖放、原生打开）需在运行中的桌面端手工验证，`cargo check` 不能替代。
  - 工作线程与事件循环共享 GIL 的抖动、以及 `Settings` 的 `QIO_DATA_DIR` 覆盖显式 `data_dir`
    这一测试陷阱，均记录在案（后者本轮未改）。
  - 事件循环线程上的 `_check()/availability()` 会对引用型附件 `stat`，网络盘掉线时可能阻塞（既有风险，未在本轮处理）。
- **阶段二发现并修复的缺陷（实机复现）：** 失败/中断的一轮在**刷新（历史恢复）之后丢失过程区与「重试」入口**
  （实机证据 `processRegionsAfterRefresh:1 / retryAfterRefresh:false`）。两层修复：
  ① 前端把**后端真实给过**的结束事实按 `turn_id` 留一份**有界本机留痕**（后端一旦下发同一条 `turn_facts` 即以它为准）；
  ② **后端权威路径**：`TURN_END` 的 `reason_code/reason/stopped_by/actions` 落进 `turn_journal`
  （**追加迁移 28**，落库前过 redact），并随 `/api/session/context`、`/api/session/messages`（分页）与
  `/api/runtime/state`（RESYNC，只覆盖当前相关轮次）以 `turn_facts` 下发；旧记录无事实**不伪造**。
  实机复验：**20/20**，其中「清掉 localStorage + sessionStorage 再刷新」后过程区与重试入口仍在
  （证明来自后端权威路径而非仅前端留痕）。
- **阶段二修正的验收口径（不是放宽）：** 附件后台化的停顿断言改为**按环境地板标定**
  （同一次运行先测 1 KB 对照地板，硬指标 = `max_stall ≤ max(120ms, 3 × floor_ms)` + 探针推进），
  并带**鉴别力自证**用例：在事件循环上放 400 ms 同步阻塞时实测 402 ms 仍判越线。
  起因：CI run 37542503098 两个 job 各自越线（我们 407 ms / 既有无关测试 103 ms vs 100 ms）→ 2 vCPU runner 被抢占。
- **验证（2026-10-07）：** 独立验证方按用户可见规则先建立 **26 红 / 12 绿守卫**基线，修复后逐项转绿；
  Lead 亲自复跑：后端全量 **EXIT=0**、前端 **134 files / 1148 tests** + `vue-tsc` exit 0、`check_docs` 通过、
  独立验证方 7 个 R4/审计文件全绿；**实机 20/20**（截图与网络台账见阶段二报告）。
  Lead 另有**仓外独立探针**三条：正式回答在 provider 结束前已流式显示（3 条 150 ms 间隔增量、首条早于
  `stream_end`）、原文件删除后重试新轮**读出同一份内容**、写盘失败上传在有限时间内返回且无残留记录。
- **已知风险（记录不修）：** `AttachmentService.delete()` 置位取消后会把取消事件从 `_cancel` 中移除，
  任何**事后**用 `is_cancel_requested()` 轮询的消费者看不到「已取消」（当前由上传作业终态兜住）。
- **后续依赖：** 真实厂商端点与原生桌面交互仍需在对应环境验证；阶段二报告见独立验证方产出。

### P19 — 收尾三项：上传终态 / 回答重复 / 附件复制阻塞（2026-10-07）

- **Status：** partial
- **背景：** 上一轮交付后仍有三条遗漏路径：上传工作线程失败后接收端还在等下一块网络数据；
  工作阶段直接写出的完整答案会被再生成一次（过程区与回答区各一份）；重试克隆在硬链接失败后
  用 `shutil.copyfile` **在事件循环线程**同步复制。本轮逐条闭合，并**废止** P18 的「每轮固定多一次回答调用」。
- **Implementation（问题一 · 上传接收端与工作线程共同收敛）：** 接收循环把 `request.stream().__anext__()`
  包成任务，与**作业终态**做 `asyncio.wait(FIRST_COMPLETED)` 竞争 —— 等待网络时同样观察终态，
  **工作线程失败后不需要客户端再发送任何字节**即可进入失败收尾；每轮回收待决读取任务。
  原因归属互不覆盖（写盘失败 > 超限 > 客户端断开 > 用户取消 > 服务关闭）；只有**实际退出**的工作线程
  才被报告为已退出，卡在不可中断磁盘调用时保留真实状态、阻止迟到结果提交 `ready`、并在真正退出后补清理；
  失败/取消不留 `prepared`、不留临时文件、不留无人认领副本。
- **Implementation（问题二 · 内容角色协议）：** 角色由模型在正文开头的 `[[QIO:ANSWER]]` **显式声明**
  （声明不展示）；流式按最长可能前缀缓冲判定，匹配即为回答调用、其后正文**从第一个可发布增量起**
  实时进正式回答区；**未声明**正文先不展示（有界缓冲 256 KB），出现工具调用或超限才放行到过程区，
  调用结束无工具调用则**一次性**交付回答区（`role_evidence="undeclared_answer"`，**不重新生成、不搬动、
  过程区不留副本**）；声明后的迟到工具调用**不执行**并给可见警告；非法/冲突声明按未声明处理；
  整轮完全没有回答内容时才补**一次** `tools=[]` 兜底。协议由 `prompts.CONTENT_ROLE_PROTOCOL` 单常量注入三档。
  **成本：合规直接问答 1 次调用、工具轮 + 回答 2 次**（旧方案固定 2 / 3 次）。
- **Implementation（问题三 · 重试克隆文件 I/O 异步化）：** `AttachmentService.bind_for_turn` 改为 **async**
  三段式：① 事件循环线程校验 + 建 `prepared` 行 + 算目标路径；② `asyncio.to_thread` 只做文件 I/O
  （`os.link` 优先、失败退化为复制，含 stat/大小校验）；③ 回到事件循环线程定稿 `ready`/`failed` 并绑定。
  **不把含数据库操作的整个方法塞进线程**；取消/失败不留 `prepared`、不留半截文件、不留无人认领副本，
  迟到结果按「行是否仍在 `prepared`」校验，绝不提交 `ready`。turns/resend 两处路由由 Lead 接线。
- **Tests：** 独立验证方按用户可见规则先建立反例（问题一 4 红走**真实 ASGI 上传路由**、问题二 6 红、
  问题三 1 红含**线程身份**证据），修复后逐项转绿；问题三另有**100,000,000 字节等号边界**的真实文件验证
  （kind=copy、sha 一致、原轮与新轮都能读出、删除原文件后仍可读、无 `.part` 残留）。
- **验证（2026-10-07）：** 独立验证方按用户可见规则先建立反例（问题一 4 红走**真实 ASGI 上传路由**、
  问题二 6 红、问题三 1 红含线程身份），修复后 **36 条验收全绿**；**实机 15/15**（假 provider + uvicorn + vite +
  msedge/Playwright，10 张截图与网络台账见阶段二报告）；Lead 亲自用**仓外独立探针**复核三条关键：
  客户端暂停时上传请求自己返回且无残留、未声明完整答案进回答区且只 **1 次调用**、硬链接失败后复制线程
  为 `asyncio_1`（非事件循环线程）且闸门关闭期间循环仍在推进。闸门：后端全量 **EXIT=0**、
  前端 **134 files / 1148 tests** + `vue-tsc` 0、`check_docs` 通过。
- **CI 已知 flake（非本轮引入）：** `backend (windows-latest)` 上既有测试
  `test_interactive_during_heavy_work::test_health_probe_stays_responsive_while_slow_prediction_runs`
  （阈值 100ms）在共享 runner 上越线（观测到 1191ms / 121ms），而本轮**未改动**该文件、本机带 6 个抢核进程
  连跑 5 次全绿、上一轮 CI 亦曾通过 —— 判为负载敏感的既有 flake；**未改阈值、未 skip**。
  其余 8 个 job（py3.11 / py3.12 / frontend / install e2e / rust×2 / frozen worker / docs）全绿。
- **Known limitations：**
  - 真实厂商模型是否按协议发出 `[[QIO:ANSWER]]` 未验证（无外网/无真实 Key）；不遵守时走**降级路径**
    （一次性交付、不重复生成），但该次回答**不是流式**。
  - 真实 uvicorn 下**客户端半开连接**（TCP 不 FIN、只是不发数据）的收尾未覆盖（用的是 ASGI 层暂停）；
    「失败与最后一块/成功提交同时到达」的确定性竞态亦未构造。
  - 原生桌面交互与真实厂商端点仍未验证。
  - 事件循环线程上的 sqlite 为 autocommit + 默认 `synchronous=FULL`，每次写都 fsync；CI 上观测到过
    数百毫秒的单次停顿（**推断**归因），本轮未改（可考虑 `synchronous=NORMAL` 或独立写线程）。

### P20 — 四项剩余问题：附件就绪放行 / 声明解析 / 长正文退路 / 失败原因保留（2026-10-08）

- **Status：** partial
- **背景：** P19 之后仍有四条路径有洞：附件还在复制时模型已启动；合法声明与大正文落在同一分块时识别失败
  （声明泄漏进正文）；未声明长正文超过缓冲上限后被改判过程区、并在结束时**再生成一次**；查询失败附件状态时
  真实失败原因被通用 `missing` 文案覆盖。
- **Implementation（问题一 · 附件就绪后才放行执行）：** `TurnManager.reserve / activate / abandon` 三段式 ——
  预留分配 turn_id 并落台账但**不入队、不发 TURN_START**；turns 与 resend 两处路由改为
  `precheck → reserve → await 准备 →（失败 abandon + 结构化拒绝）/（成功 activate）`；放行**按预留顺序**（FIFO，
  有界等待兜底）；resend 的 claim **只在准备成功后消费**；准备期断开/取消/关闭 → `abandon` + 清理克隆，
  台账记 `cancelled`（不是 interrupted）。**准备期间模型 0 次调用、工具 0 次执行。**
- **Implementation（问题二 · 增量前缀解析）：** 删除「探测累计超过 32 字符即判未声明」的判据，
  探测缓冲**只保存控制前缀**（`len(声明)+2`），**超出部分一律是正文**；匹配成功后同一分块剩余正文立即进回答流。
  **分块边界无关**：同一字节序列任意拆分/合并，角色、最终正文、声明隐藏、控制流语义完全一致。
- **Implementation（问题三 · 未声明长正文的角色待定退路）：** 新增 `core/answer_buffer.py` ——
  有界内存（`UNDECLARED_MEMORY_LIMIT`，**UTF-8 字节**计量）+ 超出后工作线程追加写暂存文件；
  **缓冲上限只管理资源、不决定角色**（废止「超限即判为工作调用」）；硬上限/暂存失败**如实报告**
  （可见 WARNING `answer_truncated` + 截断事实），绝不无界增长、偷偷丢字或换角色；无工具调用 → 一次性交付
  回答区、**不重新生成**；有工具调用 → 按序完整放行过程区；取消/断流/关闭/重启清理暂存。
- **Implementation（问题四 · 失败原因保留）：** `failed` 与 `missing` 严格区分并**粘性**（GET/列表/历史/payload
  不得覆盖原始原因）；只有显式重试成功或 **sha256 可验证恢复**才转 `ready`；payload 新增 `actions` 与
  `recoverable_from_source`，前端按钮由其驱动 —— 浏览器字节上传给 **`reupload`**（并明说无法从原地址恢复），
  不再给必然失败的 `retry` 或不适用的 `relocate`。
- **Tests：** 独立验证方按用户可见规则先建立反例：问题二/三 **22 红**（声明泄漏 + 角色判错 + 重复生成）、
  问题一 **2 红**（闸门关闭时模型调用=1、准备期取消后仍执行）、问题四 **6 红**（failed 被改判 missing、
  原因被覆盖）；修复后逐项转绿（问题一 8/8 含多附件/排队失败/resend 恢复/准备期关闭；问题四 8/8 含
  5 种注入 × 首次/重复 GET/列表/重新打开一致、重试换新原因）。Lead 另用**仓外独立探针**复核分块无关性
  （7 种拆法）与 20 万字符未声明长正文：**37 项全过，每种拆法只有 1 次模型调用、过程区无副本、无声明泄漏**。
- **验证（2026-10-08）：** 后端全量 **2403 tests / 0 failures / 0 errors / 10 skipped**；前端 **136 files / 1164 tests**
  + `vue-tsc` exit 0；`check_docs` 通过（32 个里程碑）；R6 三个独立验收文件 **43 passed**；
  **实机 18/18**（假 provider + uvicorn + vite + msedge/Playwright，12 张截图 + `summary.json`）；
  CI 在最终 HEAD 上 **9/9 全绿（含 Linux py3.11 / py3.12）**。
- **CI 暴露的两处「装置在 Linux 上不成立」已修（值得记住）：**
  - 复制失败注入原先包 `builtins.open`/`io.open`，**Linux 的 `shutil.copyfile` 走 `_fastcopy_sendfile`**（`os.sendfile`
    直接搬字节），不经过该层 → 注入不命中、克隆实际成功，于是「拒绝」与「模型未启动」两条断言在 Linux 上假红。
    改为打在**真实调用点** `attachments.py:1390 os.link` / `:1393 shutil.copyfile`，跨平台确定。
  - 有界等待用例原先断言 `queued`/`running` 这类**瞬态**快照；Linux 上被兜底放行的空转轮在同一 tick 内跑完，
    采样必然错过（装置诊断实测：哨兵与 runner 起止同时间戳）。改为**不可逆事实**（TURN_START / TURN_END /
    台账未完成集）+ 下界检查，并用**变异测试**证明判据未被放宽（把兜底函数摘成 `return` 后用例照样红）。
- **Known limitations：**
  - 真实厂商模型是否按协议声明仍未验证（无外网/无 Key）；不遵守时走降级路径（一次性交付，不冒充流式）。
  - 有界等待的生产值（60s）未做真实等待验证（用例改为 0.2s 验证兜底逻辑）；进程**崩溃**在准备期的台账路径
    （`queued` → 重启后 `interrupt_stale`）无用例。
  - 暂存盘真实故障（盘满/权限）只做了 OSError 注入模拟；未做真实进程 RSS 采样（只验证有界性与清理）。
  - 有一条未定位观察：复制工作线程被闸门卡住时重活池线程长时间停在 `inject.py list_active_for_node`（SQLite 读），
    怀疑与共享连接写事务有关；同期 API/SSE 仍能推进（已断言），**未定位根因**。
  - 原生桌面交互、安装包 E2E 与真实厂商端点未跑。

### P21 — 取消确认 / 附件就绪 / 暂存故障交付 / 等待者收尾（2026-10-08）

- **Status：** partial
- **背景：** P20 之后仍有四条路径有洞：用户中止准备（客户端 abort）后端照样放行执行；首次登记的附件仍
  `prepared`（首次复制没完成）就被当可就绪放行；暂存读取失败只写日志、回答尾部静默丢失；`abandon` 先删
  结果 Future 再 resolve，等待者永不返回。
- **Implementation（问题一 · 可确认取消）：** `X-QIO-Prepare-Id` 标识 + 幂等端点
  `POST /api/turns/prepare/{prepare_id}/cancel`（`cancelled` / `already_started` / `unknown`）；
  **服务端监测到准备期间请求断连也按同一契约 `abandon`**；`activate` 前复核取消标记，迟到的复制成功不得
  重启本轮；前端「中止」以后端**确认**为准（确认前「正在中止…」；`already_started` 走既有停止流程并如实显示，
  不得宣称「没有发送」）。实测：**真 uvicorn + 真 TCP 断连**后释放磁盘闸门，模型调用 0 次、台账 `cancelled`、
  无孤儿克隆、取消端点 200。
- **Implementation（问题二 · 唯一就绪条件）：** copy 必须 `ready` **且副本实际存在、可打开、大小与登记一致**；
  `prepared` **一律不就绪** —— 要么**等待在飞首次准备**（`asyncio.Event` 唤醒，无轮询/固定延时/第二份复制，
  有界 `PREPARE_WAIT_MS=60_000`，实例属性 `prepare_wait_seconds` 供测试收紧），要么**结构化拒绝**
  `attachment_not_ready`（人话原因含重试指引）。覆盖首次登记 / 普通发送 / 旧客户端缺字段兜底 / 重试克隆 /
  resend / 排队；**显式空列表仍表示不带附件**；任一被拒则整个绑定一个字节都不写（不留半绑状态）。
- **Implementation（问题三 · 暂存故障准确交付）：** `AnswerBuffer.collect()` 返回结构化
  `BufferOutcome(text, complete, kind, reason, total_bytes)`，`kind ∈ {complete, limit, spill_create, spill_write,
  spill_read}` —— **读取故障绝不说成「超过上限」**；事实在 `collect()` 之后、清理之前进①**可见事件**
  （`limit` → `answer_truncated`；`spill_*` → `answer_incomplete` + `kind`，写明「已交付 N / 原共 M 字节」）
  ②**轮次警告**；**交付正文 = 已确认可交付的原样部分**（不把说明追加进正文）；`_maybe_fallback` 在结果
  不完整时**不再调用模型**。
- **Implementation（问题四 · 放弃预留先兑现等待者）：** `abandon` 先按既有约定兑现该轮所有等待者再清结果表；
  重复 `abandon`/`cancel`/`shutdown` 幂等；单个等待者超时/取消不影响共享 Future 与其它等待者；终态不被
  迟到 `abandon` 改写；放弃后后续就绪预留仍能推进。
- **Tests：** 独立验证方按用户可见规则先建立反例（问题一 1 红：真 TCP 断连后仍执行；问题二 3 红：prepared
  未就绪被调用；问题三 3 红：读取失败 0 警告 / 创建·写入被说成「超出上限」；问题四 5 红：等待者永不返回），
  修复后**四文件全部转绿**。Lead 亲自复跑四文件（含真 TCP 断连用例）确认。前端「中止确认」由 DOM 用例 +
  实机截图覆盖。
- **验证（2026-10-09）：** 后端全量 **2445 tests / 0 failures / 0 errors / 10 skipped**；前端
  **137 files / 1167 tests** + `vue-tsc` exit 0；`check_docs` 通过；`agent.eval.run` 与基线一致（verdict=skip）。
- **两个集成期发现（都非本轮验收项的错误）：**
  1. **准备期取消的监听任务死锁**（Lead 代修，`_prepare_with_cancel`）：断连监听挂在 Starlette
     `BaseHTTPMiddleware` 的 `wrapped_receive` 上，那个 receive 要等**本请求的响应完成**才返回
     `http.disconnect`，而响应要等路由返回 —— `finally` 里再 `await` 这个被取消的监听任务就是**自己等自己**
     （`test_turn_journal` 两条 resend 用例实测永不返回，全量卡住）。修法：bind 任务**取消并等待**（克隆清理
     挂在它身上）；监听任务**只取消不等待**（CancelledError 在其下一个 await 点送达）。
  2. **就用未就绪附件点发送：前端本就有一道可见闸门**（`attachmentBlockReason`，自附件链路 `9b716ca` 起即有）：
     prepared 附件按「发送」**不发请求**、屏幕显示「附件还在准备中…」。独立验证方的实机装置按
     `data-test` 读原因元素而该元素当时没有 `data-test` → 读成「无任何拒绝提示」，被误列为
     「待定性的观察」。补上 `data-test`（`attach-error`）+ 2 条 DOM 用例定性。
- **Known limitations：**（见 `docs/verification-r7-phase2.md` §4）：60s 生产等待值未真实等待、准备期**进程崩溃**
  台账路径无用例、真实磁盘故障仅 OSError 注入、多实例 tmp 清理竞争、重开对话后警告可见性未验证、
  实机带附件发送的 3 条证据因前端就绪闸门不可达（后端门由 HTTP/ASGI 层覆盖）、HTTP/2 与反代下断连行为、
  真实厂商/原生桌面/安装包未验。
- **Known limitations：**
  - 60s 准备等待生产值未做真实等待验证（用例收紧到亚秒）；准备期**进程崩溃**（非优雅关闭）台账路径无用例。
  - 真实磁盘故障（盘满/掉线）只做 OSError/FileNotFoundError 注入；多实例共用 `<data_dir>/tmp` 的暂存清理竞争未验证。
  - 「重新打开对话后不完整警告是否仍可见」未验证（取决于轮次事实持久化）；警告事实按契约**不进正文**。
  - 事件循环上 sqlite `synchronous=FULL` 的 fsync 停顿（推断，未改）；真实厂商/原生桌面/安装包未验。

### P22 — 带附件发送 CORS / 精确取消目标 / 兼容路径整体拒绝 / 暂存完整性（2026-10-09）

- **Status：** partial
- **背景：** P21 之后仍有四条：新取消头 `X-QIO-Prepare-Id` 没进 CORS 允许列表，带附件发送在**浏览器侧的预检
  被直接 400 Disallowed CORS headers** 拦截（r7 报告里「带附件发送 no-request」的真根因）；取消确认返回
  `already_started` 后前端停的是 `stopActiveTurn()`（**另一轮在跑时会被误伤**）；旧客户端兼容路径在等待期间
  **重新枚举**未绑定附件，`_bindable` 不满足即 `continue` —— 复制真实 failed 的附件被静默丢掉、该轮照常执行；
  `AnswerBuffer.collect()` 只把读取 OSError 当故障，暂存被**截短/清空/异常增长**或**多字节边界损坏**时
  仍标 `complete=true`（把成功保存量说成完整生成量）。
- **Implementation（问题一 · CORS）：** `X-QIO-Prepare-Id` 纳入 `allow_headers`（Lead 实施，`725fc75`）；
  保留既有认证、可信来源与 Host 检查；预检过后正式请求仍走认证。前端发送失败给可理解原因、保留草稿与附件。
- **Implementation（问题二 · 精确取消目标）：** `already_started` 回执里的 `turn_id` = 唯一取消目标 ——
  新增 `session.stopTurnById(turnId)`，`Composer` 用 `stopConfirmedTurn` 以它为准；身份缺失明确说明、
  **不静默退回** `stopActiveTurn()`（普通停止按钮语义不变，有用例钉住）；文案依事实（发出停止请求 ≠ 已停止、
  已执行不得称「没有发送」）；`preparingHandled` 让重复点击只问一次。
- **Implementation（问题三 · 兼容路径整体拒绝）：** 进入兼容发送**枚举一次并固定**集合快照，等待期间
  **不重新枚举**；快照内任一附件失败/取消/删除/超时/不可读/被占用 → **结构化拒绝整轮**（复用显式路径同一份
  判据与 code）；等待之后到落库之间**不再有 await**（`_recheck_planned` 按当下事实复核全部再一次落库）；
  快照为空 → 正常执行；历史失败记录不阻断纯文字发送。
- **Implementation（问题四 · 暂存字节事实核对）：** `collect()` 核对实际读回与成功写入的字节事实 ——
  截短/清空/异常增长/非法 UTF-8（含中文末字截断的多字节边界）**不再被当完整交付**、不用 replacement 字符
  掩盖；三个数字分开记（`generated_bytes / saved_bytes / delivered_bytes`，`total_bytes` 降为只读别名），
  警告统计名称真实；事实走可见事件 + 轮次警告，不重调模型。
- **Tests：** 独立验证方先在基线跑出四项红（预检 4 红 / 取消目标 5 红 / 兼容路径 8 红 / 暂存完整性 4 红，
  另有绿守卫确认边界），修复后**同一套断言**在集成分支四文件全绿；实机**跨来源真浏览器 12/12**（请求级观察：
  预检 200、POST 真到达 + prepare 头、回执绑定、`read_attachment`、回答完成；10 张截图 + summary.json）。
- **验证（2026-10-09）：** 后端全量 **2460 tests / 0 failures / 0 errors / 10 skipped**；前端
  **138 files / 1178 tests** + `vue-tsc` exit 0；`check_docs` 通过（34 里程碑）；`agent.eval.run` 与基线一致
  （verdict=skip）。`test_turn_journal.py` 在 Lead 集成树上单独复跑 EXIT=0（独立验证方本机曾停住，判定为其
  环境残留进程所致，非产品问题）。
- **过程记录：** 本轮发生一次 **git stash 跨 worktree 撞车**（`git stash` 是仓库级共享栈）—— A/B 工作区曾
  交叉污染，按共享 FS 复制 + 哈希核对恢复，**多 worktree 禁用 git stash** 已写入契约
  （`docs/plans/2026-10-09-send-cancel-integrity.md`）。
- **Known limitations：**（见 `docs/verification-r8-phase2.md`）真实硬件级损坏未验证（用真实文件操作模拟）、
  多实例共写同一 tmp 目录未验证、100MB 兼容路径与引用型 changed 时序未单独造例、Windows 原生窗口
  与安装包 E2E 未跑、真实厂商未验。

### P23 — 对话过程区/流式/附件审计集中修复（F01—F24，2026-10-09）

- **Status：** partial
- **背景：** 对 P16—P22 这条开发线做一次集中审计（F01—F24）：先在**基线**上跑反例（能失败），
  再逐项修复并复跑；同时把 C1—C8 冻结成本轮契约（终稿见 `docs/architecture.md` §12.1.7）。
  审计范围：附件读取与资源边界、流式结束语义、输出脱敏、未声明前缀中断、前端 turn 归属与回答校准、
  Markdown 列表渲染、耗时口径、附件前后端一致性。集成期另发现相邻路径 F25（TOOL_END 出口未脱敏），
  一并登记；逐项判定见文末「本轮判定」。
- **Implementation（附件读取与资源边界 · F01/F02/F21/F22/F23）：** `backend/src/agent/tools/attachment_tools.py`
  - **有界解压/解析**：zip 成员数与单成员字节、累计解压字节、共享字符串与整份解析总量都有预算；
    超资源给**明确、可理解的限制原因**，不伪装成完整读取成功。
  - **超长单行的有界分块与增量解码**：不再按整行分配、不再整文件解码；单行按片段分页。
  - **可中止的读取调度**：解析/解压在「不让事件循环被同步解析阻塞」的前提下推进，取消能中断本轮读取。
  - **编码嗅探的未完成尾字节处理**：被截断的 UTF-8 多字节序列不再被误判成另一种编码。
  - **分页事实与实际交付一致**：截断时 `next_offset` 指向真实继续位置；超长行引入**行内片段游标**
    （`next_fragment_offset` / `next_cursor`），旧游标兼容；元数据反映实际交付内容。
  - **测试：** `backend/tests/test_acc_a_f01_parse_bounds.py`、`test_acc_a_f02_line_bounds.py`、
    `test_acc_a_f21_scheduling.py`、`test_acc_a_f22_encoding.py`、`test_acc_a_f23_paging.py`。
- **Implementation（流式结束语义 · F06）：** `backend/src/agent/adapters/native.py`、`anthropic.py`、
  `core/loop.py`、`core/turn.py`、`storage/turn_journal.py`
  - `TURN_END.status` 终态集合新增 `incomplete`，**只**用于不完整 EOF（`reason_code == "incomplete_stream"`）：
    native 无 `finish_reason`、anthropic 无 `message_stop`、仅 usage/空分块、未结束的工具调用。
  - 厂商合法终止保持诚实区分而不升级为失败：`length_limit`、`content_filter` 的 status 仍是 `completed`，
    只用 `reason_code` 区分。
  - `incomplete` 时：**已确认正文保留**在 `final_content`，**未确认后缀不得出现**，`stopped_by=system`，
    带人话 reason，`actions` 含 `retry`；语义贯穿 adapter → loop → `TURN_END` → 前端 → **历史台账**
    （`turn_journal.record_facts` 落 `reason_code/reason/stopped_by/actions`），刷新后仍是「未完成 + 原因 + retry」。
    终态表现由 `core/turn.py` 的 `TERMINAL_STATUSES` 与 `_completion_status` 定稿；`turn_journal` 的终态
    集合同步接受 `incomplete`（**不折算成 `failed` / `completed`**），刷新 / 重连 / 分页都如实带回。
  - **测试：** `backend/tests/test_acc_b_stream_end.py`、`test_acc_f_06_incomplete_stream.py`、
    `backend/tests/test_acc_b2_incomplete_status.py`。
- **Implementation（输出脱敏跨分块 · F07）：** `backend/src/agent/trace/redact.py`、`core/loop.py`
  - 所有可观测输出（增量 / 累计快照 / 一次性正文 / 最终校准 / 注释 / 事件 / Trace / 历史 / 错误）
    统一走 `redact_text`；**先脱敏再发布**。
  - 跨分块敏感值用**有界未定稿尾部缓冲**（`undecided_tail_length`）：尾部不发布，直到确认没有完整对齐再放行；
    缓冲有界、随流推进释放，**不退化为「整段生成后显示」**。
  - **测试：** `backend/tests/test_acc_b_redact_stream.py`、`test_acc_f_07_stream_redaction.py`。
- **Implementation（F25（相邻路径新发现）· TOOL_END 出口未脱敏）：** `backend/src/agent/core/loop.py`
  - 相邻路径同范围：`TOOL_END` 的 `error` 与 `content_preview` 在**发布之前**过 `redact_text`，并与
    **同源落库**（`tool_state.finish`、工具事实、工具历史 `_record_tool_call`）同口径 —— 工具失败信息里的
    登记敏感值不得从事件出口或历史漏出（Lead 接手，提交 `59c3766`）。
  - 发现方式：独立验证者（acc-f2）在集成分支上用最小反例复现（合成敏感值经必失败工具的 `ToolResult.error`
    进入 `TOOL_END`，SSE 出口原样发布；对照路径均已脱敏）。
  - **测试（反例）：** backend/tests/test_acc_f_25_tool_end_redaction.py（独立验证者产出、尚未并入本分支，
    见「后续依赖」）。
- **Implementation（未声明前缀中断不丢字 · F19）：** `backend/src/agent/core/loop.py`
  - 在短角色前缀阶段被中断时，保留可交付文本并**如实标记未完成**；**完整控制声明不泄漏为正文**。
  - **测试：** `backend/tests/test_acc_b_prefix_interrupt.py`。
- **Implementation（前端 turn 归属与回答校准 · F05/F11/F12）：** `frontend/src/stores/events.ts`、
  `stores/session.ts`、`components/TurnProcess.vue`、`components/MessageStream.vue`、`services/api.ts`；
  后端 `backend/src/agent/core/turn.py`
  - 过程 / 工具 / 回答 / 结束事实按服务端 `turn_id` 归属；**排队 turn 不改变活动轮**。
  - `applyFinalAnswer` 按 **turn 身份**校准（不再用「全文是否相等」判断同一次回答）；系统核对注释走
    `TURN_END` 独立字段 `annotation`（兼容 `final_annotation`），在独立「系统事实」区域渲染；
    `final_content` 保持**纯正文**、正文只出现一次、不重启打字动画。
  - **排队轮取消**留下自己的结束事实：`cancelled` / `reason_code=user_stopped` / `stopped_by=user` /
    `actions` 含 `retry`，立刻发出，不影响活动轮；后端在 `core/turn.py` 里**补发恰好一条 `TURN_END`**
    （`end_actions=("retry",)`，先落台账与 `record_facts` 再清理队列标记），前端 `events.ts` 对「非 active
    但已知 turn」的 END 按该轮归属消费，`TurnProcess.vue` 如实显示「未完成 / 已取消 + 原因 + retry」。
  - **测试：** `frontend/src/stores/__tests__/acc_c_final_answer.test.ts`、`acc_c_queued_cancel.test.ts`、
    `acc_f_05_active_turn_ownership.test.ts`、`acc_f_11_final_answer_annotation.test.ts`、
    `frontend/src/components/__tests__/acc_c_turn_identity.test.ts`。
  - **测试（集成期补齐）：** `backend/tests/test_acc_b2_queued_cancel_end.py`、
    `frontend/src/stores/__tests__/acc_c2_incomplete_turn.test.ts`、
    `frontend/src/components/__tests__/acc_c2_incomplete_ui.test.ts`。
- **Implementation（Markdown 列表内块语义 · F13）：** `frontend/src/components/MarkdownContent.vue`
  - 按 AST **递归渲染列表项内的段落 / 代码块 / 子列表 / 引用 / 表格**，保留转义与链接安全策略。
  - **测试：** `frontend/src/components/__tests__/acc_f_13_markdown_lists.test.ts`、`acc_c_markdown.test.ts`。
- **Implementation（耗时口径 · F14）：** `frontend/src/components/TurnTimingPanel.vue`、`services/trace.ts`；
  后端权威字段 `backend/src/agent/core/turn.py`
  - 用户可见**总耗时 = 排队 + 执行**（可分列），**折叠态即可显示**；明细失败不覆盖已知总耗时、
    不永久显示「读取中」；缺字段的旧记录只显示**可证明**的时间。
  - **测试：** `frontend/src/components/__tests__/acc_c_timing.test.ts`。
- **Implementation（附件前端 · F03/F04/F08/F09/F10/F24 前端）：** `frontend/src/services/attachments.ts`、
  `components/Composer.vue`、`components/MessageItem.vue`、`utils/externalLink.ts`
  - `noopener` 打开判定**不再用 `window.open` 返回值断言失败**（保留 opener 隔离、blob URL 生命周期与去重下载）。
  - 历史重传结果**归属到发起话题的待发送列表**并可恢复；异步结果按 **topic / 操作版本**落地；
    替换只在**指定的新附件 ready 且加入列表**后才提交；暂时恢复失败**不清持久化身份**。
  - **测试：** `frontend/src/services/__tests__/acc_d_attachment_open.test.ts`、
    `acc_d_restore_pending.test.ts`、`acc_d_wait_settled.test.ts`、`acc_f_03_external_open.test.ts`、
    `frontend/src/components/__tests__/acc_d_composer_reupload_replace.test.ts`、
    `acc_d_composer_topic_scope.test.ts`、`acc_d_reupload_result.test.ts`、
    `frontend/src/utils/__tests__/acc_d_external_link.test.ts`。
- **Implementation（附件后端一致性 · F15/F16/F17/F18/F20/F24 后端）：**
  `backend/src/agent/services/attachments.py`、`api/server.py`
  - 绑定跨 `await` 后按**记录身份 / 归属 / 可读性 / 操作版本**条件提交。
  - 多附件重试的中间克隆在后续失败或取消时**完整补偿回滚**（删行 + 删本次副本 + 清 `preparing`，
    原历史副本与归属不动）；取消后仍在执行的线程不得写回已撤销结果（落库前校验代际）。
  - 副本就绪含**真实打开读取探针**（`stat` 正常但打不开的副本不再放行）。
  - 引用消失的大文件重试按**当下事实**重校验；重定位用**代际版本**防止旧后台结果覆盖新结果。
  - 缺失副本可恢复时返回「**已受理且正在准备**」，而不是立即 `missing`。
  - **测试：** `backend/tests/test_acc_e_f15_binding_boundary.py`、`test_acc_e_f16_clone_rollback.py`、
    `test_acc_e_f17_readability.py`、`test_acc_e_f18_reference_retry.py`、
    `test_acc_e_f20_relocate_version.py`、`test_acc_e_f24_preparing_semantics.py`。
- **【回归修复】r8「兼容路径多附件任一失败整轮拒绝」在组合/负载下变红（2026-10-09）：**
  `backend/src/agent/services/attachments.py`（acc-e2）
  - **现象：** `backend/tests/test_r8_compat_path_reject_verify.py` 的多附件用例在组合/负载运行下返回
    200 accepted、`rejected=[]`；单文件运行通过（时序依赖）。
  - **根因：** 兼容兜底把「进入时就能带」（`_entry_carriable`）当成**快照过滤器** —— bad 附件的失败若在
    兼容路径枚举之前落库，它就被过滤掉，于是只剩 ok 附件被绑定并照常执行。
  - **修复：** 集合口径分两层 —— 进入时本话题**至少有一条能带的草稿** → 集合 = 进入时**全部未绑定草稿**
    （含进入即 failed / cancelled / missing / 不可读者）→ 任一条不合格**整轮拒绝**；**一条能带的都没有**
    → 纯文字发送（保住既有集合政策）。
  - **装置同步点：** 冻结用例只补**确定性同步点**（闸门把登记提交卡到兼容路径真正进入等待之后再放行），
    **未改任何断言**；修复实现与用例由 acc-e2 产出（集成分支并入状态以提交记录为准）。
  - **测试：** `backend/tests/test_r8_compat_path_reject_verify.py`（同一份断言，只加同步点）。
- **本轮判定（逐项）：**
  - **本轮修复：** F01—F04、F07—F10、F13—F24（集成分支复跑阶段一反例后转绿）。其中 F15—F24 是更早
    审计登记的对照项，按「先核实是否已有修复 + 反例通过」的口径处理：需要修复的已在本轮落地，
    能证明此前已有修复的只登记提交与验证，不重复实现。
  - **已有修复且反例通过：** F05（基线即绿，修复位于基线的祖先提交 `2b204d7`，反例保留为回归守卫）。
  - **本轮修复（集成期补齐后已落地）：** **F06**（`core/turn.py` 的 `incomplete` 终态出口、历史台账接受
    与前端如实消费）、**F12**（排队轮取消补发恰好一条 `TURN_END`，`end_actions=("retry",)`）、
    **F11 前端消费**（后端独立 `annotation` 字段 + 前端按 turn 身份在独立「系统事实」区域渲染）。
  - **本轮修复（相邻路径新发现）：** **F25**（`TOOL_END` 出口与同源落库脱敏，Lead 接手 `59c3766`；
    反例由独立验证者随阶段二并入）。
  - **本轮修复（回归）：** r8「兼容路径多附件任一失败整轮拒绝」的负载回归（acc-e2，见上）。
  - **未完成 / 未实测：** 见下「Known limitations」。
- **Tests：** 上列每个实现分组都带对应的反例/回归文件；独立验证方另有按产品规则先建红、再逐项转绿的
  基线反例（逐项登记处见 `docs/plans/2026-10-09-process-attachment-audit-consolidation.md` §一）。
- **验证（2026-10-09）：** 本轮文档侧验证 `python scripts/check_docs.py` 退出码 0；
  后端全量与前端 `npx vue-tsc --noEmit` / `npm test`、以及实机取证由独立验证方在阶段二复跑产出
  （具体数量随时会变，不写进本文件）。阶段一的基线反例与逐层证据见本轮验证报告。
- **Known limitations：**
  - **真实厂商端点未实测**：F06 的不完整结束语义只在假 provider 脚本与**真 HTTP + 真 SSE 的本地假厂商**上验证，
    OpenAI / Anthropic 实网行为未验。
  - **Windows / Tauri 原生文件入口未实测**：原生选择器、拖放与原生打开只有编译级验证，没有在运行中的
    桌面进程里手工点过；浏览器环境拿不到真实路径，只能上传字节（能力限制如实提示）。
  - **实机取证与跨层组合（阶段二）已完成**：10 张截图、11/11 通过（`docs/verification-shots-acc/summary.json`、
    `docs/verification-shots-acc/visual-report.md`），跨层组合用例 `backend/tests/test_acc_f_20_cross_layer_combination.py`
    覆盖「排队 + 取消 + 不完整结束 + 注释」的正常与异常两路。真实厂商端点、Windows/Tauri 原生入口仍未实测
    （见 `docs/verification-acc-phase2.md` §六）。
  - **兼容路径（旧客户端不传 `attachment_ids`）下，话题里未绑定的陈旧失败草稿会阻断带附件发送**，
    直到用户删除它或重试成功；**显式 `attachment_ids=[]` 的纯文字发送不受影响**（新客户端一律走显式路径）。
  - **读取预算与分页的旧游标兼容**只覆盖实现声明的旧形态；真实海量超长行文件的端到端分页未做耗时取证。
- **后续依赖：** 阶段一的基线反例见 `docs/verification-acc-phase1.md`；阶段二的逐项判定报告见
  `docs/verification-acc-phase2.md`（含 F25 与 r8 兼容路径回归）；F25 的最小反例见
  `backend/tests/test_acc_f_25_tool_end_redaction.py`。以上均已并入本分支，可被 `python scripts/check_docs.py` 校验。
- 契约终稿见 `docs/architecture.md` §12.1.7，逐项状态表见 `docs/plans/2026-10-09-process-attachment-audit-consolidation.md` §一。

### P24 — 七项残留边界问题收尾（R1—R7，2026-10-10）

- **Status：** partial
- **Implementation（本轮修复）：**
  - **R1 定位/复制的代际提交边界**：`backend/src/agent/services/attachments.py` —— 每个复制操作/代际使用
    **独有临时文件**（`temp_path_for`，后缀仍是 `.part` 以便重启清理认领），提交只在覆盖
    「再校验代际 + 核对本操作写出的文件身份（st_dev/st_ino）+ `os.replace`」的**短锁**内完成（锁不覆盖复制）；
    已提交但落库失败时按身份回滚**本操作**的副本（不误删新代际文件）；克隆与上传同纪律。
  - **R2 集合放行的整组复核**：同文件 `_reverify_committed`（不写库、不 `await`）在所有等待结束后按
    **当下事实**复核每条的存在/身份/归属（turn/topic/message）与副本可用性；任一条失效 → 整轮拒绝 +
    完整补偿（`bound` 清空，放行集合 == 实际绑定集合），新克隆不留半成功。
  - **R3 异步附件操作的归属与恢复写回**：`frontend/src/composables/attachmentOps.ts`（新增共享守卫）、
    `components/Composer.vue`、`services/attachments.ts` —— 每个操作在发起时捕获 (topic, 附件, opToken)；
    移除/发送确认使在飞操作失效；removed tombstone 随待发送持久化、跨卸载重挂载仍有效；
    `restorePendingAttachments` 改为**返回补丁**、由 Composer 在修订号一致时合并（只新增，绝不整表回写旧快照）。
  - **R4 选择期话题归属**：同批文件 —— `pickFile`/`addPaths`/`relocate` 在**点击入口**捕获发起 topic 与操作身份，
    返回后不再读 `currentTopicId`；取消只清理自己的选择意图。
  - **R5 系统注记的历史恢复**：`frontend/src/stores/session.ts` 的历史转换读 `raw.annotation`
    （字段优先；仅字段缺失才从正文拆旧内联；并存只渲染一份；异常 raw 不制造虚假提醒），渲染层独立
    「系统事实」区域、正文与注记各一次。后端形状 `{verified, annotation}`（`services/turn_orchestrator.py` 既有）。
  - **R6 回答身份校准**：`TURN_END.answer_id`（= 最终校准目标回答的 `delta_id`；`core/loop.py` 记录最近一条
    非 interim 正式回答、`core/turn.py` 透传，无回答段为 null）；前端 `applyFinalAnswer` 按身份**覆盖式**校准
    （缺省 = 不校准、显式空串 = 清空目标正文、无身份 = 校准该 turn 最后一条正式回答），删除文字相似度/前缀判定
    （`mergeFinalBody`）。
  - **R7 终态动作一致**：`core/turn.py` `user_stopped → ("retry",)`（`interrupted` 保留 `resend`）；
    `storage/turn_journal.py` 与 `api/server.py` 做**读时投影**：cancelled 轮的旧 `resend` 归一成 `retry`、
    真正可恢复（用户消息、未被 claim）的 interrupted 行补 `resend`（已领取/系统通知轮不补），**journal 行不改写**；
    前端 `components/TurnProcess.vue` 与 `stores/session.ts` 同规则归一；`retry` 用既有发送接口创建**新 turn**
    （同话题 + `retry_of_turn_id` + 附件按既有克隆规则），只有点击才执行、重复点击只产生一轮。
- **Tests：** `backend/tests/test_fb_a_r1_stale_relocate_overwrite.py`、`test_fb_a_r2_group_recheck.py`、
  `test_fb_d_r6_answer_id.py`、`test_fb_d_r7_actions_retry_resend.py`、`test_fb_d_r5_history_annotation.py`；
  前端 `fb_b_attachment_ops` / `fb_b_restore_patch` / `fb_b_attachment_ownership` /
  `fb_c_r5_annotation_history` / `fb_c_r5_annotation_dom` / `fb_c_r6_answer_identity` / `fb_c_r7_turn_actions`；
  以及 F01—F25 与 r8 兼容路径等既有回归。
- **Known limitations：**
  - 放行之后、模型真正读取之前，用户仍可能删除文件；R1/R2 不宣称消除这一段，读取侧按当下事实报错。
  - R2 被拒回执里的 id 是本轮克隆行 id（克隆失败时回落到用户请求的源行 id）；前端按「实际请求的那条」理解。
  - `copy_to_disk` 的 `on_commit` 是**测试用**确定性缝（默认 None）。
  - 兼容路径下话题里未绑定的陈旧失败草稿仍会阻断旧客户端带附件发送（P23 既有已知限制）。
- **后续依赖：** 逐项判定与实机取证见 `docs/verification-final-boundaries-phase2.md`；
  基线反例见 `docs/verification-final-boundaries-phase1.md`；契约见 `docs/architecture.md` §12.1.8。


<!-- 并入 main 时的编号处理（2026-10-11）：互动线与 main 各自用过 P16–P24，
     这里把互动线的各轮改号为 P25–P33，正文逐字保留（只改标题编号）。 -->
### 互动模式线各轮（历史记录；并入 main 时编号改为 P25–P33）

下面几节是**互动线各轮自己的历史记录**：编号已改为 P25–P33（互动线与 main 各自用过
P16–P24，直接并列会产生两套同号里程碑）。它们的验收结论属于**各自那一轮的候选**，
不能读成「本次并入 main 之后已经验收」——本次并入的验证范围与结论见文末的集成小节。

### P25 — 互动模式第一阶段（2026-10-06）

- **Status：** partial
- **起点与分支：** 起点提交 `ee6bbff`（= `origin/main`，已用 `git ls-remote` 核对）；开发分支
  `feat/interactive-foundation`。**没有合并 main、没有发安装包**。子智能体各自在
  `wt/int-a-board` / `wt/int-b-persist` / `wt/int-c-approval` 上开发，由主智能体按依赖顺序集成。
- **约定先冻结：** `docs/interactive-mode-contract.md`（数据含义、可见性边界、提交语义、审批状态机、
  HTTP 接口与跨模块函数签名、写作用域分工）。实现按冻结签名分头完成，公共文件只有主智能体写。
- **Implementation（数据）：** 迁移 26 追加互动模式相关的七张表（板面最新状态、状态快照、草稿、
  提交记录、未提交改动、意图、板面元信息）；只追加、未改历史迁移。
- **Implementation（可见范围，权限边界）：** `backend/src/agent/interactive/models.py` 的
  `selectable_cards` 是唯一实现：文字注释默认未勾选、勾选后才允许查看；材料默认在范围内；
  明确隐藏与已删除一律不在范围内；QIO 自己的结果卡片不进提交载荷。
  `backend/src/agent/interactive/submission.py` 用它投影 before / after：链接要求两端都可见、
  组只列可见成员且无可见成员的组整体不出现、服务端从**已保存状态**推导范围（客户端塞未勾选卡片 id 无效）。
- **Implementation（后端）：** `backend/src/agent/interactive/` 下 `board.py`（分组 / 顺序 / 拖动预演 /
  关系）、`board_store.py`（保存即落盘 + 快照 + 草稿，保存路径无模型调用）、`submission.py`（可见范围 /
  有效改动 / 基准 / 空与重复提交幂等）、`intents.py`（意图状态机、冲突、依赖等待与再次确认、
  材料变化标记需要更新、失败撤回与保留用户修改、重启恢复）；
  路由聚合在 `backend/src/agent/api/interactive.py`，`server.py` 只加一处注册。
- **Implementation（前端）：** 与对话模式并列的入口 `#/interactive`（对话页底部一行
  「互动模式：一起整理材料与关系」）、`frontend/src/views/InteractiveView.vue`（板面为主 + 可收起辅助区 +
  提交区常驻 + 保存前影响确认）、`frontend/src/stores/interactive.ts`（自动保存、撤销重做、
  **只有提交才调用 QIO**）、`frontend/src/services/interactive.ts`、
  `frontend/src/interactive/{types,board,submission,approval}.ts`、
  `frontend/src/components/interactive/`（板面、卡片、组框、连线、搜索、预览叠加层、提交面板、
  回复与审批面板）。
- **Tests（后端）：** `backend/tests/test_interactive_board.py`、`test_interactive_submission.py`、
  `test_interactive_intents.py`、`test_interactive_integration.py`（后一个是端到端验收：前后状态可见性、
  勾选自动取消、保存不调用 QIO、只改位置不产生意图、迁移预览、冲突与依赖、材料变化暂停、失败撤回、
  重启恢复）。
- **验证（真实界面）：** `scripts/interactive-verify/ui-scenarios.mjs` 驱动真实 Chrome 走
  加材料 → 写注释 → 只勾选一条 → 多选成组 → 真实鼠标拖动 → 提交 → 演示意图 → 板面虚线预览 → 批量条件，
  18 项断言全过，截图见该目录说明；字体路径只通过验收专用配置放宽（`frontend/vite.e2e.config.ts`）。
- **Known limitations：**
  - QIO 的真实理解、模型调用与外部执行**没有接入**：提交会落库并标记「第一阶段没有接入 QIO 模型调用，
    QIO 还没有真正读取或理解这些内容」，`delivery.delivered` 恒为 false；演示意图只走可控状态机，不代表 QIO 判断。
  - 未做：高亮、指向箭头、自由手写绘制、旋转、标签、筛选、前后遮挡顺序、直接对象包含、独立重点标记控件。
  - 待审批预览会**同时**画在板面上，多个意图的预览互相叠压，没有逐个显示 / 隐藏开关。
  - 有序组的成员不会自动重排成一行：序号由成员顺序表达，插入位置按落点计算。
  - 组框不能单独拖动或缩放（位置由成员推导）。
  - 撤回只覆盖「任务自己新增的内容」：用户改过的会被保留并列入未撤回部分，会影响其他工作的部分
    需要用户再决定（`revert.pendingDecision`）。
  - 「保存前的影响确认」只对**执行中**的任务弹：已暂停任务的材料依据即使已经失效也不再拦住保存
    （复核实测过：若拦住，板面会长期存不下去），它的影响只作为文字提示。
  - 暂停的任务有「继续（按当前材料）」入口，没有「放弃 / 拒绝」入口：暂停后只能在原地等待用户决定。
  - 反复点演示入口、且上一批还有未结束项时可能再生成一组演示意图（实测出现过冲突组里三项并存）。
  - 板面预览与正式卡片的坐标一致，但没有做缩放 / 平移。
- **后续：** 真实 QIO 理解与执行接入 `submission.submit_board` 的投递点（当前只落库并把未提交表达交给
  后续处理入口）。

---

### P26 — 互动板前端改版（2026-10-07）

- **Status：** partial
- **起点与分支：** 起点提交 `66b0e22`（= 远端 `feat/interactive-foundation` 顶端，已用 `git ls-remote` 核对）；
  开发分支 `feat/interactive-frontend-layout`。四个子智能体各自在 `wt/fe-a-layout` / `wt/fe-b-board` /
  `wt/fe-c-chat` / `wt/fe-d-approval` 上开发，主智能体按依赖顺序集成。
  **没有合并 `main`、没有动 `fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`、
  没有改数据库结构**（本轮不新增迁移）。
- **本轮范围：** 只改互动板前端，让真实应用符合已确认的布局与操作规则。契约新增
  `docs/interactive-mode-contract.md` §8（页面结构 / 板面操作 / 聊天与提交互相独立 / 前端所有权与冻结签名 /
  批次判定），实施计划见 `docs/superpowers/plans/2026-10-07-interactive-frontend-layout.md`。
- **Implementation（布局）：** 板面为主体、**取消常驻右侧栏**；顶部只留身份、回到对话、准确的保存状态、
  任务入口与明确标注的演示入口；常用操作收进**底部横向悬浮工具栏**（添加菜单统一五类材料、整理、选择模式、
  撤销/重做、板内搜索浮层），**右端是提交区**；右下角独立悬浮聊天；右上角批量列表入口（同一批待审批 ≥4 才出现、
  默认收起、不因数量变化自动展开）；影响确认改成**页面中央确认框**；卡片操作改成**选中后浮出的局部工具栏**。
- **Implementation（板面交互）：** 拖动空白处 = 平移（抓取滚动模型）；**空格 + 拖动 = 框选**；
  滚轮以指针附近为缩放中心；输入框/聊天/菜单/确认框里空格正常输入；坐标换算统一走
  `frontend/src/interactive/viewport.ts`（平移缩放后拖动、框选、连线、局部工具栏仍准确）；
  选中卡片显示**连接点**，拖到另一张卡片建链，无效位置或取消不建链；两张未分组卡片**明确重叠**时提示
  「松开后合并成组」，松手才成组，只认一张最明确的目标（重叠 ≥ 较小矩形 25% 且中心覆盖），
  靠近或边框相碰不成组；组名初始为系统默认名「组 N」，留空保留默认名且组仍成立。
  **（P18 更正：系统默认名改为「默认组名」，见契约 §9.1）**
- **Implementation（聊天与提交互相独立）：** 右下悬浮聊天复用 `stores/session.ts`（不新建事件订阅），
  Enter 发送 / Shift+Enter 换行 / 输入法选字中不发送 / 空白不发送；**只发文字，不带板面、未提交改动、
  注释或选择范围，也不调用板面提交接口**；失败保留输入并显示真实原因；收起展开不丢消息与草稿。
  提交区显示准确的保存/提交状态与本次有效改动、允许查看范围。
- **Implementation（批次判定，不改数据库）：** 前端 adapter 按「本次会话记录的创建批次 > 服务端 `submissionId`
  > `createdAt` 截断到秒 > 自成一批」分组，**不同批次不累加**。
  **（P18 更正：第三级「按创建秒」被证明会误合并两个不同批次，已删除；「宁可不出批量列表，也不把不同批次相加」
  这句与当时的实现不符，见契约 §8.5 的作废说明与 §9.2 的正确规则。）**
- **Implementation（保存与恢复）：** 每次有效操作后防抖自动保存（450ms）；卡片编辑草稿与聊天草稿分开保存、
  互不覆盖；未完成的拖动不保存；重新打开不自动提交、不自动批准、不自动继续任务。
- **本轮修掉的真实缺陷（都有实测证据）：**
  1. **对话页与互动聊天都会卡在「运行中」**：没有可用凭据时一轮十几毫秒就走完 `TURN_START`→`TURN_END`，
     而 `POST /api/turns` 的回执更晚才到，`send()` 在回执里无条件把 `turnRunning` 点亮，之后再也没有
     `TURN_END` 来熄灭它（停止按钮一直在、发送一直被禁用）。改法：`endTurn()` 记下有界集合，
     `send()` 只在「这一轮还没结束」时点亮；负向验证过（退回旧写法用例必失败）。
  2. **前后端成组判据不一致**：后端原来「碰到 1px 就把一叠卡片整堆合并」，与前端阈值相反。
     已把后端对齐到同一判据（25% + 中心覆盖 + 只认一张最明确目标），并补了边框相碰 / 低于阈值 /
     中心未覆盖 / 一叠只合一张四类用例。
  3. **批量面板遮挡底部工具栏**（独立复核发现）：绝对定位浮层高度由内容决定，
     `max-height: calc(100% - Npx)` 的百分比解析不成确定高度，约束等于没生效（实测 800×600 压住 143px、
     1024×768 压住 392px）。改成按「最下面那条板面工具栏的顶边 − 面板顶边」算出确定像素上限。
- **验证（前端）：** `npx vue-tsc --noEmit` 无输出；`npx vitest run` 全绿（112 个文件、1162 个用例）；
  新增用例覆盖视口换算、重叠阈值、聊天输入规则、批次判定与批量列表、影响确认框。
- **验证（后端回归）：** `cd backend; uv run --frozen pytest` → 2107 passed / 9 skipped（本轮只改了
  `interactive/board.py` 的成组判据与对应用例）。
- **验证（真实界面）：** `scripts/interactive-verify/fe-scenarios.mjs` 用真实 Chrome（CDP 真实鼠标/键盘/滚轮）
  走提示词第七节的场景：布局与浮层、平移 / 空格框选 / 滚轮缩放后坐标、重叠成组与改名、勾选与允许查看范围、
  聊天与提交两条独立请求路径、聊天草稿与失败保留、批量列表阈值、连接点拖线、草稿恢复不自动提交、窄窗口与主题。
  截图在验收输出目录（`%TEMP%\qio-visual-lead\shots`）。
- **独立复核：** `docs/interactive-frontend-verify.md`（子智能体 D 在集成提交上自己读源码 + 自己起应用复现，
  不采信其他子智能体的报告），含阻断/重要/次要问题、三档窄窗口实测数值与明确标注的「没能验证」项。
- **Known limitations：**
  - QIO 的真实理解与工具执行**仍未接入**：提交只落库并标记未接入，演示意图只走可控状态机，
    界面与文档都如实写明，不把演示结果说成真实 QIO 判断。
  - 批次判定曾经是**前端 adapter**（服务端没有批次字段，本轮不改数据库），而且**实现里写错过**：
    早期版本把「创建时间落在同一秒」当作归批依据，独立复现证明它会把**两个不同批次各两项**
    误并成一个四项批次（P18 已删除时间推断，改成只用可证明的来源；契约 §9.2）。
  - 窄窗口的工具栏与聊天面板布局在本轮末尾又修了两处（工具栏被提交区撑高、聊天面板被挤出视口）；
    本地探针环境在最后阶段出现渲染进程卡死（`about:blank` 与 WebGL 页正常、应用页卡死），
    这两处修复由 D 复验，结论见 `docs/interactive-frontend-verify.md`。
  - 800×600 下**同时**展开聊天面板与批量列表面板时，批量面板会视觉上盖住聊天面板上半部分
    （独立复核实测两面板重叠约 45080 px²；但两处入口与提交按钮都仍可点，聊天输入区未被覆盖）。
    没有做「同时打开时自动错位/收纳」的处理，按次要问题记录。
  - 窄窗口的卡片局部工具栏已经收进视口（`max-width: min(420px, calc(100vw - var(--sp-4)))` + 换行 +
    定位时按实测宽度夹取）；如果以后按钮再增加，需要复核它是否仍能完整显示。
  - 未做：触摸 / 高 DPI / 最小窗口的像素级检查；批量列表与聊天面板的拖拽调整大小；跨设备同步。
  - 最后两处窄窗口修复（工具栏被提交区撑高、聊天面板被挤出视口）的复验是由子智能体 D 用 **Edge** 驱动完成的：
    本机 Chrome 在累计几十个 headless 实例后无法再承载（12 个进程 / CPU 91%，CDP 调用超时），
    Edge 与 Chrome 同为 Chromium、同一套 CDP，几何口径一致；主智能体本地的 Chrome 复验在最后阶段不可用。
- **后续：** 真实 QIO 理解与执行接入提交投递点；把批次判定挪到服务端（需要新的迁移）。

---

### P27 — 互动板状态恢复收尾轮：F1–F3 / N1–N6 / V1 与证据缺口（2026-10-10 第八轮）

- **Status：** partial（九项功能问题的修复与并行集成已落地；独立复核与真浏览器证据的未覆盖项在报告里如实标注）
- **起点与分支：** 起点提交 `b3245e5`（`git fetch` 后核对远端 `fix/interactive-closure-followup` 与之完全一致、**未推进**）。
  本轮分支 `fix/interactive-state-recovery-completion`（从互动开发线建立，不从 `main` 建立）。
  不合并 `main`、`fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`；**无数据库迁移**、未改历史迁移。
- **范围：** 两轮核查仍未关闭的 F1/F2/F3、N1–N6、V1，以及证据缺口 E1/E2（触发路径与用户可见结果逐条验收，不写成笼统的「优化状态管理」）。
- **统一机制（`docs/interactive-state-recovery-contract.md`）：**
  服务器 `seq` 是唯一版本事实，候选与已保存版本**分开接收**；前端只有 `adoptServerFacts`（纯函数在
  `frontend/src/interactive/serverFacts.ts`）一个合并入口：吸收版本事实、协调服务器独立变化、保留本页候选与用户勾选选择、
  同一张卡片正文两边都改时登记内容冲突交用户决定；一次保存 = 影响检查→写入→回执**整段**受「候选是否仍是当前候选」保护，
  被取代的检查不许发出旧候选；服务端对普通整板写入按真实 `seq` 兜底（旧版本 409 `stale_state`，不落库、不推进快照、不暂停任务），
  前端据此读最新事实、重新预判、重新展示范围；本机记录按「用途 + 版本 + 内容身份指纹」守卫；
  未完成输入（正文 + 适用附加字段 + 空值）作为一个整体保存与恢复。
- **并行方式（如实记录）：** 主智能体 + 四个子智能体 A（草稿与恢复：F2/N6）/ B（后端写入与撤回保护：N1/N4）/
  C（决定界面与前端设计：N2/N3/V1）/ D（独立验证：反例冻结、E1/E2），各自独立 worktree，全部从 `b3245e5` 建立；
  A/B/C 的成果由主智能体合并进集成分支，D 只写独立命名验收文件与证据、不改产品代码。
- **交付 tip：** `fix/interactive-state-recovery-completion` 分支 HEAD（本报告主体提交 `72c6d8e`；其后至多追加一次「仅回填 CI run」的文档提交，tip 的完整 SHA 与 CI run 见交付说明）；**代码/测试冻结候选 `ccc651a`**（其后 `390e0b1` 只增加独立验收证据与截图，最后一个提交只增加文档）。未合并 `main`、未发布。
- **门禁（可复跑 `scripts/recovery-lead-verify/final-gate.ps1`，在冻结候选 `ccc651a` 上实跑）：** `vue-tsc --noEmit` exit 0；
  前端全量 **194 文件 / 1723 用例全绿**；后端受影响模块（15 个引用 `interactive`/`board_store` 的测试文件）**226 用例 / 0 失败 / 0 错误 / 0 跳过**；
  `scripts/check_docs.py` 通过。独立验收（D）：前端反例 **14/14**、后端 **4/4**、E1 基线 32/32 格采集断言、E1 修后受影响 24/24；
  E2 真实浏览器旅程基线 2 绿 3 红 → 修后 **J1–J5 全绿**（含真实进程关闭重开）。详见 `docs/interactive-state-recovery-report.md`。
- **两处有意变更的旧口径（旧断言随行为更新，不是回归）：** R1/F1「取消+回读失败」改为「本地撤回立即生效 + 撤回未被服务器确认 + 重试入口」；
  08 路径2「等待期间又改别处」的重新核实信息挂到新的确认说明 `note` 并清掉 `impactCheckError`（避免界面同时宣称两种状态）。
  理由与受影响文件见 `docs/interactive-state-recovery-contract.md` §8.5。
- **CI（推送后实跑）：** run **38043038509**（https://github.com/HebiIsHere/QIO/actions/runs/38043038509）**9/9 任务 success**，
  对应提交 `ab1b6ca`（`git ls-remote --heads origin fix/interactive-state-recovery-completion` 已核对一致）。
- **已知限制（详见报告 §7）：** 服务端版本门是「读 `seq` + 比较」，没有数据库级串行化（并发同 `seq` 的两个请求仍可能都通过）；
  `scripts/closure-c-verify`、`scripts/interactive-verify`、`scripts/closure-d-verify` 里仍有把固定 `seq` 写进 PUT 的旧脚本（在非空板面上会 409，建议后续统一按「先读当前版本」改造）；
  真浏览器层的覆盖范围如实标注（R2/R5 与 N1/N4/N5 的浏览器级证据未做，已有 store/ASGI 层反例；N6 的 file/code 字段真机未验证）；
  同一浏览器多页面共用一份本机记录槽位（既有设计限制）。
### P28 — 互动板收尾后续轮：六条反例与前端验收收尾（2026-10-10 第七轮）

- **Status：** partial（六条反例的修复与并行集成已落地；独立验收的阶段 3 结论与未覆盖项见报告 §8 的如实标注）
- **起点与分支：** 起点提交 `1da2172c6876c98f9b1076b4477dc75e7fe37638`
  （`git fetch` 后 `git ls-remote --heads origin` 核对远端 `fix/interactive-final-closure` 与之完全一致、**未推进**）。
  本轮分支 `fix/interactive-closure-followup`（从互动开发线建立，不从 `main` 建立）。
  不合并 `main`、`fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`；**无数据库迁移**、未改历史迁移。
- **范围：** 六条新反例 R1–R6（取消影响确认后正式改动未撤回；迟到的读取回退已成功保存的新板面；
  保住第二版正文却丢掉第一版保存的版本事实；本机删除重试误变为整份草稿清除；服务端兜底确认框拿不到 `checkId`；
  确认说明未列出的新运行任务也被暂停）+ 前端审美与可读性、失败文案层级、窄窗口与双面板避让、字体真实加载验收。
- **共同规则（`docs/interactive-closure-followup-contract.md`、`docs/interactive-mode-contract.md` §13）：**
  候选与已保存版本**分开接收**（旧回执吸收版本事实但不替换候选）；取消即丢弃候选并**收回版本记账**；
  本机记录的**处理目的**显式保留（删本机冗余副本 ≠ 整份草稿清除）；影响确认**绑定真实范围**并由服务端保存前重核；
  兜底路径必须补取带 `checkId` 的预判；无关任务变化不制造无意义重复确认。
- **并行方式（如实记录）：** 主智能体 + 四个子智能体 A（草稿与恢复）/ B（后端确认）/ C（界面与审美）/ D（独立验收），
  各自独立 worktree，全部从 `1da2172` 建立；A/B/C 的成果由主智能体合并进集成分支，D 只写独立命名验收文件、不改产品代码。
  **C 与 D 的会话在收尾阶段先后异常中断**：C 的产品代码与全部证据在中断前已提交并并入（其工作区中断后的中间态未被并入；
  其中一个已通过但未提交的用例由主智能体代为提交并注明来源）；D 的阶段 1（基线红）与集成分支分层自测证据已并入，
  阶段 3 的正式结论按报告如实标注。
- **门禁（交付 SHA `fc7184f5075d98ba51058a8e6a958eff2f94f59c` 上实跑）：** `npx vue-tsc --noEmit` exit 0；
  前端全量 **178 文件 / 1618 用例全绿**；后端全量 **2167 用例 / 0 失败 / 0 错误 / 9 跳过**；`scripts/check_docs.py` 通过（35 个里程碑条目）。
  远端 CI **run 38026730414**（https://github.com/HebiIsHere/QIO/actions/runs/38026730414）**9/9 任务全部 success**。
  可复跑入口 `scripts/closure-lead-verify/final-gate.ps1`（vue-tsc / 前端全量 / 后端全量 / 文档一致性一次跑完并落真实输出）。
  详见 `docs/interactive-closure-followup-report.md` §6 与 §7。
- **已知限制（详见报告 §9）：** 真浏览器深链路本轮只覆盖 R1 与「真实进程关闭重开」，R2/R3/R4/R6 由状态/组件/真实 HTTP 层覆盖；
  `test_interactive_during_heavy_work` 的健康探针计时用例在并发负载下偶发超时（单独复跑通过，无并发全量通过）；
  480×600 常态工具栏 104px（96px 口径只对 800×600 要求）。
### P29 — 互动模式收尾轮：二十项可靠性与界面收口（2026-10-09 第六轮）

- **Status：** partial（二十项修复与集成都已落地；独立验收的机器可复制证据与未验证项见下）
- **起点与分支：** 起点提交 `b67d1fecd5a4f3a71c02c4b944415c0761d61d60`
  （`git fetch` 后核对远端 `fix/interactive-content-protection-polish` 与之完全一致、**未推进**）。
  本轮分支 `fix/interactive-final-closure`。不合并 `main`、不合并
  `fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`；**无数据库迁移**、未改历史迁移。
- **范围：** 上一轮遗留五项（迟到读取、重发身份、跨话题关联、绑定前归属、冲突被改写）
  + 本轮十五类（板面版本、草稿清理绑定、影响确认链、长草稿、话题迁移、清除事实、删除失败、
  重开重发、错误可见、审批材料复核、去重身份、演示成组落地、定位与浮键、临时按键复位）
  + 第六章前端审美核验。
- **统一机制（`docs/interactive-final-closure-contract.md` M1–M9）：** 板面/草稿的读取与写入版本基线；
  本机恢复副本与「计划清除 vs 真实落盘」；发送尝试身份 + 显式重发关联；影响确认的 checkId 版本绑定；
  提交的 baseStateVersion 校验；审批材料依据指纹；去重按对象身份；几何四方向与按键复位。
- **并行方式（如实记录）：** 计划 5 个子智能体（A/B/C/D/E）。**A、B 两个会话反复中断、零产出**，
  改用替代智能体 A2（草稿底层与清除事实）、A3（卡片冲突与完成编辑）、B2（会话发送链路与话题迁移）接续；
  B3 范围（对话页/失败原文错误可见、被让位草稿界面）由主智能体亲自完成。
  **C 的交付不完整**（缺 `impact-check` 路由与 `confirm_binding_version`），**D 的 19/20 用例未绿**：
  主智能体补路由与辅助函数、修 16（状态流转把 `__basisWatch` 私有键丢掉）与 17（端点身份被正文指纹顶替）
  两个关键缺陷、完成 19（定位换算二次减滚动量、任务定位真实通知）与 20（失焦/隐藏/Escape/卸载复位）、
  并接完 A2 的删除/清除底层到 store 的 6 处调用点。E 建立二十项正确行为验收矩阵并跑基线探针。
  交付按「部分并行 + 主智能体串行收尾」描述。
- **门禁（最终提交实跑）：** `npx vue-tsc --noEmit` = 0；前端全量 `npx vitest run` = **579 文件 / 1549 用例全绿**
  （与后端并发的一次运行中 `eventBufferOverflow` 负载敏感用例失败，单独复跑与无并发全量均全绿）；
  后端 `pytest` exit 0；`python scripts/check_docs.py` 通过（34 个里程碑条目）。
- **分层验收：** ①单元/状态与②组件/DOM 由各批 `final-*` 用例覆盖；③API/数据库：
  `scripts/final-lead-verify/final-lead-api-journey.py`（真实 HTTP + 临时库）**12/12 通过**
  （服务端门 409、带确认落库、过期提交 409 且无成功记录、`delivery.delivered` 未被伪造）；
  ④真浏览器：1440×900 / 1024×768 / 800×600 / 480×600 × 明暗两主题截图与几何测量
  （工具栏高度 51 / 80 / 80 / 108px，无溢出、无裁切）；⑤关闭重开：同一用户目录结束进程后新进程恢复同一草稿文字。
- **独立复核：** 专设的验收智能体 E 在被中断前交付了 20 项反例矩阵与基线探针；
  另由**不参与开发的独立 agent** 自写探针做对抗性复核：01/12 组件级 3 例全绿、
  16 号后端 16/16、17 号后端 14/14，并独立跑出前端全量 168 文件 / 1549 用例全绿。
  它发现并促成修复了两处开发者漏掉的缺陷：12 的第二个反例（重试未先补写本机清除保护）
  与 12b 的多页面版本守卫缺口（补写 cleared 时不校验记录版本，会把另一页面后来写入的更新草稿覆盖并删除）。
  两处修复后独立探针 4/4 通过。第二批独立探针再覆盖 06/07/08/02/03/04：未突变 8 用例 + 后端 21/21 全绿，
  并用定向突变逐项证明用例承重（08 用自建真实 sqlite 文件库并直接 SELECT 核对「不落库」）；该批**未发现新反例**。
  当前独立对抗证据覆盖 01/02/03/04/06/07/08/12/12b/16/17；其余条目的证据仍来自开发侧正/反例与主智能体实跑。
- **未验证项：** 真实鼠标/触摸旅程；
  比 480px 更窄的档位；验收环境中前端依赖用 junction 指向 worktree 之外，Vite 会 403 掉 webfont，
  截图使用系统回落字体（几何与布局不受影响，已在报告注明）。

### P30 — 内容保护与界面精修（2026-10-09 第五轮）

- **Status：** partial（七项修复已全部落地并通过独立反例；真实关闭重开等收尾验收见下）
- **起点与分支：** 起点提交 `403983f197b10392b3d8c96c56d62d18872d80b0`
  （远端 `fix/interactive-draft-recovery-completion` 顶端，无更新）。本轮分支 `fix/interactive-content-protection-polish`。
- **范围：** 提示词给出的五项内容保护问题 + 两项界面问题（契约 §12）。
- **并行方式（如实记录）：** 首轮四个子智能体都只读到文件就退出；重派 B2/C2/D2 并与 A 并行。
  成果与分工明细见交付报告 `docs/interactive-content-protection-report.md` §2。
- **完成项：**
  - §12.1 冲突不被绕过（payload 按服务器事实回写 + 打开编辑器不选边 + 关闭态提示可见可点）
  - §12.2 旧清除记录不删新输入（restoreLocalCardDrafts 归属→取代→cleared 顺序）
  - §12.3/§12.4/§12.5 发送记录保护（归属稳定、成功只清该次、失败原文不静默淘汰）
  - §12.6 切换条只在确有第二个面板
  - §12.7 透明区标题/说明局部底色
- **门禁（最终提交上实跑）：** `npx vue-tsc --noEmit` = 0；全量 `npx vitest run` = **145 文件 / 1417 用例全绿**
  （最终提交上含负载敏感用例全绿；此前一轮并发时超时、单独跑 4/4 通过为已知现象）；后端 `pytest` **exit 0**；
  `python scripts/check_docs.py` 通过（33 个里程碑条目）；第六轮反例合集 **46 例通过**。
- **关闭重开（已完成）：** 同一 profile 新浏览器进程验证两个入口都能找回同一失败原文
  （证据 `docs/interactive-ui-screenshots/r6-lead-{chat,composer}-reopen-recovery.png`）。
- **未验证项：** 真实鼠标旅程（探针用合成指针事件与受控接口流程覆盖，功能经状态级与组件级用例钉死）；
  360px 等比 480px 更窄的档位。

---

### P31 — 草稿恢复补齐与失败处理（2026-10-08 第四轮）

- **Status：** partial
- **起点与分支：** 起点提交 `4b16595b849d2392feb0d6991eb8d11a29f5876d`
  （= 远端 `fix/interactive-draft-races-ui-polish` 顶端，`git ls-remote` 与 `git log 4b16595..origin/...` 均为空，
  **远端没有更新的修复**；本文缺陷全部仍然存在）。本轮分支 `fix/interactive-draft-recovery-completion`。
  **不合并 `main`、不合并 `fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`、不改数据库结构与迁移。**
- **并行方式（如实记录）：** 原计划四个子智能体并行（A 卡片草稿 / B 文字发送恢复 / C 布局与审美 / D 独立验收），
  各用独立 worktree。**四个子智能体都在中途退出、没有收尾汇报**：A 留下 6 个提交、C 留下 2 个提交 + 6 个未提交文件、
  B 留下 1 个提交 + 未提交截图、D 留下基线反例用例与基线报告。主智能体逐个并入集成分支后**串行完成收尾**：
  修独立用例的三处驱动缺陷（断言未改）、补「删除卡片同步清除草稿」、修批量面板压住聊天入口的几何、
  重写不可靠的实机场景 22；随后又派一个独立验收子智能体在最终提交上复核。
  交付按「部分并行 + 主智能体串行收尾」描述，不按四路并行完成描述。
- **修复 1 · 首次输入必须能恢复（§11.1）：** 恢复改为**枚举本机记录**（不再只遍历服务器/内存已有的键）；
  本机记录带 `kind` / `version`（跨刷新单调）/ `boardId` 归属；存在性按记录是否存在（空串也算）；
  板面或卡片不存在不恢复、也不误恢复别的板面；恢复只回到待编辑内容，恢复期间的新输入优先。
  实机：编辑后约 20ms 就刷新（服务端此刻还没有草稿）→ 重新打开编辑器恢复出原文。
- **修复 2 · 清除必须同步、不能复活（§11.2）：** `clearDraft` 留下「待同步的清除依据」；
  `flushDrafts` 的条件扩成「有未保存草稿**或**有待确认清除」，清空最后一份（payload `{}`）也会发请求；
  服务器确认前不算已同步；在飞的旧保存晚到不许复活；旧版本的清除不删后来新建的版本；
  **确认编辑**与**删除卡片**两条路径都同步清除。实机：确认后与删除卡片后，服务端草稿都在 **5ms** 内消失
  （修复前 9 秒轮询仍在）。
- **修复 3 · 本地写入失败可见、过期判断按记录（§11.3）：** 区分「服务器保存结果」与「本机恢复记录保存结果」；
  过期与否只看这条记录自己的归属/版本/确认状态，不再用整个草稿集合的 `updatedAt` 或别张卡片的保存时间；
  无法判定的旧格式记录两份都保留并由用户选择。
- **修复 4 · 失败原文在两个入口都能找回（§11.4）：** 新增共用恢复组件，对话页与互动聊天都用；
  显示真实原因、找回原文、与当前文字互换、不再保留；失败事实按话题持久化，刷新/重开后仍在；
  **恢复不是发送**；清理只依据受理成功或用户显式操作。
- **修复 5 · 未绑定话题的归属迁移（§11.5）：** 发送归属带稳定身份，成功时只清理被发送的那一个版本。
- **修复 6 · 提交失败原因默认可见（§11.6）：** 默认失败区显示**本次请求**的真实原因
  （实机：拦截制造 `TypeError('Failed to fetch')` → 界面显示「失败原因：Failed to fetch」），保留情况另有说明。
- **修复 7 · 窄窗口切换条是完成元素（§11.7）：** 需要切换显示时用同一个明确布局状态算空间，
  关掉一个面板仍预留切换条高度；切换条有真实定位与令牌样式；批量列表让开右下角常驻的聊天入口按钮
  （实测按钮底边在工具栏顶边往上 58px、高 39px）。
  实机 480×600：只显示一个面板、切换条 `position: fixed` 383×34、按钮命中、点它能换面板、都在视口内；
  480×800：两块叠放、相交 **0 px²**、聊天入口仍可点。
- **追加修复（用户报告）· 拖动时关系线不跟随（§11.10）：** 卡片自己用实时坐标渲染，但
  **关系线与组框这两个图层**只拿到 store 里的静态卡片 —— 所以拖动中线段不动，松手（提交）才跳过去。
  现在给这两个图层一份「显示用卡片列表」（拖动中的那张卡片用实时坐标，其余不变），
  它们只做显示、不参与落点判定，板面状态仍然只在松手时提交。
  实机（1200×800）：卡片中心 180 → 拖动到 420 的过程中，线端点 `x1` 同步变为 420；
  基线同一操作下 `x1` 仍是 **180**（线不动）。已固化为实机场景 24（3 项断言）。
- **已知限制与未验证项：**
  - 强制结束（杀进程）、崩溃、真实无网/存储配额耗尽没有验证；本机写入失败是页面级注入。
  - 多客户端（另一设备/另一标签页同时改同一份草稿）未验证：记录 `version` 只在单客户端内单调。
  - 互动聊天入口的「关闭重开」未单独复测（卡片草稿侧有同一 profile 新进程的实机证据）。
  - 真实 QIO 未接入（`delivery.delivered=false`）：发送失败与存储失败是**模拟**，应用自身流程是真实的。
  - 负载敏感用例 `eventBufferOverflow.verify.test.ts` 在并发高负载下偶发超时（单独跑通过），未降门槛。

---

### P32 — 草稿竞态修复与界面收敛（2026-10-08 第三轮）

- **Status：** partial
- **起点与分支：** 起点提交 `224bc63`（= 远端 `fix/interactive-ui-refinement` 顶端，已用 `git ls-remote` 与
  `git log 224bc63..origin/...` 核对，**远端没有更新的修复**）。本轮分支 `fix/interactive-draft-races-ui-polish`。
  **不合并 `main`、不合并另两条开发线、不改数据库结构与历史迁移。**
- **并行方式（如实记录）：** 原计划四个子智能体并行（会话草稿 / 卡片草稿 / 布局与手势 / 独立验收），
  但**四个子智能体都在中途失败**（A/B/C 只留下半成品，D 只留下两份未完成的独立用例）。
  主智能体把半成品收拢到集成工作区后**串行补齐实现**，并修好独立用例的编译错误与驱动缺陷（断言未改）。
  本轮按「串行完成」交付，不按并行描述。
- **修复 1 · 发送归属在点击那一刻确定（契约 §10.1）：** `sendAttribution()` 记录原话题/原文/草稿版本，
  `send()` 消费这份归属发起请求；兜底归属也保护原文（未到防抖就发送，失败后原文仍落在**原话题**的键上）。
  失败事实由会话层统一持有（`failedSend`/`failedSendError` + `retryFailedSend`/`swapFailedSendText`/`discardFailedSend`），
  悬浮聊天与对话页共用同一份事实：切到别的话题时一个字都不改，也不把原话题的错误显示成那边的失败；
  原话题已有更新文字时不覆盖，两份都保留并提供取回入口（不自动拼接、改写或发送）。
- **修复 2 · 成功回执只清那一次发送的旧版本（§10.2）：** 清理依据是「键 + 发送时的草稿版本」，
  同话题后续输入与别的话题草稿都不动；输入为空不再被当作「没有新草稿」的证据；覆盖 A→B→A→B 的切换。
- **修复 3 · 串行保存不许遗忘新版本（§10.3）：** `flushDrafts` 不再「等旧请求后直接 return」；
  请求结束后重新检查未保存的新版本，只有**更新版本**才自动再试一次（有界），同一版本失败就明确报错并可重试，
  不会出现「没有请求、没有计时、却一直显示保存中」。
- **修复 4 · 空草稿是有效编辑状态（§10.4）：** 新增 `hasCardDraft`/`cardDraftText`/`clearDraft`，
  按「记录是否存在」判断（而不是空字符串真假值）；确认编辑后清记录而不是写空串，
  所以空草稿重开/刷新后仍是空的，也不会把刚确认的正式内容盖掉。
- **修复 5 · 几何模式改变必须落实面板切换（§10.6）：** 空间不足时按**最近打开的面板**只显示一个，
  常驻切换条明确告知可切换（看对话 / 看审批列表），消息、草稿、勾选与阅读位置都保留；
  挂载、缩放、工具栏高度变化都按最新几何执行，放大回去不自动弹出第二个。
- **修复 6 · 手势固定，移除常驻模式（§10.7）：** 删掉「选择 / 框选 / 连线」三个会改变手势的常驻入口
  （store 不再暴露可切换的指针模式），改为工具栏「操作说明」；规则固定为
  「空白拖动＝平移、空格＋拖动＝框选、连接点拖线＝建立关系」，纯单击重叠卡片不成组。
- **界面收敛（§10.8）：** 提交区重写成「一行简短范围 + 一行简短状态 + 详情 + 提交按钮」，
  完整清单与说明进「详情」，并删除上一轮压缩子组件文字的 `:deep()` 补丁；聊天常驻一句「文字发送不会提交板面」，
  完整范围说明进可打开的详情；聊天外层保持透明、消息与用户消息加轻底色（压在密集板面文字上仍可读）；
  互动板作用域已无硬编码色值（含 `IntentPreviewCard` 的备用阴影）。
- **本机恢复副本（§10.5）：** 卡片草稿在输入时**同步**写一份本地副本（不等防抖、不等网络），
  重新打开时只有它比服务端更新才恢复，且按对象与版本在保存成功/确认/删除后清理；
  只用于编辑恢复，不提交、不发送、不扩大 QIO 可见范围。**不把 `pagehide` 里的异步请求当作关闭恢复的证据。**
- **实测（集成实例 5441/8941）：** 主验收脚本 `scripts/interactive-verify/fe-scenarios.mjs` **79/79 通过**
  （含新增场景 16 空草稿、17 480px 单面板切换）；工具栏 1440/1024/800 = **51 / 80 / 80 px**（目标 ≤96px）、
  两面板相交 **0 px²**、提交可点、连接点命中、无横向溢出；独立用例
  `d4ChatDraftRace.verify.test.ts` **18/18**、`d4CardDraftRace.verify.test.ts` **10/10** 通过。
- **已知限制与未验证项：**
  - 探针浏览器每次调用新建实例、**localStorage 跨调用不保留**：因此「关掉浏览器再打开」的草稿恢复**没有实机验证**，
    只验证了同一次运行内 `reload` 的恢复；`pagehide` 路径本身也不作为证据。
  - 强制结束（杀进程）与崩溃未验证；本机恢复副本的清理只在「保存成功/确认/删除」路径上验证。
  - 本机无模型凭据：真实 QIO 回复不可验证；发送失败是**页面级拦截模拟**（应用自己的失败处理是真的）。
  - 480px 属于**浏览器窄窗口边界**，与桌面最小窗口 800×600 是两回事，两者都测了但结论分开写。
  - 话题切换的实机验证受限于实例只有一个话题，主要靠会话层/组件级用例覆盖。

---

### P33 — 互动板修复与前端审美优化（2026-10-07 第二轮）

- **Status：** partial
- **起点与分支：** 起点提交 `4538578`（= 远端 `feat/interactive-frontend-layout` 顶端，已用 `git ls-remote` 核对，
  自上一轮核对以来**没有新提交**；产品实现对应 `1306611`，其后只补了验收脚本）。
  本轮分支 `fix/interactive-ui-refinement`。四路并行开发（布局与审美 / 批次与分组规则 / 草稿与恢复 / 浮层与独立验收），
  各自独立 worktree，主智能体负责契约、冻结接口、集成与最终验收。
  **没有合并 `main`、没有合并 `fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`、
  没有改历史迁移、没有新增数据库结构。**
- **本轮范围：** 修掉用户点名的五个问题 + 互动板前端审美与信息层级；契约新增
  `docs/interactive-mode-contract.md` **§9**（明确覆盖此前写偏的 §1.3/§6.1/§6.4/§8.1/§8.5），
  实施方案见 `docs/superpowers/plans/2026-10-07-interactive-ui-refinement.md`，
  交付报告见 `docs/interactive-ui-refinement-report.md`，独立复核见 `docs/interactive-ui-verify-integrated.md`。
- **修复 1 · 默认组名（§9.1）：** 所有需要系统默认名的路径统一 **「默认组名」**（前后端一致，不只是显示文字）；
  `defaultName` 只表示「系统给的名字」；组身份按 id、默认名可重复；历史「组 N」不被批量覆盖
  （识别默认名时同时认两种形态）；留空/取消改名时组仍成立并保留默认名；加入已有组保留已有组名。
- **修复 2 · 批次只靠可证明来源（§9.2）：** **删除**「`createdAt` 截断到秒就是同一批」这级推断。
  早期实现把两个不同批次各两项（同一秒创建）误并成一个四项批次 —— 契约 §8.5 里「宁可不出批量列表，
  也不把不同批次相加」的说法**与实现不符，已作废并写明原因**。现在只用：会话里按次记录的创建批次
  （有界：条数上限 + 7 天 TTL + 读侧上限 + 损坏/禁用/写满降级）与真实 `submissionId`；都不可靠时各自成批。
- **修复 3 · 批量列表保留到处理完（§9.3）：** 资格从「剩余待审批 ≥4」改成「该批产生的意图总量 ≥4 且还有未处理项」，
  入口显示「待审批 N 项（共 M 项，已处理 K 项）」，已处理项不能再被选中/提交，全部处理完后入口消失，
  刷新后资格与处理状态保持，开合只由用户决定。
- **修复 4 · 聊天草稿持久化（§9.4）：** 草稿按会话/话题持久保存（防抖 400ms + 收起面板/页面隐藏时落盘），
  刷新后恢复且不自动发送；发送**受理成功才删**，失败恢复原稿且不覆盖新输入；迟到的保存回执用序号判定丢弃；
  存储不可用时明确提示并允许重试（不显示「已保存」）；悬浮聊天与对话页共用同一份草稿、单一写者。
- **修复 5 · 卡片草稿失败反馈（§9.5）：** 草稿保存有真实状态（idle/saving/saved/error）+ 失败原因 + 重试，
  提示就近显示在编辑器里；关闭编辑器或页面卸载前把待保存内容落盘；重试不建卡、不提交、不调用 QIO。
- **审美与布局：** 底部工具栏常态只留添加/搜索/撤销重做/视图/可见范围/右端提交，分组类操作移到**选中对象附近**的
  `SelectionMenu`（未选中时整块不存在，条件不满足的操作不显示而不是禁用）；顶部只留板面名、**唯一的**保存状态、
  未接入状态与必要入口，数量退到次级；聊天与批量列表**并排优先**、按实际可用区域算尺寸（不再「窗口高度减固定值」）；
  卡片类型→标题→正文→元信息分层，组框更轻，正式内容实线 / 待审批完成后虚线配文字。
- **实机证据（详见交付报告）：** 三档尺寸 × 暗色亮色对照截图 `docs/interactive-ui-screenshots/`
  （`before-*` 36 张 / `after-*` 36 张，同数据同尺寸同主题）；指标 `metrics-before.json` / `metrics-after.json`：
  工具栏高度 172→**60**（1440）、236→**84**（1024）、309→**84**（800×600，目标 ≤96）；
  聊天×批量相交 127200/133600/94864 → **0 px²**；提交按钮三档可点；连接点三档命中；无横向溢出。
  完整验收脚本 `scripts/interactive-verify/fe-scenarios.mjs` **69/69 通过**（含新规则的反例场景 13/14/15）。
- **本轮修掉的真实缺陷（集成期发现，都有实测证据）：**
  1. **演示入口复用路径漏记批次**：后端对尚未结束的同名演示项会复用，store 只记录「新出现的 id」，
     复用时整批记不进去 —— 实测「点了生成 4 项，批量入口却不出现」。改成以**接口返回的这一批**为准。
  2. **多选时每张选中卡片各渲染一份局部工具栏**（叠成一摞，点击命中的是板面顺序最后那张）：
     改成只由所选里最后一张渲染。
- **验收脚本本身的问题（已修）：** 上一轮「连跑失败、单跑通过」的根因是**跨会话量坐标**——
  两次会话之间顶部栏高度会变（实测差 28px），拖拽起点落到卡片外；现在探针新增 `drag` 操作，
  在**同一会话里**先量锚点再派发真实鼠标事件。取值一律按标记取，不再「取最后一个 JSON」。
  探针收尾改成先 `Browser.close` 再兜底 kill（避免刚写的 localStorage 被强杀丢掉）。
- **已知限制（本轮未解决，如实记录）：**
  - 探针 Chrome 每次调用新建实例，**localStorage 跨调用不保留**（同一次运行内 reload 正常）：
    因此「关掉浏览器再打开」这类跨调用持久化无法在本环境验证；草稿/批次记录的恢复证据是同一次运行内 reload。
  - 本机无模型凭据：真实 QIO 回复不可验证；发送失败与存储失败都是**页面级拦截模拟**（报告中逐条标注）。
  - 健康探测 130ms 现象：`backend/tests/test_interactive_during_heavy_work.py` 的相关用例复跑 5 次全过
    （CPU 48–91%、空闲内存 9.8–10.8GB），**未复现**；不降低门槛、不删测试，按待确认的稳定性现象记录。
  - 未做：触摸 / 高 DPI / 最小窗口的像素级检查；真实 QIO 理解与执行仍未接入（`delivery.delivered` 恒为 false，
    界面与文档都如实写明）。

---

### P34 — 主线可靠性审计修复（F01—F12，2026-10-11）

- **Status：** completed
- **Implementation：** 按审查基线的十二类可靠性缺陷逐条修复，分布在四条线上：
  * 板面与草稿（F01/F07/F08/F10/F11）：`backend/src/agent/interactive/board_store.py`、
    `backend/src/agent/api/interactive_store.py`、`frontend/src/stores/interactive.ts`、
    `frontend/src/interactive/serverFacts.ts`、`frontend/src/interactive/submission.ts`。
    草稿保存改为逐键三态协议（`mode=patch` + `baseRev` + `removed` + `keyVersions`，未出现的键 = 未修改）；
    本地删除的组/关联按 base 三路判定，服务器无关改动不再复活它们；已确认的最高服务器版本独立于候选正文单调推进；
    提交结果与后续读取结果分开记录；任务清单读取失败与「成功空集合」区分。
  * 发送与恢复（F02/F03/F04/F12）：`frontend/src/stores/session.ts`、
    `frontend/src/components/Composer.vue`、frontend/src/components/interactive/ChatDock.vue（2026-10-26 已随旧互动展示层删除）、
    frontend/src/components/interactive/FailedSendNotice.vue（2026-10-26 已随旧互动展示层删除）、`backend/src/agent/core/turn.py`、
    `backend/src/agent/api/server.py`。每次发送冻结不可变提交快照（原文 / 话题 / 显式附件列表 / 请求身份 /
    重试来源 / 起点信息）；未决发送有独立持久台账，不做数量裁剪；受理事实由
    `acceptance_facts()` 如实给出四种状态（`preparing` / `not_accepted` /
    `cancelled_after_accept` / `accepted`），同身份重试从原请求的真实记录返回受理事实与原附件绑定。
  * 记忆与关闭（F05/F06）：`backend/src/agent/services/memory_lifecycle.py`、`derived_tasks.py`、
    `maintenance.py`、`app.py`、`entities/cards.py`。派生任务的领域写入与任务完成进同一事务并原子核验
    owner + claim_generation + 状态 + 内容版本；手动维护与周期维护纳入同一生命周期管理，
    关闭时拒绝新维护、有界等待并确认执行单元终止。
  * 凭据验证（F09）：`backend/src/agent/credentials/store.py`、`backend/src/agent/api/server.py`。
    验证开始前冻结输入指纹（密钥版本 + endpoint + default_model + kind），结果条件写入；
    旧配置的迟到结果只记「历史结果」，不改当前可用性、默认项与自动候选。
- **Tests：** `backend/tests/test_audit_f01_draft_patch.py`、`test_audit_f04_acceptance_receipt.py`、
  `test_f05_superseded_writes.py`、`test_f06_maintenance_close.py`、
  `test_credential_verify_fingerprint.py`，以及前端 `src/acceptance-state-recovery/f01/f07/f08/f10/f11-*`、
  `src/stores/__tests__/b-sendSnapshotRecovery.test.ts`、`b-sendRequestShape.test.ts`、
  `src/components/__tests__/b-chatDockPendingSend.test.ts`。
- **Known limitations：** 本轮只修可靠性缺陷，**没有**接入互动模式的真实模型、没有新增产品功能、没有重做视觉。
  互动板的对象级删除/修改冲突入口目前只在提交区给出；草稿协议的旧客户端（不带 `baseRev`）删除会被
  `blockedRemovals` 拦住，属有意的安全取舍（建议先发后端再发前端）。
  `tests/test_derived_tasks.py::test_recover_stale_running_tasks_after_restart` 是**基线既有**的顺序依赖用例：
  单独跑该文件会红，与 `test_derivation_recovery.py` 同批（即全套字母序）通过；与本轮改动无关。
- **后续依赖：** 无强制依赖；草稿三态协议要求前后端同版本或后端先行。
  本轮的「没有接入互动模式的真实模型」已由下面 **P35** 补齐（来源分支 `feat/interactive-real-model`）；
  P35 的真实模型人工验收仍未完成，两条限制按各自小节读。

---

### P35 — 互动模式真实模型理解与真实执行（2026-10-11 第九轮，整合编号）

- **Status：** partial
- **真实 main 基线：** `8fa845255c118a7104b1de8e37298ca745924482`（fetch 后 `origin/main` 的实际顶端，
  与上一轮记录一致）；交付分支 `feat/interactive-real-model`。
  五路并行（A 提交受理与材料读取 / B 真实理解与结构化校验 / C 批准后真实执行与调度 / D 前端状态与恢复 / E 独立审查），
  各自独立 worktree 与分支，主智能体负责契约冻结、公共文件、顺序集成与最终验收。
  没有合并 `main`、没有发布版本、没有改历史迁移（只追加迁移 37 与补偿 38）。
- **契约：** `docs/interactive-mode-contract.md` **§14**（本轮冻结接口：理解状态、材料来源、允许操作集合、
  HTTP 接口、跨模块签名、前端接口、文件所有权）。交付报告 `docs/interactive-real-model-report.md`，
  独立审查 `docs/interactive-real-model-review.md`。
- **受理与理解分开（§14.1）：** 提交成功建立一条 `accepted` 的理解请求并把它交给后台真实理解，
  返回 `delivery.delivered=true` / `delivery.status='accepted'` / `delivery.understandingId`；
  理解完成分三态 `understood` / `needs_clarification` / `no_action`，失败为 `failed` 且可从保存的快照重试。
  受理失败（理解模块不可用 / 建立请求抛错）按失败处理：不更新基准、不清勾选、保留改动；
  **受理之后**的模型失败只写理解记录，不回改提交记录。
- **材料真实读取（§14.1 材料来源）：** 文件卡按 `meta.file.attachmentId` 读已保存附件的正文，
  复用既有附件服务与既有解析能力；没有 attachmentId 的卡片如实标成「只有文件名，文件名不是正文，未读取正文」；
  图片在当前模型链路没有视觉能力，一律 `image_unsupported`；网址区分「已读到正文」与「只有地址」；
  截断与未读取范围写进上下文 `limits` / `honesty`，不声称已完整阅读。
- **真实模型理解（§14.3 / §14.5）：** 上下文**只从冻结的提交快照构建**（不读当前板面），
  以结构化上下文交给既有模型链路（复用凭据、用量、预算与脱敏机制）；
  程序校验结构、对象引用、材料范围、操作类型、依赖与冲突、修改范围；无效输出不落成可执行任务并给出可读原因，
  自动修复至多一次。模型输出三种判定，**不强制每次提交都生成任务**，也不用固定模板或关键词映射。
- **真实执行（§14.4 / §14.5）：** 批准后建立执行记录并**实际启动执行**；只允许
  `ALLOWED_INTENT_OPS`（生成结果卡 / 文字卡、移动卡片、成组、排序、连线）——用户在互动模式中能做的子集。
  逐步落 checkpoint、写前写后重核材料与对象版本、暂停 / 继续 / 取消作用于真实执行、
  迟到结果按 (执行记录, attempt, 所有者实例) 丢弃、重启后保持暂停、恢复不重复已完成步骤、
  失败撤回只处理该任务造成且仍可安全撤回的改动、重复批准不重复执行。
- **数据库：** 只追加迁移 37（`board_understanding` / `board_executions`，以及意图的目标 / 预期 / 操作 /
  依据 / 稳定身份 / 理解来源列）与补偿 38；历史迁移与用户数据不动。
- **前端：** 提交受理 / 模型理解 / 理解完成 / 任务执行四种状态分开显示；理解阶段可展开历史；
  执行显示排队 / 等待依赖 / 正在执行 / 暂停 / 结束，成功、失败、取消都结束读条；
  待补充、待审批、失败、暂停、取消各有明确状态与可用操作。
- **验证（模拟模型）：** 各模块定向用例 + 主智能体端到端验收 `backend/tests/test_interactive_real_model.py`
  （提交 → 受理 → 材料 → 理解 → 意图 → 审批 → 执行 → 结果卡与来源引用；未勾选 / 隐藏 / 间接链接不进上下文；
  换材料与要求结果相应变化；重复提交不额外调用模型；三种理解判定；重复批准不重复执行）。
  全部使用 fake/mock provider，不联网、不依赖真实 API Key、不产生真实模型费用。
- **已知限制：**
  - **没有真实模型人工验收**：本环境没有可用的、已明确授权的模型凭据，真实模型判断质量与真实执行链
    没有在本轮跑过；不把模拟验证说成真实模型验收。
  - 图片视觉理解未接入（当前模型链路没有视觉能力），界面与上下文都如实标注不支持。
  - 长期记忆复用既有的知识 / 长期记忆机制（落待审批），没有改写用户会话 transcript。
  - 引用型附件（没有副本、直接引用原文件）的读取路径没有专门用例覆盖。
  - 结果卡正文来自模型在**理解阶段**产出、并经用户在预览中批准的那段内容；执行阶段只在方案没有正文时
    才调用模型生成。
  - 前端窄窗口与主题只做了 DOM 与样式令牌级验证，未做真实浏览器截图核对。
- **后续：** 真实凭据下的端到端人工验收；图片视觉能力接入后打开 `image_unsupported`；
  引用型附件读取的专项用例；前端实机视觉核对。

---

## 尚未完成

这些是最容易让后续 Agent 误判的地方，明确列出来：

- **互动模式已接通真实理解与真实执行，但还没有真实模型人工验收**（见 P35）：板面、分组与顺序、
  可见范围、保存与提交、虚线预览与审批边界都是真的；提交现在会建立理解请求并真的调用模型理解
  （三种判定：提出意图 / 需要补充 / 无需开展任务），批准后会真的执行允许的板面操作并落结果卡。
  **仍未验证的是「真实模型判断质量」与真实凭据下的端到端链路**：自动化证据全部来自 fake/mock provider，
  本环境没有可用的已授权模型凭据；**不要把模拟验证说成真实模型验收**。
  演示入口（`demo=true` 与 `/demo/advance`）仍然存在，但演示推进不得驱动真实任务，正式路径不显示演示内容。
- **斜杠命令体系**：未实现。工具创建没有独立的「入口页面」——按第三阶段的入口原则，
  它在对话里发生（说明需求 → Agent 调 `create_tool` → 同一张工具创建卡推进到「已创建」），
  设置页只放长期配置（电脑操控权限、联网通道），不承担这个动作。
- **对话页内嵌的记忆/知识面板**：未实现，面板在星球页详情里；对话页也不做常驻面板。
  高影响知识候选在回答完成后进入统一审批的「待确认」（`UnifiedApprovalLayer` 直接 ingest），
  保存 / 修改 / 忽略都在统一审批弹窗内完成，不再有对话流内的候选卡。
- **工具创建的真实端到端**：**后端全链已在安装包自带的 sidecar 上跑通**（2026-10-02 第二阶段：
  `create_tool → dev_write_file → dev_run_tests(2/2) → dev_submit_tool → 两次审批 → 注册 → 调用 → 工具记录`，
  见 `docs/e2e-install-2026-10-02.md`）。**仍未跑的是界面那一层**：这条链是在 API 上用离线假厂商驱动的，
  「真实模型在对话里提出需求 → 界面上同一张创建卡推进到已创建」没有在真实界面里走过。
- **工具开发第一阶段的三个未收口项**（2026-09-30 收尾后的现状，详见文末「第一阶段收尾」一节）：
  1. **最终结论的事实校正已做完（后端 + 界面标记）**：后端有「每轮事实台账 +
     `declare_completion` 核对 + 收尾事实注记」，界面上的「后端已核对」那一行也已上线
     （见文末「结论标记与已创建文案」一节）。「未完成任务入口与审批跨重启语义」也已补齐
     （见文末「未完成任务入口 · 前端」与「审批跨重启」两节）。
  2. **测试前授权与真实能力策略没补**：`dev_run_tests` 在拿到任何授权之前就执行生成代码，
     测试用的隔离数据（临时库 / 临时目录 / 模拟服务）也没做。
  3. **已保存对话的原文检索已补齐**：`memory_search` 现在同时检索保存的对话原文，
    开放片段与摘要失败的内容都找得回来（见文末「已保存对话的原文检索」一节）。
- **能力降级的真实触发未跑**：`CAPABILITY` / `FALLBACK` 的语义有测试（native 不提示、text 只在
  模式变化那一次提示、unsupported 不发降级），但没有用真实「不支持原生工具调用」的模型跑过。
- **子 agent 长任务未验证**：独立任务卡的 queued / running / done / failed 与结果回传有测试，
  但耗时很久（分钟级）的真实子任务没有跑过。
- **片段级检索偏置（已评测，决定不实现）**：Planet「从这里开始」/ Agent
  `continue_from_fragment` 只改变 Focus 与当前位置，**不**改变记忆检索的排序权重
  （检索侧只有话题级亲和 `anchor_topic_id`）。离线 Anchor Continuation Eval
  （`agent/eval/anchor_eval.py` + `backend/evals/anchor_continuation/` 下的 case 集）对比了
  baseline（Focus + 语义检索 + 身份去重）、focus_only 与 anchor_distance（按序数距离加权）：
  距离偏置 recall@5 无提升（1.00 → 1.00）、MRR 反而下降（0.667 → 0.633）、
  wrong-memory injection 翻倍（0.20 → 0.40）、anchor distraction 上升（0.333 → 0.667），
  因此**不实现**距离偏置。基线数字与结论存于 `backend/evals/baseline.json`，
  `tests/test_anchor_eval.py` 会守住这个决策（哪天评测翻盘会直接测试失败，强制重新决策）。
- **取消只掐客户端这一头**：取消现在会**真的中断**正在等待的模型请求（不再等它跑完再丢结果），
  单飞队列里的下一条消息也会立刻开始；但服务端是否立刻停止生成由供应商决定 ——
  「不再占用等待时间、不再堵住下一条消息」有保证，「不再产生费用」没有。
- ~~**Trace 时长归因缺口**~~ **已修（第一阶段，2026-10-02）**：turn 现在有阶段账本（`agent/trace/phases.py`），
  顶层阶段铺满时间轴、缺口成为显式的 `other`，`duration = 各阶段 + residual`。真实一轮实测
  `duration_d 1699ms = sum 1699ms / residual 0`，其中 `approval_wait 910ms` —— 正是「模型 1.7 秒、
  turn 51.5 秒」的形状，现在有名字。那次事故的原始 Trace 已不在本机（数据目录是空库），
  所以「那一次具体是不是审批等待」无法反查，修的是「不可解释」本身。
- **无嵌入模型时话题预判变弱**：缺本地 ONNX 模型时降级到规则层，关键词重叠分数被 1-gram/2-gram
  分词稀释；阈值调整需要 eval 支撑（见 P3）。
- **内置嵌入模型已有离线基线，但只有两小套用例**：`python -m agent.eval.run --embedding onnx`
  把真实模型接进话题判定（12 条）与检索（8 条）两套评测；结果与明细留在做那次评测的分支上，
  没有随主线提交（要复核就重跑这条命令）。评测数字指向两处需要重新校准：
  话题阈值 0.7 对真实模型偏严（「继续聊 SQLite 迁移」被判成新话题），检索排序权重是在
  「关键词召回候选很少」的前提下调的（向量召回把「近但无关」的条目也带进候选，`person_entity`
  与 `cross_topic_recall` 因此从对变错）。锚点延续评测没有接模型；用例规模只够看方向，
  不是生产准确率。
- **记忆的类别化衰减**：只对已有可靠元数据（知识条目 vs 片段）做差异化；未引入模型生成的记忆分类字段。
- **多用户/多会话并发 Agent Server**：明确不做。当前是单机、单用户的 single-flight 主 turn。
- **非 Windows 平台**：keyring 与桌面壳只在 Windows 验证，Linux/macOS 未验证。
- **联网搜索通道（2026-09-12 重做）**：免密钥搜索改为「Exa / Parallel 免费 MCP +
  DuckDuckGo HTML」，实测可用（前者返回结构化结果，后者中文结果正常但会限流）；
  必应/百度降级为最后兜底，并保留两道闸门（解析抓 `h2 > a`、相关性闸门、反爬通道
  冷却 10 分钟，见 `services/params.py::SEARCH`）。公共 SearXNG 实例实测全部被
  Anubis/Substation 拦截，故只支持自建实例（设置 → 高级 填 URL）；博查 Key 仍然最稳。
  免密钥开关在「设置 → 模型与联网」，默认开启、保存即生效。
- **搜索设置的即时生效**：此前保存 SearXNG/博查只写 settings 表，运行中的
  `SearchService` 不更新（要重启后端才生效）——已修为保存时调用
  `AppContext.apply_search_settings()`。
- **单 turn token 已有输入/输出分解，前端展示仍是总量**：后端 `USAGE` / `TURN_END` 现在给出
  `input_tokens` / `output_tokens` / `total_tokens`（见 P10 的 `ModelUsage`），但界面目前只显示
  总量，不展示分解数字（需要界面设计后再开）。
- ~~**审批成功路径未做端到端注入**~~ **已补（第一阶段）**：真实创建 pending approval → 无头浏览器点击批准 →
  库里 `pending_approvals.status=approved`、`tool_calls` 该命令**只有 1 行**（只执行一次）；
  请求体带 `turn_id` + `request_digest`。第二阶段又补了审批的**会话身份**真正绑定
  （`pending_approvals.session_id` 以前恒为 NULL，那条比对是死代码）。
- **无障碍只做了自动化抽样**：Tab 顺序、focus-visible、对比度（暗/亮）已用无头浏览器脚本检查，
  未做完整 WCAG 审计，也未做屏幕阅读器实测。
- **星球浏览体验只做了结构 + 静帧验证**：同屏上限、旋转推动话题流、反向连续性、对象池复用
  都有自动化测试与真实运行证据（`scripts/baseline/planet-phase2-probe.mjs`），
  但惯性曲线、触控板手势、逐帧「看不到数据替换」与真机 GPU 帧率**未验证**；
  完整清单见 `docs/release-planet-phase2.md` 的「剩余问题」。
- **第四阶段（视觉语言）尚未收口的项**（2026-09-20 收口一轮后更新）：
  - **工具创建卡只统一了语言、没有真实端到端跑通**：家族骨架（`.qio-card` + `data-state` + 状态徽章 +
    真实高度过渡的展开详情）已在代码与单测层面成立，但验收脚本里「界面上没有工具创建卡」——
    需要真实模型触发创建流程才能看到它，这条链路仍未在本阶段跑过（与本文档前面 M10 的已知限制同源）。
  - **Planet 关键转场只在无头软件渲染下逐帧验证**：`scripts/baseline/qa/phase4.mjs` 用
    swiftshader 采到了 0.13 → 1.00 的长大与 1.00 → 0.18 的收拢，但没有在真机 GPU、120Hz 屏、
    触控板上重新测量帧率与手感；`?planetdemo=slow` 只能逐帧看，不能替代手感评估。
  - **首次打开的冷启动耗时没有优化**：实测 WebGL 初始化 + 话题装配要 1.5–3.4s（软件渲染更久）。
    本阶段只保证「这段时间不会把转场动画吞掉」（等数据与渲染就绪再开始展开），没有缩短它。
  - **动效手感未经真人评审**：所有判断来自令牌、截图与帧序列，需要真实用户反馈来定
    「高频弹性是否合适、中频是否偏慢」。
  - **收缩中途的画质已查清（2026-09-20）**：曾怀疑「中间段间歇性断续」，pilot 用两条独立证据
    否掉了 —— 冻结渲染下确实能复现（那是栅格化时机造成的假象：画面冻住后浏览器不再按动画尺度
    重新栅格化），真实动画里「缩放合成层」（现状）与「缩放投影」（候选）的墨迹量逐档相同
    （40–60px 档 4.6 vs 4.5、350–1000px 档 18.5 vs 18.4）。那条「把缩小搬进渲染器」的路数
    几何等价性已验证（目标 300px 量到 302px），但它换来的是「主线程一卡动画就卡」
    （探针轮询下一度 p50 34ms vs 16ms），因此**决定不做**；尾段钉死 1:1 保留作为加固。
   判断这类问题必须以**真实动画**的逐帧数据为准，冻结渲染的静态对照会放大差异。

---

## 核对方法

文档里的状态可以自己复核，不需要信任本文件：

```powershell
# 1. 文档一致性检查（里程碑状态、被引用的路径与命令是否存在、是否出现硬编码数字）
python scripts/check_docs.py

# 2. 后端测试（uv 环境）
cd backend
uv run --frozen pytest

# 3. 评测基线（离线、确定性）
uv run --frozen python -m agent.eval.run

# 4. 前端
cd ..\frontend
npm ci
npx vue-tsc --noEmit
npm test
```

命令与 CI 一致，见 `.github/workflows/ci.yml`。

---

## 本轮变更：Fragment 重设计（2026-09-21 起）

> 任务书：`docs/superpowers/specs/2026-09-21-fragment-redesign-spec.md`
> 实施计划与逐条清单：`docs/superpowers/plans/2026-09-21-fragment-redesign.md`
> 配套笔记：写入入口清单、分段边界评测报告（`docs/superpowers/notes/`）

这一轮把「一轮对话属于哪个 Topic / Fragment」从「写入时临时看 Anchor」改成**持久绑定**，
并把封存、派生数据、历史路径、分段边界逐层拆开。按阶段记状态（口径同上表）：

| 阶段 | Status | 说明 |
| --- | --- | --- |
| 1 固定轮次归属 + 安全接续 | completed | 绑定与接续意图两张表；点击历史只登记、执行时才原子交接；队列顺序与幂等有测试；接续提示与取消已上界面。**工具自己换话题是唯一受控例外**：`create_topic` / `switch_topic` 整轮跟着走，用户导航仍一步不动 |
| 2 封存与派生分开 | completed | 派生任务表（退避、重启恢复、幂等）；封存不等模型；摘要失败不影响对话；索引失败不再回滚封存 |
| 3 历史关系与路径上下文 | completed | 祖先链（深度/环保护）、路径前提与「仅参考」标注、知识与其他话题实体卡的范围标注、旧数据迁移 16 |
| 4 边界策略与容量 | partial | 确定性规则（容量 / 明确开工 / 短确认 / 同阶段修正）可运行、三档模式（off/shadow/enabled，默认 shadow）、离线评测；**Embedding 语义信号只做到「有模型就观察」**，尚未校准 |
| 5 界面适配 | partial | 接续提示与取消、设置页分段文案与单段长度已完成；生成中改选等状态有实现但视觉重检未全部覆盖 |

**阶段 1 的缺口与修复（2026-09-22 · 工具导航归属）**：阶段 1 把「本轮归属在轮前
固定」落到绑定上时，把「模型自己用 `create_topic` / `switch_topic` 换话题」和
「用户在本轮跑着的时候改导航」一起排除了 —— 于是建话题的那一轮（用户提问 +
助手回答）留在**上一个**话题里：新话题的历史从第二轮才开始，追问看不到前提，
`memory_search` 也搜不到（索引在片段封存时才生成）。现在把两者按来源分开：

- 工具在 turn 派生的 task 里执行，能读到本轮的 `turn_id`；另一个请求（用户导航）
  读不到，所以登记不上 —— 判定不靠 trace（trace 可关闭，靠它会静默退化）。
- 工具换话题登记在 `services/navigation.py`（`note_tool_navigation`），由
  `services/turn_orchestrator.py::persist` 在写回答前落实：先把「本轮自己的工具
  换过话题」当成一次受控改向（`TurnBindingService.rebind_topic_from_tool_nav`，
  只允许 `write_state='open'`），再把用户消息搬进新话题的开放片段
  （`MemoryWriter.move_message`，两个片段的 `start/end_message_id` 都按实际消息
  重算）。两个前提缺一不可：Anchor 仍停在那个话题、本轮绑定还没收尾；任何一条
  不成立都退回原绑定（目标片段由 `get_or_create_open` 懒创建）。
- 用户导航、失败/取消、`continue_from_fragment`（只影响后续提交）行为不变。
- Tests：`backend/tests/test_tool_nav_rebind.py`（工具建话题 / 工具切话题 / 本轮内
  用户导航三条），并同步更新了 `test_turn_short_term.py`、
  `test_p7_message_uniqueness.py` 里原先把旧行为写死的断言。

**向量模型（内置已落地）**：默认内置 **fp32** ONNX（`model.onnx`，约 90MB）。
构建前用 `scripts/models/fetch_model.py` 把模型（含清单、许可证、声明）抓到
`frontend/src-tauri/resources/models/`，由 `bundle.resources` 打进安装包；
桌面壳启动时按内容指纹复制到 `%APPDATA%\qio\models\` 并注入 `QIO_MODELS_DIR`，
失败不阻塞启动（退回 BM25 并在日志写明原因）。向量缓存按「身份 + 维度」过滤，
换档位必须重新编码。清单生成：`scripts/models/write_manifest.py`；
自检与 fp32/int8 对比：`scripts/models/onnx_ab.py`。

**已知限制（不粉饰）**

- 语义阶段切分没有经过校准，默认不实际切分；Embedding 只做观察与降级；
- 进程重启后**排队中的消息**不会自动恢复（队列未持久化）；
- 旧数据里 `relation_type='unknown'` 的片段没有路径隔离能力；
- 打包时需附模型许可证与 NOTICE（上游 BAAI/bge-small-zh-v1.5 为 MIT）。

---

## 本轮变更：工具调用历史（2026-09-24）

> 设计：`docs/superpowers/specs/2026-09-24-tool-record-history-design.md`
> 计划：`docs/superpowers/plans/2026-09-24-tool-record-history.md`

**问题**：工具卡只活在实时事件流里。刷新、重开应用、切话题之后，那一轮"调了哪些工具、
各自成功还是失败、为什么失败"在对话里就没了；而且实时卡片的展开内容来自
`TOOL_END.content_preview`（后端只给 200 字），**实时与历史都看不到完整输出**。

**改法**：新增一张专用表 `tool_records`（迁移 20）+ 一个按 id 取全文的接口，
写入挂在工具的权威终态上，历史与实时两种卡片共用同一条取全文的路径。

| 部分 | 实现 | Tests |
| --- | --- | --- |
| 存储 | 迁移 20 建 `tool_records`（打码后的参数与输出全文、状态、一行错误、耗时、截断与"输出不在库里"的原因）；`storage/tool_records.py` 集中写入 / 查询 / 清理 | `test_tool_records.py` |
| 写入 | `tool/result` 暂存 call 与 result，`tool/end` 拿权威终态后落库（取消与失败要分开）；`AppContext._record_tool_call` 同时写轨迹审计行与历史行，返回记录 id | `test_tool_record_wiring.py`、`test_tool_call_audit.py` |
| 读取 | 历史接口按 `turn_id` 附带 400 字预览；`GET /api/tool-records/{id}` 取参数与输出全文（记录不存在 404，输出被清理时 `output_missing=1`） | `test_tool_record_api.py` |
| 设置 | `GET/PUT /api/settings/tools`：`tools.record_outputs`（默认开）与 `tools.output_retention_days`（默认 90，0 = 永久）；启动、后台维护、保存设置三处清理 | `test_settings_tools_api.py` |
| 前端 | 历史加载把记录按时间插进对应轮次（跨页按 id 去重）；卡片展开时才取全文，显示「参数」与「输出」两段；未保存 / 已清理 / 截断各有文案 | `toolRecordHistory.test.ts`、`MessageItem.test.ts`、`SettingsView.test.ts` |

**保留期口径**：到期只清空**输出全文**（`missing_reason='retention'`），参数、状态、
失败原因、耗时继续保留 —— 三个月后仍能查到"当时哪个工具失败、为什么失败"。

**边界（与用户确认过的产品决定）**：这些记录**只给人看** —— 不进上下文、不进片段摘要、
不进检索索引，模型检索不到；子任务内部的工具调用不入表（与"子任务不进主对话"一致）。

**实拍证据**：隔离实例里写两条真实记录后刷新，历史卡片展开可见参数、
密钥已打码为 `***redacted***`、输出为全文（含第 25 行）；窄窗口（820px）下卡片
横向不溢出。脚本 `scripts/ui-catalog/tool-history.mjs`，图在
`frontend/e2e-shots/ui-catalog/toolhist/`。

**已知限制（不粉饰）**

- 打码是规则匹配（`trace/redact.py`）：规则之外的密钥形态仍可能落库；
  关掉「保存输出全文」是唯一彻底的做法。
- 库会随时间增长。默认 90 天只清输出；参数与错误永远保留（体积很小），
  目前没有"整条记录自动删除"的规则。
- 本功能上线前的历史调用没有记录，不做回填。
- 单条参数与输出上限 4 万字（与工具自身的输出上限对齐），超出会截断并标注。

---

## 本轮变更：无边框窗口与自定义标题条（2026-10-13）

**要解决的问题**：窗口一直带着系统标题栏（那条写着 QIO、带 − □ ✕ 的页眉）。
去掉它之后，窗口既没有可拖拽的区域、也没有最小化/最大化/关闭按钮 —— 这两件事必须由前端补上，
否则用户只能靠 Alt+F4 关窗口。

| 改法 | 实现 | Tests |
| --- | --- | --- |
| 窗口无边框 | `frontend/src-tauri/tauri.conf.json`：窗口 `decorations: false`；`shadow: true` 保留（Windows 上它给无边框窗口 1px 边线与 Win11 圆角） | 「无边框窗口契约」逐条点名配置与权限 |
| 自绘标题条 = 拖拽面 + 三个按钮 | 新增 `frontend/src/components/WindowChrome.vue`：满宽拖拽面（`data-tauri-drag-region`，双击由 Tauri 内置逻辑转成最大化/还原）+ 右侧最小化/最大化/关闭；按钮只在壳里渲染（浏览器预览里画了也点不动），最大化状态跟着系统的 resize 事件走 | `frontend/src/components/__tests__/WindowChrome.test.ts` |
| 按钮真的调得动壳 | `frontend/src-tauri/capabilities/default.json` 补 `core:window:allow-start-dragging` / `allow-minimize` / `allow-toggle-maximize` / `allow-close` / `allow-is-maximized`：少一个按钮就静默失灵（调用被权限挡掉），所以契约测试逐个点名 | `frontend/src/design-guards.test.ts` |
| 顶部让出条高：贴顶控件不许被压住 | 高度只在 `frontend/src/styles/tokens.css` 定义一次（`--titlebar-height`），JS 侧经 `frontend/src/utils/titlebar.ts` 读同一个值；`App.vue` 用 `padding-top` 让出这一段；原本贴顶的控件（设置齿轮、待确认入口、设置页 toast、星球页「收起星球」与两条顶部状态条）各下移一个条高 | `frontend/src/utils/__tests__/titlebar.test.ts`、frontend/src/components/__tests__/SettingsFloat.test.ts（2026-10-26 已删除，设置入口由 WindowChrome 全局栏承接） |

**实拍与命中证据**（`frontend/scripts/window_chrome_shots.mjs`，图与结果在 `frontend/e2e-shots/window-chrome/`）：
脚本自己判红绿，不靠人读图 —— 标题条 32px、整条除按钮外都是拖拽面、页面内容从 y=32 起、
三个按钮打到壳命令 `plugin:window|minimize` / `toggle_maximize` / `close`、点齿轮能进设置页、
星球页「收起星球」可命中。

**真机核对（Windows，无边框这件事只能在真窗口上量）**：窗口矩形 1216×809，客户区 1200×800 @ 偏移 (8,1) ——
非客户区只剩缩放宽边，**顶部没有标题栏那一条**（带系统标题栏时顶部非客户区是 20px 以上）。
注意 `WS_CAPTION` 样式位仍然保留：tao 靠它保住拖边缩放与系统贴靠，标题栏是靠收掉非客户区实现的，
所以**别拿样式位判断有没有页眉**，要看客户区与窗口矩形的差。

**已知限制（不粉饰）**

- Win11 的「贴靠布局」面板（鼠标悬停在最大化按钮上弹出的那个）没有：它由系统画在原生最大化按钮上，
  自绘按钮拿不到。Win+方向键与拖到屏幕边缘是否仍然贴靠，本轮没有实测。
- 标题条上没有原生标题栏右键那份系统菜单；标题文字也不再由系统显示（条里刻意不放文字）。
- 全屏模态（审批 / 凭据弹窗）之上仍然保留标题条：这是故意的 —— 模态开着也应该能拖窗口、能关窗口。
- **真机核对到哪一步**（Windows 11，1600×852 工作区）：最小化按钮 → 系统报告窗口已最小化且能还原；
  最大化 → 客户区 1600×852 正好等于工作区，按钮自己变成「向下还原」，再点回到 1216×809；
  关闭 → 进程退出。**拖拽只核到结构**（真窗口里 `data-tauri-drag-region` 在位、权限与上面三个同批生效），
  没有再用系统级合成鼠标去拖窗口 —— 本机前台被别的应用占着，合成点击会落到那个应用上（实测踩过一次）。

---

## 本轮变更：启动加固（2026-09-25）

**问题（真机事故）**：0.1.8 首次启动时窗口一片空白，旁边弹出 `reg.exe` 的
「应用程序无法正常启动(0xc0000142)」。核对证据：外壳 15:05:41 启动，日志里第一条记录
出现在 15:07:23（**启动被拖住 1 分 42 秒**），后端 15:07:23 才被拉起；同一错误码
（`ExitStatus(3221225794)`）也出现在外壳更新前调用的 `taskkill.exe` 上。也就是说：
白窗是"启动被拖住"的等待状态，而拖住它的是**系统级**的子进程初始化失败
（`reg.exe` 报错后会留一个必须先点掉的模态框，外壳在等它退出）。

| 改法 | 实现 | Tests |
| --- | --- | --- |
| 跑系统命令带**硬超时**：超时杀子进程（模态框随之关闭）并按"拿不到"处理 | `frontend/src-tauri/src/main.rs` 新增 `run_command_with_timeout`；`reg query`（读系统代理）3 秒、`taskkill`（更新前结束后端）5 秒 | `hanging_command_is_killed_after_the_timeout`、`fast_command_still_returns_its_output` |
| 探测失败即直连，不再等待 | `system_proxy()` 拿不到就返回 None → 更新按直连走（原本就有直连兜底），并写一条 WARN | 同上（超时返回 None 就是这条路径） |
| 启动耗时进日志，下次能直接归因 | setup 里记录并打印「代理探测 / 内置模型 / 拉起后端 / 合计」四段耗时 | 随应用启动实测 |
| 后端没应答时**不再白屏** | 新增 `services/boot.ts::waitForBackend`（解析连接信息 + 轮询 `/api/health`，单次探测带超时）；`App.vue` 增加启动状态：等待时显示「正在启动 QIO 后端…（已等 N 秒）」，超过 60 秒显示原因、日志位置与「重试」，后端应答后才渲染主界面并建立事件流 | `frontend/src/__tests__/appBoot.test.ts`、`frontend/src/services/__tests__/boot.test.ts` |

**实拍证据**（`scripts/ui-catalog/boot-gate.mjs`，图在 `frontend/e2e-shots/ui-catalog/bootgate/`）：

- 后端故意不起来：界面显示「正在启动 QIO 后端… 已等 N 秒」；
- 等满 60 秒：「QIO 后端没有应答（已经等了 60 秒）／后端没有应答（最后一条错误：Failed to fetch）／
  也可能只是启动很慢：点「重试」会接着等。日志：…QIO.log／重试」；
- 后端起来后：同一套代码自己渲染出主界面（不靠刷新）。

**已知限制（不粉饰）**

- `0xC0000142` 本身是系统级故障（同一台机器上 `reg.exe`、`taskkill.exe` 都中过），
  应用只能做到"不因为它卡死"，不能修好本机环境。
- 代理探测仍在启动路径上（它决定后端继承的代理环境变量），现在最坏 3 秒/次；
  启动日志会如实写出这一段花了多久。
- 启动等待的默认上限是 60 秒：超过就显示可重试的失败说明（后端只是很慢时，重试会接着等）。

---

## 本轮变更：工具失败与静默失败加固（2026-09-24）

> 规格：`docs/superpowers/specs/2026-09-24-tool-failure-hardening-spec.md`
> 计划：`docs/superpowers/plans/2026-09-24-tool-failure-hardening.md`

起因是一次真实安装实例的运行数据核对：工具调用失败里，一批是设备自身没准备好
（默认工作区目录不存在、开发工作区重启后不认），一批是等待方式不对（审批窗口被
工具超时截断、审批结局只有一句「未获批准」），还有一批是原因说不清（抓取把脚本
渲染的页面报成登录墙、底层异常没有文本时只剩半句）。另有两轮以**空白回答**结束，
用户在界面上看不到任何解释。逐条修法如下。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 默认工作区根目录不存在 → 相对路径的文件工具全报路径错误 | `ensure_dirs` 一起建工作区目录；沙箱新增 `ensure_root()`，启动与保存设置时各建一次（建不出来只记日志，工具照实报错） | `config.py`、`services/computer.py`、`services/app.py`、`api/server.py` | `test_workspace_root.py` |
| 应用重启后 `dev_*` 报「找不到工作区」 | `DevWorkspace` 构造时扫盘回填 `ws_*` 工作区（需求正文从 `request.md` 读回） | `tools/dev_workspace.py` | `test_dev_tools.py` |
| `run_shell` 的审批窗口被 45 秒工具超时截断 | 工具级超时改为「审批窗口 + 执行窗口」；超时/拒绝/取消用 `refusal_reason` 分别表述 | `tools/cmd_tools.py`、`tools/fs_tools.py`、`tools/approval.py`、`tools/registry.py` | `test_cmd_tools.py`、`test_fs_tools.py` |
| 抓取把脚本渲染的页面报成「需要登录或验证」；异常无文本时错误只有半句 | 登录墙只认明确短语；新增「正文由 JavaScript 生成」这一类；连接失败带地址与原因；异常兜底补 cause 说明 | `tools/web_fetch.py`、`tools/registry.py` | `test_web_fetch_tool.py`、`test_tool_pipeline.py` |
| 护栏终止 / 预算停止后回答是空串 | 循环结束前若没有任何最终文本，写出一句说明（哪个工具、失败几次、最后一次原因）；取消轮不补 | `core/loop.py` | `test_loop_continue.py` |
| 失败原因不在轨迹表里 | 迁移 19 给 `tool_calls` 加 `error` 列；循环把失败原因交给审计回调 | `storage/schema.py`、`services/app.py`、`core/loop.py` | `test_tool_call_audit.py` |
| 工具卡把真正的原因折叠成笼统文案 | 失败结论上限从 60 字放到 120 字，超出才截断并提示展开 | `frontend/src/components/MessageItem.vue` | `MessageItem.test.ts` |

**已知限制（不粉饰）**

- 外部站点仍然会抓不到：站点下架、反爬、必须浏览器渲染的页面依旧失败，改动只让
  原因准确。
- 工具调用历史受打码与保留期约束（见文末「工具调用历史」一节）：本功能上线前的
  历史没有记录，不做回填。
- 用户主动取消的轮次不补收尾文本（界面已有「已停止」状态行）。
- `import agent.tools.*` 必须在 `agent.core` 之后：单独导入会触发既有的循环导入
  错误，因此只有 `agent.tools.*` 作为首个模块的单文件测试无法单独收集（整目录运行
  不受影响）。本轮未改这条链路。

---

## 本轮变更：确认之前不外发已存的 Key 与摘要超限（2026-09-29）

> 计划：`docs/superpowers/plans/2026-09-29-credential-key-leak-and-summary-limit.md`

起因是 2026-09-29 的代码评审。两条高危问题的共同形态是「自动动作跑在用户确认之前」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 编辑已有凭据时改了服务地址，界面会自动去新地址取模型列表 —— 用户还没点「保存」、也还没勾「确认发送到新地址」，**原来保存的 Key 已经被发到新地址** | 已存的 Key 只回答**它自己那个地址**：地址/协议变了又没有新 Key 时根本不取列表，提示保存后再取 | `frontend/src/services/credentials.ts` | `CredentialForm.test.ts`（改地址后一次都不发 / 地址没变仍然用 `keyId`） |
| 同一件事的另一半：`GET /api/credentials/models` 只要拿到 `key_id` 就解密并向任意 `endpoint` 发请求，前端漏改就直接变成外发 | 请求地址与这条凭据落库地址不一致（忽略大小写与结尾斜杠）时不解密、不外发，返回空列表加一句说明 | `backend/src/agent/api/server.py` | `test_credential_endpoint_api.py` |
| 摘要的实体/关键词超过上限（实测出现过 79 项）被判成 schema 违规 → 整个派生任务失败：这个片段没有摘要、没有检索记录、没有实体卡、没有知识条目 | 本地先「去空白 + 去重 + 保序 + 截断」再交给 pydantic 校验；上限同时写进提示词；校验只负责挡真正的结构错误 | `backend/src/agent/memory/summary.py` | `test_memory.py`、`test_fault_injection.py`（超出上限时摘要与索引照常落库） |

**已知限制（不粉饰）**

- 两条都只做到「源码 + 自动化测试」：**没有用真实 Key 实测**，改地址的完整点击路径也没有真机走查。
- 摘要的同类风险没有一次收完：`title` / `summary` 文本超长、知识提炼的 `candidates` 超上限仍会让那一项失败
  （文本超长会丢掉整条摘要，候选超上限只丢知识条目）。本轮只改了实体与关键词。
- 「已存的 Key 只能问它自己那个地址」是精确匹配（去尾斜杠、忽略大小写）：给同一个地址补一段路径也会被
  当成换了目标，需要保存之后才重新取候选模型列表。

---

## 本轮变更：Windows 检查、用量累计与「停止」真取消（2026-09-29）

接上一节的评审，处理剩下的三条：Windows 路径没有自动检查、用量上限不累计、按停止不掐请求。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 发布平台是 Windows，但 CI 只有 Linux；壳的单元测试（含 `#[cfg(windows)]` 的启动超时用例）**在哪都没跑过** | CI 新增 `backend-windows`（同一套后端测试）与 `rust-windows`（`cargo test`，不再只 `cargo check`）；两条命令同步进 SETUP | `.github/workflows/ci.yml`、`docs/SETUP.md` | 本机等价命令实跑：`cargo test` 八个用例全过（含 `hanging_command_is_killed_after_the_timeout`） |
| 「用量上限」从来不随真实调用累计（`record_usage` 只有测试调用），已用量永远停在 0，上限也就不可能生效 | 建 adapter 时记住自己的 `key_id`；每次模型调用把「进 / 出」报给归因 sink 并写进这条凭据；主循环、子 agent、后台维护共用同一份归因 | 迁移 22（`usage_input` / `usage_output`）、`credentials/store.py`、`credentials/usage.py`、`core/loop.py`、`services/app.py` | `test_usage_accounting.py`、`test_credentials.py`、`CredentialCard.test.ts`（进 / 出 / 合计与上限比较） |
| 按「停止」只是不再用结果，已经发出的模型请求照跑 | 请求放进自己的 task，与取消事件竞速；取消时立刻取消它（连接随之中断），turn 仍按 `cancelled` 语义收尾 | `core/loop.py`（`_await_completion`、`cancel`） | `test_cancel_and_rapid_turns.py`（请求被真正中断、排队消息不再被堵住） |
| 附带修掉的既有缺陷：通知在「最后一次 planning 之后」到达时会被**静默丢掉** | 这一轮的 loop 把没读到的通知交还给上层，由它变成自己的一轮 | `core/loop.py`（`TurnResult.unread_notices`）、`services/turn_orchestrator.py` | `test_p7_subagent_race.py`（迟到的子任务结果仍成为独立一轮） |

**已知限制（不粉饰）**

- 新增的两个 CI 任务**还没在 GitHub 上真跑过**（本机等价命令通过）：Windows runner 是全新环境，
  第一次跑可能要按它的情况再调一轮（占位文件命名、个别依赖本机状态的用例）。
- 计量口径是「进 + 出」合计：注入的 system prompt / 记忆上下文都算在「进」里，所以进度条会比直觉更快接近上限。
  换上限＝三个数字一起归零（重新计数）。
- 取消掐的是客户端等待；服务端是否立刻停止生成、停止计费，由供应商决定。
- 用量只在主循环 / 子 agent / 后台维护三条路径归因；其它直接调用适配器的实验代码不计入。
- 第 6 条（真实界面与 Planet 手感验收）**没有做**：那需要真机人工验收，自动化替代不了。

---

## 本轮变更：工具开发规范与可靠性修复 · 第一阶段（2026-09-29）

计划：`docs/superpowers/plans/2026-09-29-tool-dev-spec-phase1.md`。目标不是改提示词，而是让「失败被如实、结构化地反馈」以及「开发任务有权威状态」进入代码。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 工具失败且 `content` 为空时，模型收到空正文，随后声称成功也被接受 | 公共执行层新增唯一的结果→模型消息构造点：失败至少带状态、错误类别、简短原因、是否可重试、`call_id` 与脱敏限长的诊断；成功且正文非空时保持原样 | `core/tool_feedback.py`、`core/loop.py`、`tools/base.py`（`ToolResult.category/recoverable`） | `test_tool_feedback.py`（含主循环集成：失败且空正文仍回填事实） |
| 沙箱捕获的 `stderr` 没传到上层，模型只看到「exit code 1」 | `SandboxResult` 增加错误类别与 `diagnostic()`（stderr 优先、脱敏限长）；tester 与 CodeTool 把诊断一并交给模型 | `tools/sandbox.py`、`tools/tester.py`、`tools/runtime_tools.py` | `test_tool_feedback.py`（已知异常 / 缺依赖两类 stderr 与类别） |
| 子进程用 `sys.executable -c`，冻结后指向后端 exe（入口只启动服务） | 改为统一执行协议：开发态 `python tool_worker.py`、正式态 `后端 exe --tool-worker`（见下一节）。本轮先做「解析出可执行命令 + 明确报环境问题」，旧接口随后被 worker 取代 | `tools/executor_env.py`、`tools/sandbox.py` | `test_executor_env.py` |
| `dev_submit_tool` 要求模型再传完整 `definition`，容易与工作区文件/测试对象不一致 | 提交以工作区 `tool.json` 为唯一权威：省略 `definition` 即可提交；传了则必须与工作区一致，否则拒绝；提交时绑定工作区内容摘要 | `tools/dev_tools.py`、`prompts.py` | `test_dev_tools.py`（工作区为准的提交路径 + 不一致拒绝） |
| 开发任务只有内存态，缺少可枚举的权威状态 | `DevWorkspace` 增加 `state.json` 落盘（阶段 / 测试通过与否 / 摘要 / 次数 / 提交摘要）、`list_tasks()`、`status()`、`content_digest()`；新增 `dev_list_tasks` 工具 | `tools/dev_workspace.py`、`tools/dev_tools.py`、`tools/display.py`、`services/app.py` | `test_dev_tools.py`（重启回填、删 `state.json` 后标未知、摘要随内容变化、任务枚举） |

**已验证**：后端全量 `uv run --frozen pytest`（通过），新增用例覆盖上表每一行；`scripts/check_docs.py` 通过。

**仍未验证 / 留到后续阶段（NOT RUN，不宣称完成）**

- **冻结产物的真实执行链**：已由下一节「工具 worker 模式」补齐（后端 exe 自带 `--tool-worker`），本节的这项缺口作废。
- **开发规范落地为单一权威、分节注入**：本阶段只更新了 `DEV_GUIDE` / 工具描述文案；尚未建立结构化的规范模块与按步骤注入。仍属 planned。
- **模型谎报成功时的状态校正**：只修了「失败被如实反馈」，尚未实现「最终答复涉及验证/可用结论时按后端状态校验并纠正」。仍属 planned。
- **所有已保存对话的原文检索**：已补齐（见文末「已保存对话的原文检索」）；原判「未实现」作废。
- **前端界面**：任务卡、未完成任务提示、恢复入口都没动。未验证。

---

## 本轮变更：工具 worker 模式（2026-09-29）

目标：让冻结产物在没有系统 Python、没有 Docker 的 Windows 上也能跑生成工具，同时不把后端主进程当作执行器、不把独立子进程说成安全沙箱。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 冻结后 `sys.executable` 是后端 exe，入口只启动服务，工具子进程测试与运行必然失败 | 后端 exe 增加 `--tool-worker` 模式；`agent/main.py` 顶层只留标准库，`main()` 第一件事按 argv 分流，worker 分支在加载 uvicorn / FastAPI / 数据库 / 凭据**之前**返回 | `src/agent/main.py`、`src/agent/tool_worker.py` | `test_entrypoint_split.py`（真子进程：import 入口不加载服务栈；worker 模式不建 app.db） |
| 开发与正式运行的执行协议不一致（`-c` 拼脚本 vs 冻结 exe） | 统一的 worker 协议：stdin 传结构化 JSON 请求，stdout 只回一行 JSON 结果；工具自身的打印与异常栈收进结果的 `stdout`/`stderr` 字段 | `src/agent/tool_worker.py`、`src/agent/tools/executor_env.py`、`src/agent/tools/sandbox.py` | `test_tool_worker.py`（真子进程跑协议：成功/异常类型/输出捕获/非法结果/畸形请求）、`test_sandbox_worker.py` |
| 超时只 `kill()` 直接子进程，工具自己起的子进程会残留；按名称清理会误伤别的进程 | 新增 `_kill_process_tree`：Windows 用 `taskkill /PID <pid> /T /F`（按 PID 遍历子树），POSIX 用独立进程组；取消路径同样清理后再传递取消语义 | `src/agent/tools/sandbox.py` | `test_sandbox_worker.py`（超时/取消各清理一次且 pid 正确；Windows 断言命令行是 `/PID` 而非镜像名） |

**已验证（本机实跑）**

- 后端全量 `uv run --frozen pytest`（退出码 0），含上表所有新用例。
- **真实冻结产物**：用 `uv run --with pyinstaller` 按 `scripts/build_sidecar.ps1` 的参数打包出 `qio-backend.exe`，然后：
  - `qio-backend.exe --tool-worker`（stdin 传请求、PATH 里已剔除所有真实 Python，只剩 Microsoft Store 的 `python.exe` 占位符）返回 `{"ok": true, "value": {"sum": 42}}`，退出码 0，**没有**创建数据目录与 `app.db`（证明 worker 模式不连库、不跑迁移）。
  - 同一 exe 正常启动：创建 `app.db`、HTTP 端口应答（`/api/tools` 返回 404 = 服务在听）、按 PID 停止成功（证明正常启动路径没有退化）。

**仍未验证 / 未实现（如实标注）**

- **完整安装包（`tauri build` 产物）的人工安装验收**：NOT RUN。上面跑的是 PyInstaller 冻结 exe，不是装完的安装包。
- ~~**「没有 Docker」的对照实验**：NOT RUN。~~ **已由 2026-10-02 一节作废**（用进程边界桩复现了「守护进程可用 → auto 选容器 → 注入被覆盖」这条路径）。
- ~~**Docker 执行器的协议统一**：Docker 分支仍用容器内 `python -c` 的旧协议。~~ **已由 2026-10-02 一节作废**：容器路径现在跑同一份 worker 源码与同一个 `_parse_worker_result`。
- ~~**额外依赖的项目级隔离**：未实现。~~ **已由 2026-10-02 一节作废**：专用环境 + 锁定清单 + 清理入口已实现；容器执行路径下的依赖准备有准备侧实现、执行侧待 ubuntu CI 验证。
- 「测试也执行实际权限检查」这条只覆盖了既有 `policy` 能力分级与审批路径，**测试数据隔离**（临时库/临时目录/模拟服务）没有在本轮补。

> 上面两条与 worker 协议可信度、输出保护相关的缺口，已由下一节「第一阶段收尾」补齐；安装包人工验收仍是 NOT RUN。

---

## 本轮变更：第一阶段收尾 · 可信状态与执行协议（2026-09-30）

计划：`docs/superpowers/plans/2026-09-30-tool-dev-spec-phase1-completion.md`。范围是复核报告里指出的五个会**掩盖真实状态**的缺陷：测试证据不绑定版本、`state.json` 可被伪造、提交后项目被删、注册与落库失败不回滚、worker 的假成功被采信。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 测试通过后把代码改成错的，任务列表仍显示「测试通过」 | 测试证据绑定被测内容的 sha256 摘要；内容一变（含越权直接改磁盘）立即标为 `stale`，任务列表照实说「证据失效，需重跑」 | `tools/dev_workspace.py`、`tools/dev_tools.py` | `test_dev_tools.py`（内容变更即失效、重启后仍失效、任务列表不谎报） |
| agent 能用 `dev_write_file` 写 `state.json` 伪造「测试通过 / 99 次测试」 | `state.json` 与 `request.md` 列为后端保留名，文件工具写入即拒绝；`state.json` 带 `schema` 与 `source`，恢复时必须对上才认，否则按「未知」处理 | `tools/dev_workspace.py` | `test_dev_tools.py`（伪造被拒、无 schema 的 state 不被采信） |
| 提交成功后工作区被删除，工具注册了但任务再也枚举不到 | 完成后保留项目：标记已提交 + 复制一份不可变快照到 `dev-workspaces/archive/<id>/` | `tools/dev_workspace.py`、`tools/dev_tools.py` | `test_dev_tools.py`、`test_dev_workflow_integration.py` |
| 提交时的交叉复测失败，持久状态仍是「测试通过」 | 复测结论经 `test_sink` 在**任何对外事件之前**写回同一个任务 | `tools/lifecycle.py`、`tools/dev_tools.py` | `test_dev_tools.py`（复测写回同一任务） |
| `ToolStore.save` 抛磁盘错误时，提交报失败但工具已在内存注册表里可调用 | 改为「先持久化、后注册」；失败回滚：撤销本次注册 + 写回上一可用版本 | `tools/lifecycle.py`、`storage/tool_store.py`（新增 `load`） | `test_tool_lifecycle.py`（落库失败不留注册、注册失败恢复上一版本） |
| worker 直接吐一行形似成功的结果再以退出码 17 退出，沙箱仍报 `ok=True` | 成功必须同时满足：退出码 0、结果通道恰好一行 JSON、`ok` 是布尔、成功时 `value` 是对象 | `tools/sandbox.py` | `test_sandbox_worker.py`（伪造成功 / 多余行 / 非布尔 ok / 非对象 value 全部拒绝） |
| Windows 上工具子进程环境缺 `SystemRoot` 等系统变量 | 环境改为显式白名单（`SystemRoot`、`SystemDrive`、`WINDIR`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`、`TMPDIR`、`PATH`、`LANG`），白名单外一律不传 | `tools/sandbox.py` | `test_sandbox_worker.py`（系统变量可见、非白名单变量不可见） |
| 输出限制发生得太晚：worker 先全写进 StringIO，父进程再 `communicate` 全部读回 | 两侧都加界：worker 端捕获缓冲封顶并标注截断，父进程边读边计数（stdout 2 MiB / stderr 512 KiB），超限即终止进程树并如实报错 | `tool_worker.py`、`tools/sandbox.py` | `test_sandbox_worker.py`（工具狂打印仍可用且标注截断；坏 worker 无限输出被截断） |

**已验证（本机 Windows，源码运行）**：受影响的工具 / 沙箱 / 开发工作区相关测试文件全绿（`tests/test_tool_feedback.py`、`test_tool_worker.py`、`test_sandbox_worker.py`、`test_entrypoint_split.py`、`test_executor_env.py`、`test_dev_tools.py`、`test_tool_lifecycle.py`，以及 `test_tool_store.py`、`test_tool_restore.py`、`test_tool_recovery.py`、`test_tool_registry_reversible.py`、`test_tool_policy.py`、`test_tool_pipeline.py`、`test_active_tools.py`）；`python scripts/check_docs.py` 通过。

**仍未实现 / 仍是 NOT RUN（不宣称完成）**

- **模型谎报成功时的最终结论校正**：后端部分已实现（见文末「最终结论的事实校正」一节）；前端「已核对」标记仍未做。
- **测试前授权与真实能力策略**：`dev_run_tests` 仍在审批之前就执行生成代码；测试数据隔离（临时库 / 临时目录 / 模拟服务）没有补。
- **所有已保存对话的原文检索**：已补齐（见文末「已保存对话的原文检索」）。
- **开发规范分节注入**：仍是单一长文 `DEV_GUIDE`，没有按当前步骤注入。
- **多文件项目与项目级依赖隔离**：未实现，文件工具仍只接受单层文件名。
- **前端**：未完成任务入口已由 2026-09-30 的「未完成任务入口 · 前端」一节补齐；
  独立任务的运行卡片沿用既有实现，本轮没动。
- **真实冻结产物与安装包验收**：本轮只跑源码测试，未重跑 PyInstaller 冻结 exe，也未做安装包人工验收 —— NOT RUN。

---

## 本轮变更：最终结论的事实校正（2026-09-30）

规格：`docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md`；计划：`docs/superpowers/plans/2026-09-30-final-answer-fact-check.md`。

要解决的问题：工具失败的结果**已经**如实回填给模型（`core/tool_feedback.py`），模型随后仍然说
「测试全部通过，已提交审批」，而这句话照样成为最终答复 —— 因为「本轮到底做了什么」没有任何
地方记得住，最终答复也不经过任何核对。明确不采用的做法是「扫关键词改话」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 本轮的失败与开发任务状态没有任何地方记得住 | 新增每轮事实台账（工具终态 + 开发工具上报的任务状态）；`ToolResult` 增加机器可读的 `facts` 通道，开发类工具在**操作之后**上报「任务 / 版本摘要 / 测试证据 / 是否提交」 | `core/turn_facts.py`、`tools/base.py`、`tools/dev_tools.py`、`tools/dev_workspace.py` | `test_turn_facts.py`、`test_dev_tools.py` |
| 「测试通过 / 已注册 / 可以使用」可以被模型自己宣布 | 新增只读工具 `declare_completion`：按工作区状态、注册表与持久层逐条核对 `test_passed` / `registered` / `usable`（版本必须等于当前内容摘要，`subagent` 型不要求测试证据）；对不上就逐条说清缺什么 | `tools/declare_completion.py`、`services/app.py`、`prompts.py`、`tools/display.py` | `test_declare_completion.py`、`test_dev_workflow_integration.py` |
| 最终答复不经过任何核对 | 主循环收尾：本轮有未解决失败（工具最后一次结局是失败、任务测试失败/证据失效/还没有测试证据）且没有被接受的声明时，在答复**末尾追加**一段后端事实说明。不重写模型正文；取消不算失败；先脱敏再限长；文案不含花括号 | `core/loop.py` | `test_final_answer_fact_check.py` |

**已验证**：本机 Windows 源码运行，`tests/test_turn_facts.py`、`test_dev_tools.py`、`test_declare_completion.py`、`test_final_answer_fact_check.py`、
`test_tool_feedback.py`、`test_loop.py`、`test_dev_workflow_integration.py`、`test_app_integration.py`、`test_settings_tools_api.py` 全绿；后端全量测试见本次提交说明。

**仍未做（不宣称完成）**

- **前端「已核对」标记**：已由下一节补齐（复用 `messages.raw`，未加迁移）。
- **声明面只覆盖开发类结论**：网页 / 桌面操作类结论要核对时，沿用同一张台账与同一套 claim 协议扩展，本次不做。

---

## 本轮变更：结论标记与「已创建」文案（2026-09-30）

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 「已核对」这件事在后端没有留痕，用户看不到哪句话是后端核对过的 | 被接受的 `declare_completion` 结论随那条 assistant 消息落库（复用已有的 `messages.raw`，**不加迁移**），并随 `TURN_END` 发给前端；界面在回答里显示一行「后端已核对：版本 … ；测试 … 」 | `core/turn_facts.py`、`core/loop.py`、`core/turn.py`、`services/turn_orchestrator.py`、`services/app.py`、`frontend/src/stores/session.ts`、`frontend/src/stores/events.ts`、`frontend/src/components/MessageItem.vue` | 后端 `test_turn_facts.py`、`test_final_answer_fact_check.py`；前端 `finalAnswer.test.ts`、`MessageItem.test.ts` |
| 「已创建」卡片写死「现在可以使用」，把「注册成功」说成「验证过」 | 文案按实际拿到的证据写：「已注册，可以调用（1/1 tests passed）」；引用凭据的工具再加一句「真实服务未验证（测试不注入凭据）」 | `tools/dev_tools.py`（`ready_detail`）、`tools/lifecycle.py` | `test_dev_tools.py`、`test_tool_create_events.py` |

**已验证**：后端全量 `pytest` 退出码 0；前端 `npx vitest run` 全绿、`npx vue-tsc --noEmit` 通过；`scripts/check_docs.py` 通过。

**仍未做（不宣称完成）**

- **真机界面验收**：只有组件级渲染测试（`.verified-note` 存在/不存在），没有在真实应用里看着这条标记出现。
- **未完成任务入口与审批跨重启语义**：已补齐（见文末「审批跨重启」与「未完成任务入口 · 前端」两节）。

---

## 本轮变更：审批跨重启（2026-09-30，B 批前半）

语义（已定）：**重启后不恢复等待** —— 等待中的那次工具调用随进程一起没了，恢复一个「等你回答」的授权是假的。要做的是把它变成一条明确记录：「那一次操作没有执行」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 等待中的审批只活在进程内存里，重启后既不会执行、也没人告诉用户「那件事没做」 | 新增迁移 23 的 `pending_approvals`：每次 `ApprovalService.request` 落一行；应答 / 超时 / 取消时收口；**构造新实例时**把仍是 `pending` 的行标成 `interrupted`（新进程不可能有自己的等待项，此刻还 pending 的都是上一个进程留下的） | `storage/schema.py`（迁移 23）、`tools/approval.py` | `test_approval_persistence.py` |
| 「那次没执行」只在库里，界面看不到 | `/api/runtime/state` 增加 `interrupted_approvals`（只有 kind / 描述 / 时间 / `outcome: not_executed`，不回传完整载荷） | `api/server.py` | `test_approval_persistence.py`（装配级：真装配后能读出来） |
| 应用装配没把连接交给审批服务，记录永远是空的 | `AppContext` 用 `ApprovalService(bus, conn=conn)`；没有连接时（老装配 / 单测）行为与以前完全一致 | `services/app.py` | `test_approval_persistence.py` |

**已验证**：涉及审批、API、库不变量的测试文件全绿；后端全量测试见提交说明。

**仍未做**：前端还没显示这条记录，也还没有「未完成任务」入口 —— 那是 B 批的后半，跟着一起做。

**B 批后半（界面，2026-09-30 同日）**

- 前端在 RESYNC 时读 `/api/runtime/state` 的 `interrupted_approvals`，在审批入口上方显示一句事实：
  「上次有一项操作没有执行：…」（多项时写「上次有 N 项操作没有执行（例如 …）」）。
  它**不是待办、也点不动** —— 那次调用随进程没了，恢复一个「等你回答」的授权是假的。
- 实现：`frontend/src/stores/session.ts`（`interruptedOperations` + RESYNC 时填充）、
  frontend/src/components/ApprovalEntry.vue（2026-10-26 已删除，非聊天路由入口由 NonChatApprovalEntry 承接）（只读的一行）、`frontend/src/services/api.ts`（类型）。
- 已验证：前端 `npx vitest run` 全绿、`npx vue-tsc --noEmit` 通过。
- 下一步：**「未完成任务」入口**（把开发任务列表带进界面）——已由紧接着的这一节做完。
- 仍未做：`ApprovalEntry` 那一行只有 store 级测试，没有组件级渲染测试；真机界面验收仍未做。

**未完成任务入口 · 后端（2026-09-30 同日）**

- 新增 `GET /api/dev/tasks`：返回开发任务的权威状态（id / 需求 / 阶段 / 是否已提交 /
  测试结论 / **证据是否还对应当前内容** / 更新时间）。状态来自工作区本身，刷新、重启、
  断线之后都查得到 —— 以前任务只存在于工具调用里，模型不说，界面就再也找不到它。
- 实现：`api/server.py`；测试 `tests/test_dev_tasks_api.py`。注意：这些用例显式清掉
  `QIO_DATA_DIR`，否则本机开发用的真实数据目录会漏进来（与 `test_workspace_root.py`
  那条既有环境敏感用例同源）。

**未完成任务入口 · 前端（2026-09-30 同日）**

- 对话页顶部多一行浅色文字「有 N 个工具开发任务没做完」（`N` 是真实条数，一条都没有就
  不出现）。它不自动弹窗、不抢焦点；点开是一份**只读**清单：需求、阶段、测试结论、
  更新时间，每条一个「继续开发」——把那句话（含任务 id 与当前状态）交给模型接着做，
  而不是从头重写。
- **测试结论按证据说，不按记忆说**：结论对应**当前**文件内容时写「测试通过」；通过之后
  又改过文件就写「测试通过，但文件后来改过，结论不算数」；从没跑过写「还没跑过测试」。
  这正是后端 `test_evidence_current` 的用途。
- 「未完成」只有一个判据：任务**还没提交**（提交成功 = 工具已注册，界面不再把它当待办）。
  界面不推断阶段、不写本地缓存；数据只来自 `GET /api/dev/tasks`。
- 刷新时机：连接建立、RESYNC、以及**每一轮结束之后**各拉一次 —— 一轮里刚创建或刚提交的
  改动不用刷新页面就能看到。
- 顺带修掉一处真实重叠：顶部原来有三个各自 `position: fixed` 的提示（审批入口 /
  「上次那项操作没有执行」/ 新增的这一行），同时出现时会互相盖住；现在由 `App.vue` 的
  `.top-notes` 容器统一堆叠，容器本身不接收点击。
- 实现：`frontend/src/components/DevTaskEntry.vue`、`frontend/src/stores/session.ts`
  （`devTasks` / `unfinishedDevTasks` / `refreshDevTasks`）、`frontend/src/stores/events.ts`
  （三处刷新时机）、`frontend/src/services/api.ts`（`getDevTasks` 与 `DevTaskRow`）、
  frontend/src/components/ApprovalEntry.vue（2026-10-26 已删除，非聊天路由入口由 NonChatApprovalEntry 承接） 与 `frontend/src/App.vue`（顶部容器）。
- 测试：`frontend/src/components/__tests__/DevTaskEntry.test.ts`（没有任务不显示 / 有任务只亮
  一行 / 已提交不算未完成 / 点开才展开且说清证据过期 / 「继续开发」把 id 与状态交给模型 /
  交不出去时如实说明且不收起）、`frontend/src/stores/__tests__/devTasks.test.ts`（取数、
  已提交不算未完成、拉不到时保留旧值、TURN_END 与 RESYNC 都刷新）。
- 已验证：`npx vitest run` 全绿、`npx vue-tsc --noEmit` 通过。
- 仍未做：**真机界面验收**（没有在真实应用里点过这一行）；打开清单的**无障碍细节**（例如
  Esc 收起、焦点回到那一行）没做。

---

## 本轮变更：测试前授权（2026-09-30）

补的是第一阶段里那条顺序错误：`dev_run_tests` 会把模型刚写出来的代码**真的跑起来**，
而以前这条路径没有任何授权 —— 审批只发生在注册之后，等于「先执行、后确认」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 执行 AI 生成代码之前没有任何授权 | 新增执行前闸门：第一次在某个任务上执行生成代码之前必须拿到用户确认；确认按（能力策略指纹 + 实际执行环境）记在工作区状态里，同一任务之后不再重复打扰 | `tools/dev_auth.py`、`tools/dev_workspace.py` | `test_dev_test_authorization.py`（首次询问、二次放行、换环境/换策略重新确认、重启后仍有效） |
| 拒绝 / 超时之后仍可能被执行 | 拒绝 / 超时 / 拿不到审批服务 → **不执行**，返回类别为 `permission` 的失败；测试记录也不会留下 | `tools/dev_auth.py`、`tools/dev_tools.py` | 同上（断言沙箱执行次数为 0） |
| 审批说明可能说不准「到底在哪儿执行」 | 说明用运行路径同一份策略（`policy.default_policy_for`），写明执行环境（Docker 容器 / **受限子进程，不是安全沙箱**）、一次性临时目录、超时、不注入凭据 | `tools/dev_auth.py` | `test_dev_test_authorization.py`（子进程与凭据两种边界的文案断言） |
| 提交时的复测同样在执行生成代码，却没被授权约束 | `dev_submit_tool` 在 function 型工具提交前走同一道闸门 | `tools/dev_tools.py`、`services/app.py` | `test_dev_test_authorization.py`（未授权时提交被拦下且不进入审批） |
| 「测试通过」容易被当成「真实链路验过」 | 引用凭据的工具，测试结果正文与审批说明都写明「凭据是模拟的，未注入真实凭据」 | `tools/dev_tools.py`、`tools/dev_auth.py` | `test_dev_test_authorization.py`、`test_dev_tools.py` |

顺带收敛：应用启动时只建一份 `SandboxExecutor`，开发测试、提交复测与注册后的真实执行共用它（超时 / 环境 / 输出上限同一套）。

**仍未做（不宣称完成）**

- ~~**项目级依赖隔离**仍未实现。~~ **已由 2026-10-02 一节补齐**（专用环境 + 锁定清单 + 清理入口）。
- ~~**真实外部服务模拟**没有做。~~ **已由 2026-10-02 一节补齐**（`qio-mocks.json` 显式模拟服务夹具；默认断网、假凭据、结论写明「不等于真实服务已验证」）。
- ~~**旧的传统创建路径** `ToolLifecycle.create_from_request` …… 但确实还留着。~~ **已删除**（2026-10-02）。
  复现证据：调用它时即使用户拒绝，沙箱仍执行了 2 次 AI 生成的代码；生产代码里只有定义、没有调用点（6 处调用全在测试里）。
  现在由 `tests/test_tool_creation_entrypoints.py` 的源码守卫锁住它不会复活（AST 断言 + 「执行生成代码只允许两个位置」扫描）。

---

## 本轮变更：已保存对话的原文检索（2026-09-30）

补的是第一阶段里那条「找到的只是摘要，说过的话查不到」：`memory_search` 只检索
`memory_index`（**封存并摘要成功**的片段）。一条还在开放片段里的消息、或者摘要失败的内容，
在索引里根本不存在 —— 用户明明说过，Agent 却回「未找到相关记忆」（已复现）。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 开放片段 / 摘要失败的内容搜不到 | 在摘要通道之外补一条**原文通道**：直接读 `messages` 表（当前行，不是摘要） | `services/retrieval.py`（`MessageHit` / `search_messages`） | `test_message_search.py`（开放片段命中并读回原文） |
| 中文查不到 | 复用 BM25 那套 CJK 1/2-gram 分词；匹配只用 2 字以上的词元（单字会命中一切） | `services/retrieval.py`、`selector/tokenize.py` | 同上（「青瓷松树」这类二字词命中） |
| 给了 `topic_id` 只是「偏向」，不是过滤 | 原文通道按 `f.topic_id` 真过滤；工具层同时过滤摘要命中（实体卡没有话题归属，保留） | `services/retrieval.py`、`tools/memory_search.py` | `test_message_search.py`（两个话题同样命中时，指定话题只回该话题） |
| 结果看不出「这是原话还是摘要」 | 工具输出单列一段「（以下来自**保存的对话原文**…）」，每行带 Fragment / Topic / 时间 / 说话人 / Text | `tools/memory_search.py`、`prompts.py` | `test_message_search.py`（来源标注、同一片段不重复列） |
| 删除之后旧内容还会被答出来 | 原文通道**故意不缓存**（缓存会与进程同寿），每次读当前行 | `services/retrieval.py` | `test_message_search.py`（删除后不再命中 —— 第一版加了缓存，被这条用例抓住） |

边界（说清不夸大）：这条通道是**子串匹配**（词元命中率排序），不是语义检索；一次最多扫最近
的若干条候选、单条原文最多回固定字数 —— 超过的部分没有读，是「够看清说了什么」而不是全文导出。
界面上还没有独立的「搜对话」入口：本次补的是 Agent 的检索路径（第一阶段要求的「可搜索并读取
命中原文」）。

**已验证**：`tests/test_message_search.py` 全绿；用到 `memory_search` 的相关测试文件
（`test_services.py`、`test_anchor_lifecycle.py`、`test_continue_fragment.py`、`test_tool_display.py`、
`test_tool_recovery.py`、`test_tool_router*.py`、`test_topic_ended.py`、`test_memory.py`、
`test_service_injection.py`、`test_injection_short_term.py`、`test_decay.py`、`test_focus.py`）全绿；
后端全量测试与 `scripts/check_docs.py` 见本次提交说明。

---

## 本轮变更：多文件项目与依赖契约（2026-09-30）

补的是第一阶段里那条「文件工具仍禁止路径、定义与运行仍以 code 字符串为主」：模型写不出
`pkg/util.py`，沙箱也只接收一段代码 —— 稍微拆分的项目一跑就是 ModuleNotFoundError。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 文件工具只接受单层文件名 | 改成工作区内的**相对路径**：按需建子目录、枚举返回相对路径；绝对路径 / `..` / `.` / 空段 / 非法字符 / 过深目录一律拒绝，保留名（`state.json`、`request.md`）在**任意深度**都不可写 | `tools/project_files.py`（路径规则唯一一份）、`tools/dev_workspace.py` | `test_dev_workspace_multifile.py` |
| 沙箱只执行一段代码，import 不到自己的模块 | `execute(..., files=..., entry=...)`：项目文件写进**这次调用的一次性临时目录**，worker 把工作目录放进 `sys.path`；`entry`（`pkg.main:run`）走 importlib，包内相对 import（`from .util import x`）也成立；容器分支同语义（写进容器 `/tmp/project`） | `tools/sandbox.py`、`agent/tool_worker.py` | `test_tool_project_execution.py` |
| 注册后的工具带不走项目文件 | 定义新增 `files` / `entry`，随定义 JSON 一起落库（**无新迁移**）；提交时后端从工作区收集项目文件（清单与保留文件不算），测试与运行都用**完整定义** | `tools/spec.py`、`tools/dev_workspace.py`（`project_files` / `collect_definition`）、`tools/dev_tools.py`、`tools/tester.py`、`tools/runtime_tools.py` | `test_dev_multifile_submit.py`（提交收集 / 重启后仍能 import 自己的模块 / 越界与超限被拒 / 只有 entry 也合法） |
| 依赖只有「缺了才知道」，而且不知道缺的是谁 | 定义新增 `requirements`（≤10 条，工具可声明）；缺依赖时按 `No module named 'x'` 点名，并说清「声明过没有、本机不会自动安装」；测试与运行两处都把这句话放在诊断最前面 | `tools/spec.py`（`dependency_hint`）、`tools/tester.py`、`tools/runtime_tools.py` | `test_dev_multifile_submit.py` |
| 用户批准时不知道要跑的是什么项目 | 执行前审批的说明补上「项目文件：N 个」与「声明的依赖：…（不会自动安装）」 | `tools/dev_auth.py` | `test_dev_test_authorization.py` |
| 模型不知道能这么做 | `DEV_GUIDE` 第 3 条写明多文件项目与依赖声明；`dev_write_file` / `dev_list_files` 的描述改成相对路径口径 | `prompts.py`、`tools/dev_tools.py` | —（文案） |

边界（说清不夸大）：**没有受管依赖环境** —— 声明了不等于会装上，缺依赖仍然明确失败；
项目体积上限 40 个文件 / 单文件 200 KB / 合计 400 KB；一次调用一个临时目录，不落工作区。

**已验证**：`test_dev_workspace_multifile.py`、`test_tool_project_execution.py`、
`test_dev_multifile_submit.py`、`test_dev_tools.py`、`test_dev_workflow_integration.py`、
`test_dev_test_authorization.py`、`test_tool_lifecycle.py`、`test_tool_credentials.py`、
`test_sandbox_worker.py`、`test_tool_worker.py` 全绿；后端全量测试与 `scripts/check_docs.py` 见本次提交说明。

**仍未做**：项目级隔离依赖（QIO 管理的专用 Python 环境）；把工作区里的**二进制**文件带进定义
（现在只带能按 UTF-8 读回的文本文件）。

---

## 本轮变更：开发规范分节注入（2026-09-30）

补的是第一阶段第 5 条：规范原来是一份长文 `DEV_GUIDE`，创建任务时一次性灌给模型 ——
之后模型在写代码、排错、提交时，该看的口径已经不在眼前，而开头那份里大半内容当下还用不到。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 规范只有一整份长文 | 拆成 7 个分节（流程 / 需求规格 / 契约 / 测试与授权 / 提交 / 结论 / 恢复），并声明「每个步骤该拿哪几节」；**文本仍只有这一份来源** | `prompts.py`（`DEV_GUIDE_SECTIONS`、`DEV_GUIDE_BY_STEP`、`dev_guide`） | `test_dev_guide_sections.py`（每步只拿自己的那一节、没有孤儿分节、任何一步都不超过全文的六成） |
| 规范只在创建时出现一次，后面再也看不到 | 按步骤注入到对应的工具结果里：创建任务给「流程 + 需求规格 + 契约」；跑测试给「测试与授权」（通过与失败都给）；提交成功给「提交 + 结论」；`dev_list_tasks` 给「恢复口径」 | `tools/dev_tools.py`（`_with_guide`） | `test_dev_guide_sections.py`（创建结果含需求规格、不含提交口径；测试结果含执行授权与「不等于真实链路」） |

**已验证**：`test_dev_guide_sections.py` 全绿；`test_dev_tools.py`、`test_dev_workflow_integration.py`、
`test_dev_test_authorization.py`、`test_dev_multifile_submit.py`、`test_tool_lifecycle.py`、
`test_tool_create_events.py`、`test_tool_display.py`、`test_turn_facts.py`、
`test_final_answer_fact_check.py` 全绿；后端全量测试与 `scripts/check_docs.py` 见本次提交说明。

**仍未做**：规范分节仍写在代码里的长字符串，没有独立文件与版本号；用户可见的「开发规范」页面没有做。

---

## 本轮变更：范围授权的查询与撤销（2026-09-30）

补的是第一阶段里那条「查询/撤销的范围授权」：用户批准过一次「可以在这个环境里跑这个任务的
生成代码」之后，既看不到**授权范围**（在哪儿跑、能碰什么、用哪个凭据），也没有任何办法
收回 —— 只能一直有效。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 授权只是一句「已允许」，没有范围 | 批准时构造一份范围（能力 / 目录 / 网络与允许的主机 / 凭据引用 / 执行环境），**审批说明、落盘记录、之后查到的范围是同一份数据** | `tools/dev_auth.py`、`tools/dev_workspace.py` | `test_dev_authorization_scope.py`、`test_dev_test_authorization.py` |
| 授权查不到 | 新增 `GET /api/dev/authorizations`：列出仍然有效的授权与范围（任务、工具名、执行环境、能力、目录、网络、凭据、授权时间） | `api/server.py`、`tools/dev_workspace.py`（`authorizations()`） | `test_dev_authorization_scope.py` |
| 授权收不回 | 新增 `POST /api/dev/authorizations/{task_id}/revoke`：清掉这条授权并落盘，下一次测试（或提交复测）重新征求确认 | `api/server.py`、`tools/dev_workspace.py`（`revoke_test_authorization`） | 同上（撤销后 `test_authorized` 为假、重启后仍然为假、未知任务不报假成功） |
| 界面看不到、点不到 | 任务清单里展开时查一次授权范围，写着「已授权在本机受限子进程里跑它的测试；使用凭据：…」，并给一个「撤销授权」；撤销失败如实说「仍然有效」 | `frontend/src/components/DevTaskEntry.vue`、`stores/session.ts`、`services/api.ts` | `frontend .../DevTaskEntry.test.ts`（展开时查询、显示范围、撤销后提示消失、撤销失败不假装收回） |
| 任务列表看不到「这个任务授权过没有」 | `GET /api/dev/tasks` 每行补 `authorized` | `api/server.py` | `test_dev_authorization_scope.py` |

边界：这次收回的是**测试执行授权**；已经注册的工具走注册审批，不受这条撤销影响。

**已验证**：后端 `test_dev_authorization_scope.py`、`test_dev_test_authorization.py`、`test_dev_tasks_api.py` 全绿；
前端 `npx vitest run` 79 文件全绿、`npx vue-tsc --noEmit` 通过；后端全量测试与 `scripts/check_docs.py` 见本次提交说明。

**仍未做**：真机界面验收（撤销按钮只在组件测试里点过）；授权的**过期时间**没有做（现在一直有效到被撤销）。

---

## 本轮变更：有证据的进展判断与无进展暂停（2026-09-30）

补的是第一阶段里那条「有证据的进展判断、无进展暂停」：原来的护栏只看**失败次数**
（同一工具 15 次告警 / 30 次停），一个「成功但毫无信息增量」的循环（反复读同一个文件、
反复查同一个状态）永远触不到阈值，只能把迭代预算和 token 烧完 —— 用户最后看到的
只是一句「预算用完」，不知道其实什么都没发生。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 「有没有进展」只按失败次数判断 | 新增按**证据**判断的 `ProgressTracker`：指纹 =（工具 + 规范化参数 + 成败 + 结果正文摘要）；连续 3 次拿到完全相同的指纹 = 这一轮没有新信息。结果变了（文件改过、状态变了）就不算重复 | `core/progress.py` | `test_progress_tracker.py`（同调用同结果三次即判、结果变化算进展、参数变化不算、失败反复也算、中间穿插别的调用会清零、阈值可配） |
| 预算被烧完才发现「一直在原地」 | 主循环每批工具调用后按**调用顺序**喂给进展判断；命中就**暂停**：有审批通道 → 走既有「继续/停止」通道问用户（用户选继续则计数清零接着跑）；没有通道 → 停下并写明原因 | `core/loop.py` | `test_loop_no_progress.py`（同样的调用提前停下且远没用完预算、发出 code=no_progress 的 WARNING、结果每次不同则照常跑完、用户选继续→跑完、用户选停止→停下并说明） |
| 预算这条兜底被顺带掩盖 | 三个预算用例改成「参数每次不同」：预算仍然是**兜底**，而「无进展」不该靠它来兜 | `tests/test_loop.py` | 同上 |

边界：判定的是「这几次调用拿到的东西完全一样」，不猜测模型意图；暂停不删任何东西，
用户可以选继续，也可以换一个方向重新说。真正的语义（`recoverable`、类别等）不参与判定 ——
那是自述，不是证据。

分工（第一次全量测试抓出来的）：**反复失败仍归 `RunawayGuard`**（15 次告警 / 30 次询问，
用户 2026-09-22 专门调过），进展判断只管「成功却没有信息增量」这一类护栏看不见的循环 ——
否则会把用户调过的失败保护悄悄提前到第 3 次。相应地，护栏与预算那几条用例改成
「参数每轮不同」，它们考的仍然是护栏/预算本身。

**已验证**：`test_progress_tracker.py`、`test_loop_no_progress.py`、`test_loop.py`、
`test_turn_facts.py` 全绿；后端全量测试与 `scripts/check_docs.py` 见本次提交说明。

**仍未做**：真机界面验收（「无进展暂停」在真实应用里的继续/停止条没点过）。

---

## 本轮变更：项目级依赖环境（按需受管的 Python）（2026-09-30）

补的是第一阶段里那条「隔离依赖、按需受管 Python 环境」：以前声明了第三方依赖也
没有任何地方能把它装上 —— 工具永远跑不起来，用户还得自己猜要装什么、装到哪。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 声明的依赖没人装 | 新增 `ToolEnvManager`：为每个**依赖集合**准备一个 QIO 管理的专用虚拟环境（目录在数据目录的 `tool-envs/` 下，按依赖集合指纹复用） | `tools/tool_envs.py` | `test_tool_envs.py`（无依赖不建环境、未准备时明确不 ok、按依赖集合去重、失败不记成「已就绪」） |
| 装依赖是一次网络 + 磁盘动作 | 必须先拿到用户确认（新增审批 kind `dependency_install`，列出要装的包）；拒绝或超时 → **不建环境、不跑测试**，并说明「没有同意」 | `tools/tool_envs.py`、`tools/dev_tools.py` | `test_tool_envs.py`、`test_tool_env_integration.py` |
| 测试与运行可能偷偷用错解释器 | 声明了依赖的工具：测试用专用环境的 Python；注册后的调用也用它，环境没准备好就**明确失败**（不静默回落随包环境） | `tools/tester.py`、`tools/runtime_tools.py`、`tools/executor_env.py`、`tools/sandbox.py` | `test_tool_env_integration.py`（测试跑在专用环境里 / 拒绝安装则不执行 / 无依赖工具不受影响 / 注册工具缺环境时拒绝执行） |
| 容器执行时依赖环境会被忽略 | 容器隔离执行 + 需要专用环境 → 直接说明「容器里不会装这套依赖」，请改用受限子进程或去掉依赖声明 | `tools/sandbox.py` | `test_tool_project_execution.py` |
| 界面看到安装请求会显示英文枚举名 | 审批标题加「安装依赖」；payload 里的包列表与说明沿用既有渲染 | frontend/src/components/ApprovalModal.vue（2026-10-26 已删除，统一审批由 UnifiedApprovalModal 承接） | `frontend .../ApprovalModal.test.ts` |

**真实链路验证（不是替身）**：本机用默认安装器实跑过一遍 —— 真建 venv、真从 PyPI 装
`six`、拿到专用解释器；第二次调用直接复用同一个环境（`reused=true`），没有重复安装。

**已验证**：`test_tool_envs.py`、`test_tool_env_integration.py`、`test_tool_project_execution.py`、
`test_dev_tools.py`、`test_dev_test_authorization.py`、`test_dev_guide_sections.py`、
`test_app_integration.py`、`test_tool_lifecycle.py`、`test_tool_restore.py` 全绿；
前端 `npx vitest run` 与 `npx vue-tsc --noEmit` 见本次提交说明；后端全量测试与 `scripts/check_docs.py` 见提交说明。

**仍未做**：依赖版本锁定（装的是声明里的约束，不是哈希锁定）；卸载 / 清理专用环境的入口；
容器执行路径下的依赖安装。

---

## 真机界面验收（2026-10-02）

第一次把这一批界面改动放进**真实运行的应用**里点了一遍（后端 `127.0.0.1:8734` +
Vite `127.0.0.1:5199`，独立数据目录，不碰用户的真实数据）。做法：按 `docs/status.md`
的已知形状在磁盘上准备一个未完成开发任务（含一条执行授权范围），启动服务，然后在应用内
浏览器里操作。

**看到的（验收通过）**

1. 对话页顶部出现「有 1 个工具开发任务没做完」——**不自动展开**；
2. 点开后是只读清单：「做一个把 Markdown 表格转成 CSV 的小工具 / 正在写代码 /
   还没跑过测试 / 更新于 2026-10-02 00:40」，下面一行是**授权范围**：
   「已授权在本机受限子进程（同一用户权限，不是安全沙箱）里跑它的测试；使用凭据：weather_key」，
   以及「撤销授权」「继续开发」两个动作；
3. 点「撤销授权」后，那一行授权说明与按钮**当场消失**；后端 `GET /api/dev/authorizations`
   返回空、`GET /api/dev/tasks` 的该任务 `authorized=false` —— 界面说法与后端事实一致；
4. 把授权写回 `state.json` 并**重启后端**后重新加载页面，授权与任务都还在（恢复路径有效）。

**验收中发现并修掉的真实缺陷**

- 顶部提示条（fixed、顶部居中）与对话页的「还没设置完 QIO」横幅**叠在一起**，
  横幅文字被压住（见本节验收截图之前的状态）。修法：横幅可见时给浮层一个偏移
  （`ConversationView` 设置 `--qio-top-notes-offset`，`App.vue` 的 `.top-notes` 读取它），
  横幅收起后自动回到 12px。修完在真实应用里复核：两行互不遮挡。
- 测试：`frontend/src/views/__tests__/ConversationViewNotices.test.ts`（横幅在→偏移存在、
  横幅收起→偏移清空）。

**仍未做**：这一批之外的老界面（星球、设置、审批弹窗）没有重跑人工验收；「继续开发」
按钮没有真点过（它需要真实模型凭据，本次环境没有）。

---

## 冻结产物验证（2026-10-02）

第一阶段里「真实冻结产物执行链」与「CI 用冻结产物验证」原来都是 NOT RUN。这次在本机
真的走了一遍：

1. `pwsh -File scripts/build_sidecar.ps1` → 打出 `qio-backend.exe`（56.9 MB，
   写入 `frontend/src-tauri/binaries/`）。本机 venv 没有 PyInstaller，脚本按既有设计用
   `uv run --frozen --with pyinstaller` 临时提供，不改动任何环境。
2. 对着这个 exe 跑 `scripts/frozen_worker_smoke.py`（新增，stdlib only），三项全过：
   - 单文件代码 → 恰好一行 JSON、值正确；
   - **多文件项目 + 入口 + 包内相对 import** → 值正确（这是本轮新加的「按项目执行」在
     冻结产物里的验证，不再是只跑源码）；
   - 先打印一行形似成功的结果、再以 17 退出 → 没有结果输出、不被当成成功。
3. CI 新增 `frozen-worker` 任务（windows-latest）：装依赖 → `scripts/build_sidecar.ps1`
   → `scripts/frozen_worker_smoke.py`；`docs/SETUP.md` 补上同一条本地路径。

**诚实边界**：上面第 3 步的**工作流文件本身没有在本机执行过**（没有 CI 运行环境），
它只是把已经在本机验证过的两条命令串起来；`frozen_worker_smoke.py` 与
`build_sidecar.ps1` 是实跑过的。安装包（Tauri NSIS）**仍未做端到端人工验收** ——
本轮只验到「后端冻结产物能按协议跑工具」。

---

## 本轮变更：可靠性收敛（2026-10-02）

这一轮没有加产品功能，只做「把已经发现且仍然成立的问题修到根因，并把修不了的边界写清楚」。
所有条目都带真实执行证据；没有用 skip / xfail / 放宽断言 / 平台特判换取绿灯。

### 执行链与 worker 协议（P0）

| 问题（复现） | 根因 | 修法 | 证据 |
| --- | --- | --- | --- |
| Windows 上 `tests/test_tool_worker.py` 3 条红：结果通道 stdout 全空、错误通道是字面 `\u5de5\u5177…` | worker 从不钉自己的 I/O 编码：结果用 `json.dumps(ensure_ascii=False)` 写 `sys.stdout`，cp1252 机器上只要有中文就 `UnicodeEncodeError` 死在结果通道；`sys.stderr` 默认 `backslashreplace` 把中文写成转义 | 协议通道钉死 UTF-8（字节层读写 + `reconfigure` 兜底），编不出去时降级成 ASCII 转义行（同一个 JSON 值）；结果行加字节上限，超限给**合法**的协议失败 | 显式 `PYTHONIOENCODING=cp1252` 子进程复现改前/改后；`tests/test_tool_worker.py` 新增 8 条在 cp936 与 cp1252 下都跑 |
| Linux CI 上 `tests/test_sandbox_worker.py` 10 条红，错误信息里是 `docker exit code 1` | ubuntu runner 的 docker 守护进程可用 → `auto` 选了容器路径；而测试的 fake worker 是 monkeypatch 内部 resolver，**只有受限子进程路径会读它**，注入被静默覆盖；容器分支还是第二套实现（只认最后一行、不校验 ok/value、不限长、超时只杀客户端） | 正常依赖注入 `SandboxExecutor(tool_executor=…)`，显式注入后 `auto` 不再探测 docker；容器路径跑**同一份 worker 源码**，协议判定收敛为唯一 `_parse_worker_result`；容器补齐 `docker rm -f <本次唯一名字>`、有界读取与取消语义 | 修前用进程边界桩复现出与 CI 逐字相同的 `error='docker exit code 1'`；CI run `36908760127`：backend py3.11 / py3.12 / windows-latest / frozen worker 全绿 |
| 冻结产物冒烟在 CI 第一条打印就 `UnicodeEncodeError`，冒烟本体结论未知 | 报告层没钉编码（cp1252 控制台） | 报告层按「tty 保留控制台编码 + `backslashreplace`，重定向写 UTF-8」处理，永不抛异常；新增 `--script` 源码模式 | CI 上真 onefile 产物 3/3 通过；本机 onedir 3/3 通过；`PYTHONIOENCODING=cp1252` 下源码模式 3/3 通过 |

- **环境变量白名单**保持逐项列出，绝不整体继承 `os.environ`；调用方显式注入的 `extra_env` 是受信通道，在白名单之后合并（有回归用例断言它进得去、而主机上的 `QIO_SECRET_MARKER` 进不去）。
- **进程树清理**的上层语义在 Windows 与 POSIX 上一致，断言完全相同（真子进程 + 存活标记 + 正对照）。

### 记忆派生：模型输出统一可修正层

| 问题（复现） | 根因 | 修法 | 证据 |
| --- | --- | --- | --- |
| `title` 120 字 / `summary` 4000 字 → 摘要、索引、实体卡、知识**全部归零**；`candidates` 50 条 → 知识静默为 0 | 硬上限直接压在 pydantic 契约上，调用方把任何 `ValidationError` 当成整条派生失败；旧归一化只补了 entities/keywords 两个字段 | 新增 `memory/model_output.py` 统一可修正层（声明式字段规则：类型/数量/长度/去重/空白/坏条目逐条丢弃），只有「无法解析 / 类型完全错误 / 必需结构缺失 / 无法安全恢复」才算失败；实体卡提炼也接入该层 | `tests/test_derivation_recovery.py`（45 条，含 8 类故障注入与「不可修复必须进 trace」） |
| 知识提炼失败只有 `logger.info`，没有状态行 | 摘要是派生任务、知识是内联的，两者不对齐 | 知识成为独立派生任务（与摘要同构：状态 / attempts / 可读 last_error / 可重试 / 幂等） | `tests/test_derivation_recovery.py`、`test_fault_injection.py` |
| 摘要被截断在数据里不可见 | 只有 trace note | `fragments.meta.summary_truncated` + `summary_repairs`（封块摘要与滚动摘要同口径），不改摘要正文 | 同上 |

### 检索与话题判定：阈值/权重来自真实 eval，不再来自历史常量

- 话题判定**由一条绝对余弦阈值管三种决定**（延续 / 切换 / 新建）改为 owner-first 三门槛；默认值来自 104 条真实 ONNX 语料（`bge-small-zh-v1.5:fp32`）。旧实现在同一语料上 `acc 0.279 / 延续召回 0.056 / 假新话题 0.926`，新实现 `acc 0.885 / 延续 0.930 / 真新话题 0.826 / 假新 0.074`。
  **诚实口径必须一起读**：分层 5 折「折内选参」的泛化估计只有 `acc 0.731 / 真新话题 0.696`；900 组网格里没有任何组合能把真新话题召回推到 0.80（分数带天然重叠）。两个数都写在代码注释与 `backend/evals/EXPERIMENTS.md` 里。
- 检索排序**由两层收敛为单层唯一入口**（`services/retrieval.py`）：候选阶段只产出底层相关度，规则分项作为信号带出；同一维度不再算两遍，第一层不再提前截断。默认权重由实验决定：`relevance=1.0 / rule=0.25`，**时效与话题亲和为 0**（权重扫描与 5 折里从未被选中）。臂对比：两层 `R@1 0.597 / MRR 0.664`，单层纯相关性 `0.792 / 0.840`，生产默认 `0.847 / 0.873`。
- 12 条旧话题评测里有 4 条用的是 `topic_eval` 的假几何体（同一条消息假值 0.2946 vs 真实 0.5259）——**测的不是产品路径**。已换成带模型身份的真实余弦快照（缺快照大声失败，不静默回落）；`backend/evals/baseline.json` 一个字节未改，六项指标全部不劣于基线。
  ＊其中 3 条标签（「好」「ok」「继续刚才那个」被标成新话题）是从**当时的实现回落行为**推出来的，不是语义地面真值，已按语义修正并在 `note` 写明理由；12 条旧集仍作护栏。
- Fragment 语义切分**保持 shadow**：44 条七类边界数据上规则臂 `acc 0.8182 / 误切 0`，规则+语义最好只多 1 例，而阈值从 0.35 挪到 0.45 误切就从 0 涨到 6 —— 这是数据支持的「先不启用」，不是没做完。
- 旧数据 `relation_type='unknown'`：只做可确认的推断，不编造关系；隔离实际由来源链与话题校验承担，实测无法扩大上下文。

### 授权、安全与打码

- **删掉旧创建路径** `ToolLifecycle.create_from_request`（审批之前就执行 AI 生成代码，复现：用户拒绝后沙箱仍执行 2 次）；新增源码守卫测试锁住它不会复活。
- **授权有明确生命周期**：本次执行 / 当前开发任务 / 长期授权（**没有**硬编码时长）；身份逐字段绑定 task、能力指纹、执行环境、目录、网络、凭据、内容摘要；范围只允许收窄，环境变化即失效。
- **隔离文案与策略同源**：`isolation` 由真实执行器推导并进指纹；「受限子进程」不再被称为安全沙箱。威胁模型与分阶段方案见 `docs/security/tool-execution-isolation.md`。**强制隔离未实现**。
- **打码补上「已知密钥登记表」与全局日志过滤**：登记表精确匹配（有界 64 条、只在内存、只在读路径喂入）+ 结构化 JSON（只动字符串叶子）+ 形状正则兜底；日志层覆盖 `msg` / `args` / `exc_info` 栈文本 / `stack_info`。真实渗透实验（工具回显 `QIO_KEY_*`）修前有 2 条出口把密钥原文交给模型，修后 5 条出口全部不含原文。
- **工具历史 `error` 通道**（第三条通道，docstring 只写了参数与输出）以前不打码也不截断，已与 `output` 同构。

### 运行时可靠性与可观测性

- **turn 时间账本**：新增 `trace/phases.py`，顶层阶段铺满时间轴（缺口变成显式的 `other`，嵌套只作细分不重复计）；阶段覆盖凭据/能力探测、上下文装配、检索、模型等待、工具等待、审批等待、落库、收尾记忆处理。真实一轮：`duration_ms=1699 = sum_ms 1699 / residual 0`，其中 `approval_wait 910ms` —— 这就是「模型 1.7 秒、turn 51.5 秒」的形状，现在有名字。
  ＊那次 51498 的**原始 Trace 不在本机**（两个数据目录都是空库），所以「那一次具体是不是审批等待」无法反查；修的是「不可解释」本身。
- **队列持久化**：迁移 25 `turn_journal`。受理即落 `queued`，worker 取到 → `running`，重启时 `queued`/`running` → `interrupted`（**不自动执行**）；`/api/runtime/state.interrupted_turns` 如实给出，`resend`（一次性 claim）或 `dismiss`。前端尚未接这两个入口。
- **import 边界**：新增每模块单独起进程的导入冒烟（对当前全部模块逐个新解释器导入）。已记录的循环导入**在当前 main 上不复现**；红绿对照证明旧的三入口测试抓不到它、新测试能抓到。
- **工具历史保留**：整条保留 / 单条删除 / 清空历史，**默认行为不变**（默认永久保留）。
- **取消语义**只做回归验证：queue 不被堵、HTTP client task 被取消、turn 正确 `cancelled`、不保存假 final。**供应商是否停止服务端生成与计费，QIO 无法保证。**
- **测试计时断言**：`test_trace_store` 的「200 次写入 < 1.0s」改成自校准相对口径（同进程同形状原生基线，实测 0.95–1.12x，阈值 3.0x）。原来那个绝对常数在这台机器上贴着原生基线本身，负载一高必红，会让「全绿」失去意义。

### 依赖与环境

- **依赖可复现**：安装时向 pip 要 `--report`，落成锁定清单（package / 精确版本 / 下载来源 / sha256 / Python 版本与平台 / 环境指纹）；重建按锁定版本装（全有哈希时进哈希校验模式），锁定版本装不上时如实标 `re-resolved`。环境身份 = 依赖集合 + Python 主次版本 + 平台。锁定清单两份存放，**删环境不丢**。
- **环境清理**：`inventory` / `cleanup_candidates` / `remove` / `cleanup` + 运维 CLI；删除三道闸（显式确认 / 仍被已注册工具引用则拒绝 / 无引用信息时保守拒绝），删除后工具侧明确提示「需要重新准备 + 重建会用哪批版本」。
- **容器依赖**：准备侧按锁定清单构建指纹 tag 镜像（本机有就离线复用，没有则一次审批后构建）；执行侧接上依赖镜像，**镜像缺失不回退**到受限子进程（回退等于用没有依赖的解释器跑一遍）。**容器执行路径本身未在本机真实验证**（本机无 Docker 守护进程），真跑在 ubuntu CI。
- **外部服务测试**：`qio-mocks.json` 显式模拟服务夹具 —— 默认断网（未声明 host 连 DNS 都不做）、只把声明过的 host 接到本机桩服务器、凭据只注入明显假值、结论固定写明「没有访问真实服务，不等于真实服务已验证」。

### 前端与发布

- 「继续/停止」条不再猜原因：无进展暂停不再显示「已达迭代上限 3/128」；预算耗尽的两种形状（迭代次数 / 输出 token）由后端给 `reason` + `budget_kind` + 人话说明，界面照说。
- 无障碍：凭据弹窗补 Tab 焦点循环；星球话题列表补 `listbox` 祖先；暂停条文案与 `aria-label` 同源。
- **发布闸门** `scripts/release_gate.py`（离线、不联网、不安装）：真实跑 `dist/` 得到 12 项 PASS + **1 项 FAIL** —— 安装包（2026-09-29）比 sidecar（2026-10-02）旧，**重打包前不要发布**。`--selftest` 会注入两种缺陷验证闸门自己会红，并已加进 CI 的 `docs consistency` 任务。
- **完整 NSIS 安装包端到端验收：未执行**（一步都没跑）。本会话环境拒绝工作树外与 HKCU 写入，安装器 `/S /D=` 8 秒后退出码 2、目标目录与注册表项均未产生。可离线完成的部分（发布闸门 + 人工清单）已交付，见 `docs/e2e-qualification-agent-g.md`；**该文档里所有通过的结论都不覆盖安装包**。
- 旧报告里的 favicon 404 与「assistant 数据到达前短暂 `tok 0`」经代码路径与真实启动采样核对，**均不再存在**（obsolete）。

### 本轮明确没有做的事（边界，不是 bug）

- ~~**真实强制隔离未实现**~~ **部分推进（第二阶段）**：工具子进程现在有两项**真实的内核强制** —— Job Object（内存/活动进程上限 + KILL_ON_JOB_CLOSE 收整棵树）与低完整性降级（写边界）。**网络与读仍未隔离**，AppContainer 在本机被拒（需要提权）。见下一节与 `docs/security/tool-execution-isolation.md`。
- **供应商侧停止生成 / 停止计费**：取消只能保证客户端 HTTP task 被取消。
- **非 Windows 平台**：Linux/macOS 仍未完整产品化验证。
- **Planet 手感**：`getContext('webgl2')` 约 101 ms、点开到星球视图约 101 ms，本轮**未复现** 1.5–3.4 秒的说法；没有为启动速度改动任何东西。真机 GPU / 高刷新率 / 触控板 / 冷启动仍需人工。

---

## 本轮变更：产品化收尾（2026-10-02 第二阶段）

第一轮把「工程能力本身」修对了；这一轮的目标是把它变成**用户可用、可发布、可持续维护**的状态。
没有重新设计已经稳定的系统（worker 协议 / sandbox 执行语义 / 记忆派生 / 检索排序核心一行未改，除非新测试证明回归）。

### 一、发布与安装包：安装 E2E 第一次真跑（P0）

`scripts/install_e2e.py` 在**独立安装目录 + 全新空数据目录**下跑完整链路，**43 条结果 / 40 PASS / 0 FAIL / 0 SKIP**：

安装 → 安装目录清单（8 个文件）→ 内置模型与 `model_manifest.json` 逐文件核对 →
**启动安装目录里的 sidecar**（白名单环境，不继承开发变量）→ `/api/health` 200 且回答者就是那个 exe →
离线假厂商 + 保存凭据 → 真实 turn → 工具开发全链（create_tool → dev_write_file → dev_run_tests 2/2 → dev_submit_tool）→
两次审批（tool_execution / tool_create）→ 注册 → 调用（`{"sum": 5}`）→ 工具记录 →
**重启后**任务状态 / 授权 / 已注册工具仍可用（`{"sum": 42}`）→ 重装保留用户数据 → 卸载清空安装目录且保留用户数据。

安全设计：**诱饵实验**（把「既有安装」指向诱饵目录 + 会留痕的假卸载器，跑真安装器，验证假卸载器没有被执行）；
整轮 D:\QIO 既有安装逐文件 sha256 未变，注册表已还原。完整记录见 `docs/e2e-install-2026-10-02.md`。

**上一轮「静默安装装不上」的根因不是安装器逻辑**：读上次构建真正用过的 `installer.nsi` + 现场编译最小探针证明
`INSTALLMODE=currentUser`（不弹 UAC）、传了 `/D=` 就不受既有安装影响、**静默模式下自定义页回调根本不被调用**。
真凶是参数形式：`/D=` 必须最后且**不能带引号**，而 Python 传列表会给含空格路径加引号。

### 二、发布闸门：现在是硬阻断，且有历史

- `scripts/publish_release.ps1` **先过闸门再发**，不过就 `throw`（有意做成硬阻断，不要改成 continue-on-error）。
- 闸门结果**落库**：每次运行追加一条 JSONL（时间 / commit / 版本 / 逐项状态 / 安装包 sha256）到
  `docs/releases/release-history.jsonl`，并与上一条比较（新通过 / 新失败 / 一直失败）；`--history` 读回。
- 当前真实状态：**共享 `dist/` 仍然停在「不要发布」** —— `sidecar.fresh` 报红（那个包是 2026-09-29 打的，
  里面是更早构建的后端）。这正是这条闸门存在的意义。

### 三、这一轮**没有**达到发布级别（诚实结论）

三个独立拦路项，都不在本轮可解范围内：

1. **产不出被应用信任的更新签名**：`dist/qio-updater.key` 是**口令加密**的（`tauri signer sign -p ""` →
   `incorrect updater private key password`，内层写着 `rsign encrypted secret key`），口令不在本机。
   没有伪造 `.sig` / `latest.json` —— 未签名产物走**显式标记**通道（名字带 `UNSIGNED-TEST`、不写签名、
   附 `UNSIGNED-TEST-NOTICE.txt`），闸门会如实报 `latest.json` / `installer.sig` 两项不通过。
2. **安装/卸载的注册表登记没验过**：本会话的子进程拿不到写注册表权限（`WriteRegStr` / `DeleteRegKey` 静默失败），
   「控制面板里出现/消失」这件事无法在本机验证。
3. **界面层一步没走**（Tauri WebView 没有 CDP）：图形向导、界面审批、界面卸载复选框等 6 步必须人工，
   已逐条写进文档，不当作通过。

另外：**第三方依赖安装链路**因为铁律禁止联网测试而跳过（不是通过）；**应用内更新**完全没验。

### 四、用户可见的两项前端能力

- **重启后的未完成消息有恢复入口**：后端早就有 `interrupted_turns` / `resend` / `dismiss`，前端以前只消费
  `interrupted_approvals`，于是用户看到的是「我说过的话不见了」。现在有常驻一行入口，展开显示原文 / 中断时间 /
  人话原因，可「继续发送这条」或「忽略」，多条时可「全部忽略」。**不自动重发**（避免重复调用模型/工具/改文件）。
  真实证据：真后端重启路径（queued/running → interrupted）、连点三次「继续」只发 **1 个**请求、
  真实 409 有可读回执、completed/cancelled/notify 不出现、出现与展开都**不抢焦点**。
- **一轮为什么等这么久，用户看得懂**：助手消息下可展开耗时分解，按**用户语义**分类
  （排队 / 准备环境 / 上下文准备 / 记忆检索 / 模型 / 工具 / 等待确认 / 保存 / 记忆整理 / 其它），
  不出现内部阶段名（未知阶段落进「其它」，原名只在开发者模式显示）；嵌套按独占时间算不重复计；
  `residual_ms` 如实并入「其它」。**真实开发库里的 7 条旧 turn 没有 phases 列**，所以降级路径不是假想：
  旧数据显示「总耗时 8.4 秒，但这次没有分阶段记录」。隐私侧做过扫描与敌意实验：`phases` 不含路径/密钥/用户原文，
  面板只消费 `phases`。

### 五、工具隔离：从「只有声明」到两项真实内核强制

`backend/src/agent/tools/isolation.py` + 对 `sandbox.py` 的**纯追加式**接入（worker 协议 / env 白名单 / 输出上限 /
进程树清理语义 / docker 路径一行未改）：

- **Job Object**：内存 1 GiB、活动进程 32、`KILL_ON_JOB_CLOSE`。实测：超限分配 → `MemoryError`；连拉 5 个进程 →
  4 个 `WinError 1816 配额不足`；关句柄 → worker 与孙进程都不在了。
- **低完整性（MIC）降级 —— 默认关闭（`QIO_TOOL_LOW_INTEGRITY=1` 打开）**：机制本身实测有效
  （对照组可写 `user_files` / QIO 数据目录，降级后两者 `WRITE-DENIED PermissionError`）。
  **但它在 Windows CI（普通完整性 runner）上把 8 条用例打红了**：降级一旦真正生效，
  工具连**自己的一次性 scratch 目录**与 mock 夹具目录都写不进去 —— 标签没有可核实的落地。
  因此现在默认关闭，只在显式打开时启用；打开时会先**读回核实**标签，核实不了就跳过降级（fail-safe）。
  「默认生效的真实强制」目前**只有 Job Object 这一层**。

**仍然没做到的（明确写清，不改文案假装安全）**：网络**未隔离**；读**未隔离**（知道路径就能读）；
AppContainer 本机被拒（`0x80070005`，需提权）；受限令牌启动路径需要改 sandbox 启动方式，留作下一阶段；
Job 只收容「指派之后创建」的后代（真实执行流程没有这个窗口，已用时序用例锁住）。
**受限子进程不是安全沙箱**这句话依然成立。

### 六、跨话题召回：三方案实测都不达标，默认保持不变

先把问题量化：语料从 10 条扩到 **46 条 / 7 子类**（每条带 note）。真实 ONNX 下跨话题
**R@1 0.065 / topic@1 0.435 / topic@5 0.804**，普通集 R@1 0.847 未变。

缺口分解（这一步改变了结论方向）：底层召回可见 31/46 → 进候选池 14/46 → 进 top-5 10/46；
即 **15/46 在召回阶段就找不到**，只有 4/46 属于「进池却没排上」。

| 方案 | 跨话题 R@1 | topic@1 | 普通集 R@1 |
| --- | --- | --- | --- |
| 现状 | 0.065 | 0.435 | 0.847 |
| A 查询改写 | 0.022 | 0.413 | **0.444**（双向变差） |
| B 话题指纹扩候选 | 0.065 | 0.435 | 0.847（等价，无收益） |
| C 关系扩展 | 0.065 | 0.413 | 0.847（无收益） |
| O oracle（直接给正确话题） | **0.065** | **0.435** | 0.847 |

oracle 那一行最有信息量：**把正确话题直接喂进去，top-5 一点没变** → 瓶颈不在候选生成，也不在排序层缺维度。
`CrossTopicPolicy` 的三个开关**默认全关**（现状行为逐位不变）。结论与两次踩坑（指纹文本召回会让普通集崩到 0.347；
指纹摘要里可能装着答案）都记在 `backend/evals/EXPERIMENTS-CROSSTOPIC.md`。

### 七、长期测试体系与自身回归

- **护栏做了红绿对照**（以前从没做过，等于不知道护栏是否抓得住）：话题阈值改回 0.7 → 2 条红；
  边界否定句护栏失效 → 误切从 0 变 3、2 条红；注入真循环导入 → 2 条红 + 1 条 ERROR。全部恢复后绿。
- 话题语料 104 → **118 条**（`git diff --numstat` = `14 0`，只增不改），新增多轮上下文 14 条；真实 ONNX 复算
  `acc 0.8983`（12 条旧误判逐条未变）。边界 44 条标定值与「语义切分不接线」都被钉住。
- 修掉 `scripts/check_docs.py` 一个静默缺陷：扩展名候选顺序让 `.jsonl` 被截成 `.json`，所有 `.jsonl` 路径永远误报不存在
  （并给它加了自身回归测试）。长期体系说明见 `docs/longterm-testing.md`。
- **CI 真抓到一个真缺陷**：`scripts/release_gate.py` 在 cp1252 的 Windows runner 上第一条中文 print 就 `UnicodeEncodeError`，
  闸门根本没跑完 —— 已修并加回归用例。

### 八、这一轮新发现的产品缺陷

- **冻结后端的访问日志被我们自己的日志打码工厂打坏（已修）**：`backend/src/agent/trace/redact.py` 为了避开
  「打码误伤消息模板」先合成消息再 `record.args = None`，而 uvicorn 的 `AccessFormatter` 要**按位置解包** `record.args` ——
  安装包里的后端每处理一个请求就往 stderr 打一条 `TypeError: cannot unpack non-iterable NoneType object`，
  访问日志整条丢失（请求仍 200，所以自动化测试没抓到）。修法：args 按叶子打码且**容器形状与简单类型不变**，
  模板只在「%-指令数量不变」时替换。验真：真 uvicorn 打 `/api/health` → 日志错误 0 条、正常访问日志 1 条。
- **卸载不清理 `HKCU\Software\qio\QIO`（未修）**：`installer.nsi` 只在勾选「删除应用数据」时才删这个键；
  下次图形安装会用 `RestorePreviousInstallLocation` 指向已删目录。属打包脚本改动，待排期。
- **onefile 子进程可能成孤儿（待确认）**：PyInstaller onefile 的服务子进程不随引导进程退出，
  壳异常结束时可能留下孤儿 sidecar 并锁住安装目录里的 exe。需要专门实验，未列入本轮结论。

---

## 本轮变更：第三阶段（2026-10-03）——把「已知但没答案」的问题变成有答案

这一阶段不新增功能，只处理第二、三阶段已经暴露、并且**现在可以明确推进**的剩余问题。
凡是实验没证明有效的方案一律不上线；凡是环境无法验证的一律不写成完成。

### 一、后端进程生命周期：孤儿是真的，但壳里已有修复，且它依赖时序

- **进程模型**：onefile 是 **launcher + child**，**真正监听端口的是 child**；两者 `ExecutablePath` 完全相同，
  所以按名字/路径都分不出父子（这正是「按进程名杀」会误伤的原因）。
- **孤儿真实存在**：只结束 launcher → child 仍在、端口仍开。
- **但壳里的 Job Object 是真的生效**，而且**不是竞态**：child 比 launcher 晚 **1.2~2.3 秒**才创建
  （launcher 要先解 55MB 压缩包），而 assign 在 `spawn()` 返回后毫秒级完成 → **10/10 次都赶在 child 创建之前**，
  收容成功；关掉 job 句柄后无残留、端口释放。
- **反证（重要）**：等 child 出现再 assign → child **逃逸**，关 job 后它继续监听。
  也就是说「assign 必须紧跟 spawn」是修复的一部分，已写成代码注释 + 单测钉住。
- **本轮发现的真实缺口**：job 建不出来 / assign 失败时，代码注释写着「退出仍有 taskkill 兜底」，
  但 `RunEvent::Exit` 里只有 `child.kill()`（只杀 launcher）—— 兜底当时**只存在于注释里**。
  已接上：job 未生效时按 **pid 结束整棵树**（`taskkill /PID <pid> /T /F`，5s 硬超时），
  **绝不按进程名杀**。
- **更新/卸载影响**：运行中的 `qio-backend.exe` **可以改名，但不能删除、不能原地覆盖**
  （WinError 5 / EACCES）—— 这正是安装器历史上 `Can't write` 的形状。**端口关 ≠ 文件没被锁。**
- **CI 门槛**：`--case 2`（孤儿形状）与 `--case job`（Job Object 时序，含延迟 assign 的反证）已进 CI。
  注意一个教训：**采集脚本本身不能当门槛** —— 它跑完 `return 0`，进程模型退化了照样绿；
  所以另写了一个断言检查器（非 0 退出 = 步骤红，失败时发 `::error` 注解）。

### 二、安装与卸载：安装信息与用户数据已经分开

- **普通卸载残留 `HKCU\Software\qio\QIO`（安装位置）已修**。修法是在新增的 NSIS 钩子
  （`frontend/src-tauri/nsis/installer-hooks.nsh`，通过 `installerHooks` 挂载）里：
  清掉安装位置与 `Installer Language`，并用 `DeleteRegKey /ifempty` **保留 `DbBaseline`（用户状态）**。
  选钩子而不是 fork 900 行的模板 —— 后者会把每次 Tauri 升级变成人工合并。
- **卸载器只收「本安装实例自己的」sidecar（2026-10-03 P4-A 修复）**：
  以前钩子写的是 `CheckIfAppIsRunning "qio-backend.exe"` —— 按**可执行文件名**找当前用户的进程，
  两份 QIO 安装并存时卸载 A 会连 B 的 sidecar 一起杀（**已用改动前产物实测复现**，见 `docs/e2e-install-2026-10-02.md` §15）。
  现在改成按**所有权记录**：
  * 外壳在安装目录写所有权记录（schema=1）：自己与后端的 `pid` + **进程创建时间**
    （Windows FILETIME，100ns）+ 映像路径。三者同时匹配才算「就是当时那个进程」→ 抗 PID 复用；
    文件名**本轮起改为按实例区分**：权威记录是 `sidecar.lease.<shell_pid>.json`，
    同时仍写一份旧的 `sidecar.lease.json` 作兼容镜像（卸载钩子与既有脚本只认旧名；
    旧名已被另一个活实例占用时不覆盖）。理由与多实例语义见文末「跨进程共享资源」；
  * 卸载钩子调同源的 `qio-uninstall-helper.exe`（`frontend/src-tauri/src/ownership.rs` 是两个二进制
    **共用的一份实现**）：记录对得上才动手（先 WM_CLOSE 让外壳自己按 job 收树，超时才 `taskkill /PID`），
    对不上就**什么都不动**（exit 3）；
  * 帮助程序缺失 / 起不来 / 判定不了 → 直接往下走，**绝不回退成按名字杀**。宁可留下删不掉的文件，
    也不误杀别人的进程（「宁可不杀」写进了契约）。
  顺带发现并修掉第二条连坐路径：模板卸载段里还有一处 `CheckIfAppIsRunning "qio.exe"`（同样是按名字，
  静默卸载直接杀当前用户**所有** qio.exe）。被杀的那个壳持有 Job Object（KILL_ON_JOB_CLOSE），
  它一死 → job 关闭 → 它那份安装的 backend 也一起死。Tauri 的 `installerHooks` 只能追加宏、
  **不能替换模板里已有的语句**，所以在构建期做一次**机械、可验证、幂等**的模板补丁：
  `scripts/patch_nsis_template.py` 把那一处换成 `!insertmacro QIO_CloseMainExeIfOwned`
  （守卫定义在 `frontend/src-tauri/nsis/qio-ownership.nsh`：先 `--check-only` 问「本实例的壳在不在」，
  在才 `--close-installation --allow-main-exe` 收自己的壳；不在就什么都不做）。补丁由
  `scripts/build_nsis_with_patch.py` 夹在「Tauri 生成 `installer.nsi`」与「makensis 编译」之间执行：
  用**原子替换**写回（就地写会让 makensis 读到写了一半的文件，实测报 `Invalid command: "!either"`），
  并且钩子里有**编译期门禁** —— `!ifndef QIO_OWNERSHIP_PATCHED` 就 `!error` 中止编译。
  于是「补丁到底进没进产物」是编译期事实：**构建成功 = 补丁一定生效**，没打上补丁的构建会直接失败
  （包装脚本还会重试几次；不看产物字符串 —— NSIS 用 LZMA 压整包，字符串搜不到，会得到假阴性）。
  发布闸门 `uninstall.contract` 会把「钩子又按名字杀」「缺守卫宏」「缺编译期门禁」都判红
  （自检里各有一条用例）。
- **CI 上现在是硬断言**（`install e2e (windows-latest)` 任务）：安装信息写入 → 普通卸载后
  卸载登记被移除、安装位置被清掉、**合成 `DbBaseline` 仍在**、安装目录清空、用户数据保留、
  运行中的 sidecar 被卸载器收掉。注册表这条路径在本机**永远无法验证**（子进程写不了注册表，
  如实记 NOT VERIFIED），由 runner 覆盖。
- **日志通道曾经是坏的（2026-10-03 本机实测并修掉）**：安装态下 tauri-plugin-log **完全初始化失败**
  （`failed to initialize plugin log: 拒绝访问 (os error 5)`），`qio.log` 一个字都没有。
  根因不是权限：`Builder::new()` 的默认 targets 是 `[Stdout, LogDir]`，而 `.target()` 是**追加** ——
  那个默认 `LogDir` target 初始化失败就把整个插件拖死，跟我们自己加的 target 成不成没关系
  （两个变体报的错一模一样）。改用 `.targets([...])` **替换**默认列表，并按
  「数据目录文件 → 插件默认日志目录 → 控制台」顺序兜底，每个失败原因都留档。
  实测：正常启动后 `$QIO_DATA_DIR\logs\qio.log` 有内容，且含 `已写 sidecar lease：shell pid … / backend pid …`。
- **归属记录写失败 = fail-closed（2026-10-03 实现并实测）**：写不下 lease 时**绝不留下后台**。
  处理顺序：留明确原因（`<QIO_DATA_DIR>\qio-startup-error.txt` → `<install_dir>\qio-startup-error.txt`
  → stderr，首行稳定标记 `QIO-LEASE-WRITE-FAILED`；同一段也进日志的 [ERROR] 行）→ 用 job
  `TerminateJobObject` 收掉**本次启动的后台** → 退出码 1。GUI 下默认弹一次错误框，
  `QIO_STARTUP_ERROR_DIALOG=0` 关掉它（无人值守/自动化环境用）。
  为什么必须 fail-closed：那条路的终点是「卸载时无法确认归属 → 宁可不杀 → 安装目录删不干净」，
  而且用户拿不到任何指向真因的线索。本机实测（把 `sidecar.lease.json` 做成目录制造真实写失败）：
  exit=1、原因文件首行 `QIO-LEASE-WRITE-FAILED`、安装目录里**没有** `qio-backend.exe` 残留。
- **卸载残留 python-runtime（2026-10-03 实测 + 已修）**：装完用一次依赖型工具再卸载，
  会残留整个 `<安装目录>\python-runtime`（33.9MB）。根因不在卸载器逻辑：
  Tauri 的卸载段只删**登记过的文件**（逐个 `Delete`），最后只做一次**非递归**的 `RMDir "$INSTDIR"`；
  而 venv 与工具用的都是安装包自带解释器，stdlib 就在 `python-runtime\Lib` 下 ——
  import 时生成的 `__pycache__` 就是"安装器没登记过的文件"，整个目录因此删不掉。
  两层修：
  * **根治**（backend）：`tools/tool_envs.py::_clean_env` 给所有 python 子进程钉
    `PYTHONDONTWRITEBYTECODE=1`，安装目录里不再长出字节码（有单测守）。
  * **兜底**（NSIS POSTUNINSTALL 钩子）：`RMDir /r "$INSTDIR\python-runtime"` +
    清掉 `sidecar.lease.json`、按实例的 `sidecar.lease.*.json` 与它们的临时文件等运行期文件，
    再重试一次非递归 `RMDir "$INSTDIR"`。
    **不做** `RMDir /r "$INSTDIR"`（用户可能把 QIO 装在别的目录旁边，整棵删会连带删别人的东西）。
- **"界面静默消失"的真因：不是产品缺陷，是"程序放在带沙箱 ACL 的工作区目录里"**（2026-10-04 定位）
  现象：窗口建出来（class `Tauri Window` / title `QIO` / visible）后 1~9 秒被销毁、进程继续活着、
  `qio.exe` 从不生成 `msedgewebview2.exe` 子进程、`EBWebView` 零文件变动，同时产生
  `msedgewebview2.exe` APPCRASH（`msedge_elf.dll`，异常码 `0x80000003`）。
  **定位过程（同一份二进制、只换目录）**：
  * `C:\qio-probe`、`C:\qio test\app`、`C:\Users\zxy\Documents\qio-probe-app`、`Desktop`、`D:\` → 窗口**全程存活**；
  * 只要放在 `C:\Users\zxy\Documents\Front agent\...`（本会话的工作区根）下面 → **1~1.5 秒就死**；
  * 复制品 `.probe-app`（内容与能跑的副本逐字节相同、只换了父目录）→ 也死；
  * 换 CWD（用工作区目录当工作目录、但 exe 在干净目录）→ 能跑 → **是 exe 所在目录，不是 CWD**。
  机制：工作区根目录带**沙箱 ACL** —— `ADMIN\CodexSandboxUsers:(OI)(CI)(M)`、
  `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`（低完整性、禁止向上写）、
  `Everyone:(CI)(DENY)(DC)`（拒绝删除子项）。放在其下的可执行文件因此继承低完整性标签，
  WebView2 的宿主进程在这种文件上起不来（Chromium 沙箱直接崩）。
  **这与产品无关**：用户真实的 `D:\QIO` 安装、以及任何普通目录都正常（0.1.10 装在那里一直能用）。
  这也解释了为什么"重装/回滚 WebView2、`--no-sandbox`、`--disable-gpu`、换 profile、
  换 `WEBVIEW2_USER_DATA_FOLDER`、脱离我的进程树"全都无效 —— 试的方向从一开始就错了。
  教训（写给下一个在本工作区里跑桌面验证的人）：**桌面/E2E 验证必须把被测程序放到工作区之外的
  干净目录**（例如 `C:\qio-verify`），否则会得到"产品坏了"的假结论。
  **没有**在本轮加"窗口没了就报错退出"的看门狗：写好了但唯一一次正向实验被外部误杀，
  没验证过失败路径就不进发布（补丁留在 `.build-tmp/window-diagnostics-and-watchdog.patch`，下一轮再做）。
- **安装后完整使用过程：四步全过（2026-10-04 真桌面；② 在干净目录下补验）**
  §17 里那次（包 `f46ea483…`，`PASS 53 / FAIL 4`）的 4 条 FAIL 全部来自上面那条工作区沙箱假象：
  把同一份构建放到 `C:\qio-verify` 重跑，**第 ② 步也过了** ——
  窗口出现 → lease 写出（`install_dir=C:\qio-verify`，shell/backend 的 pid 与 exe 逐项一致）→
  给主窗口发 1 次 `WM_CLOSE` → **外壳自己退出（exit=0）、lease 被删除、零残留进程**。
  详见 `docs/e2e-install-2026-10-02.md` §17.8。
  * **① lease 内容：全绿** —— install_dir 对；shell/backend 的 pid + 创建时间 FILETIME + exe 与
    **独立枚举**（Toolhelp/`GetProcessTimes`/`QueryFullProcessImageNameW`）逐项一致；
    安装目录里 2 个活动进程全部能被 lease 解释；lease 不含令牌；日志里记了本次写入。
  * **② 正常退出 → lease 清理：NOT VERIFIED（本机没有时间窗）** —— 窗口存活 0.1~7.6s，
    而 lease 写出在 8.0~8.7s，**两者不重叠**；对照实验：窗口一出现（t=0.06s）就发 WM_CLOSE，
    外壳**一直没退**（启动期主线程被 WebView2 初始化挡住，消息根本没被处理）。
    按纪律**没有**用 taskkill 凑过 —— 这条要等窗口寿命问题（见上一条，机器侧 WebView2）修好后重跑。
  * **③ 保持开着直接卸载：修复后全绿** —— `S3-010 … 目录存在=False；残留=无`；本次安装的壳+后台被结束、
    用户数据目录**逐字节不变**、诱饵进程与第二份安装（进程 + lease 原文）**一个都没被动过**。
    （中间轮在旧包上残留 `python-runtime` → 见上面那条修复。）
  * **④ lease 写失败：全绿** —— exit=1、`QIO-LEASE-WRITE-FAILED` 在原因文件与日志、后台被收掉、零残留。
- **仍未验的（别读成"全绿"）**：
  * 本机：受限令牌下安装版外壳起不来（旧签名是 log 插件 panic；把日志插件改到 `.setup()` 里注册后
    不再 panic，但本机仍走不到写 lease 那一步）；
  * windows runner（CI run 37088404384 的 coinstall 步骤实测）：外壳进程起来了、120s 内没写
    `sidecar.lease.json`、也没有任何日志输出 → 断言按"不是已知沙箱签名"记 **FAIL**
    （10 PASS / 4 FAIL，原始行：`C-020-A/B 外壳没有写出 lease（exit=None）；日志尾部=（空）`）。
  * 代码层线索：Tauri 的 setup 回调是在事件循环 `RuntimeRunEvent::Ready` 时才调用
    （tauri `app.rs:1424`），Ready 之前不会有 setup 里的日志、也不会有 lease —— 所以最像
    「事件循环没到 Ready」（runner 会话/窗口站差异）或「Ready 之前的初始化卡住」，
    **不是**已证实的写 lease 逻辑错误。
  * 下一步诊断（未做）：① 在 setup 首行与写 lease 前后各留一条可外部观察的痕迹，或让 E2E 把应用日志
    （`%APPDATA%\qio\logs\qio.log` / `$QIO_DATA_DIR\logs\qio.log`）打出来，先定位卡在哪；
    ② 在一台真桌面机器上手工验「启动 → lease 出现 → 退出 → lease 消失」。
  * 因此 `coinstall` **没有**挂进 CI 门禁（挂上去只会让 main 一直红，且红的原因与被修的 Bug 无关）；
    判定逻辑与整条验收脚本仍在 `scripts/install_e2e_multi.py`（`--stages coinstall`），
    本机跑出的 31 PASS / 0 FAIL 与「改动前产物 FAIL」的对照见 `docs/e2e-install-2026-10-02.md` §15。

### 三、发布闸门：按实际产物判定，不再只看源码配置

以前闸门的 `bundle.config` 只读 `tauri.conf.json`，而构建期 `--config` 覆盖（例如关掉
`createUpdaterArtifacts`）它看不见 → 未签名产物在这一项上**假报 PASS**。现在：

- 新增 `updater.artifacts`（**按实际产物**判定签名）、`build.manifest`、`repo.commit`、`uninstall.contract`；
- 新增 **WARN** 状态：查到了、如实说，但不构成「不要发布」（历史比较里单列）；
- `bundle.config` 口径收窄为「只看源码配置」，不再假装能代表产物；
- 构建产出 `<installer>.build.json`（commit / version / 两个 sha256 / overrides / signing state），
  **闸门知道自己正在检查什么**；
- 自检扩到 8 个用例（缺卸载钩子、钩子删用户状态、manifest 串包……都必须变红）。
- **签名仍然 `BLOCKED BY SIGNING CREDENTIAL`**：没有口令，就不伪造 `.sig` / `latest.json`。

### 四、工具隔离：判据升级了，所以结论仍然是「默认关闭」

上一阶段低完整性被默认关闭，但**没有人知道为什么在某些环境失效**。本轮的答案：

- **`SetNamedSecurityInfoW` 返回 0 但标签根本没落上**（读回 `S:AINO_ACCESS_CONTROL`）——
  旧代码只信返回值就降级，这才是上一阶段 CI 8 条红的根因；`icacls /setintegritylevel` 才真的落标签。
- **判据升级**：**「标签读回 Low」只是必要不充分条件**。CI runner 的真实完整性是 **High**，
  即使标签读回 `S:AI(ML;OICI;NW;;;LW)`、令牌读回 Low，工具写自己的 scratch **仍然被拒**。
  充分条件是**一个真的 Low 进程能写那个目录** → 因此加了「真实 Low 写入探针」，探针不过就整体不降级。
- **真正执行工具的进程不是被降级的那个**：`.venv/Scripts/python.exe` 是 uv 的 **trampoline**
  （它再起一个真解释器，工具代码跑在子进程里）。只处理被 spawn 的 pid，两层强制一起逃逸。
  已改成递归枚举整棵后代、逐个降级 + 指派 job + 读回核实，**核实不到就不许声称**。
- **结论：仍然默认关闭**。可判定条件是「一个真的 Low 进程能写工具的工作目录」；
  只有探针通过的机器才生效，默认那一层 Job Object 不受写权限影响。
- **诚实缺口**：High 环境下「标签读回 Low 却写不进去」的**内核级原因没有追到底** ——
  本轮做到的是**可判定 + 自动 fail-safe**，不是解释清楚。
- **网络**：只给工程判断（容器 / AppContainer / WFP 三条可行），**未实现**；
  并且明确写了 monkeypatch / 环境变量 / 提示词**都不算**网络安全隔离。
- 文案审计：前端与文档里与隔离相关的表述**未发现越界**（仍严格区分「QIO 不给」与「OS 阻止」）。

### 五、安装版第三方依赖工具：一个真实的产品级缺陷（本轮最重要的发现）

**缺陷**：安装版 QIO **完全无法为带第三方依赖的工具准备环境**。根因是 `ToolEnvManager.base_python =
`sys.executable`，冻结后它就是 `qio-backend.exe`；于是执行 `qio-backend.exe -m venv <dir>` ——
后端只认 `--tool-worker`，参数被忽略，**又启动了一个后端**（真机上会悄悄跑到 600s 超时），
而 `_clean_env()` 又把 `USERPROFILE/HOMEDRIVE/HOMEPATH` 清掉，于是它死在 `Path.home()`。

**修复**：非冻结态行为不变；冻结态**绝不使用 `sys.executable`**，按 `QIO_PYTHON` → Windows `py -0p` →
`PATH` 找，并要求 major.minor 与后端一致；找不到时给出**可行动的明确失败**（「需要 Python 3.11，
可用 `QIO_PYTHON` 指定」）且**一个子进程都不起**。`_clean_env()` 保留 home 变量
（子进程不是安全边界，缺 home 只会让工具炸）。

**修好之后，在真实安装产物上跑通了完整链**：依赖审批 → 环境创建 → 安装并锁定（`six==1.17.0` + sha256）→
测试 → 注册 → 调用 → **重启后仍可调用且版本一致** → 删除环境后**明确进入「需要重新准备」而不是静默换环境** →
重建后仍是同一批锁定版本 → 离线（近似）再调用正常。**21 PASS / 0 FAIL**（1 WARN、1 NOT TESTED）。

**2026-10-03 P4-B 已把「要求用户自己装 Python」这条产品级缺陷修掉**：安装包现在**自带**一份 Python 运行时。

- **产物**：`scripts/build_runtime.ps1` 用 `uv python install <ver>` 取官方 CPython（python-build-standalone），
  裁剪掉 `tcl/`、`include/`、`libs/`、`Lib\test`、`idlelib`、`tkinter`、所有 `__pycache__`、
  `site-packages` 里的 pip/setuptools/`_distutils_hack`（以及会报错的 `distutils-precedence.pth`），
  **保留 `Lib\venv` 与 `Lib\ensurepip\_bundled`**（`-m venv` 的离线 pip 就来自这里）→ **33.9 MB / 726 个文件**。
  脚本带 4 项自检（产物解释器能跑 / `-m venv` 建得出 / venv 内 `pip --version` 可用 / major.minor 一致），
  任何一步失败就 exit 1（坏运行时绝不当成功产物）。
- **版本唯一来源**：`scripts/python-version.txt`（仓库根另有 `.python-version` 给 uv 用，两者必须一致）。
  冻结后端与自带运行时必须是**同一个 major.minor**：后端按 `sys.version_info[:2]` 记环境身份，
  `ToolEnvManager._prepare` 会拒绝不一致的环境。`build_sidecar.ps1` 现在会**先核对再打包**，不一致就明确失败。
- **解析顺序**（`tools/tool_envs.py::_resolve_base_python`）：`QIO_PYTHON`（显式，优先级最高，不匹配就明确失败）
  → **`QIO_BUNDLED_PYTHON_DIR`**（外壳在冻结态把 `resource_dir()/python-runtime` 交下来）
  → 冻结态 `py -0p` / `PATH` → 非冻结态 `sys.executable`。自带运行时存在但版本不对时**明确失败**，
  不会静默换成本机碰巧有的解释器。
- **打包**：`tauri.conf.json` 的 `bundle.resources` 增加 `resources/python-runtime → python-runtime`；
  `build_installer.ps1` 把「生成运行时」排在「打包」之前，缺运行时即构建失败（与缺模型同一口径）。
- **验收（2026-10-03，第 2 步）**：`scripts/install_dep_e2e.py --no-system-python` 已接进 CI 的
  `install e2e` 任务（静默安装之后、替身 sidecar 之前）。CI run
  [37132457659](https://github.com/HebiIsHere/QIO/actions/runs/37132457659)（commit `bedd056`）在**真安装包**上
  **34 条 32 PASS / 0 FAIL / 1 WARN / 1 NOT TESTED**：先断言"此刻系统 Python 不可用"（where 探针 + `py -0p`
  都拿不到解释器，断言不成立直接 FAIL）→ 用 `<安装目录>\python-runtime` 建 ToolEnv（`pyvenv.cfg` 的 home
  就是它，3.11.17 == 冻结后端版本）→ 装 `six==1.17.0` → **真的调用**依赖型工具，返回版本 == 锁定版本；
  缺运行时的对照（D-110）仍是「可行动的明确失败、不静默换解释器」。本机私建 sidecar 的同口径复跑
  33 条 30 PASS / 0 FAIL。原始输出与口径（含"私建 sidecar 不是安装包"）见 `docs/e2e-install-2026-10-02.md` §18。
- **仍然诚实的两条**：① 依赖工具**首次装包要联网**（D-053 冷缓存实证：pip 从 files.pythonhosted.org 取回
  six 的 wheel）——发布口径只能写「无需另装 Python，首次使用时自动安装依赖」，**不能**写"离线开箱可用"；
  ② 这一轮把 docker 从 PATH 摘掉了（`--no-docker`，模拟没装 Docker 的用户机器）：run 37131520475 实测
  runner 自带 Docker（Windows 容器模式）时产品走容器路径并失败，所以**容器路径本轮没验**（D-060 NOT TESTED）。
  顺带发现一条待定位的问题：跑过解释器之后卸载会残留 `python-runtime`（CI 的 A-090/A-096 红，见 §18.7）。

### 六、话题与记忆：一个负结果 + 一个数据支持的修复

- **跨话题（负结果）**：把 46 条拆成 explicit(30) / implicit(16) 分别评估。
  「把目标话题当前状态当检索上下文」看着很好（explicit R@1 1.000），但**全部来自答案泄漏**
  （状态取最新记忆时 46/46 被判泄漏）；换成不含答案的同话题记忆后 **P2 比 P0 更差**
  （R@5 0.267 → 0.067）。**不满足上线条件，生产行为未改**。
  explicit R@1 0.100 / implicit R@1 0.000（implicit 查询里没有目标话题的任何线索，
  **如实保留，没有去猜用户指哪个话题**）。
- **无 embedding 时的 Topic 判定（已修，有数据）**：规则层分数实际只落在 **0~0.22**，而门槛是 0.2 →
  **假新话题 72.8%**。按真实量纲重新标定（`rules_*` 四项 = 0.02，onnx 与规则共用的门槛不动）：
  **acc 0.4237 → 0.8305、假新 0.728 → 0.185、切换召回 0 → 0.909**；分层 5 折一致（非过拟合）；
  onnx 路径与普通检索**逐字段未变**（curve eval 0.8983 不变、普通集 R@1 0.847 不变）。
  产品取舍记一笔：选择「宁可多建话题，不要乱并」—— 乱并的损害不可见且累积。

### 七、工程遗留

- **迁移不是原子的（真实可靠性缺陷，已修）**：`storage/db.py` 用 autocommit（`isolation_level = None`），
  此时 `with conn:` **不会隐式开事务** → 迁移中途失败/进程死掉会留下**半截 DDL**，而 `schema_version` 不推进。
  `migration 24` 是单条 `ALTER TABLE turn_traces ADD COLUMN phases`：ALTER 生效而版本行没写就**再也起不来**
  （`duplicate column name: phases`）。现在「本迁移的全部语句 + 版本行」在一个真实事务里，
  并对**已存在的对象**做窄口径自愈（只认 duplicate column / already exists，其它错误照旧抛）。
  实测：修复前 `partial_ddl_left_behind: true`、`crash_window_restart: raises`；修复后 small/medium/large
  三档全部 ok（large = 100k turn_traces / 500k messages / 169 MB，迁移 0.07s，数据与关键字段无变化）。
  顺带确认了**真实存量库**（本机开发库的 7 条 `turn_traces` **没有 `phases` 列**）确实停在 23 版，
  说明 24/25 就是存量升级路径，不是纸面迁移。
- **容器镜像清理（F1）**：新增 inventory / 引用判定 / 清理候选 / **显式**清理 + CLI；
  三道闸（只认自家 tag → **被注册工具引用的镜像绝不删** → 必须显式确认），拿不到引用表时保守拒绝。
  测试与**定点变异验证**（拆掉任一保护分支，对应用例必须变红）见 `backend/tests/test_container_images.py`；
  真 docker 路径由 ubuntu CI 的 `requires_docker` 用例覆盖（本机无 docker，如实记）。
- **TraceStore 写放大（F2，负结论）**：benchmark 四档规模（短 / 中 / 长 / 大量事件），单次成本只涨约 1.9x，
  与**同连接同 autocommit 的裸写**同量级（1.0–1.7x）→ 开销主因是 SQLite 每条语句的提交/写盘，
  **不是 TraceStore 的形状**。**结论：不重构。**
- **CI 升级（F4）**：actions 升到 Node 24 运行时（checkout v5 / setup-python v6 / setup-node v5 / setup-uv v7），
  最小改动、无 workflow 重写；CI 保持全绿。
- **测试可靠性**：`tests/test_events_bus.py::test_parallel_waits_for_all` 的**墙钟硬断言**在高负载下会假红
  （本轮在多个 agent 并发时真的红过）。改成相对断言（并行 < 串行 × 0.75），
  并用变异验证证明它**仍有区分度**（把并行改成顺序执行 → 断言必须失败）。

## 本轮变更：放弃未完成的开发任务 + 未完成任务列表滚动（2026-10-04）

**版本：** 本次改动随 **0.1.13** 发布，用户可见的说明见 `docs/releases/v0.1.13.md`。
已发布：GitHub Release `v0.1.13`（安装包 + `.sig` + `latest.json` 三个资产都上传），
应用内更新源 `https://github.com/HebiIsHere/QIO/releases/latest/download/latest.json`
实测返回 `version = 0.1.13` 且 URL/签名结构正确；发布闸门 `scripts/release_gate.py`
本地可判定的项目全部通过（18 PASS / 0 FAIL，构建清单里的 commit 与本次发布的提交一致）。
**未验证的一段**：`latest.json` 里的下载地址走公共加速前缀 `gh.llkk.cc`（与 v0.1.12 完全相同），
这台机器连不上该主机（connect timeout），所以「加速通道能否真正下到安装包」没有实测；
GitHub 直链与 `gh` 上传本身是通的。
版本位共 7 处（`backend/pyproject.toml`、`backend/src/agent/__init__.py`、
`backend/src/agent/api/server.py` 的 `FastAPI(version=…)`、`frontend/package.json`、
`frontend/src-tauri/Cargo.toml`、`frontend/src-tauri/tauri.conf.json`、
`frontend/src-tauri/Cargo.lock` 里 `name = "qio"` 那一条），逐处断言一致。
**注意 `Cargo.lock` 只能按包名定位**：历史上一次批量替换把 `winapi-util` 的版本也改成
0.1.12，导致 cargo 解析失败（`f5b16e3` 修回）；本次复核 `winapi-util` 仍是 0.1.11。
`frontend/package-lock.json` 的根版本停在 0.1.10 是既有情况（0.1.11 / 0.1.12 也未同步），
本轮沿用先例，未顺手改动。

两件事：① 用户可以**放弃**一项不再需要的未完成开发任务（此前只有「继续开发」「撤销授权」）；
② 「有 N 个工具开发任务没做完」的清单支持内部滚动，任务多、需求文字长时不再溢出窗口。

### 一、三个动作互不混用（语义边界）

| 动作 | 含义 | 本次范围 |
| --- | --- | --- |
| 放弃开发 | 结束一项**没做完**的开发任务：进终态、从未完成列表移除、收回该任务的执行授权 | 新增 |
| 撤销授权 | 只收回「在某个环境里跑它的测试」的授权，任务本身仍在开发中 | 保留（原有） |
| 删除已注册工具 | 把已注册、可调用的工具从注册表里去掉 | **不在本轮** |

放弃**不删**数据库记录、**不删**工作区文件、**不删**已注册工具。工作区里的代码、需求与
测试证据原样保留，作为之后的排查与修复依据。

### 二、终态是不可逆的（后端）

- `DevTask` 增加 `abandoned` / `abandoned_at`，落盘在 `state.json`（schema 升到 3，
  **读回同时接受 2 与 3**）：本改动之前写下的任务目录必须仍能读回测试证据 / 授权 / 提交摘要，
  不能因为升了版本号就把旧记录当成「未知」丢掉。
- **对已放弃的任务，8 个写操作一律无效**：`set_phase` / `record_test` / `mark_submitted` /
  `write_file` / `archive` / `grant_test_authorization` / `consume_test_authorization`。
  这是「迟到的执行结果不能让任务复活」的可靠落点 —— 只靠工具层拦截不够：
  任何一条迟到的调用都不能把 `abandoned` 擦回 `False`，也不能清掉它。
- `abandon_readiness()` 把前置判定说清楚（状态词即接口的一部分）：
  `not_found` / `already_abandoned`（幂等成功）/ `submitted`（拒绝：已做完的工具要走别的路径）/
  `running`（拒绝）/ `ok`。**「正在执行」与「正在等审批」必须分开**：
  `stage=executing` 时拒绝并请用户先停止当前执行；`stage=waiting_approval` 时**允许**放弃
  （等审批不等于在执行，这时不放行会让用户对着确认卡无路可走）。
- 活跃执行登记是**进程内**事实（不落盘：重启后本来就没有东西在跑）。`can_stop` 由登记里的
  `stopper` **算出来**，不是硬编码：当前架构里开发工具跑在主 turn 协程中
  （`core/loop.py` 用 `asyncio.gather` 直接 await，没有留单次工具调用的取消句柄），
  所以 `can_stop=False` —— 如实报告「没有只停止这一个任务的能力」，不假装已经停止，
  也不去调 turn 取消（那会误停用户别的任务）。

### 三、接口与审批边界

- `POST /api/dev/tasks/{task_id}/abandon`：成功/幂等重复返回 `200 {ok:true, …}`；
  被拒绝（正在执行 / 已做完）返回 `200 {ok:false, status, message}` 且**零状态改动**
  （不标放弃、不收回授权、不作废审批、不停任何东西）；未知任务 `404`。
- **顺序（契约 v2，不许调换）**：允许放弃时**先把放弃终态可靠落盘**
  （同目录临时文件 → `flush` + `fsync` → `os.replace` → 回读校验 `id` 与 `abandoned`；
  失败则内存与磁盘一起保持「未放弃」，并且**一个审批都不作废**），落盘成功之后才作废未决审批：
  `ApprovalService.invalidate_for_task()` 让等待方收到 `ApprovalResult(..., "cancelled")`
  （不是异常、不是静默丢弃），落库为 cancelled，并发一条 `APPROVAL_RESULT` 事件让界面上的
  确认卡自己消失。作废是**不可逆**的，所以不能先作废再保存：保存失败会留下
  「确认已作废、任务还在」的部分完成状态；反过来（先落盘）失败代价只是
  「任务仍在、卡片还在、重试收敛」。0.1.13 里这里是相反的写法，本轮按契约 v2 反转。
- 注册前的守卫挡在 `tool_store.save` 与 `_register` **之前**（创建审批载荷现在带 `workspace`）：
  即使审批刚好在放弃前通过，也不会注册出一个用户已经放弃的工具。
- `GET /api/dev/tasks` 的行增加 `abandoned` / `abandoned_at`：这是**事实清单**，
  已放弃的任务仍然列出（`abandoned: true`）；「未完成」是界面按 `!submitted && !abandoned`
  过滤出来的视图。刷新、重启、断线都不会让它重新出现。

### 四、界面（`frontend/src/components/DevTaskEntry.vue`）

- 每行新增「放弃开发」，确认用项目已有的 `frontend/src/components/ui/QConfirm.vue`
  （`inline` + `danger` 档）：文案说清三件事 —— 结束这项开发并从未完成列表移除、
  工作区文件与记录保留且**不删除已注册的工具**、同时收回执行授权且没回答的确认会失效，
  并**点名是哪一项**（清单长时只说「这项开发」用户对不上）。
- **以后端确认为准**：`ok === true` 才移除条目与更新数量（不做乐观移除）；失败时条目留在列表里、
  原因就地显示在**滚动容器之外**（`role="alert"`，任何滚动位置都看得见），按钮恢复可点可重试。
- 提交期间该行禁用、文案变「正在放弃…」，并有第二道防重判断：重复点击不会发出第二个请求。
- 成功移除最后一项时面板收起，入口整行随列表为空一起消失（不留空面板、不留空入口）。
- `status="running"` 时原样显示后端给的原因（说清「先停止当前执行」），不说成已放弃。

### 五、列表滚动

- 面板最大高度**随窗口可用高度变化**：
  `max-height: max(180px, calc(100dvh - var(--qio-top-notes-offset, 12px) - var(--dev-panel-reserve)))`
  （`--qio-top-notes-offset` 由对话页按顶部提示条 / 设置横幅的**实测高度**写入，
  见 `.top-notes`；预留项在样式注释里逐条列出，不写成一个说不清来源的常数）。
- 列表 `.dev-task-list` 自身是滚动容器：`overflow-y: auto` + `overscroll-behavior: contain`
  （滚列表不带动背后的对话，滚到底继续滚也不穿透）、`tabindex="0"` + `aria-label`
  （键盘可聚焦后用方向键 / PageDown / End 滚动）、细而安静的滚动条。
- 错误提示区与滚动容器是兄弟节点，滚到任何位置都看得见；长需求文字 `overflow-wrap: anywhere`，
  窄窗口不横向溢出、按钮不会被挤出可见区。

### 六、本轮实测（真实实例 + 截图）

- 命令：`cd frontend; npm test` → 全绿；`npx vue-tsc --noEmit` → exit 0；
  `cd backend; uv run --frozen pytest` → exit 0（全绿）；`python scripts/check_docs.py` → 通过。
- 隔离实例（独立数据目录 + 独立端口，播种 30 项未完成任务，含多条超长需求与一条连续无空格长串）
  上的界面采集脚本 `scripts/ui-catalog/dev-abandon.mjs` 共 49 条检查、**0 条 FAIL**，
  截图落在 `frontend/e2e-shots/ui-catalog/dev-abandon/`（该目录不进仓库）。关键实测数值：
  - 滚动：滚轮 `0 → 760`、PageDown `603`、End 到底、ArrowUp 回退；列表 `scrollHeight` 远大于
    `clientHeight`；背景对话的 `scrollTop` 在列表滚动前后**恒为 160**，滚到底继续滚也不穿透；
    两个视口下横向溢出均为 0px；滚到底后最后一项的按钮 `elementFromPoint` 命中自身；
    滚到底后顶部入口仍可见；面板高度 712px / 视口 900px。
  - 滚动条（有头窗口，列表确实占到一条经典滚动条 10px）：从滑块中心向下拖生效（`0 → 1844`）、
    拖到轨道底部到 `maxScroll`、**反向拖回 `2971 → 0`**、点轨道翻页生效、拖动时背景不动；
    对照组说明「合成鼠标事件本身能拖动滚动条」。
  - 放弃：真实后端上点确认后行数 `30 → 29`、只发出一次请求、后端那一行变 `abandoned=true`、
    刷新后不再出现；**把 uvicorn 进程真的重启**后：已放弃仍为 abandoned、已提交的行不受影响、
    页面里一条都不复活；只剩 1 项时入口显示「有 1 个」，放弃最后一项后入口 / 面板 / 容器全部消失。
- **未验证**：真实触控板手势（本机无触控板，只有滚轮小步等价与滚轮/键盘/拖动滚动条三类实测）；
  以及需要真实容器 / 真实凭据的路径（与本轮改动无关，沿用既有记录）。

### 七、已知限制（诚实边界）

- `can_stop` 目前恒为 `False`：这是如实报告「没有只停止这一个任务的执行能力」。
  用户看到的是一句明确的话（请先停止当前执行），而不是一个假装的「已放弃」。
- **已修复（契约 v2）**：凭据授权审批（`credential_grant`）此前载荷里没有 `workspace`，
  放弃时那一张确认卡不会消失。现在它与 `tool_create` 一样带 `workspace`，放弃会把三类未决审批
  （测试执行 / 工具创建 / 凭据授权）一起作废，等待方都收到 `cancelled`，界面上的卡片与入口条
  由 `APPROVAL_RESULT` 驱动**立即消失**（不依赖刷新或超时）；作废文案说的是
  「任务已放弃，这个确认已作废」，不写成「你拒绝了」。留下的是设计本身的性质而不是缺陷：
  审批单次使用，作废后的旧审批再批准必然无效。
- `GET /api/dev/tasks` 把 `request` 截到 200 字：界面上显示的、确认层里点名的就是这 200 字
  （CSS 对任意长无空格串都有断行防御，将来放开长度也不会横向溢出）。

## 本轮变更：放弃开发的两个缺口（2026-10-04）

0.1.13 已经发布的是放弃功能的第一版；这一轮修它暴露出来的两个真实缺口。

**版本：** 本次改动随 **0.1.14** 发布，用户可见的说明见 `docs/releases/v0.1.14.md`。

### 一、保存失败不能再报告「已放弃」

缺口：`_write_state()` 捕获写入错误后直接忽略，而 `abandon()` 改完内存就返回成功 ——
用户看到条目消失，重启后任务又回来了（内存已放弃、磁盘没放弃）。

- `_write_state()` 的既有语义**没有改**（它仍是「尽力而为」：其它工具操作
  `set_phase` / `record_test` / `mark_submitted` / 授权发放收回 的失败语义不跟着变）。
  可靠保存是**放弃终态这一条路径**的要求。
- 新增严格路径：同目录临时文件 `state.json.tmp` → `flush` + `fsync` → `os.replace`
  → **回读校验**（`id` 一致且 `abandoned` 为真）；任何一步失败都抛 `StatePersistError`
  并尽力清理临时文件。`state.json` 及其临时兄弟文件从「工作区文件」视图里排除
  （`list_files` / `content_digest` / `project_files`），文件工具也不能写这类名字 ——
  否则残留的临时文件会改变内容摘要、把测试证据变成假 stale。
- `abandon()` 变成**事务式**，顺序固定：先严格收回长期授权并落盘 → 再改终态与任务级授权并严格落盘。
  失败时内存回滚到与磁盘一致（**绝不允许内存停在「已放弃而磁盘没放弃」**），接口返回
  `ok:false / status:"persist_failed" / persisted:false`，`revoked` 只报**已经落盘的那部分**
  （第 3 步成功、第 4 步失败时是 true，原因里写明「任务还没有被放弃；它的长期授权已经收回；可以重试」）。
- **顺序（接口层）**：先 `abandon()` 落盘，成功之后才 `invalidate_for_task()` 作废未决审批。
  作废不可逆，反过来会在保存失败时留下「确认已作废、任务还在」；先落盘的失败代价只是
  「任务仍在、卡片还在、重试收敛」。保存失败这一支**一个审批都不作废**
  （`invalidated_approvals: 0`）。
- 幂等与重试：`already_abandoned` 的重试会把上次没收回的授权收干净；「保存成功但响应丢失」的
  重复请求是幂等成功。

**持久化保证的边界（如实记录，不夸大）**：进程崩溃 / 被强杀安全 —— 要么是旧内容、要么是新内容，
不会出现空文件或半截（临时文件 + 原子替换 + 回读校验）；`fsync` 保证新内容已交给操作系统；
`os.replace` 在 NTFS 上原子。**掉电不宣称安全**：Windows 上无法 fsync 目录项，最后一次改名
理论上可能丢失，最坏结果是回到「放弃前」的旧状态，而不是文件损坏。掉电场景没有做实测。

### 二、放弃时同时作废凭据授权审批

缺口：`lifecycle.py` 创建 `credential_grant` 审批时没有任务标识，`invalidate_for_task()` 找不到它 ——
任务已经放弃，那张凭据确认卡却还挂着。

- `credential_grant` 载荷追加 `workspace`（与 `tool_create` 同一种写法），复用
  `ApprovalService._refers_to` 的既有识别规则；没有新造字段、没有给作废逻辑加 kind 特例。
- 放弃会把三类未决审批一起作废：测试执行（`tool_execution`）、工具创建（`tool_create`）、
  凭据授权（`credential_grant`）；等待方都收到明确的 `cancelled`（不是异常、不是静默丢弃），
  库里逐条落 `cancelled`，并各发一条 `APPROVAL_RESULT` —— 界面上的弹窗与待审批入口条
  **立即消失，不依赖刷新或超时**（前端本来就是按 id 收敛，这一轮先用测试证明现状，
  再用变异检查确认测试有判别力，前端生产代码因此没有改动）。
- 取消文案：`dev_auth._refusal_text("cancelled")`、以及创建 / 凭据两条审批分支的
  `label` / `detail` 都统一说「任务已放弃，这个确认已作废」，不再把系统的作废写成
  「你没有同意」或「你拒绝了」。
- 边界不变：作废是单次使用语义，作废后的旧审批再批准必然无效；其它任务的审批、
  与开发任务无关的独立凭据审批、已注册工具都不受影响。

### 三、本轮实测

- `cd backend; uv run --frozen pytest` 全绿（exit 0）；与放弃相关的几个测试文件
  （`backend/tests/test_dev_abandon.py`、`backend/tests/test_dev_abandon_persist.py`、
  `backend/tests/test_verify_dev_abandon.py`、`backend/tests/test_verify_dev_abandon_v2.py`、
  `backend/tests/test_verify_credential_abandon.py`、`backend/tests/test_credential_approval_task_link.py`）
  专项跑同样 exit 0。
- `cd frontend; npm test` 全绿；`npx vue-tsc --noEmit` 通过；`python scripts/check_docs.py` 通过。
- **失败注入**（临时文件写失败 / `os.replace` 失败 / 回读校验对不上 / 半截 JSON）：接口一律
  `ok=false / status=persist_failed`，内存与磁盘都仍是「未放弃」，重启后不复活，解除注入后重试成功。
- **中断点强杀**（真实子进程 `os._exit`，三个位置都覆盖）：写临时文件前 → `state.json` 一个字节没变、
  重启后未放弃；临时文件写完但 replace 前 → 正式文件仍是旧的**完整**内容、无半截、残留临时文件
  不影响内容摘要与证据；replace 与回读之后、响应之前 → 重启后已是「已放弃」且授权已收回。
- **凭据卡真实浏览器实测**：卡片出现 → 「稍后处理」收成入口条 → 在面板里放弃该任务 →
  入口条**立即消失且页面没有刷新** → 运行时状态里没有它、库里 `cancelled`、
  等待方得到 `cancelled` → 别的任务的凭据卡不受影响。把这条**作废过的**审批再打到产品自己的
  批准接口 `POST /api/approvals/{id}/respond`，得到 `404 approval not found or already answered`
  （即作废后不能再被批准）。
  说明：这一格第一次跑时报过一条 FAIL，事后定性为**验证台自身读错了登记表**
  （作废后那次「再批准」的助手去查了测试执行那张表，拿到 404 却按「仍然可以被批准」报出来），
  修好验证台后整套凭据卡检查项全部通过；产品代码没有因此改动。
- 没覆盖的中断窗口：长期授权文件自己的写入窗口、`fsync` 之后的掉电窗口（见上）。

### 四、仍然存在的限制

- `can_stop` 依旧恒为 `False`（没有「只停止这一个任务」的能力，如实提示先停止当前执行）。
- `GET /api/dev/tasks` 的需求文字仍截到 200 字。
- 掉电场景只做如实声明、没有实测。


## 本轮变更：卡顿竞态与「回到最新消息」按钮（2026-10-05）

启动、检查更新、准备更新与后台计算期间，窗口要一直能拖动、能响应；过时的异步结果不能覆盖当前状态；
「回到最新消息」按钮要更贴近输入区。下面按「改了什么 / 怎么验证 / 还有什么没解决」写。

**版本：** 本次改动随 **0.1.14** 发布，用户可见的说明见 `docs/releases/v0.1.14.md`。
已发布：GitHub Release `v0.1.14`（安装包 + `.sig` + `latest.json` 三个资产都上传，tag 指向构建提交 `0ac624e`），
应用内更新源 `https://github.com/HebiIsHere/QIO/releases/latest/download/latest.json`
实测返回 `version = 0.1.14` 且 URL/签名结构正确；发布闸门 `scripts/release_gate.py`
本地可判定的项目全部通过（18 PASS / 0 FAIL，构建清单里的 commit 与本次发布的提交一致）。
公共加速前缀 `gh.llkk.cc` 实测可达（HTTP 200）。
**本轮没有在真机上做的**：安装 → 首次启动 → 内置模型就位 → 设置 provider → 保存凭据 → 对话 → 创建工具 →
测试前授权 → 依赖安装 → 提交 → 注册 → 调用 → 重启恢复 → 应用内更新 → 卸载，这条人工验收链仍未跑（闸门覆盖不到）。

### 一、窗口在启动与更新期间不再被同步命令挡住（外壳）

- `qio_prepare_for_update`、`qio_refresh_updater_proxy` 以前是**同步命令**，会在主线程上做系统命令等待、
  DNS、多地址连接尝试与进程结束等待。现在两条都是异步命令 + 后台线程执行；启动阶段只做日志插件与状态注册，
  代理探测、模型准备、拉起后端都在后台任务里完成。
- 启动的依赖顺序没有被打乱：仍然是**拉起后端 → 指派任务对象 → 写归属记录**，写失败仍然让启动停下（fail-closed）。
- 代理探测有**总预算**（超时即按直连处理并写明原因）；高优先级配置可用时不再启动低优先级的系统查询；
  DNS 同一时刻只允许一次解析在飞，超时不再新起任务，不留越积越多的后台任务。一次更新操作自带它的代理配置，
  不再靠修改进程级环境变量来决定连接方式。
- 结束后端返回**真实结果**：按 pid 结束整棵进程树并逐个验证真的退出；超时、权限失败、仍有幸存者一律报失败，
  不吞异常、不按进程名批量结束其它实例。下载与校验完成、真正要替换安装文件时才结束后端；
  之后任何失败都会恢复本实例后端与前端连接，恢复状态写在错误信息最前面。
- 自动检查的定时器句柄全部保存，停用、重复启动、退出时一并清理；手动检查、自动检查、安装之间只有
  一个有效操作，旧回调不再改写新状态；自动检查仍然只检查、不下载不安装。

### 二、过时的异步结果不再覆盖当前状态（前端）

- 完整历史与更早分页在**成功、失败、收尾**三处都校验归属（请求代次 + 发起时的话题 + 分页票据）：
  慢的旧历史不会把话题改回去，旧分页不会串进新话题、也不会解锁新分页；加载期间新到的本地消息不会被快照抹掉。
- 有桌面壳但读不到连接信息时返回可重试的「尚未就绪」，**不再退化成默认地址并被当成成功结果缓存**；
  连接信息变化（后端重启、认证明确失效）会让代次变化前发起的旧解析作废，不写回缓存。
- 请求按用途设超时；**写操作（发送、审批、工具执行、设置）永不自动重试**——超时只说明没等到响应，
  操作可能已经生效；读操作可有限重试一次。
- 事件同步缓冲有明确上限，溢出会标记「需要重新同步」并再拉一次权威快照，恢复轮失败如实报失败，
  不再静默丢事件还宣称同步成功。

### 三、耗时计算不再占住事件循环（后端）

- 话题预判拆成「只读输入 / 纯计算 / 结果提交」三段，计算搬进有并发上限的执行器；提交前检查取消与代次，
  取消后不写回向量、不发切换建议、不写本轮上下文。嵌入按「输入 + 模型身份」复用，缓存访问有小锁保护。
- 真实测量（真实模型 + 真实 uvicorn + 真实 HTTP，在多次冷预判组成的阻塞窗口里并发打探针）：

  | 请求 | 修复前在事件循环上 | 修复后 |
  | --- | --- | --- |
  | `GET /api/health` | 228.7 ms | 6.7 ms |
  | `POST /api/turns/cancel` | 228.0 ms | 5.0 ms |
  | `GET /api/events` 建立连接 | 227.9 ms | 5.0 ms |

- **诚实边界**：不释放 GIL 的那一小段搬不走（批量文本处理在线程里仍会让循环停顿十几毫秒），
  所以效果是「从整个推理时长降到很小一段」，不是「完全不阻塞」。要再降需要进程池，本轮不做。
- **后台整理**同样做了拆分：维护一轮的耗时几乎全在「工具候选聚类」的逐条嵌入上，
  现在按「读取（循环侧只读）→ 纯计算（执行器）→ 提交（循环侧，提交前校验代次）」重组，
  清理范围、判定阈值、dreaming 条件与「只在没有主轮次时跑」的语义一字未改。
  同一段聚类、同一份数据的对照（唯一变量是执行位置）：`wall` 481.4ms → 510.5ms（工作量没变），
  事件循环停顿 **481.6ms → 17.2ms**；整轮维护停顿 **463.7–481.8ms → 16.6–17.1ms**。
  真实 HTTP 在整轮维护（约 503ms）期间的响应：健康探测 12.6ms、取消请求 7.2ms、事件流 5.5ms。
  **维护一轮的总时长仍是约 0.5 秒**（后台任务该有的代价），变的是不再把这 0.5 秒压在事件循环上。

### 四、「回到最新消息」按钮的位置与滚动收口（界面）

- 位置由**真实元素边界**算出：按钮下缘距输入区面板上缘 10px，水平对齐对话内容列中心。
  按钮用固定定位，不在滚动流里，不再被「消息区底边」抬高（修复前实测高出约 148px）。
- **空着的底部内容块不再写无效内边距**：以前即使没有任何内容也留出「输入区高度 + 16px」，
  消息区底边被抬高，靠贴底定位的元素跟着飘高。
- 输入区尺寸只量一次（消息流底部缓冲、底部让位、按钮锚定三处共用），不再各量一遍、各挂一个观察器。
- 滚动相关的异步回调都有归属与句柄：切话题或卸载后不再改位置；用户一操作就作废「恢复阅读位置」；
  等输入框挂载有明确终止条件；观察器通知合并到同一帧。
- 真实浏览器实测（隔离实例 + 截图）：按钮只在上翻阅读时出现、点它才回到最新；按钮中心的命中元素就是按钮本身；
  宽窗口、窄窗口、多行输入下间距都是 10px；页面缩放 80% 与 125% 下同样是 10px；点击后最后一条消息完整可读；
  输入框与发送按钮可正常操作。

### 五、跨进程共享资源的核查结论

- **模型文件同步**：以前直接复制到公共数据目录、直接写 `.ready`，跳过条件只看文件是否存在 →
  多实例共享数据目录同时首次启动时，另一个实例可能读到半成品，并且之后一直跳过复制。
  本机没有安装态外壳，**未复现**（静态证据 + 复现条件写在验证报告里）；**已修**：
  逐文件「唯一临时名 → 校验临时文件 → 同目录 `rename` 原子替换」，`.ready` 最后发布（发布前先撤旧标记），
  快路径要求「指纹一致**且**逐个文件校验通过」；校验判据 = 体积完全相等，清单声明了 `sha256` 的再逐字节比对。
  失败或中断会清掉自己的临时文件、不留假 `.ready`，下次启动重做；超过一小时的过期临时文件会被清理。
  受控验证：把实现换回旧版 → 新用例红（截断文件配匹配的 `.ready` 被判成已就绪、同体积错内容同样误判、
  发布失败仍写标记、过期临时文件未清），换回新版全绿。
- **所有权记录**：以前路径固定为安装目录下的单个文件，后写的覆盖先写的，**先退出的实例会删掉后启动实例的记录**
  → **已修**：权威记录改为按实例区分的 `sidecar.lease.<shell_pid>.json`（别人覆盖不到），
  旧名单文件仍写一份兼容镜像（已被另一个活实例占用时不覆盖）；退出只删自己那份，
  镜像只在「记的正是本进程」时删；重复启动同目录会识别并列出已有实例（不阻断合法多实例），
  只清理「外壳进程已不存在」的死记录。受控验证：把实现换回旧语义 → 新用例红
  （两个实例落在同一个文件里、候选数 1 对 2、清理与识别同样错），换回新版全绿；
  真实进程端到端（同一安装目录两个实例）实测：`--check-only` 能看到两个实例，
  `--close-installation` 后四个进程全灭、两份记录都清掉。
- 真实多实例并发（两个**安装态**外壳同时首启共享数据目录、同目录重复启动、掉电下的原子发布）
  **未复现/未跑**，复现条件已写明。

### 六、本轮实测（命令与结果）

- `cd backend; uv run --frozen pytest` 全绿（exit 0）。
- `cd frontend; npm test` 全绿（exit 0）；`npx vue-tsc --noEmit` 通过。
- `cd frontend/src-tauri; cargo check` 通过；壳侧单测（含真进程的结束与验证用例）通过。
- `python scripts/check_docs.py` 通过。
- 界面：真实实例 + 截图逐条检查（`frontend/e2e-shots/ui-catalog/latest-button/`）全部通过。
- 「修复前会红、修复后转绿」的受控用例覆盖：历史竞态、连接缓存与代次、事件缓冲上限、
  更新定时器与编排顺序、后端重活期间的健康探测/取消/事件流。
- **未跑/未验证**：真实 Windows 安装态下的窗口拖动与缩放响应、真机端到端更新安装、
  真实多实例并发、后台整理的真实耗时（本机无模型凭据）——这几项在本轮报告里逐条标为 NOT RUN。

### 七、仍然存在的限制

- 轮末与后台派生工作仍可能在事件循环上占住约 150ms（另一条路径，本轮未改动，已记录）；
  后台整理里不释放 GIL 的那一小段仍有约 17ms 停顿。
- 后台整理一轮的总时长仍约 0.5 秒（工作量没变，只是不再压在事件循环上）；
  整理里模型调用那几步的真实耗时未测（本机没有模型凭据，那几步没有真正产出）。
- 检索整段搬进了执行器（静态核查确认是只读路径）；若以后这条路径加入写操作，需要重新评估。
- 按钮位置在页面被 CSS 缩放时用「探测参考原点 + 比例」自校准，属于测量式定位；正常窗口不受影响。
- 停止后端「杀不掉的进程」分支、真实 VPN/死代理场景没有构造条件，未测。
- 多实例共享数据目录/同目录重复启动的**真实安装态**并发未复现（复现条件已写明）；
  模型同步的掉电窗口未测。

---

## 本轮变更：main 可靠性、记忆一致性与预算记账（2026-10-09）

本轮在 `origin/main`（`6e073e9`）上修 20 项功能正确性 / 数据完整性 / 任务恢复 / 资源使用 /
用量统计 / 交互问题。**不做网络安全审查**，也不扩到提示词以外的问题。
代码 + 测试 + 独立验证都在仓库里；分支 `fix/main-reliability-memory-20261009-r2`。

### 一、实例归属与可靠恢复（R01 / R04 / R05 / R06 / M06）

- **实例归属（R01）**：新增 `backend/src/agent/storage/instance_registry.py` 与迁移 **29**
  （新增 `instances`、`record_owners`，并给 `turn_journal` / `derived_tasks` /
  `pending_approvals` 补归属列）。存活判据是**四态**而不是单一阈值：
  显式退出 → 死；心跳新鲜 → 活；心跳过期**且** pid 不存在 → 死；其余 → 未知。
  **未知一律不改状态**，只在后续维护里重新判定；没有归属的旧记录保守保留并计入待处理。
  第二个实例启动不再把活跃实例的运行中任务、待确认事项、派生任务标成中断。
  Windows 上存活探测用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 只读查询，
  不用 `os.kill(pid, 0)`（实测会带崩解释器）。
- **可靠接受（R04）**：`core/turn.py` 的受理顺序固定为 **先持久化、再派发**；
  台账写失败抛 `JournalWriteError`，`POST /api/turns` 返回 503
  （`{ok:false, accepted:false}`），不入内存队列、不发 `TURN_START`。
  提交之后、派发之前退出的窗口由恢复清单覆盖（记录可见，但不自动执行）。
- **重发关联（R06）**：`claim_for_resend` 在一个事务里完成「老记录 recovered_by/at +
  新记录 + 关联」；并发或重复重发只有一个后继生效；`orphaned_claims()` 列出
  recovered_at 非空但 recovered_by 为空的遗留抢占，`repair_orphan()` 让它重新可重发
  （孤立即记录不再被永久隐藏），出口在 `/api/runtime/state.orphaned_turns`。
- **派生任务恢复（R05）**：`derived_tasks` 用 `owner_instance_id` + `claim_generation`
  做双校验；`recover_stale()` 只回收**确认已退出**实例的任务，未知保留；
  `claim_due` 每次调用都顺带核对一次超期 running 任务（恢复不再只在启动跑一次）；
  完成 / 失败 / 释放都带 `expected_generation`，迟到结果被丢弃。
- **关闭顺序（M06）**：新增 `backend/src/agent/services/background.py`；派生工作统一登记
  在 `app.background`，`aclose` 先 `background.shutdown()`（拒绝新建 → 有界等待 → 取消 →
  确认结束），再停维护 / turn / 独立任务 / 重活，最后关适配器；数据库仍由 lifespan 最后关。
  未结束的派生任务被放回队列（可恢复），关闭过程不会留下永久 running。

### 二、知识一致性（R02 / R03 / M02 / M03）

- **版本链唯一当前版本（R02）**：`knowledge/lifecycle.py` 以 `chain_id` / `version` /
  `supersedes_id` 定义链身份；`revise_atomic` / `deactivate_atomic` 在单事务里
  「核对目标仍是当前版本 → 撤销旧行 → 插入并激活新行 → 写关联」，版本不符抛
  `VersionConflict`（HTTP 409，带 current_id / current_version）。迁移 29 只加列**不回填**，
  所以 `chain_of` 会把 `chain_id` 为空的历史行按 supersedes 派生链一并纳入——
  否则同链的第二个 active 会看不见，R02 形同虚设。
  已有同链多 active 的历史冲突用只读 `list_duplicate_active_chains()` 暴露（保留原文与历史）。
- **原子替换（R03）**：撤销与激活在同一事务边界内；注入一次激活写失败后旧版本仍 active、
  有效版本数不为零，解除失败后正常纠正只有新版本生效。
- **范围归属（M02）**：新增 `knowledge/scope.py`；`node_ids` 为空不再等于全局——
  默认表单新建的知识挂到**用户全局节点**并真正进入全局注入路径；范围以真实节点归属为准
  （`topic_id` 只是兼容字段）。旧无归属条目保持 active 但标 `unresolved`
  （管理界面可见可改），不一律改成全局。
- **多目标（M03）**：`services/onboarding.py` 用 `goal:<sha1(内容)>` 作为每个目标的稳定身份，
  多值字段 upsert、不覆盖其他目标；单值字段（称呼 / 背景 / 单值偏好）保持替换语义；
  重复提交幂等；结果清单按实际生效结果生成。

### 三、实体与人工纠正（M04 / M05）

- **部分候选不删未提及项（M04）**：`entities/cards.py` 区分「用户明确整张修订」
  （`source="user"`）与「模型部分候选」（默认 `source="auto"`，按字段合并）。
  `entity_cards.field_meta` 记录各字段来源 / 修订版本 / 用户删除墓碑与冲突候选；
  迟到候选在**提交时**再次核对修订版本，冲突时保留人工值并把候选登记为可管理项；
  别名、摘要、类型、属性共用同一套冲突规则。引导表单里用户自己填的称呼 / 背景也标成
  user 来源，不会被后续普通提炼覆盖。
- **实体提炼独立任务（M05）**：实体提炼登记成独立的 `entities` 派生任务（带内容版本、
  尝试次数、可读失败原因、幂等身份），失败只重试实体、不回退已成功的摘要；
  实体只依赖原文，摘要失败时实体仍能产出（基线已有行为，本轮未倒退）；
  历史「摘要已完成、实体缺失」用有边界的补派策略（不覆盖人工修订、不重复建卡）。

### 四、模型调用：取消、预算与记账（M07 / M09 / M10 / C01）

- **外层取消回收内层请求（M07）**：`core/loop.py::_await_completion` 在正常完成、
  用户停止、外层 `Task.cancel`、关闭、请求异常五条路径上都显式管理 request 与 waiter；
  外层取消时先取消**并等待**内层请求清理，再继续传播取消，不广泛吞 `CancelledError`。
- **用量耗尽后不再发新请求（M09）**：`credentials/policy.py` 提供
  `remaining_budget`（`None` = 无上限）与 `ensure_budget_available`（耗尽抛
  `BudgetExhausted`）；三个适配器在**每次实际请求**（含内部解析重试）紧邻发送前核对，
  主循环 / 子任务 / 维护 / 后台提炼共用同一规则；耗尽只给简短真实原因，不静默换配置。
- **每次实际请求记一次（M10）**：`credentials/usage.py` 的 `record_request_usage(...)`
  是唯一入账入口（含适配器内部重试的每次响应）；失败但已知用量照记，无用量失败标
  `incomplete` 且不造数；循环内的旧回调按 sink 身份判断，消除双记，展示统计不漏记。
- **文本兼容模式的工具结果（C01）**：`adapters/text.py::_to_text_messages` 把工具请求与
  工具结果转成合法的普通文本消息（带工具名、参数、结果与 call_id 关联），
  不再下发「`role=tool` 但没有 `tool_call_id`」的非法消息，也不靠丢掉工具结果来让请求合法。

### 五、导航、设置、更新与完整恢复（M01 / M08 / M11 / M12）

- **明确起点替换未提交的选择（M01）**：明确「进入最新位置」或选择当前开放片段时，
  原子取消 / 替换旧的待接续选择（复用既有取消接口）；单纯浏览不改变起点；
  受理消息时捕获并核对选择与起点的稳定身份，已受理的排队消息不被后续导航改向。
- **设置先全量校验再原子保存（M08）**：新增 `backend/src/agent/services/settings_service.py`
  （校验 → 单事务提交 → 再应用运行时），`api/server.py` 的 memory / loop / maintenance /
  tool history / search 五个设置端点与 computer 端点全部改走它；校验失败返回 400、
  写库失败返回 500，两种情况数据库与运行时都保持整套旧值；工具历史的清理只在提交成功后执行。
- **更新资源在所有出口释放（M11）**：`frontend/src/services/updater.ts` 对取得的 Update
  在信息提取结束、版本不匹配、下载失败、停止后端失败、安装失败等出口明确释放，
  一次性关闭、释放异常不掩盖原始失败，仍保持「用户点击才下载 / 安装」。
- **所有连接入口统一恢复（M12）**：新增 `frontend/src/stores/restore.ts` 作为唯一恢复入口
  （单飞 + 代次 + 同步期间缓冲事件 + 快照后按序补事件），首次连接、刷新、重连、RESYNC
  共用；快照覆盖队列、待确认事项、独立任务、工具事实、过程说明、中断事项与未完成用户消息；
  旧代次 / 旧实例的结果被丢弃；缓冲有上限（`RESYNC_BUFFER_LIMIT`），溢出时登记「需要重新同步」，
  不静默丢事件还宣称同步成功。

### 六、冷归档按消息身份还原（L01）

- `backend/src/agent/storage/archive.py` 的归档记录写入稳定消息身份
  （`_archived_message_id`），`restore_message_content` 只返回身份匹配的那一条；
  目标缺失 / 归档损坏 / 旧格式无法确定目标都返回明确的「不可靠还原」，不再把第一条当答案；
  写入改成「先写完整暂存文件并原子发布 → 再在一个事务里登记 DB 引用」，
  任一步失败都回滚并删掉刚发布的归档，不留「DB 说已归档、归档里却没有」的假成功记录。
  冷归档**当前没有接入主要运行流程**，本轮只修既有模块，没有顺手接进生产路径。

### 七、本轮实测（命令与结果）

- 后端：`cd backend; uv run --frozen pytest` 全绿（exit 0）。
- 前端：`cd frontend; npm test` 全绿（exit 0）；`npx vue-tsc --noEmit` 通过。
- 运行时 / 预算 / 用法相关改动：`uv run --frozen python -m agent.eval.run` 与同环境基线
  逐项指标一致（评测输出见独立验证组留存的原始日志）。
- 独立验证：`scripts/rm-verify/run_all.ps1` 一键跑「基线 vs 集成后」对照，
  基线反例与修复后结果的原始输出都落在 `scripts/rm-verify/output/`。
- 文档一致性：`python scripts/check_docs.py` 通过。

### 八、数据迁移与历史状态处理

- schema 只**追加**迁移（本轮为迁移 **29**），不改历史迁移；重复启动不会重复修复。
  **号段为什么是 29 而不是 26**：`apply_migrations` 是「`target <=` 已记录版本就整条跳过」，
  而基线 `main`（`6e073e9`）自己停在 25，同基线的其它修复分支（附件发送 / 进程审计 /
  统一进程流）已经用掉了 26 / 27 / 28。最初写 26 时，任何被那些分支碰过的存量库
  （例如长期数据目录）都已经记着 26，本轮迁移被整段跳过 → `instances` 表不存在
  → 后端在 `AppContext.__init__` 抛 `no such table: instances` 起不来。
  取 29（已知最大号 + 1）后，无论那些分支先合还是后合，这条迁移对任何存量库都必然生效；
  受控回归见 `backend/tests/test_rm_lead_migration_discipline.py`（构造「版本已到 28、
  缺本轮对象」的历史样例，迁移后对象齐全、存量数据不丢、重复跑幂等），
  并用长期数据目录的副本实测后端从「起不来」变为 `backend_ready=True`、版本推进到 29。
- 历史无归属的 turn / 审批 / 派生任务：保守保留原状态并计入待处理，不自动标中断。
- 历史同链多个 active：只读列出、保留原文与历史，新写入由条件校验保证不再产生重复。
- 历史无归属知识：保持 active 但标 `unresolved`，等用户在管理界面确认，不擅自改成全局。
- 冷归档旧格式：无法确定目标时明确报「不可靠还原」，不做兜底猜测。

### 九、仍然存在的限制（诚实边界）

- 实例存的存活判定依赖心跳 + pid 探测：心跳新鲜时不看 pid（避免误判），
  因此「进程刚被杀、心跳还没过期」这一段仍会被判成存活——恢复会推迟到心跳过期，不会误中断。
- R05 的「另一活实例的任务不被抢」有受控用例覆盖；**真实多实例并发**没有在本机复现。
- `repair_orphan` 只有库层与只读出口，没有新增 HTTP 修复端点（未在本轮授权范围内定权限语义）。
- 文本兼容模式只做了**本地严格请求结构校验**，没有对任何真实厂商端点做前后对照，
  因此不对「所有厂商原先都失败」下结论。
- 用量记账以「每次实际请求一次」为准；单次请求最终消耗无法预先精确知道，
  所以只保证「已确认耗尽后不再发新请求」，不声称零误差账单预测。
- 前端界面只跑了单元 / 组件测试与类型检查；真实浏览器的窄窗口、滚动、代码块溢出
  仍未在本轮逐项截图复核。

---

## 本轮变更：历史数据可恢复、来源保护与关闭真实性（2026-10-10）

本轮在**已合入可靠性修复的 main**（`da0436b`）上修 A01–A06、B01–B02 共 8 项。
主题是「升级前留下的历史数据」与「关闭 / 记账的真实性」。只做功能正确性、历史数据、恢复、
长期信息的用户控制、生命周期、资源释放、预算与记账归属、界面接线；**不涉及网络安全，
不改认证 / 授权 / 隔离 / 安全策略**。

### 一、历史无归属的消息终于可操作（A01）

- **缺陷**：升级前的用户消息没有实例归属、状态停在 `queued`。启动恢复按契约保守保留
  （unknown 不改状态），但收件箱只认 `interrupted`、`resend` 只认可重发状态 ——
  这条消息**既不可见也不能继续**，永久卡死。
- **修法**：新增 `backend/src/agent/services/recovery.py`（`RecoveryInbox`）与
  `backend/src/agent/api/recovery_routes.py`，把「可恢复记录」做成**一套规则**，
  同时覆盖确认中断、孤立抢占、历史无归属、归属判不出来、以及卡住的派生任务；
  六种状态分开表达（`ready` / `orphaned_claim` / `legacy_unowned` / `owner_unknown` /
  `derived_stale` / `derived_legacy`），**unknown 绝不批量改成死或中断**。
- **继续的语义**：`queued` / `running` 的开放行先做**接管**——`BEGIN IMMEDIATE` +
  条件 `UPDATE ... WHERE owner_instance_id IS NULL`（只为「无归属」这一精确状态），
  命中 0 行就 409 且**一行不改**；接管之后**沿用既有可靠重发事务**
  （`TurnJournal.claim_for_resend`：老记录 + 新记录 + 关联同一事务），不另写一套派发。
  任何写入之前先检查派发通道，没有通道就 503 且一行不改（绝不假 200）。
- **忽略**：先接管再标记用户已知晓（`status='dismissed'` 终态），**不删除原文**；
  系统通知轮（`notify = 1`）永不进清单、永不可操作。
- **历史派生任务**：无归属的 `running` 派生任务进清单（`derived_legacy`）并可「重新排队」——
  条件更新 + `claim_generation + 1`，`attempts` / `last_error` 原样保留；
  归属者还活着或判不出来时拒绝（重排会让同一件事做两遍，代次只挡迟到写回）。
  已超期的无归属任务由启动恢复自动放回 `pending`（此时不需要用户操作），
  这条行为也被用例钉住，不会被误当成「永久卡住」。
- **界面**（`frontend/src/components/RecoveryInbox.vue`）：并入既有的「未完成事项」槽位
  （不另起页面），显示消息原文、原状态、**一句「为什么现在动不了」**与可用动作；
  详情默认收起；失败就地显示原因并可重试；被截断时如实显示「还有 N 条未显示」。

### 二、历史来源未知的实体值会被保护（A02）

- **缺陷**：迁移 29 只加列不回填。升级前用户明确改过的摘要 / 类型 / 属性，
  `field_meta` 是空的、`revision = 0`；自动候选把「来源为空」当成可以覆盖，
  于是**人工值被模型值改掉，而且没有登记任何候选**。
- **修法**：新增 `SOURCE_UNKNOWN`；判定规则是「卡上**已有非空值** + `field_meta` 没有它的来源记录」→
  来源未知，**按用户值保护**：自动候选不得覆盖，冲突进 `user_value` / `source_unknown` 候选。
  **不伪造** `source="user"`（来源就是未知，如实标记）；`aliases` / `summary` / `kind` /
  `attributes.<key>` 共用同一套规则；未提及的字段与属性一律保留；空缺仍可合理补充；
  `user_deleted` 墓碑与已归档卡片不被绕过；提交时仍按 `expected_revision` 做 CAS。
  这是**读写兼容**方案（读时判定），没有为它追加迁移、也没有写回历史标记。

### 三、冲突候选由用户真正管理（A04）

- **缺陷**：底层已经保存候选、序列化也返回，但**产品界面没有任何入口**，
  用户看不到、也采纳 / 丢弃不了。
- **修法**：新增 `backend/src/agent/entities/pending.py` 与
  `backend/src/agent/api/entity_pending_routes.py`（列表 / 采纳 / 丢弃），
  前端 `frontend/src/components/planet/EntityCandidates.vue` 并入 `EntityPanel`：
  当前值 / 候选值 / 一句话原因 / 「采纳」「丢弃」，默认收起，不新增弹窗与反复提示。
  - **采纳** = 本次用户的明确决定：写值 + `source="user"` + 新 revision + 移除该候选；
    `expected_revision` 不符 → 409 且零改动；归档卡 `adoptable=false` 并说明
    「该实体已归档；恢复实体是另一个动作」；不支持的候选类型不给「点了没效果」的按钮；
    重复点击幂等。
  - **丢弃** = 只解决这一个候选：不改实体内容值、不影响其他候选，幂等。
    注意：丢弃会**递增卡级 `revision`**——它是卡级 CAS 令牌（丢弃要落
    `field_meta.pending/resolved`），递增之后并发的「丢弃 / 采纳 / 自动提交」
    会以 409 暴露而不是静默覆盖。这条是本轮的**明确定稿**，回归用例按它断言
    （内容值一字不变 + 其它候选仍在 + 用丢弃前的 revision 采纳必须 409 且零改动）。

### 四、关闭结果必须反映真实后台状态（A05）

- **缺陷**：后台任务延迟响应取消时，`shutdown` 如实报了 `unfinished`，但 `aclose` 只打一条
  告警就继续 `mark_clean_exit()`、释放适配器；lifespan 随后无条件关数据库 ——
  结果既写了「干净退出」，又出现「关闭后写库」。
- **修法**：`BackgroundTasks.shutdown` 改为**两阶段有界**（等待 → 取消 → 取消确认窗口 →
  final grace → 如实回报），`ShutdownReport` 增加 `still_running` / `phases` / `detail`，
  并通过 `register(..., unit_finished=...)` 声明**底层执行单元**探针 ——
  「asyncio 句柄取消了」不再等于「执行单元结束了」。新增
  `backend/src/agent/services/lifecycle.py`（`CloseReport` / `close_decision` /
  `log_close_report` / `backend_stop_verdict`）。
- **接线**（`AppContext.aclose` + lifespan）：`aclose()` 返回 `CloseReport`；
  `close_decision` 是唯一口径 —— 不干净时**不写干净退出**、**不释放适配器**
  （残留协程还会用它们）、**不关数据库**。lifespan 只在报告干净时才关 DB。
  这样做的数据安全依据：**不关库等于让进程自然退出**，SQLite WAL 崩溃安全，
  而「谁还没结束」已经写在本实例的归属里 —— 下次启动按归属恢复；反过来，
  在残留执行单元还可能写库时先关库，才是真正会丢 / 会炸的路径。
- 「更新前停止后端」：前端 / Tauri 侧本来就要求真实 `verified`（不 verified 就放弃安装），
  无需改动；缺的是 `scripts/e2e_down.py` —— 它以前用「任一端口关闭就报 ok」且**早于真实确认**，
  现改为对该进程负责的端口做**有界轮询真实确认**，确认不了就如实说「未确认停止」。

### 五、适配器显式绑定所属账本（A06）

- **缺陷**（限定场景：**同一进程多个 `AppContext`**）：`CredentialPolicy.__init__` 会把
  自己的 store 登记成**进程级默认账本**，后创建的上下文覆盖前一个；适配器若没有显式绑定，
  首次**直接调用**（后台提炼、引导追问、内部重试）就会用最后登记的那个账本 ——
  A 的 0 预算没挡住，用量却记到 B 头上。
- **修法**：`bind_request_accounting(adapter, store, key_id=None)` 成为显式归属权威
  （已有显式绑定不得被后来上下文改写，缓存复用的 adapter 保持原绑定）；
  兜底结果**钉在 adapter 上**，保证「预算核对」与「用量写入」必然是同一个账本；
  新增 `clear_default_accounting_store`（只撤销「当前登记的正是它」那条）。
  Lead 在 `AppContext._create_adapter`（新建 adapter 的唯一路径）里加一行显式绑定。
- **边界**：独立进程之间不共享该全局变量，所以这不是「所有多开都失效」。

### 六、高版本存量库不会漏掉后补对象（B01）

- **缺陷**：迁移器按「`target <= 已记录版本` 就跳过」。用户先跑到 29，之后集成其它分支的
  新 26–28 迁移时，低编号条目**永远不会执行** —— 只看版本号看不出这件事。
- **修法**：`storage/schema.py` 追加**补偿迁移 30**（编号 > 29，纯幂等写法，不改 1–29 的含义）；
  `storage/migrate.py` 增加 `REQUIRED_OBJECTS`、`missing_objects()`（**按对象校验**：
  表 / 列 / 索引，不只看数字）与 `verify_required_objects()`（缺失就补偿，仍缺抛可读
  `SchemaIncompleteError`），并在 `apply_migrations()` 结束前调用。
  四种库状态各有断言：只到 25、25 + 其它分支形状的 26–28、**版本已到 29 但本轮对象缺失**、
  全新库；都要求对象齐全、版本推进、存量数据不丢、重复执行幂等。
- **诚实边界**：那三个 26–28 分支**没有**集成到本分支，它们自己的对象本轮无法补偿，
  该组合**未验证**；它们合并前必须把各自的迁移号抬到 **30 以上**，
  否则会被本轮的 30 整段跳过（与 B01 是同一个根因的不同方向）。
  `missing_objects` 是**按名字**校验，同名异形的对象判不出来。

### 七、POSIX 低 PID 不再套用 Windows 特殊 PID 规则（B02）

- **缺陷**：`_default_pid_alive` 在**平台判断之前**就 `pid <= 4 → None`，
  于是 POSIX 上合法的 PID 1/2/3/4 一律「判不出来」。
- **修法**：先分平台 —— 通用只拒 `pid <= 0`；Windows 保持 `<= 4 → None`（0/4 是内核伪 pid，
  查询不可靠）与只读 `OpenProcess` 探测；POSIX 的 1/2/3/4 **正常探测**
  （`ProcessLookupError → False`、`PermissionError → True`、其它 `OSError → None`）。
  用替身探测覆盖，**不结束任何真实进程**。

### 八、本轮实测（命令与结果）

- 后端：`cd backend; uv run --frozen pytest` 全绿（exit 0）。
- 前端：`cd frontend; npm test` 全绿；`npx vue-tsc --noEmit` 通过。
- 运行时 / 预算：`uv run --frozen python -m agent.eval.run` 与同环境基线对比。
- 界面：真实起后端 + Vite，截图核验恢复收件箱（首次连接 / 刷新 / 失败反馈 / 窄窗口）
  与候选管理区。
- `python scripts/check_docs.py` 通过。
- 独立验证：`scripts/fu-verify/run_all.ps1`（`-Label baseline|after`）给出前后对照，
  原始输出在 `scripts/fu-verify/output/`；其中四处「判据修正 / 设计裁定」在
  `scripts/fu-verify/README.md` 里逐条写明依据（不是放宽期望）。

### 九、仍然存在的限制（诚实边界）

- A01「继续」之后的真实模型执行：受控验收用记录型 runner 只断言「恰好一次有效执行」，
  真实链路一律 fake provider。
- A05 用同进程受控实例替代「真被 kill 的进程」；pid 复用与跨机 host 判定未覆盖；
  关闭的有界性前提是事件循环没有被**同步阻塞**（协程里的同步阻塞调用无法被 timeout 打断）。
- A06 的 Anthropic 能力探测会临时建一个没有 `key_id` 的 adapter，那次探测请求不入账
  （既有行为，本轮未改）。
- B01 的 26–28 分支组合未集成、未验证（见上）。
- 候选「保存失败」在后端没有独立契约面，前端用注入 500 覆盖。
---

## 本轮变更：附件与发送边界收口（2026-10-10）

在 `fix/process-attachment-audit-final-boundaries` 之上做的一轮边界收口。中心纪律只有一条：
**所有「异步结果回来后要改文件或改状态」的地方，都要在同一个有效串行边界内再核对一次当下事实，
过期操作一律不得覆盖更新的结果。**

### 一、后端附件的提交边界

- 旧问题：提交前的代次/身份检查与最终 `os.replace` 不在同一有效串行边界内；目标在操作开始时不存在时
  身份核对整段被跳过；浏览器上传路径没有代次。旧任务因此可能覆盖更新的重新定位结果并回写旧元数据。
- 现在：提交边界在同一临界区里核对 **代次仍当前 + 提交票号仍最新 + 目标身份未变**，三者同时成立才提交；
  票号随结果回到落库路径，过期结果连行状态都不写（既有孤儿副本补偿契约不变）。上传路径与
  `copy_to_disk(generation=None)` 的旧调用方同样被这条边界保护。

### 二、引用型附件的最终接受边界

- 旧问题：引用附件通过初步复核后，克隆或等待期间源文件消失、变得不可读、或被同名文件顶替，
  最终绑定仍报成功。
- 现在：在**最终接受边界**按当下事实复核引用事实（存在、可读、未变差），只允许不变或变好；
  变差即结构化拒绝且不留下克隆行。进入时就缺失的引用仍如实登记为非可用行（既有语义不变）。

### 三、前端恢复补丁与轮询结果的归属

- 迟到的恢复补丁：应用前比较「当前本地修订号」与「请求发起时捕获的修订号」，期间有新写入就不应用
  旧的缺失剔除，保留当前记录。
- 旧轮询结果：条目级操作发起时记操作声明序号；不再是最新声明的结果既不写界面也不落盘。
  仍然有效但属于别的话题的结果，只按它自己的原话题落盘。

### 四、重新定位与重叠发送（界面）

- 重新定位：原生选择器失败回退到「粘贴路径」时，发起时刻冻结的话题与附件身份一路带到提交，
  不再用提交时刻的当前话题重新冻结；取消路径行、移除附件、切话题与组件卸载都会清掉这份捕获。
- 重叠发送：每次发送持有**自己的**准备状态与取消目标（准备标识、连接控制器、防抖定时器、中止状态），
  某次发送的收尾只清自己那一份；中止入口始终绑定它自己的请求标识。两次带附件的发送会同时停在
  「后端尚未受理」时，第二次在派发前被拒绝并保留草稿与附件。组件卸载只清定时器、**不**中止在途请求
  （中止只由用户显式动作触发，离开页面不得丢掉已经发送的消息）。

### 五、中断轮次成功重发后的终态动作

- 旧问题：`interrupted` 行如果持久化的动作表里本来就写着 `resend`，在已经被领取（已成功重发过）
  或系统通知轮上仍会原样透出，用户再点必然得到 409。
- 现在：终态动作表只有**一份**读时投影，可恢复判据是「`interrupted` 且非系统通知且尚未被领取」；
  非可恢复的 `resend` 一律去掉（`cancelled` 仍归一到真正可用的 `retry`）。台账行里的执行事实不改写，
  接口与历史读路径共用这一份投影。

### 六、Windows 后端 CI 的测试装置

- 探针子进程明确 UTF-8（`PYTHONIOENCODING` 加标准流 reconfigure），父进程按 UTF-8 解码：
  不再依赖 runner 默认编码（英文 Windows 的 cp1252 会让打印中文的探针直接崩）。
- 拒读/恢复装置：`icacls` 不动继承，只拒读写数据权限；恢复只做一次移除并**复核可读**；
  所有 `icacls` 调用的返回码和主体标识都会被检查，恢复不了就大声失败（带命令、退出码与输出）。
  装置故障不再被当成产品缺陷，也不再被静默当成通过。

### 七、本轮实测

- 新增受控回归（受控闸门/承诺对象构造时序，无随机等待）见
  `backend/tests/test_r2_w1_r1_commit_boundary.py`、`backend/tests/test_r2_w1_r2_reference_final_acceptance.py`、
  `backend/tests/test_r2_w4_terminal_facts_projection.py`、`backend/tests/test_r2_w5_windows_ci_harness.py`、
  `frontend/src/components/__tests__/r2-w3-r5-relocate-topic.test.ts`、
  `frontend/src/components/__tests__/r2-w3-r7-overlap-send.test.ts`。
  每一项都先在本轮基线上复现为红，再修到绿。
- `cd frontend; npx vue-tsc --noEmit` 通过；`npx vitest run` 全绿。
- `python scripts/check_docs.py` 通过。

### 八、仍然存在的限制

- 本轮的 Windows 侧验证以本机受控条件等价复现 CI 的失败机制；拒读装置的权限口径需要在 GitHub Actions 的
  windows-latest 上实跑复核（本机与 runner 的权限环境不同）。
- 引用型附件的「与登记时同源」只能按登记事实（大小加修改时间）与当前状态秩判断：登记里没有 inode 或
  内容摘要，「同名、同大小、同修改时间」的重建文件在登记事实层面不可区分；要更强需要登记文件身份。
- POSIX 分支的拒读装置（`chmod`）在本机（Windows）无法执行，未实测。
- 本机全量后端套件存在与会话环境相关的既有失败与顺序抖动（部分用例单独跑即通过），因此本机全量结果
  只作对照，不作为合并依据；合并依据是 GitHub Actions。

### 九、继续审计发现并已修的问题

本轮在集成之后又做了一轮独立验证与继续审计，发现并修掉了下面几条「同一族根因的残留」：

- **提交票号跨代际误伤（N1）**：票号原先在**工作线程**里领取，所以票号的先后不一定等于代际的先后；
  顺序反转时两个操作会互相作废，附件永远停在「准备中」，绑定要等满等待上限才被拒绝。
  现在代际是硬闸、票号只**在同一代际内**比较（按附件与代际分桶）——过期代际的操作不可能再作废当前代际的操作。
- **兼容调用形状仍带旧缺陷（N3）**：`restorePendingAttachments` 不带选项的那条兼容路径也会按
  「当前本地修订号」判断，期间被更新过的记录不再被旧事实删除。
- **旁路落盘入口（N4）**：`register_upload` 不再自己提交文件，改走与上传同一条提交边界；
  绕过边界的私有落盘入口已删除。
- **上传 worker 的代际错配（N5）**：工作线程原先绑定的是「它自己启动那一刻」的代际，而落库用的是
  「登记时」的代际 —— 登记在前的上传能覆盖后发起的重定位成果，行与磁盘还会不一致。
  现在上传作业携带登记时的代际，提交边界与落库用**同一个**代际。

这四条都有确定性回归：`backend/tests/test_r2_v_n4_n5_upload_boundary.py`（含把
`run_upload_worker` 的代际透传钉死的接线守卫）以及 `backend/tests/test_r2_w1_*.py` 里的 N1 时序用例。




---

## 本轮收口：恢复入口、来源保护与前端一致性（F01–F10，2026-10-10）

本轮在**上一轮补充修复分支**（`fix/reliability-memory-followup-20261010-a7f3` = `d31ea9e`）
之上做独立收口。上一轮 CI 全绿，但独立边界核查仍在 F01–F09 上找到真实缺陷；
本轮逐条修复并补确定性回归（分支 `fix/reliability-followup-closeout-20261010-f01f10`）。

### 一、旧执行者还在跑时不能再启动第二次执行（F01）

- **缺陷**：`_turn_actions` 把「没有归属」（`OWNER_NONE`）当成可操作，于是
  `legacy_unowned` / `derived_legacy` 直接给出「继续 / 知道了 / 重新排队」。
  而旧版本（升级前）**不写任何**实例 / 心跳 / 归属记录 —— 「旧 runner 还在跑」和
  「旧 runner 已经退出」在库里留下的形状**完全一样**。核查实测：旧 AppContext 的 runner
  停在事件门上时，继续接口返回 200 并在旧 runner 释放前就开始了第二次执行。
- **修法**：把「没有持久 owner 字段」与「已经证明执行者停止」分开。无归属的开放行
  **仍然可见**，但 continue / ignore / repair / requeue 默认全部禁用并给出原因；
  唯一解锁入口是用户显式确认「旧执行者已停止」（`POST …/confirm-stopped`），
  确认持久化在 `settings` 表里、跨重启有效（**不新增表、不占迁移号**）。
  `derived_tasks.recover_stale()` 不再按超时自动回收无归属的 running 任务：
  超时是时间猜测，不是「执行者已停止」的证据。
- **验收**：新用例覆盖旧版本形状（可见 + 三种动作全被 409 挡住 + 零派发）、
  确认后一次性恢复、确认跨重启有效、两个新版本实例并发仍然只有一个后继、
  派生任务同一套规则。

### 二、用户清空的字段不再被自动候选复填（F02）

- **缺陷**：`entities/cards.py` 的摘要 / 类型分支只看「现值是不是空」，现值空就直接写
  自动值，绕过 `SOURCE_USER` 与删除语义。用户 `revise(summary='', kind='')` 清空后，
  下一次自动提炼又把摘要和类型填回来。
- **修法**：空格子也先过 `_conflict_reason`（与 aliases / attributes 共用同一套来源规则）：
  `source=user`（用户设定过 / 明确清空）与迟到结果只登记待处理候选，原值保持为空；
  **从未填写过**的空字段仍可补全；旧记录里已有非空值但来源缺失仍按用户值保护。
- **验收**：用户清空 + 自动候选 / 重复候选 / 迟到候选 / 迁移后的空字段与有值字段 /
  aliases 与属性墓碑规则不回归。

### 三、派发失败的继任者在当前运行里可恢复（F03）

- **缺陷**：`claim_for_resend` 成功、`submit` 抛错时接口 503，但后继停在
  `queued` + 本实例归属：它没进内存队列，又因为「归属者还活着」不进恢复清单 ——
  用户在当前运行里看不到、点不到，只能等应用退出后的重启恢复。
- **修法**：后继被改写成显式的 `dispatch_failed`（同一个 turn_id），在恢复清单里
  给出「重新执行 / 知道了」；重试复用**同一个** turn_id（不产生第二个后继、也不静默
  宣称已执行）。两个入口都覆盖：收件箱的 continue 与 `POST /api/turns/{id}/resend`。
  另外补上「提交之前失败」的分支：重发事务整体回滚时，已经落库的那次接管会被
  `TurnJournal.release_takeover` 退回原状，记录仍然可见、可重试。
- **验收**：在提交后派发前、以及提交前两处注入确定性故障；断言 503、只有一个持久后继、
  当前运行内可见可重试、重试后生命周期（running / terminal）正常、重复点击不产生第二次派发。

### 四、孤立 claim 的修复遵守 owner 状态（F04）

- **缺陷**：专用恢复清单过滤活 owner，但 `orphaned_claims()` 没有同等过滤，
  前端合并回退数据后会把被排除的项重新显示出来；`repair_orphan` 也不检查 owner，
  活 owner 的孤儿被 200 接受、claim 被清掉。
- **修法**：repair 与 continue / ignore / requeue 共用**同一套 owner 判定**
  （`alive` / `unknown` 一律拒绝，`dead` 或「无归属 + 用户已确认」才放行），
  并且写入本身带 `IFNULL(owner_instance_id,'')` 条件（归属列与权威归属不一致时命中 0 行）。
  `orphaned_claims()` 过滤确认活着的 owner，与专用清单口径一致。
- **验收**：owner 活跃 / 已退出 / 无归属 / unknown / 并发改变状态分别覆盖；
  活跃与 unknown 不会被修复且 claim 不动，已退出的合法孤儿仍可修复，重复请求只生效一次。

### 五、读取失败不再伪装成空清单（F05）

- **缺陷**：`RecoveryService` 的 turn / derived 读取捕获 SQLite 错误后返回空数组，
  API 仍然 200 + `total = 0`：故障前有可见记录，接口却报「一条都没有」。
- **修法**：任一来源读不动 → `RecoveryReadError` → `GET /api/recovery/records` 回 503，
  原因里写清**哪一个来源**没读出来；前端保留上一次可见的内容并显示可重试原因。
- **验收**：分别注入 turn / derived 读取失败；断言 503（不是 200 + 空清单）、
  按 kind 请求时未请求的来源不影响本次结果、恢复后重试能读出真实清单。

### 六、前端：迟到响应、单一状态来源与复合身份（F06–F09）

- **F06**：候选面板的每次读取用「请求代次 + 当前 entityId」校验写回（数据 / 错误 /
  total / truncated / loading / finally 全部属于当前实体与当前请求），
  切换实体或组件卸载后的迟到响应整段丢弃；A→B 乱序返回时面板只显示 B，操作只发往 B。
- **F07**：收件箱读取记录「实例 + 代次」，实例切换后旧实例的响应不得写 records / total /
  error / loading，也不得清掉新请求的 loading；同一实例内仍然是单飞。
- **F08**：两个恢复入口共用一份状态 —— `interruptedTurns` 从权威快照派生，
  收件箱已覆盖 / 已处理的 id 不再由旧入口重复提供；忽略 / 继续成功后列表、旧入口与
  `total` 一起有依据地收敛（`total` 只减掉确实被处理的那一条，不把未知数量猜成 0），
  真实多页截断提示保留。
- **F09**：跨实体候选的渲染 key、逐行提示、忙碌状态、操作状态都用
  `(entity_id, candidate_id)` 复合身份；操作 URL / body 仍用对应的实体 id，
  **不改**持久化里的历史候选 id。

### 七、去掉「测试进程 PID 必须大于 4」的环境假设（F10）

- **缺陷**：`test_fu_verify_b02_pid.py` 断言「测试进程 PID > 4」。核查环境里 pytest
  进程 PID = 2，因此受影响集合 187 passed / 1 failed —— 失败与生产逻辑无关，
  只是一条环境假设（POSIX 上 1/2/3/4 都是合法 PID）。
- **修法**：去掉该假设，改成与 PID 大小无关的两条自证：POSIX 低 PID 逐个用信号 0 探测、
  Windows 分支绝不落 `os.kill`（只走只读 `OpenProcess` 替身）。
  生产逻辑没有为了通过测试而回退。

### 八、本轮实测（命令与结果）

- 后端：`cd backend; uv run --frozen pytest`（全量，收敛后跑一次）。
- 前端：`cd frontend; npx vitest run` 与 `npx vue-tsc --noEmit`。
- 基线对照：把同一段缺陷探针分别跑在 `d31ea9e`（基线）与修复分支上 ——
  5 条缺陷在基线**全部复现**，在修复分支**全部消失**。
- `python scripts/check_docs.py` 通过。

### 九、仍然存在的限制（诚实边界）

- F01 的「旧执行者已停止」是**用户显式确认**的结论：旧版本不写任何生命周期记录，
  语言层没有可靠的库内证据可推断；本轮不假装能自动检测旧进程。
- F05 只覆盖**清单读取**：单条记录的读取（`_turn_row` / `_derived_row`）失败仍按
  「没有这条记录」处理（409），没有单独的错误码。
- F03 的「原地重试」在当前进程内完成；进程再次崩溃时后继仍是 `dispatch_failed` 行
  （归属者已死 → 走归属判定，仍可在清单里继续重试）。
- F06–F09 只跑了单元 / 组件测试与类型检查，没有新增真实浏览器截图复核。
- 迁移边界与上一轮一致：其他分支的 26–28 号迁移未集成，该组合**未验证**，
  本轮也没有新增迁移。

---

## 本轮变更：权限与真实行为一致、报错不泄漏、失败如实收尾、发送状态不被迟到消息改错（2026-10-09）

入口：2026-10-09 独立审查的五组反例（分支 fix/main-safety-state-responsiveness-20261009）。全部反例以「未修复基线红 / 集成后绿」的对照测试钉住。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
`git tag/branch/remote` 只看名字被判只读：git tag v1、git tag -d、--git-dir= 等号形式、-C 指外仓库都能零审批写出 | 自动放行改为结构化判定：可执行文件真实身份（cwd/根内替身不放行）× git 子命令查询/写形态分级 × 等号赋值形式 token × 文件操作数与 fs 工具同规则（红线 deny、根内 auto、根外 approve） | services/computer.py（resolve_program 等） | test_sr_a_computer.py、test_sr_verify_permissions.py（临时真实 git 仓库验证拒绝后零变化） |

命令失败被报成功（带 stderr fatal 仍 ok=True），超时后进程不受控残留 | 成功/失败按真实退出码判定；超时/取消用共享清理函数按 pid 收割进程树并 await 退出验证；清不干净就如实说 | tools/cmd_tools.py、tools/proc_cleanup.py（新） | test_sr_b_exit_code.py、test_sr_b_process_cleanup.py、test_sr_verify_process.py（受控子进程 + 同名诱饵存活） |

日志打码了，但 ERROR 事件 / 重放缓冲 / 恢复快照仍带原文 | bus.publish 建立打码总闸：事件进入重放缓冲与扇出之前打码；错误出口统一 sanitize_error_text（先打码再截断） | api/bus.py、trace/redact.py、services/turn_orchestrator.py、core/loop.py | test_sr_c_redact.py、test_sr_verify_redaction.py（真实 SSE 字节 + 重放 + api_key 结构化字段四路径） |

HTTP 回执晚到把结束的轮拉回 running；超时撤掉已开始的任务 | 发送带 client_request_id（幂等受理、重试复用）；前端四态 sendAttempt：回执只单向前进；无响应失败（非 4xx）一律「正在确认」，按请求身份查证 | core/turn.py、api/server.py（by-request 查证端点）、前端 session.ts、services/sendIdentity.ts（新）、Composer.vue | test_sr_lead_send_identity.py、前端 sr-e-*.spec.ts、sr-f-turn-lifecycle.test.ts、sr-e-composerConfirm.spec.ts |

工具路由（嵌入）与文件 I/O 跑在事件循环线程 | 结构判断留循环、embed/余弦经有界线程执行器、缓存只在循环侧提交；六个 fs 工具的真实 I/O 全部进执行器；fs 写取消用 shield 等待/如实描述「可能已在后台完成」 | services/tool_router.py（route_async）、tools/blocking.py（新）、tools/fs_tools.py、core/loop.py、services/app.py | test_sr_d_*.py（线程身份 + 事件闸门内健康请求/取消推进）、test_sr_verify_responsiveness.py |

**已知限制（不粉饰）**

- 修复前的历史事件/记录没有补打码：总闸只对发布时刻起的事件生效；历史读取路径的出口打码已覆盖（redact 总闸 + sanitize 语义检验）。
- 工具级回执语义变化：shell 命令退出码非 0 现在明确 ok=False（以前被当成成功）。
- POST /api/turns 的 client_request_id 是运行期身份：进程重启后查证返回「本进程没有记录（可能未送达）」，不做跨重启重复执行判断。
- 本轮改动影响工具策略相关评测：uv run --frozen python -m agent.eval.run 输出与修复前基线一致（topic/retrieval/anchor 全指标无回归）。
- 未验证：Windows 安装包 E2E、真机浏览器截图走查（本轮以组件级 + store 级测试替代）。

---

## 集成：流式回复修复线并入 main（2026-10-11）

把 `fix/process-attachment-audit-round2`（`cc19b68`，含统一过程区、真实增量、附件生命周期
与 8 轮审计的全部修复）集成到当时的 `origin/main`（`84eb4b4`），并以 **PR #2 合并进 main**：

| 项 | 值 |
| --- | --- |
| 合并前 main | `84eb4b4` |
| 集成分支线头 | `6356c0e`（`integrate/streaming-main-20261011`，保留为历史） |
| **合并提交（当前 main）** | `7ff5e2e`（`merge: 流式回复修复线集成到 main`，父提交 `84eb4b4` + `6356c0e`） |
| 合并 CI | run `38075732622`（push，`7ff5e2e`）：docs / frontend / rust（两个平台）/ frozen worker / install e2e 成功；`backend (windows-latest)` 失败（唯一失败是 R8 取消幂等用例，固定等 1.0 秒的时序敏感断言，本机复跑通过）；两个 Linux backend 任务因 **runner 失联**失败（GitHub 注解：hosted runner lost communication，属基础设施，不是断言） |

### 一、数据库

- 来源线三条迁移改号 **26/27/28 → 31/32/33**（SQL 与语义逐字保留）：main 的迁移已到 30，
  `apply_migrations` 只按版本号前进，沿用旧号会被最新 main 的存量库整段跳过，attachments 永不建出。
- 新增**补偿迁移 34**（`COMPENSATION_VERSION` 随之改为 34）：main 的实例归属对象 + 本次新增的
  attachments / 结束事实对象合并为一份幂等语句；`verify_required_objects()` 按对象复核、缺则重放、
  仍缺抛 `SchemaIncompleteError`。只有 CREATE / ADD COLUMN，没有 DROP / DELETE / UPDATE。

### 二、运行与事件

- `TurnManager.reserve()` 支持 `request_id`（预留同样算已受理，幂等索引同一份）。
- `/api/turns` 与 `/api/turns/{id}/resend` 统一为「预留 → 准备 → 放行」，同时保留契约 C2
  （持久化失败 → 503 `accepted=false`）与契约 C3（`claim_for_resend` 单事务消费 claim + F03 派发失败标记）。
- 持久接受收口为唯一入口 `_persist_accept()`：`submit()` 与 `reserve()` 对同一故障给出同一结果
  （以前附件预留路径会静默吞掉台账写失败）。
- `claim_for_resend` 的新行插入改为幂等 upsert（兼容 `reserve()` 已经落下的预留行）。

### 三、流式用量记账（集成补丁）

`native.stream()` / `anthropic.stream()` 原先不记账：main 的 `accounts_requests=True` 会让 loop
跳过上层补记，于是回答阶段的主路径既不进凭据账本也不受预算上限约束。补丁在两条 `stream()` 的
外层补 `ensure_adapter_request_allowed()`（请求前核对预算）与 `account_adapter_request()` /
`account_adapter_failure()`，并新增 `backend/tests/test_streaming_usage_accounting.py`。

### 四、前端

- 发送一律 `api.sendTurn(message, startIdentity.topicId, ids, …)`：起点身份用提交时快照（M01），
  第三参是显式附件绑定数组（契约 §1.4：空数组也成字段）。
- 失败分流统一：只有带 status 的 4xx 算明确拒绝；5xx / 无 status 算「没拿到回执，结果未知」；
  没有结构化 detail 时不伪造附件原因。
- 用户中止带附件的发送（准备期 abort）按「明确未发送」收口，不滑进「正在确认」。
- main 的回执台账与来源线的准备态/过程区/终态动作都保留。

### 五、合并收尾补缺（2026-10-11，分支 `fix/streaming-main-closeout-20261011`，从 `7ff5e2e` 起）

合并后在 main `7ff5e2e` 上复核出两处缺口，已在独立分支修掉（未并入互动模式线、无无关重构）：

**缺口一：结构完整性检查漏掉了本线新增的半个 schema。** `REQUIRED_OBJECTS` 只覆盖实例归属
那批对象，于是「版本号已到 34、但 `attachments` 被整张删掉」的库照样被判结构完整、也不补建。
修法：清单补上 `attachments` 表、`attachments.source_attachment_id`、三个附件索引与
`turn_journal` 的 `reason_code / stopped_by / actions`（每项都与补偿迁移 34 的语句一一对应），
缺了就重放 34，仍缺抛 `SchemaIncompleteError`；只追加清单、不改历史迁移。
对照装置：`backend/tests/test_main_closeout_migration_objects.py`（四种升级起点 + 补偿幂等 +
真实业务行的数据保护 + 补不齐时明确失败）。

**缺口二：流式降级时漏记已经真实发出的请求。** `native.stream()` 按异常类别判断要不要记账，
把 `UnsupportedCapability` 一律当成「没有请求发出」；但 `_stream_once()` 可能在已经调用客户端、
读到响应之后才抛它（空异步流 / 非异步响应 / 零增量且看不到 Content-Type）。修法：改成按
**请求事实**判断（`_StreamAttempt`），已请求但无可靠用量记「不完整用量」（不估算 token），
未请求就发现能力不支持则零记账；`stream_options` 被拒后的重试在发出前重新核对预算。
对照装置：`backend/tests/test_main_closeout_streaming_accounting.py`。

**缺口三（CI 层面）：Linux 上探针收尾把 runner 一起 SIGKILL。** `scripts/sr-verify/probes.py`
的 `kill_tree()` 在 POSIX 上无条件 `os.killpg(os.getpgid(pid), SIGKILL)`；诱饵进程与调用方
**同组**，`os.getpgid()` 返回的正是调用方自己的组 → `tests/test_sr_verify_process.py` 跑到收尾时
把 pytest 连同 **runner 的 agent** 一起杀掉（Linux 任务表现为「跑到 ~47 分钟后失联、日志一条都
不上传」，Windows 走 taskkill /T 所以一直绿；定位手段是把整套用例按文件跑、每文件独立会话）。
修法：只在目标**自己就是组长**时才按组收割，否则只杀那一个 pid；诱饵改为自立进程组。顺带修掉
两处「考平台而不是考契约」的 Linux-only 用例（写死 `exit /b 7`、只造 `git.bat`），断言未放松。
对照装置：`backend/tests/test_main_closeout_process_group_scope.py` + Linux CI 实跑。

### 六、验证与未验证项

定向（两处缺口的反例、受影响面）与交付前的一次全量后端、前端类型检查与全量、`check_docs.py`、
评测基线对比：结果与本轮分支 CI 的 run ID 逐条记在
`docs/integration/2026-10-11-streaming-into-main.md`。

本轮分支 CI：**分支最终 SHA `2a27e48` 的 run `38121670380`（push）与 `38121674258`（PR）9/9 全绿**
——backend (py3.11) 8 分钟 /
(py3.12) 9 分钟 / (windows-latest) 17 分钟，外加 docs consistency / frontend / rust（两个平台）/
frozen worker / install e2e（12 分钟）。修复前的 run `38078800507` 曾红在「慢推理期间事件循环
停顿」的 100 ms 阈值上（实测 104 ms，本机复跑该文件 13 passed / 4 skipped），那是负载敏感的
既有问题；Linux backend 当时的「runner 失联」真因是上面的缺口三。
**未验证（不粉饰）**：Windows 桌面实机走查（附件发送与中止、重发、历史打开、关闭清理、窄窗口）
本轮仍未做——本检出没有构建好的桌面壳/安装包，Playwright 也未安装，实机交互流程无法在本会话执行；
组件级测试不能替代实机结论。
---

## 集成：互动模式线并入 main（2026-10-11）

来源 `fix/interactive-state-recovery-completion`（`866c97d`）并入 main（`7d20b24`），集成分支
`integrate/interactive-main-20261011`；共同基线 `6e073e9`。只做合并与必要兼容修复，不扩展互动功能。

**数据库（本次唯一的 schema 改动）**：互动对象的迁移号从来源线的 **26 改为 35** ——
`apply_migrations` 只按版本号前进（`target <= 已记录版本` 就整段跳过），沿用 26 会被任何
main 存量库整段跳过、互动接口缺表。main 的历史迁移 1–34 原样保留；补偿入口追加为 **36**
（= 迁移 34 的全部语句 + 互动对象），`REQUIRED_OBJECTS` 同步补上互动模式的全部必需表与索引（清单见 backend/src/agent/storage/migrate.py）。不删改用户数据。

**前端**：`session.ts` 保留 main 的受理/幂等/发送动作状态机与地址确认，接入互动线的点击
归属、共享草稿与失败原文；**明确未受理**（4xx / 准备期中止）才登记可恢复原文，
「受理情况未确认」（超时 / 无回执）一律只走「正在确认」，不提供原文恢复、不复用新身份重发。
`Composer.vue` 的附件闸门/准备期中止/失败恢复与互动线通知、让位草稿入口并存；
`ChatDock.vue` 的失败入口与 Composer 读同一份会话事实。

**验证范围（本轮只做针对性验收，不是全量回归）**：迁移五态（空库 / main 34 / 互动 26 /
版本已推进但对象缺失 / 重复启动幂等）与数据保留；互动后端 store/API 用例；
前端类型检查与受影响的 13 个 store/组件用例文件；文档一致性。
**未跑全量测试**：其它未触及路径（含安装包 E2E、rust 侧）与全仓用例不在本轮验证范围内，
不得据此声称「全量回归通过」或「可以发布」。

---

## P36 — 互动模式可用性修复：材料解析、草稿提升与附件预览（2026-10-12）

来源：用户在本机安装版 **v0.2.0**（`D:\QIO`，数据 `D:\QIO-data`）上报告的三个问题，分支
`fix/interactive-materials-drafts-preview`。三条问题各自有独立根因，**不是同一个 bug 的三种表现**。

### 一、冻结产物缺材料解析模块（问题：图片/文件"加不上、读不到"）

**现象（用户库中的原话，不是推断）**：`D:\QIO-data\app.db` 的 `board_understanding.context`
里，`honesty.materialsError = "材料解析模块不可用：No module named 'agent.interactive.materials'"`，
`materialsResolved = false`、`materials = []`；模型据此输出 `no_action`，并如实说"我无法读到图片内容"。

**根因**：`backend/src/agent/interactive/understanding.py` 用
`importlib.import_module("agent.interactive.materials")` 动态导入；PyInstaller 静态分析看不到动态
导入，`scripts/build_sidecar.ps1` 与 `backend/qio-backend.spec` 也没有该 `hidden-import`。
对已安装在 `D:\QIO\qio-backend.exe` 的产物逐字符串核对：
`agent.interactive.board_store / submission / understanding / execution` 都在，
**`agent.interactive.materials` 完全不存在**（`resolve_materials`、`image_unsupported`
等标识符也一并缺失）。结果：每次理解都静默降级成"没有材料"——文件卡片的正文读取
（`materials.py::_attachment_entry`）整条链路在安装版里从未生效。

**实现（两道保险）**：① `understanding.py` 增加一条**字面** `import agent.interactive.materials`
（`try/except ImportError` 兜底，只为让静态分析看见；`_materials_module()` 的 importlib 兜底与
材料错误的诚实语义不变）；② `scripts/build_sidecar.ps1` 与 `backend/qio-backend.spec` 显式声明
`--hidden-import agent.interactive.materials`。另有 `scripts/check_frozen_materials.py`（冻结产物自证：
解析 PYZ 模块清单 + 临时数据目录起真链路提交）与 `backend/tests/test_frozen_materials_packaging.py`（回归）。

**验证（对着三类真产物跑，不是只跑单测）**：

| 产物 | 模块清单 | 静态 | 真链路 |
| --- | --- | --- | --- |
| 现状安装包 `D:\QIO\qio-backend.exe`（0.2.0） | 3616 条，**无** materials | FAIL | FAIL：`materialsResolved=false`、`materialsError=材料解析模块不可用…`（与用户库原话逐字一致） |
| 修复后正式构建 `frontend/src-tauri/binaries/qio-backend-x86_64-pc-windows-msvc.exe` | 3617 条，含 materials | PASS | PASS：`materialsResolved=true`、材料 1 项 `source=inline` 正文读到 |

**真机端到端（修复构建 + 用户真实库副本 `D:\QIO-data\app.db`，真实模型 deepseek-v4.1-flash）**：

- 图片卡（已关联附件）→ `source=image_unsupported`，原因句「当前模型链路没有视觉能力……」；
  0.2.0 坏包在这里是 `materials=[]` 且整条解析模块缺失。
- 文件卡（挂用户真实 `简报_2026-06-30.docx`）→ `source=attachment_text`、`chars=4000`、`truncated=true`，
  正文首段真的被读出来（此前安装版这条链路从未生效）。
- 勾选的注释（正文已确认）→ `requirement.text` 就是那张卡的文字；0.2.0 里是 `""` 与「（无文字）」。

### 二、勾选的注释 QIO 看不到（问题三）

**现象**：用户写下的文字停在编辑草稿里（`board_drafts` 有 `card:c_mv4io4hf1uuka = "为什么链路不可用？"`），
而卡片正文 `content` 是空串；快照时间线为 `选择卡片 → 勾选注释 → 清空选择 → submitted`。
提交后的理解上下文 `requirement.text = ""`、卡片 label 是「（无文字）」，模型输出 `no_action`。

**根因**：卡片编辑框的输入只进草稿（`BoardCard.saveDraftInput`），只有点「完成编辑」才把正文写进卡片；
而 `store.submit()` 只调 `saveNow()`（保存板面），**不提升草稿**。后端只读 `card.content`
（`models.selectable_cards` / `materials.py`），因此"已勾选"的卡片在提交载荷里是空正文。
对照实验（用真实模型）：把同样文字确认进正文后再勾选提交，上下文里 `requirement.text` 与
`materials[].source="inline"` 都正常——**勾选语义没问题，断点在未确认草稿**。

**实现（策略由用户拍板：提交时把草稿提升为正文）**：`store.submit()` 在 `saveNow()` 之前把未确认草稿
写进该卡 `content` 形成候选板面，再走既有「保存 → 版本门 → 影响确认 → 提交」链路（`promoteDraftsBeforeSubmit`）。
边界与口径：

- 只提升**这次会进提交快照**的卡片；**未勾选的注释草稿一律不提升**（越过「勾选才允许 QIO 查看」= 越权）；
- 存在未决草稿冲突 / 正文内容冲突的卡片**不提也不提交**，给出可读原因，两份内容都留着；
- 旧草稿只通过既有 `requestDraftClear` 登记，真正的清除仍由 `saveNow` 按登记版本执行（没有第二套清理，
  用户随后又输入的新版本不会被误删）；
- 提交**结果未知**（没有拿到回执）时不声称「已生效」，并用既有 `setDraft` 把草稿候选放回；
- 计数只认「回执里的本次快照与提升后的正文逐字一致」的卡片，不谎报；
- 副作用（如实记）：影响确认未完成时，提升已经写进候选并保存成功，但本次提交被拦下 —— 用户会看到
  正文已是那段文字、提交未发生，可重新提交。

### 三、图片/文件卡片无法预览（问题二）

**事实**：互动板面**完全没有**附件打开/预览接线——全仓库 `openAttachment()` 只在
`Composer.vue` 与 `MessageItem.vue` 调用；`BoardCard.vue` 在关闭态只渲染一行
「还没有关联已保存的附件」文字，从不渲染图片内容，也没有可点的打开入口。能力本身是现成的
（`fetchAttachmentContent` / `openPlanFor` / `openBlobInNewTab` / 桌面壳的
`open_attachment_path` 与 `reveal_attachment_path`），缺的只是接线。

**实现**：卡片增加"查看/打开"入口（桌面走系统默认程序、可执行类只"在文件夹中显示"、浏览器走带认证
fetch 的 blob 新标签页），图片有关联附件时显示缩略图；没有 `attachmentId` 时保持既有诚实文案。

### 四、图片视觉能力（已接入，见第八节）

v0.2.0 里图片只能是 `image_unsupported`（"只登记元数据，不声称看懂内容"）：`ChatMessage.content`
是 `str`、`native.to_openai_messages` 只发字符串，凭据体系虽有 `vision` 用途标签却没有多模态通道。
本轮把这条通道补齐了 —— 契约见 `docs/interactive-vision-contract.md`，落地见第八节。

### 五、窗口审计与闪现修复（用户单独要求）

完整清单（37 条，含触发操作、代码位置、会开出什么、是否本意、无窗口标志、最小复现）见
`docs/window-audit-report.md`。结论摘要：

- **C 档（用户没做任何事也会闪）**：`reg query` 系统代理探测（`updater_net.rs:321`，每次启动 + 启动后 10 秒 +
  每 24 小时 + 点「检查更新」）与 `taskkill`（`ownership.rs`）都经 `run_command_with_timeout` 用
  `std::process::Command` 拉起且无创建标志；release 壳是 GUI 子系统（PE subsystem=2），于是每次都新分配
  并显示一个控制台窗口。实测：启动头 13 秒 4 次可见窗口（各 194–394ms），taskkill 约 1.17 秒；
  对照组只加 `CREATE_NO_WINDOW` → 0 次。
- **A 档（本意）**：原生文件选择器（两条入口：`attachment_picker.rs` 的 `GetOpenFileNameW` 与 WebView2 的
  `input[type=file]`）、系统默认程序打开附件、`explorer /select`、外链交给浏览器、安装器、第二个实例。
- **B 档（需确认/需批准）**：启动失败对话框（`QIO_STARTUP_ERROR_DIALOG=0` 可关）、agent 执行任意程序、
  以及后端子进程「靠继承隐藏控制台」这一结构脆弱点（后端自身 0 处 creationflags）。

**修复**：`ownership.rs` 的 `run_command_with_timeout()` 统一按 `CREATE_NO_WINDOW` 创建子进程
（`reg` 与 `taskkill` 两条路径共用这一个函数，一处改动全覆盖；`explorer.exe` 是 GUI 程序，不走这里、
无需改动）。

**实测（同一采样器、各自独立启动，只统计控制台/终端类可见窗口）：**

| 壳 | 启动全过程控制台类窗口事件 | 说明 |
| --- | --- | --- |
| 旧安装版 `D:\QIO\qio.exe`（0.2.0） | **10 条** | QIO 主窗口 2 条 + `reg.exe` 拉起的 WindowsTerminal 窗口 8 条（启动头 2 秒内） |
| 新构建 `target/release/qio.exe`（本次修复） | **2 条** | 只剩 QIO 主窗口自身；`reg` 的终端窗口归零 |

**未修（如实记）**：后端子进程的创建标志仍靠继承（结构脆弱点，见审计报告 B-3）；
`BoardCard` 的 url 卡片 `target="_blank"` 被 wry 默认吞掉（点了没反应）——属 UX 缺陷，非窗口问题，本轮未改。

### 六、验证与未验证项（P36）

- 前端：`npx vue-tsc --noEmit` 无诊断；全量 `npm test` **319 文件 / 2529 用例全绿**。
- Rust：`cargo test --lib` **31 passed**（含 `run_command_with_timeout` 的超时与输出用例）；
  `cargo check --all-targets`、`cargo test --no-run` 通过（无 error / unused 警告）。
- 后端：材料与互动区域定向全绿；`tests/test_docs_check.py` 在本轮修正「最后核对」日期后通过；
  受影响的附件 / 轮次 / 克隆回滚 / 互动共 10 个用例文件全绿（`test_r7_disconnect_cancel_verify` 多次重跑稳定通过）。

- **未验证（不粉饰）**：
  1. 窗口闪现修复是 Rust 代码改动，本轮只重建了后端 sidecar、**没有重建/重装 Tauri 壳**，
     所以「用户不再看到控制台闪现」尚无实机运行证据（代码级 + 对照实验证据在审计报告里）；
  2. 安装包（NSIS）本轮未重建，安装器的实机流程未跑；
  3. 草稿提升与附件预览没有真实界面走查（本环境不启动 Tauri）；组件/状态层有自动化覆盖；
  4. 视觉能力本轮**未做真实凭据的实机验收**（见第八节「未验证」）。

### 七、两条既有红的处置（本轮一并修掉）

**① `test_r7_disconnect_cancel_verify.py::test_disconnect_does_not_start_the_turn_after_gate_release`**
（契约 §1.1「准备期间的断连也是取消」）。根因不是断连没送到 —— 用 ASGI 层探针实测：
`http.disconnect` 在客户端 FIN 之后 0ms 就到达（日志：`receive#2 -> http.disconnect`），
而 `asyncio.wait(FIRST_COMPLETED)` 在 bind（磁盘拷贝在线程池里完成）先就绪时**立刻返回**，
监听任务虽然被唤醒却还没被事件循环跑到 → `any(w.done())` 为假 → 本轮被放行并调用模型。
对照实验：改动前 6/6 次都调用模型；给取消证据一个有界调度窗口后 6/6 次都判为取消、0 次调用。
第二段证据（观察窗实验）：断连与闸门释放间隔 0–2ms 时取消仍会漏（留下无人认领的克隆），
≥5ms 就干净 —— 说明还有一条「取消落在提交边界之后」的路径必须显式补偿。

修法（三处，都在既有语义上收口，不新增协议）：

1. `api/server.py::_prepare_with_cancel`：bind 完成后再让出**有界次数的任务切换**
   （`_CANCEL_EVIDENCE_TICKS`，只让出调度、不引入时间延迟、不轮询网络），再看取消证据；
   两条取消路径合并为同一条收尾（bind 必须真的被取消并等到它结束 —— 附件侧的整轮补偿挂在
   `CancelledError` 上，只取消不等待会留下无人认领的克隆）。
2. `services/attachments.py::bind_for_turn` 新增 `should_abort`（同步判据）与 `created_ids`：
   接受边界前再问一次「这一轮还要不要」，要取消就整轮补偿并返回空绑定；新建的克隆 id 如实
   暴露给调用方。
3. `api/server.py`：`/api/turns` 与 `/api/turns/resend` 都传同步判据；取消落在「已提交、未放行」
   的窗口里时，按 `created_ids` 回收这次新建的副本（只删自己新建的行，不碰既有行与别人的绑定）。

**② `test_interactive_during_heavy_work.py::test_maintenance_does_not_block_the_loop`**：
该用例写死 `100ms` 阈值，而同一文件其它用例已经改成「随刺激时长缩放」的预算；
执行器线程被 CPU 抢占造成的调度延迟（实测 116ms）会被误判成回归。改为同一口径
（`loop_stall_budget_ms(12 × 50ms)`，即 300ms 预算）：修复前 600ms 级的同步重活仍然抓得住，
环境地板不再造成假红。同文件的取消延迟断言一并统一。
### 八、视觉能力：图片真的交给模型看（2026-10-12 追加）

契约：`docs/interactive-vision-contract.md`（材料来源四种事实、唯一交付闸门、凭据选择、交付边界、
消息形状、测试清单）。落地：

* 消息形状：`agent/adapters/base.py` 新增 `ImagePart` 与 `ChatMessage.images`；
  `native.to_openai_messages` 把带图消息发成多模态数组（文本在前、`[图片] <标签>` 与 `image_url` 紧随），
  **没有图片时逐字保持字符串形状**（兼容只认字符串的端点）。native 适配器声明 `supports_images=True`。
* 材料层：`materials.resolve_images()` 是**能力无关**的读取层（只回答「字节读得出来吗」）；
  `resolve_materials(..., vision=...)` 在视觉可用时把图片记成 `image_delivered`（read=true），
  不可用时维持 `image_unsupported` 并写清原因（没有附件 / 取不到 / 状态不是 ready / 格式不在交付集合 /
  超过 8 MiB 上限 —— 超限**不截断**，因为半张图会给出错的画面）。
* 交付闸门只有一处：`understanding.build_context(..., vision=...)`；`vision=False` 时 `context["imageParts"]`
  必为空、材料必为 `image_unsupported`。`image_parts_from_context()` 只做形状转换，
  于是「这次交付了哪些图片」可从落库上下文重建。
* 凭据选择：带图时优先 `vision` 用途凭据（`AppContext.build_vision_adapter`），没有就如实回落主循环；
  回落之后能否交付图片仍由适配器声明决定，**回落不等于假装看得见**。
* 自动修复那一轮同样带图（否则等于让模型在看不见图的状态下重出 JSON）。

**测试**：`backend/tests/test_interactive_vision.py`（真实 PNG 字节、data URL 与原字节一致、无视觉时如实降级、
没有/取不到附件与超限各自的原因、不可见卡片不交付、纯文本形状不变、上下文闸门、上下文可重建内容块）。

**未验证（不粉饰）**：本轮没有真实视觉凭据，所以「某个具体模型看图的效果」没有实机验收 ——
协议形状、降级路径、交付边界与闸门都有自动化覆盖，但「模型判断质量」不在本轮证据范围内。


---

## P37 — 统一文字卡 · 持续许可 · 卡片内真实预览 · 多媒体对话（2026-10-12）

- **Status：** partial
- **基线：** 标签 `v0.2.1` = `65cdd7e3759fe44e12c6802a0cf41777bfc2e086`（远端与本地 SHA 一致，未改用 main）。集成分支 `feat/unified-cards-media-preview-021`；契约基提交 `dacc7cb`。五路并行（共享内容模型 / 统一文字卡与迁移与持续许可 / 真实文件预览 / 多媒体聊天 / 临时布局），各自独立 worktree 与分支，主智能体负责契约冻结、公共文件、顺序集成与最终验证。没有合并 main、没有发布版本、**只追加迁移 41/42/43**（历史迁移一字未改）。
- **契约：** `docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md`（冻结接口与数据形状）、`docs/superpowers/plans/2026-10-12-unified-cards-media-preview-plan.md`（方案 / 分工 / 验证清单）；互动语义见 `docs/interactive-mode-contract.md` §1.4 与新增 §1.4.1；多模态事实见 `docs/interactive-vision-contract.md`（新增 §8 音频与视频）；架构见 `docs/architecture.md` §15。

### 一、统一文字卡

- **只有一种字符卡**：文字卡。`backend/src/agent/interactive/text_card.py` 与 `frontend/src/interactive/textCard.ts` 是同一套规则的两侧实现，契约测试锁死三条性质（代码逐字保真、`serialize(parse(t)) === t`、块序稳定）。
- **一个连续编辑区**：编辑面是单个文本域，工具栏对选中文字或段落设置格式（加粗 / 斜体 / 行内代码 / 删除线 / 标题 / 列表 / 引用 / 链接 / 代码块），格式动作只改文本并把选区还给用户；在文字、链接与代码之间继续输入、移动光标、选择、撤销重做都由文本域自身承担。
- **卡片显示**：块各自渲染；代码块显示语言并可复制；链接显示地址、可编辑、可打开（沿用既有外链白名单）。
- **添加菜单与识别**：新建字符内容只有「文字卡」，默认未许可；代码文件仍属于文件材料（只读预览），与卡内代码块不是一回事。

### 二、历史卡片逐张迁移

- 旧 code 卡 → 一个代码块（正文逐字、语言保留）；旧 url 卡 → 一个链接块（地址与标题保留）；旧文字注释卡补齐块结构。**逐卡迁移，绝不合并**；id / 位置 / 分组 / 关系 / 书签 / 折叠 / 草稿键 / 任务引用全部保留。
- 迁移在板面状态的加载 / 保存层执行一次（`text_card_migration`，seq+1），**幂等**；历史快照表按原样可读。
- **可见范围不变**：旧注释保留原许可；旧代码 / 网址卡迁移后 `checked=true`（它们过去默认在材料范围内）。迁移不会让任何原本不可见的内容变成可见。

### 三、许可持续有效

- 一张文字卡只有一个整卡许可，覆盖卡内文字、链接与代码；未许可时**整体不交付**（链接地址、代码正文与派生文本都不进入提交载荷、材料解析与模型请求）。
- **提交不再取消勾选**：`checkedCleared` 恒为空数组（字段保留以兼容旧前端），直接路径与幂等重放路径一致；只有用户手动取消（或隐藏卡片）才失效。
- 遗留 code / url 卡在迁移完成前一律不可见。

### 四、卡片内真实预览

- 点卡片上的「展开预览」，这张卡片本身扩大阅读区域；「收起预览」收回；收起时仍显示文件名、类型与状态。
- 支持范围：图片（等比 + 放大 / 缩小 / 适应）、PDF（前端 pdf.js 逐页、保留原页面外观）、Word / Excel / PowerPoint（后端结构化转换 + 原卡片渲染）、代码 / 文本（行号 + 语言，只读）。**旧版 .doc / .xls / .ppt 不支持内嵌预览**，界面给可读原因并保留打开 / 另存入口。
- 转换与缓存：`attachment_previews` 按（附件, 版本, 配方）缓存，版本变化重新转换、旧结果绝不复用；转换在工作线程执行、可取消；超限标注截断；损坏 / 加密 / 未支持分别给不同原因，**不返回 500**。
- 阅读位置（页码 / 幻灯片 / 工作表 / 行 / 缩放）按附件 id + 版本落库，版本不符回默认。
- **预览 ≠ 模型已读**：`backend/tests/test_preview_real_files.py` 用同一份真实 .docx 同时给出「预览可用」与「材料是否读到正文」两条链路的结论对照。

### 五、临时布局（展开与让位）

- `frontend/src/interactive/cardLayout.ts` 是纯函数：展开卡原位放大，相交的未展开卡按最短方向让开（最多 8 轮，仍相交记为 `displaced`，不隐藏也不硬推）；多个展开卡之间互不让位。
- **只影响显示**：展开 / 让位 / 收起前后板面坐标与版本号逐字节不变，不产生表达，不触发模型理解或任务暂停；展开期间用户手动移动过的卡片，收起后停在新位置。
- 减少动画下完全不做过渡动画；展开状态下按 Esc 只收起预览。

### 六、多媒体对话

- 两处入口（对话模式聊天 + 互动悬浮聊天）共用同一套会话、附件与多媒体处理；悬浮聊天只发送当前消息与明确选定的附件。
- **能力以本次真实链路为准**：适配器声明 `supports_images` / `supports_audio` / `supports_video` / `audio_transcription`；没有能力就如实降级，绝不静默改用用户未配置的外部服务。
- 事实口径（八种 `source`，见 vision 契约 §8）：图片字节已交付 / 音频字节已交付 / 音频经派生转写（带时段与提供方）/ 视频字节已交付 / 视频按时间点抽样帧（带时间位置，明确「不是逐帧」）/ 各自对应的不支持与不可读原因。
- 派生处理需要本机可用的 ffmpeg，查找顺序与随包方式见 `docs/SETUP.md` §2.1；找不到时如实说明「这台机器上没有可用的转换程序，也没有随包提供」。
- 材料依据进入本轮回答以及需要再次调用模型的后续步骤（用记录请求的假适配器断言每一次调用都带着依据）。
- 互动板面提交本轮只完成图片：按提交时冻结的可见范围、材料身份与版本取图；隐藏 / 删除 / 未许可不通过派生结果绕过范围判断；视频与音频不在板面提交理解范围内。

### 七、数据库与接口

- 迁移 **41** `attachment_previews`、**42** `board_preview_state`、**43** 补偿入口（40 的全部语句 + 41 + 42）。文字卡内容不新增列（存在板面状态的卡片 meta.doc）。
- 接口：`GET /api/attachments/{id}/preview`、`GET/PUT/DELETE /api/interactive/boards/{boardId}/preview[/{cardId}]`；`GET /api/attachments/{id}/content` 的 Range 行为沿框架自带实现。
- `backend/src/agent/api/server.py` 只做两件事：挂载预览路由（一行）+ 补齐内联安全后缀（代码 / 文本一律 `text/plain`）。

### 八、验证

- 开发期定向：内容模型前后端契约测试；P1 后端（迁移 / 许可 / 材料）；P2 预览转换与真实文件端到端（六类格式 + 旧版 Office + 损坏文件 + 预览与模型读取分开）；P3 媒体服务与会话链路；P4 临时布局纯函数与接线。命令与结果记录在 `docs/superpowers/plans/2026-10-12-uc-ledger.md`。
- 全量本地验证与 CI：各执行一次，运行号与 SHA 回填在台账与交付报告里。

### 九、已知限制（不粉饰）

- Word / Excel / PowerPoint 的卡片内预览是结构化正文与表格预览，不是逐页原版面渲染；PDF 与图片保留原外观。
- 旧版 Office（.doc / .xls / .ppt）不支持内嵌预览。
- 音频转写与视频抽样依赖本机可用的转换程序与所选链路的真实能力；缺失或链路不支持时只给诚实结论（派生转写的实际交付范围以交付报告为准）。
- 「已交付模型」是请求侧的事实，不等于「模型理解正确」；抽样帧不等于逐帧读取；转写文字不代表理解了语气、音乐与全部环境声音。
- 真实模型 API 的联网验收不在自动化范围内：多模态链路的协议形状、降级路径与交付事实由 fake / mock provider 覆盖。

## P38 — 会话模式的图片交付缺口修复（2026-10-13）

### 一、问题（真实故障，用户侧观感是「无法识图」）

- 桌面端 0.2.2 里，在**对话模式**（含互动悬浮聊天）附一张 .jpeg，模型拿不到画面：
  它只收到「本轮附加了哪些文件对象」的通知，再通过 `read_attachment` 拿到尺寸/大小这类元数据，
  于是如实回答「我看不到画面」。
- 证据（真机数据）：附件 `state=ready`、`turn_id` 已绑定；该轮 `input_tokens` 只有一万出头
  （一张 65KB 图的 base64 远不止这些 token）→ **字节从未进入请求**；trace 的 `warnings` 为空。

### 二、根因（四处，缺一处都不成立）

1. 会话轮的媒体候选清单 `collected_media_candidates` 用 `classify_media_kind` 归类，
   而它按设计**只认音频 / 视频** → 图片归类为空 → 候选为空 → 没有内容块。
2. 媒体分派 `_media_candidate_entry` 没有图片分支：非音频的候选会**落到视频分支**去 ffmpeg 抽帧，
   回执写成 `video_unsupported`「没有抽到任何一帧」（实测）。
3. `media_facts_note` 没有 `image_delivered` 这一条，即使交付了也不会写进提示词。
4. 主循环 `core/loop.py` 本就能消费 `media_parts` 里的 `ImagePart`，但**对话路径没有生产者**：
   图片内容块只在互动（板面）路径构造，视觉凭据选择也只发生在互动 API。

### 三、修复

- `services/attachments.py`：`classify_media_kind(name, *, include_images=False)` —— 默认语义不变，
  只有会话媒体清单显式传 `include_images=True`；图片交付格式与上限沿用既有交付集合（png/jpg/jpeg/gif/webp，8MiB）。
- `interactive/materials.py`：收集器传 `include_images=True`；`_media_candidate_entry` 新增**图片分支**
  （有能力位就交付真实字节并记 `image_delivered` + `ImagePart`；没有能力位 / 格式不支持 / 超限一律 `image_unsupported`
  并给可读原因、不截断、不猜画面）；新增**显式 video 分支**，其余类型如实记 `unreadable`
  （历史反例：非媒体曾被当成视频抽帧）。
- `services/turn_orchestrator.py`：`media_facts_note` 增 `image_delivered` 一行（「真实字节已交给本次链路，由模型自己看」）。

### 四、边界（不粉饰）

- 对话轮**不**因为「有 vision 标签的凭据」而切换链路：凭据策略明确主循环优先 `main-loop`
  （`credentials/policy.py` 的注释记录了「专项凭据被主循环悄悄拿去用」这个历史问题）。
  能力位以**本次真实链路**为准；没有视觉能力就如实降级。
- 「已交付」是请求侧事实，**不等于**所选模型一定能看懂：这条仍由使用者的模型决定。
- 本轮只修「对话模式的图片」这一格；音频 / 视频原有行为不变。

### 五、第二个坑（用线上那张 65KB .jpeg 实测才发现）

- 候选拿到之后，交付又被拒了一次：回执 `image_unsupported`「不把无法确认媒体类型的字节当图片发给模型」。
- 原因：展示标签 `_label` → `_snippet` **只留 24 字符**，而 QIO 的附件副本名是「哈希 + 原名」（线上那张 52 字符），
  于是标签被截成「…」、**扩展名跟着丢**，按标签判断媒体类型必然判不出来。
  音频 / 视频之所以没暴露，是因为它们有 `audio/*` / `video/*` 通配兜底；图片按设计**不允许**猜 MIME。
- 修法：`_media_candidate_entry` 把候选里的**真实文件名**传给三个 builder 做媒体类型判断（`media_name`），
  展示文本仍用 `shown`。新增两条回归：长哈希名图片仍交付、长名音频拿到真实 MIME 而不是通配。
- 线上数据复核（拿真附件真轮次跑修复后的代码）：候选 `image` / 65138 字节 → `image_delivered` / `read=True`，
  内容块 `ImagePart(image/jpeg)`，解码后 **65138 字节与文件逐字节一致**，提示词里出现「图片「…」的真实字节已交给本次链路」。

### 六、第三个缺口（独立复审发现：读不到字节时静默丢弃）

- 复审用 100MB+ 的**引用型** .jpeg 实测：这类附件（`kind=reference`，>100MB 不复制副本）
  在 `content_target` 上必然报错，而收集器当时是 `except: continue` → **候选为空、连一句说明都没有**，
  模型完全不知道有这张图。这违反本项目「要么交付、要么如实说明」的口径。
- 修法：收集器取不到路径时**不再丢弃**，而是带 `path=None` + `unreadable_reason`（直接用服务端那句可读原因）
  继续走到 `resolve_media`，由它产出一条 `image_unsupported`（音频 / 视频同理）；
  `_media_candidate_entry` 优先采用候选带来的具体原因；同时修掉自己引入的一处 `Path(None).stat()` 崩溃。
- 新增回归：引用型大图 → 有 1 条候选（`path is None`、带原因）→ `image_unsupported` 且不交付内容块。
- **同一类静默丢弃还有一处**（复审用「删掉副本」实测）：`state != "ready"` 的附件原来也是直接 `continue`
  → 候选为空、没有任何说明。现在一并改成带可读原因（「这个附件当前不可用（…）」）继续走交付判定。
- 新增回归两条：引用型大图、副本丢失（`state` 非 ready）都必须给出带原因的 `image_unsupported`。
- 记进契约：`unreadable` 作为合法的 `source` 值写进 vision 契约 §8.2（原表只列了音视频与图片）。
- 仍未覆盖（如实记）：同一入口另有 `limit=50` 的截断（第 51 个附件会被静默略过，与 `read_attachment` 同源上限）；
  前端 `components/mediaFacts.ts` 对未知 `source` 会渲染成「尚未识别的来源取值」，未在本轮补 —— 它不在会话媒体链路上。

### 七、第四个缺口（复审指出：互动板面「文件」卡里的图片）

- 现象：在板面用**「文件」入口**加一张 .jpeg → 走 `_attachment_entry` 的 `mode == "image"` 分支，
  那里**硬编码**「当前模型链路没有视觉能力」且从不读字节 —— 链路其实有视觉能力时，这句话是假的，
  用户也永远拿不到画面。图片卡（`kind="image"`）没有这个问题，只有文件卡走了这条老路。
- 修法（两处必须同时改，否则会出现「标了已交付却没有内容块」的假账）：
  1. 把图片的交付事实拆成共用核心 `_image_fact`（图片卡与文件卡共用同一份判断与措辞），
     `vision` 从 `_resolve_card` → `_file_entry` → `_attachment_entry` 一路传下去；
  2. `resolve_images` 也接受 `kind="file"` 的卡片 —— `_deliverable_image` 只认可交付图片类型，
     所以 docx / pdf / txt 这类文件卡天然返回 None，不会误交付。
- 新增 4 条回归：文件卡图片在有 / 无视觉能力下的两种事实（有视觉能力时不许出现「没有视觉能力」）、
  非图片文件卡不许标成 `image_*`、以及上下文档（`build_context`）里真的带上 `imageParts` 并可重建内容块。

### 八、验证

- 新增 `backend/tests/test_chat_image_delivery.py`（17 条，**不需要 ffmpeg**：图片夹具是内嵌的真实 JPEG/PNG 字节）：
  候选清单认图片（根因回归）、交付字节与文件**逐字节一致**、无能力位如实降级、超限不截断、
  格式不支持点明格式、非媒体不被当视频抽帧、事实文案、以及真实流水线的两条端到端
  （每次模型调用都带着图片 / 没有能力位时在提示词里如实说明）。
- 受影响套件（媒体族 + 互动材料 + 视觉 + 冻结产物打包 + 新用例）共 96 条全过。
- 尚未在真机安装版上复验（需要出 0.2.3 并走发布闸门）。
### P36 — 互动空间结构与自由尺寸调整（2026-10-13 第十一轮）

- **Status：** completed
- **Implementation：** 卡片与互动对话窗口都能自由调整尺寸（四条边 + 四个角 + 键盘 −/＋），
  尺寸调整只改布局与查看 —— 不产生表达 / 意图 / 审批 / 模型调用，不取消持续许可，不动草稿与阅读状态。
  - 公共原语：`frontend/src/interactive/resize.ts`（八向算术、最小/最大夹紧、恢复收敛、新卡落点）、
    `frontend/src/composables/useResizeGesture.ts`（指针手势；取消即恢复：指针取消 / Esc / 失焦 / 卸载）、
    frontend/src/components/interactive/ResizeHandles.vue（2026-10-26 已删除，卡片缩放热区由 BoardResizeLayer 承接）（四边四角热区 + 右下角克制标记 + 键盘可达的 −/＋）、
    `frontend/src/interactive/chatFrame.ts`（对话窗口位置/尺寸按**板面身份**绑定的本机偏好）、
    `frontend/src/interactive/chatFrameState.ts`（显示框共享桥）、
    frontend/src/interactive/overlayAvoid.ts（2026-10-26 已删除，无生产消费者）（浮层互相让开的带宽几何）。
  - **卡片常态尺寸就是板面状态的 `card.w/h`**：常态提交 `{x, y, w, h}`；展开中或被让位时只提交 `{w, h}`，
    临时槽位坐标**绝不**写回板面。后端 `submission.py` 的 `_card_meaning` 不含位置/大小，
    纯几何变化归入 `DISPLAY_ONLY_EXPRESSIONS`（`layout_only`），因此不产生意图。
  - 展开让位 / 组框 / 关系线共用**同一份显示坐标**（`frontend/src/interactive/cardLayout.ts` 的
    `planExpandedLayout` + `frontend/src/components/interactive/BoardCanvas.vue` 的
    `displayCards` / `displayGroups`）；展开卡之间互相避让，被让位的卡片之间出现的新碰撞再迭代化解。
    可用区域扣掉平移/滚动后的可视起点、缩放、自绘标题栏、底部工具栏、对话窗口
    （`chatObstacleFrame()`）与审批浮层。
  - 新卡落点改为「当前可见范围的空白位置」（`findFreeSlot` / `planNewCardSlot`），不再按固定四列 + 数量取模。
  - 对话窗口精简为「标题 + 一句运行状态 + 尺寸调整 + 帮助 + 收起 / 连续消息流 / 一个输入区」；
    「发送不提交板面」是短提示，完整可见范围、媒体读取、附件限制与快捷键进帮助面板。
  - 板面提交状态只显示**最新**事实，旧阶段进可展开历史；待审批时提交区的审批入口无需展开详情即可操作。
- **Tests：** `frontend/src/interactive/__tests__/`（resize / chatFrame / chatFrameState / overlayAvoid /
  irl-c-layout / irl-c-overlay-chat-frame / irl-verify-resize / irl-verify-chatframe）、
  `frontend/src/composables/__tests__/`（useResizeGesture / irl-verify-gesture）、
  `frontend/src/components/interactive/__tests__/`（resizeHandles / irl-lead-tray-avoid / irl-a-chatDock /
  irl-b-card-states / irl-c-canvas / irl-c-new-card / irl-d-submit-status / irl-d-approval-access /
  irl-verify-handles）、`frontend/src/stores/__tests__/irl-d-status-identity.test.ts`。
  真实应用验收脚本 `scripts/verify-irl-shots.mjs`，截图在 `docs/verification-shots/irl/`。
- **Known limitations：** 真实应用检查在 Windows 无头 Edge + dev server 上完成，
  **未在打包后的桌面窗口上复验**（0.2.3 自绘标题栏的真实高度、真实 PointerEvent / 触控、
  输入法组合跨「收起销毁 / 展开重建」都未实测）；临时显示布局（展开或被让位）里的西 / 北把手按左上角锚定，
  折叠卡片与部分 `height: auto` 状态不摆把手；对话窗口的位置与尺寸是本机偏好（按板面身份绑定），
  不做跨设备同步；假厂商端到端对话本轮未跑，审批与聊天过程只做了结构与状态复核；
  `frontend/src/components/__tests__/AttachmentActions.test.ts` 里那条 Composer chip 用例
  曾观察到失败（当时判断为「基线同样失败」）；2026-10-14 在冻结基线 `17f28f7b` 的工作树上
  单独复跑该文件为 **10/10 通过**，因此旧结论不成立（更可能是用例间本机存储残留造成的偶发），
  本轮已给该文件补上 `localStorage.clear()` 的用例隔离，并把「基线版本测试文件在集成树上同样通过」
  也复跑确认过。
- **后续依赖：** 打包桌面窗口的真实避让复验、多设备尺寸偏好同步、把尺寸偏好写进服务端用户数据，
  这三件事从 `docs/interactive-mode-contract.md` §15 的边界接手。

### P39 — 统一滚动体验 / 对话不横向滚动 / 表格适配（2026-10-14 第十二轮）

- **Status：** completed
- **Implementation：** 覆盖 QIO 自有界面的滚动规则收敛，权威约定见
  `docs/superpowers/specs/2026-08-09-qio-frontend-design.md` §15、`docs/interactive-mode-contract.md` §16，
  逐区域覆盖见 `docs/global-scroll-coverage.md`，合并材料见 `docs/global-scroll-merge-prep.md`。
  - **共享原语（唯一实现）**：`frontend/src/styles/scroll.css`（全局消除原生滚动条外观，不设全局
    `overflow:hidden`、不禁用滚轮）、`frontend/src/composables/useQioScrollbars.ts`（auto/hidden 两策略的
    浮动指示条引擎）、`frontend/src/composables/useQioTables.ts` + `frontend/src/utils/qioTable.ts`（表格两态）、
    `frontend/src/utils/scrollIndicator.ts`（指示条偏好）、frontend/src/components/ui/QioScrollArea.vue（2026-10-26 已删除，滚动指示条由 v-qio-scrollbar 承接）；
    令牌集中在 `frontend/src/styles/tokens.css` 的 `--scroll-*`。
  - **对话内不得横向滚动**（取代此前「代码块可以横向滚动」的写法）：消息流、消息项、气泡、正文、过程详情、
    附件、输入区、错误恢复与对话内预览都不是水平滚动容器；代码块软换行（`white-space: pre-wrap`，
    复制/下载仍是原字符序列）；输入区软换行、到上限后只纵向滚。
  - **表格适配**：`frontend/src/components/MarkdownContent.vue` 产出列身份（`th[id|scope|data-col|data-label]`、
    `td[headers|data-col|data-label]`、隐藏 caption），按**实际可用宽度 + 列结构 + 内容真实最小宽度**
    （带 88–112px 滞回）在「表格」与「纵向记录」间切换；记录模式保留原记录顺序、列顺序与表头，
    并保持辅助技术可辨认。记录模式只用于存在真实表头行的表格。
  - **区域收敛**：普通对话与互动对话各自只保留一个主要纵向滚动视口（过程、通知、附件摘要、帮助改为
    自然展开 + 展开入口）；板面滚动区用 hidden 策略（不画条但保留平移/缩放/坐标换算）；
    卡片展开阅读只保留一个纵向视口；独立材料预览的二维浏览区仍可横向并走统一指示条；
    设置页新增「QIO 滚动指示条：自动显示 / 始终显示」（默认自动，不作用于板面、不开放对话横向滚动）。
- **Tests：** `frontend/src/styles/scroll.test.ts`、`frontend/src/utils/__tests__/qioTable.test.ts`、
  `frontend/src/composables/__tests__/qioScrollbars.test.ts`、`frontend/src/composables/__tests__/useQioTables.test.ts`、
  `frontend/src/components/__tests__/gs-qio-chat-scroll-fit.test.ts`、`frontend/src/components/__tests__/gs-scroll-policy.test.ts`、
  `frontend/src/components/interactive/__tests__/scroll-board-viewport.test.ts`、
  `frontend/src/components/interactive/__tests__/scroll-horizontal-fit.test.ts`，
  以及 `MarkdownContent.test.ts`、`MarkdownStreaming.test.ts`、`Composer.test.ts`、
  `irl-b-card-states.test.ts` 等既有用例按新规则更新（旧断言「代码块可以横向滚动」被反转并写明原因）。
  真实浏览器证据在 `D:\qio-dev\_gs-verify\`（Lead 探针与独立复核，跨 1200×800 / 800×600 / 亮暗主题）。
- **Known limitations：** 滚动条占位判定在无头浏览器里是 overlay 形态，未做像素级判定；
  125% / 150% 显示缩放用 CSS 视口等价换算验证，未在真实 Windows 缩放设置下复验；
  未在打包后的 Tauri / WebView2 窗口里验收（自绘标题栏、真实 PointerEvent、输入法组合、触控板横向手势仍待验证）；
  `frontend/src/components/interactive/CardInlinePreview.vue` 的 `context="chat"` 分支当前只有单测覆盖，
  对话里还没有调用点；该轮改动已于 2026-10-14 合并进 main（`3a4f256e`）。
- **后续依赖：** 合并顺序与冲突点见 `docs/global-scroll-merge-prep.md` §7；
  合并前必须重新核对 main 差异并重跑门禁（本轮的结论只对冻结基线 `17f28f7b` 成立）。

### P40 — 0.2.5 互动模式两处回归修复：附件成功提示常驻 / 连接点被尺寸热区遮挡（2026-10-14）

> 编号说明：本轮原本也写成 P39，但 P39 已被先合入 main 的「统一滚动」一轮占用，
> 合并时按「先入 main 者保留编号」改成 P40；正文内容与来源分支一致，未改写。

- **Status：** completed
- **Implementation：**
  - 新增临时提示生命周期原语 `frontend/src/composables/useTransientNotice.ts`：进行中一直显示、成功约 3 秒后自动移除、
    失败不自动消失且可关闭 / 重新操作；每次 `begin()` 作废旧提示与旧计时器，迟到结果带 token 写不进来，组件卸载即失效。
  - 附件入口 `frontend/src/components/interactive/AddMenu.vue` 与卡片内关联 `frontend/src/components/interactive/BoardCard.vue`
    共用这一份原语：成功只陈述「附件已保存 + 板面关联是本地改动」，不再声称服务端已保存；
    取消文案按卡片当前是否已有附件区分；等待选择 / 上传 / 处理都是真实进度；长文件名换行并可查看完整名称；
    工具栏入口的提示与菜单放进同一悬浮列、各占一行且整层不吃指针事件，因此不会挡住「添加」与其它主要操作。
  - 连接点与尺寸热区统一进**同一份不随画布缩放的交互层**（`frontend/src/components/interactive/BoardResizeLayer.vue`，
    位置 = 显示矩形 × 缩放）：四边中点连接点有独立命中区（屏幕像素不小于 18×18）且位于边 / 角热区之上，
    视觉仍是原先的圆点尺寸与既有颜色令牌；尺寸热区（边 / 角）不缩小。卡片内部不再有连接点，
    从结构上不可能被缩放层里的热区盖住。
  - 拖线目标判定改用**当前显示矩形**（`frontend/src/components/interactive/BoardCanvas.vue` 的 `displayRect`）而不是正式保存矩形：
    展开卡片、因展开而临时让位的卡片、调整尺寸后、画布平移缩放后都按当前可见区域命中，旧位置不再命中；
    临时显示坐标只用于渲染，不写回板面。
  - 同一指针只驱动一种手势：连接点按下会清掉拖动 / 平移 / 框选 / 尺寸预览，拉伸开始后不再拖线；
    拖线取消、Esc、指针取消、失焦、组件卸载都会清理临时状态并恢复尺寸调整能力，不留半条关系。
  - 同源坐标缺陷一并修正：拖线落点 / 框选判定 / 局部工具栏定位 / 合并提示定位 / 滚轮缩放锚点原来把滚动量减了两次
    （滚动为 0 时碰巧正确），统一成「内容原点 + 平移量 0」的口径后，先平移再拖线 / 框选 / 缩放都落在正确位置。
  - 框选虚线自身的渲染也从「视口相对像素」改成与判定同一条来源的**板面矩形**：它画在带缩放的内容层里，
    原来平移后整块虚线偏「滚动量」、缩放 ≠ 1 时还会被二次放大 —— 现在虚线框与真正选中的卡片始终是同一件事。
- **Tests：** `frontend/src/composables/__tests__/useTransientNotice.test.ts`、
  `frontend/src/components/interactive/__tests__/fix025-attach-notice.test.ts`、`fix025-card-attach-notice.test.ts`、
  `fix025-connect-resize.test.ts`、`fix025-independent-verify.test.ts`；既有 `irl-verify-handles` / `resizeHandles` /
  `boardCanvas` / `irl-c-canvas` 等套件继续通过。真实应用验收用 Playwright 真实鼠标（不是 dispatchEvent）：
  四个连接点中心 `elementFromPoint` 命中连接入口、按下进入拖线、25%–250% 缩放与平移后的命中与位置、
  四边四角仍可调整尺寸、附件提示约 3 秒后移除。
- **Known limitations：** `.card-toolbar` 仍挂在带 `transform: scale()` 的 `.board-surface` 内部（工具栏自身随画布缩放，
  且行内坐标与屏幕差值的口径不一致），本轮未修改；框选虚线的边框宽度仍随画布缩放（内容层内 1px 边框在 25% 下约 0.25px，
  选区本身与位置都正确）；多指针（双指 / 多鼠标）场景只有单测级守卫，未真机复验；
  打包后的桌面窗口未复验；`frontend/src/components/__tests__/AttachmentActions.test.ts` 里那条 Composer chip 用例
  曾观察到失败并一度判断为「基线同样失败」；2026-10-14 在冻结基线 `17f28f7b` 的工作树上单独复跑为 **10/10 通过**，
  旧结论不成立（更可能是用例间本机存储残留造成的偶发），已按统一滚动一轮的做法补上用例隔离。

### P41 — 板面操作提示不再常驻；说明并入帮助（2026-10-14 第十三轮）

- **Status：** completed
- **Implementation：**
  - `frontend/src/components/interactive/BoardCanvas.vue` 里的 `.view-hint` 原来是**无条件渲染**的常驻提示，
    与互动空间既有规则冲突（frontend/src/components/interactive/ChatDock.vue（2026-10-26 已随旧互动展示层删除） 里写着「快捷键说明只留一处
    （帮助里），面板里不再常驻小字」）。现在改成：只在本机**还没用过查看手势**时显示一条，
    平移 / 滚轮缩放 / 空格框选任一手势**结束**后永久收起 —— 在手势结束后才收，避免拖动中途改变容器高度；
    状态记进 `localStorage` 的 `qio-board-hint-seen`，读写沿用项目既有的防御式范式
    （隐私模式 / 企业策略下连读属性都可能抛异常，抛了就退回本次会话内存）。
  - 该提示去掉 `role="status"`：它不是实时状态，而文案里嵌着实时缩放百分比，
    作为 aria-live 区域会让读屏在每一次滚轮缩放时重复播报整条提示。
  - 完整说明并入帮助：frontend/src/components/interactive/ChatDock.vue（2026-10-26 已随旧互动展示层删除） 的帮助里新增「板面手势」一段
    （`data-im="chat-help-board"`），摘要行同步改为「帮助：可见范围 · 板面手势 · 媒体读取 · 附件限制 · 快捷键」。
- **Tests：** `frontend/src/interactive/__tests__/boardCanvas.test.ts` 新增一组用例（首次进入显示且不是 aria-live、
  平移后收起并把「已看过」写进本机、缩放后收起、框选后收起、已看过时不再显示）；
  frontend/src/components/interactive/__tests__/irl-a-chatDock.test.ts（2026-10-26 已删除） 的帮助用例补上板面手势断言。
  既有「拖动空白处 = 平移查看位置」用例原先靠 `toContain("平移")` 间接依赖这条常驻提示，
  改为断言「平移不留下任何提示条」。真实浏览器（Chrome，1200×800）核对：
  首次进入有提示 → 空白处滚轮缩放后消失且本机标记写入 → 刷新后不再出现。
- **Known limitations：** 本轮只在本地 main 上提交，**未推送**（按用户要求不再触发新的全量 CI）；
  「已看过」标记按浏览器存储，换浏览器或隐私模式会各自重新显示一次。
- **后续依赖：** 排查中还定位到另一个问题：板面里除空白表面以外的按下点
  （卡片正文、底部提交区、工具栏等）没有 `preventDefault()`，浏览器会启动原生文本选择拖拽，
  指针越过滚动容器边缘时触发 Chromium 的选择自动滚动，把 `.board-viewport` 滚到底
  （实测滚动量 300 → 911，并选中 162 字符；与是否按住 Ctrl 无关）。
  本轮**未修**（当时修法待定）—— **已在 P50 按方案 A 修好**。

### P64 — 底部功能栏布局重构：按实际可用宽度分档 + 两行主栏 + 三类互斥展开面板（2026-10-15 第三十六轮）

- **Status：** completed（分支 `fix/interactive-bottom-toolbar-layout`，基线 `6e1d4cd4`，**尚未合并 main**）
- **Implementation：**
  - **根因（静态 CSS 链 + 真实渲染实测）：** 功能栏的行列关系原先由**视口**媒体查询决定
    （`BoardToolbar.vue` 的 `@media (max-width: 1080px)`、`SubmitCluster.vue` 的 `≤900 / ≤620`）。
    功能栏实际宽度由 `max-width: min(--im-toolbar-max-width, 100% − --im-toolbar-chat-reserve − 两侧留白)`
    决定，所以「窗口很宽、但聊天保留宽度把功能栏压到 764px」时两条断点都不触发，仍按宽档挤一行。
    提交区的长说明正文（`.u-detail`）同时具备 `flex: 1 1 18ch` + `min-width: 0` +
    `overflow-wrap: anywhere`（`anywhere` 参与固有尺寸计算，min-content 可降到「一个字」），
    而同行 `.range/.status` 是 `nowrap`、`.actions` 是 `flex:none` —— 唯一能继续缩的就是正文。
    `.u-line → .submit-cluster → .tb-submit → .tb-main → .board-toolbar` 五层都没有高度上限，
    于是「正文被压成极窄竖列」与「整条功能栏被撑高」同时发生。基线实测：764px 下功能栏 151px、
    1178px 下 106px；同一段说明在 764px 下 229px 宽（2 行），在 1178px 下反而只有 146px 宽（4 行），
    3 张卡时 80px 宽（6 行）——「越宽越窄」。
  - **分档改为实测功能栏自己的可用宽度：** 新增 `frontend/src/interactive/toolbarLayout.ts`
    （纯函数 `toolbarBand`：wide / mid / narrow 三档 + 40px 滞回；`toolbarAvailableWidth` 用两个隐形
    探测器量 `--im-toolbar-chat-reserve` 与两侧留白，再与 `--im-toolbar-max-width` 取小 —— 与 CSS 的
    `max-width` 同源；那两个令牌是 `min()/vw` 串，`getPropertyValue` 拿不到像素，必须用探测器）。
    `BoardToolbar.vue` 用 `ResizeObserver`（舞台 / 功能栏自身 / 保留宽度探测器）驱动 `data-tb-band`。
  - **两行主栏：** `.tb-submit` 与 `.submit-cluster` 都是 `display: contents`，
    `.sc-history / .sc-stats / .sc-notes / .actions` 各自声明 `grid-area`，由 `BoardToolbar` 的
    `grid-template-areas` 决定每一档的行列关系（wide：编辑 | 上次提交、统计 | 操作；
    mid：上次提交整组独占一行；narrow：提交操作组独占一行并右对齐）。
    「上次提交」与「说明区」两块都是按状态出现的，不出现时用 `:has()` 收掉那一行 ——
    写死的网格行即使没有网格项也会白占一条 `row-gap`（实测 0 高行仍让主栏多 4px）。
  - **三类互斥展开面板：** 原来的单一 `detailsOpen` 被 `openPanel` 取代，
    「上次提交结果 / 本次提交范围与改动 / 上次提交过程」各有自己的面板与 `aria-controls`，
    同一时间只开一个、同一入口再点收起、Esc 收起。面板 `position:absolute; left/right:0;
    bottom:calc(100% + …)`：宽度 = 功能栏完整宽度、不参与主栏高度，限高 + 唯一一层纵向滚动
    （`v-qio-scrollbar` + `data-qio-fit`）、横向 `clip`。三块数据分家：结果面板只取提交 / 理解记录
    （`understanding-no-action` 优先显示服务端真实 `summary`），详情只有本次范围与未提交改动，
    过程面板只读渲染 `understandingHistory / pastNotes`。
  - **视觉与可达：** 撤销 / 重做改图标按钮（保留 `title` 与 `aria-label`，禁用态更清晰）；
    功能栏左侧与底部留白同源（`--im-toolbar-left` / `--im-toolbar-bottom`）；
    功能栏宽度改为取满可用宽度（参考图里宽档就是撑满、两组内容各贴左右两侧）；
    面板过渡用动效令牌（`--dur-menu` / `--ease-1`，reduced-motion 由令牌归零）。
- **Tests：** 新增 `frontend/src/interactive/__tests__/toolbarLayout.test.ts`（三档 + 滞回 + 量不到不误判）、
  `frontend/src/components/interactive/__tests__/boardToolbarBands.test.ts`（栅格契约 / 空行收口 /
  面板定位）、`submitClusterPanels.test.ts`（三类面板互斥、aria 指向、数据不串用、默认无第三行）；
  更新 `closure-c-visual-guards.test.ts`（不再要求靠 `-webkit-line-clamp` 收窄正文，改为钉
  「不用省略号隐藏唯一可读正文」）、`irm-d-status-ui.test.ts` 与 `irl-d-submit-status.test.ts`
  里锁定旧界面形状的断言。
- **验证：** `npx vue-tsc --noEmit` 0 错误；前端全量 `vitest run` 全绿。真实渲染（1600×900，
  独立后端 8803 / 前端 5303 + 全新数据目录，`scripts/visual_probe.mjs`）：
  764px 可用宽 → `data-tb-band=mid`、功能栏 764×91、`.tb-main` 高 73 且只有 2 条网格轨道；
  1178px → `band=wide`、功能栏 1178×91、`.tb-main` 高 73、2 条轨道；
  两档主栏与页面横向溢出均为 0，控制台错误 0、失败请求 0；
  两档点开「详情」面板时功能栏 rect **完全不变**（764×91 / 1178×91），面板宽 762 / 1176
  （= 功能栏完整宽度）、面板横向溢出 0、高度受上限约束；再点收起回到同一 rect。
- **Known limitations：** 本轮只在分支上提交，**未合并 main、未打包发布**；
  narrow 档的视觉顺序与 DOM 顺序不一致，Tab 顺序按宽档的视觉顺序走；
  两档阈值（900 / 560 + 40 滞回）来自 764px 与 1178px 的真实渲染实测，
  遇到明显更长的真实文案应重新量一次。

### P63 — 卡片表面收敛（文字卡 / 文件卡）+ 西·北把手拉不动修复（2026-10-14 第三十五轮）

- **Status：** completed
- **Implementation：**
  - **「向左 / 向上拉伸没反应」的根因（真实浏览器复现，不是注释推断）：** `BoardCanvas.vue` 的 `planCards`
    为了「拖动中展开底衬跟手」调了 `displayRect`，而 `displayRect` 的槽位来自 `layoutPlan` —— 两者成环，
    槽位恒慢一帧。西 / 北把手的**预览框位置**因此取到上一帧的槽位：向左拉时左边界不动、右边界反而向右
    （向上拉时下边界向下）。提交本身一直是对的（松手后位置正确），错的只有拖动过程中的预览。
    改为 `planCards` 只认这一帧的实时值（拖动坐标 / 尺寸预览框），不再回调 `displayRect`，环断开。
  - **文字卡编辑面收敛**：卡片正文区里只有「格式工具栏 + 输入框」，输入框吃满卡片 ——
    `TextCardEditor.vue` 新增 `fill` 档（`flex: 1 1 0%` / `overflow: auto` / `resize: none`，
    非 fill 的独立挂载行为不变），`BoardCard.vue` 正文区在文字卡编辑态加 `card-body-editing-text`（flex 列）。
    删掉 markdown 语法说明与「输入过程只保存草稿」两段常驻文字。
  - **完成编辑 = 退出选中 / 再点一次编辑按钮**：工具栏的编辑按钮在编辑态变成「完成编辑」；点板面空白或
    点别的卡片（`selected` 变 false）同样收尾。没有真实改动时不产生板面变更；未决冲突仍保留选择入口，
    退出编辑不替用户做选择。
  - **换行修复**：`inlineHtml` 把段落 / 引用块里的 `\n` 渲染成 `<br />`（此前被 HTML 折叠成一行）。
  - **文件卡只显示文件名**：卡片本身只留文件名（标题；`meta.name` 缺失时退回附件名），去掉「待补充文件说明」
    输入框、附件信息行、卡片内预览与完成 / 取消按钮行；「完成编辑 / 重新关联附件 / 查看」全部收进卡片工具栏，
    「查看」走既有 `openAttachment`，结果如实显示在工具栏状态行（打不开就说打不开）。图片卡（含封面）不受影响。
- **Tests：** 新增 `frontend/src/components/interactive/__tests__/card-minimal-surfaces.test.ts`（8 条：文字卡极简
  编辑面 / 完成编辑两条路径 / 无改动不产生变更 / 换行渲染 / 文件卡关闭态与编辑态 / 工具栏查看成功与失败）；
  更新 `irl-verify-size-semantics`、`n6-incomplete-input-fields`、`irl-b-card-states`、`fix025-card-attach-notice`、
  `image-cover-card`、`p1-closed-card-shows-promoted`、`final-a3-01`、`final-a3-05`、`d4CardDraftRace`、
  `recovery-a-integration-n6-store-input`、`recovery-a-n6-board-card-restore`、`scroll-board-viewport`、
  `textCardEditor` 里锁定旧界面的断言。
- **验证：** `npx vue-tsc --noEmit` 0 错误；`npx vitest run` 全绿。真实浏览器（Playwright + 本机 Edge，
  指向运行中的 5396 实例）：西把手向左拖 60px → 左边界 −60、右边界不动（改前：左边界 −10、右边界 +50）；
  北把手向上拖 60px → 上边界 −60、下边界不动；文字卡编辑态输入框底边与卡片正文区底边对齐、无任何说明文字；
  文件卡关闭态只有文件名，编辑态只有名称输入框，工具栏为「完成编辑 / 重新关联附件 / 查看 / 折成标题 / 更多」。
- **删除「不是原版面」说明行（2026-10-14 用户要求）：** 卡片内预览不再显示
  「结构化正文预览，不是原版面：按正文顺序给出段落、标题、列表与表格的文字」（docx / xlsx / pptx 三处一并删除）。
  `CardInlinePreview.vue` 去掉 `noticeText` 与 `cip-notice` / `cip-xlsx-facts` / `cip-pptx-facts` 三行，
  三个渲染器去掉对应的 `*_NOTICE` 常量与 `notice` 视图字段（渲染结果本身不变，只是不再在界面上写这句话）。
- **Markdown 文件卡片做解析（2026-10-14 用户要求）：** `.md / .markdown` 附件不再按纯文本逐行显示，
  改为渲染成正文（标题 / 列表 / 引用 / 表格 / 代码块 / 行内格式）。后端分类与载荷一个字没改（仍按 `code`
  类别给文本行）：`previewPlan.rendererFor` 按扩展名把它分到新的 `markdown` 渲染器，
  `renderers/markdown.ts` 直接复用 `code` 的视图解析（只有一处实现），真正的解析由组件用
  `MarkdownContent`（remark-gfm 管线，链接白名单、代码高亮）完成。Markdown 没有页码 / 缩放 / 工作表 /
  幻灯片这类阅读位置，因此不写阅读位置记录。
- **对话输入框下方的帮助整块删除（2026-10-14 用户要求）：** 互动模式对话面板里，输入框下方那条
  「文字发送不会提交板面 / 帮助：可见范围 · 板面手势 · 媒体读取 · 附件限制 · 快捷键」不再渲染
  （`chat-scope` 摘要与 details 正文一起删掉），标题栏的「帮助」按钮一并删除 —— 它已经没有任何可打开的内容。
  `helpOpen` / `onHelpToggle` / `SHORTCUT_TEXT` / `scopeText` / `hintText` 与 `.help*` / `.panel-hint` /
  `.panel-icon` 死样式一起清掉；输入区（附件入口 / 输入框 / 发送）与短窗口下的让位规则一个字没动。
- **Known limitations：** 文件卡的收敛只覆盖 `kind="file"`；图片卡（含封面变体）按原样保留。真机 Tauri 壳内
  未验证（本轮只在浏览器实例上验证）；`content` 字段仍在数据模型里，只是文件卡不再编辑 / 显示它。

### P62 — 局部工具栏与卡片不再分离：独立不缩放浮层 + 真实 DOM 外框锚点（2026-10-14 第三十四轮）

- **Status：** completed
- **Implementation：**
  - **根因（三条，全部由真实浏览器几何证实，不是注释推断）**：
    ① `.card-toolbar` 由 `frontend/src/components/interactive/BoardCard.vue` 的片段根渲染，DOM 上落在
    `.board-surface`（`position: relative` + `transform: scale()`）内部；定位写入的是**客户端像素差值**，
    被祖先再乘一次 —— 实测 250% 时行内 `left: 1997.5`、屏幕 `left: 4787.75`（= −1081 + 1997.5 × 2.5），
    工具栏自身也被放大（宽 337.59 → 843.98、高 35.25 → 88.13）。
    ② 刷新定位用的是**正式板面坐标**，而卡片本体与交互层走 `displayRect()`（拖动实时坐标 / 尺寸预览 / 展开槽位）；
    展开、让位、拖动、尺寸预览之后两套位置不一致 —— 这就是「拖动中正确、松手后又偏」。基线还算出了可见性却从未
    传给卡片，完全离屏也不隐藏。
    ③ 旧的落点纯函数会竖向翻转、横向夹取、必要时侧移，与「固定相对位置」直接冲突。
  - **唯一一套位置规则**：工具栏左边缘 = 所属卡片**实际渲染外框**的左边缘（0 偏差）；底边在卡片顶边上方
    **10 屏幕像素**；删除夹取 / 翻转 / 侧移 —— 任何视口边缘都不改变相对位置。工具栏字号 / 按钮尺寸 / 间距
    不随板面缩放。
  - `frontend/src/interactive/overlayPlacement.ts`：删除 `placeCardOverlay` 与其边缘常量（改后无调用方，
    且避让语义与新规则冲突），新增 `placeToolbarRelativeToCard()`（屏幕坐标 → 宿主定位原点；宿主确有缩放祖先时
    按实测比例逆变换）与 `rectsIntersect()`（可见性）。合并提示的避让在 `BoardCanvas.vue` 的 `mergeHintStyle`
    里自成一套，未动。
  - `frontend/src/components/interactive/BoardCanvas.vue`：`.board-main` 内与 `.board-viewport` 并列新增
    独立浮层宿主 `.card-toolbar-host`（`inset: 0`、`pointer-events: none`、`z-index: 50`、
    `overflow: hidden` 裁切到板面可视区），工具栏因此脱离 `.board-surface` 的缩放与滚动。锚点按卡片 id 注册
    `BoardCard` 实例（`defineExpose` 暴露只读 DOM 元素），只读该卡真实 `getBoundingClientRect()`，
    不回写卡片坐标、布局计划或视图状态（jsdom 没有真实布局时才退回数学兜底，注释写明）。
  - **单帧合并调度**：选择与所有者切换、view 与真实 scroll、展开集合、store 提交 / 撤销重做、窗口 resize、
    宿主 / 锚点 / 工具栏三个 `ResizeObserver`、拖动 `pointermove`、尺寸预览与提交、挂载恢复全部走
    `requestPlacement()`，同一帧最多一次有效测量与一次位置写入；卡片有 left / top / width / height 过渡时
    `startFollow()` 逐帧测量，连续若干帧静止或超过安全时限后做最后一次定位并停止（`transitionrun` 重新触发，
    减少动画时直接不进循环），空闲不跑 rAF；卸载 / 切板取消待执行帧、断开全部观察器与过渡监听。
  - `BoardCard.vue`：`<Teleport :to="toolbarHost" :disabled="!toolbarHost">` 把工具栏送进宿主（宿主未挂载时
    就地渲染，不会出现在 (0,0)）；新增 `toolbarHost` / `toolbarVisible` 可选 prop 与
    `getCardElement()` / `getToolbarElement()`；菜单展开方向不再混算两套坐标（原来拿「宿主像素」比「板面单位」），
    改为按实测屏幕矩形 + 面板真实高度决定；切换所有者时收起「更多」面板。
  - **可见性（状态诚实）**：卡片与板面可视区相交**且**工具栏屏幕外框与板面可视区相交（都严格相交）才显示；
    完全不相交时隐藏（`v-show`），重新相交后恢复；部分相交仍按固定位置显示（允许被宿主裁切）。
    隐藏只影响显隐，`left / top` 照常写入、不因隐藏改变。
- **Tests：** 新增 `frontend/src/components/interactive/__tests__/cardToolbarLayoutHarness.ts`（jsdom 布局桩：
  按浏览器语义算 containing block、祖先 scale 与真实 scroll）、
  `frontend/src/components/interactive/__tests__/cardToolbarAnchor.test.ts`（渲染后几何断言：选中与恢复、
  缩放 25–250%、平移、拖动中与松手、拉伸、展开收起、让位、撤销重做、容器尺寸变化、工具栏换行变高、切换所有者、
  部分与完全离屏）、`frontend/src/components/interactive/__tests__/cardToolbarLayoutHarness.test.ts`
  （桩自检 + 反证：基线结构会报出 dx > 100、宿主原点漏减会报出 dx ≈ 40，证明断言不是恒真）；
  改写 `frontend/src/interactive/__tests__/overlayPlacement.test.ts`；更新 `fix025-connect-resize.test.ts`
  里一条过时的「相对 surface」断言。守卫口径：`abs(工具栏.left − 卡片.left) ≤ 1px`、
  `abs((卡片.top − 工具栏.bottom) − 10px) ≤ 1px`。
- **验证：** 真实浏览器（Playwright 驱动本机 Chrome，真鼠标 / 滚轮 / 连续手势采样）：可见采样最大
  `|dx| = 0.02px`、最大 `|gap − 10| = 0.16px`，违反 0 条；工具栏从不落在 `.board-surface` 内；
  可见性不变式（显示 ⇔ 卡片 ∩ 可视区 ∧ 工具栏外框 ∩ 可视区）在连续缩放 / 平移 / 拖动穿越边界时无一次不符、
  无边界闪烁；边界恰在「工具栏底边 = 可视区上沿」处切换。菜单、许可说明、连接点拖线与尺寸把手都在真实鼠标下命中，
  操作期间板面 scroll / scale / selection 不变。基线对照：同一探针在改动前实测最大 `dx = 2996.25px`、
  `gap` 偏差 `1416.02px`。前端全量 `npm test` 全绿（2026-10-14）。
- **Known limitations：** 真实浏览器里工具栏在各验证宽度下始终单行（换行变高只有 jsdom 用模拟高度覆盖）；
  「边贴边」严格相等那一档由组件级探针覆盖，浏览器扫描落在边界两侧；只在 headless Chrome +
  `deviceScaleFactor = 1` 下验证，Windows 125% / 150% 显示缩放与其它内核未覆盖；撤销 / 重做只在 jsdom 覆盖；
  动画与连续手势采样间隔 60–120ms（逐帧跟随由 jsdom 的逐帧断言覆盖）；验证使用共享的 e2e 板面数据
  （探针会拖动 / 拉伸 / 建关系），不是干净数据域。
- **后续依赖：** 与 main 的合并说明、冲突清单与验证证据见交付说明；本轮未合并、未打包、未发布。

### P61 — 画布向左上也无限：原点重定位（方案 B 第二步）（2026-10-14 第三十三轮）

- **Status：** completed
- **Implementation：**
  - `board.ts`：`clampCardRect` 去掉**左下边界**（坐标允许为负，只留 ±1e6 哨兵）；
    新增两个纯函数 —— `rebaseToFit(state, pad)`（算出"内容全部 ≥ pad"需要整体平移多少）
    与 `rebaseOrigin(state, dx, dy)`（卡片与组框一起平移）。
  - `BoardCanvas.vue`：拖放提交前先算重定位；需要时**整块板面整体平移**并把滚动量补上同样的量
    —— 卡片在屏幕上原地不动，用户察觉不到坐标系换了原点。阈值取 0（只有真拖到负坐标才触发）。
  - **为什么用整体平移而不是让坐标长期为负**：卡片的 x/y 会被渲染、连线、组框、工具栏换算、
    板面可用区发布各自读一遍；整体平移把这些地方一次性免掉，语义上也没有损失（位置只是位置）。
- **Tests：** 更新 `boardSurfaceBounds.test.ts`（原来断言"负坐标被夹回 0"，现在断言
  "纯函数层允许负坐标 + 重定位后回到 ≥0"）与 `boardNoOverlap` 的占满用例；
  `vue-tsc` 0 错误；前端全量 378 文件 / 3338 用例全绿。
- **Known limitations：** 「回到内容」（一键把视图框到所有卡片）仍未做；
  重定位会整体改写卡片坐标（位置不构成意图依据，后端按 layout_only 处理，不会产生工作请求）。
- **后续依赖：** 用户同轮还要求「再次尝试解决工具栏偏移」，仍未做（见 P59 的线索）。

### P60 — 板面改为「随内容增长的画布」（方案 B 第一步）（2026-10-14 第三十二轮）

- **Status：** completed（方案 B 的**第一步**；第二步见 Known limitations）
- **Implementation：**
  - 需求（用户在选项 A/B/C 中选定 B）：板面要「无限」。
  - `frontend/src/interactive/board.ts`：`clampCardRect` **去掉右上边界** —— 不再夹到 2400×1600，
    只保留 `BOARD_COORD_LIMIT`（1e6）这个"离谱值哨兵"，保证坐标始终是有限数；
    **左上仍以 0 为界**（坐标原点不漂，理由见下）。
  - `frontend/src/components/interactive/BoardCanvas.vue`：新增 `surfaceSize` computed
    = `max(最小画布 2400×1600, 内容包围盒 + 400px 留白)`；滚动内容尺寸、`clampScroll` 上界、
    容器兜底尺寸与两处 SVG 的 width/height 全部改用它 —— 卡片往右下拖多远，画布就长多大。
- **Tests：** 更新 `boardSurfaceBounds.test.ts` 的两条旧断言（原来断言"夹到画布 − 卡片尺寸"，
  现在断言"不再夹回旧画布、数值仍然有限"）；`vue-tsc` 0 错误；相关用例全绿。
- **Known limitations（第二步待做）：** 目前是**半无限** —— 右下方向无边界，**左上仍以 0 为界**。
  要连左上一起无限，必须让**坐标原点随内容漂移**（卡片 x/y 会出现负数），
  那样渲染、连线、组框、工具栏换算、板面可用区发布都要跟着改一遍 —— 代价明显更大，
  建议先按当前形态用一段时间，确有必要再上第二步。另外「回到内容」（一键把视图框到所有卡片）
  也还没做，无限画布通常需要它。
- **后续依赖：** 用户同轮还要求「再次尝试解决工具栏偏移」，见 P61。

### P59 — 工具栏准确跟随卡片（拖动中 / 恢复选中都不再漂移）（2026-10-14 第三十一轮）

- **Status：** completed
- **Implementation：** 用户报「工具栏漂移出去，拖动时应准确跟随卡片、相对位置不变」。实测两组证据：
  1. **拖动中**：卡片到 x=1288 时工具栏停在 1121（dx 从 0 变 **−166**）—— 根因是
     `placeCardOverlay` 的**横向夹取**：卡片一靠近视口边缘，工具栏就被夹到视口边。
     修法：卡片**没有完全在视口内**时不做横向夹取（跟着卡片走）；完全可见时才夹（保持原行为）。
  2. **刷新页面后**：恢复出来的选中卡片让工具栏停在 (0,84)（dx **−1303**）—— 根因是
     `refreshOverlay()` 只在点击 / 拖放结束 / 窗口尺寸变化 / 视图变化时调用，
     **恢复选中**这条路径不经过任何点击。修法：新增对 `primarySelectedCard` 的 watch，
     并在挂载后补一帧刷新。
- **Tests：** `vue-tsc` 0 错误；前端全量 378 文件 / 3338 用例全绿。
- **验证：** 真实浏览器四态实测：初始 / 选中后 / 拖动中 / 松手后 **dx 全部为 0**（相对位置恒定）。
- **后续依赖：** 与 P53–P58 同一轮。

### P58 — 宽高按钮不再出现；提示条浮在板面上；对话按钮固定右下角（2026-10-14 第三十轮）

- **Status：** completed
- **Implementation：**
  - **宽 / 高按钮不再出现**（用户附截图要求）：板面卡片与对话窗口标题栏里的那一对按钮
    **仍在 DOM 里**（组件契约与既有用例不变），只在**板面 / 面板这一层**用 `display: none` 视觉隐藏
    （`BoardResizeLayer` 的 `.resize-host :deep(.rz-keys)`、`ChatDock` 的 `.panel-head :deep(.rz-keys)`）。
    实测：板面 DOM 里 1 个、对话打开后 2 个，**可见的都是 0**。八向拖动热区保留。
  - **提示条浮在板面上**：`.banner` 原来排在 flex 列里占一行 —— 一出现就把板面与所有卡片整体下移。
    改成 `position: absolute; top: 0` + 半透明底 + `pointer-events: none`。
    实测：提示条出现与消失时板面 `top/height` 完全一致（84 / 816），不再跳动。
  - **对话按钮固定右下角**：`.toggle` 改 `position: fixed`，并删掉 `.chat-dock.open .toggle` 的
    `top: calc(100% + …)`（配合 fixed 会把按钮推到视口下方 43px）。实测收起 / 缩放中 / 展开三态都是 16 / 16。
  - **纯位置拖动不再提示**（补 P57）：拖动中的实时提示（`dragHint`）在纯位置变化时返回空串，
    横幅改成 `v-if="dragging && dragHint"` —— 用户两次反馈的那条粉边栏（拖动过程中出现、松手即消失）
    才真正消失；进组 / 成组 / 移出组的提示保留。
- **Tests：** `vue-tsc` 0 错误；前端全量 378 文件 / 3338 用例全绿（按钮改走 CSS 隐藏后，
  既有"键盘 −/＋"用例与组件契约用例都不需要改）。
- **验证：** 真实浏览器逐项核对（按钮可见数 0、提示条出现前后板面几何不变、按钮三态固定右下角、
  拖动中不再出现那条横幅）。
- **后续依赖：** 与 P53–P57 同一轮；缩放不变性经实测已正确（未发现缺陷，待用户给出具体场景）。

### P57 — 只移动位置不再弹提示（2026-10-14 第二十九轮）

- **Status：** completed
- **Implementation：** 用户要求删掉拖动后那条「松手后只移动位置：位置不构成意图依据。」
  （附截图）。`BoardCanvas.vue` 的拖放收尾只在**结果成组 / 合并**时提示；
  纯位置变化不再弹提示 —— 位置不构成意图依据这件事已经写在提交区与操作说明里，每次拖动都弹属于噪音。
  `dragLabel` 里那句仍然保留（它是**撤回栈的标签**，不是界面提示）。
- **Tests：** `vue-tsc` 0 错误；互动面 101 文件 / 1233 用例全绿；前端全量见下。
- **验证：** 真实浏览器核对：拖动一张卡片后，页面文本里不再出现「只移动位置」与「位置不构成意图依据」。
- **后续依赖：** 与 P53–P56 同一轮。

### P56 — 展开卡片拖动时白框归零；工具栏在平移/缩放后跟随（2026-10-14 第二十八轮）

- **Status：** completed
- **Implementation：**
  - **② 白框归零（收口 P54）**：拖动中的卡片本体走**实时矩形**，而展开底衬走计划槽位 ——
    两者差一个「展开抬升」（P54 时是 46px）。修法：`expandedSlots` 对**正在拖动**的那张卡
    直接给实时矩形（`displayRect`），计划本身不动，所以展开收敛那条路径不受影响。
    实测偏移 122px → 46px → **0**（底衬与卡片逐像素对齐）。
  - **④ 工具栏跟随**：`refreshOverlay()` 原来只在点选 / 拖放结束 / 窗口尺寸变化 / 挂载时调用，
    **平移或缩放板面之后不刷新**（实测卡片已到 x=840、工具栏还停在 996）。
    修法：新增对「视图（缩放 / 平移 / 滚动）+ 展开集合」的 watch。
- **Known limitations：** 展开**中**的卡片，工具栏仍按正式坐标定位（与展开槽位差一截）——
  把 `slotOf` 接进 `refreshOverlay` 会让展开收敛用例变红（读布局计划在这里有副作用），
  所以这一小块留到下一轮用不读计划的方式解决。普通卡片的工具栏在平移 / 缩放后已完全跟随。
- **Tests：** `vue-tsc` 0 错误；`irl-c-canvas` / `boardCanvas` 等既有用例全绿；前端全量见下。

### P55 — 卡片不允许叠放（2026-10-14 第二十七轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户确认按方案 A 做）：拖动卡片时若落点被别的卡片占住，**自动挪到最近的空位**，
    不再直接压上去；不自动整理板面上**已有**的重叠。
  - `frontend/src/interactive/board.ts` 新增纯函数 `freeDropSpot`：以落点为中心一圈圈向外
    （步长 16px、最多 40 圈）找第一个与别的卡片都不相交的位置；找不到就**如实返回原落点**
    （不假装让开了），越界仍由 `clampCardRect` 兜住。
  - `dropCard` 在**不会成组**时用它。成组有**两条**路径必须都排除：
    `resolved.target`（落点明确指向某张卡）与 `bestMergeTarget`（两张未分组卡片明确重叠 → 自动成组）
    —— 只看前者会让「重叠自动成组」的卡片先被推开再成组（既有用例抓到过）。
- **Tests：** 新增 `boardNoOverlap.test.ts`：落点被占时自动让开且不动别的卡片、
  落点为空时一个像素都不改、`freeDropSpot` 是纯函数、整个画布被占满时如实返回原落点。
- **验证：** `vue-tsc` 0 错误；相关用例全绿；前端全量见下。
- **Known limitations：** 只在**新落点**上生效，板面上已经存在的重叠不会自动整理（用户未要求）；
  让位是「就近找空位」，不是全局最优排布。
- **后续依赖：** 与 P53/P54 同一轮。

### P54 — 展开卡片拖动时不再留下白色底框（进行中：偏移已缩小，未归零）（2026-10-14 第二十六轮）

- **Status：** completed（**部分修复**，见 Known limitations）
- **Implementation：**
  - 现象（用户报「卡片在拖动时原地出现了白色底框」）：把一张**展开预览**的卡片拖动时，
    展开面板停在原地，露出它的白色底衬 —— 看起来像卡片掉下一个白框。
  - 根因：布局计划（`planExpandedLayout`）的入参是 `liveCards`（**正式坐标**），
    而卡片本体走 `displayRect`（**拖动中的实时坐标**）—— 拖动期间板面状态不变（只预演），
    于是展开槽位停在原地。不能直接复用 `displayCards`：它依赖 `layoutPlan`，会形成循环依赖。
  - 改动：`BoardCanvas.vue` 新增只依赖 `dragging` / `resizing` 的 `planCards`（把拖动中卡片的
    实时矩形替换进计划入参），布局计划改用它。
- **Tests：** `vue-tsc` 0 错误。真实浏览器核对：拖动展开卡片时，展开底衬的纵向偏移从 122px
  缩小到 **46px**。
- **Known limitations（未修完）：** 仍有约 46px 纵向偏移 —— 展开槽位与卡片本体的
  「展开抬升」口径还不一致（计划里带抬升、拖动预览里没带）。下一步要把两处口径统一到同一个来源。
- **后续依赖：** 与 P53 同一轮；用户同轮报的另外两项（禁止叠放 / 工具栏跟随）另起里程碑。

### P53 — 修回归：文件与图片合并后，图片预览整块消失（2026-10-14 第二十五轮）

- **Status：** completed
- **Implementation：**
  - 现象（用户报「原先的图片预览效果在点击一下后就完全消失了」）：图片卡在**第一次客户端写入**之后
    就再也不显示图片 —— 封面没了，`<img>` 也没了。
  - 根因：**P48 的回归**。`frontend/src/interactive/materials.ts` 的附件可用性判定里写着
    `const isImage = kind === "image"`，而 P48 把遗留的 `kind="image"` 迁移成 `file` ——
    迁移一发生，`imagePreviewable` 就永远是 false，封面与缩略图同时消失
    （单测实测：`kind=image` → true；`kind=file` + `meta.file` → false）。
  - 修法：能不能当图片预览**只看附件扩展名**（`isPreviewableImageName`），与 kind 无关；
    同时 `cardAttachmentBlock` 在 `meta[kind]` 缺失时回退读另一个键
    （与后端 `_attachment_id(meta,"file") or _attachment_id(meta,"image")` 同一口径），
    这样「kind 已迁移、meta 还是老键」的中间状态也读得到。
- **Tests：** `materialType.test.ts` 增加回归用例（先 RED）：`file`+`meta.file` 的图片附件必须可预览、
  遗留形状仍可预览、半迁移形状（kind=file + meta.image）也要读得到、非图片扩展名不许当图片预览。
- **验证：** `vue-tsc` 0 错误；相关用例全绿。真实浏览器核对：图片卡恢复
  `data-cover="1"` + `.card-cover` + `<img>`（blob，自然宽 945），图片作为卡片背景显示。
- **后续依赖：** 与 P41–P52 同一轮测试中发现；用户同轮报的另外三项（禁止叠放 / 拖动时的白框 /
  工具栏不跟随）待处理。

### P52 — 按用户要求删除一批界面解释文案（2026-10-14 第二十四轮）

- **Status：** completed
- **Implementation：**（用户逐项点名，全部按字面执行）
  - **02 添加菜单**：删掉每个入口下的说明与菜单底部那句「只加到本地板面…」（只留入口名）。
  - **04 板内搜索**：删掉面板里的两段说明（「这不等于交给 QIO」与旧卡片迁移说明）。
  - **05 提交区**：删掉失败块下方那行红色保留说明（「重试只会从这次提交保存的快照恢复…」）。
  - **06 提交区详情**：**只留**「本次有效改动」这一块；保存状态、可见范围、材料读取状态、
    理解过程历史、草稿规则等说明全部不再渲染。
  - **07 操作说明**：整个入口（按钮 + 浮层）删除。
  - **11 对话帮助**：删掉其中的板面手势段（可见范围 / 媒体 / 附件 / 快捷键仍在）。
  - **12 对话流空态**：删掉「还没有消息。这里发送的文字会进入当前 QIO 会话…」。
  - **13 卡片元信息层**：删掉许可说明与「在组「…」」（只留状态与时间）；
    许可状态仍由卡片上的 `card-license` 徽章（未允许查看 / 允许 QIO 查看）与卡片工具栏承载。
  - **17 任务状态浮层**：删掉「待审批…运行中」那段说明。
- **Tests：** 受影响的断言按新界面更新（每条都注明是本次删除带来的口径变化）；
  其中三条**保证并未减弱**，只是换了承载处：失败原因仍在折叠区之外、
  许可状态仍可辨认（`card-license`）、旧阶段仍不与当前状态并列。
- **Known limitations（如实记录）**：本次删除移除了几处**如实说明类**文案 ——
  受理来源（「精确查证」）、可见范围与材料读取状态、详情里的较长诊断、理解过程历史。
  这些是用户明确要求删除的；如需恢复，改动集中在 SubmitCluster / ChatDock / AddMenu /
  BoardSearchPanel / BoardToolbar / BoardCard / IntentStatusPopover，可直接回退对应片段。
- **后续依赖：** 与 P41–P51 同一轮测试中发现。

### P51 — 窄窗口下工具栏不再被压塌（「详情压住查看过程」）（2026-10-14 第二十三轮）

- **Status：** completed
- **Implementation：**
  - 现象（用户报，附截图）：「详情」按钮压住「查看过程」按钮。
  - 根因：**P47 的回归**。P47 把工具栏上限改成「扣掉几何计划里的聊天宽度」，
    窄窗口下这个 420px 仍然被整块扣掉 —— 480 宽时上限只剩约 12px，
    工具栏塌成 18px、**提交区宽度变成 0**、高度 1318px，按钮在塌陷布局里互相压住。
  - 修法：新增 `--im-toolbar-chat-reserve`：宽窗口 = `min(--im-geo-chat-max-w, 40vw)`（P47 的意图不变），
    窄窗口（≤1024）= `--im-chat-reserve`（104 / 88px）。工具栏上限改用它。
- **Tests：** 新增 `frontend/src/interactive/__tests__/boardToolbarNarrow.test.ts`（源码契约守卫）：
  工具栏上限必须用 `--im-toolbar-chat-reserve`、不许直接扣 `--im-geo-chat-max-w`，窄窗口必须有换算。
  真实浏览器核对：提交区宽度 620 → 466、560 → 406、**480 → 326（原来 0）**、420 → 266（原来 0）；
  1400×900 工具栏仍是 16–948（P47 的效果没有被破坏）；480×600 截图确认按钮不再互相压住。
- **Known limitations：** 480 宽下失败原因文字列较窄（每个词一行），但不再互相遮挡；
  要更宽松的可读性需要用户手动放大窗口。
- **后续依赖：** 与 P41–P50 同一轮测试中发现。

### P50 — 板面 chrome 不再启动原生选择拖拽（画布不再被「Ctrl+拖拽」滚走）（2026-10-14 第二十二轮）

- **Status：** completed
- **Implementation：**
  - 现象（P41 记录、用户在测试中再次点名）：在板面上按住 Ctrl 拖动时，画布会一路向下滚。
  - 根因：**与 Ctrl 无关**。在非画布表面的板面 chrome（提交区 / 徽章 / 工具栏文字）上按下并拖动
    会启动浏览器**原生文本选择拖拽**，指针越过滚动容器边缘时 Chromium 触发选择自动滚动，
    把最近的滚动容器 `.board-viewport` 滚走 —— 实测从提交区拖到窗口底，
    `scrollTop` 从 0 跳到 **811**（卡片正文不会：卡片自己有 `preventDefault`）。
  - 修法（方案 A）：frontend/src/styles/interactive-shell.css（2026-10-26 已删除，新版公共层变量迁到 frontend/src/styles/layers.css） 里给互动舞台
    `main.im-stage` 加 `user-select: none`；需要选文字 / 复制的地方**显式开回来**：
    卡片正文（`[data-im="card-body"]`）、对话面板（`[data-im="chat-panel"]`）、
    `textarea` / `input` / `[contenteditable="true"]`（选择不是继承锁，子元素可以重新打开）。
    比逐区域 `preventDefault` 更彻底：新增 chrome 不会重新引入这个 bug。
- **Tests：** 新增 `frontend/src/interactive/__tests__/boardNoTextSelection.test.ts`（源码契约守卫）：
  舞台必须 `user-select: none`、卡片正文与对话面板必须开回 `text`、输入框 / 可编辑区必须 `text`。
  真实浏览器（Chrome 1400×900）核对：从提交区 / 工具栏 / 对话面板顶部 Ctrl+拖到窗口底，
  `scrollTop` 全部保持 0（修复前提交区那次是 0 → 811）；
  计算样式：舞台 / 提交区 / 工具栏 = `none`，卡片正文 / 对话面板 / 输入框 = `text`。
- **Known limitations：** 板面 chrome 上的文字不再能直接划选复制（卡片正文、对话、输入框不受影响）；
  需要复制 chrome 文案时用「操作说明」面板或卡片正文。
- **后续依赖：** 与 P41–P49 同一轮测试中发现；这是 P41 记录的那条未修问题的收口，至此十个问题全部完成。

### P42 — 对话窗口在短窗口下不再把输入框裁掉（2026-10-14 第十四轮）

- **Status：** completed
- **Implementation：**
  - 现象：窗口变矮（实测 1200×560）时，对话窗口的默认框被收敛到 238px，输入行被推到面板底边之外，
    再由 `.panel` 的 `overflow: clip` 裁掉 —— 发送键完全看不见，也没有任何入口能滚到它。
  - 根因两层：① frontend/src/components/interactive/ChatDock.vue（2026-10-26 已随旧互动展示层删除） 的 `.composer` 是 `min-height: 0`，
    面板一矮先压输入区；② `frontend/src/interactive/chatFrame.ts` 的 `CHAT_FRAME_MIN.h = 220` 是按
    「标题栏 44 + 输入区 92 + 阅读区 72」估的，实测这三段分别是 52 / 147 / 72，最小值本身就偏小。
  - 修法：`.composer` 给出必需的最小高度 96px（附件行 31 + gap 4 + 输入行 57 ≈ 92，留一点余量），
    面板变矮时**先让阅读区、再让帮助摘要**（帮助另有顶部入口，不会失去访问路径）；
    `CHAT_FRAME_MIN` 按实测分解抬到 260（内边距 24 + 标题栏 52 + gap 16 + 阅读区 72 + 输入区必需 92 ≈ 256）。
- **Tests：** frontend/src/interactive/__tests__/chatFrame.test.ts（2026-10-26 已删除，chatFrameState 发布语义测试保留） 的最小值用例改成按实测分解断言；
  frontend/src/components/interactive/__tests__/irl-a-chatDock.test.ts（2026-10-26 已删除） 增加源码守卫
  （`.composer` 必须声明不小于 92px 的最小高度）；`frontend/src/interactive/__tests__/irl-verify-chatframe.test.ts`
  里那条写死 580 的断言改为按 `CHAT_FRAME_MIN.h` 推导，避免数字过期。
  真实浏览器核对（Chrome）：1200×800 / 1024×768 / 1440×900 与修复前一致；
  **1200×560 面板仍是 238px，但输入行与发送键完整可见**（修复前两者都不可见）；
  800×600（文档规定的桌面最小窗口）与 1024×600 通过。
- **Known limitations：** 浏览器窗口矮到 480px 以下（低于文档规定的 800×600 桌面最小窗口）时面板只剩 158px，
  输入行仍会被裁掉约 16px；要再往下撑住，需要「按高度折叠附件行」这类布局规则，本轮未做。
- **后续依赖：** 与 P41 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P43 — 卡片局部工具栏不再压住卡片（2026-10-14 第十五轮）

- **Status：** completed
- **Implementation：**
  - 现象：卡片被选中后，局部工具栏会**整块落在卡片内部**（实测 1200×500 窗口：卡片 104–508、
    可视区 111–500，工具栏落在 442–478），用户报告「他不应该在下面的时候挡住卡片」。
  - 根因：`frontend/src/components/interactive/BoardCanvas.vue` 的 `placeOverlay` 用
    `viewRect.bottom − 30` 夹下方位置 —— 既没算工具栏自身高度，也看不见「落在卡片下半部分」，
    而当时的判定只检查「是否碰到卡片顶边」，于是这种重叠一路放行。
  - 修法：把落点决策抽成纯函数 `frontend/src/interactive/overlayPlacement.ts`（先上方、后下方，
    两者都要求整个工具栏在可视区内；竖向两边都放不下时挪到卡片横向之外，右侧优先），
    组件只负责把结果换算到 offsetParent 坐标。契约 §8.1 补上这条落点规则。
- **Tests：** 新增 `frontend/src/interactive/__tests__/overlayPlacement.test.ts`：上方放得下 / 顶到上沿放下方 /
  底边贴近下缘不许被夹回卡片 / 卡片高于可视区时挪到横向之外 / 横向也没地方时保底可见。
  真实浏览器核对（Chrome）：1200×800 仍在上方（与修复前一致）；**1200×500 与 1000×430 从「压住卡片」
  变为「卡片右侧、完全不相交」**，且工具栏完整落在可视区内。
- **Known limitations：** 卡片横向也占满可视区（几乎没有这种卡片）时只能夹进可视区保底，此时压住卡片无法避免；
  工具栏仍挂在带 `transform: scale()` 的 `.board-surface` 内（P40 记录的已知限制：画布缩放 ≠ 1 时
  工具栏自身随画布缩放、行内坐标口径不一致），本轮未改。
- **后续依赖：** 与 P41 / P42 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P44 — 星球不再为空槽画融合环（2026-10-14 第十六轮）

- **Status：** completed
- **Implementation：**
  - 现象：星球上出现一圈圈「里面什么都没有」的融合环（用户报告「星球这里多了三圈圆环」）。
  - 根因：`frontend/src/composables/usePlanetScene.ts` 把展示窗口的**槽位**直接当成环锚点
    （`slotDirs.map(... w: 1)`），空槽既没有圆点也没有标签，却带着权重 1 参与 smooth-min 融合；
    而空槽的占位方向都是 `(0,1,0)`，多个空槽的环因此叠在同一处，看起来就是一组很实的环。
  - 修法：新增纯函数 `ringAnchorsForSlots`（`frontend/src/planet/topicData.ts`）——只有真的有话题的槽位
    权重为 1，空槽用与「未使用 uniform 槽」同一个哨兵 `EMPTY_SLOT_WEIGHT = -100` 中和
    （场项变成 `ang + 100·rL`，比三层环半径 0.14 / 0.22 / 0.34 大两个数量级，任何方向都不与环层相交）；
    整窗同步与单槽替换（槽位变空）都走同一条规则。设计规范 §4.3 补上「环属于话题，不属于窗口槽位」。
- **Tests：** 新增 `frontend/src/planet/__tests__/ringAnchors.test.ts`：空槽权重被中和且下标仍对应槽位；
  环所在方向上「有话题时正好落在环上、空槽中和后远离所有环」；中和的空槽不会把真实话题的环推开。
  真实浏览器 A/B（Chrome，1536×1145，开启「减少动画」以固定画面）：
  旧实现 vs 修复后 —— 环像素总数 12273 → 6328，空槽占位方向所在的上半部 8534 → 2589，
  **真实话题所在的下半部 3739 → 3739（完全不变）**。
- **Known limitations：** 本轮只修「空槽画环」；话题本身为空（`fragment_count = 0`）但仍在展示窗口里时，
  它依然有自己的环与圆点（这是「话题存在」的表达，未改）。
- **后续依赖：** 与 P41–P43 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P45 — 图片卡改为「以图为主」的封面式呈现（2026-10-14 第十七轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户在测试中提出并逐项确认）：图片卡应当把**图片作为卡片背景**，而不是「标题 + 说明 +
  140px 缩略图 + 查看按钮 + 一段解释」的文字卡；图片在卡片里的视觉占比必须成为主体。
  - 范围：`kind="image"` 的卡片，以及**附件是可显示图片**的 `kind="file"` 卡片（判定用与预览组件
    同一个 `attachmentPreviewPlan`，两边不会各说各话）。
  - 常态与折叠态：`frontend/src/components/interactive/CardAttachmentPreview.vue` 新增
    `variant="cover"` —— 图片 `object-fit: cover` 铺满整张卡（`position: absolute; inset: 0` +
    `border-radius: inherit`），上下两条 `--bg-surface` 派生蒙版保证文字对比度；卡片上只保留
    标题 + 一行补充说明 + 右下角「查看」浮层按钮，附件信息行与缩略图让位（阅读态仍完整显示）。
  - **编辑态与阅读态不铺封面**：那两个状态的主体分别是表单与真实预览。
  - 封面层 `pointer-events: none`（只有「查看」自己接事件）：卡片拖动 / 选中 / 双击手势照旧；
    背景图是装饰（`aria-hidden` + 空 alt），卡片可访问名称与阅读态预览的替代文本不变。
  - 契约 §16.3 补上这条规则；blob 通道与生命周期与默认变体完全共用，没有第二条取字节路径。
- **Tests：** 新增 `frontend/src/components/interactive/__tests__/image-cover-card.test.ts`：
  常态封面 + 标题 + 一行说明 + 「查看」、不再出现附件信息行与缩略图；封面图 `aria-hidden` + 空 alt；
  非图片附件的文件卡不铺封面；折叠态保留封面带；编辑态与阅读态不铺封面；模板完整性守卫
  （属性被挤成文本节点时立刻失败）。真实浏览器核对（Chrome 1400×900）：封面 385×402 铺满
  387×404 的卡片、「查看」51×25 完整落在卡内且可见、说明与卡片状态行互不重叠。
- **Known limitations：** 封面按 `object-fit: cover` 裁切（可能裁掉图片边缘）；没有给「按比例完整显示」
  的切换入口。卡片极小时（`is-compact`）标题与说明按既有规则让位，封面仍在。
- **后续依赖：** 与 P41–P44 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P46 — 卡片坐标夹在画布范围内（页眉不再「吃掉」卡片顶部）（2026-10-14 第十八轮）

- **Status：** completed
- **Implementation：**
  - 现象：卡片可以被拖到板面**顶边之外**（实测某张卡 `y = −63.84`），它的顶部落在滚动区外，
    被页眉那一带遮住、既看不见也够不到；用户报告「页眉挡住板面上方，一部分卡片被截断，
    有些卡片还能放进被遮住的地方」。
  - 根因：板面**没有任何一处**把卡片坐标约束在画布内 —— `dropCard` 直接写 `card.x / card.y`，
    `addCard` 直接收调用方给的坐标，`updateCard` 也不夹取；而滚动范围被 `clampScroll` 限制在
    `[0, 内容 − 视口]`，画布原点之外的内容**永远滚不到**。
  - 修法：`frontend/src/interactive/board.ts` 导出 `BOARD_SURFACE_W/H`（画布尺寸的唯一事实源，
    BoardCanvas 改为引用同一对常量）与 `clampCardRect`（尺寸先收敛到画布内，再夹左上角）；
    新建 / 拖动落下 / `updateCard` 都走它，`normalizeCard` 也走它 —— 于是**已经存成负坐标的卡片
    在下次加载时自动回到画布内**，不会一直卡在「被遮住」的状态。契约 §8.2 补上这条不变量。
- **Tests：** 新增 `frontend/src/interactive/__tests__/boardSurfaceBounds.test.ts`：
  拖到左上之外夹回原点、拖到右下之外夹到「画布 − 卡片尺寸」、范围内一个像素都不改、
  新建卡片的负坐标同样被夹、`updateCard` 既不能挪出画布也不能把尺寸撑出画布。
  真实浏览器核对（Chrome 1400×900）：加载后 `minY = 0` 且**没有任何卡片在视口之上**
  （修复前那张卡是 `−63.84`）；把一张卡从可见处往窗口顶拖，落下后 `y = 0`（不是负数）。
- **Known limitations：** 只约束画布边界，不改变卡片之间的重叠规则；`folded` 卡片的显示高度
  与状态高度不同，夹取用的是状态里的 `h`。
- **后续依赖：** 与 P41–P45 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P47 — 对话窗口随窗口恢复；底部工具栏左对齐让出右下角（2026-10-14 第十九轮）

- **Status：** completed
- **Implementation：**
  - 现象（用户报告）：改窗口大小 / 长宽比再恢复时，对话窗口**不跟着恢复**；而且它被下方工具栏
    「卡在上方」，右下角一直留白。用户明确希望：**工具栏尽可能向左缩小**，把右下角让出来。
  - 根因一：窗口缩小时把「收敛后的框」直接写回**当前生效框**，窗口放大回来时已经没有
    「用户原本想要的框」可恢复 —— 实测 520 → 244，窗口恢复后仍停在 260。
    修法：frontend/src/components/interactive/ChatDock.vue（2026-10-26 已随旧互动展示层删除） 把**用户框**（`preferredFrame`，
    唯一落盘的那份）与**生效框**（`frame`）分开；窗口变化只重新推导生效框
    （`convergeChatFrame(preferredFrame, area)`），只有用户拖动 / 调整尺寸才更新用户框。
  - 根因二：工具栏在「扣掉 132px 聊天保留宽度后的剩余空间里居中」并固定上限 1180px
    （`components/interactive/BoardToolbar.vue` + `styles/interactive-shell.css` 各一份），
    对话窗口的可用区又把「工具栏顶边」当**整行**边界。
    修法：工具栏改成**内容宽度 + 左对齐**，上限按几何计划的聊天宽度
    （`--im-geo-chat-max-w`）扣掉；可用区改成**只在横向真的相交时**才让位
    （`measureArea` 的 `toolbarReachesDock` + 逐框复核的 `avoidToolbar`）。
  - 契约补上这两条（工具栏布局 + 用户框/生效框分离）。
- **Tests：** 既有套件继续通过（含 `irl-c-overlay-chat-frame` 的几何计划用例、`chatFrame` 的收敛用例）。
  真实浏览器核对（Chrome，1400×900 → 900×520 → 恢复）：对话窗口 520 → 244 → **恢复 520**；
  工具栏 16–948（932 宽，内容宽度左对齐）与对话窗口 956–1376 **横向不相交**；
  对话窗口底边从 718 延伸到 **828**（右下角不再留白）。
- **Known limitations：** 工具栏宽度由内容决定，提交区文字很长时会在上限处换行（不截断）；
  窗口极窄（≤1024px）时工具栏仍会退化成接近整行，此时对话窗口按老规则让位。
- **后续依赖：** 与 P41–P46 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P48 — 文件与图片合并成同一种材料卡（2026-10-14 第二十轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户在测试中提出并逐项确认）：互动模式不再区分「文件 / 图片」两种卡片，
    卡片样式**统一按文件类型**考虑。
  - `frontend/src/interactive/board.ts`：`ADDABLE_CARD_KINDS` 收敛成 `["text", "file"]`；
    `normalizeCard` 把遗留的 `kind="image"` 逐张迁移成 `file`，并把 `meta.image` 搬到 `meta.file`
    （老键保留，读老数据仍可用；与历史上 code / url → text 同一套做法）。
  - `frontend/src/interactive/previewPlan.ts` 新增 `materialTypeLabel` / `attachmentDisplayName`：
    类型标签按**附件类型**显示（图片 / PDF / 表格 / 演示文稿 / 文档 / 文本 / 代码 / 其它，
    没有附件时显示「文件」）。`BoardCard` 的类型标签改用它。
  - 样式沿用 P45 的封面式判定（"附件是可显示图片"，本来就与 kind 无关）→ 图片附件自动封面、
    其余走常规材料卡，不需要额外分支。
  - 后端无需改动：已核对 `backend/src/agent/interactive/materials.py` 明确同时处理
    `kind="image"` 与「`kind="file"` 但附件是图片」两种情况。
  - 契约 §1 的 kind 表与 §16.3 的卡片规则同步更新。
- **Tests：** 新增 `frontend/src/interactive/__tests__/materialType.test.ts`：菜单不再提供图片、
  遗留 image 迁移成 file 且 meta 搬家（幂等）、类型标签按附件类型显示。
  既有测试文件按新契约更新（断言"三类入口 / kind=image"改为两类入口与 file，
  附件断言由 `meta.image` 改为统一的 `meta.file`）。
- **Known limitations：** 迁移发生在归一化路径（新建 / 写入 / 客户端加载）；服务器直接返回的
  遗留 `image` 卡片在内存里会保持 `image` 直到下一次写入 —— 但呈现与标签都与 kind 无关，
  用户看到的完全一致（后端两种都认）。
- **后续依赖：** 与 P41–P47 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P49 — 详情默认收起；新建文字卡默认提交给 QIO（2026-10-14 第二十一轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户在测试中提出）：详情默认收起；文字卡片默认提交给 QIO。
  - **详情**：`frontend/src/components/interactive/SubmitCluster.vue` 去掉「要补充 / 理解失败时
    自动展开详情」的规则（原来板面底部会常年挂着一大段过程文字）。
    失败原因（`reason`）与保留说明（`retention`）本来就在折叠区**之外**（契约 §11.2 的分层口径），
    默认仍然可见，不会藏住重要信息；过程详情只在用户点「查看过程」时展开。
  - **文字卡默认提交**：新建文字卡 `checked = true` —— `addTextCard`、`addCard` 的兜底
    与添加菜单（`AddMenu.vue` 的 `add()`，原来显式传 `checked: false`）三处统一；
    添加菜单提示文案由「默认未许可」改为「默认提交给 QIO（可取消）」。
    用户仍可在卡片工具栏取消勾选（取消后 QIO 看不到卡内文字、链接与代码，提交也不带上它）。
    **只改新建卡片的默认值**：已有卡片保持原样，不悄悄改已保存的许可意图。
    材料卡不受影响（它们本来就是「默认在本次允许查看范围内」）。
- **Tests：** 既有用例按新默认更新（新建文字卡默认值、许可徽章文案、添加菜单提示文案、
  搜索面板要显式建「未勾选」的注释、`addTextCard` 默认值、未许可语义用例显式关掉勾选）；
  「需要补充」用例改为断言详情**不**自动展开。
- **Known limitations：** 详情收起后，失败原因只在提交区首行显示（完整过程要点开「查看过程」）。
- **后续依赖：** 与 P41–P48 同一轮测试中发现；P41 记录的另一处未修问题（板面非表面区域缺 `preventDefault`
  导致原生选择自动滚动）仍未处理。

### P65 — 图片展示简化：图片统一为文件卡附件、常态即完整展示（2026-10-15 第三十七轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户在测试中提出并逐项确认）：互动模式里的图片不再是一种卡片，而是**文件卡关联的附件**；
    图片在卡片里**常态即完整显示**（等比、不裁切、不出卡片内滚动条），
    卡片内不再有「展开预览 / 收起预览」，也不再保留旧图片卡的迁移与兼容回退。
  - **统一为文件卡附件**：`kind="image"` 从前端类型（`frontend/src/interactive/types.ts` 的 `CardKind`）、
    卡片默认值、创建 / 展示 / 编辑 / 保存 / 提交 / 状态分支里删除；卡片侧读附件**只看 `meta.file`**，
    不再回退 `meta.image`（`frontend/src/interactive/materials.ts`、`previewPlan.ts`、
    `attachmentPreview.ts`、`drafts.ts`、`submission.ts` 一并收敛）。添加菜单只剩「文字注释 / 文件」，
    附件是不是图片由**文件本身**决定；图片附件的判定只有一份口径
    `frontend/src/interactive/materials.ts` 的 `cardAttachmentIsImage`（store 与组件共用）。
  - **常态即完整展示**：`frontend/src/components/interactive/CardAttachmentPreview.vue` 的封面变体改成
    `object-fit: contain`（留白走 `--bg-inset`），去掉上下大面积蒙版与重复说明，只留一行真实说明 + 「查看原图」；
    图片层不吃指针事件、禁止浏览器原生图片拖动；卡片尺寸 / 位置仍由用户设置（`card.w/h`），
    不因图片加载、选中或查看原图而重排。
  - **图片不进卡片内展开列表**：`frontend/src/stores/interactive.ts` 的 `toggleCardPreview` 在入口拒绝图片
    （返回未展开、不假成功），`expandedCardIds` / `previewExpandedCount` 过滤图片，
    板面变化时重新判定并只移除「已变成图片」的 id（`flush: "sync"`，界面不会有一帧把图片画成阅读态）；
    `BoardCard.vue` 的图片文件卡不再渲染展开入口、不挂 `CardInlinePreview`，
    `CardInlinePreview.vue` 的图片阅读分支删除（PDF / Office / 代码 / markdown 原样保留）。
  - **「查看原图」复用既有 `openAttachment`**（带认证 fetch / 桌面原生打开）：不新增高清接口、
    不挂第二套图片查看器；解码失败（文件损坏 / 不是真图片）、取字节失败、文件丢失、超限
    都在**原图片区域**显示真实原因，并给出重试（重走既有 blob 加载）/ 查看原图 / 重新关联附件入口。
  - **后端同口径**：`backend/src/agent/interactive/models.py` 的 `CARD_KINDS` / `MATERIAL_KINDS`、
    `interactive/materials.py`（`resolve_images` / `_deliverable_image` / `_image_fact` / `_media_attachment`）、
    `api/preview_routes.py` 的 `_card_attachment_id` 全部只认 `meta.file`，无调用方的 `_image_entry` 分支删除；
    **保留** `image_delivered` / `image_unsupported` 的视觉交付契约（见 `docs/interactive-vision-contract.md`）。
  - **可达性回归修复（真实应用核对发现）**：封面模式下 `.card-body` / `.card-head` / `.card-meta` 画在封面层之上，
    `.card-body` 又是可命中的空容器，真实指针点不到「查看原图 / 重试 / 重新关联」
    （`elementFromPoint` 命中 `card-body`）；改为封面模式下正文层让开指针命中、只对真正需要交互的子树放行
    （草稿 / 冲突提示仍可见可点，不靠移动 z-index 解决），并补源码守卫。
  - **删除的旧分支**：旧图片卡迁移成 `file` / `meta.image` 搬到 `meta.file`（含老键回退）、`meta.image` 读取、
    图片的 `object-fit: cover` 裁切展示与「查看」浮层文案、图片的卡片内阅读态与 `CardInlinePreview` 图片渲染分支、
    前后端接口与状态分支里的 `kind="image"`。
  - **保留**：文件材料的附件关联 / 上传 / 重新关联 / 打开；其它材料（文字卡、PDF / Office / 代码 / 文本）
    的展开阅读、临时布局与阅读位置服务；模型视觉输入与 `image_delivered` / `image_unsupported` 事实。
- **Tests：** 前端定向用例 `frontend/src/interactive/__tests__/imageCardPreviewBlocked.test.ts`、
  `frontend/src/interactive/__tests__/board.test.ts`、`frontend/src/interactive/__tests__/materialType.test.ts`、
  `frontend/src/interactive/__tests__/attachmentPreview.test.ts`、`frontend/src/interactive/__tests__/previewPlan.test.ts`、
  `frontend/src/components/interactive/__tests__/textCardUnified.test.ts`、
  `frontend/src/interactive/__tests__/recovery-a-n6-draft-input.test.ts`、
  `frontend/src/components/interactive/__tests__/image-cover-card.test.ts`、
  `frontend/src/components/interactive/__tests__/CardAttachmentPreview.test.ts`、
  `frontend/src/components/interactive/__tests__/CardInlinePreview.test.ts`、
  `frontend/src/components/interactive/__tests__/recovery-a-n6-board-card-restore.test.ts`、
  `frontend/src/components/interactive/__tests__/recovery-a-integration-n6-store-input.test.ts`、
  `frontend/src/components/interactive/__tests__/scroll-board-viewport.test.ts`、
  `frontend/src/components/interactive/__tests__/scroll-horizontal-fit.test.ts`、
  `frontend/src/components/interactive/__tests__/image-card-simplification.test.ts`；
  后端 `backend/tests/test_interactive_materials.py`、`backend/tests/test_interactive_board.py`、
  `backend/tests/test_interactive_vision.py`、`backend/tests/test_interactive_understanding.py`、
  `backend/tests/test_preview_api.py`、`backend/tests/test_interactive_intents.py`、
  `backend/tests/test_text_license.py`、`backend/tests/test_text_migration.py`、
  `backend/tests/test_frozen_materials_packaging.py`。
  完整前端 / 后端套件与 `npx vue-tsc --noEmit` 由 Lead 本轮各跑一次。
- **验证：** 后端定向用例全部通过；前端定向用例全部通过；
  `python scripts/check_docs.py` 与 `python scripts/check_docs.py --selftest` 通过。
  真实应用核对由独立验收子任务在隔离端口执行（含封面按钮真实命中）。
- **Known limitations：**
  - 遗留 `kind="image"` 数据**不做迁移 / 自动转换 / 兼容恢复**：加载归一化时按未知 kind 的既有兜底落到文字卡，
    旧图片卡上的附件关联不再显示；需要保留旧图的板面必须手动重新关联附件。
  - 图片不再有卡片内阅读态：超长图在卡片可用空间里等比缩小后细节较小，看细节要走「查看原图」；
    这是本轮的取舍，不是缺陷。
  - 封面模式下卡片正文层不再接收指针（事件穿到封面与卡片本身），卡片正文里的交互只保留显式放行的子树
    （草稿 / 冲突提示、头部按钮）；本轮未做真机 Tauri 壳内复验。


### P66 — 结果卡 Markdown、几何权威化与续用协作（2026-10-15 第三十八轮）

- **Status：** completed
- **Implementation：**
  - 需求（用户在测试中确认的 P1 缺口 + 本轮集中修复）：①结果卡真正解析并显示 Markdown；
    ②放大、平移、展开阅读与浮层变化不再让卡片自动移位、跳动或新重叠；③新建 / 移动卡片有可靠落点与尺寸保护；
    ④QIO 获得获准范围内的几何信息，能提出有依据的相对布局；⑤用户许可后的结果卡可作为后续提交依据；
    ⑥同一提交批次内新生成的卡片与组可被后续已批准操作引用；⑦用户要求实际排列时排序真的改变卡片位置。
    契约见 `docs/interactive-mode-contract.md` §17（取代 §1.4 的 reply 行、§15.1 / §15.2 的临时槽位口径、§15.6 相关已知限制）。
  - **F1 正式几何唯一权威**：`frontend/src/interactive/cardLayout.ts` 的 `planExpandedLayout` 退化为**恒等映射**
    （`slots` 逐字段等于正式矩形、`displaced` 恒空）；删除 `expandedRect` 的视野夹取与
    `displaceLegacy` / `displaceEnhanced` / `pushAway` / `candidateMove` / `MAX_DISPLACE_ROUNDS` 的生产用法；
    `BoardCanvas.vue` 删除 `layoutPlan` / `expandedSlots` / `userSizedCards` / `isTemporaryDisplay` /
    `previewRectOf` / `resizeAreaFor`，`BoardPreviewLayer.vue` 删除 `expandedSlots` 输入与 `preview-expanded` 容器。
    缩放 / 平移 / 窗口变化 / 聊天窗口 / 工具栏 / 审批浮层开合不再改变卡片坐标、尺寸、组成员与成员顺序；
    展开阅读只切卡内呈现；卡片可大于视野或部分在屏外；只有用户点击「定位／完整查看」时才一次性调整视野
    （大卡只对齐左上角，不两边轮流对齐）。`EXPANDED_PREVIEW_SIZE` 标 deprecated 且无生产引用。
  - **F2 统一几何约束服务**（新 `frontend/src/interactive/geometry.ts`）：`resolvePlacement`（拉伸走 bounded：
    只把被拉伸的边停在邻卡边界 − 间距，绝不移动别的卡片；移动 / 新建走 free：确定性找附近空位）、
    `clampCardSize`（180×96 .. 1600×1200，**视野不是上限**）、`placeRelative`。
    拖动 / 拉伸期间关闭几何过渡，预览与提交共用同一入口（所见即所得）。
  - **F3 结果卡 Markdown**：`frontend/src/components/interactive/BoardCard.vue` 的 `kind="reply"` 正文本体改用既有
    `frontend/src/components/MarkdownContent.vue`（`source = card.content`，不传 reveal），支持标题 / 加粗 / 斜体 /
    删除线 / 普通与有序列表 / **任务列表** / 引用 / 链接 / 行内代码 / 代码块 / 表格；
    `card.content` 仍是 Markdown 原文，「复制原文」给完整原文，代码复制保留代码原文；历史普通文字结果照旧显示。
    表格 / 代码 / 长链接不撑宽卡片、不引入横向滚动；沿用 `markdownPipeline` 转义与 `isSafeExternalUrl` 白名单。
    结果撰写提示词明确允许用 Markdown 组织结果（`backend/src/agent/interactive/prompts.py`）。
  - **F4 结果卡续用**：`models.CHECKABLE_KINDS` 加入 `reply`，`visible_except_deleted` 对结果卡取用户的
    整卡许可（新结果卡默认未许可，许可持续有效，隐藏 / 删除 / 取消许可立即退出范围）；
    `submission.diff_states` 引入 `result_added` / `result_updated`（载荷带正文与来源）；
    `understanding.build_context` 交付可见结果卡的完整正文与 `meta.reply` 来源
    （intentId / understandingId / executionId / updatedAt）。前端 `board.ts` / `submission.ts` /
    `stores/interactive.ts` 同步许可判定、可见范围计数与勾选显示。
  - **F5 批次内类型化临时引用**（`card:<symbol>` / `group:<symbol>`，裸符号按卡片）：
    新增 / 成组操作可声明 `ref`，只能引用**同一意图内更早声明、类型正确**的符号；
    `understanding._validate_operation` 先收集声明再校验引用（拒绝不存在 / 重复 / 前向 / 自引用 / 跨意图 /
    类型错误 / 未获准）；正式对象 id 由服务端在 `execution.build_plan` 稳定分配，重复批准与重试不重复创建；
    审批预览与执行后暴露同一份符号 → 正式对象映射。
  - **F6 几何上下文**：提交载荷可带冻结视图（板面坐标的可见区域 + scale），
    `understanding.build_context` 增加 `geometry{unit, unitNote, view, cards, groups}`，
    **只覆盖本次获准范围**；量不到时如实为 `null`；纯缩放 / 平移 / 浮层变化不构成新意图、不调用模型。
    画布在 `BoardCanvas.vue` 里发布 scale（`interactive/submission.ts` 的显示态桥）。
  - **F7 新建落点与相对摆放**：新 `backend/src/agent/interactive/placement.py` 统一落点与尺寸
    （默认 reply 320×240 / text 260×170；显式 `x/y` 生效且冲突时确定性找附近空位；省略时用
    `place{relativeTo,side,gap}` 的相对摆放或冻结视图内的空白落点；**禁止默认 (0,0)**）；
    审批预览与执行共用同一函数与同一冻结视图。
  - **F8 两种排序**：`order_group` 不带 `arrange` 只改逻辑顺序（文案明确说明）；
    带 `arrange:"row" | "column"` 时真实写入成员 `x/y` 并更新组框。
- **Tests：** 前端 `frontend/src/interactive/__tests__/geometry.test.ts`（新增）、
  `frontend/src/interactive/__tests__/c-continuity.test.ts`（新增）、
  `frontend/src/components/interactive/__tests__/iml-b-reply-markdown.test.ts`（新增）、
  `cardLayout.test.ts` / `board.test.ts` / `irl-c-layout.test.ts` /
  `components/interactive/__tests__/{cardExpandLayout,irl-c-canvas,irl-verify-size-semantics,cardToolbarAnchor,fix025-connect-resize,fix025-independent-verify,irl-verify-panzoom-draft}.test.ts`
  （按 §17 改写被否定的旧行为断言：展开放大与让位、视野夹取、视野上限、展开中只提交 `{w,h}`）。
  后端 `backend/tests/test_interactive_{submission,understanding,execution,intents,board,verify}.py`、
  `backend/tests/test_text_license.py`。完整前端与后端套件由 Lead 在集成完成后各跑一次。
- **验证：** 全量前端与全量后端套件、`npx vue-tsc --noEmit`、`python scripts/check_docs.py` 由 Lead 执行；
  真实浏览器验收（headless）由独立验收子任务执行，证据见 `_iml-evidence/`。
- **Known limitations：**
  - 结果卡正文没有前端展示层长度上限，因此没有前端截断标记；后端上下文对结果卡正文有上限并带
    `textTruncated` 显式标记（前端不截断，故不会把围栏 / 列表 / 表格截成半段）。
  - 拖动落点仍由 `frontend/src/interactive/board.ts` 的既有下落策略负责（为保留「拖到卡片上自动成组」
    的显式分组语义）；`geometry.ts` 覆盖拉伸与新卡落点，QIO 移动 / 审批预览在服务端复用同口径。
  - 本轮不扩展通用工具调用、不开放删除 / 隐藏 / 改用户已有卡片正文 / 替用户改许可，不纳入其他 P2 外观功能。
  - 桌面壳（Tauri）内未复验；浏览器验收为无头 Chromium（受「不在桌面开窗」约束）。

### P67 — v0.2.9 打包台账（2026-10-15 第三十九轮）

- **Status：** completed
- **范围**：把第三十八轮（P66）的互动模式集中修复合并进 main、版本号升到 0.2.9、产出**带更新签名**的 Windows 安装包。
  本轮先打包、再发布：安装包产出并通过闸门后，已创建 GitHub Release `v0.2.9` 并上传安装包 / 签名 / 更新清单。
- **版本号**：`backend/pyproject.toml`、`backend/src/agent/__init__.py`、`backend/src/agent/api/server.py`、
  `backend/uv.lock`、`frontend/package.json`、`frontend/package-lock.json`、
  `frontend/src-tauri/Cargo.toml`、`frontend/src-tauri/Cargo.lock`、`frontend/src-tauri/tauri.conf.json`
  全部 0.2.8 → 0.2.9。
- **提交**：`22a4651a`（`chore(release): v0.2.9 版本号与发布说明`，内容含 `fix/interactive-markdown-layout-continuity` 的全部改动）。
- **产物**：`dist/QIO_0.2.9_x64-setup.exe`（sha256 `c732ca5b…`）、`QIO_0.2.9_x64-setup.exe.sig`、
  `latest.json`、`QIO_0.2.9_x64-setup.exe.build.json`、`SHA256SUMS.txt`；
  sidecar `qio-backend-x86_64-pc-windows-msvc.exe`（58.6 MB）。
- **发布闸门**：18 项全部 PASS —— 含 `installer.sig` / `latest.json.signature` 的 key id 与内嵌公钥一致、
  `sidecar.fresh`（与 release 后端逐字节一致）、`models`（内置模型与清单一致）、`nsis.payload`（含 0.2.9）、
  `repo.commit`。记录见 `docs/releases/release-history.jsonl`。
- **验证（本轮只跑一次全量）**：前端 `npx vitest run` 全绿；`npx vue-tsc --noEmit` 0 错误；
  `python scripts/check_docs.py` 通过；后端 `uv run --frozen --no-sync pytest -q` 全绿。
- **Known limitations：**
  - 应用内更新链路（真实签名产物 + 真实更新源）未做端到端验证；闸门输出里列出的真机人工清单
    （安装 → 首次启动 → 打包后端启动 → 内置模型就位 → … → 更新 → 卸载）仍待执行。
  - 更新清单已随 Release 上传（`latest.json` 指向 tag `v0.2.9`）；客户端能否取到更新取决于其更新源与网络，本轮未做端到端更新验证。
  - 第三十八轮自身的已知限制见 P66。

## P68 — 第三方组件声明与分发合规（2026-10-17）

### 一、缺口（本轮补齐的对象）

- 仓库里**没有任何 `LICENSE` / `NOTICE` / `THIRD-PARTY` 汇总文件**：唯一的授权字样是根
  `package.json` 里 `npm init` 留下的 `"license": "ISC"`，那不是一次授权决定。
- 安装包里只有内置模型那一份声明：`tauri.conf.json` 没配许可证页；`build_runtime.ps1` 裁剪出的
  内置 CPython 运行时**不带** PSF 许可证；前端产物 `dist/` 里一行许可证文本都没有。
- 缺护栏：下次加依赖时声明不会自己跟上，也不会有任何检查因此变红。

### 二、本轮做了什么

- `LICENSE`（仓库根）：QIO 自身的授权改为 **MIT**（Copyright (c) 2026 左修源、尚雅淇），
  并同步四个清单里的 SPDX 字段（根 `package.json` / `frontend/package.json` /
  `frontend/src-tauri/Cargo.toml` / `backend/pyproject.toml`）。
- `scripts/licenses/build_third_party.py`：从三套锁文件推导声明，输出 `licenses/third-party.json`、
  `licenses/texts/<SPDX>.txt` 与 `THIRD-PARTY-NOTICES.md`。本机装不出来的平台依赖（锁文件是全平台的）
  由 `--refresh` 从 crates.io / PyPI 各取一次元数据（含 `.crate` / 发行包里的许可证文件与真实版权行），
  落进 `licenses/overrides.json` 的 `fetched` 段；此后**离线可重现**，CI 与用户侧都不需要联网。
- `scripts/licenses/check_third_party.py`：离线覆盖率校验 —— 锁文件组件是否一个不漏、有没有 `UNKNOWN` 许可证、
  每个被引用的许可证是否都有全文、声明正文是否与清单同步、每条是否至少有版权行或上游出处；
  `--selftest` 用合成工作区逐条证明这些检查**既会绿也会红**。
- 分发出口：设置页新增「关于」分区，把 `LICENSE` 与 `THIRD-PARTY-NOTICES.md` 的**原文**内联进构建
  （离线可读、不看后端）；`build_runtime.ps1` 把 `LICENSE-CPython.txt` + `NOTICE.txt` 放进内置运行时，
  缺许可证直接构建失败，不再静默出包。
- 文档：`docs/compliance.md`（口径 / 命令 / 已知边界）、`licenses/README.md`、`SETUP.md` 的验证清单与
  `README.md` 的许可证小节；CI 的 `docs` 任务加了覆盖率门禁。

### 三、验证

- `python scripts/licenses/check_third_party.py --selftest`：七项检查逐条红 / 绿。
- `python scripts/licenses/check_third_party.py`：锁文件组件全部覆盖、许可证全文齐、正文与清单同步。
- 后端 `backend/tests/test_third_party_notices.py`：把「覆盖率检查本身会不会静默放过」也变成自动化断言。
- 前端 `frontend/src/views/__tests__/SettingsView.test.ts`：导航里有「关于」、切过去能读到版本、
  许可全文与第三方声明原文（声明长度不足即视为没生成）。
- `npx vue-tsc --noEmit` 与 `npx vitest run`：类型与用例全过。

### 四、边界（不粉饰）

- NSIS 安装向导**没有**许可证页：Tauri v2 的 bundle 配置里没有对应字段（`gen/schemas/desktop-schema.json`
  里检索不到），文本改由应用内「关于」与安装目录里的资源提供。
- 标准许可证全文取自 SPDX `license-list-data`；`PSF-2.0` 直接用随包 CPython 的那份 `LICENSE.txt`。
  校验保证「组件不漏、全文在、正文与清单同步」，**不逐字节比对上游**。
- 覆盖范围是**过覆盖**：三套锁文件里的全部组件（含只在 Linux / macOS / Android / WASM 用到的）都声明了，
  而 QIO 目前只发 Windows 安装包。
- 上游确实没有版权行的条目（例如 `objc2` 的 `.crate` 里没有许可证文件）按「有上游出处」处理，声明里单列一节写明。
- 更新通道经过第三方加速镜像（内容有 minisign 验签，但代理可见请求元数据）；这是隐私面的事实，不是许可合规问题。

### 五、留给后来者的约束

- 声明是**生成物**：改依赖必须重跑生成器并提交生成物；覆盖率检查进 CI 就是为了这件事不再靠自觉。
- `licenses/overrides.json` 的 `manual` 段是人工确认的例外，脚本永不覆盖；`fetched` 段可被 `--refresh` 重写。
- 要改 QIO 自身的授权：替换 `LICENSE` 并同步 `README.md`、`docs/compliance.md` 与
  `frontend/src/views/SettingsView.vue` 的「关于 → 授权」文案（测试里有对应断言）。

### P69 — QIO 整体改造第一阶段：提示词 / 消息组装 / 记忆记录基础（2026-10-21）

- **Status：** partial
- **范围与基线**：整体改造分四阶段推进，本阶段只做第一阶段。基线提交 `aef3c43c`（origin/main）；
  公共契约冻结提交 `d265d033`；整合分支 `feat/qio-refactor-p1`（本地，未推送）。
  契约与交接：`docs/qio-refactor-contract.md`；验证台账：`docs/qio-verification-ledger.md`。
- **提示词单文件**：全部固定模型可见文案统一到 `backend/src/agent/prompts.py`
  （`PROMPTS` / `TOOL_DESCRIPTIONS` / `ARGUMENT_DESCRIPTIONS` / `TOOL_ARGUMENT_DESCRIPTIONS` /
  `CONTEXT_LABELS` + 纯组装入口）。互动模式、text 档协议、开发指南、维护 / 提炼 / 实体 / 滚动摘要规则、
  子任务 / 通知 / 工具反馈 / 媒体事实等文案从各模块迁入；旧英文摘要 / 提炼提示词删除，
  旧常量改为指向同一来源的薄别名；工具描述与参数说明只从 prompts.py 取。
- **消息组装**：新增 `backend/src/agent/core/message_assembly.py`。主对话请求 = 系统规则 + 模式规则 +
  实际工具定义 + 结构化上下文块（`ContextBlock`，key 来自 `CONTEXT_LABELS`）+ 实际对话消息 +
  当前用户原文与真实多媒体内容块；删除 `turn_orchestrator` 的「【用户消息】」拼接与通知轮的
  「【系统通知】」拼接。回答协议三档适配器只注入一次；补救调用保留通用规则再追加补救要求；
  `[[QIO:ANSWER]]` 原值保留；text 档工具协议保留 `tool_calls` / `name` / `arguments` 键名。
- **结构化工具结果**：`ToolResult` 增加程序自有字段（`protocol_version` / `status` / `code` /
  `data` / `model_payload`），`tools/contracts.py` 提供 `ToolStatus`、`ToolEnvelope`、
  `envelope_json`、`json_schema_for`、`validate_model`；结构化结果的 `data` 完整进入模型消息，
  旧工具正文兼容；新工具参数 schema 与本地校验来自同一 pydantic 模型（enum / additionalProperties 真正执行）。
- **记忆记录基础**：封块摘要按完整消息 / 可追溯分段覆盖整段再合并（不再只取尾部）；滚动摘要只处理
  未覆盖的新消息，覆盖位置与输入版本持久化（迁移 44，补偿迁移 45）；主对话上下文使用
  「滚动摘要 + 未覆盖原文」；存取路径不再按旧 2000 字上限二次截断；原文保留、失败可重试。
- **知识 / 实体**：主对话新增知识走 `propose_knowledge`（一律待审批、不能按分类绕过、待审不进注入面、
  证据程序校验、复用知识面板审批）；后台提炼输出新增 `epistemic_status` / `evidence_ids` 并按原分类规则
  决定是否需用户确认；明确拒绝写墓碑且不被其他入口无理由重存；`correct_knowledge` / `correct_entity`
  支持稳定 ID 与版本 / 修订 CAS，明确纠正当场生效；`create_entity` 先查重、同名不同身份不合并、
  不覆盖受保护信息；后台实体输出 `decision`（create / update / observe / identity_uncertain）与证据。
- **其他接线**：`continue_from_fragment` 登记后**当轮**即把片段摘要与头尾原文作为工具结果真实内容
  交给模型（受 FocusPolicy 预算、omit 如实），不改变本轮归属、不影响排队消息；实体完整卡片优先注入
  与总预算约束保持；附件与多媒体能力仍以适配器声明为准。
- **验证（只做定向，未跑完整集合 / 完整 CI）**：见 `docs/qio-verification-ledger.md`。覆盖三种适配模式
  与回答协议、特殊字符 / JSON 不破坏组装、schema 与参数同源、摘要完整覆盖与滚动覆盖位置、来源与
  确定程度存取可读、低影响分类不能绕过新增审批、明确纠正当场生效、同名实体不误合并、历史接续
  当轮生效；模型调用全部 fake / mock，不联网、不使用真实 Key。
- **Known limitations：**
  - 只完成四阶段中的第一阶段；第二至第四阶段未开始，后续阶段从本阶段整合提交继续。
  - 按四阶段共同验证预算，本阶段**未跑完整测试集合、未启动完整 CI**；完整验收轮次保留给四阶段收口。
  - 平台验证仅 Windows 开发机；桌面壳（Tauri）实机与真实模型链路未在本阶段复验。
  - 兼容期保留：旧 `build_injection` / `short_term_items` 文本路径仍在（供旧测试）；通知轮的记忆块
    暂时沿用旧渲染文本；prompts.py 里少量旧常量作为指向新来源的薄别名保留，待后续阶段清理。
  - 本轮不推送子分支、不创建 PR、不合并 main、不发布安装包（按预算与流程约束）。

### P70 — QIO 整体改造第二阶段：独立后台维护 / 记忆待确认入口 / 安全恢复（2026-10-21）

- **Status：** partial
- **范围与基线**：从第一阶段整合提交 `5021a4d5` 继续；契约冻结 `bcce3a2a`；
  整合分支 `feat/qio-refactor-p2`（本地，未推送）。契约与交接见
  `docs/qio-refactor-contract.md` §12；验证台账见 `docs/qio-verification-ledger.md` 阶段二部分。
- **变化计数与调度**：知识正式生效的新增 / 实质修改写入变化日志（（chain_id, version）幂等）；
  维护自身写入、未审批提议、通知、失败重试不计数；20 条变化或最老变化等待 30 轮任一触发
  （可配置）；新变化不重置较早内容的等待；无待处理变化不累计等待、不调模型；按快照推进处理位置，
  期间新增变化留给后续；单次处理量上限、同一时间只运行一轮、失败保留位置并退避；
  与工具输出清理 / 工具记录保留 / 自动化建议调度分开。
- **维护动作**：merge（至少两个不同 ID，保留有效信息、条件、归属、确定程度与来源，
  provenance 保存 merged_from 全部来源关系，不删除历史）；normalize（恰好一个 ID，只做
  不改原意的格式整理 / 明确文字修复）；ask（持久化为待回答事项，不直接改知识，同一问题不重复催问）；
  不同类别 / 归属 / 确定程度不做自动合成；未知 ID、版本过期、非法候选拒绝并保留原记录；
  不回落旧 revoke / expire 分支。
- **独立回答**：仅依据当前事项、涉及知识版本、必要来源与用户本次回答；revise / revoke /
  needs_clarification / deferred / no_change 全部本地强校验；写前重查版本（CAS），冲突不改；
  用户回答是依据，模型不能自报批准 / 验证；后台问答不写主对话 messages / 摘要、不计用户轮次，
  更新后的知识由正常注入路径读取。
- **记忆待确认入口**：两类事项（主对话新增知识待审批 / 后台独立问题）共用入口但按
  kind / handler 区分处理程序与动作；展示问题、对象、来源、修改前后与真实状态；
  回答、批准、修改后批准、拒绝、稍后处理；维护记录与单条 / 整批恢复预览和结果；
  事项持久保存，不以临时审批等待代替；不把 `entities/pending.py` 当作实体预选库或问答服务。
- **安全恢复**：预览不写库；逐条 / 整批恢复通过领域操作与新版本记录实现，不删除历史；
  保护用户后来发生的修改，无法安全恢复的部分明确说明；多来源合并保留各来源关系。
- **来源展示**：后台写入、API 读取与界面「后台整理」筛选使用同一组来源键与标签映射。
- **验证（只做定向 + 真实界面检查，未跑完整集合 / 完整 CI）**：见
  `docs/qio-verification-ledger.md`。覆盖跨重启累计、新变化不重置等待、已处理位置、
  维护自身不循环触发、无效候选不撤销原知识、重复问题不重复催问、独立回答不进主对话、
  多来源合并保留信息、逐条/整批恢复保护后续修改、审批不被显示为已证实、来源筛选可找到
  后台修改记录。真实界面检查（临时数据目录 + Playwright 无头浏览器）断言与截图见台账。
- **Known limitations：**
  - 只完成四阶段中的第二阶段；第三、四阶段未开始，后续从本阶段整合提交继续。
  - 按四阶段共同验证预算，本阶段未跑完整测试集合、未启动完整 CI；完整验收轮次留到最终收口。
  - 平台仅 Windows；桌面壳实机、真实模型链路未复验（维护 / 回答全部 fake/mock）。
  - 维护批次 / 事项 / 操作与用户 turn 去重日志暂无归档清理策略（契约未要求），长期增长待后续评估。
  - 通知轮记忆 legacy 文本块、旧 build_injection 文本路径仍在；第三阶段继续清理。
  - 本轮不推送、不创建 PR、不合并 main、不发布。


### P71 — QIO 整体改造第三阶段：互动模式接入完整主循环（2026-10-21）

- **Status：** partial
- **范围与基线**：从第二阶段整合提交 `6e71abbb` 继续；契约冻结 `f80a5fa8`；
  整合分支 `feat/qio-refactor-p3`（本地，未推送）。契约与交接见 `docs/qio-refactor-contract.md` §13。
- **主循环接入**：提交作为主循环 Turn 受理（同一 TurnManager / adapter / 工具循环），
  TurnContext 由程序绑定 mode / board_id / submission_id / 原话题 / 续作身份；
  普通模式不暴露板面工具，服务端仍校验模式与对象权限；保留提交快照与「提交给 QIO」；
  聊天是否另发回复由 QIO 判断，已保存意图 / 记录等待的轮次允许不回复、不启动强制补救。
- **退休旧链**：独立意图理解模型调用、整份互动 JSON 独立重写修复调用、空正文卡片二次
  模型生成均不再用于新流程；旧历史记录只读兼容；移除 `interactive/prompts.py` 中禁止接入
  通用主循环的旧约束。
- **板面工具**：12 个工具从可信上下文取当前板面（模型不能传其他 board_id）；意图含目标、
  预期、材料依据、结构化 scope、依赖与冲突；授权对象是目标与影响范围，实质扩大范围使旧授权
  失效并重新确认；中文说明 / 模型自报批准不构成授权；外部服务 / 文件 / 电脑操作仍走各自权限。
- **账本与并发**：对象真实版本 + 写前 CAS；operation_key 幂等 / 冲突；创建数量按意图与续作
  累计；依赖 / 冲突同事务解析并校验环与自依赖；写入冲突只暂停受影响步骤；独立意图可在资源
  允许时并行，数据库提交按需要串行。
- **等待与续作**：等待事项与原提交持久关联（迁移 49），跨重启恢复；用户回答后自动继续原提交，
  不要求再次点击提交；回答不足或属于其他事项不清除等待；问题由工具参数写出、系统作为正常
  对话呈现一次，不二次生成。
- **完成 / 失败 / 恢复**：删除旧 `_settle_failed` 默认整体回滚成果的行为；部分失败保留有用成果、
  标明未完成部分、条件恢复后继续、不重复建卡；`partially_completed` 不永久锁死意图；
  `board_finish_intent` 事务内核对意图版本 / 有效授权 / 完整权威账本 / 子任务 / 等待 / 冲突 / 依赖，
  迟到完成不覆盖取消 / 暂停 / 新尝试；成果引用由服务从真实操作记录生成。
- **前端**：意图工作台（目标 / 范围 / 依赖 / 冲突 / 真实状态）、批量与单条确认（前后端门槛统一
  两条）、结果卡完整 Markdown（表格 / 代码块 / 折叠 / 滚动）、等待 / 局部冲突 / 部分完成 / 失败 /
  可续作、恢复预览与结果；长正文不静默截断；文件按文件交付；编辑保存不调用模型。
- **验证（只做定向 + 真实界面检查，未跑完整集合 / 完整 CI）**：见
  `docs/qio-verification-ledger.md` 阶段三部分。覆盖普通模式 / 伪造板面身份、旧授权与扩大范围、
  重复操作、依赖环、回答续作、问题一次呈现、两条意图批量确认、部分失败保留成果、局部写入冲突、
  并行不越权、完成工具漏查运行任务、长正文完整保存。
- **Known limitations：**
  - 只完成四阶段中的第三阶段；第四阶段未开始，后续从本阶段整合提交继续。
  - 按四阶段预算，未跑完整测试集合 / 完整 CI；完整验收留最终收口。
  - 平台仅 Windows；桌面壳与真实模型链路未复验（模型 fake/mock）。
  - 前端失败重试 / 中断轮重发暂不携带互动绑定（按普通回合执行，不越权），后续可持久化进快照。
  - 旧理解读取 / 重试 / 取消 API 与旧执行链保留为历史兼容，新流程不使用；后续可集中清理。
  - 本轮不推送、不创建 PR、不合并 main、不发布。


### P72 — QIO 整体改造第四阶段（最终阶段）：子任务续作 / 凭据 / 工具闭环 / 集中验收（2026-10-21）

- **Status：** partial（本地实现与集中定向验证完成；完整 CI 最终运行进行中，以运行记录为准）
- **范围与基线**：从第三阶段整合提交 `c68ec692` 继续；契约冻结 `92d927f2`；
  整合分支 `feat/qio-refactor-p4`（本地 + 已推送阶段分支，未合并 main、未发版）。
- **凭据与子任务**：新增可用凭据目录工具（不返回密钥，区分模型凭据与业务服务凭据）；
  QIO 自主选择凭据与模型（尊重用户指定/权限/上限）；`_qio_subtask` 保留控制字段
  （credential_ref 必填，model/context/evidence_ids/budget 严格校验，先剥离再传业务参数，
  仅 subagent 工具可用）；真实终态（queued/running/completed/partially_completed/failed/
  cancelled/budget_exhausted）；完整结果保留 + 分页读取；可靠通知去重；结果回原任务原话题续作，
  不打断当前交流、不强切界面，尊重取消/暂停/目标修改，迟到结果不复活已取消任务。
- **工具开发闭环**：完整版本摘要进入模型可见 data（不再只存在 facts）；测试、注册、真实条件
  验证分开记录，模拟测试+注册不能声明 usable；条件与授权充分时做必要真实验证，不足时明确
  待验证范围；已放弃任务允许只读查看与材料复用，禁止写入/测试/提交；工具确认重大缺陷可暂暂停用、
  修复验证后恢复；受限子进程表述为「不是安全沙箱」。
- **自动化建议与引导**：结合完整需求、实际结果与已有工具判断（不再只看前 200 字），可空、
  拒绝后无新依据不催问；公共结构化规则 + 实际 schema + 独立 JSON 数据，不注入聊天回答标记；
  首次引导最多两问、可空、可跳过、失败不阻塞；旧 ToolCreator 生产调用退出，旧理解链退役。
- **提示词单源**：本阶段新增文案全部写入 prompts.py；10 个工具 description 改为引用
  TOOL_DESCRIPTIONS（不再保留旧常量生效副本）；补全 board_* 与凭据目录中文展示名；
  DEV_GUIDE_SECTIONS 七节更新；[[QIO:ANSWER]] 与机器 claims 保留。
- **验证（本地集中）**：定向回归宽集 577 项通过；D 独立契约测试 57/57；D 相关回归 414/414；
  D 广域回归 619 项仅 1 条（工具 description 单源）已修；离线评估 sha16=d1a8ff768a8afad6
  （与阶段一基线一致，topic/retrieval 未退化）；D 真实界面 19/19（上一轮）与最终复跑记录；
  完整 CI 第一轮在旧 SHA 自动运行失败（不作验收依据）、第二轮在 `b1025c66` 失败于 14 条
  旧测试未适配（已修复），最终修复 SHA 重新运行作为第二轮完整验收。
- **Known limitations：**
  - 真实模型/真实凭据下的端到端质量未验证（模型 fake / 种子数据）；完整用户旅程、Windows
    安装包/原生窗口、发布闸门未验证。
  - A 的 `tests/test_p4_a_*.py` 独立用例未落地（当前覆盖靠既有 subagent 测试适配 + 冒烟）；
    B 的停用/恢复仅程序 API，无工具/界面触发入口；真实条件验证依赖真实凭据链路，当前如实
    记录 unavailable。
  - 旧理解链测试按退役口径 skip（原因写入模块，替代套件为 test_p3_*/test_p4_*）；旧理解读取/
    重试路由仍保留为历史兼容（仅旧记录）。
  - 维护批次/操作/等待/工具记录长期清理策略未做；通知模型从「注入运行中轮」改为「排队+独立
    通知轮」，语义变化已记录于台账。
  - 未合并 main、未发版。


### P73 — 对话页 v2：对话工作台（2026-10-22）

- **Status：** completed（实现、集成、本地验收与完整 CI 均完成；代码 SHA `4607a4b1`，CI run 39762585293 全部 job 通过；未合并 main、未打包、未发布）

### 一、交付范围

- **布局与视觉**：顶部全局栏（QIO 字标 / 设置入口 / 窗口按钮最右；浏览器不画假窗口按钮）、
  左侧任务栏、中央对话工作区、输入区上方五图标入口、右下角记忆星球入口（保持既有样式与行为）、
  右侧只读多标签文件预览。颜色继续只用 `frontend/src/styles/tokens.css` 的令牌；亮色/暗色/
  减少动画均可用；对话区域不出现横向滚动。
- **任务栏**：统一平铺展示真实任务与需要处理事项，当前/历史两个视图；状态诚实映射
  （未知不显示成运行中、部分完成不折叠成完成、预算耗尽与取消独立），需要处理项置顶，
  同一工作去重；点击原位展开、可同时展开多项；长输出按需读取；只有后端真实支持的动作才出现；
  等待审批项打开统一弹窗（左侧不放批准/拒绝按钮）；迁移继续开发 / 撤销授权 / 放弃开发；
  恢复事项合并到左栏并可打开既有恢复区；三栏可拖宽、可收起、窄窗口临时收起、变宽恢复。
- **中央工作区**：话题标题与说明、全局异常区（历史读取失败可重试且明确「不代表没有历史」）、
  流式输出 / Markdown / 代码 / 表格 / 链接 / 附件与多媒体事实、历史按需加载、虚拟列表、
  复制、工具详情、预算继续、轮次失败/中断/取消的逐轮说明、耗时与用量可展开、
  真实未读计数的「回到最新消息」；执行记录与解释按实际顺序穿插，可定位左侧任务。
- **输入区与五入口**：多行输入 / 中文输入法 / 快捷键 / 草稿与附件（气泡内上方）；发送/停止
  共用按钮四态（有内容→发送并排队；回复中无内容→停止；空闲无内容→禁用；有内容但未满足条件
  →保留发送态并给真实原因）；入口①发送异常与草稿恢复、②从历史继续、③话题切换建议、
  ④排队消息、⑤待审批；有事项才显示、默认收起、一次最多展开一个、再点收起；审批入口直接
  打开统一弹窗并收起其余四项。模式切换只在输入区旁一处。
- **统一审批**：固定位置弹窗；按结构化身份分派（有 `approval_id` 走既有审批接口，知识候选走
  知识接口），不按文案猜；队列一次一项、可切换、新申请不替换正在编辑的；关闭/稍后只隐藏；
  暂缓不因重渲染/重连/轮询重复自动弹；提交防重；失败、超时、失效、已在别处处理分别说明；
  自动弹出遵守「主智能体新增权限 / 后台工作 / 重点确认」规则，知识保存不自动弹。
- **知识保存**：「确认保存 / 修改后保存 / 不保存」；修改后保存使用新增的原子接口
  （单事务以用户确认后的内容生成 active 版本，版本 CAS、幂等、冲突说明），
  不再使用「先批准原文再修改」的非原子流程；星球待确认的同一操作委托同一接口。
- **记忆问题提醒**：只显示真实待回答数量；加载失败与 0 分开；点击打开现有星球并定位待确认事项；
  回答仍走既有维护接口、不进入主对话上下文；不改星球入口样式与功能。
- **右侧预览**：多标签（身份不是文件名：附件 id+版本 / 回复代码块 / 任务成果）、同一身份复用标签、
  同名不同文件不合并；图片 / PDF / DOCX / XLSX / PPTX / 文本 / 代码 / Markdown 只读阅读，
  搜索 / 复制 / 缩放 / 翻页 / 外部打开按类别真实可用；全窗口与返回；文件更新按真实版本刷新、
  保留阅读位置、不切换标签、不重新打开已收起预览栏；旧请求不得覆盖新版本；不支持 / 过大 /
  无权限 / 缺失 / 解析失败分别说明。
- **持久化与恢复**：布局偏好与预览轻量位置写入 `/api/settings/ui` 的 `conversation_layout`；
  任务历史与「知道了」状态有后端持久化（迁移 55 的 `task_feed` / `task_ack`），
  不依赖组件内存或 localStorage；断线、乱序事件、后端重启按权威快照与实例归属恢复，
  历史任务不会被重新标成运行中。

### 二、共用能力边界（与并行任务 R9）

- 审批：只做**统一呈现与类型分派**，工具/权限仍走既有 `/api/approvals/{id}/respond`；
  知识侧是新增的只追加接口 `POST /api/knowledge/{id}/approval`。审批存储替换点集中在
  `frontend/src/stores/unifiedApprovals.ts` 与 `frontend/src/components/approval/UnifiedApprovalModal.vue`。
- 文件预览：沿用既有附件身份与 `/api/attachments/{id}/preview`，未新建第二套文件身份或表；
  未调用/修改互动板面的 `board_preview_state`。
- 窗口按钮/关闭：只调整顶部栏排布与外观，Tauri 调用与关闭语义一行未改；
  R9 的托盘/真正退出替换该层时不需要碰布局代码。
- 记忆问题：只读既有 `/api/memory/pending` 与既有回答/稍后接口；未重做星球布局。

### 三、验证

- 前端：`npx vue-tsc --noEmit` 通过；`npm run build` 通过；`npx vitest run` 全量 3745 项通过。
  首轮全量曾发现一批仍指向旧界面位置的断言（发送恢复 / 被让位草稿 / 设计守卫 / 不完整结局提示），
  按 v2 位置更新后通过，未放宽行为断言；另修复一个只在 UTC 环境失败的时区相关断言。
- 后端：全量 4610 通过 / 51 跳过；任务栏、知识原子保存、设置布局、迁移升级路径与迁移不变量的
  定向用例全部通过；离线评估 sha16=`d1a8ff768a8afad6`，与既有有效基线一致。
- 真实界面：实际运行后端 + 前端，用浏览器在 1440×900 / 1024×768 / 800×600 / 480×600、
  亮色/暗色/减少动画下检查；验证左右栏拖动与收起、窄窗口临时收起与全窗口预览、五入口
  互斥展开、审批自动弹出与暂缓、任务多项展开、历史阅读与回到最新、长中文/网址/代码/宽表格、
  多文件标签、文件更新、对话/设置往返与记忆星球定位。截图见 `docs/conversation-ui-screenshots-v2/`。
- 完整 CI：run `39762585293` 全部 job 通过（docs / frontend / backend py3.11 / backend py3.12 /
  backend windows / rust ubuntu / rust windows / frozen worker / install e2e / dedup preflight）；
  首次 run `39759115586` 仅 frontend 因上述时区用例红，其余 job 已通过，修复后重跑全绿。
  最终记录见 `docs/conversation-page-v2-merge-notes.md`。

### 四、已知限制

- 桌面壳（Tauri 窗口拖拽、最小化/最大化/关闭、托盘与真正退出）未在本轮真实安装包中验证：
  界面检查在浏览器环境完成，窗口按钮按既有桌面外壳接线保留、不画假按钮。
- 回复代码块与任务输出正文只存在内存（契约要求不写布局偏好）：刷新页面后对应标签仍在，
  但正文需要从原消息/任务详情重新打开。
- PDF 文字搜索只提取前若干页文字；PDF 不提供正文复制；Word/Excel/PowerPoint 预览是结构化
  正文/表格，不是逐页原版面渲染；旧版 Office 不支持内嵌预览。
- 任务栏的「工具执行」只收录有真实调用记录的、值得展示的执行（外壳类工具不重复列出）；
  未触发的维护与仅计划中的工作不进入列表。
- 未合并 main、未打包、未发布。

### 五、后续可复用

- `frontend/src/conversation/contracts.ts` / `previewTypes.ts` / `layoutPrefs.ts`：任务、预览身份、
  布局偏好的共享形状与纯函数；`stores/conversationShell.ts` 的偏好持久化可直接被互动页复用。
- `stores/conversationPreview.ts` + `components/conversation/preview/*`：与板面坐标无关的通用只读阅读器。
- `stores/unifiedApprovals.ts` + `components/approval/*`：按类型分派的统一审批队列（R9 替换存储时只改这两处）。
- `services/task_feed.py` / `api/task_feed_routes.py`：统一任务展示快照与持久记录，可继续接入互动执行任务
  （需在任务身份与权限边界上单独约定，不应与对话页任务混为一谈）。

### P73 — R9 统一授权 / 源文件引用 / 桌面关闭与退出（2026-10-22）

- **Status：** partial（本地实现与定向验证完成；全量测试、完整 CI 与安装版真机验证在收口时记录，以实际证据为准）
- **范围与基线**：开工时重新 fetch 确认 `origin/main` = v0.2.10 = `249a63443c72b3dad011b5446ee9920f4dadf0fd`（无更新）。
  契约冻结提交 `ca95236f`（`docs/r9-contracts.md`、迁移 52–54）；主开发分支 `r9/main`；
  并行分支 `r9/w1-auth`（统一授权/审批）、`r9/w2-exec`（执行边界/脚本/自建工具/子智能体）、
  `r9/w3-attach`（源文件引用/文件身份/迁移）、`r9/w4-ui`（文件卡/上传/预览/权限 UI）、
  `r9/w5-shell`（托盘/关闭/退出）。验收矩阵见 `docs/r9-acceptance-matrix.md`，合并记录见 `docs/r9-merge-notes.md`。
- **概念纠正**：不再以 topic / turn / session_id / 工具开发任务记录或子智能体任务作为授权有效期；
  统一授权依据是"具体用户指令 + 对象 + 操作 + 目的 + 边界与例外"。turn/topic 只用于追踪与校验。
- **三种权限模式**：`read_only`（只读）/ `restricted`（受限，首次安装默认）/ `unrestricted`（完全无限制）。
  旧值 `default→restricted`、`plan→read_only`、`accept-edits→restricted`、`bypass→unrestricted`，
  读取时归一、新写入只写新值；只有用户可通过 `PUT /api/security/mode`（`user_confirmed=true`）提高模式，
  工具/模型/子智能体没有切换路径；模式常驻显示，切换严格模式不自动批准待审批项。
- **统一授权边界**：新增 `backend/src/agent/security/`（modes / effects / instructions / intent / engine /
  gateway / context / operations / internal_data / audit / tempfiles）。文件工具、命令、脚本、自建工具、
  依赖安装、子智能体、子进程都在真实执行前经同一 `AuthorizationGateway` 判定；判定以实际效果为准，
  不以工具名或工作目录代替；审批通过 `submit` 持久化为待执行操作并立即返回（不阻塞后续对话），
  批准后由执行器重核对并执行。
- **指令授权**：确定性意图提取（明确文件+动作 / 修复指定项目目录 / 明确删除具体文件）；grant 随指令结束关闭，
  不因同话题、同目录或历史许可延续到后续要求；一次批准不扩张到其他对象/服务/内容。
- **内部运行数据**：数据库、内部配置、凭据等直接以文件/脚本方式维护时，必须明确范围+警告+后端校验的
  连续三次确认（CAS 推进、任一步取消即停止、范围变化重置），适用于全部模式（含完全无限制），
  覆盖脚本、自建工具与子智能体；正常设置保存/记忆整理走各自受控接口，不要求三次确认。
- **审批 v2**：默认有效期 600 秒（原始截止时间，刷新/暂缓/重启不续期）；持久化可恢复操作与上下文
  （operation/payload/fingerprint/instruction/目的/模式）；状态机 waiting→approved→executing→
  completed/failed，rejected/cancelled/expired/invalidated/interrupted_unverified；原子领取防重复执行；
  批准后执行前重核对模式、对象身份与位置、内容与范围、批准是否仍有效、是否已执行；崩溃后 executing
  不自动重放、标待核对；退出不清除有效待审批。
- **源文件引用**：本地附件不再复制内容副本，登记真实源位置、文件身份（Windows volume+file_id / POSIX
  dev+ino）、内容摘要、来源与归属（用户原有 / 用户提供首次落盘 / QIO 生成）；读取、预览、媒体解析、
  历史附件读取与获准写回共用 `resolve_source` 单一入口；添加附件/创建文件卡只构成提供材料，不授权修改；
  无路径材料（浏览器上传、剪贴板、截图）首次落盘到工作区并记为用户材料；删除附件引用不删源文件；
  文件移动/改名后只有文件身份相同才自动修复，同名/同大小/同时间/同内容不足以认定，无法唯一确认时让用户选择。
- **文件卡片**：卡片快照/互动上下文暴露文件名、类型、源引用 id、来源、可用性、用户说明；
  「允许 QIO 查看」改为「作为互动材料」（只控制是否主动纳入互动上下文，不是操作系统文件访问权限）；
  删卡只移除卡片与引用、源文件保留；复制卡共享源引用，撤销/重做不破坏共享。
- **旧数据迁移**：先盘点附件表、真实文件、消息/卡片/预览引用；迁移 52 的审计表支持幂等与断点续跑；
  有有效原路径的核实后改源引用（原文件与历史副本不同则保留差异事实）；无原路径但保存文件仍在的登记为
  当前源文件（用户材料，不删不复制）；全部失效的保留缺失状态与重新指定入口；登记迁移与磁盘清理分开，
  清理清单经批准后才进系统回收站；首次迁移不搬动现存文件。
- **删除与系统回收站**：用户业务文件删除统一走 Windows 系统回收站（SHFileOperationW，ALLOWUNDO）；
  失败时保留文件并如实说明，不静默永久删除；卡片/引用删除与源文件删除是不同动作；不新增默认永久删除能力。
- **桌面关闭/托盘/退出**：设置 `ui.close_behavior ∈ {ask, tray, quit}`（默认每次询问）；标题条关闭、
  系统 CloseRequested 与 Alt+F4 统一处理；隐藏到托盘不触发退出收尾、可恢复窗口；直接退出与托盘退出共用
  真正退出流程：停止受理新工作（写请求 503）→ 有界收尾（`SHUTDOWN_GRACE_SECONDS`，初值 8.0 秒）→
  保存状态并保留有效待审批 → 结束本实例管理的执行与进程（按 pid/Job Object，不按进程名）→ 核验；
  壳等待 sidecar 按 pid 进程树退出，超时 `taskkill /PID <pid> /T /F` 兜底。复用既有实例归属与 Job Object。
- **验证（定向 + 真机；全量测试与完整 CI 在收口时只跑一次，见合并记录）**：
  - 后端定向矩阵：三模式与引擎、指令范围、内部数据三连、审批生命周期/重启恢复、附件源引用/身份/迁移/
    卡片引用、执行边界与子智能体范围、退出收尾与设置接口；命令与输出见 `docs/r9-merge-notes.md`。
  - Windows 真机脚本（真实运行，证据在 `_r9-evidence/`）：`scripts/verify_r9_execution_boundary.py`
    （Low IL 写 Medium 文件被拒、Job Object 收进程树、只读 argv 白名单、只读拒绝任意脚本）、
    `scripts/verify_r9_recycle_bin.py`（真实回收站条目、还原、占用失败保留）、
    `scripts/verify_r9_file_identity.py`（移动/改名/复制件/编辑）、
    `scripts/verify_r9_process_tree.py`（两棵独立进程树、只按 pid 收本实例）。
  - 前端：`npx vue-tsc --noEmit` 与定向 `npx vitest run`；设置页/关闭行为/审批卡/文件卡/上传入口覆盖。
- **已知边界（本轮如实保留）**：
  - Low IL 能阻止写入 Medium 完整性对象，但不阻止读取、不阻止网络；它降低风险，**不是安全沙箱**。
    无法可靠约束的脚本路径明确拒绝执行并报告"暂不可用"，不静默降级。
  - 脚本运行时拼接路径/间接写内部数据无法在执行前可靠识别，只能事后核验并如实上报。
  - 截图/剪贴板/浏览器上传的材料是"本机首次落盘"的源文件；若浏览器在另一台设备，不声称能修改那台设备上的原文件。
  - 安装版真机验证（NSIS 安装/卸载、真实 GUI 托盘与关闭、退出时数据状态）以收口记录为准；
    未完成的项会在 `docs/r9-acceptance-matrix.md` 标注"未验证"及原因。
  - 本轮不合并 main、不发布安装包（按流程约束）。



### P74 — R9-F 授权与执行修复 / 受限模式模型审查（2026-10-22）

- **Status：** partial（实现与定向验证完成；完整 pytest 与完整 CI 按本轮纪律未跑，安装版路径未复验。以实际证据为准）
- **范围与基线**：从 R9 集成树建立修复分支（基线 `37a2d26b94df113b45fbc8d0702801c2d1a06cf3`，代码父提交 `b2d9229f`），
  修复审查发现的 A/B/C/D 四个问题；保留 R9 已完成的源文件引用、审批持久化、文件卡片、系统回收站与托盘退出。
  契约见 `docs/r9f-contracts.md`，验收矩阵见 `docs/r9f-acceptance-matrix.md`，合并材料见 `docs/r9f-merge-notes.md`。
- **A 统一程序执行边界**：新增 `security/launch.py`（LaunchSpec/LaunchPermit/ControlledLauncher）；`run_program`/`run_cmd`、
  自建工具 worker、依赖安装命令统一经受控启动；执行前重算 spec_digest（程序内容/argv/cwd/脚本内容/shell 身份）与许可一致，
  执行后再核对；审批后重放先核对 payload/指纹/对象身份，不一致即 invalidated；事后核验只作补充审计（区分 QIO 自身写库/日志与越界）。
- **B 受限模式模型授权审查**：删除关键词授权（`intent.extract_grants` 永久返回空，指令不再自动产生 grant）；
  新增 `security/review.py` + `requirements.py`：受限模式需要自然语言理解的操作由用户已配置主模型单独审查
  （无工具/无文件/无设置能力），输入由后端组装（用户原话+来源、修正/限制、具体操作、后端核实对象与来源、既有授权/拒绝），
  文件/网页/工具结果只作不可信材料；输出经后端结构/对象/操作/模式/limits 校验后形成**有界授权记录**（迁移 55），
  同 fingerprint 复用不反复调用模型；模型不可用/超时/非法输出不默认允许，退回普通审批。只读与完全无限制不调用普通授权审查。
- **C 工作区文件来源**：新增 `services/file_origins.py`（迁移 55 `file_origins`，身份优先查询）；fs_write/fs_patch/fs_delete、
  附件写回/原子替换、批准后执行统一查询后端来源；创建成功后记账 qio_generated，已存在用户文件记 user_original、
  首次落盘材料记 user_saved_material；模型传入 origin 不可信；外部替换不继承生成身份；重启后来源仍可查。
- **D 项目必要执行授权**：目录/项目授权不再靠关键词放行任意程序；执行审查记录绑定具体 program/argv/cwd 与 mode
  fingerprint，同一命令复用、不反复审批；新程序/新参数/新接收方重新判断；不以目录命中放行目录写、系统变更或外发。
- **用户明确禁止**：三种模式都遵守用户原话中的“只分析/不要修改/不要删除/不要外发”等禁止（只做拒绝，不产生许可；
  受限模式下禁止优先于模型审查；完全无限制也不因跳过审查而无视明确限制）。
- **授权生命周期**：以具体用户要求（原话+来源）为生命周期，不以 topic/turn/session 为有效期；同一要求的后台工作持有必要范围；
  后续新要求不继承旧记录；用户修正/撤回/替换、对象身份变化、模式变化、过期/拒绝后失效；子智能体只取父级授权子集。
- **CI 响应性红诊断与修正**：刷新实际状态——run `39771507126` 重跑仍红（349ms/300ms）、run `39784106563` 完整重跑红
  （981ms/300ms）。W4 同机对照测量（每格 30 次）：offload 段 p50≈19ms/max≈58ms（工作线程），整轮 gap 主要来自无关同步段与
  runner 调度（CPU 压力下 p90≈304ms，栈为 selector/线程等待）；据此把 `test_a_slow_retrieval_does_not_stall_the_turn_loop`
  的测量窗口收窄到慢检索刺激段（保留 446ms 回归签名检测力，未抬高阈值）。
- **验证（本轮只跑失败项/直接受影响项，未跑全量 pytest/完整 CI）**：R9-F 新增/适配测试与受影响既有套件的定向批次
  exit 0（清单与命令见合并记录）；真实 Windows 真机执行边界证据在 `_r9f-evidence/w2-exec/`；真实链路 E2E
  使用真实 create_app/引擎/注册表/工具调用（模型审查用 FakeAdapter 是唯一 fake）。
- **已知边界（如实保留）**：受限子进程不是安全沙箱；Low IL 只阻止写、不阻止读/网络；任意脚本的间接副作用无法全部事前约束，
  无法可靠限制的路径明确拒绝/要求确认；POSIX 无 Low IL 强制，仅事后核验；真实模型审查为单模型单次固定案例评估（18 条），
  未做多模型/多次稳定性统计；安装版自建工具重启恢复（A-075 路径）与批准后执行未复验，标“未验证”。

### P75 — R9-G 审批与授权机制修复（2026-10-23）

- **Status：** partial（实现、集成与定向验证完成；全量 pytest 与完整 CI 结果以本轮实际运行记录为准；安装版路径未复验）
- **范围与基线**：从 R9-F 集成树（r9f/main = 9211652650e6141d1dd32dc731fad450f8df71b5）新建修复分支 r9g/main；
  契约见 docs/r9g-contracts.md，合并材料见 docs/r9g-merge-notes.md。不新增运行期沙箱/文件范围隔离/网络隔离/子进程隔离。
- **关键词授权清除**：security/intent.py 整体删除（不是恒返回空保留）；review.py 删除 deterministic_allow_guard
  及全部动作词/拒绝词/条件/问号/引号规则；engine.py 删除 _MUTATION_PROHIBITIONS/_DELETE_PROHIBITIONS/_EGRESS_PROHIBITIONS
  与 _user_prohibition；cmd_tools.py 删除命令 token 全文搜索式永久删除判断。语义由模型理解，明确事实（程序身份/argv/cwd/
  对象/内容/路径关系/身份/版本/确认次数）由固定规则核对。
- **批量逐项**：新增 security/batch.py（BatchItem/逐项状态）；fs_delete、附件回收/写回等批量入口逐项 effect、逐项 authorize；
  需要确认的项合并为一个审批操作（payload.items 列明对象/动作/内容），已允许项直接执行、被拒/失效项保留，恢复只继续未执行且仍获准项。
- **要求版本绑定**：迁移 57（user_requirements.version、审批/授权记录/审查审计的 requirement 绑定列、
  requirement_resolutions 审计表）+ 补偿 58；新增 security/resolution.py（模型理解新要求/补充修正/撤回 + 结构化限制，
  后端校验目标存在/active/版本，失败记 unavailable、不关键词兜底）；操作通过 RequirementBinding contextvar 显式绑定，
  引擎不再读「最近一条 active 要求」；违反版本/状态即失效。
- **内容一致**：审查请求、授权记录、审批单、执行器共用操作指纹与绑定字段（对象身份/程序/argv/cwd/脚本摘要/接收方/模式）；
  审批单提交时写独立 operation_digest，批准/领取/执行前重算比对，operation 被改动即 invalidated；三次确认绑定同一操作内容，
  内容变化重置确认次数。
- **执行前重核对**：新增 security/recheck.py（要求有效/版本一致 → 当前模式重走统一判定 → 指纹/对象/内容/前置状态/
  条件证据/审批有效期/确认次数/委派范围）；文件工具、命令/审批执行器、批量子项、模型审查返回、人工批准恢复、后台继续与
  重启恢复接入；只有重核对通过才真正开始，检查后不再长期排队。过期审批不得批准/执行，重启不续期。
- **模式行为**：只读（固定查询允许、业务写/删除拒绝）；受限（固定规则/有效授权直接执行；需理解时模型审查；永久删除必须
  明确人工确认，模型 ALLOW 不可替代；模型不可用不默认允许）；无限制（跳过常规审查/审批，永久删除允许，用户结构化限制仍生效）。
  内部运行数据三次确认在所有模式仍成立，且不叠加第四次；普通同名文件不误判。
- **审计**：scripts/audit_r9g_decision_paths.py 同时做静态删除核对与真实装配动态核对（固定查询/批量逐项/重核对/缺绑定审批
  不执行/判定审计落库）；W4 产出判定入口与执行点审计表（9 判定入口 + 5 批准/恢复入口 + 8 执行点）。
- **验证**：定向测试覆盖见 docs/r9g-merge-notes.md；真实 Windows 固定查询/永久删除策略证据在 _r9g-evidence/w2-exec/；
  W1 真实模型评估（审查 26 案例误授权 0；要求解析 12/12）证据在 _r9-evidence/w1/；本地全量 pytest 一次启动后中止
  （不计为有效全量）；完整 CI 两次预算已用尽：run 39899916527（失败：补偿迁移链/模型调用清单/取消计数/e2e 假厂商）与
  run 39903261628（失败项收敛为 3 个：旧验收审批绑定/POSIX npm 符号链接固定查询/工具重启恢复绑定），上述问题已在
  最终代码提交 b0eb34b3 修复；最终树在 backend-py311/backend-py312/windows 上用定向 CI 对失败文件验证通过
  （run 39905806975 / 39905686592 / 39905693250），未再跑第三次完整矩阵。
- **已知边界（如实保留）**：受限子进程不是安全沙箱；Low IL 只阻止写、不阻止读/网络，POSIX 无 Low IL；解释器内联代码/
  xargs/sudo/嵌套 shell/shell 展开等无法结构性约束，按普通审批流程；旧版纯文本限制不参与新判定（结构化 limits 跨重启持久化
  受当前 schema 限制，已记录）；历史审批/授权缺 requirement 绑定字段时不默认补成宽泛有效授权，按重新判断处理；
  真实模型评估为固定合成案例，未做多模型/多次稳定性统计；前端未改（仅只读检查文案），安装版未复验。

### P76 — R9-H 审批与授权第三轮修复（2026-10-23）

- **Status：** completed（实现、集成、定向验证与完整 CI 已完成；安装版未复验）
- **范围与基线**：基线 r9g/main = 60208e325679303a545f1cac47bef2f0edb56b21；分支 r9h/main；契约 docs/r9h-contracts.md；
  只修前两轮已确定功能的真实接线/一致性/限制恢复/验证缺口，不新增产品功能、不新增沙箱/隔离机制。
- **要求绑定与真实原话**：app._execute_turn 改读 AppliedResolution.requirement，new/amend 用 binding_for 取有效版本并核对版本，
  withdraw 不绑定已撤回要求；解析成功但绑定建立失败记录异常/审计并阻止沿错误依据继续（不回退最近一条 active）；
  apply_resolution 传真实原话 message、message_id/turn_id/topic_id；resolution 增加 removed_limits 与 extract_limits（历史 legacy 限制重新理解），
  模型 corrections 不替代原话。
- **内容绑定**：工具为真实操作生产 content_digest/precondition_digest/spec_digest/script_sha256/delete_mode/conditions/来源目的地；
  引擎 _review_operation 从 effect.detail 读取并进入 operation_fingerprint；授权记录/审批单/执行前重核对/模型审查与人工批准共用同一操作事实；
  同文件身份不同内容、同路径脚本不同内容、内联代码/argv/cwd/前置状态变化不复用；正确内容在有效期内正常复用。
- **名称判定清理**：internal_data 删除 SENSITIVE_* 与 is_sensitive_credential_path；computer 删除 REDLINE_NAMES/REDLINE_SUFFIXES/_is_redline；
  内部运行数据只按 AppContext 真实注册路径 + 路径规范化/对象身份识别；普通 .key/.pem/.crt、credentials/.config 同名材料按普通规则；
  改名/符号链接/相对路径不绕过真实注册范围；不新增名称词表/正则/全盘扫描。
- **结构化限制持久化与恢复**：迁移 59 增加 structured_limits/structured_limits_state/structured_limits_source；补偿 60 = 补偿 58 全部 + 59；
  security/requirement_limits.py 提供规范化/序列化/合并；requirements 增加字段与 set_structured_limits/structured_limits_for/legacy_requirements；
  new/amend/withdraw 正确继承/修改/解除/失效；legacy/NULL 不当作明确无限制，unrestricted 相关操作不自动放行；
  RecheckService 重建 AuthContext 时从台账加载限制；后台/子智能体/审批恢复/重启沿用同一要求版本。
- **固定查询**：fixed_queries 兼容 POSIX 系统 bin 目录（含 /opt/<...>/bin）与 resolve 后同族脚本入口（npm-cli.js→npm），
  真实目标必须在系统安装根内；工作区/cwd/临时目录/任意脚本/同名替身仍拒绝；Windows/POSIX 平台规则测试覆盖。
- **验证**：定向批次（R9-H 新增 + 受影响安全/审批/工具/迁移/turn 套件）通过；完整 CI 运行号/结论见 docs/r9h-merge-notes.md；
  本轮独立预算：全量 pytest 最多 2 次、完整 CI 最多 2 次。
- **完整 CI（本轮唯一完整矩阵运行，run 39930482370，SHA bb260107f61f47a2874296de84f4a29f140f7959）：** 结论 success；
  10/10 jobs 绿（docs consistency / frontend / rust×2 / frozen worker / install e2e / backend py3.11 / py3.12 / windows）。
  首次运行中 py3.12 仅 test_health_probe_stays_responsive_while_slow_prediction_runs 746ms>300ms（同一 run py3.11 通过，既有 runner 调度地板 flake），
  windows 仅 test_timing_contract_verify 排队时长断言 864<647（仓库既有负载型 flake，docs/verification-acc-phase2.md 有记录）；
  按纪律仅重跑失败 job：py3.12 通过；windows 第一次重跑出现已知 sqlite 跨线程 InterfaceError（测试线程直接读共享连接，docs/status 历史记录），
  再次仅重跑 windows 后通过。未新增完整 CI 运行、未删检查/降阈值/放宽断言。
- **定向 CI（固定查询 Linux 平台）：** run 39925645309（backend-py311）success、run 39925793535（backend-py312）success（SHA c363fa33，
  覆盖 test_r9h_fixed_queries_platform.py / test_r9g_fixed_queries.py / test_r9g_deletion.py / test_r9g_cmd_binding.py）。
- **已知边界**：受限子进程不是安全沙箱；Low IL 只阻止写、不阻止读/网络；解释器内联代码/xargs/sudo/嵌套 shell 等无法结构性约束；
  conditions 已有结构贯通但当前无产生结构化条件的真实工具；move/copy/外发工具不存在，未伪造来源/目的地；批量附件部分子项失效 E2E 未覆盖；
  安装版未复验；旧数据 legacy 重新理解依赖模型可用，失败时只对相关操作要求确认、不清空全部授权。

### P77 — R9-H2 第三轮后续补修（2026-10-23）

- **Status：** completed（实现、集成、定向验证与完整 CI 已完成；安装版未复验）
- **范围与基线**：基线 r9h/main = 6ee961d293ec9d06390ca36c00fa491b895b4647（代码提交 bb260107f61f47a2874296de84f4a29f140f7959，完整 CI 39930482370 success）；
  补修分支 r9h2/main；契约 docs/r9h2-contracts.md。只修三个已复现缺口，不新增产品功能、不改变既定权限政策。
- **历史要求修正链恢复**：RequirementLedger.recovery_context 按真实 parent_id 组装根->当前完整链（原话/顺序/父子/版本/状态/已知限制/来源），缺父/环/版本不连续/非 active → complete=False；
  ModelRequirementResolver.extract_limits_from_context 由模型理解目标版本完整有效限制（考虑后续补充、明确解除/替换与顺序），空数组仅在完整理解后表示明确无限制；
  app 自动恢复使用完整链上下文，写入前重读 id/version/status/parent/chain_digest，条件写回 expected_*；失败保留 known limits 与 source 并标 unresolved，不覆盖为空限制。
- **解析失败状态一致**：解析失败/超时/解析器缺失/非法输出 fallback 显式写 structured_limits_state=unresolved、source=resolution_unavailable:<类>，不再立即把失败消息重解释为 structured；
  空数组只表示成功理解后明确无限制；unresolved/legacy 在引擎中禁止复用旧 allow/delete_permanent 记录；unrestricted 对相关操作复用既有模型审查做针对性语义判断（不是普通授权审查），审查不可用退回具体确认，避免静默允许也避免全部无关工作被阻塞。
  迁移 61 只对“structured + [] + 同 turn 有 unavailable/unknown 解析失败审计”的旧记录纠正为 unresolved；无证据的空数组保持不动；补偿 62 只重放追加 DDL，不重放数据 UPDATE。
- **内部数据真实对象身份**：迁移 61 新增 internal_data_identities；InternalDataIdentityStore 以 file_identity（Windows volume+FILE_ID_128 / POSIX st_dev+st_ino）登记真实注册文件/目录（目录有界枚举），
  原子替换旧身份 retired、新身份登记；注册路径缺失/身份读失败保留历史登记；同内容复制件身份不同不保护；
  internal_data.effect_targets_internal_data 支持路径 + target.identity + 身份登记 + known_paths 保守命中；engine/app 启动同步并注入；普通同名文件仍按普通规则，不新增名称/后缀判断。
- **验证**：A 17 项、B 16 项、C 身份/内部数据、迁移 61 纠正、Lead 真实主流程 3 项（含 Windows 硬链接实际执行）、宽回归 382 项、受影响套件 345 项通过；
  Linux 定向 CI run 39958384570（backend-py311，SHA ea60595b）success，验证新测试在 POSIX 上通过。
- **完整 CI（R9-H2 最终代码，run 39958699070，SHA 8cb92eab6d9724c910ed8b7b8642c149be0c5bde）：** 结论 success；10/10 jobs 绿
  （docs consistency / frontend / rust×2 / frozen worker / install e2e / backend py3.11 / py3.12 / windows）。首次运行即通过，无失败 job 重跑。
- **预算**：R9-H + R9-H2 累计完整 CI 2/2（39930482370、39958699070），均 success；本地全量未完成（不把定向当全量）；Linux 定向 CI 39958384570 success（backend-py311）。

- **已知边界**：POSIX 硬链接/身份语义由定向 CI 验证，POSIX 符号链接能力未单独真机；文件系统不支持 file id 时只能 known_paths 保守命中（新路径不猜）；跨卷移动身份变化按新对象；
  conditions 字段无真实工具产生；无 move/copy/外发工具；批量附件部分子项失效 E2E 未覆盖；安装版未复验；真实模型链恢复效果依赖模型可用。

### P78 — R9-H3 第三轮后续补修（2026-10-23）

- **Status：** completed（实现、集成与定向 CI 完成；完整 CI 未新增，累计额度 2/2 已用尽；安装版未复验）
- **基线与范围**：基线 r9h2/main = 7b66092e01f57121067c48588e8c3a30237367d0（代码 8cb92eab6d9724c910ed8b7b8642c149be0c5bde；完整 CI 39958699070 success）；
  修复分支 r9h3/main；契约 docs/r9h3-contracts.md。只修三处已复现问题，不新增产品功能、不改变既定权限政策。
- **已知限制优先级**：引擎判定顺序改为先计算当前绑定要求与有效限制（含 legacy/unresolved 已保存 known limits、context resolution_limits），
  先做限制冲突核对，命中即 DENY；之后才内部数据特殊规则、子智能体范围、永久删除、固定查询、模式、未恢复部分的具体确认。
  单项/批量/审批恢复/执行前重核对共用；旧 allow、人工批准、内部数据三次确认、权限模式均不能覆盖仍有效禁止；INTERNAL_DATA 映射为 write 参与限制核对；
  用户真实补充解除限制后按原版本/语义机制恢复正常。
- **恢复完全无限制模式**：删除 ReviewRequest.targeted_limits_review 字段/参数与校验例外；删除 engine 的 unresolved 普通审查路径；
  完全无限制模式不再调用 ModelAuthorizationReviewer（普通独立授权审查仅 restricted）；限制恢复仍由专用语义流程完成，不判断具体操作授权、不生成普通授权记录；
  unresolved/legacy 在 unrestricted 对相关操作返回具体确认（APPROVE），已知限制先 DENY；恢复成功后按既定规则继续；restricted/read_only/内部数据/永久删除规则保持不变。
  迁移 63 撤销可明确识别的 targeted 例外授权记录（unrestricted + model_review），保留行/审计；补偿 64 只重放 DDL；版本 64。
- **内部文件身份自动更新**：InternalDataIdentityStore 新增 note_committed_path/note_committed_paths/retire_path；AppContext 包装真实审批执行器 operations.execute，
  在 fs.write/fs.patch 真实提交成功后按明确 payload 目标更新身份；SettingsService 真实提交成功后通知 DB 路径；失败记录 internal_data_identity_error/ExecResult.detail，不把已成功写入改报失败、不静默降级；
  普通工具、tool.invoke、proc 不猜；只针对实际变更对象有界更新；启动 sync 与显式 refresh 保留但正常保护不依赖手动刷新。
- **验证**：A 11 项、B 14 项、C 10 项真实引擎/台账/执行器测试通过；宽回归 431 项、受影响套件 330 项通过；审查清理回归 124 项通过。
- **最终代码定向 CI（SHA 7f877bf3547b85a38c6209acb91971c453d3596d）**：run 39979313725（backend-py311）success、run 39979659563（backend-py312）success、run 39979919730（backend-windows）success；
  首次定向 run 39978484989 暴露 C 测试在 POSIX 上 logs/sub/.. 未建父目录的平台假设错误，已修测试并重跑通过（未放宽产品断言）。
- **完整 CI 未新增**：累计 2/2（39930482370、39958699070）仅作基线证据；本轮最终代码没有完整矩阵结果，需要额外完整 CI 额度才能声称完整 CI 成功。
- **预算**：累计完整 CI 2/2（39930482370、39958699070）已用尽；本轮不得触发第三次完整 CI；最终代码只做定向 CI，不把定向/基线绿色写成完整 CI 成功。
- **已知边界**：凭据无独立文件未单独接线（SQLite+OS keyring）；进程外日志轮转 Python 不可感知；原子替换退休旧身份但不全盘扫描旧对象可达性；
  POSIX 只做定向 CI，Windows 本机硬链接验证；安装版/真实模型/完整矩阵未验证；完整 CI 需额外运行额度。

### P79 — R9-H4 审批与授权机制大修（2026-10-24）

- **Status：** completed（实现、集成、Linux/Windows 定向验证完成；完整 CI 未新增，累计 2/2 已用尽；安装版未复验）
- **基线与范围**：基线 r9h3/main = 68101d6261253bb742d01af73a458bab2e0a1c5a（代码 7f877bf3）；修复分支 r9h4/main；契约 docs/r9h4-contracts.md。
- **实际影响与限制**：effect.detail 增加多实际影响 limit_impacts（execute/program、delete/path、read、internal_operation、directory）与完整性标记；命令已解析删除对象保留 delete 影响，程序影响使用 resolve 后真实程序；INTERNAL_DATA 按 internal_operation；read/program/path_prefix/directory 从提示词/解析/持久化到引擎一致；相对路径按 workspace/cwd；已知限制先于内部数据/scope/审批/模式/审查。
- **限制继承/解除**：父 unresolved 的 known limits 在 amend 继承，恢复失败/空结果不覆盖；removed_limits 精确部分对象/部分操作，无法精确时保留原限制并 unresolved。
- **撤回/内容一致性**：绑定撤回/替换/版本变化明确拒绝；模型审查返回后重读要求/版本/状态/限制并重算指纹；内容/脚本/程序等待期变化不采用旧许可；文件/补丁/附件/程序/脚本覆盖。
- **目录/批量**：目录有界枚举子对象（含受保护/内部数据），审批与执行比较目录影响；旧 path 目录 fail-closed；批量逐项，仅受影响项失效；平台语义去重；目录内保护子对象触发三次确认。
- **内部身份/确认**：Windows 创建时间、Linux statx btime（取不到保持未知，不用 ctime 冒充）；identity+birth 匹配，编号复用 different；替换同事务、旧对象仍存在保留；启动有界回填；可信提交自动更新；目录删除有界退休子身份；执行前按当前识别复核三次确认；runtime state 暴露错误。
- **子智能体/审批并发**：三模式 scope 不足 DENY；审批 CAS/事务/审计一致；unresolved 具体批准仅本次；proc.kill 快照/pid 生命周期核对；期限/重启/重复领取保持。
- **验证**：A 22、B 27、C 10、D 13、E 24、F 19；真实交叉 2；宽回归 553；受影响 336；核心 118/150。最终代码定向 CI（SHA 76e6248bfc6694367c43ef7fab4f7f9a9907fe11）：40008364084（py311）success、40008722828（py312）success、40009077589（windows，首次 sqlite 跨线程 flake，重跑失败 job 后 success）。完整 CI 未新增（累计 2/2），不把定向写成完整 CI。
- **边界**：Linux 仅 statx 支持 btime 的文件系统可区分编号复用，不支持时未知（路径/历史保护）；POSIX ctime 不作生命周期；跨卷移动按新对象；目录影响有界上限超限明确不确定；Windows 符号链接权限不足如实记录；安装版/真实模型/完整矩阵未验证；额外完整 CI 需新增额度。

### P80 — R9-H5 审批与授权补修（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 均完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h4/main d2403425（上轮定向代码 76e6248b）；修复分支 r9h5/main；契约 docs/r9h5-contracts.md。
- **问题一（文件等待变化）**：fs_write/fs_patch 执行前以真实磁盘核对 identity/base content/新文件 absent；RecheckService 路径不再提前返回跳过本地核对；等待期间修改/新增/删除/替换使旧方案失效；人工审批路径由执行器 base digest/identity 重核对。真实临时文件测试覆盖模型等待、人工等待、未变化、新增/删除/替换。
- **问题二（脚本替换）**：审查前 launch facts/spec/argument_files/spec_digest 作为绑定；直接模型允许、人工批准恢复执行前按当前事实比较；变化 invalidated，不把新事实替换旧依据；真实脚本替换测试证明新内容未执行，未变化正常执行，人工恢复路径同样拒绝。
- **问题三（patch 先读）**：fs_patch 在读取正文前构造真实 read effect 并执行读取授权；拒绝则不读/不算补丁/不修改/不返回正文；读写分别核对；spy 证明未读；禁读允许写/允许读禁写/读写允许及三模式覆盖。
- **问题四（批量逐项）**：approval 对多子项先 common_only 核对共同条件（要求 active/版本、期限、模式、确认基础）；整批共同失败终止；逐项用子项 effect 重核对，单项限制/内容变化只失效该项；每项结果及 partial/completed/failed 计数保留；真实工具→真实 decide→执行恢复测试覆盖单项禁止、单项内容变化、全部有效、全部失效、共同要求撤回（fake 回收，非真实系统回收站验收）。
- **问题五（结构化决策）**：AuthDecision 增加 decision_code（unresolved_limits_requires_confirmation），引擎按结构事实设置；operations 只认 code，不解析中文 reason；无 code 的旧审批不能据文字放行；仍核对 active/版本/已知限制/次数/期限；更改中文/空文字不改变行为，相同中文不能使其它 APPROVE 可执行。
- **问题六（程序限制扩大）**：程序限制只按平台规范化/真实路径/对象匹配（realpath 解析链接别名，Windows 大小写不敏感，POSIX 保留大小写），删除 basename/去后缀/统一小写回退；名称限制不支持隐式回退，历史含义不明保持保守。
- **验证**：新增 R9-H5 回归 23 项；基线 8f07f86f 上同一批测试 10 项失败（写入等待/新文件、patch 禁读三模式、脚本替换、批量、结构化、程序同名）；修复后 23 全过；宽回归 695 passed；核心/审批/迁移回归通过；agent.eval.run 通过。最终代码 SHA b5271538 的定向 CI：40065856405（py311）success、40066080217（py312）success、40066324492（Windows）success。
- **边界**：RecheckService 只在工具实际持有并调用时生效；执行前核对后到系统调用之间的竞态未消除（未新增沙箱）；真实系统回收站未验证；Linux/Windows 链接能力以平台权限为准；旧审批无结构化 code 将要求重新确认；未做完整 CI。

### P81 — R9-H6 审批与授权补修（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h5/main b5ae83c9（代码 b5271538）；修复分支 r9h6/main；契约 docs/r9h6-contracts.md。
- **问题一（补丁双读）**：fs_patch 补丁方案与 base 摘要来自同一次正文读取；不再在补丁计算后二次读取填摘要；执行前核对使用同一文本口径，文件变化/替换/删除使旧方案失效。真实文件在读取后、摘要形成前变化测试证明用户修改不被覆盖。
- **问题二（目录逐项影响）**：approved_operations 逐项重建 DELETE effect 时携带审批快照 directory_impact 的全部子对象路径、完整性、目录/保护事实；B 内深层子文件限制命中时仅 B 整体保留，A 完成并报告 partial；目录缺失/不完整继续失效。
- **问题三（补丁恢复读取）**：fs_patch 操作事实同时记录 read+write 影响；审批恢复在读取正文前用最新要求/版本/限制核对读取 effect，读取禁止时不读正文/不计算补丁/不修改；读取允许、修改禁止仍保留原文件。
- **问题四（程序名称）**：新增明确 program_name 结构化类型；历史裸 program 名称归一为 program_name（不静默当路径忽略），路径限制仍只按平台规范化/真实路径/对象身份匹配，不回退 basename/后缀/小写；program_name 只按名称主体匹配；提示词、解析、持久化、继承/解除、恢复和执行匹配同步。
- **问题五（普通写入读取限制）**：FsWrite 覆盖已有文件时操作事实包含 read+write；读取原内容/摘要前核对最新读取限制；禁止读时 content_sha256 调用为零且不覆盖；新文件不存在不虚构读取；allowed 路径保持等待变化失效。
- **验证**：新增 H6 回归 16 项；基线 b5ae83c9 上同一批 9 项失败（补丁双读、目录子项、patch 恢复读、程序名称、写读取限制三模式）；修复后 16 全过；宽回归 711 passed；H4/H5/审批/迁移核心通过；agent.eval.run 通过。
- **边界**：快照采用工具既有 UTF-8/换行规范化文本口径，不声称保留原始换行字节；执行前核对后到系统调用仍有竞态；program_name 按名称主体匹配，不覆盖同族不同名称解释器；真实回收站、安装版、真实模型、完整矩阵未验证。最终代码 SHA 80f67283 的定向 CI：40073102261（py311）success、40073349551（py312）success、40073582301（Windows）success。

### P82 — R9-H7 审批与授权补修（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h6/main e8cf89da（代码 80f67283）；修复分支 r9h7/main；契约 docs/r9h7-contracts.md。
- **问题一（程序名称漏拦/误拦）**：保留真实调用入口 requested_program；program_name 作为未解决来源，命中真实调用入口时进入具体确认/恢复，不由执行层直接允许或拒绝；不用解析后目标、basename、去后缀或版本号猜范围；入口 python 不静默执行，python3.11 不误拒 python3.12；路径限制仍只按真实路径/对象身份。
- **问题二（内部维护预读）**：FsWrite 对已有内部文件不再在确认前读取正文/摘要；只绑定身份/存在性并创建三次确认审批；确认前读取入口为零；确认完成后执行写入；用户明确读/写禁止仍拒绝。
- **问题三（删除摘要预读）**：FsDelete 先以元数据枚举目录并核对读取限制，再计算正文摘要/目录影响；文件/目录禁读时正文与摘要读取为零；审批恢复删除也在目录快照/内容摘要读取前核对最新读取限制。
- **问题四（重核对读取顺序）**：recheck_before_execute 先核读取权限，再读当前内容做前置/摘要核对；直接执行、模型等待、人工审批恢复共用读取优先顺序；被拒时不再为了诊断读取内容。
- **验证**：新增 H7 回归 9 项；基线 e8cf89da 上同一批 6 项失败（程序入口、内部维护、删除文件/目录/审批恢复读取、重核对顺序）；修复后 9 全过；宽回归 720 passed；agent.eval.run 通过。
- **边界**：程序名称只是来源绑定，执行层不按名称扩大范围；未绑定入口的 program_name 不直接决定权限。内部目录删除的正文快照属于既有内部维护流程，未宣称本轮改为完全延迟读取。执行前核对后到系统调用仍有竞态。真实回收站、安装版、真实模型、完整矩阵未验证。最终代码 SHA ca47d07d 的定向 CI：40084768559（py311）success、40085038728（py312）success、40085258787（Windows）success。

### P83 — R9-H8 根因修复（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h7/main 89397527（代码 ca47d07d）；修复分支 r9h8/main；根因记录 docs/r9h8-root-causes.md。
- **共同根因**：H7 局部修复程序名称时改了共享限制匹配函数，把“objects 为空=限制全部对应操作”与“对象存在但 program_name 未绑定”混成一个 return False；同时把名称未绑定从明确禁止降为可普通批准的 APPROVE；内部维护用取消内容摘要换确认前不读取；删除读取检查在循环内整体返回；脚本摘要同一字段承载两种事实。
- **问题一**：恢复 objects 为空=全部对应操作；区分空对象、仅对象、混合未解决名称；所有执行/恢复走同一规则。真实读取/写入/删除/执行整体限制验证三模式。
- **问题二**：program_name 按平台真实程序解析/对象身份绑定；同一对象换绝对路径仍拒绝；不同版本/程序不合并；明确禁止但对象无法绑定直接拒绝，普通批准不能覆盖；路径 A 不误拒同名 B。
- **问题三**：内部维护确认前只保存身份/size/mtime 元数据，不读正文；确认后先核对元数据再写；等待期间原地编辑/替换/删除/重建使旧方案失效。
- **问题四**：批量子项读取限制失败形成独立 denied 结果并继续其他项；A 可进入审批，B 保留并说明；不改目录整体语义；审批子项集合只含有效项。
- **问题五**：内部目录维护确认前只做元数据枚举；确认后只比较元数据影响，不读正文；普通目录中的受保护对象同样识别；临时回收验证。
- **问题六**：script_sha256 只表示单一脚本来源；参数文件集合由 argument_files + spec_digest 表达；提交/直接执行/审批恢复同一事实；未变化脚本批准后真实执行成功，变化仍失效。
- **验证**：新增 H8 回归 12 项；基线 89397527 上同一批 11 项失败；修复后 12 全过；宽回归 732 passed；agent.eval.run 通过。
- **边界**：内部维护元数据无法检测同 size/mtime 伪造；执行前核对后到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证。最终代码 SHA 967401c2 的定向 CI：40090355400（py311）success、40090608125（py312）success、40090842980（Windows）success。

### P84 — R9-H9 共享规则修复（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h8/main 92c7688b（代码 967401c2）；修复分支 r9h9/main；根因与覆盖表 docs/r9h9-root-causes.md。
- **共同根因**：H8 把规则修在专用分支/函数内：内部目录 content_deferred 分支只比较摘要未检查完整性；program_name 专用分支只看对象相关性不看 operations；只读模式在程序检查前分支；program_name 执行时按当前 PATH 重解析且路径只比较 realpath。因此共同检查没有成为所有执行入口必经步骤。
- **问题一**：目录完整性成为独立执行前提。审批时/执行时 complete 不为 true 均 invalidated，不由三次确认/模式/无限制/摘要相同豁免；普通目录与内部元数据快照同一规则；批量子项独立。
- **问题二**：程序限制使用已有文件身份/生命周期核对真实对象；硬链接、符号链接目标按同一对象处理；同内容复制件身份不同不合并；路径限制与名称绑定共用对象身份。
- **问题三**：program_name 在保存限制时按程序查找与文件身份固化绑定，执行/重启不再用当前 PATH 重解析；环境变化不转移禁止；无法绑定按 A 状态恢复/拒绝。
- **问题四**：程序名称专用分支复用共同操作范围；write/read/delete/execute 分别核对，operations 为空=全部；禁止修改不阻止运行。
- **问题五**：明确程序禁止在三种模式共同检查，早于固定查询/只读规则；固定查询仅在不违反限制时允许；审批/模式切换不能清除限制。
- **验证**：新增 H9 回归 8 项；基线 92c7688b 上同一批 5 项失败（目录不完整、硬链接、PATH 绑定、写限制误拦、只读固定查询）；修复后 8 全过；既有宽回归 732 passed；agent.eval.run 通过。
- **边界**：对象身份依赖平台文件 ID/生命周期能力；硬链接在平台支持时验证；内部维护元数据仍无法识别同 size/mtime 伪造；执行前核对到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证。最终代码 SHA 1f460c67 的定向 CI：40099823964（py311）success、40100043649（py312）success、40100395438（Windows）success。

### P85 — R9-H10 程序限制绑定生命周期修复（2026-10-24）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h9/main fec13810（代码 1f460c67）；修复分支 r9h10/main；根因与生命周期 docs/r9h10-root-causes.md。
- **问题一**：normalize/serialize/deserialize 不再查找程序、采集身份或补建绑定；历史无绑定原样保留。绑定只在要求保存/恢复流程由 attach_program_bindings 显式创建，写回记录 requirement_id/version/status/path/identity/birth/source；读取要求不刷新、不替换、不生成。
- **问题二**：program_name 在 write/read/delete/execute 文件操作中使用 program_binding.path/identity，不再把来源名称当相对路径；程序名称修改限制阻止真实文件修改，运行不受 write 限制影响。
- **问题三**：路径限制与程序对象共用路径 + 文件身份/生命周期核对；硬链接读取/执行均命中，同内容复制件身份不同不合并。
- **问题四**：区分无关对象、未绑定、有效匹配、绑定失效。原绑定路径仍在但身份/生命周期变化时进入拒绝/恢复，不自动绑定新对象，不静默放行绝对路径；旧对象硬链接仍保护。
- **验证**：新增 H10 回归 7 项；基线 fec13810 上同一批 6 项失败；修复后 7 全过；H9/H8/H7/H6/H5/H4 核心 217 passed；既有宽回归 732 项中 1 次身份复用测试环境性失败、同套件立即复跑 24 passed；agent.eval.run 通过。
- **边界**：绑定创建依赖保存时平台程序查找与文件身份读取能力；无绑定历史按未绑定恢复处理；同 size/mtime 伪造仍无法识别；执行前核对到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证。最终代码 SHA 0a0df3a1 的定向 CI：40106770972（py311）success、40107072141（py312）success、40107409191（Windows）success。

### P86 — R9-H11 程序对象状态判定与跨操作一致性修复（2026-10-25）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h10/main 80b06308（代码 0a0df3a1）；修复分支 r9h11/main；共同对象判定契约 docs/r9h11-contract.md；根因 docs/r9h11-root-causes.md。
- **问题一**：历史无绑定 program_name（state=structured、保存时解析失败）进入明确 UNBOUND/UNKNOWN，不再退化成 false：名称入口相同、当前 PATH 澄清到同一对象 = 绑定缺失；当前无法澄清且候选存在 = 无法确认；只有候选不存在/不同裸入口名/澄清到别的对象等可靠依据才是无关。读取正文/摘要/修改/删除/执行/审批恢复消费同一判定；拒绝时 0 次正文读取、0 次程序启动；普通批准不能解除。structured-but-unbound 走服务层专用恢复：只用要求原话/链/版本 + 后端只读候选事实补绑定，条件写入核对 id/version/status/chain_digest，恢复期间要求修改/撤回/链变化零写入；失败保留原限制/known/来源，历史原始 JSON 不变。
- **问题二**：已绑定对象使用保存身份（identity+birth）与候选对象核对，文件操作不再把 binding.path 交给路径比较并重新取该路径当前对象身份；原路径替换/删除/移动后旧对象硬链接/符号链接目标仍命中；原路径新对象按绑定失效拒绝/恢复，不自动继承或清除禁止；不同对象其它路径可靠无关；read/write/delete/execute 与审批恢复同一结论。
- **问题三**：编号与生命周期按共同身份契约核对（同一 compare_bound_identity），编号相同生命周期不同 = 另一个生命周期对象 → 绑定失效/恢复，不视为原对象也不直接当无关；保存有生命周期但当前取不到 = 无法确认（不默认一致）；生命周期只来自文件身份，不用内容摘要/mtime/名称相似/调用方自报。
- **验证**：基线 80b06308 上 D 独立验收 18 项 3 passed/15 failed；修复后 18 全过。H11 定向 146 项全绿（object_state 91、binding_recovery 15、cross_operation 22、D 验收 18）。Lead 复现探针（临时文件）基线 3/3 失败、修复后 3/3 通过（无绑定绝对路径真实执行/正文读取/硬链接替换）；H4-H10 核心 112、合计关键 258；宽回归 71 个受影响文件 1015 项：1014 passed、1 项在基线与修复后都以同一 Windows 环境原因失败、0 skipped；docs 一致性通过（95 里程碑、--selftest 6/6）；agent.eval.run 与基线输出逐字节相同。
- **边界**：绑定创建依赖保存时平台程序查找与文件身份能力；保存失败显式 unresolved，恢复依赖当前环境候选事实；当前无法澄清名称且候选存在按无法确认失败关闭。真实 file_id 复用本机不稳定复现（身份提供器边界注入，测试标注模拟）；真实硬链接/替换/删除/移动/绝对路径/PATH 变化为真实验证。执行前核对到系统调用仍有竞态；审批预检查行只持久化 reason（执行器 detail 带结构化码）；读取/执行边界命中即拒绝不生成 AuthDecision 审计行（ToolResult.data 带 object_decisions）。真实回收站/安装版/真实模型/完整矩阵未验证。最终代码 SHA d9d806b4 的定向 CI：40114369702（py311）success、40114727713（py312）success、40114932045（Windows）success（同一 SHA；首轮 py311 因单元测试平台假设失败，修正测试后重跑）。

### P87 — R9-H12 审批与授权补修（2026-10-25）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h11/main 503df11a（代码 d9d806b4）；修复分支 r9h12/main；契约 docs/r9h12-contract.md；根因 docs/r9h12-root-causes.md。
- **共同契约**：五态保留；UNRELATED 只能来自 UNRELATED_BASES 白名单（operation/类别/候选不存在/前缀外/服务范围/绑定身份不同/路径不同对象/生命周期不同/入口解析到另一对象），判定工厂强制校验；入口名不同、原名称查找失败、当前解析到别处、限制路径身份不可读、无完整绑定，单独都不能证明无关，改判 UNBOUND/INVALID/UNKNOWN；聚合优先级 MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED。
- **问题一（硬链接 alternate 绕过）**：删除“入口名不同=无关”提前返回；保存候选参与判定（同路径/同身份→UNBOUND，有候选不匹配→UNKNOWN，无候选时当前解析只作候选事实）；修复后绝对路径/不同名称入口在启动前拒绝（0 启动、无 marker）。
- **问题二（恢复转移历史对象）**：新保存与历史恢复分成两个入口；recover_program_bindings 只采纳保存候选（路径存在+身份可读+路径未变+候选 identity/birth 一致），写回前复核 + id/version/status/chain_digest 条件写入；无候选/不可验证/多冲突零写入保持未解决；当前 PATH 只作审计候选。修复后恢复绑定 A（source=saved_candidate_verified），绝不绑 B，恢复前后 A 都受保护。
- **问题三（显式路径身份不可读误放行）**：_path_core 对 target_observed=None 不再判 UNRELATED（目标不存在→path_limit_target_unreadable，存在但身份不可读→path_limit_identity_unavailable，均 UNKNOWN）；候选不存在仍可靠无关；修复后读硬链接在正文/摘要读取前拒绝（0 读取）。
- **错误契约测试修订**：H11 四处“名称不同/当前解析到别处/身份不可读=UNRELATED”、H11 独立验收“不同入口名放行”、H5/H7/H8 引擎级“名称差异 ALLOW”、H11 恢复“无候选从当前 PATH 绑定”均保留原输入按契约改期望；新增可靠无关邻例（候选不存在/操作范围外/已绑定不同对象/前缀外/已绑定入口解析到可核验另一对象）；未删用例、未 skip/xfail、未放宽断言。
- **验证**：基线 503df11a 独立验收 H12 6 红→最终 27 passed（H12 8 + H11 19，0 skip）；Lead 复现探针基线 3/3 失败→修复后 3/3 通过（0 启动/0 读取；恢复采纳 A 不采纳 B）；H12 新增 97 项全绿；邻近 H11+H4-H10 核心 260 项全绿；宽回归 75 文件 1113 项：1112 passed、1 项在基线与修复后都以同一 Windows 环境原因失败、0 skipped；docs 一致性通过（95 里程碑）；agent.eval.run 与基线输出逐字节相同。
- **边界**：execute 入口在 assessment 前采集 launch facts（程序内容摘要，H5/H7 既有设计），execute 验收口径为 0 启动 + 0 正文读取；审批重放网关未透传 assessment，权威判定在审批执行器入口 authorize_started；无保存候选的历史未解决绑定不做模型侧语义恢复（保持未解决零写入）；候选只有 path 且被换成另一对象时无法区分（不猜内容/mtime/名称）；真实 file_id 复用/身份读取失败用提供器边界注入（模拟）；真实回收站/安装版/真实模型/完整矩阵未验证。最终代码 SHA 245f76aa（生产代码 e4e478b4；4594358f→0a7a430b→245f76aa 仅验收测试平台修正与证据）的定向 CI：40122111129（py311）success、40122291315（py312）success、40122468968（Windows）success（同一 SHA；首轮因测试平台假设失败，修测试后重跑，生产代码未改）。

### P88 — R9-H13 审批与授权统一补修（2026-10-25）

- **Status：** completed（实现/真实流程回归/定向 CI 完成；完整 CI 未新增，历史累计 2/2 已用尽；安装版未复验）
- **基线**：r9h12/main e68fb9b7（生产代码 e4e478b4）；修复分支 r9h13/main（工作流 r9h13/a-e）；契约 docs/r9h13-contract.md；报告 docs/r9h13-report.md；根因 docs/r9h13-root-causes.md。
- **H12 交付记录更正**：三个绿色定向运行 40122111129 / 40122291315 / 40122468968 实际对应提交 245f76aac153bc621e86559173b0519c6562def1（生产代码 e4e478b4），而不是 4594358f；产品实现提交、最终测试提交与文档证据提交分开记录。
- **共享契约**：冻结 12 类共享事实的唯一含义与生产者/验证者/消费者（要求 id/version/status/chain；动作-对象配对；用户指定路径；最终执行路径；身份比较路径；对象身份/生命周期；读取前内容与摘要；拟写入内容与摘要；影响集合与完整性；当前模式；委派范围与撤销；审批期限/确认阶段/单次执行）；所有真实入口只构造 Effect 并调用统一授权入口，读取先于内容，逐动作逐对象核对影响，确认次数由后端按真实影响对象决定，身份/读取前内容/拟写入内容分离，操作目标与比较路径分离，恢复只使用保存事实，子范围逐动作逐对象，模式变化按当前规则。
- **A1-A3（要求继承/恢复/去重）**：amend 只对本次新增对象建绑定，继承条目原样保留（含未解决事实/候选/来源）；恢复只采纳带保存时 identity/birth 事实且当前观测一致的候选，缺字段零写入、保持未解决并提示澄清；去重键含绑定对象/身份/生命周期/来源，精确解除只移除指定对象，多命中歧义时保留限制并标 unresolved。
- **B1-B3（对象判定过严）**：未绑定 program_name 读取限制不再拒绝普通文件读取（新注册 basis program_concept_file_read_out_of_scope；执行/写/入口名/显式路径限制仍失败关闭；未识别程序本体按路径读取的残留边界如实列明）；路径限制保存 identity/birth/exists/observed_at/source，限制路径消失后可靠无关继续、旧硬链接仍保护；同名入口解析到独立对象按对象事实继续、绑定路径替换仍 INVALID。
- **C1-C2（读取先于内容）**：RunProgramTool/附件写回在正文/摘要/shebang/补丁/重新核验前先核读取权限；拒绝时读取 0 次、程序 0 启动、文件不变；写批准不解除读取禁止。
- **D1-D3（内容与影响）**：删除 path/size/mtime 摘要缓存，每次读真实内容重算（同大小同 mtime 改写被拒、未变化正常执行）；参数脚本按解释器 argv 语义识别并入绑定，有界采集超限显式不完整并拒绝按完整方案执行；递归删除携带真实目录子对象影响，不完整不执行。
- **E1-E3（附件/批量）**：最终真实目标参与限制/路径/审批绑定核对；身份/读取前内容/拟写入内容分离，正常写回/回收不再误判；批量整批只核共同条件、逐项使用自己的 effect/确认要求，单项失效不影响独立有效项。
- **§8 内部维护三次确认**：engine.required_confirmations 按真实对象推导（提交覆盖调用方值、重核对取 max）；模型 ALLOW 不完成内部维护；提交/批准/恢复/执行前按实际 confirmed_stage 核对；历史 1 次不能当 3 次；批量内部对象非首项命中；普通对象仍 1 次、无第四次。
- **§10 链接语义**：删除/回收链接保留用户指定目录项语义（符号链接/junction/失效链接），realpath 只用于身份与审计，不改写操作目标；审批恢复按 payload.is_link_entry/requested_path 回收目录项本身。
- **F1-F6（子智能体）**：读取也检查操作权限/撤销/有效期；内部数据与 unrestricted 不绕过子范围；复合操作逐动作逐对象核对；模型审查授权形成明确可委派子集（对象/版本/用途/边界保留），请求→授予→重试→撤销结构化状态；不足先向主智能体申请、无权限再请求用户。
- **§12 模式变化**：删除三处“仅模式字符串不同即失效”；放宽后当前规则允许的操作继续且只执行一次；收紧后不允许的停止；read_only 仍一律停止；不扩大用户禁止。
- **旧期望修订**：A1/A2/A3、B1/B2/B3、C2、§12 引起的 H11/H12/H4/H2/H6 旧断言按 H13 契约修订（保留原输入与复现证据）；新增 a/b/c/d/e/lead 六个专用测试文件；未删除失败用例、未 skip/xfail、未放宽产品断言。
- **验证**：各工作流基线红→修复绿（A 9、B 10、C 8、D 22、E 13、Lead 6 项）；最终受影响集合（48 个受测文件 / 728 项 collected）pytest exit 0；审批/附件邻近套件通过；agent.eval.run 与 H12 基线输出完全一致；docs 一致性通过（97 里程碑）。已知基线既有失败 test_r9h_content_binding::test_script_content_change_prevents_reuse（Windows 环境/非本轮引入，基线与修复后均失败）。
- **边界**：专用模型语义恢复未实现（历史缺保存身份时保持未解决，支持用户澄清路径）；B1 未识别程序本体按普通路径读取的残留边界；D2 采集有界（128 候选/8MB/64MB）未做无上限流式；文件符号链接本机无权限，POSIX CI 覆盖；执行前核对到系统调用仍有竞态；真实回收站/安装版/真实模型/完整矩阵未验证；只做受影响测试与三平台定向 CI（历史完整 CI 2/2 已用尽）。最终受 CI 验证的提交 17b9775c（r9h13/main）的三平台定向 CI：py311 run 40177769502 success、py312 run 40178148732 success（首次派发 40177772125 在队列层被并发组取消、未执行任何检查，同 SHA/同筛选重派）、Windows run 40177774774 success（同一 SHA）；其后仅有回填 CI 编号的 docs/证据提交。


### P89 — 三分支整合：对话页 v2 + 互动页 v2 + R9-H13（2026-10-25）

- **Status：** partial（整合实现、冲突裁决、数据库升级验证、前后端定向与真实界面检查完成；完整 CI 与最终验收以收口记录为准）
- **来源**：origin/main 249a63443c72b3dad011b5446ee9920f4dadf0fd；feat/conversation-page-v2 13ec09357cd1477348635096a3999eb2e4ef8298；feat/interactive-page-v2 9b23634892fa7fac0a8ccd471626d07d2bdefd0e（仍是最终提交的祖先）；r9h13/main 6f56d815d6be3136c446ecdf9ad7591256a5350e；追加交付 fix/interactive-v2-frontend-flow 2881346365062ab6b36adfdd08c7433b83576514。
- **合并顺序**：cv（无冲突）→ i2（无冲突）→ r9h13（19 冲突全部解决）→ 互动页 F01–F10 交付（自动合并 + vue-tsc/定向用例复核）。全程 merge --no-ff，不 squash、不改写来源历史、不重复摘取已被包含的历史修复（R9/R9F/R9G/R9H..H12 已在 R9H13 历史内）。
- **统一审批**：统一展示层继续负责队列与呈现，提交出口经 approvals.respondById → _respondItem 按结构化标志分派到 R9 生命周期（isLifecycleApproval → decide / confirmNext），旧 /respond 只服务无 state 的旧记录；三次确认由后端阶段决定。
- **文件与授权**：board_tools._card_snapshot 同时给出 display/组可见范围与经附件服务核验的源引用事实，全部调用点传 attachment_service；R9 的文件源引用、对象身份与授权前置在互动页 v2 上仍生效（工作流 C 的接缝复核与新增集成断言）。
- **桌面外壳**：WindowChrome 外观取页面线 v2，关闭/ask/托盘/真正退出取 R9 壳协议（X 只发窗口关闭请求，壳按设置执行）。
- **数据库**：页面线迁移改号到 R9 链之后（67 = task_feed/task_ack，69 = P3-D，68/70 = 统一补偿），R9 的 52/53/55/57..66 语义不动；REQUIRED_OBJECTS 取两线并集；新增分支线前置兼容修复（50 < 版本 < 70 且对象缺失时只重放补偿的 CREATE/ALTER，不写版本号、不改数据）。
- **验证**：五类来源库升级（空/main 51/cv 55/i2 57/r9 66）全部 → 70，必需对象齐全、原行保留、二次启动幂等；后端定向（迁移/审批/安全/互动/设置）与前端定向（审批/互动/外壳/对话页）通过；真实界面截图检查。完整 CI 记录见整合说明 §6。
- **边界**：真机 Tauri 关闭/托盘/真正退出与安装形态 E2E 未在本机验证（无壳二进制），按未验证记录。子智能体预算在 v2 生命周期路径只读展示（/decide 不透传 overrides）。

### P90 — 合并后前后端漏接修复与旧代码清理（2026-10-26）

- **Status：** completed（实现/定向验证/真实界面/真机桌面/本地全量/完整 CI 记录见本节与交付文档；未合入 main、未打包、未发布、未改版本号）
- **基线**：origin/main 1b03541eedd2dcaa9b82d6102108d717c71c6fb4；修复分支 fix/post-merge-integration-cleanup-20261025（五个工作流各自独立分支/worktree，Lead 统一集成）。上一轮整合为 P89。
- **F01–F06 能力修复**：互动页权限模式徽标常驻（收起/展开可见、单一状态源、读取中/失败如实）；统一审批「待审批 / 结果与历史」两视图（按 approval_id 建键、approved/executing 明确等待执行、终态持久可回看、两个聊天路由与设置/调试等非聊天路由均可达、0 待办仍可查）；批量逐项结果（真实 item_id/对象身份/状态/原因 + 整批汇总）；待审批恢复失败与历史读取失败分别可见并可重试（不清空、不冒充空集合、不续期、不重复创建、不推进确认阶段）；480 窄窗队列换行、对象身份在前、键盘可切换、状态/期限只显示一处；子智能体预算旧 tool_create 路径可编辑且「真的改过才发 overrides」并由后端测试证明执行器采用，只读生命周期记录不再给可编辑控件或「可修改后批准」承诺。
- **C01–C05 清理**：删除 17 个已不挂载的旧前端文件（含旧审批展示层 3 个）与对应旧测试/mock；旧浮层样式 interactive-shell.css 重构为 styles/layers.css 唯一公共来源（层级变量、工具栏测量、板面选择约束按真实钩子）；batchOpen 死字段/导出/断言删除并统一 tasksOpen；对话几何桥生产者改为 v2 互动对话窗真实可见几何（收起/卸载发布空、不读持久化假框）；过时 MIGRATED_CAPABILITIES 与假断言删除；QSlider/QioScrollArea、零消费者包装、旧面板文案函数、闲置演示入口删除；保留 /respond、/demo/advance 真实撤回、历史迁移、archive.py、sdfRings 与通用缩放手势工具等保护清单能力。
- **D01–D02 记录更正**：上一轮 CI 台账改为「一次完整矩阵跑完 9/10 + 单条基线失败、定向复验；另一次重复完整矩阵启动部分 job 后取消」，保留运行号与取消事实，明确 dispatch 元数据 head_sha 不等于实际受测 SHA、产品/测试/文档树区别；回退说明删除 force-with-lease 强推 main，改为保留历史的回退提交/PR 与数据库独立说明（本轮未执行回退）。
- **F07 真机桌面**：真实编译并启动候选壳（内嵌候选前端 + 真实 Python 后端，不用 stand-in sidecar、不用真实密钥），7 场景通过：X/ask 三选、关闭到托盘、托盘恢复、托盘菜单退出、记住选择与重启后生效、系统 WM_CLOSE、挂起后端阻塞退出（taskkill /T 核验、进程树清空）、重复关闭不叠加不二次退出；WindowChrome 观感与 R9 壳协议同时保留。安装形态、冻结 sidecar、系统关机注销、ACK 兜底、物理鼠标托盘未验证（见交付文档）。
- **CI 防重复**：ci.yml 排除本轮子工作分支 push、concurrency 按实际受测提交（PR head 回退 sha）、新增 workflow_dispatch（ref + expected_sha + checkout 后 SHA 校验）；本轮唯一完整矩阵由集成分支一次 push 承担，失败修复用 targeted-verify 定向复验（[skip ci] 策略见交付文档）。
- **验证**：本地后端全量与前端全量各一次（见交付文档 §9.2）；类型检查、生产构建、文档一致性、第三方声明、离线评估与基线对比按仓库要求执行；完整 CI 运行号与逐 job 结果、定向复验记录见交付文档 §9.3；真实界面 1440x900 / 480x600 与真机桌面证据见 §9.4。
- **交付文档**：docs/post-merge-integration-cleanup-20261025.md（能力对应表、F/C/D 逐项状态、删除清单、测试迁移对照、验证台账、未验证边界、合并准备）。
- **边界**：真实模型链路与 POSIX 平台未本地验证；安装包形态与系统关机/注销未验证；480 收起态徽标位于既有悬浮对话状态条内，该行可能与一张卡下边缘重叠（非新增浮层）；旧 /respond 兼容路径的执行结果没有持久化历史，界面不伪造进「结果与历史」。

### P91 — 后续修复轮：续作话题归属、审批闭环与通知可靠投递（2026-10-26 后续轮）

- **Status：** completed（五项修复与清理均实现并验证；本地全量/完整 CI/失败项定向复验/独立验收记录见下；未合入 main、未打包、未发布、未改版本号）
- **基线**：`origin/fix/post-merge-integration-cleanup-20261025` = 373fda40（P90 分支 tip；origin/main 仍为 1b03541e，未前进）；Lead 集成分支 fix/post-merge-followups-20261026，工作流 A–F 各自独立 worktree/分支。
- **A 互动续作话题归属**：续作 Turn 优先使用原提交保存的 binding.resumeState.topicId；当前选中话题不得覆盖；binding 缺失/话题无法核对时明确失败（不静默回退、不启动、不写绑定、不重复执行）。
- **B 审批后端**：旧 request() 落库行在输出中标记 legacy=true（不伪造 state=waiting），恢复保留原 turn/session/digest/期限绑定事实，旧审批状态用 interrupted_legacy 等独立事实；/decide 对 legacy 明确拒绝、/respond 继续服务且单次消费与绑定校验不变；执行结束写 approval_history（PK approval_id，后写真实终态）并发 APPROVAL_RESULT（executed/failed），「已批准/执行中」不显示成成功；预算 overrides 真实生效，不支持时显式 400 拒绝，不静默丢弃。
- **C 审批前端**：按 approval_id 合并 pending/history/事件；legacy 或无 state 项只走 /respond，绝不调 /decide、不补 state；重连/回前台主动补读；读取失败可见可重试；已批准等待执行与执行成功/失败严格区分；预算仅端到端支持 overrides 的旧路径可编辑。
- **D 通知投递**：只追加迁移 71 建 subtask_notifications（task_id 主键、payload、origin、created_at、delivered_at、attempts、last_error、last_attempt_at）+ 待投递索引；72 为补偿入口（SCHEMA_VERSION=COMPENSATION_VERSION=72，REQUIRED_OBJECTS 同步表/列/索引）；enqueue 只做持久化去重、不算交付成功；只有 notify turn 被 TurnManager 接受后才写 delivered_at；失败保留 last_error 待重试、重启继续投递；同一 task_id 至多一次、FIFO（跨崩溃窗口为 at-least-once，已如实记录）。
- **E 文档与许可**：本文档区分 P90 已完成事实与 P91 进行中；`docs/post-merge-integration-cleanup-20261025.md` §12 记录本轮范围与收口占位；`docs/compliance.md` 按实际许可证组（含 MPL-2.0 文件级弱 copyleft、LGPL-2.1-or-later 仅作为并列可选项）更正概括；本轮未改依赖/锁文件，未重新生成第三方声明。
- **验证**：本地全量一次——后端 601 文件/6013 项 exit 0；前端 452 文件/4025 项全通过；`vue-tsc --noEmit` 0 error；`vite build` 成功；docs/license/release gate/workflow 契约检查通过；离线评估与基线逐字节相同。完整 CI run **40257319813**（SHA 48b01f0c，event=push，10 job：9 success + backend py3.12 1 failure；install e2e/frozen worker/rust×2/frontend/docs/backend py3.11/backend windows 均 success，无 skipped/cancelled）。失败定位为**测试**跨线程使用同一 sqlite 连接（py3.12 抛 sqlite3.InterfaceError；产品代码未改、py3.11 与 Windows 同代码全绿），修测试 helper 后以 `[skip ci]` 推送 0c47e11b，targeted-verify py3.12 run **40263546217** success、py3.11 run **40263563419** success（均指定完整 SHA 并在 workflow 内断言实际 checkout SHA）。独立验收 F：五项反例全绿、保真 6/6、回归 101+81、真实界面 480 4/4；LIMITED：通知 AppContext 端到端未构造、网络故障注入未做、跨崩溃 at-least-once 窗口保留。
- **收口后追加修复（定向验证，未触发全量/CI）**：普通互动聊天续作 `resolve_chat_binding` 与确认/恢复/重试同规则——原提交保存的 topicId 优先；原提交存在但话题无法核对时明确 400 拒绝，不回退当前话题、不静默改绑；无 submission 的新消息仍用当前话题。新增 `backend/tests/test_fu3_chat_topic_binding.py`（基线 2 红 → 修复后 5 绿；相邻 waiting 13 绿）。按用户要求未跑全量、未触发 CI，改动以 `[skip ci]` 推送。
- **边界**：真实模型/厂商链路一律 fake/mock；POSIX 平台无本地证据（本机仅 Windows）；安装包形态与系统关机/注销未验证；真机桌面壳默认未在本轮重新验证（除非收口时另有证据）；历史里程碑中「未合并 main/未发布」等表述是各历史阶段事实，不因本轮开始而改写。

### P92 — 第二轮定向修复：审批读取失败、CI 派发提交、过期相位、补读竞态、通知话题（2026-10-26 后续轮）

- **Status：** completed（定向验证；按用户要求**不做全量测试、不触发 CI**；未合入 main、未打包、未发布、未改版本号）
- **基线**：上一轮修复分支 tip `dae32828`（P91 收口后）；本轮继续同一修复分支，B/C/D/E 子工作流各自独立分支（fix/fu3-*），Lead 统一集成。
- **P1 审批读取失败显式化**：`ApprovalService.pending()/history()` 读取失败不再返回空/部分集合；`/api/approvals/pending`、`/api/approvals/history` 返回 503 + `approvals_unreadable`，前端保留已有待办并显示可重试错误。
- **P1 手动完整 CI 测试提交修正**：`ci.yml` 全部测试 job 显式 checkout `inputs.expected_sha || github.sha`（dispatch 检被 preflight 校验的提交；push/PR 不变）；契约检查器新增逐 job 断言与 4 个 selftest 反例。
- **P2 到期事件相位修正**：`_execute_approved` 到期路径事件 phase 由 invalidated 改为 expired（history 同步 expired），前端显示「已过期」而非「执行前核对不通过」。
- **P1 补读快照竞态**：前端按 arrivedSeq 区分「请求开始前已存在」与「补读期间新到达」，旧快照不再删除新审批；失败/404 保留全部待办。
- **P1 通知轮话题**：`_execute_notify_turn` 优先 `ctx.initial_topic`（origin 原话题），缺失才回退 current_topic；绑定/上下文/trace/结果保存同一话题，不再改绑界面当前话题。
- **验证**：各修复均有红→绿定向证据（`_fixpm2/evidence/`）；集成后定向回归通过；`vue-tsc --noEmit` 0 error、`vite build` 成功、docs/license/workflow 契约自检通过、离线评估与基线逐字节相同。按用户最新指令**不做全量测试、不触发 CI**（前端全量在指令前已完成，仅作参考；后端全量已中止）。
- **边界**：`/api/runtime/state` 的 approvals 快照同故障返回 500（不再伪装空，但非 503+code）；`interrupted()/audit_trail()` 仍保留读失败返回空旧行为；本轮无真实桌面/安装/真实模型验证。

### P93 — FU4：权限模式移到全局栏、互动搜索内联、权限模式切换 422 修复（2026-10-26）

- **Status：** completed（定向测试 + 真实浏览器截图复核；按用户要求**不做全量测试、不触发 CI**；未合入 main、未打包、未发布、未改版本号）
- **权限模式移到全局栏**：删除页面内胶囊徽标（ChatColumn 话题栏 / InteractiveView 对话窗状态条），改为 WindowChrome 全局栏里的纯文字 `PermissionModeText`（对话页 / 互动页同一处、同一个 store；读取中/失败如实显示；点击进设置 →「工具与权限」，不切模式）。
- **互动搜索内联**：去掉「按钮 + body 弹窗面板」，在全局栏挂载点直接渲染内联 input：点击/聚焦即展开加宽（一次点击即可输入）；除 input 自身 placeholder 外无任何标题/label/说明/scope/关闭按钮文字；结果是锚定输入框下方的小浮层列表（非 dialog，无标题/说明，可点选定位）；Esc/点击外部收起，支持键盘与 480 窄窗。
- **修复切换权限模式 422**：`setSecurityMode` 的 PUT 请求补 `Content-Type: application/json`。此前无 Content-Type，FastAPI 的 `body: dict` 把 JSON 文本当字符串校验，返回 422「Input should be a valid dictionary」；实测同一 body 带 JSON 头 200、不带 422，已在真实后端验证。
- **验证**：定向测试（权限模式 47、交互组件 73 文件/689 例、security 请求契约 2、相关 design-guards/窗口栏等）；`vue-tsc --noEmit` 0 error；`vite build` 成功；Playwright(Edge) 实机 1440/480 两个路由：全局栏文字可见且纯文字（无边框/透明底/0 圆角）、旧徽标 0、搜索只保留输入框（容器无额外交字）、点击后 94→226px 且聚焦、无 dialog、窄窗 scrollWidth=480 无横向溢出、consoleErrors=0（截图与 JSON：`_fixpm2/evidence/FU4/ui/`）。
- **边界**：本轮不做全量/CI；未做真机桌面壳复验；搜索结果未在真实有匹配卡片的板面上做视觉复核（无卡片数据时按设计不渲染说明）；脚本 `scripts/i2v2-verify/playwright_flows.mjs` 已同步改为操作内联输入框。

- **补充修复（用户反馈：全局栏消失，只剩 QIO）**：用户打开的是旧深链/手输深链 `#/conversation`，而对话页真实 path 是 `/` —— 无匹配路由导致页面不渲染、WindowChrome 走「非聊天页」分支只剩 QIO 字标（图标 / PERSONAL AGENT / 权限模式 / 设置入口全部没有）。已加 `/conversation` → `/` 别名与未知路径 catch-all 回对话页；Playwright 实测 `#/conversation`、任意未知哈希都回到完整全局栏（图标 + PERSONAL AGENT + 权限模式文字 + 设置），`#/interactive`、`#/settings` 不受影响（截图 `05-conversation-redirected.png`）。

### P94 — FU5：空态居中、模式切换直连、收起任务栏圆角对齐、互动输入列居中（2026-10-26）

- **Status：** completed（定向测试 + 真实浏览器截图/几何复核；按用户要求**不做全量测试、不触发 CI**；未合入 main、未打包、未发布、未改版本号）
- **空状态居中**：`MessageStream` 的 `.stream` 明确 `display:block; position:relative`，空状态 `.empty` 改为绝对铺满消息可视区并水平/垂直居中（`pointer-events:none`），不再被虚拟列表 spacer 的 `width:100%` 推到右侧；对话页与互动页复用同一组件一起修正；三种空态文案（读取中/读取失败/真空）不变。
- **模式切换直连**：`ConversationModeSwitch` 去掉弹出菜单，点击按钮直接 `router.push` 到另一模式；按钮显示当前模式、`aria-label/title` 说明「切换到…」；保留只导航语义（不碰草稿/审批/附件/任务），无菜单/Esc/外部点击旧逻辑。
- **收起任务栏圆角对齐**：`ConversationView` 的 `.collapsed-rail` 去掉内边距与内层小按钮，`.rail-btn` 铺满整栏、`border-radius: inherit`、无内层边框；悬停用整栏底色；未读徽标移到栏内侧右上角（`overflow:hidden` 下不再被裁）。
- **互动输入列居中**：`.ic-composer-row` 改三列 grid（`minmax(0,1fr) minmax(0,860px) minmax(0,1fr)`），输入列固定 860 并真正居中；模式切换在第一列 `justify-self:end`、紧贴输入列左侧（不占输入宽度）；`.ic-composer` 去 860 上限铺满面板；≤700 窄窗切到输入上方左对齐；消息列 `.ic-stream` 仍 860。
- **第 5 条（透明高斯模糊对话栏）本轮不实现**：可行性已确认（见交付文档 §10.5 的方案与风险），按用户要求先只给结论与做法。
- **验证**：新增 FU5 布局守卫测试 8 例（红 6 failed → 绿 8 passed）；模式切换测试改为直连语义；相关定向 9 文件 77 例 + pm-acceptance/design-guards/harness 33 例全部通过；`vue-tsc --noEmit` 0；`vite build` 成功；Playwright(Edge) 实机：空态相对消息区水平偏移 0px、点按钮直接 `#/interactive` 且无 menu、互动输入列相对面板中心偏移 0px、switch 在输入左侧 gap 12px、480 收起栏按钮与栏同圆角(14px)且无横向溢出、consoleErrors=0（截图/JSON：`_fixpm2/evidence/FU5/ui/`）。
- **边界**：jsdom 无布局引擎，布局用源码守卫 + 组件结构断言，像素效果以实机截图为准；第 5 条未实现；本轮无全量/CI。


### P95 — 记忆星球页改版：布局 / 三标签管理区 / 星球过渡 / 管理流程 / 全局搜索（2026-10-27）

- **Status：** completed（三条子分支独立 worktree 并行完成、Lead 集成；定向 + 全量前端 + 后端定向 + 文档一致性 + 完整 CI 见交付文档；未合并到基线分支、未打包、未发布）
- **页面结构**：`.planet-view` 内新增 `.planet-content`（`inset: var(--titlebar-height) 0 0 0`，位于全局栏下方）；星球层铺满内容区且尺寸不随管理区变化，管理展开只对画布做 `translateX(calc(var(--mp) * -50%))` → 球心从内容区中心连续移到左边缘、只露右半球；默认态不裁切（美术与基线一致）；顶部全局栏原样保留。
- **星球过渡**：新增 `composables/usePlanetManageTransition.ts`（唯一进度来源 `--mp` / `data-mp` / `.managing`，曲线取 CSS 令牌，中途反向按瞬时速度用三次 Hermite 衔接，减少动画一帧到位）；`usePlanetScene` 新增独立姿态组 `poseGroup` + `setManageYaw`（管理旋转不碰 planetGroup 的四元数/自转/聚焦 slerp/OrbitControls），并新增静帧停绘（无变化 12 帧后停 rAF，交互即恢复）。球体/融合环/相机参数未改。
- **管理区**：记忆 / 知识 / 实体三标签（同一块内容区域，首次激活挂载后 v-show 保活，切换保留浏览状态且不换景）+ 待处理（真实数量）/ 整理记录辅助入口（返回恢复原标签页）。记忆页：话题列表 → 概览 → 完整讨论记录，「对话当前话题」与「正在浏览」分开写，只有「继续讨论 / 从这里继续」走既有锚点切换。知识/实体：列表 → 详情 → 返回（恢复搜索/筛选/滚动）+ 草稿化编辑（保存才一次提交、取消丢弃、失败保留草稿、409 冲突保留输入并展示服务端当前值、未保存离开先确认）。待处理合并三类真实来源（知识建议 / 记忆整理问题 / 实体更新候选），「修改后确认」走既有原子接口；整理记录列表 → 单次详情（受影响条目默认折叠，撤回/恢复复用中央弹窗）。
- **全局搜索**：页面工具行入口，一次并行覆盖话题/知识/实体并按三类分组，独立于当前标签页；加载/空/失败三态自绘；请求序号丢弃旧响应；点结果切标签并打开详情，返回保留关键词与结果，关闭恢复此前管理上下文。
- **接口**：`POST /api/entities/{id}/revise` 增补 `relations{add,remove}`（与字段同一事务）与 `expected_revision` CAS（不符 409 且不写任何字段）；知识修订沿用既有 `expected_version` + 409。无 schema 变更、无依赖变更。前端新增 `stores/planetPending.ts`（真实待处理数）。
- **视觉证据**：DEV 钩子半径与基线逐位相同（452.66504294495525）；像素扫描直径差 -1px（基线自跑两次差 37px，作噪声地板）；轮廓环/球内/融合环三组结构色众数相同、环宽笔画峰相同；同姿态逐像素差 3.35%（未对齐 19.4%）。管理态实测球心 x=0、右缘 0.987R、管理区 left=800 不遮挡。停帧 208→208（2.5s）、减少动画 `intermediateFrames=0`。
- **规范核查**：页面内圆角一律 `var(--qio-ui-radius)`（源码 0 处其它取值）；页面 DOM 内原生外观控件 0、无 `alert/confirm/prompt`；颜色全走 `var(--*)`；1440×970 / 1024×768 / 900×700 三档窗口逐层无横向滚动。
- **追加（用户要求，2026-10-27）**：对话页 / 互动页共用的输入胶囊**恢复胶囊形**（`.input-pill` 用 `--r-pill`，`design-guards` 断言同步改为胶囊形）——它是「全应用表面统一 14px」的唯一例外；记忆星球页内的 14px 统一不受影响。`r`n- **追加（用户要求，2026-10-27）**：对话页「消息框」改版 —— 消息流与底部输入块（入口按钮行 / 模式切换 / 输入胶囊 / 记忆星球入口）收进**同一个圆角框**（`.chat-box`，边框/圆角/底色只在框上）；入口行移到输入行之外，出现/换行只在框内**向上生长**，输入胶囊不再被推动；`.composer-line` 的 `align-items: center` 让模式切换 / 输入胶囊 / 星球入口共用一条中心线；星球入口直径改为**与输入胶囊等高**（`--composer-pill-h: 54px`，入口画布 `--ball-size: 61px` ≈ 54/0.887，所以开合动画的入口尺度 scale ≈ 1.000、不失真）；消息流与输入区共用 `--chat-pad-inline` 一套左右留白，并去掉不再需要的 132px 星球通道与 `.bottom-cluster` 占位逻辑。实测（1440×970 / 1024×768 / 760×820）：有/无入口行时输入胶囊中心 y 不变、模式/胶囊/星球三者中心完全一致、球直径 = 胶囊高度 = 54px、无横向滚动。
- **边界**：星球美术「逐像素完全一致」不可证（基线每次打开朝向不同），已用同姿态差异 + 噪声地板 + 三条旋转无关结构证据替代；窄窗口 900×700 走上下排布时星球区随高度等比变小（排布结果，非 CSS 缩放），左右排布窗口（≥880 宽且放得下）直径保持不变；实体 `pending_relations` 无读写端点，未计入待处理数量。细节：`docs/memory-planet-redesign-20261027.md`。

### P96 — 设置页改版与模型/用途接入（2026-10-28）

- **Status：** partial（五类设置页、搜索、混合保存、Key→模型→用途数据模型与迁移、模型发现与按模型验证、用途解析接入真实调用链、搜索通道状态与真实检查均已实现并有自动化测试；完整 CI 在集成分支 `feat/mp-20261027-settings-integration` 的 `2fdf8a4dbb6bd793411fb4eb101491a185e45018` 上通过（run 40635523464，全部 job 成功）；未接通能力与边界见本节末尾，故不标 completed）
- **信息架构**：设置页从基线的 8 个 tab 并为**五类** —— 常规 / 模型与服务 / 记忆与整理 / 工具与权限 / 关于与更新。复杂参数回到所属分类的「高级选项」（诊断开关→常规；迭代上限与输出预算→记忆与整理；SearXNG 地址、搜索条数、正文预算→模型与服务），不再集中成一个「高级」分类。基线每个控件都在新页面有位置，逐条对照见交付文档的迁移清单。
- **响应式**：正常窗口是左侧分类导航 + 右侧内容；窄窗口把导航移到顶部变标签页。**只换导航容器的位置与形式**：内容用同一份 DOM（面板 `v-show` 常驻、不重建表单），分组 / 顺序 / 操作方式在两种宽度下完全一致；宽窄切换保留当前分类、滚动位置、展开状态与未保存草稿。
- **保存模型**：即时保存（主题 / 动画 / 滚动指示条 / 输出速度 / 窗口行为 / 后台整理 / 工具记录 / 搜索免密钥开关）失败回滚控件到真实值；按组保存（搜索配置、Key 表单、默认模型与用途分配、记忆分段、后台整理间隔、工具记录保留）各自「保存 / 取消」+ 未保存标记，失败保留草稿；分区反馈互不覆盖，每次写带递增序号，迟到的旧响应不落界面。**离开设置页直接丢弃未保存草稿**：不自动保存、不弹询问、返回后不恢复，也不额外发写请求。
- **搜索**：设置页内独立「搜索设置」入口（顶部全局栏行为不变），静态索引覆盖设置名称、说明、用途名称与高级参数；结果给名称 + 所属分类 + 简短说明；点击后进分类、必要时展开高级选项或后台用途分组、定位并短暂高亮，同时保留其它草稿；返回搜索结果保留关键词与结果位置；无结果与失败用自绘状态。索引里不含任何密钥。
- **数据模型**：`credentials` 仍是「一条连接（一把 Key）」；新增 `credential_models`（该连接下已知的模型）与 `model_usages`（用途 → 连接 + 模型，`mode` 区分 explicit / legacy / inherit）；默认模型落 `settings['model.default']`。追加迁移 73（建表 + 索引 + 回填）与 74（补偿入口，只含可重放 DDL），`SCHEMA_VERSION`/`COMPENSATION_VERSION` 随之推进；历史迁移一行未改。回填只处理 `default_model` 非空的旧凭据（不猜模型）；历史 tags 以 `mode='legacy'` 保留映射而**不**当成显式指定 —— 基线里 tags 只参与「选哪把 Key」的政策，写成显式会让老用户那把它已停用/欠费的 Key 把主对话打成硬失败。用量账本、预算、审计、系统凭据库引用与历史默认项全部保留，不按模型复制额度、不重置用量。
- **模型接入**：正常流程收敛为「选服务商或接受本地识别提示 → 填 API Key → 自动获取模型 → 分配用途」，正常流程不要求填地址 / 协议 / 模型名；自定义地址、连接协议、手动模型名、显示名称、累计用量上限与历史自定义用途标签都在默认收起的高级入口。本地 Key 前缀只作**提示**、不当作验证成功；有歧义时要求用户补选，必要时选地区或接入类型；只对用户提交的那一个地址发认证请求，不跨平台逐一试、不交给模型判断来源；改地址或协议必须重确认，且确认前不拿这把 Key 去新地址取模型或验证。发现支持分页、刷新、缓存，并把断网 / 超时 / 限流 / 无权限 / 无效 Key / 无列表分开归类；公共模型目录与服务商目录分别标注，不把公共目录说成「这把 Key 全部可用的模型」。
- **用途分配**：13 个用途（主对话 / 通用对话 / 写代码 / 子任务 / 看图视觉 / 视频 / 语音 / 研究 / 向量嵌入 / 记忆整理 / 知识提炼 / 实体提炼 / 历史自定义标签）。先默认模型，各用途按需单独指定；未指定跟随默认；换默认不覆盖显式指定；显示每个用途实际使用的模型与连接并提供「恢复跟随默认」；候选按服务商与连接分组，停用 / 撤销 / 过期 / 预算耗尽 / 验证未通过 / 能力不满足的候选照样列出但写原因且不可选。主对话与通用对话的调用路径已核实等价，日常入口合并展示、两个用途键都保留在数据里。视频 / 语音 / 嵌入明确「沿用现有处理方式」，不继承默认对话模型；嵌入显式换外部模型时说明维度与索引影响并复用中央确认。
- **调用接入**：新增用途解析（`backend/src/agent/services/usage_target.py`）：显式指定 → 默认模型 → 基线 policy 回退；显式指定不可用时如实失败并带原因码（不静默换 Key），默认模型不可用时按基线政策回退但标记降级并给出原因与实际使用的目标。主对话 / 通用对话、子任务、看图、记忆整理、知识提炼、实体提炼六条路径都已接到解析结果（派生任务开始时冻结三把 adapter，已开始的调用保留原配置归属）。**研究没有独立调用路径**，只有历史标签与 policy 回退，已如实标注「未接通」，不提供看起来生效的选择器。
- **联网搜索**：模型凭据与搜索凭据分开管理，博查 Key 不进模型列表或用途选择器。免密钥通道、博查、自建 SearXNG、必应/百度等备用通道与回退顺序全部保留；**免密钥通道不是总开关**，关闭它只移除免密钥那几条，不清除也不关闭其它配置，不改回退顺序。博查 Key 留空保存不等于清除，清除是显式操作。新增 `GET /api/settings/search/status`（各通道 configured / check_state / checked_at / 原因码，不含密钥）与 `POST /api/settings/search/check`（真实一次极小请求，区分断网 / 超时 / 限流 / 凭据无效），「已配置」与「检查通过」分开显示。另修正一处基线缺陷：搜索的默认返回条数与正文字符预算此前只存不读，现在工具每次调用真的读设置。
- **验证**：设置页有「当前生效参数」只读区，显示已保存且真实生效的值（含默认模型与各用途实际目标），不把草稿当生效值。
- **未接通 / 边界**：研究无独立调用路径；写代码与主对话同一路径、无独立调用；视频 / 语音按设计沿用现有处理方式（显式指定才生效）；搜索连接检查结论是进程内状态，重启后回到「未检查」；所有验证均为 fake/mock，**未做真实联网与真实 Key 联调**。
- **不改的东西**：首次引导的步骤与完成条件、三种权限模式与授权审批规则、后台整理算法与触发规则、软件更新与重启决定方式、许可证与第三方声明的离线内容来源。

### P97 — 设置页视觉修复：去灰底 / 去药丸描边 / 删多余解释 + 对话页话题卡间距（2026-10-28）

- **Status：** completed（实现、定向验证与完整 CI 通过：run `40664735538` → success，10/10 job 绿；首次运行时 Windows 后端 job 因附件上传 I/O 压测的既有不稳定项红过一次，已单独重跑通过。范围仅视觉与文案，未改接口、保存、草稿、权限、搜索定位、首次引导与模型调用）
- **基线**：`feat/mp-20261027-settings-integration` @ `af77d2e74d94eb05b5227d69376f57810086d84f`；修复分支 `fix/settings-visual-20261028`（独立 worktree）。
- **表面**：设置内容区改为**一层**承载面（轻玻璃 + 1px 中性边界 + `--shadow-1` + 14px 圆角），不再为每组/每行/每个状态套卡片。新增 `frontend/src/styles/settings-surface.css` 承载全部共享原语的设置页作用域覆盖（`.qio-settings` 前缀；不用 `!important`；不改 `--bg-inset` 等令牌、不改 base.css 默认样式，其它页面不受影响）。
- **去灰底**：分类导航返回键、分段轨道、下拉、数字步进、输入、次级按钮、筛选、高级选项、未保存条、搜索通道、许可折叠组、搜索面板状态与命中项、更新卡与权限卡片的灰底/盒面全部去掉；改成「标题 + 留白 + 对齐 + 中性细线」。实测（五分类 × 净白/暗紫晶）：`--bg-inset` 填充 **0 处**。
- **去描边**：分段选中态不再有灰轨道、闭合外框与 `inset 0 -2px` 底边内阴影；导航选中态去掉玫红左竖条与顶部底线；权限选项去掉灰卡片与玫红选中边框；提示信息去掉左竖条。选中统一为**品牌色文字 + 4px 圆点**。实测强调色边框/内阴影/单侧竖条 **0 处**。
- **开关**：保留可见拨动结构（1px 线性轨道 + 14px 滑块，开=滑块右移 + 品牌色 + 柔光），命中区仍是 38×22；不用纯文字勾选，不加外框。
- **浮层**：下拉菜单、候选面板、确认层为轻玻璃 + 一层浅中性边界 + 层级阴影；菜单宽度随触发框（触发框设最小宽度保证选项单行，不横向溢出）。
- **焦点与状态**：`focus-visible` 保留（实测 2px `--focus-ring`，Tab 可达）；错误/警告/危险语义色不变。
- **删除多余解释**：按「解释为什么/怎么实现/与控件重复/同句重复」的规则删除设置页说明文字；**保留**安全与危险提示、错误与失败原因、限额/范围/单位/默认值语义、状态词、许可与第三方/BYOK 合规说明。模型用途列表里同一句重复 8 次的状态说明改为列表顶部说一次（行内不再重复）。搜索索引里的 description 保留（它是搜索结果元数据，不是页面正文）。
- **对话页**：话题详情卡的纵向节奏统一为一套固定值（标题→描述 4 / 描述→元信息 10 / 元信息→分隔线 12 / 分隔线上下对称 12 / 分隔线→小标签 12 / 小标签→段落 4），段落行高与正文一致（`--lh-base`）；480 与 320 宽无横向溢出。
- **验证**：`npx vue-tsc --noEmit`、设置页/模型面板/对话页定向 vitest、整页计算样式审计（五分类 × 双主题：灰底 0、强调色描边 0、单侧竖条 0）、1440/480 截图与键盘焦点截图见交付说明。
