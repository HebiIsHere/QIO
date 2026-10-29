# 发布前第三轮：设置可靠性返修（2026-10-29）

- 基线：已验收提交 `69c44558487aff3957140dab6a4b7f6bf36a2a92`
  （分支 `fix/release-step2-ui-consistency-20261029` 尖端）。开工核对：该提交即上一轮验收通过的完整交付，前两轮成果全部保留。
- 工作分支：`fix/release-step3-settings-reliability-20261029`
- 范围：只处理本轮指定的六项（F08 独立设置保存 / F14 初始读取不覆盖 / F09 模型获取归属 /
  F12 模型获取反馈 / F13 Key 保存重试 / F11 搜索结果完整性）及其直接影响。
- 不做的事：不合并 main、不打标签、不打包、不发布、不开始下一轮；不改 CI 工作流；
  不改历史迁移；不做无关重构与视觉改版；不引入真实 API Key 或联网测试（全部 fake provider）。
- 验证口径：只跑定向前端 vitest、前端类型检查、定向后端 pytest 与文档一致性检查；
  未跑全量测试、未跑构建，未触发 CI（本轮所有提交使用仓库支持的 `[skip ci]`）。

## F08 同一区域不同设置的保存互不取消

- 原触发条件：设置页每个分区只有一个写序号。同一分区里 A（例如工作区根目录）保存还在途时，
  B（例如输出保留期）一开始就领走了新的序号，A 的响应被判定为「过期」——A 失败时既不回滚
  控件、也不留下错误提示；A 的乐观值会一直停在界面上，看起来像保存成功。
- 修复：
  - `frontend/src/composables/useQioSettingsSave.ts`：`saveNow` 支持每个设置一个身份
    （`key`）。不同设置的保存在途互不作废，只有同一个设置的后发请求才作废先发请求；
    失败仍按本次调用回滚，错误反馈优先于另一个设置的成功提示保留，直到该设置再次操作。
  - `frontend/src/views/settings/sections/MemorySection.vue`、`ToolsSection.vue`、
    `SearchConfigGroup.vue`、`RegularSection.vue`：每个即时保存的开关 / 数值 / 下拉都带
    自己的 key；单字段保存的成功响应只回写它自己拥有的字段与生效摘要，
    不拿响应里附带的其它字段覆盖另一个设置在途或已成功的选择。
- 验证：`frontend/src/views/settings/__tests__/ReleaseStep3Settings.test.ts`
  （A 失败 B 成功时 A 控件回滚 + 错误可见 + B 值保持；同一设置旧响应迟到不覆盖新选择），
  以及既有 `settings-save-model.test.ts` 的迟到响应 / 失败回滚用例同步通过。

## F14 迟到的初始读取不覆盖较新的编辑 / 保存值 / 生效摘要

- 原触发条件：记忆分段 / 后台整理 / 对话深度 / 联网搜索等分区在挂载时并发读取初始值；
  用户先编辑或保存后，迟到的初始读取仍会 `reset` 草稿并覆盖「当前生效参数」。
  另一个后果：组保存进行中如果被初始读取 `reset` 作废，`saving` 永远不会清零，
  界面永久停在「保存中」，后续保存也会被入口判断拒绝。
- 修复：
  - `useGroupDraft`（`frontend/src/composables/useQioSettingsSave.ts`）：作废在途保存时，
    该次保存结算后仍要自己清除 `saving`；草稿修订号（`revision`）对外可见，供分区判断
    「等待期间用户是否改过」。
  - `MemorySection.vue` / `SearchConfigGroup.vue` / `ToolsSection.vue`：加载响应落地前
    校验「期间是否发生编辑 / 保存 / 在途保存」；被更新的用户动作取代时跳过 reset 与
    生效摘要写入；只有确实仍有效的初始读取才写。免密钥开关、博查 Key、工作区、
    工具记录各自独立判断，互不牵连。
- 验证：同一测试文件中的「迟到的 getMemorySettings 不改回已保存值、也不覆盖生效摘要」、
  「迟到的 getSearchSettings 不改回已保存搜索参数」、「组保存在途被作废后 saving 结束且能再次保存」；
  既有记忆分段 / 搜索配置组保存与取消用例全部通过。

## F09 模型获取归属

- 原触发条件：表单获取模型与已保存连接的模型发现都是带 `await` 的网络调用。
  用户在等待期间换服务商 / 地址 / 协议 / Key 后，旧响应仍会写回：表单的模型列表和自动选中的
  第一条模型、已保存连接的目录刷新与获取状态都会被旧结果改写。后端 `discover` 在等待网络
  之后直接写库与发现状态，不校验期间连接是否已被重配；发现缓存也不记录自己属于哪份配置。
- 修复（前端）：
  - `frontend/src/components/models/KeyForm.vue`：每次获取带请求身份与「本次配置签名」
    （服务商 + 地址 + 协议 + Key）。换服务商 / 改 Key / 在高级选项里改地址或协议都会作废
    在途请求；响应回来签名不一致一律丢弃，不写模型列表、不自动选择、不显示旧条数。
  - `frontend/src/components/models/KeyAccessSection.vue`：已保存连接的自动 / 手动获取按
    「连接 + 当时配置（版本 / 地址 / 协议）」领身份；更新的保存接管后旧请求静默退场；
    配置已变时如实提示「连接配置已改变，本次获取结果已忽略；请重新获取」，不写目录状态。
- 修复（后端）：
  - `backend/src/agent/services/model_catalog.py`：发现开始时计算配置归属指纹
    （Key 版本 + 地址 + 协议，刻意不含默认模型），网络返回后重算；不一致则不写模型、
    不写发现状态，返回 `stale=true` + `reason_code="config_changed"` 与人话说明。
  - 发现缓存（`backend/src/agent/credentials/models.py` 的发现状态）记录该指纹；
    换配置后上一份配置的缓存不再被当成本次结果（会重新真实获取）。
    手动模型、用途引用与历史模型行都不删除（旧模型行只作为历史保留）。
- 验证：`frontend/src/components/models/__tests__/ReleaseStep3ModelFetch.test.ts`
  （表单换 Key / 换地址后旧响应不落界面；保存后重配期间的旧 discover 结果被忽略并提示）、
  `backend/tests/test_release_step3_c.py`（fetch 挂起期间重配 → 旧结果未写库、发现状态未覆盖、
  默认模型 / 手动模型 / 用途引用保持；换配置后缓存不被误用）。

## F12 连接保存成功后的自动获取反馈

- 原触发条件：保存成功后界面先显示「连接已保存。正在同步模型目录…」，随后只处理了
  网络异常；业务失败（`error` 非空）与空列表时提示永远停在「正在同步」，用户不知道
  到底拿到没有；成功提示也没有说清「列表 ≠ 验证通过」。
- 修复（`frontend/src/components/models/KeyAccessSection.vue`）：
  - 成功：显示已获取数量与来源，并明确「只代表可选的模型列表，尚未验证可用」；
    公共目录仍带上服务商说明。
  - 空列表：说明这个地址没有返回可用模型列表，可重新获取或手动填写模型名。
  - 业务失败：显示可读原因，并提示可点「重新获取」重试。
  - 网络异常：单独说明是网络异常、连接已保存、可稍后重试。
  - 四条路径都结束「正在同步」；失败后的重试只再次调用发现接口（不重复创建连接）。
  - 后端确认发现接口对网络异常 / 空列表 / 成功都返回结构化结果（`error` / `reason_code`
    / `stale`），不会抛未分类 500。
- 验证：同一前端测试文件中的四种反馈用例与「失败后重试只重新获取、不创建连接」用例；
  `backend/tests/test_release_step3_c.py` 的结构化错误用例。

## F13 Key 保存重试的请求身份

- 原触发条件：设置页 Key 表单每次保存都新生成一个 `client_request_id`。后端按这个身份
  派生确定性的 `key_id`，所以「服务端已经保存、客户端丢了应答」后用户再点一次保存会得到
  第二条连接；同一身份携带不同内容时也会被静默当成同一次提交（新内容被丢掉）。
- 修复：
  - `frontend/src/components/models/KeyForm.vue`：按「本次提交内容签名」缓存请求身份；
    内容未变的重试复用同一 `client_request_id`，内容变化才换新身份；提交入口与保存函数
    都有 `busy` 保护，提交中的重复点击 / 回车不会再发请求。
  - `frontend/src/services/credentials.ts`（首次引导表单）：同样只在内容变化时轮换请求身份。
  - `backend/src/agent/api/server.py`：幂等命中时比较内容（Key 只做内存比较，不落原文）——
    同 `client_request_id` + 同内容 → 返回原记录（同一 `key_id`、`idempotent=true`，
    仍然只有一条连接）；同身份但内容不同 → 409 `client_request_id_conflict`，
    不改写原记录、不新增连接。
- 验证：`frontend/src/components/models/__tests__/ReleaseStep3ModelFetch.test.ts`
  （双击只发一次；首次网络失败后重试复用同一身份并 emit 相同 `key_id`；改内容后身份变化）、
  `backend/tests/test_release_step3_c.py`（同身份同内容两次 / 三次都幂等且只有一条；
  同身份不同内容 409 且原记录字段与版本不变）。

## F11 搜索结果完整性

- 原触发条件：全局搜索在纯函数里把每类结果 `slice(0, 8)`，并把截断后的条数之和当作
  「共 N 项」；第九条及以后的结果根本不存在，不能打开定位。
- 修复：
  - `frontend/src/planet/manageViews.ts`：`buildSearchGroups` 返回每类**完整命中列表**
    与真实总数（`total` + 每类 `counts`）；截断只发生在展示层。
  - `frontend/src/components/planet/GlobalSearchPanel.vue`：每类首批展示 8 条，
    标题显示真实总数（如「话题（12）」），底部「查看更多（还剩 N 条）」分批展开，
    展开到最后一条。换关键词重置展开状态并沿用请求序号丢弃旧响应；失败保留上次结果、
    给出原因并可重试；切走再切回保留关键词、结果与展开状态（v-show 保活）。
    结果点击仍只上报真实 kind + id。
  - 打开定位：管理区既有 `openHit → focusId` 链路保持；第 9 条与最后一条的 id 能被子面板
    真实定位（知识面板按 `focusId` 打开第 9 条的真实内容已验证）。
- 验证：`frontend/src/components/planet/__tests__/release-step3-search.test.ts`
  （真实总数与每类计数、首批 8 条、展开后第 9 条与最后一条出现并可点、每类独立展开、
  换词重置与旧响应不覆盖、失败重试保留结果、真实知识面板按 focusId 定位第 9 条），
  并同步更新 `mp-a-manage-views.test.ts` 的旧「每组截断」断言。

## 本轮定向验证

- 前端：`npx vitest run src/components/models src/views/settings src/components/planet`
  与受影响的 `src/components/credentials`、`src/components/onboarding` 定向套件全部通过；
  `npx vue-tsc --noEmit` 通过。
- 后端：`uv run --frozen pytest tests/test_release_step3_c.py` 与相邻的
  `tests/test_model_catalog.py`、`tests/test_credential_verify.py`、
  `tests/test_credential_endpoint_api.py`、`tests/test_release_step1_c.py` 通过。
- 文档：`python scripts/check_docs.py` 通过。
- 未跑全量测试、未跑构建、未运行 CI（所有提交带 `[skip ci]`，不触发也不重跑工作流）。

## 已知限制（如实记录，不在本轮扩展）

1. 发现归属指纹覆盖 Key 版本 / 地址 / 协议，不含默认模型（改默认模型不影响「这份列表属于谁」）；
   若有人绕过应用入口在系统凭据库里替换了同一版本的密钥，后端无法识别（正常入口都会推进版本）。
2. 模型发现只 upsert 不删除：服务商不再返回的旧模型行作为历史保留，因此获取成功后的目录
   可能仍含历史行；删除仍需用户显式操作（本轮不引入清理迁移）。
3. 前端「配置已变」判断以父级传入的已保存连接元数据为准；保存应答尚未回流到父级的极短窗口内，
   由请求身份（token）兜底，保证旧请求不会写回。
4. 搜索「查看更多」是本地分批展示（话题 / 知识 / 实体三个接口本来就是全量列表），
   不是服务端分页；数据量极大时首次并行读取的体积不变。
5. 第 9 条 / 最后一条的「定位」验证覆盖了结果上报真实 id 与知识面板按 focusId 打开第 9 条；
   跨面板的滚动位置仍沿用各面板既有行为。
6. 全量测试、构建与 CI 本轮未运行；F13 的请求身份只在设置页 Key 表单与首次引导表单实现，
   其它历史入口未改动。
7. 本轮不合并、不打包、不发布，等待验收。

## 状态

- 六项修复与定向验证已完成；分支已推送远端，远端完整 SHA 以交付回执为准。
- 未合并 main、未打标签、未发布、未开始下一轮。

## 返修（验收遗漏，2026-10-29 第二次交付）

基线仍为 `b4c8afe868079b967090270589956cc31e9f3518`（上一轮交付 SHA），分支不变；本轮只处理验收列出的六类遗漏，不删除/缩小上一轮已通过的语义。

### 1. 失败回滚：回到真实已保存值

- 原缺陷：数值控件先经 `update:modelValue` 把输入写进状态，`@change` 里再用「当前值」当回滚基线，于是保留期 90 → 30 保存失败后仍显示 30。
- 修复：工具记录（输出保留 / 记录保留 / 输出全文开关）、后台整理（开关 / 间隔）、对话深度（迭代上限 / 输出预算）分别维护「最近一次已保存（或刚加载）的真值」；失败回滚用它，不再用用户刚输入/乐观值。
- 验证：`frontend/src/views/settings/__tests__/ReleaseStep3Settings.test.ts` 中「输出保留期 90 改 30 失败后回 90」「后台整理间隔 24 改 30 失败后回 24」。

### 2. 错误保留：按设置保留未解决失败

- 原缺陷：反馈区只有一个「当前提示」，两个不同设置先后失败、只重试修好第二个时，第一个的错误被第二个的成功覆盖而消失。
- 修复：`useQioSettingsSave` 按设置 key 保留未解决失败；成功只清除它自己那个设置的错误，若还有其它未解决错误，原反馈区继续显示最近一条未解决错误；同一设置再次成功/失败仍更新自己。
- 验证：同一测试文件的「工作区失败 + 输出保留期失败 → 只重试保留期成功，工作区错误仍可见」。

### 3. 正在输入的内容保护

- 原缺陷：工作区、迭代上限等入口只在失焦 / 保存时才标记「用户改过」，用户正在输入（未失焦）时迟到的初始读取仍会覆盖未提交输入。
- 修复：输入事件（`update:modelValue`）即建立保护——工作区、后台整理间隔、对话深度两个数值、工具记录两个保留期分别在输入时递增保护计数；初始读取落地前按该计数判断，被用户输入取代时整体跳过。
- 验证：同一测试文件的「工作区只输入未失焦 → 迟到读取不覆盖」「迭代上限只输入未失焦 → 迟到读取不覆盖」。

### 4. 取消恢复正确值

- 原缺陷：初始读取因用户已编辑被跳过时，草稿基线仍是加载前的占位默认值（记忆 4096 / 搜索 5），取消回到占位值而不是服务端实际保存值。
- 修复：`useGroupDraft` 新增 `adoptBaseline`（只把服务端真值记为取消基线，不动用户正在编辑的草稿）与 `savedRevision`（保存成功次数）。记忆分段 / 搜索配置组在「读取迟到 + 用户已编辑」时 adopt 服务端真值；若在途保存或已有更新的保存成功，则整体跳过，保护较新保存结果。
- 验证：同一测试文件的「记忆分段取消回 12000（不是 4096）」「搜索条数取消回 9（不是 5）」。

### 5. 模型获取归属和顺序

- 原缺陷：成功 / 失败 / 空结果三条路径只有成功路径在写库前校验配置；旧配置的超时（失败路径）仍会写发现状态，覆盖新配置那次成功；同配置下较早的请求晚回来也能覆盖较新的结果（前后端都缺顺序判断）。
- 修复（后端 `backend/src/agent/services/model_catalog.py`）：每次发现按连接领进程内请求代号；失败 / 空结果 / 成功在**写库与写发现状态之前**统一检查 ① 是不是最新请求 ② 结果是否仍属于当前配置。任一不符则不写库、不写状态，返回 `stale=true` + `reason_code=superseded/config_changed`。
- 修复（前端 `frontend/src/components/models/KeyAccessSection.vue`）：`superseded` 的旧结果静默退场（不改当前反馈）；配置已变才提示，并保留更新的成功状态。
- 验证：`backend/tests/test_release_step3_c.py` 的「旧配置超时不覆盖新配置成功」「同配置较早请求不覆盖较新结果」；`frontend/src/components/models/__tests__/ReleaseStep3ModelFetch.test.ts` 的「旧配置超时不能改写成功状态」「同配置较早结果不能覆盖较新结果」。

### 6. 当前目录与历史分开

- 原缺陷：发现结果和目录都返回该连接的全部模型行；新地址只返回一个模型时，旧地址的模型也被算进本次结果与数量，缓存与重载后同样偏大。
- 修复：新增「当前配置下的发现集合」（settings 键 `model.current_set.<credential_id>`，记录 config signature + model_ids；不新增迁移、不删除任何行）。发现成功时把这次返回（以及同配置下已返回过）的 id 记入集合；发现响应、目录快照、缓存与重载都按同一集合划分「当前目录 / 历史」：当前 = 手动/历史兼容行 + 当前配置真实返回的模型；旧配置发现过的行进入 `historical_models`（历史 / 手动 / 用途引用全部保留）。
- 前端类型同步 `historical_models`/`historicalTotal`（展示层本轮不加历史浏览器，数据已可供后续使用）。
- 验证：同一后端测试文件的「当前目录只算当前配置、历史单独列出；缓存与重载一致；历史行 / 手动模型 / 用途引用保留」。

### 本轮定向验证

- 前端：`npx vitest run src/views/settings src/components/models` 全部通过；`npx vue-tsc --noEmit` 通过。
- 后端：`uv run --frozen pytest tests/test_release_step3_c.py tests/test_model_catalog.py tests/test_model_routes.py tests/test_credential_verify.py tests/test_credential_endpoint_api.py` 通过。
- 文档：`python scripts/check_docs.py` 通过；提交使用 `[skip ci]`，未运行全量测试 / 构建 / CI。

### 返修已知限制

1. 「当前 / 历史」划分记录在 settings（随该连接存在），不删除任何模型行；旧连接在第一次按新逻辑成功获取前仍按「全部当前」兼容显示。
2. 发现请求代号的顺序判断是**进程内**的（单后端进程）；多实例同时写同一 SQLite 的场景不在本轮范围。
3. 历史模型目前只在 API 返回 `historical_models`，界面暂无独立历史浏览入口（不删除、不隐藏用途引用与手动模型）。
