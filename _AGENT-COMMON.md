# 本轮协作公约（QIO 0.2.1 → 统一文字卡 · 持续许可 · 卡片内预览 · 多媒体对话）

基线：`v0.2.1` = `65cdd7e3759fe44e12c6802a0cf41777bfc2e086`。
**共享契约（先读，不可单方面修改）**：`docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md`

## 你的工作方式

* 你在**自己的 worktree / 分支**里工作，绝不直接改 main，绝不 push remote。
* 用 **TDD**：先写失败测试 → 跑到红 → 最小实现 → 跑到绿 → 提交（小步多次 commit）。
* 提交信息用中文、带范围前缀，例如 `feat(textcard): …` / `fix(preview): …`。
* 只在**你自己的文件所有权范围内**写文件。需要动别人范围的文件时，先发消息给主智能体（lead）。
* 发现契约有问题：**不要自行改契约**，发消息给主智能体说明冲突与建议。

## 环境

* 后端测试：`cd <你的 worktree>/backend`；venv 已软链到共享环境，用
  `& .\.venv\Scripts\python.exe -m pytest tests/<file>.py -q`（不要跑全量 pytest）。
  需要数据目录时设 `$env:QIO_DATA_DIR = '<你的 worktree>\.tmp-test-data'`。
* 前端测试：`cd <你的 worktree>/frontend`；`node_modules` 已软链。单文件：
  `npx vitest run src/<path>.test.ts`。类型检查只在交付前对**你改动的范围**用 `npx vue-tsc --noEmit`（一次即可）。
* **不要跑全量测试**、不要跑 `npm run build`、不要跑 `cargo`、不要触发 CI。全量验证由主智能体统一执行一次。
* 不要新增 Python 依赖（除非契约里写明）。不要新增 npm 依赖；需要新前端库时先问主智能体。
* 不要写任何需要真实 API Key 或联网的测试；模型一律用 `adapters/fake.py` 的 fake provider。

## 硬性约束（沿用仓库 AGENTS.md）

* 改 schema 只能**追加新迁移**，禁止修改历史迁移。
* 日志 / 事件 / Trace / 测试输出不得出现密钥原文；新增输出路径要过 `agent/trace/redact.py`。
* 前端颜色一律用 `var(--*)`（`styles/tokens.css`），字体三声部（标题衬线/正文无衬线/数据等宽），复用既有设计令牌与非线性缓动。
* 安全表述诚实：不把「已显示附件」「已生成预览」「已交付模型」说成「模型已理解」。

## 交付时给主智能体的汇报（必须）

1. 分支名 + 你所有 commit 的 SHA 列表。
2. 你改了/新增的**文件清单**（精确路径）。
3. 你跑过的**定向测试命令**与结果（成功/失败各几条）。
4. 你**没有做到**的部分、已知限制、以及需要主智能体在集成时注意的接缝。
5. 契约里你认为实现不了或有冲突的点。

汇报要短、要真实 —— 不要复述代码。
