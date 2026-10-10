# 复跑证据：N6 修复后的集成候选 ccc651a（after2）

- 被验收 SHA：**ccc651ae53d356240e1560abd39f9bedd7f0f86f**（fix/interactive-state-recovery-completion）
- 复核分支/工作树：wt/src-d-verify-after @ D:\qio-dev\qio-src-d2（已 merge 到 ccc651a）
- 触发：Lead 按 D 的 N6 根因分析修复了「带附加字段快照的本机记录被当冗余副本清理」，
  要求复跑 J3（N6 网址/标题随未完成输入恢复）与其余受影响场景。

## 1. 反例测试（全绿）

| 层 | 命令 | 结果 |
| --- | --- | --- |
| 前端 | npx --no-install vitest run src/acceptance-state-recovery | **8 文件 / 14 用例全部通过** |
| 后端 | uv run --frozen --extra dev pytest tests/test_state_recovery_d_acceptance.py -q | **4 通过 0 失败** |

raw：evidence/after2-frontend-ccc651a.txt、evidence/after2-backend-ccc651a.txt。

## 2. E2 真实浏览器旅程：**5/5 全绿**（before 是 2 绿 3 红）

命令：

    node scripts/state-recovery-verify/run-with-env.mjs -- journey.mjs --label=after2 --scenarios=J1,J2,J3,J4,J5

raw：shots/after2-journey-report.json、evidence/e2-after2-run.log、shots/after2/*.png。

| 场景 | after(c8bde01) | after2(ccc651a) |
| --- | --- | --- |
| J1 草稿冲突→选服务器稿→本机删除失败→重试→关闭重开→编辑入口恢复 | 通过 | **通过** |
| J2 删除失败未重试→强制结束→重开后处理入口可达 | 通过 | **通过** |
| J3 R3 连续编辑 + N6 附加字段→关闭重开 | 红（N6） | **通过** |
| J4 N3 待决定撤回项→界面「继续」 | 通过 | **通过** |
| J5 F1 取消影响确认 + 受控延迟 | 通过 | **通过** |

### J3 的关键实测（N6 缺口已消除）

- 关闭前前置条件：附加字段确实已输入进界面 → ["https://new.example/path","新标题"]；
  未完成正文已真实保存到服务器草稿 → {"card:c_url":"第二版草稿（连续编辑）"}。
- **关闭浏览器进程 → 同一 profile 重开**后：
  - 正文恢复最后版本："第二版草稿（连续编辑）"；
  - **网址恢复 → https://new.example/path；标题恢复 → 新标题**（此前红的那两条）；
  - 恢复不自动形成正式改动：正式内容仍是原值「网址卡的正文」。

### 其余场景的关键实测

- J4：「继续」真实发出 POST /api/interactive/intents/{id}/demo/advance，
  body = {"outcome":"revert_rest","decisionIds":["c_e0aa200b5cc0","c_49260409d82e"]}（N3）。
- J5：取消影响确认后，卡片正文回到服务器已保存内容「材料一（运行任务的依据）」（F1）。
- J1：关闭前本机记录为 null、重开后仍为 null，编辑入口恢复服务器稿且服务器稿未被误删。

## 3. E1

E1 after 侧 24 格（受影响场景 × 亮暗 × 4 尺寸）在 c8bde01 上已全部通过；本次改动只涉及
「带 meta 的本机草稿记录的清理判据」，不进入 E1 的四个场景路径（它们不留下未完成的卡片编辑），
因此 E1 结果仍然有效，**E1 未在 ccc651a 上重跑**（如实标注为未重跑）。

## 4. 未验证（不变）

- J5 的「等待期间独立移动保留」子断言：真实指针拖动在本装置未让卡片移动 → 未验证。
- R2/R5 与 N1/N4/N5 的浏览器级证据未采集（有 store/ASGI 级反例覆盖）。
- N6 的 file/code 附加字段：真实浏览器只覆盖 url 卡；file/code 有组件级反例覆盖。
