# 互动模式第一阶段的验收截图

这些截图由 `scripts/interactive-verify/ui-scenarios.mjs` 驱动**真实 Chrome** 跑出来的，
对应最终提交（分支 `feat/interactive-foundation`）。跑法与说明见
`scripts/interactive-verify/README.md`。

| 截图 | 对应验收 |
| --- | --- |
| `im-40-conversation-entry.png` | 对话页底部的互动模式入口（与对话模式并列） |
| `im-11-two-notes-one-checked.png` | 两份材料 + 两条文字注释，只勾选其中一条（未勾选的那条写明「QIO 看不到它的文字」） |
| `im-12-two-cards-grouped.png` | 多选两张卡片后成组（组框、组名、成员） |
| `im-20-dragging-preview.png` | 真实鼠标拖动过程中：显示将要加入的组 / 插入位置 |
| `im-13-submitted.png` | 提交之后：本次有效改动、本次允许查看的范围、勾选自动取消，以及「保存不调用 QIO」的说明 |
| `im-30-demo-intents-and-previews.png` | 四项演示意图、辅助区任务列表、板面上的虚线预览、批量列表与演示标注 |
| `im-31-batch-conflict-blocked.png` | 在界面上批量批准一对互不相容的意图：只有一项被批准，另一项继续等待 |
| `im-50-save-with-stale-paused-task.png` | 库里存在「材料依据已失效的暂停任务」时，板面照常保存（复核发现的缺陷修复后） |
