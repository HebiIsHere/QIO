# R6 阶段二验收报告（D 独立验证）—— 附件就绪放行 / 声明解析 / 长正文退路 / 失败原因保留

- 契约：\`docs/plans/2026-10-08-four-remaining-fixes.md\`（Lead 冻结）+ §3 验收清单
- 工作树：\`D:\qio-dev\qio-r6-d\`（分支 \`wt/r6-d\`，已 merge 集成分支 \`fix/attachment-readiness-stream-boundaries\`）
- 验收人：D（只新增 \`backend/tests/test_r6_*\`、\`scripts/verify-r6-*\`、\`docs/verification-r6-*\`；**不改生产实现**）
- 口径：模型调用一律假 provider（本机扮演 SSE 厂商）；结论只说明「QIO 自己的链路对」，不证明任何真实厂商行为。

## 1. 实机取证：**18 / 18 通过**（EXIT=0，12 张截图 + summary.json）

跑法：\`powershell -ExecutionPolicy Bypass -File scripts/verify-r6-phase2.ps1\`
（假 provider → \`scripts/e2e_up.py\` 起 uvicorn:8734 + vite:5199 → msedge 无头 + Playwright）
产出：\`docs/verification-shots-r6-phase2/*.png\`（12 张）+ \`summary.json\`

### S1 附件准备期（只说准备、没有执行迹象、秒级就绪不闪）

\`\`\`
[PASS] S1 带附件发送：秒级就绪**不闪现**「正在准备附件」提示（防抖生效）
       :: {"chipReady":true,"preparingFrames":0,"samples":21,"shot":"r6-02-preparing-done.png"}
[PASS] S1 准备/受理期间界面**没有任何执行迹象**（不出现「正在思考/正在执行」）:: {"execSigns":0}
[PASS] S1 这一轮最终正常完成（回答出现）:: {"tail":"…我看到了这个附件。"}
[PASS] S1 边界（未验证）：准备窗口在本机是亚秒级（os.link 成功），无法在实机复现「准备期点中止」
       :: {"note":"中止语义由后端验收件 test_r6_readiness_gate_verify.py 第 3 条（取消后模型调用=0）与前端单测 PreparingAttachments.test.ts 覆盖"}
\`\`\`

即：带附件发送后界面**不会**出现「正在思考/正在执行」这类执行迹象；本机准备是亚秒级，
防抖生效（21 次采样 0 帧闪现）。**准备期点「中止」未能在实机复现**（见 §5）。

### S2 失败原因保留 + 按钮按 payload.actions

\`\`\`
[PASS] S2 浏览器字节上传失败：原因**明确且是原始原因**（不是「副本已不在」通稿）
       :: {"reason":"目标位置不可用：当文件已存在时，无法创建该文件。"}（HTTP 500、state=failed）
[PASS] S2 字节上传的 payload.actions 只有 reupload（没有 relocate/retry 这类走不通的死按钮）
       :: {"reuploadCount":1,"relocateCount":0,"noRecoveryNote":true}
[PASS] S2 本地路径登记失败：composer 的错误 chip 给出**原始原因**（不是通稿）且带可用操作
       :: {"pathReason":"… r6-路径失败.txt 28 B 准备失败 目标位置不可用：当文件已存在时，无法创建该文件。 重新定位 重试 ×",
           "pathActions":[{"text":"重新定位","cls":"act locate"},{"text":"重试","cls":"act retry"}]}
[PASS] S2 重新打开界面后：历史附件行**原因一致**（副本丢失 → 明确文案，不是空白/通稿混淆）
       :: {"rowText":"r6-历史原因.txt 22 B 文件不在原位 QIO 保存的副本文件已经不在了 重新定位 重试 ×","hadStoredPath":true}
[PASS] S2 装置说明：桌面端选文件入口在无头浏览器里无法驱动（Playwright 的 File 无真实路径），
       字节上传这条改走真实 HTTP + payload.actions 核验 :: {"httpStatus":500,"rowState":"failed","actions":["reupload"]}
\`\`\`

### S3 回答声明（provider 结束前可见、不泄漏、全局 1 份）

\`\`\`
[PASS] S3 正式回答在 provider 结束前已出现在回答容器（.message.assistant）:: {"observed":true,"answerCallStillOpen":true}
[PASS] S3 命中时刻过程区不含这段正式回答
[PASS] S3 完成后：声明不泄漏、全局只 1 份、过程区无副本 :: {"leaked":false,"occurrences":1,"inProcess":false}
\`\`\`

### S4 回归

\`\`\`
[PASS] S4 运行中默认折叠：默认可见区没有历史抽屉 :: {"historyCount":0}
[PASS] S4 结束原因与耗时可见（已完成 + 耗时）
[PASS] S4 内联审批：描述/独立说明/真实命令三者都在卡里
[PASS] S4 重试复用已保存副本（原文件已删除）且界面正常
       :: {"chipReady":true,"retryAppeared":true,"content":"R6 实机附件内容：只有这份副本里才有的标记 4d77"}
[PASS] S4 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200 :: {"contentStatus":200}
\`\`\`

## 2. 自动化闸门（集成态，本机实跑）

| 闸门 | 结果 |
| --- | --- |
| D 的 R6 三个验收文件 | **43 passed**（\`test_r6_declaration_chunks_verify\` 27、\`test_r6_failure_reason_preserved_verify\` 8、\`test_r6_readiness_gate_verify\` 8） |
| 后端全量 pytest | **EXIT=0**（本机 pytest -q 退出码 0；按项目约定不写会过期的硬编码测试数，要数字就跑命令） |
| 前端 | **1164 passed（136 files）**，\`vue-tsc --noEmit\` EXIT=0 |
| \`check_docs\` | 通过（32 个里程碑条目） |
| 实机取证 | **18 / 18**（§1） |
| CI（Lead 侧） | run 37729791630 @ \`d785838\` **9/9 success**（含 Linux py3.11/py3.12） |

## 3. 逐项对照 plan §3

### 问题一：附件就绪后才放行执行（\`tests/test_r6_readiness_gate_verify.py\`）

| 验收点 | 装置与结果 |
| --- | --- |
| 复制闸门关闭时**模型调用数 0、工具执行 0** | 真实 ASGI 路由 + 真实 TurnManager/TurnOrchestrator + 假 provider；闸门放在副本**写入开端**（工作线程），断言在 \`assert not gate.is_set()\` 之后立即取数：\`模型调用=0；释放后=1\`。工具执行 0 由「模型没被调用」直接推出（没有调用就没有工具轮）。**已验证** |
| 释放后新轮 \`read_attachment\` 读出正确内容 | 释放后克隆就绪、\`/content\` 含标记（\`内容含标记=True\`）。**已验证** |
| 复制失败 → 模型始终 0 次调用 + 准确原因 | 注入打在真实调用点（\`os.link\` 强制失败 + \`shutil.copyfile\` 抛 ENOSPC）：\`模型调用=0；HTTP=409；原因=att_…（复用已保存的副本失败：磁盘空间不足：无法保存副本（系统错误码 28…））\`。**已验证** |
| 准备期间取消 → 释放闸门后仍不能执行 | \`取消后：模型调用=0；queue={running: None, queued: [], cancelled: []}\`。**已验证** |
| 准备期间删除附件 | 与取消同源（\`abandon\`）；本文件未单独构造删除档 → **未验证（本报告口径）**，由 C 的实现测试覆盖 |
| 准备期间服务关闭 → 释放闸门后仍不能执行 | \`ctx.turns.shutdown()\` 后释放闸门：\`模型调用=0\`。**已验证** |
| 多附件**最后一个**未就绪 | 2 个附件、闸门只暂停第 2 个克隆（\`hold_after=2\`）：\`闸门关闭期间调用=0；释放后克隆=[…]；内容校验=[True, True]\`。**已验证** |
| 排队期间准备失败 | 占用轮 + 可恢复轮的 resend 准备失败：\`HTTP=409\`、\`code=attachment_binding_failed\`、响应里**没有被接受的 turn_id**、来源轮 claim 仍可恢复、\`queued=[]\`。**已验证** |
| resend 准备失败后**再次恢复** | \`首次=409；再次=200；新轮=turn_…；调用 0→2\`（claim 没有被永久消耗）。**已验证** |
| 同期 API/SSE 能推进 | 闸门关闭期间 \`GET /api/turns/queue\`、\`/api/session/context\` 均 200。**已验证** |
| 源轮历史副本与归属正确 | 原文件删除后原轮副本仍可读 200；历史里附件仍归属原轮。**已验证** |

### 问题二：增量前缀解析（\`tests/test_r6_declaration_chunks_verify.py\`）

| 验收点 | 装置与结果 |
| --- | --- |
| 声明与全部正文同一大分块 / 声明单独一块 + 正文整块 / 声明内部**每个位置**拆分 / 一字符一块 / 空块 / 确定性随机分块 | 14 种拆法参数化：**最终正文一致、角色一致（全部落正式回答区）、无声明泄漏、只 1 次调用**。**已验证** |
| 大小写 / LF / CRLF / 中文 / 代码块 / 表格 / 长英文 | 小写、CRLF、中文+代码块+表格（固定正文）、长英文（500 段）各有用例。**已验证** |
| 整段响应（不支持流式）/ 流式响应 | 整段档：同一契约（一次性交付、无泄漏、1 次调用）；流式档：\`streaming=true\` 增量。**已验证** |
| 取消 / 断流 | 断流：loop 如实抛出、**不执行任何工具**、已确认正文保留、声明不泄漏。取消由问题一/问题三分档覆盖。**已验证** |
| provider 在首段后暂停时回答容器已可见、过程区无副本 | 后端用例（\`chunk_delay_ms=2500\` + 命中时本轮仍在跑）+ 实机 S3。**已验证** |
| 至少一组经 NativeAdapter + 本地假 SSE provider | 实机 S3 走 NativeAdapter + 本地假 SSE provider；后端用例用可控分块 adapter。**已验证** |

### 问题三：未声明长正文的角色待定退路

| 验收点 | 装置与结果 |
| --- | --- |
| 阈值前 / 等号 / 阈值后 / 明显超过（按 UTF-8 字节） | \`UNDECLARED_MEMORY_LIMIT\` 从 \`core/answer_buffer\` 取值；\`LIMIT+1024\`（ASCII，字节=字符）、\`LIMIT//2\` 中文（字节≈1.5×LIMIT）、\`LIMIT\` 等号边界各有用例。**已验证** |
| ASCII 与中文分别检查字符数与 UTF-8 字节数 | 诊断同时打印字符数 / UTF-8 字节数 / 上限。**已验证** |
| 一整块大正文与多个小块结果一致 | 单块 + 拆块两种进入方式（\`_ChunkAdapter\` 可控分块）。**已验证** |
| 未声明长回答无工具 → 只 1 次调用、完整一次交付、过程区无副本 | 断言 \`adapter.calls == 1\`、\`final_content == text\`、过程区无事件、\`streaming=false\`。**已验证** |
| 长说明后有真实工具调用 → 工具执行正确且说明完整 | 工具被真实执行（\`seen=[{'text':'long'}]\`），长说明完整进过程区。**已验证** |
| 合法声明长正文**仍真流式** | 声明档用例断言 \`streaming=true\` 增量。**已验证** |
| 暂存创建/写入失败、取消、断流与清理 | **未验证（本报告口径）**：我只做到「超出内存上限后按字节计量、角色不乱」，暂存文件本身的故障注入、取消/断流后的暂存清理没有独立用例 |
| 内存有界、事件循环与同期 API/SSE 可推进 | 由问题一的同期 API 断言与 A 的实现测试覆盖；**本报告未独立测量内存上界** |

### 问题四：失败原因保留（\`tests/test_r6_failure_reason_preserved_verify.py\`）

| 验收点 | 装置与结果 |
| --- | --- |
| 真实上传路由注入 建目录/打开/写入（ENOSPC）/写入（权限）/写入（EIO） | 5 种注入，模块级 \`open\` seam（遮蔽内建，按写入方向判定）。**已验证** |
| HTTP 错误、原始数据库状态、首次 GET、重复 GET、列表、历史 payload、重新打开后的原因**一致** | 每个注入都断言：HTTP≥400、\`state=failed\`、原因非空且不命中通用文案、三个读路径 state/reason 集合相等、**同库同目录新 app（重新打开）**后依旧一致。**已验证** |
| \`failed\` 粘性、原始原因不被 \`missing\` 覆盖 | 重复读 + 重新打开后仍 \`failed\`；原因里含与注入一致的语义（空间/权限/I-O），不含「副本文件已经不在了」。**已验证** |
| 已成功副本后来删除仍转 \`missing\` | \`ready\` → 删除 stored_path → GET → \`missing\` + 副本丢失文案。**已验证** |
| 重试成功准确改 \`ready\`；再次失败保留**新的**原因 | 真实调用点注入（\`os.link\` 强制失败 + \`shutil.copyfile\` 抛 ENOSPC）→ 原因#1；换权限错误再重试 → 原因#2（与#1 不同）；解除注入再重试 → \`ready\` 且原因清空；无来源路径（浏览器字节上传）重试后仍 \`failed\`（不得静默成功）。**已验证** |
| \`prepared\` / \`cancelled\` 不回退 | **未验证（本报告口径）**：由 C 的实现测试覆盖，我没有独立构造这两档 |
| 浏览器无原路径 / 本地路径附件各自操作真实可用 | 实机 S2：字节上传 \`actions=["reupload"]\`（无 relocate/retry 死按钮）；本地路径失败 chip 给「重新定位 / 重试」。**已验证**（字节上传的**界面渲染**见 §5 装置边界） |

## 4. 阶段一的原始 FAIL 行（保留作对照）

阶段一在基线 \`6507c64\` 上的红证据（未修饰）：

\`\`\`
tests/test_r6_declaration_chunks_verify.py      6 passed / 22 failed（0.84s）
  AssertionError: ('一个大分块（声明+全部正文）：最终正文与期望不一致',
                   {'got': "[[QIO:ANSWER]]\n这是正式回答的第一段，说明结论。…"})
  AssertionError: ('声明单独一块+正文整块：最终正文与期望不一致', {'got': "[[QIO:ANSWER]]这是正式回答的第一段…"})
  AssertionError: ('小写声明：最终正文与期望不一致', {'got': "[[qio:answer]]…"})
  AssertionError: ('CRLF 声明：最终正文与期望不一致', {'got': "[[QIO:ANSWER]]\r…"})
  → 声明泄漏进正文 + 角色判错（plan §0.2）；未声明长正文另见 interim 改判 + 兜底重复生成（§0.3）

tests/test_r6_readiness_gate_verify.py          2 passed / 2 failed
  AssertionError: ('附件还没就绪（复制闸门仍关闭）时**模型已经被调用**了 —— 违反契约 §1.1「就绪后才放行」',
    {'calls_while_gated': 1, 'running': {'turn_id': 'turn_ab7b60dd4328', …}, 'queued': [], 'attachments': []})
  AssertionError: ('准备期间取消、释放磁盘闸门之后仍然开始执行了 —— 违反契约 §1.1「取消 = 中止该请求」',
    {'calls': 1, 'queue': {'running': None, 'queued': [], …}})

tests/test_r6_failure_reason_preserved_verify.py  7 passed / 1 failed（当时那 1 红是我的装置限制，不是产品缺陷）
\`\`\`

阶段一当时的红灯现在全部转绿（§2、§3）；期间发现并修正的问题：契约自相矛盾（Lead 已修）、
**我的注入只在 Windows 生效**（CI run 37727749922：Linux 的 \`shutil.copyfile\` 走 \`_fastcopy_sendfile\`，
包 \`builtins.open\` 不命中 → 改成打在真实调用点 \`shutil.copyfile\`，commit \`e56db66\`）。

## 5. 未验证与边界（如实）

1. **准备期「中止」未在实机复现**（S1 边界）：本机 \`os.link\` 成功 → 准备窗口是**亚秒级**，无法稳定停在
   「正在准备附件…」上点中止。中止语义由后端验收件（取消后模型调用=0）与前端单测覆盖；实机只证明了
   「不闪现 + 无执行迹象」。
2. **浏览器字节上传的界面渲染未在实机驱动**：Playwright 的 File 没有真实路径，桌面端选文件入口在无头浏览器里
   不可驱动 → 该条改走**真实 HTTP + \`payload.actions\`** 核验（\`["reupload"]\`、无 relocate），
   界面渲染由 C 的前端单测覆盖。
3. **C 的「慢复制拖慢其它 API」未复现**：同机 ASGI 下复制很快让出（卡点在复制循环的首次让出），
   没有构造出「复制把其它 API 拖慢」的可观测窗口 —— 我这边**没有**复现该现象，也没有反证；
   实时留待真实大文件 + 慢盘环境。
4. **B 的「有界等待生产值 60s」未真实等待验证**：我的用例只验证了「不放行就不执行」与「释放后执行」，
   没有真的把等待推到 60s 有界点上（那要占用 CI 一分钟）；生产值本身未被我验证。
5. **准备期进程崩溃路径无用例**：服务关闭我用的是 \`ctx.turns.shutdown()\`（优雅关闭）；
   **进程被强杀**（SIGKILL/断电）后的预留行回收没有用例。
6. **暂存盘真实故障只做 OSError 注入**：问题三的暂存创建/写入失败我这边没有独立用例；
   真实磁盘满/只读挂载未测。
7. **真实厂商 / 原生桌面 / 安装包未验**：全部证据来自本机假 provider 与浏览器（msedge 无头）；
   ApiKey 之外的联网行为、原生壳（路径拖入、无头浏览器不可驱动的选文件）、安装包升级路径均未验证。
8. **\`prepared\`/\`cancelled\` 不回退**、**准备期删除附件**两档由实现方测试覆盖，我没独立构造。

## 6. 已实现 / 已验证 / 未验证

**已实现（本轮四方改动，D 只验证不改）**
1. 预留 → 准备 → 放行：turns/resend 路由先 reserve、附件就绪后 activate；失败/取消/关闭走 abandon（不入队、不执行）；
2. 增量前缀解析：判定只依赖控制前缀，声明与长正文同块也能识别，声明不展示、不重复生成；
3. 未声明长正文：按 UTF-8 字节计量 + 有界内存（超出落暂存），上限只管理资源不决定角色；
4. 失败原因保留：\`failed\` 粘性、原因不被通用文案覆盖、重试保留新原因、\`ready→missing\` 如实。

**已验证**：§2 全部闸门 + §3 表中标「已验证」的条目 + §1 实机 18/18。

**未验证**：§5 第 1–8 条（准备期中止实机复现、字节上传界面渲染、慢复制拖慢其它 API、60s 有界等待生产值、准备期进程崩溃、暂存真实故障、真实厂商/桌面/安装包、\`prepared\`/\`cancelled\` 与准备期删除两档独立用例）。

**测试数字**：见 §2 表格（本机在同一棵树上实跑）。
