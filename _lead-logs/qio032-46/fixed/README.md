# QIO032-46 修复后浏览器定向验收（最终证据）

- 工作树：D:\qio-dev\pm-qio032-integration，分支 fix/qio032-46-board-state
- 验收时 HEAD：**e8fa8434**（fix(#46): 达到自动恢复上限后保持停止（切板/重读不重启自动保存）；含前一条 f9e08133 切板归属修复）
- 工作树状态：只有 ?? _lead-logs/qio032-46/（产品文件无本 harness 改动）
- 环境：隔离后端 127.0.0.1:8734（QIO_DATA_DIR=_lead-logs/qio032-46/e2e-data）、vite 5199、
  本机 fake provider 8807（scripts/verify-pm-qio032/fake_provider.py）、系统 Edge headless CDP 9333
- 驱动：真实浏览器 + 真实 Pinia store + 真实后端；可控时序只用 CDP Fetch（延迟 / 失败 / 自定义 409）；
  模型只打本机 fake provider（本轮场景不调用模型）。

## 一键复跑
    powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-up.ps1
    cd D:\qio-dev\pm-qio032-integration
    $env:QIO_UI_OUT='D:\qio-dev\pm-qio032-integration\_lead-logs\qio032-46\fixed'
    node _lead-logs\qio032-46\verify\run-46.mjs
    # 单项 / 多选：$env:Q46_ONLY='s1,s3,s4'; node _lead-logs\qio032-46\verify\run-46.mjs
    powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-down.ps1

产物：fixed/summary.json（结构化结果）、fixed/console.log、fixed/run.log、fixed/shots/*.png、fixed/materials/。
harness：verify/lib.mjs（CDP/网络/Fetch 规则/store 读取）、verify/run-46.mjs（场景）、verify/seed.py（隔离库执行中任务种子）。

## 最终汇总（HEAD e8fa8434）
运行：`Q46_ONLY='s0,s1,s2,s3,s4,s5,s8,s9,s10,s11,s12'` → **PASS 11 / FAIL 0 / OBSERVE 1 / TOTAL 12**

| 项 | 场景 | 结果 | 关键事实 |
| --- | --- | --- | --- |
| 0 | 原始路径 上传→设置→返回 | **PASS** | 上传 POST 1 次 200；后端回读 file 卡 meta.file.attachmentId=att_103a15fa5158；卡片/状态保留，无重复上传 |
| 1 | B 草稿自动保存前切 A + 旧回执晚于新输入 | **PASS** | 草稿 PUT 2 次全部是 /drafts/board_b_46（A 0 次）；B 草稿落库；页面新输入第三版未被旧回执覆盖 |
| 2 | B 保存后切 A、放回 B 的 409 | **PASS** | PUT 2 次全指向 B（1×409 + 1×200 恢复）；A 0 次 PUT、A saved 无错误；B saved |
| 3 | B 409 达上限 / 切出切回 / 手动重试 | **PASS** | 上限恰好 4 次 PUT；切 A/B 往返后仍 4；手动重试 1 次 PUT 200，只写 B |
| 4 | B 旧/新两次预判反序到达 | **PASS** | 旧预判迟到后确认仍只有一份（checkId/board 未变，chk_2381f550119d、rev=3）；确认后 PUT 只写 B（1×200），A 0 次 |
| 5 | B 点确认后切已保存 A、B 保存失败 | **PASS** | B 连续 3 次 PUT net::ERR_FAILED → B saveStatus=error；A 仍 “已保存”、无 saveError、A 0 次 PUT |
| 6 | B 撤回读取挂起 → A 撤回 → 返回 B 失败 | **UNVERIFIED** | 撤回（revert decision）链路未能在真实界面稳定驱动（需要 paused 任务 + revert 决策组合），不编造 |
| 7 | B 读取延迟/失败 + 旧请求不盖新读取 | **UNVERIFIED（harness 时序歧义）** | 真实 GET 共 3 次、服务端 seq=2，但 harness 观察到渲染仍为旧 seq=1；JS store 的 load 并发与 Fetch 放行顺序存在歧义，未能给出可信 PASS/FAIL，不判产品 |
| 8 | B 保存后材料范围刷新迟到（成功/失败） | **PASS** | 两轮 B 均 saved；A 的 visibleRange 在 B 迟到回执前后一致（均为空对象，断言偏弱但确无串写） |
| 9 | B 任务列表迟到 | **PASS** | 隔离库种子 running 任务（owner=当前实例）后：A 的 intents/intentViews 均为空，A 保存 2 次 PUT 全指向 A、saved |
| 10 | A 停留期间 B 上传完成 | **PASS** | 上传 POST 1 次；切回 B 立即看到真实终态 + 恢复入口；job=failed 原因“回执到达时当前板面不是发起上传时的原板面”，附件事实保留 |
| 11 | 已上传成功后重试关联（单卡） | **PASS** | 上传 POST 1 次；重试后后端回读 attachmentId=att_6fddfea67fe2；只写 B。**UI 重试入口未出现**（cardRetryVisible=false、add-material-retry 不在），用同一 store 动作 retryCardAttachment 驱动（详见边界） |
| 12 | 上传成功但保存关联失败 → 修复后重试回读 | **PASS** | 故障期 saveStatus=error；修复后重试成功，后端回读 attachmentId=att_55d4f09f9d26，成功文案“已关联附件…”；上传仅 1 次 |

## 诚实边界
- s6 / s7 为 UNVERIFIED，不当 PASS，也不把 s7 的现象直接判成产品缺陷。
- s11/s12 的“重试关联”在真实界面未找到可点入口（提示为临时提示，可能已在轮询前消失；卡片工具栏无 retry），
  最终用 store 的 retryCardAttachment（UI 按钮调用的同一动作）驱动；**UI 入口本身需要 Lead 决定是否算未覆盖**。
- [模拟] 项：s2/s3 的 409、s5/s11/s12 的 PUT 故障、s7 的迟到读取、s1 的草稿迟到回执、s8 的 visible-range 迟到，
  均由 CDP Fetch 制造；其余请求打真实隔离后端。
- 本轮未跑全量测试 / CI；不证明真实厂商、Tauri 原生窗口。
- 数据目录事故与处置：harness 早期版本的 seed 步骤继承了系统环境变量 QIO_DATA_DIR=D:\QIO-data（用户真实数据目录），
  曾向该库 ensure_board 写入 5 个空的 board_*_46_* 板面（无卡片、无意图）。发现后已删除这 5 个板面及其 board_states 行，
  真实库仅剩 board_default；之后 seed 强制使用隔离目录。产品代码与用户真实材料未改。
