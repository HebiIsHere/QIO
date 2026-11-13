# PM-QIO032 E2E 实测证据（Lead 汇总；权威明细见同目录 README.md / summary.json）

> 权威结论以 `README.md` 的「逐场景结论 / 产品缺陷 / 未验项」与 `summary.json`（**38/40 PASS**）为准；
> `summary-s1.json` / `summary-s4.json` 为单场景复跑台账，`mergedRuns` 记录 runId。
> 上轮口径更正：①-6b 曾把「显示但不参与」的 TXT 当交付对象，属验收脚本自相矛盾；参与 TXT 的
> 正文实际以 source=attachment_text 进入首轮请求，参与 PNG 以 image_url 交付且字节一致（①-6c）。

## 最终合并台账：38 / 40 PASS（2 项 UNVERIFIED：②-2 / ②-3，非产品结论）
| 场景 | 结果 | 说明 |
| --- | --- | --- |
| ① 首轮交付 / 三态 / 不泄漏 | **14/14** | 正常入口、文字卡正文、参与 TXT 文件正文（source=attachment_text 逐字）、参与 PNG（image_url data URL，base64 与本地字节一致）、三态与恢复显示；排除/隐藏/未提交材料均不泄漏 |
| ② A/B 三路 | 1/3（②-1 PASS） | 提交 A→改 B 后板面显示 B（②-1 PASS）；②-2/②-3 绑定追问轮未执行脚本化 board_read_cards（toolRequests=0），**UNVERIFIED 不作产品结论**；读文件一路未取证 |
| ③ 重复提交/延迟回执/迟到事件/草稿清理 | **6/6** | 双击只执行一次；未改动再提交 submissions 不增、界面「已提交过」并保留原受理记录（#80）；延迟回执；迟到事件不倒退；保存成功但草稿清理失败保留原文 |
| ④ 800×600 / 1120-1121 / #05 压力 | **9/9** | 单开/组合/Esc、边界两侧中心与命中；800×600 六项改动时详情面板 h=237（钳制/内部滚动）、overlapSubmit=false、任务入口真实点击可打开 |
| ⑤ 六件拖放/首件失败/路由往返 | **3/3** | 路由往返；六件拖放只接收 5 + 第 6 件未接收并「重新导入」补卡成功；首件失败可见原因+重试成功、后续不陪葬（[模拟] abort） |
| ⑥ 主题/Tab/Esc/中文输入法 | **5/5** | 净白/暗紫晶、连续 Tab、聊天焦点 Esc、IME 组合态 Esc、中文落字 |

## 未验项（UNVERIFIED，不宣称通过）
- ②-2/②-3 A/B 工具读取链：fake provider 脚本化工具调用未被执行，原因未定位；后端冻结快照/材料单测通过。
- ② 读文件一路（read_attachment）未纳入 A/B 取证。
- ⑤ 旧卡迁移（需旧 schema 板面 fixture）、隐藏成员、撤销连线（画布手势未纳入 CDP 驱动）。
- Windows 原生 Tauri 拖放（dropped_path）：浏览器层只覆盖字节拖放。

## 模拟标注
③-4 延迟回执（CDP Fetch 延迟）、③-5 重复/迟到事件（/api/events/test）、③-6 草稿清理失败（Storage.removeItem 抛错）、⑤-3 首件失败（CDP Fetch abort）、⑥-4 输入法组合态（CompositionEvent）。均在 summary.json 的 simulated 字段列明。

## 注意
- 并发执行 run.ps1 会互杀 8734/5199/8807 端口并导致场景作废；README 已明确警告，勿并发跑。
- 本轮 evidence 目录中 `rerun-20261113-1307/` 为并发事故作废产物，不用于结论。
