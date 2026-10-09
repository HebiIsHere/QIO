# 2026-10-09 互动模式收尾轮实施方案（fix/interactive-final-closure）

- 基线：b67d1fe（远端 fix/interactive-content-protection-polish，已 fetch 核对一致，未推进）。
- 分支：fix/interactive-final-closure；子智能体各持独立 worktree 与 wt/final-{a,b,c,d,e} 分支。
- 范围：上一轮五项（01–05）+ 本轮十五类（06–20）+ 前端审美收尾（第六章）；不开发附件/记忆/真实模型板面理解。
- 协议：见 docs/interactive-final-closure-contract.md（M1–M9）。

阶段：
1. E 与各组并行建立正确行为反例矩阵（20 项，逐项触发→应有结果→基线结果→证据类型）。
2. 统一机制实现：M1–M9，修复共同根因。
3. Lead 集成各 wt 分支 → 固定候选 SHA → E 独立分层验收 → 缺陷回归修复。
4. 全量检查 + CI 核对 + 文档同步 + 推送（不合并、不发布）。

分工与所有权见契约 §0。
