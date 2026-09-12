# QIO Release Qualification Report

日期：2026-09-12 ｜ 范围：Prompt 7（最终集成 / Dogfooding / E2E / 视觉验收）
被测版本：`main` + 本报告同批次改动（见「本轮修复」）

> 结论先行：**Result = PASS（Ready with known limitations）**。
> 自动化门槛全部通过；发现并修复 4 个真实缺陷；仍有 6 项已记录未修的限制，
> 其中 1 项（受限子进程不强制文件/网络隔离）属于安全边界，必须在文档与界面上保持诚实表述。

---

## 1. Result

| 门槛 | 结果 | 证据 |
| --- | --- | --- |
| Backend full suite | PASS | `uv run --frozen pytest` → 542 passed, 1 skipped |
| Frontend full suite | PASS | `vitest run` → 24 files / 170 tests passed |
| Frontend typecheck | PASS | `vue-tsc --noEmit` exit 0 |
| Frontend production build | PASS | `npm run build` exit 0 |
| Rust | PASS | `cargo check` exit 0（清理陈旧 target 后，1m01s） |
| SSE smoke | PASS | `backend/scripts/verify_sse.py` → `verify_sse: OK` |
| Offline eval | PASS | `python -m agent.eval.run` 指标 = baseline |
| E2E suite | PARTIAL | `scripts/e2e-checklist/run_memory_tests.py` → 15/19，4 项已分类 |
| Visual QA | PASS | 18 张截图 + 三种窗口尺寸 + 控制台检查 |

---

## 2. Automated Tests

新增 P7 验证测试 44 个，全部通过（已并入上表 542）：

| 文件 | 覆盖场景 |
| --- | --- |
| `backend/tests/test_p7_message_uniqueness.py` | 1：当前消息唯一性（normal / switch / create） |
| `backend/tests/test_p7_rapid_turns.py` | 2：A/B/C 快速提交、FIFO、单飞、队列事件、取消后继续 |
| `backend/tests/test_p7_subagent_race.py` | 3：子任务完成竞态（两种时序 × 三种延迟） |
| `backend/tests/test_p7_memory_authority.py` | 4 / 5：决策更新、稳定偏好不被新闲聊压制 |
| `backend/tests/test_p7_topic_and_knowledge_flow.py` | 6 / 7：话题导航全流程、知识注入闸门 |
| `backend/tests/test_p7_capability_enforcement.py` | 8 / 9：能力强制、路由暴露（含 2 个回归） |
| `backend/tests/test_p7_budget_and_provider.py` | 10 / 11：token 硬边界、provider 矩阵 |
| `backend/tests/test_p7_restart_trace_resources.py` | 12 / 13 / 14：重启恢复、Trace 可调试性、资源 |

关键断言结果：

- 当前消息在**每一次**模型请求中只出现一次（切换/新建话题路径同样是 1 次）。
- 主 turn 最大并发 1；队列不丢消息；`TURN_START`/`TURN_END` 按 `turn_id` 成对；落库顺序 = 提交顺序。
- 子任务完成的 notice 只进入正确 turn；A 已结束时转为独立 notify turn，绝不混入 B。
- 被 supersede 的旧决策不再进入注入面；旧偏好不被 20 条新闲聊挤出。
- 注入总量恒 ≤ hard cap；未知模型保守兜底；极小窗口下注入为 0 而不是溢出。
- 同一个 AgentLoop 跑 native/text 两种 provider；provider 错误只以 QIO 自己的类型逃逸。
- 重启后 memory/knowledge/topic/已批准工具/Trace 恢复；active turn、队列、内存凭据不恢复。
- 40 轮后 listener 数不变、asyncio task 数不增长；SQLite 无悬挂事务。

---

## 3. Eval Metrics

`uv run python -m agent.eval.run`（离线、确定性、不联网）：

| 套件 | 指标 | 值 | baseline |
| --- | --- | --- | --- |
| topic（n=12） | in_topic_accuracy | 0.75 | 0.75 |
| | switch_accuracy | 1.0 | 1.0 |
| | new_topic_precision | 0.8571 | 0.8571 |
| | new_topic_recall | 1.0 | 1.0 |
| | false_new_rate | 0.1667 | 0.1667 |
| | false_switch_rate | 0.0 | 0.0 |
| retrieval（n=8） | recall@1 | 0.75 | 0.75 |
| | recall@5 | 0.875 | 0.875 |
| | MRR | 0.8125 | 0.8125 |
| | wrong_memory_injection_rate | 0.25 | 0.25 |
| | stale_knowledge_injection_rate | 0.125 | 0.125 |

与基线完全一致（无退化）。数据规模小，只用于防退化。

---

## 4. Concurrency

- **Rapid turns**：A 阻塞时提交 B、C，观测 active=A、queued=2、主循环峰值并发 1；释放后执行顺序严格
  A→B→C；落库顺序一致；`TURN_QUEUE` 事件上报 running/queued 快照。
- **Cancel**：对阻塞在工具里的 A 取消 → A 记为 cancelled、B 正常开始并完成，取消有明确目标且不破坏后续 turn。
- **Subagent race**：三种延迟重复验证，notice 均进入 A 的后续规划步；A 已结束时转为独立 notify turn
  （独立 Trace、只写 assistant 消息），B 的任何请求都不含该 notice。

---

## 5. Security

| 检查 | 结果 |
| --- | --- |
| PURE 工具环境变量 | 仅 PATH/TEMP/TMP/PYTHONIOENCODING，无 `QIO_KEY_*` |
| 声明高风险 + 无容器隔离 | 明确拒绝执行（联网/文件路径/shell/凭据四类均验证） |
| TRUSTED 显式批准 | 仅放行被批准能力；PURE 默认不受影响 |
| 能力扩大 | 策略指纹变化 → 重启后不自动恢复，需重新审批 |
| 凭据撤销 | 撤销后立即不可用 |
| 密钥泄漏 | Trace 库表、Trace API 响应、错误信息中均无密钥原文 |
| 超时 | 返回干净失败（修复前会抛异常） |
| 输出上限 | 生效（修复前超大输出原样透传） |

**诚实边界**：受限子进程**不强制**文件系统/网络隔离。实测：声明为 PURE 的工具仍可读取用户目录
（`os.listdir` 成功）。当前执行器的强制力只来自「按声明拒绝高风险」+「剥离环境变量」，
谎报能力的工具不会被拦住。该结论已同步到 `docs/architecture.md` 与 `docs/status.md`。

---

## 6. Visual QA

实际启动后端（uvicorn 8734）与前端（Vite 5199），用 Chrome headless 驱动真实应用截图 18 张，
覆盖 1440×900 / 1280×620 / 900×700。

| 页面 / 状态 | 结论 |
| --- | --- |
| 对话空态、单轮、多轮长文本、运行中 | 正常；长文本自动换行，无横向滚动、无截断 |
| 队列（「1 运行中 · 2 排队中」） | 正常渲染，不遮挡内容 |
| 错误态 | 顶部错误条 + 「前往设置」入口，正常 |
| 设置页凭据 | 标签/状态/测试/详情/编辑/换钥/停用/删除齐全，密钥掩码 + 「只写不读」 |
| Trace Viewer：空态 / 详情 / 大 trace / 折叠展开 | 正常：7 个分区、JSON 自动换行、无横向溢出 |
| 窄窗口（900）与低高度（620） | 修复后无遮挡 |
| 控制台 | 无 page error；唯一 error 是 `/favicon.ico` 404 |

**真实端到端**：在浏览器中发出消息，DeepSeek 真实返回、消息落库、Trace 记录
（`turn_167b6ba9436a`，总时长 1580ms，模型延迟 1442ms）——主链路在真实模型下可用。

---

## 7. Performance

只记录 baseline，不做 micro-optimization：

| 指标 | 观测值 |
| --- | --- |
| 100 轮完整 turn（fake adapter，同一话题） | 3.4 秒（约 34ms/轮） |
| Trace 列表查询（limit 50，库中 100+ 条） | 0.3 ms |
| 记忆检索（top_k=6，100+ 轮之后） | < 0.1 ms |
| 40 轮后 ToolRegistry listener 数 | 与基线相同（无泄漏） |
| 40 轮后 asyncio task 数 | ≤ 基线 +2 |
| ToolRouter（6 个工具，每轮换查询） | 工具描述只嵌入 1 次；每轮仅新增 1 次 query 嵌入 |
| 真实模型单轮延迟（DeepSeek，观测） | 1.4–2.2 秒；有一次 51.5 秒异常，见 §9 |

结论：在 100 轮的规模上没有可观察到的退化迹象，也没有资源泄漏。

---

## 8. 本轮修复（4 项）

1. **沙箱超时抛异常并遗留子进程**（`agent/tools/sandbox.py`）：超时后显式 kill + wait；
   临时目录清理失败不再让执行以异常收场。
   回归测试已做红绿验证（回退修复 → 测试失败）。
2. **`policy.output_limit_chars` 声明了但从未生效**（`agent/tools/runtime_tools.py`）：
   函数型工具输出现在按策略截断并显式标注。同样红绿验证。
3. **前端悬浮星球遮挡消息**（`frontend/src/components/MessageStream.vue`）：
   修复前 900×700 重叠 78×66 px、1280×620 重叠 78×26 px；修复后三种尺寸重叠均为 0，宽屏布局不变。
4. **E2E 记忆套件自身失效**（`scripts/e2e-checklist/run_memory_tests.py`）：调用已不存在的
   `CreateTopicTool.run_sync`；未判空 `embedding`；知识状态断言读全表（被其它会话污染）；
   封块 fixture 缺「实体卡提炼」这一步，导致知识提炼永远拿不到回复；新增实体卡后清理顺序触发外键失败。
   修复后套件可完整运行，MEM-A6b 由 FAIL → PASS（`user_profile: pending_review` / `general_fact: active`）。

---

## 9. Known Limitations

**已记录、未修**

1. **受限子进程不强制文件/网络隔离**（安全边界）：PURE 工具实测可读用户目录。真正的强制需要容器/ACL
   级别的隔离；在此之前能力声明只是声明，不能当作安全保证。
2. **无嵌入模型时话题预判明显变弱**：缺本地 ONNX 模型时降级到规则层，1-gram/2-gram 分词稀释关键词
   重叠分数，E2E 用例 MEM-A3 因此拿不到主话题。阈值调整属于 eval-backed 的参数工作，不在本轮范围。
3. **Trace 存在时长归因缺口**：一次真实 turn `duration_ms=51498`，其中模型调用仅 1688ms，
   49.8s 未被任何分区解释（其余三轮未归因部分仅 44–138ms）。未定位，已记录。
4. **取消不中断进行中的模型请求**：取消只取消在途工具调用并标记 turn 为 cancelled。
5. **E2E 真实模型行为项不稳定**：MEM-B1 / MEM-B4 同一版本多次运行结果不同，应视为观测项而非门禁。
6. **视觉细节**：`/favicon.ico` 404（控制台唯一 error）；助手消息在数据到达前短暂显示 `tok 0`。

**未做视觉验证（诚实缺口）**

- 工具审批弹窗（创建 / 能力 / 凭据 / 长描述 / 拒绝 / 批准）：需驱动真实工具创建流程。
- 取消中的 UI 状态、工具失败态的 Trace 视图。

**环境专属（非仓库缺陷）**

- 本机 `frontend/src-tauri/target/` 是从另一项目拷贝来的陈旧构建缓存，导致 `cargo check` 报路径不存在；
  清理后 exit 0。CI 全新检出不受影响。

---

## 10. Release Recommendation

**Ready with known limitations.**

自动化门槛全绿，端到端在真实模型下可用，安全项按声明策略强制且拒绝路径已实测；发现的问题中 4 个已修并验证。
保留的限制主要集中在两处：受限子进程的安全边界（已如实写进架构文档，不应被当作隔离保证），
以及无嵌入模型时的话题预判质量。两者都不阻塞继续 dogfooding，但应在下一轮排期处理——
尤其是不要在没有容器隔离的前提下让 AI 生成的工具持有高风险能力。
