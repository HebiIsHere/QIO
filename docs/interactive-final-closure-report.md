# 互动模式收尾轮验证报告（fix/interactive-final-closure）

> 结构先落位，事实在集成与独立验收完成后填写；未完成项会明确标注，不写成通过。

## 1. 基线与产物

- 审查基线：`b67d1fecd5a4f3a71c02c4b944415c0761d61d60`（fix/interactive-content-protection-polish，fetch 核对远端一致、未推进）
- 修复分支：`fix/interactive-final-closure`
- 最终 SHA：待填
- 远端一致性：待填（fetch 后对比）
- 工作区状态：待填

## 2. 统一机制（契约 M1–M9）

见 `docs/interactive-final-closure-contract.md`。要点：板面与草稿的读取/写入版本基线、本机恢复副本与清除事实、发送尝试身份与显式重发关联、影响确认的版本绑定（checkId）、提交版本校验、审批材料指纹、去重按对象身份、几何与按键复位。

## 3. 二十项逐项结论

| # | 触发 | 修复 | 用户可见结果 | 证据 |
| --- | --- | --- | --- | --- |
| 01 | 待填 | 待填 | 待填 | 待填 |
| 02 | | | | |
| 03 | | | | |
| 04 | | | | |
| 05 | | | | |
| 06 | | | | |
| 07 | | | | |
| 08 | | | | |
| 09 | | | | |
| 10 | | | | |
| 11 | | | | |
| 12 | | | | |
| 13 | | | | |
| 14 | | | | |
| 15 | | | | |
| 16 | | | | |
| 17 | | | | |
| 18 | | | | |
| 19 | | | | |
| 20 | | | | |

## 4. 前端审美收尾（第六章）

待填：实际改动、四尺寸×两主题对照截图路径。

## 5. 证据分层

1. 单元/状态：待填
2. 真实组件/DOM：待填
3. API/数据库：待填
4. 真浏览器（visual_probe + e2e_up + fake provider）：待填
5. 关闭重开：待填

## 6. 全量检查

- 前端 `npx vue-tsc --noEmit`：待填
- 前端 `npx vitest run`：待填
- 后端 `uv run --frozen pytest`：待填
- `python scripts/check_docs.py`：待填
- 视觉检查（实际起应用）：待填

## 7. CI

待填：最终 SHA 对应运行号与状态（未运行 / 查询失败 / 跳过 / 通过分别如实写）。

## 8. 新发现的缺陷与剩余限制

待填：本轮流程中新发现的相关缺陷与处理；必要剩余限制与未验证项。

## 9. 边界声明

- 未合并 main，未合并 fix/unified-process-audit、feat/unified-process-attachments-streaming。
- 未开发附件系统、记忆系统、真实模型板面理解或新的任务执行能力；`delivery.delivered` 未被伪造为 true。
- 未做数据库迁移，未修改历史迁移。
- 子智能体模型：本会话仅允许默认 spawn 路由（glm-5.3-flash）；用户要求的 deepseek-v4.1-flash 在本会话不可选（`list_subagent_models` 拒绝所有 provider），已如实记录，未影响任务范围与验收标准。
