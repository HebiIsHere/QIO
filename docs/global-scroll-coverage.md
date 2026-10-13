# QIO 统一滚动覆盖清单（2026-10-14 第十二轮）

本清单回答一个问题：**每个滚动区域现在归哪一档策略、证据是什么。**
规则定义见 `docs/superpowers/specs/2026-08-09-qio-frontend-design.md` §15 与
`docs/interactive-mode-contract.md` §16；组件用法见 `docs/frontend-components.md` §8。

三条策略的含义（只此三种，组件不得自造第四种）：

- **auto**：实际溢出时允许滚动；QIO 指示条默认隐藏，滚动 / 靠近边缘 / 键盘使用时按需显示。
- **hidden**：保留滚动与定位能力（`scrollTop` / `scrollLeft`、平移、坐标换算），但**始终不画**滚动条。
- **content**：不创建滚动视口 —— 内容自然展开、换行或折叠。

判定口径（实测得到，见 §15.3 与下方「测量口径」）：
`overflow-x: clip` 在另一轴为 `auto/scroll/hidden` 时**计算值会变成 hidden**，
因此「加了 `data-qio-fit`」不等于「不可横向滚动」；合格判据是
**内容不横向溢出（`scrollWidth − clientWidth ≤ 1`）**，加上「`scrollLeft` 赋值后不移动」的辅助信号。
`clientWidth/clientHeight ≤ 0` 的折叠/隐藏元素没有可视区域，不算可滚动范围（引擎已按此判定）。

## 1. 普通对话（`views/ConversationView.vue` + `components/MessageStream.vue`）

| 区域 | 内容状态 | 原滚动层 | 必要方向 | 目标策略 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 消息流 `.stream` | 空 / 短历史 | `overflow-y:auto` + 原生 thin 滚动条 | 纵向 | auto（唯一主要纵向视口） | 溢出时才出现指示条；空态无指示条、无轨道 |
| 消息流 | 长历史 / 流式增长 | 同上（含虚拟列表） | 纵向 | auto | 跟随 / 上翻不拉回 / 回到最新 / 历史锚定保持 |
| 单轮 `.turn`、消息、气泡、正文 | 任意 | 无自身滚动 | 无 | content（`data-qio-fit` + `min-width:0` + 换行） | 消息子树内 0 个滚动视口 |
| 工具卡 / 参数 / 输出 / 错误与恢复详情 | 长 JSON、长日志 | 无（原先靠 `max-height` 小窗） | 无 | content（软换行） | 全文可读、无横向范围 |
| 过程区（`TurnProcess`） | 展开历史 / 详情 | `.tp-outcome-pre` 曾有 `max-height:160px + overflow:auto` | 无 | content | 同一阅读区展开，审批与可用操作仍在 |
| 输入区（`Composer`） | 空 / 短草稿 | 浏览器默认 | 无 | hidden（无滚动范围） | 空内容无滚动范围、不预留轨道 |
| 输入区 | 长草稿 / 粘贴 | textarea 自身 | 纵向（到上限后） | hidden | 到上限只纵向滚、光标跟随、选区与撤销不变 |
| 候选列表 `.candidates` | 候选项多 | `max-height:42vh + overflow-y:auto` | 纵向 | auto（独立并列列表） | 与消息流互不抢焦点 |

## 2. 互动对话（`components/interactive/ChatDock.vue`）

| 区域 | 内容状态 | 原滚动层 | 必要方向 | 目标策略 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 面板根 | — | `overflow:hidden` | 无 | content（两轴 clip） | 不再建立滚动容器 |
| 聊天区 `.stream` | 消息 + 过程 + 提示 + 失败恢复 | `overflow-y:auto`（原先只覆盖消息） | 纵向 | auto（唯一主要纵向视口） | 面板内 `[data-qio-scroll]` 只有 2 个：stream(auto) + composer(hidden) |
| 过程区 `.process` | 阶段 / 详情 / 审批入口 | `max-height:45% + overflow:auto` | 无（并入 stream） | content | 展开内容滚得到；审批入口不展开详情即可操作 |
| 通知 `.panel-notices` | 警告 / 轮次错误 / 失败恢复 | `max-height:30% + overflow-y:auto` | 无（并入 stream） | content | 警告「简述 + 展开全文」；错误与恢复完整可见 |
| 附件条 `.attach-list` | 待发附件 | `max-height:96px + overflow-y:auto` | 无 | content | 默认 2 条 + 「展开其余 N 个」 |
| 帮助 `.help-body` | 说明正文 | `max-height:180px` | 无 | content（details 展开） | 与编辑区同层 |
| 输入区 `.composer` | 编辑态 | `overflow-y:auto` | 纵向 | hidden（编辑区独立一层） | 不画指示条；长输入仍可纵向滚 |
| 失败恢复 `FailedSendNotice` | 失败原文 / 未决项 | 四个 `max-height + overflow-y` 小窗 | 无 | content | 原文逐字保留，规模交给「默认 3 条 + 查看其余」 |
| 上翻时的待办入口 | 新审批 / 新错误 | — | — | 固定入口（滚动区之外） | 「回到最新 · 待审批 N 项」，一步可达 |

## 3. 互动板面 / 卡片 / 预览

| 区域 | 内容状态 | 原滚动层 | 必要方向 | 目标策略 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 板面 `.board-viewport` | 任意 | `overflow:auto` + 原生条 | 平移/缩放保留 | **hidden** | 0 条轨道、`data-qio-range=none`；`scrollLeft/scrollTop`、平移、缩放、坐标换算不变 |
| 卡片正文 `.card-body` | 常态 / 展开 / 编辑 | `overflow-y:auto` + `overflow-x:hidden` | 纵向 | auto（这张卡唯一主要纵向视口） | 展开阅读只有一层纵向视口 |
| 卡片代码块 `.code-body` | 长代码 | 横纵都自己滚（第二层） | 无 | content（软换行） | 复制/文本逐字不变 |
| 卡片编辑态 | 长文本 | textarea 第二层 | 无 | content（自动增高） | 最小高度按卡片收敛，不撑出小卡 |
| 组内顺序/成员条 | 宽列表 | `overflow-x:auto` | 横向 | auto(axis x) | 保留横向浏览，去原生外观 |
| 板内搜索 `.search-panel` | 结果多 | `overflow:auto` | 纵向 | auto(axis y) | 统一策略 |
| 改动列表 `.changes-list` | 条目多 | `overflow:auto` | 纵向 | auto(axis y) | 独立并列列表 |
| 整理菜单 `SelectionMenu` | 选项多 | `overflow-y:auto` | 纵向 | auto(axis y) | 长组名换行不撑宽 |
| 预览 `CardInlinePreview` | 图片 / PDF / 表格 / 代码 | `.cip-body` + 各渲染器各一层（最多三层） | 视上下文 | content / self / viewer 三态（见下） | 逐渲染器「纵向视口恰好 1」 |
| 预览（对话内 `context="chat"`） | 任意 | 同上 | 无 | content（无横向） | 表格/代码/图片 `over ≤ 1` |
| 预览（独立材料 `context="material"`） | 放大查看 | 二维浏览区 | 二维 | auto(axis both) | 只有查看区允许横向，外层卡片/正文 `over ≤ 1` |

## 4. 其它页面与浮层

| 区域 | 原滚动层 | 目标策略 | 结果 |
| --- | --- | --- | --- |
| 设置页根 / 面板 | `overflow-y:auto` + 原生条 | auto + fit | 单层；正文与固定操作分离 |
| 设置页「QIO 滚动指示条」 | — | 偏好项（`html[data-scrollbar]`） | 自动显示（默认）/ 始终显示 |
| 下拉 / 组合框浮层（`QSelect` `QCombo`） | 列表可无限长 / `max-height` | auto(axis y) | 长标签换行，浮层不再撑出视口 |
| 审批弹窗 `.body` | `max-height + overflow-y` | auto + fit | 操作行在滚动区外 |
| 审批事实 / 技术明细 | `max-height:160px` ×2 | content | 与正文同层 |
| 影响确认对话框 | 两轴 auto | 对话框两轴 clip + `.body` auto | 操作区 `flex:none` |
| 任务清单（`DevTaskEntry`） | 自绘 `::-webkit-scrollbar{width:8px}` + track/thumb | auto + fit（局部规则已删） | 不再有第二套滚动条语言 |
| 凭据弹窗 | `overflow:auto` | auto + fit | 标题/操作固定 |
| 引导向导正文 | `overflow-y:auto` | auto + fit | — |
| 调试视图 | 根 auto + JSON `overflow-x:auto` + 限高 | 根 auto + JSON content（软换行） | 无「藏起来但还能滚」 |
| 星球页 `.planet-view` `.panel-inner` `.panel-body` | `overflow:hidden`（仍可脚本滚） | **两轴 clip** | `moved=0`，不再是滚动容器 |
| 星球页 `.topic-scroll` `.detail-scroll` | auto + 内容撑宽 | auto + 横向锁死 + 内容换行 | 实测 `over=0 / moved=0`（保留纵向滚动） |
| 恢复收件箱 / 候选卡 / 工具创建卡 / 叙事抽屉 | 单行截断或折叠裁剪 | content（换行 / 展开入口 / `clip`） | 长文本完整可读 |

## 5. 明确「完全不显示滚动条」的区域

- 互动板面滚动区（`hidden`）：原生条与 QIO 指示条都不出现。
- 普通对话与互动对话的输入区（`hidden`）：长输入到上限后仍可纵向滚，但不画条、不预留轨道。
- 一切零尺寸（折叠 / 隐藏）容器：既不画条也不报范围。

## 6. 明确「取消独立滚动」的区域（改为 content）

过程历史与工具详情、通知与恢复摘要、附件条、帮助正文、卡片代码块、卡片编辑区、
失败恢复浮层的四段原文、审批技术明细、调试视图的 JSON 正文。

## 7. 测量口径与证据

- 内容溢出：`scrollWidth − clientWidth`（≤ 1px 合格，允许取整误差）；
- 可滚动性：赋值 `scrollLeft = 500` 后读回（> 0 即真的能滚）——**不可只用 wheel deltaX**（会假通过）；
- 零尺寸元素（`clientWidth/clientHeight ≤ 0`）单列，不算可滚动范围；
- 真实证据：Lead 探针 `D:\qio-dev\_gs-verify\lead\probe.mjs`（18/18 通过）、
  独立复核 `D:\qio-dev\_gs-verify\tools\audit.mjs`（基线与集成版对照）、
  证据目录 `D:\qio-dev\_gs-verify\`（基线、clip 判定、星球页、板面声称、集成版）。
