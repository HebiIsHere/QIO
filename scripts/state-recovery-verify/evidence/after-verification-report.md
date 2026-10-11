# 第八轮状态恢复收尾 · 集成候选复核（after 侧）

- 被验收 SHA：**c8bde01457a6515c33fea3928f6ed2b7e1fd3759**（分支 fix/interactive-state-recovery-completion，
  工作树 D:\qio-dev\qio-src-d2，复核分支 wt/src-d-verify-after）。
- 复核者：子智能体 D（独立验证）。本目录的脚本与测试均来自 wt/src-d-verify（阶段提交 3b049dd）的合并结果；
  复核时只改验收侧文件（测试装置与文档），不改产品代码。

## 1. 反例测试（第①②③层）

| 层 | 命令 | 结果 |
| --- | --- | --- |
| 前端 store/组件 | cd frontend && npx --no-install vitest run src/acceptance-state-recovery | **8 文件 / 14 用例全部通过**（基线是 13 红 1 绿） |
| 前端类型 | cd frontend && npx --no-install vue-tsc --noEmit | **exit 0** |
| 后端 ASGI | cd backend && uv run --frozen --extra dev pytest tests/test_state_recovery_d_acceptance.py -q | **4 通过 0 失败**（基线 3 红 1 绿） |

原始日志：evidence/after-frontend-c8bde01.txt、evidence/after-backend-c8bde01.txt。

## 2. E1 截图断言矩阵（after 侧）

命令（24 格 = 4 尺寸 × 亮/暗 × 共存/保存失败/提交失败）：

    node scripts/state-recovery-verify/run-with-env.mjs -- capture.mjs --label=after --scenes=coexist,savefail,submitfail --themes=light,dark

结果：**24 格全部通过采集断言（失败 0）**。raw：shots/after-report-partial.json、evidence/e1-after-run.log、shots/after/*.png。
主题断言同 before（data-theme + 真实背景亮度 + 持久化偏好），工具栏高度与 before 一致（51/80/104px）。

## 3. E2 真实浏览器旅程（after 侧）

命令：

    node scripts/state-recovery-verify/run-with-env.mjs -- journey.mjs --label=after --scenarios=J1,J2,J3,J4,J5

| 场景 | before(b3245e5) | after(c8bde01) |
| --- | --- | --- |
| J1 草稿冲突→选服务器稿→本机删除失败→重试→关闭重开→编辑入口恢复 | 通过 | **通过** |
| J2 删除失败未重试→强制结束→重开后处理入口可达 | 通过 | **通过** |
| J3 R3 连续编辑 + N6 完整附加字段→关闭重开 | 红（N6） | **仍红（N6）** ← 见下 |
| J4 N3 待决定撤回项→界面「继续」 | 红（不发撤回请求） | **通过**（真实发出 POST /demo/advance，带 decisionIds） |
| J5 F1 取消影响确认+受控延迟 | 红（取消的正文仍在板面） | **通过**（取消的正文不再留在板面） |

## 4. ★复核发现：N6 附加字段在真实「刷新/关闭重开」路径上仍然丢失

- 组件级反例（第②层）在 after 侧是**绿的**；但真实浏览器旅程 J3（第④⑤⑥层）**仍然红**：
  关闭前网址/标题确实输入进界面（前置条件已断言通过），重开后两者回到卡片原值
  （["https://old.example/a","旧标题"]），正文按最后版本恢复。
- 原因（读实现得到的证据）：stores/interactive.ts 的 setCardDraftInput 注释明确写着
  「服务器草稿通道仍然只放正文（Record<string,string>），**附加字段只落在本机恢复来源里**」。
  而草稿真正保存成功后，本机恢复记录会被清理（flush 成功路径按版本删本机副本）。
  于是「服务器已确认 → 本机副本删除 → 刷新/关闭重开」这条**真实路径**上，附加字段没有来源，
  只能回落到卡片原值。组件级用例之所以绿，是因为它没有经过这次服务器确认+本机副本清理。
- 契约 §4 的要求是「完整未完成输入（正文 + 适用附加字段 + 空值）必须作为一个整体保存与恢复」。
  按此口径，J3 的失败是**真实缺口**，不是装置问题。
- 建议（供 Lead/A/B 决策，不在 D 的写入范围）：把附加字段纳入随服务器草稿同步的那份载荷
  （例如把 meta 一并编码进同一个草稿值，或新增随草稿同步的字段通道），
  保证「服务器确认后本机副本被清理」时仍能恢复；否则至少不要在服务器确认后删掉唯一带 meta 的本机来源。

## 5. 未验证 / 限制（after 侧同样成立）

- J5 的「等待期间独立移动保留」子断言：真实指针拖动在装置上没有让卡片移动，标为**未验证**
  （J5 的 F1 主路径已通过）。
- R2/R5 的浏览器级证据、N1/N4/N5 的浏览器级证据本轮未采集（分别有 store/ASGI 级反例覆盖）。
- N6 的 file/code 附加字段：真实浏览器只验证了 url 卡；file/code 有组件级反例覆盖。

## 6. 本轮复核对验收侧的三处修正（只改验收文件，不改断言语义）

1. n1-superseded-check.test.ts：deferred 的默认泛型是 never，resolve(对象) 会被 vue-tsc 判错 →
   给两个 resolve 实参加 as never（断言内容不变）。
2. f1-cancel-recovery-independent-changes.test.ts：最后那次保存前把影响预判替身改成
   「无影响」（否则按产品规则会再弹影响确认、不落库——那是契约要求的行为，不是缺陷）。
3. N4 后端用例：把「后来建立的关系」连到不同端点的对象上（原写法与既有关系是同一对端点，
   会被规范化去重，前置条件不成立）；另给断言补上关系列表诊断。
4. E2 装置修正：每场景全新 profile、正常关闭用 CDP Browser.close、decision 前置条件用本次新建的意图。
