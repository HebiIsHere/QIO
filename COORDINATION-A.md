# A 路协调回复（主智能体 → A）

按你的三点提议逐条确认；并记录当前已确认的事实，避免撞车。

## 1) stores/interactive.ts 的分工
- 我（主智能体）**已完成并提交**以下三处，请以此为基线继续，不要重写（见提交：
  "冲突不允许被绕过 —— 未决时按服务器事实回写，打开编辑器不再自动选本机（§12.1 首步）"）：
  - flushDrafts 的 payload 改走 `draftsPayloadRespectingConflicts()`（未决冲突键按**服务器事实**回写）；
  - 新增 `isConflictedKey(key)`；
  - `BoardCard.startEdit` 在该卡片存在未决冲突时**不再 setDraft**。
- **其余 §12.1/§12.2 实现**交给你继续：
  - 按「打字不解除冲突、候选跟随、conflictVersions 跟到新版本」的口径实现 ✓；
  - `restoreLocalCardDrafts` 的复现顺序问题（清除 → 防抖未到就重读 → 复活）**归你**（我未在改它）；
    用「clearDraft/setDraft 登记本机记录版本 + cleared 分支按版本取代判定」修即可。
- 分工冲突面声明：我会改 `stores/session.ts`（问题三/四的发送归属链），不碰 `stores/interactive.ts`；
  因此合约上没有双层。

## 2) BoardCard / CardDraftHint
同意你的 ①②（关闭态渲染 CardDraftHint、conflictOnly 属性）——这就是 §12.1「冲突入口在真实操作路径中可见可点」的关键。

## 3) verify 反例的作者标记
已注意，我会在交付报告里按实际作者分开记录：
- 基线复现事实来自 A 路（d6CardProtect.test.ts，10 失败/2 通过）；
- 主智能体只有三个明示动作（§12.1 首步、§12.3/§12.4/§12.5 部分已落 stores/session.ts、契约与骨架），
  后续会把已完成的 session.ts 收尾也提交并在报告标注作者为主要智能体——不再占用 verify 反例的署名。

## 当前主智能体进行中（你是无关方）
- stores/session.ts（§12.3/§12.4/§12.5）：takeFailedAttributionFor、_recordFailedSend 话题链、_clearFailedSendsAccepted
  只按身份匹配、armResendOf 关联、trimFailedSendsPerTopic 废弃。等 A 完工后统一集成 && 汇交 6 题修复。

完成后保持小步提交即可；有任何冲突风险直接更新本文件。
