# QIO 项目约定（AI 助手必读）

- 前端设计规范：`docs/superpowers/specs/2026-08-09-qio-frontend-design.md`
- 颜色一律用 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 字体三声部：标题/话题=衬线，正文=无衬线，数据/时间/密钥=等宽。
- 星球渲染：透明球 + SDF 融合环（内粗外细 10/6.5/4px），禁止另加额外水波；聚焦距离 2.25。
- 改了前端代码：IAB 需带新参数强制刷新；改了后端：重启 uvicorn（无热加载）。
