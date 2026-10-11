# 独立审查与验收：真实模型理解与真实执行（子智能体 E）

> 候选 SHA：`e452e0d356b4a0fd80f68c7019f362cdae90ba18`（分支 `feat/interactive-real-model`）
> 复核 worktree：`D:\qio-dev\qio-irm-e`（detached HEAD 到该 SHA）
> 契约基准：`docs/interactive-mode-contract.md` §14（本轮冻结）、§1.4 / §1.5 / §1.6 / §8.3
> 每条结论都由我（E）自己读源码、自己起服务、自己打真实 HTTP 得到；**没有采信** A/B/C/D 的自述报告。
> **模拟验证不等于真实模型验收**：本轮模型一律由 fake provider 顶替，请按 §7 的边界理解这些结论。

## 0. 结论摘要

三轮连续的全量定向验收（同一 SHA、同一命令）结果完全一致：

    PASS=54  FAIL=0  NOT_VERIFIABLE=12  共 66 项

- **未发现阻断问题，未发现重要问题。**
- 真实闭环**确实接通**：提交 → 受理（`delivered=true / status=accepted / understandingId`）→ 材料读取 →
  真实模型调用（fake provider 顶替）→ 结构化校验 → 意图 → 审批 → **真实执行** → 结果卡写回 + 来源引用；
  执行阶段在方案已有正文时**不再调用模型**（与 Lead 说明一致）。
- 上一轮我在基线上报的两点都已修好并复验：
  1. `submission._decide_status` 的 `empty` / `duplicate` 判定顺序（现在先判 duplicate）；
  2. `delivery` 补齐 `delivered / status=accepted / understandingId`。
- **12 项未能验证**（前端 / SSE / 截图为主），逐条列在 §7，**不得**当成通过。
- 3 条次要观察见 §8。

## 1. 方法与口径

- 环境（先做，否则绿色结论不可信）：
  ```powershell
  $env:UV_CACHE_DIR='D:\qio-dev\.uv-cache'
  cd D:\qio-dev\qio-irm-e\backend
  uv sync --frozen --extra dev
  uv run --frozen python -c "import sys,agent;print(sys.executable);print(agent.__file__)"
  ```
  实测指向本 worktree：`D:\qio-dev\qio-irm-e\backend\.venv\Scripts\python.exe`、
  `D:\qio-dev\qio-irm-e\backend\src\agent\__init__.py`。
- 验收脚本：`scripts/interactive-real-model-verify/run_verify.py` —— 临时库 + **真实 uvicorn**
  （真实监听 127.0.0.1 随机端口）+ **真实 HTTP** + fake provider（挂在统一注入点
  `app.state.interactive_adapter_provider`，理解与执行共用）。
- 结论三态：`PASS` / `FAIL` / `NOT_VERIFIABLE`；有 `FAIL` 时退出码 1。
  **`NOT_VERIFIABLE` 不等于通过**。
- 只跑针对性定向用例；**没有**跑无筛选 pytest、全前端 vitest、全仓 eval、全量 CI。

## 2. 真实闭环（P1–P10）——全部 PASS

| # | 检查 | 实测证据（脚本打印的事实） |
| --- | --- | --- |
| P1 | 提交被可靠受理 | `delivered=true, status=accepted, understandingId=u_808b9e4af7e1, baseline.updated=true` |
| P2 | 后台理解真实调用模型并落定 | `状态=no_action，模型调用 1 次，stage=persist` |
| P3 | 材料诚实（文件/图片/网址/代码/未勾选注释） | `file=unreadable(仅文件名)/image=image_unsupported/url=url_address_only/code=inline；未勾选注释未进入解析` |
| P4 | 模型上下文只含可见内容 | 按**模型实际收到的 user 消息**核对：含勾选注释与材料，**不含**未勾选 / hidden 注释正文 |
| P5 | 真实意图入口 `{submissionId}` | `意图 e-merge-1，operations=['add_reply_card']，understandingId=u_5cb42deab3d0` |
| P6 | 批准后实际启动真实执行并写回结果卡 | `execution=x_59dd751c977c status=done，结果卡已写回，执行阶段未额外调用模型（理解阶段共 1 次）` |
| P7 | 执行只做允许操作、不动用户内容 | `执行 status=done；用户卡片 2 张全部原样；新增 1 张结果卡` |
| P8 | 结果卡来源引用 | `meta.reply 指向 intentId=i_8c750c5256c2、understandingId=u_03aac7cee4b2、executionId=x_c30a18f59a38` |
| P9 | 模型输出含禁止操作 | `status=failed，原因含越权说明，未落成任务，板面未被改动（模型调用 2 次）` |
| P10 | 输出无法解析 | `status=failed，模型调用恰好 2 次（首答 + 一次修复），无任务落库` |

正面证据（不是「演示 / 模板 / 关键词映射」）：

- P9 的模型输出是 `{"op":"delete_card"}`：程序校验直接拒绝并写明「不允许的操作」，
  **没有**落成任何任务，板面也**没有**被改动 —— 白名单是程序侧强制，不是提示词口头约定。
- P10 证明「自动修复至多一次」是真实行为：两次输出都不是 JSON，于是恰好调用模型 2 次后如实失败，**不落任务**。
- P6 证明结果卡正文来自理解阶段产出、用户批准的那段内容；执行阶段**没有**再调用模型（`calls_count` 不变）。

## 3. 契约边界反例

### 3.1 不进模型上下文（B1–B6，全部 PASS）

| # | 反例 | 实测结果 |
| --- | --- | --- |
| B1 | 未勾选注释 + 它参与的链接 | `after=['c_mat']`，注释与其链接均未进入载荷 |
| B2 | `hidden=true` | 既不在 after，正文也未在提交结果中泄露 |
| B3 | 仅一端可见的链接 | 未进入载荷 |
| B4 | 无可见成员的组 | 组整体不出现，组名未泄露 |
| B5 | 客户端伪造 `requestedVisible` | 服务端忽略并如实报告被忽略项 |
| B6 | 提交成功清勾选 | 勾选已清、卡片仍在（不是删除） |

P4 从**模型实际收到的上下文**再核对了一次 B1/B2：范围外注释的正文没有出现在模型输入里。

### 3.2 不产生模型调用（C1–C4 PASS，C5 未验证）

| # | 反例 | 实测结果 |
| --- | --- | --- |
| C1 | 空提交 | `empty`；理解记录 0 条 |
| C2 | 成功清勾选后立即再提交 | `empty`；未更新基准；理解记录保持 1 条（未重复调用模型） |
| C3 | 材料仍可见、只改位置 | `duplicate`；未更新基准；理解记录保持 1 条 |
| C4 | `PUT /state` 保存 | 未建立任何理解请求 |
| C5 | 聊天发送不调用板面提交接口 | **未能验证**（见 §7） |

> C2 的期望已按 Lead 的更正写死在脚本里：勾选被清后该注释退出可见范围，before/after 都排除它，
> 正确结果是 `empty` 而不是 `duplicate`；两者安全语义相同（不更新基准、不建理解请求）。

### 3.3 意图与审批边界（D1–D7，全部 PASS）

| # | 反例 | 实测结果 |
| --- | --- | --- |
| D1 | 要求不明确 | `needs_clarification`，真实入口不产出任务 |
| D2 | 无工作需求 | `no_action`，不强制产生任务 |
| D3 | 拒绝 | `rejected`；板面卡片数不变；没有产生执行 |
| D4 | 批准前材料变化 | 意图转 `needs_update`，批准被拒：「相关材料在保存后发生了变化……需要提交并由 QIO 更新预览后才能批准」 |
| D5 | 重复批准 | 执行记录 1 → 1，结果卡 1 张（未重复执行） |
| D6 | 依赖未完成 | 前项未完成时 `waiting_dependency`（不自动开始）；前项 `done` + 再次确认后 `running` |
| D7 | 批量冲突 | 两项都未批准（`approved=[]`，两项仍 `pending`），服务端判定 |

### 3.4 可靠性与恢复（E1–E8，全部 PASS）

| # | 反例 | 实测结果 |
| --- | --- | --- |
| E1 | 受理回执丢失 / 重复受理 | 同一 submission 两次受理得到同一条记录（`reused=true`），记录数不变 |
| E2 | 刷新 / 重复调用真实入口 | 意图集合不变，执行记录保持 0 条 |
| E3 | 理解失败后重试 | 快照仍是**失败前**的内容，未包含失败之后才加的 `c_later`（证明不重读当前板面） |
| E4 | 取消 | 暂停后 `paused` → 取消后 `cancelled`；用户后加内容保留；未留下结果卡；有撤回说明 |
| E5 | 迟到结果 | 归属对不上后旧执行被丢弃（`discarded=True, reason=owner_changed`），板面与执行状态都未被改写 |
| E6 | 重启恢复 | 伪造「别的实例」归属后，`running` 降级为 `paused`（`recovery={"paused": [...]}`） |
| E7 | 继续执行不重复已完成步骤 | 已 `done`（checkpoints=2）后再次继续被拒（`not_paused`），checkpoints 仍 2、结果卡仍 1 张 |
| E8 | 状态显示真实 | 执行状态轨迹 `['done']`，最终 execution=done / intent=done，不存在「已批准就一直运行中」 |

## 4. 材料诚实性（F1–F6 PASS，F7 未验证）

| # | 检查 | 实测结果 |
| --- | --- | --- |
| F1 | 已关联附件的文件卡 | `attachment_text`、`read=true`，正文含附件里真实存在的标记，attachmentId 正确 |
| F2 | 图片卡 | `image_unsupported`、`read=false`，原因「当前模型链路没有视觉能力：只登记图片元数据，不声称看懂图片内容」 |
| F3 | 只有地址的网址卡 | `url_address_only`、`read=false`，「只有网址，没有抓到网页正文」 |
| F4 | 卡片上已登记正文的网址 | `url_fetched`、`read=true`，并注明来自已登记结果、不是本次联网抓取 |
| F5 | 读不到 / 被截断 | 找不到附件 = `unreadable` 且有原因；超长材料 `truncated=true`、`characters` 记截断前长度、交付量按 `max_chars` 截断 |
| F6 | 不支持的来源 | `unsupported`、`read=false`，有可读原因 |
| F7 | 界面显示材料来源 | **未能验证**（需要前端截图/断言） |

没有 attachmentId 的文件卡：代码与实测都落在 `unreadable`、`read=false`、正文为空，原因
「这张文件卡片还没有关联已保存的附件（meta.file.attachmentId 缺失）：只有文件名「…」，文件名不是正文，未读取正文」。
**这正是我要的诚实口径**：拿到文件名不等于读到正文。

## 5. 契约面（X1–X8，全部 PASS）

- X1：`delivery` 三字段齐备；X2：理解列表与 `?submissionId=` 过滤可用、字段齐备；
- X3：取消接口可用（对已结束的理解如实返回 409 `not_cancellable`）；
- X4：执行记录接口可用，payload 字段与前端冻结类型 `Execution` 一致；
- X5：pause / resume / cancel 三个路由都可达并给出明确结果；
- X6：真实入口只认 `submissionId`，**不回落演示**（`no_action` 时如实说没产出任务）；
- X7：`models.ALLOWED_INTENT_OPS` / `execution.ALLOWED_OPS` / `prompts.OPERATION_SHAPES`
  三处与契约 §14.5 六项完全一致，且不含删除 / 隐藏 / 勾选用户内容 / 改用户卡片正文 / 写库；
- X8：迁移 37/38、`SCHEMA_VERSION=38`、`COMPENSATION_VERSION=38`、`REQUIRED_OBJECTS`
  与库内 `board_intents` 新列齐备。

## 6. 回归（R3/R5/R6/R7/R9 PASS；R1/R2/R4/R8 见 §7）

- R3：`/api/attachments` 可用。
- R5：互动草稿保存 / 恢复可用。R7：提交**没有**清掉无关草稿。
- R6：`impact-check` 返回 `ok=True`。
- R9：演示入口带 `demo` 标注、演示推进**不**产生 `board_executions` 记录；正式入口对无效
  `submissionId` 如实拒绝（`not_ready`），不显示演示内容。

## 7. 未能验证的部分（必须如实列出）

### 7.1 脚本里记成 `NOT_VERIFIABLE` 的 12 项

| 项 | 原因 |
| --- | --- |
| C5 聊天发送不调用板面提交接口 | 需要前端 / SSE 抓包证据；我只有后端 HTTP |
| F7 界面材料来源文案 | 需要前端截图 / 断言 |
| R1 原对话发送未破坏 | 只验证到 `/api/turns/queue` 与 `/api/health` 存活 |
| R2 流式回答未破坏 | 只验证到 SSE 票据接口存活；「边生成边显示」未验证 |
| R4 / R8 话题归属与相邻路径 | 需要前端 / SSE 归属证据 |
| V1–V6 窄窗口（800×600 / 480×600）与亮暗主题 | **我没有做真实浏览器视觉检查**（本 worktree 没有前端依赖，未安装、未起前端）。若主智能体/D 补做，请以截图为准 |

补充静态证据（**不当作行为验证**）：`ChatDock.vue` 文件头注明「只发送输入的文字（POST /api/turns），
不带板面、未提交改动、注释或选择范围……也不调用板面提交接口」；全仓**生产代码**中 `submitBoard`
只有一个调用点（`frontend/src/stores/interactive.ts:2381`，即板面提交动作本身），聊天发送路径没有调用它。
这只说明「代码结构上分离」，不等于运行时已验证。

### 7.2 其他没验证的（不在 66 项里的）

- **真实模型判断质量**：本轮全部用 fake provider；模型是否真的理解模糊要求，**没有验证**。
- **真实浏览器视觉检查**：未做（见 V1–V6）。
- **理解「断流」中途失败**：验证了「输出无法解析 → 自动修复一次后失败」（P10），
  但没有构造「调用中途断流 / 完成标记缺失」的场景。
- **审批后、执行前材料再变化**：验证了**批准前**材料变化（D4）；批准之后再变的拦截只有代码层阅读
  （`execution.approval_binding` / `stale_targets`），没有行为证据。
- **E7 的「半途继续」**：验证的是「已 `done` 后继续被拒、不重复」；真正「执行到一半暂停再继续」
  没有跑出来（fake 链路下执行太快）。
- **迟到结果的 `attempt` 形态**：E5 验证的是 `owner` 变化；单独让 `attempt` 变号丢弃没有单独构造。

## 8. 次要观察（不阻断，建议后续处理）

1. **E7 语义覆盖偏窄**：实现里「继续」在任务已结束时返回「只有暂停中的任务可以继续」，这是正确拒绝；
   但「暂停后继续、校验已完成步骤不重放」缺少确定性行为证据。建议补一个「执行到一半暂停」的用例。
2. **`checkpoints` 不对外暴露**：数据库列存在且迁移/补偿都覆盖，但 `execution_payload` 只给
   `steps`。这与前端冻结类型 `Execution` 一致，**不是缺陷**；只是从接口侧审计「不重放」需要读库。
3. **理解列表响应形状**：`{understanding: 单条, understandings: 列表}` 两个键并存，与前端
   `UnderstandingResponse` 完全一致（已核对 types.ts:381）。单数键是**单条对象**，消费者需注意。

## 9. 我自己的脚本缺陷（如实记录，避免把测试错误当产品缺陷）

### 9.1 本轮在候选 SHA 上修正的 7 处

| 缺陷 | 表现 | 处理 |
| --- | --- | --- |
| 把 `understanding` 当数组 | `KeyError: 0`，10 项误报「轮询超时」 | 按 `{understanding, understandings}` 两种形状解析 |
| 真实意图入口响应键写成 `intents` | 实际是 `created`，P7/P8 误报「没有意图」 | 统一用 `_created_intents()` 兼容两键 |
| E7 调了不存在的 `execution.resume_execution` | `AttributeError` | 改用真实入口 `intents.resume_intent` |
| E4 用 HTTP 暂停去抢执行 | fake 链路执行太快，暂停撞上 `already done`，误报「没有撤回报告」 | 改为模块层构造确定的「暂停中」场景（HTTP 路由就是转发到同一批函数） |
| X4 要求 payload 有 `checkpoints` | 接口按前端类型只暴露 `steps` | 改为核对前端冻结类型字段 + 单独查库确认列存在 |
| X8 用 `MIGRATIONS[36]` 位置索引 | 迁移列表长度 ≠ 版本号，`IndexError` | 改为按「编号唯一 / 递增 / 最新=38 / SCHEMA_VERSION=38」核对 |
| R2 把空票据当失败 | 开发豁免模式下 `auth_required=false`、票据为空是预期 | 改为只在 `auth_required=true` 且票据为空时失败 |

### 9.2 一处必须说明的 harness 问题

用 `urllib` 打真实 HTTP 时本环境偶发 `ECONNRESET`（同一路由换 `http.client` 连续 5 次全部 200，
服务端日志**没有任何异常**）。我最初把它记成 FAIL，属于误报；现已把验收 harness 固定为
`http.client` + 显式连接。**这是我自己测试工具的问题，不是产品缺陷。**

## 10. 复现方式

```powershell
$env:UV_CACHE_DIR='D:\qio-dev\.uv-cache'
cd D:\qio-dev\qio-irm-e\backend
uv sync --frozen --extra dev

# 全量定向验收（66 项）
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py

# 只看真实闭环 / 指定项 / 指定组
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --group real-path
uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --check P1,E3,F2,X7
```

- 脚本只用 fake / mock provider，不联网、不需要任何真实密钥。
- 每次运行的明细 JSON：`scripts/interactive-real-model-verify/out/verify-<时间戳>.json`。
- 本次三轮连续证据：`verify-20261011-195717.json`、`verify-20261011-195744.json`、`verify-20261011-200110.json`（三次均 PASS=54 / FAIL=0 / NV=12）。
