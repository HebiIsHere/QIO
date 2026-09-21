# QIO「全部 UI」穷举截图

目标：把 QIO 界面上**所有可达状态**逐个截下来，形成可索引的 UI 目录。
不是「每个页面一张图」——QIO 的界面大多是状态驱动的（审批、流式、排队、星球面板…），
穷举的最小单位是「状态」，不是「路由」。

产物（全部已被 .gitignore 忽略，不进仓库）：

- `frontend/e2e-shots/ui-catalog/<group>/<id>.png` —— 逐状态截图
- `frontend/e2e-shots/ui-catalog/manifest-<group>.json` —— 元数据（标题 / 主题 / 视口 / 文件 / URL）
- 汇总：`contact-*.png`（带标签的接触表）、`gallery.html`（可点的图册）、`INDEX.md`（索引表）

## 为什么每个并行任务各起一套实例

后端 SSE 事件总线是**全局广播**（`backend/src/agent/api/bus.py`）：`POST /api/events/test`
注入的事件会送给所有订阅者。两个采集脚本共用一套服务时，A 注入的审批会出现在 B 的页面里。
所以并行任务各起一套「后端 + 前端 + 独立数据目录」。

```powershell
# 起（数据目录 %TEMP%\qio-ui-catalog\<name>，并播种基线测试数据）
python scripts/ui-catalog/instance.py up --name settings --backend-port 8834 --frontend-port 6199 --seed --clean
# 采集（lib.mjs 读 QIO_BASE / QIO_API）
$env:QIO_BASE="http://127.0.0.1:6199"; $env:QIO_API="http://127.0.0.1:8834"
node scripts/ui-catalog/settings.mjs
# 收
python scripts/ui-catalog/instance.py down --name settings
```

主实例（`scripts/e2e_up.py`：后端 8734 / 前端 5199）留给需要「已播种数据 + 已有 trace」的采集。

## 采集库速查（`scripts/ui-catalog/lib.mjs`）

> 对话页这一组**不要**在主实例上采：它要注入审批 / 工具 / 通知事件，而后端事件总线是全局广播
> （`backend/src/agent/api/bus.py`），注入的审批会直接弹到**用户正在用的那个窗口**上 ——
> 2026-09-20 的 phase4 验收脚本真的弹过一次，弹窗盖住整页让后续点击全部失效。
> 「首次打开（空数据目录）」也要单独一套实例（`empty`：8838 / 6203），否则拍到的是「有历史」的页面。

```js
import { createSession, launchBrowser, runGroup } from "./lib.mjs";

await runGroup(async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: "conv", name: "对话页", theme: "dark" });
  await s.goto("#/", { waitFor: ".conversation" });      // 或 { path: "/ui-catalog.html" }
  await s.shot("conv-01-empty", "空状态");                 // 记一条元数据 + 存图
  await s.shotEl(".composer", "conv-composer", "输入区");   // 局部元素截图
  await s.inject("TURN_START", { turn_id: "t1", revision: 1 });
  await s.close();                                        // 自动写 manifest-<group>.json
  await browser.close();
});
```

- `theme`：`dark` | `light`（靠 `addInitScript` 预置 localStorage，否则无头浏览器默认浅色）
- `viewport`：`DEFAULT_VIEWPORT`(1440×900) / `NARROW_VIEWPORT`(820×900) / `TINY_VIEWPORT`(620×860)
- 其它账号级状态：`motion: "reduced"`、`developerMode: true`、`storage: { ... }`
- `inject(type, data)`：后端测试注入口（只有开发模式注册）。事件词汇见 `backend/src/agent/api/events.py`
- 断言「截图非空」：`shot()` 自己会检查文件大小，空图直接抛错

## 分组与端口分配

| 分组 | 内容 | 实例 | 端口（后端/前端） |
| --- | --- | --- | --- |
| `conv` | 对话页、消息流、审批、队列、通知条、输入区 | **隔离 `conv`** | 8835 / 6200 |
| `conv-root` | 对话页穷举（root 这一组） | 隔离 `convroot` + 空实例 `empty` | 8839 / 6204、8838 / 6203 |
| `settings` | 设置页 7 分区、凭据、确认层、调试页 | 隔离 `settings` | 8834 / 6199 |
| `planet` | 星球入口球、全屏星球、话题/知识/实体面板 | 隔离 `planet` | 8836 / 6201 |
| `atoms` | 原子控件画廊（多页入口，不进产品构建） | 隔离 `atoms` | 8837 / 6202 |

## 状态清单（穷举底稿）

清单来自源码枚举（`views/*.vue`、`components/**`、`stores/{session,events,ui}.ts`、
`api/events.py` 的事件词汇），不是凭印象列的。每一条都要有对应截图。

### conv（对话页）

基础与阅读：

- [ ] 空状态（无消息）：问候语 + 引导条 + 星空入口球
- [ ] 正常多轮对话（含长回复 / 表格 / 宽代码块 / 普通宽度代码块）
- [ ] 长表格在窄窗口下横向滚动（不撑破气泡）
- [ ] 宽代码块自身的横向滚动 + 复制按钮
- [ ] 上翻阅读：停止跟随 + 「回到最新消息」+ 未读计数
- [ ] 轮次分隔头 hover（索引从淡到显形）
- [ ] 消息 hover 出现复制按钮
- [ ] 键盘 skip-link 聚焦态
- [ ] 开发者模式：每条消息 token 明细 / 诊断信息

Markdown 渲染全覆盖（往 ASSISTANT 事件里灌一段含全元素的正文）：

- [ ] 标题 / 无序列表 / 有序列表 / 任务列表 / 引用 / 行内代码 / 链接 / 分隔线 / 表格 / 代码块 / 强调

运行态（`session.activity` / `turnPhase`）：

- [ ] 等待中：三圆点 + 「正在处理」
- [ ] 正在生成：打字机流式正文 + 「过程」标记
- [ ] 正在使用工具：「正在使用工具」+ 运行中的工具卡
- [ ] 工具卡成功（折叠）/ 失败（err 色标）/ 展开（参数 + 耗时 + 完整输出）
- [ ] 独立任务：running / done / failed（三种）
- [ ] 等待你确认（activity=approval）
- [ ] 正在整理独立任务的结果（notify）
- [ ] 排队：QueueChip 运行中 + 多条排队 + 已取消
- [ ] 继续条：迭代/输出预算耗尽（`kind=continue`）

通知条（`ConversationView` 的 notice 家族）：

- [ ] 错误条（ERROR）+ 前往设置 / 查看详情
- [ ] 警告条（WARNING）
- [ ] 已停止条（TURN_END status=cancelled）
- [ ] 模型不可用（TURN_END status=unavailable）
- [ ] 兼容模式条（FALLBACK）
- [ ] 凭据状态提示：unavailable / paused / revoked（三种）
- [ ] 历史读取失败 + 重试

交互卡：

- [ ] 话题切换提示（TOPIC_SWITCH_SUGGESTED）：正常 / busy
- [ ] 「从「XXX」继续」锚点提示（ANCHOR historic=true）
- [ ] 知识候选卡（KNOWLEDGE_CANDIDATE → TURN_END 后出现）：默认 / 保存中 / 成功 / 失败
- [ ] 工具创建卡（TOOL_CREATE_STATUS 各 phase：提案/构建/测试/等待确认/注册/已创建；含失败）

审批（`ApprovalModal` / `ApprovalEntry`）：

- [ ] `tool_create`：高风险（批准按钮降调）
- [ ] `tool_create`：低风险（批准按钮主操作色）+ 只读语义
- [ ] `credential_grant`
- [ ] `high_impact_knowledge`
- [ ] 高级详情展开态
- [ ] 审批失败态（弹窗保留 + 「未做出任何授权」）
- [ ] 审批入口条（用户正在输入时不抢焦点，只亮入口）

浮动组件与主题视口：

- [ ] 设置悬浮入口（SettingsFloat）默认 / 贴边隐藏 / hover 展开
- [ ] 星球入口球（PlanetDock）默认 / 贴边 / 拖动后位置
- [ ] 全部主状态 × dark / light
- [ ] 窄窗口（820×620）布局：输入区整宽贴底、为球让出通道

### settings（设置页 + 调试页）

- [ ] 7 个分区各一张：外观 / 对话与记忆 / 模型与联网 / 工具与权限 / 数据与维护 / 凭据 / 高级（dark + light）
- [ ] 分区反馈四态：保存中（info）/ 成功（ok）/ 失败（err）/ 警告（warn）
- [ ] 测试凭据 toast：成功 / 失败
- [ ] 外观：主题三选一选中态、动画三选一、输出速度 QSelect 关闭/打开
- [ ] 对话与记忆：预设分档、自定义 QNumber 出现、迭代上限/预算回填
- [ ] 表单校验失败文案（自定义轮数越界、搜索条数越界、字符预算越界）
- [ ] 模型与联网：免密钥开/关、可用性文案「可用 / 无可用通道」、博查 Key 未配置/已配置/替换编辑态、清除按钮
- [ ] 工具与权限：工作区根目录、权限模式 QSelect 打开
- [ ] 数据与维护：离线维护关/开、间隔 QNumber、立即运行
- [ ] 窗口行为：两个贴边隐藏开关、还原默认布局反馈
- [ ] 高级：开发者模式开关、SearXNG 折叠（收起/展开）、当前生效参数表
- [ ] 凭据：4 张卡（生效中 / 预算 90% 警告 / 已停用 / 已撤销）
- [ ] 凭据筛选：全部 / 已启用 / 已停用 / 已撤销 / 已过期 + 空结果文案
- [ ] QConfirm layer：删除凭据、撤销凭据（两份确认文案）
- [ ] CredentialModal：create / meta / rotate 三种模式 + 校验错误
- [ ] 设置页窄窗口单列
- [ ] 调试页：无 trace / 有 trace 列表（done、failed、cancelled 徽章）/ 选中详情（各块折叠展开）/ 记录开关 / 分页

### planet（星球）

- [ ] 入口球：常驻态、贴边隐藏淡出、hover 展开
- [ ] 打开连续体逐帧：铺底 → 长大 → 场景接管 → 浮层进入（用 `?planetdemo=slow` 慢放取帧）
- [ ] 全屏 overview / 聚焦当前话题 / 选中话题常驻标签 / hover 标签
- [ ] 面板收起态 / 展开态、管理模式
- [ ] 话题 tab：列表、搜索过滤、无结果、选中项高亮
- [ ] 详情：有摘要 / 无摘要 / 关键词 chips / 事实行
- [ ] 片段历史：开放中 / 已封块 / 选中态
- [ ] 查看原文：加载中 / 列表 / 分页继续读 / 读取失败 / 已读完
- [ ] 底部操作区：未选片段（从最新位置继续）/ 已选片段 / 起点失败错误
- [ ] 知识 tab：各状态（active / pending_review / draft / revoked）、编辑态、归档 QConfirm inline、成功/失败反馈
- [ ] 实体 tab：列表 / 详情 / 归档确认 / 空
- [ ] 首次加载中 / 加载失败 + 重试 / WebGL 不可用降级
- [ ] 开发者模式 HUD（fps / 视角）
- [ ] 收起中（closing）与 Esc 关闭
- [ ] 窄窗口（面板覆盖布局）
- [ ] dark / light

### atoms（原子组件画廊）

多页入口：`frontend/ui-catalog.html` + `frontend/ui-catalog/main.ts`（**不在 `src/` 内**，
因此不进 `vue-tsc` 的类型检查范围、也不进产品构建产物的入口；设计语言守卫测试只扫 `src/`）。

- [ ] QInput：默认 / 占位 / 聚焦 / error / mono / disabled / password
- [ ] QSelect：关闭 / 打开 / 高亮项 / 已选中 / 禁用 / 空选项
- [ ] QNumber：常规 / 带单位 / 空值 / 禁用 / 上限夹取 / 聚焦
- [ ] QSlider：0 / 中值 / 满值 / 禁用
- [ ] QConfirm：inline（normal / danger）、popover（normal / danger）、layer（normal / danger）

## 每张图的验收标准

1. 状态是**真的**造出来的（不是近似）：文案、色标、按钮层级都对得上；
2. 主题正确（dark 图不能是浅色）、视口符合清单；
3. 图不空、不糊、关键文案完整可读；
4. manifest 里标题是中文、能让人一眼知道这是什么状态；
5. 造不出来的状态要**如实报**（写清楚为什么），不允许用别的状态冒充。

## 汇总：把各分组的清单合成一份目录

各分组各自写 `manifest-<group>.json`（多路并采同一分组时还会带后缀，
例如 `manifest-conv-root.json`），所以最后要合并一次，并做一遍体检：

```powershell
node scripts/ui-catalog/aggregate.mjs   # → manifest-merged.json / INDEX.md / gallery.html / contact-plan.json
python scripts/ui-catalog/contact.py    # → contact-<group>-NN.png（带标签的接触表，逐页扫一遍）
```

- `gallery.html`：可点开大图、可按分组筛选、可按标题/id/备注搜索的图册（离线、无外部依赖）；
- `INDEX.md`：一张表一个分组，列出 id / 中文标题 / 主题 / 视口 / 图 / 来源清单；
- `contact-*.png`：接触表，用来一眼发现空图、暗色拍到浅色、状态对不上；
- **体检**：清单里有但磁盘上没有的、磁盘上有但没进任何清单的（「未登记」）、
  小于 1KB 的疑似空图 —— 全部写进 `INDEX.md` 的体检小节，并给未登记图补一条
  「未登记」记录收进图册。目录的价值在于「穷举可信」，宁可显式报出来，也不要看起来完整实际漏了。

### 为什么 manifest 必须按 id 合并

一个分组会开多个采集会话（不同主题 / 视口 / 实例），每个会话收工时各写一次清单。
`lib.mjs::saveManifest` 因此按 `id` 合并（同 id 视为重拍、原地替换），
而不是整文件覆盖 —— 否则先跑完的那几个会话的条目会被后写的一份悄悄抹掉
（2026-09-21 两路并采 `conv` 时真实发生过一次）。
