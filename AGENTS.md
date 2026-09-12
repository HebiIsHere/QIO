# QIO 项目约定（AI 助手必读）

- 前端设计规范：`docs/superpowers/specs/2026-08-09-qio-frontend-design.md`
- 颜色一律用 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 字体三声部：标题/话题=衬线，正文=无衬线，数据/时间/密钥=等宽。
- 星球渲染：透明球 + SDF 融合环（内粗外细 10/6.5/4px），禁止另加额外水波；聚焦距离 2.25。
- 改了前端代码：IAB 需带新参数强制刷新；改了后端：重启 uvicorn（无热加载）。

## 事实来源与文档

- 进度、已知限制：`docs/status.md`。结构契约：`docs/architecture.md`。跑法：`docs/SETUP.md`。
- 改了实现就同步改 `docs/status.md`；三处里程碑状态必须一致（`python scripts/check_docs.py` 会查）。
- 不写会过期的硬编码数字（测试数、事件数、表数），要数字就跑命令。

## 改动后的验证

- 后端：`cd backend; uv run --frozen pytest`（必须全绿）。
- 前端：`npx vue-tsc --noEmit` + `npm test`。
- 改了前端：实际起应用并用截图做视觉检查（窄窗口、滚动、代码块溢出）。
- 改了 runtime / 预算 / 工具策略：除受影响测试外，重跑 `uv run --frozen python -m agent.eval.run` 对比基线。

## 硬性约束

- 改 schema 只能追加新迁移，禁止修改历史迁移。
- 任何日志、事件、Trace、错误信息、测试输出都不得出现密钥原文；新增输出路径必须过 `agent/trace/redact.py`。
- 安全表述必须诚实：受限子进程不是安全沙箱；工具扩大能力必须重新审批。
- 不要引入以真实 API Key 或联网为前提的测试；模型调用一律用 fake/mock provider。
