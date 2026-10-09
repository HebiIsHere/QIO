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
| 01 | 新稿在 GET 开始前已输入；草稿 PUT 与 GET 在飞、PUT 先成功；旧 GET 后返回 | M1 读取基线：GET 发起时记录各键「已保存版本」基线，回执时 saved 越过基线即视为迟到正文、不予应用；新输入由版本比较保护 | 真实编辑框始终是新稿，刷新/乱序回执都不回退 | stores/__tests__/final-lead-m1.test.ts（状态）+ components/interactive/__tests__/final-a3-01-late-read-draft.test.ts（真实 textarea，3 例） |
| 02 | 失败原文找回并重发，回执未到时输入新文字并互换，旧成功把新文字删了 | M3 内容版本：互换时记录正文换新并 contentVersion+1、解除旧关联；成功只清 (记录|身份)+contentVersion+topicId 三重匹配 | 互换后的新文字不被旧回执清除，旧记录按实际接受的版本清理 | stores/__tests__/final-b2-sendIdentity.test.ts + d6SendProtection.test.ts |
| 03 | A 找回失败原文后切 B，在 B 手打相同文字发送成功，A 的失败记录被清 | 重发关联必须同话题：captureAttribution 要求同话题 + 输入框正文等于该记录当前版本 | 手动输入相同文字不误清别的话题记录 | final-b2-sendIdentity.test.ts（守卫用例基线即通过） |
| 04 | 话题为 null 时发送，绑定 A 前又输入新稿并本机保存，随后绑定 A、切 B、失败 | 发送归属与输入框保护解耦：bind 迁移「点击时话题为 null 且仍未定论」的发送到真实话题 | 原文稳定归 A，在 A 能找到；输入框后来的文字不影响归属 | final-b2-sendIdentity.test.ts |
| 05 | 冲突未选择时在冲突卡上改一个字，候选被清、本机版本覆盖服务器 | setDraft 未决冲突不清：服务器候选保留，新输入成为本机候选的新版本；组件四路径（选本机/服务器/继续编辑/取消） | 两份来源与选择入口一直在，选择由用户做出 | stores/__tests__/final-lead-m1.test.ts（05 例）+ components/interactive/__tests__/final-a3-05-conflict-paths.test.ts（4 例）+ d6CardProtect.test.ts（按 05 改写旧规则用例） |
| 06 | 反例A：第一版保存未返回时完成第二版，旧响应替换整块板面并清 dirty；反例B：未保存编辑时审批收尾保存失败仍回读把候选换成旧板面 | 板面候选版本记账 boardLocalRev/boardCleanRev：保存回执落地前比较版本，旧回执不覆盖新候选；读取在有未保存候选时只合并草稿、不整块覆盖板面 | 第二版候选保留、dirty 不被旧回执清掉；审批收尾后候选仍在，可继续保存 | final-lead-m1.test.ts（06 反例 A/B） |
| 07 | 运行任务依赖的卡片上删除/完成编辑触发影响确认，草稿已被独立清除，取消后新输入失去恢复来源 | 登记式清除：组件只 requestDraftClear(key)，store 在保存回执对应当前候选时才按登记版本执行；取消丢弃登记保留候选；登记后更新版本作废 | 未确认/失败/取消期间候选与恢复来源都在；正式变更成功后才清 | stores/__tests__/final-lead-m07-clear-binding.test.ts（3）+ components/interactive/__tests__/final-a3-07-draft-clear-binding.test.ts（5） |
| 08 | 预判失败仍继续写入；预判针对 A 期间改 B，晚返回的说明放行 B；等待确认时仍发出板面提交 | M4：impact-check 返回 checkId 绑定 (版本, 候选签名, 受影响任务)；确认带 checkId+stateVersion；服务端 409 stale_check/impact_confirmation_required 不落库；提交带 baseStateVersion(+confirmedCheckId) 且 409 stale_state；前端 409 分类显示真实原因 | 影响预判失败→保存暂停、原因可见、可重试；确认只对当时那一版有效；等待确认/未保存时提交被拦 | backend/tests/test_final_c_impact.py（12）+ frontend final-lead-m1.test.ts（路径1/2/3 + 409 + dict detail 解析）+ services/__tests__/final-lead-api-error.test.ts（3） |
| 09 | >20000 字草稿被后端静默截断并返回成功，前端仍清掉完整本机副本 | 后端超限整体拒绝（422 draft_too_long，不截短不部分写入）；前端保留完整本机副本并给准确原因 | 20000 完整保存；20001/20008 明确拒绝且原文不丢，重开后仍在 | backend/tests/test_final_c_drafts.py + frontend/src/interactive/__tests__/final-a2-long-draft-local-copy.test.ts（8） |
| 10 | A 有旧稿，新输入本机写失败，切 B 再回 A 新稿被旧稿替换 | 按存储键保留「未持久化成功的内存候选」与失败状态；恢复写入后只重试保存、不发送 | 切走再回来输入框仍是新稿并显示真实失败原因 | stores/__tests__/final-b2-chatTopicDrafts.test.ts |
| 11 | 首次迁移覆盖 A 既有稿；迁移写入失败双输；无关未绑定失败被吸收 | (a) 两份保留 + 让位登记与界面入口；(b) 目标写成功后才按版本清源；(c) adoptUnboundFailedSends 只迁移本次会话真实发送产生的未绑定失败 | 两份草稿都能看到、能取回/放弃；不拼接、不自动发送、不静默覆盖 | final-b2-chatTopicDrafts.test.ts（7）+ components/__tests__/final-lead-11a-displaced.test.ts（3，Composer/ChatDock 真实 DOM） |
| 12 | 本机 v1 存在、新稿与 cleared 写入失败，服务器清除成功后拿未写成的版本删 v1 被拒却标完成；重试只重发网络不补写 cleared | 区分计划版本与真实落盘版本（committedVersion）；removeCardLocalDraft 返回真实结果；ensureCardLocalClear 幂等补写；clearDraft 只登记真实落盘版本、retry 先补本机清除保护 | 旧稿不再复活；重开仍保持「已确认清除」 | interactive/__tests__/final-a2-clear-protection.test.ts（9）+ final-a2-reopen-cleared.test.ts（4，关闭重开式）+ stores/__tests__/final-lead-m13.test.ts |
| 13 | 真实「用服务器上的」选择中 removeItem 失败却显示正常，重开后放弃的稿又出现；聊天清空草稿吞删除失败显示 idle | 底层删除/清除返回 {ok,removed,reason,error}（storage-failure 与 version-guard 分开）；store 6 处调用点消费结果、失败保留待处理决定与重试入口；聊天清空路径同样消费 | 删除失败如实提示并可重试；version-guard 不被误报成失败 | interactive/__tests__/final-a2-drafts-removal-result.test.ts（12）+ final-a2-card-draft-hint.test.ts（+3）+ final-lead-m13.test.ts + final-b2-removeDraftResult.test.ts（4） |
| 14 | 重开后原文已在输入框、恢复组件隐藏按钮，直接重发成功但旧失败记录仍显示未发送 | 恢复/重开时按记录重建显式重发关联（唯一匹配才建）；编辑后发送仍算该记录；清空/切话题/互换/放弃解除；手打相同文字不清记录 | 重开后重发成功会清掉对应失败记录，且不误清其它记录 | stores/__tests__/final-b2-reopenResend.test.ts（5） |
| 15 | 对话页看不到共享聊天草稿保存失败；失败原文读取失败、零条恢复时错误不渲染 | Composer 显示草稿保存失败与重试（读同一份会话状态）；FailedSendNotice 的错误提示移出 records 块，区分读取失败与写入失败，零条也显示真实错误 | 保存/恢复失败都在界面上可见、可重试；不虚报已丢失/已恢复 | components/__tests__/final-b3-notice-visibility.test.ts（5）+ stores/__tests__/final-b2-readError.test.ts（5） |
| 16 | 材料经真实保存接口改动后，旧待审批预览仍可批准 | 意图创建时记录材料语义指纹；保存那一刻即标记依据失效（needs_update）；审批时服务端再次复核（单项/批量/依赖等待后确认共用）；普通位置变化不判失效 | 旧预览在材料变化后立即禁止批准并给出更新原因；依赖等待后再确认也不放行 | backend/tests/test_final_c_approval.py（5）+ test_interactive_intents.py / test_interactive_integration.py（按新协议更新）+ Lead 修 progress 私有键被状态流转丢弃的缺陷 |
| 17 | A、B 内容相同身份不同；A→C 后改 B→C 被判 duplicate | 去重按对象身份：链接端点与组成员按卡片 id，卡片仍按内容键（保住「撤回再加回同样内容」的重复规则）；有效改动求差含 link_added+link_removed | 真实关系改变是新的表达；同内容不同身份不再误判重复 | backend/tests/test_final_c_dedup.py（5）+ Lead 修 endpoint 返回内容指纹的关键缺陷 |
| 18 | 材料已在旧组，批准把它与摘要放进新组，完成后新组只剩摘要（旧组先占用） | 落地先做成员调整（从原组移出）再建新组，落库前按批准内容核对；无法按批准内容落地则拒绝并保持可处理 | 新组包含全部批准成员、旧组保留其余成员、一张卡只属于一个组、默认组名一致 | backend/tests/test_final_c_preview_apply.py（4）+ Lead 修夹具（成员须与组同时首次保存） |
| 19 | 预览只在左/上离屏时回移，右/下不管；任务列表定位只改局部聚焦；浮条可能离屏 | 定位换算与真实渲染一致（修 surfaceRect 二次减滚动量）；预览四方向 + 完全离屏夹取；任务列表走同一 locate-preview 事件真实平移板面；浮条视口约束 | 四个方向的预览都能被带回可用区；点任务项板面真的移动 | interactive/__tests__/final-d-canvas-locate.test.ts（6）+ final-d-tasks-locate.test.ts（2）+ final-d-overlay-geometry.test.ts（17） |
| 20 | 按住空格切窗口、在别处松开，返回后普通拖动仍是框选 | 失焦/页面隐藏/Escape/卸载统一复位临时按键与手势；isComposing 空格不劫持；输入框与控件空格不受影响 | 返回后普通拖动恢复平移；中文输入法与控件激活键正常 | interactive/__tests__/final-d-space-reset.test.ts（5） |

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