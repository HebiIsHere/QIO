# QIO 联网搜索工具设计文档

日期：2026-09-08

状态：设计评审稿（待用户确认后进入实现计划）

## 1. 背景与目标

qio 是自托管的编码型 agent，目前具备记忆检索（`memory_search`）、话题切换、工具开发等能力，但**没有联网搜索**。本设计为 qio 增加「联网搜索 + 读取网页正文」两项能力，让它能查询实时外网信息，作为回答依据。

### 目标

- 新增一个可插拔的搜索提供方抽象层，默认免密钥可用（优先国内可达），用户可填一个密钥后自动升级。
- 新增两个原生工具：`web_search`（搜索）和 `web_fetch`（读取网页正文）。
- 新增一个极简的前端搜索配置卡片，只存「默认实例 URL + 可选密钥」，不占用现有的 LLM 凭据页。
- 严格区分「真的没有结果」和「搜索出错/被限流」，agent 绝不编造。

### 非目标（本轮不做）

- **不做浏览器控制**（Playwright、会话化连续浏览、表单/登录）。已单独排期下一轮。
- 不碰「需登录/会员/付费墙」内容；不绕过反爬；不把认证 cookie/session 发到外站。
- 不做境外提供方（Tavily / Brave / Serper / Exa 等）的集成，仅预留扩展点。

## 2. 范围（方案甲）

本轮只做模块 1–4：

1. 搜索提供方抽象层 `SearchProvider`（可插拔）。
2. `WebSearchTool`（搜索工具）。
3. `WebFetchTool`（读正文工具）。
4. 前端搜索配置卡片 + 后端 `/api/settings/search` 接口。

浏览器控制（模块 5）单独排期。

## 3. 架构与组件

### 3.1 搜索提供方抽象层（`agent/services/search.py`）

统一接口，输入 `query + top_k`，输出结构化结果：

```python
@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str

@dataclass
class SearchOutcome:
    hits: list[SearchHit]
    status: str                 # "found" | "empty" | "error"
    error: str | None = None
```

提供三个实现（全部为「免密钥 + 国内可达」优先，实测可达性见 §5）：

- **`BingProvider`（免密钥，排在用户自带通道之后）**：请求 `https://www.bing.com/search`（带 `mkt=zh-CN`），解析 `h2 > a` 结果（不要再取 `li` 里的第一个 `a`：那是站点引用锚点）；HTML 解析不到时用 `&format=rss` 再兜底一次。
  **2026-09-12 修订**：`cn.bing.com/search` 现在会 302 到 bing 首页（14KB、0 个结果容器），旧写法会得到「HTTP 200 + 0 结果」的假象；另外必应在匿名/无 Cookie 时可能返回「标题匹配 query、内容却是热门站点」的诱饵页，必须用「结果与 query 有没有词交集」做相关性闸门，判定无关时按通道异常上报而不是当搜索结果。
- **`BaiduProvider`（免密钥，最后兜底）**：请求百度搜索结果页。实测常年「安全验证页」（反爬）；识别到反爬后该通道会**冷却 10 分钟**，避免每次搜索都去撞同一堵墙（用户可见症状就是「agent 反复试百度、每次被拒」）。
- **`BochaProvider`（可选，需密钥，国内最稳）**：接博查 AI，返回结构化结果。密钥来自 `search.bocha_api_key`。
- **`SearxngProvider`（可选，需用户自建实例）**：指向用户自建的 SearXNG 实例 URL（`search.searxng_url`）。公共实例实测不可达，故不作为默认。

选择逻辑（`SearchProviderResolver`，按「稳定可达」优先级）：

1. 若配置了 `bocha_api_key` → 用博查（最稳、结果规范）。
2. 否则 → 依次尝试 用户配置的 `SearxngProvider`（如有，仅自建实例）→
   **免密钥通道** `ExaMcpProvider` → `ParallelMcpProvider` → `DuckDuckGoProvider`
   → `BingProvider` → `BaiduProvider`（最后兜底 + 反爬冷却）。

**2026-09-12 修订（实测）**：免密钥搜索改用两条已实测可用的路线 —— Exa / Parallel 的
**免费 MCP**（`https://mcp.exa.ai/mcp` 的 `web_search_exa`、`https://search.parallel.ai/mcp`
的 `web_search`，无需 API Key，返回结构化结果）与 **DuckDuckGo HTML 端点**
（`html.duckduckgo.com/html`，会限流 → 挑战页按通道异常处理）。公共 SearXNG 实例
全部被 Anubis/Substation 拦截，所以 SearXNG 只支持自建实例；必应会返回与查询无关的
诱饵页、百度常年安全验证，故降级为最后兜底。免密钥通道可用设置项
`search.keyless_fallback`（默认开）整体关闭。
3. `SearxngProvider` 仅在用户显式配置了自建实例 URL 时参与。
4. **DuckDuckGo 不参与默认兜底**：实测在大陆不可达（连接超时），仅对境外/已翻墙环境有意义，本轮不实现。

会话内新建一个 `SearchService`，通过 `ServiceRegistry` 注入给工具。

### 3.2 工具：`WebSearchTool`（`agent/tools/web_search.py`）

- 继承 `Tool`，`name = "web_search"`。
- 参数：`query`（必填，字符串）、`top_k`（整数，默认 5，上限 20）。
- 行为：
  - 调用 `SearchService.search(query, top_k)`。
  - 返回「标题 + 链接 + 摘要」Top N 的文本列表。
  - 三态处理：
    - `found`：正常列出结果。
    - `empty`：明确返回「未找到相关结果」，禁止编造。
    - `error`：明确返回「当前无法联网搜索（错误原因）」，禁止编造。
- `is_concurrency_safe = True`（只读检索，可与其他并发工具并行）。
- 设请求超时（默认约 10s）与条数上限。
- 加入 `tool_router` 的 `CORE_TOOLS`（搜索是通用能力，常驻可见），其余工具按相似度路由。

### 3.3 工具：`WebFetchTool`（`agent/tools/web_fetch.py`）

- 继承 `Tool`，`name = "web_fetch"`。
- 参数：`url`（必填）、`max_chars`（整数，默认 15000，上限 40000）。
- 行为：
  - 用 `httpx` 静态抓取 URL（不启用浏览器渲染）。
  - 用 `trafilatura` 或 `bs4` 提取正文，转纯文本。
  - 单次字符预算；超长页面首尾各取样（如各取 `max_chars//2`）。
  - 遵守 robots.txt：抓取前查询目标 robots，被禁止则停止。
  - 不发送认证 cookie/session。
  - 遇到登录墙 / CAPTCHA / 非 HTML 内容时，返回「该页面无法读取正文」。
- 失败时不编造正文内容。

### 3.4 配置存储（`SettingsStore` + API）

- 用现有的 `SettingsStore`（SQLite key-value）新增键：
  - `search.searxng_url`：可选的（用户自建）SearXNG 实例 URL。
  - `search.bocha_api_key`：可选的博查 API key。
  - `search.top_k_default`：默认返回条数。
  - `search.max_fetch_chars`：读正文默认字符预算。
- 后端新增接口：
  - `GET /api/settings/search`：返回当前搜索配置（不含明文 key，只回 `has_key` 布尔）。
  - `PUT /api/settings/search`：保存配置；key 允许为空表示清除。
- **不占用 LLM 凭据表/凭据页**：这些 key 不是模型密钥，与 `Credentials` 表隔离。

### 3.5 前端搜索配置卡片

- 在 `SettingsView.vue` 的 `pref`（偏好）tab 内新增一张「联网搜索」卡片。
- 字段：
  - 默认返回条数（数字，QNumber）。
  - 读正文字符预算（数字，QNumber）。
  - 博查 API key（可选，密码输入，仅显示已配置状态，不回显明文）。
  - SearXNG 实例 URL（可选，用户自建时填写）。
- 遵循 qio 设计风格：颜色统一 `var(--*)`、三声部字体（标题/话题用 `--serif`，正文用 `--sans`，密钥/预算/ID 用 `--mono`）、卡片样式参照 `CredentialCard` / 现有 `pref` 卡片、选中态用 `--accent` + `--accent-soft`。

## 4. 数据流

```
用户提问（含查询意图）
    │
    ▼
主 agent 决定调用 web_search(query, top_k)
    │
    ▼
ToolRegistry.execute → WebSearchTool.run
    │
    ▼
SearchService.search → SearchProviderResolver
    ├─ 有 bocha key ────► BochaProvider ──► [title,url,snippet]「found」
    └─ 无 bocha key ────► 依次尝试 BingProvider → BaiduProvider（互兜底）
                              │
                              ├─ 命中 ──► [title,url,snippet]「found」
                              ├─ 无结果 ──► status="empty"
                              └─ 全部不可达/非200 ──► status="error"
    （可选）已配置自建 SearXNG ——► SearxngProvider 加入候选
    │
    ▼
结果回传给主 agent（依 status 三态呈现）

主 agent 可选调用 web_fetch(url, max_chars)
    ▼
httpx 抓取 → trafilatura/bs4 提取正文 → 转纯文本 → 截断
    ▼
正文回传给主 agent
```

## 5. 错误处理

| 场景 | 表现 |
|---|---|
| 必应、百度全部不可达 / 非 200 | `status="error"`，agent 收到「当前无法联网搜索」，不编造 |
| 搜索无结果 | `status="empty"`，agent 收到「未找到相关结果」 |
| 博查 key 无效 / 配额用尽 | 视为 `error`，并提示检查密钥 |
| 百度返回安全验证页 | 视为该源失败，自动换下一源（必应），不当作真实结果 |
| 可选 SearXNG 实例不可达 | 视为 `error`（仅当用户配置了该实例） |
| `web_fetch` 抓取失败 / 超时 | 返回「该页面无法读取正文」 |
| 目标被 robots 禁止 | 停止抓取，返回「该页面不允许抓取」 |
| 遇到登录墙 / CAPTCHA | 停止，返回「该页面需要登录或验证」 |
| 目标是非 HTML（PDF/图片等） | 返回「非网页内容，无法提取正文」 |

## 6. 测试策略

### 后端（pytest）

- `test_search_providers.py`：`BingProvider` / `BaiduProvider` / `BochaProvider` / `SearxngProvider` 的解析、三态映射；`SearchProviderResolver` 的优先级（有 key 走博查；无 key 必应→百度互兜底；百度验证页识别并换源）。
- `test_web_search_tool.py`：`WebSearchTool` 的 `found` / `empty` / `error` 三态处理、参数校验（`top_k` 上限）。
- `test_web_fetch_tool.py`：`WebFetchTool` 抓取、正文提取、字符预算截断、robots 尊重、失败时返回错误而非编造。
- `test_settings_search_api.py`：`GET / PUT /api/settings/search` 的读写、key 不回显、清空 key。

### 前端（vitest + vue-tsc）

- 更新/新增 `SettingsView` 相关测试：搜索配置卡片渲染、读写、保存后的 notice。
- `vue-tsc --noEmit` 通过。

## 7. 设计风格约定（必须遵守）

- 后端：遵循现有 `Tool` 基类、`ServiceRegistry` 注入、`ToolRegistry.register` 挂载模式。
- 前端：颜色一律 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值。
- 三声部字体：标题/话题=衬线 `--serif`；正文=无衬线 `--sans`；数据/密钥/预算/ID=等宽 `--mono`。
- 弹窗/卡片风格参照 `ApprovalModal.vue`、`CredentialCard.vue`。
- 选中/高亮用 `--accent` + `--accent-soft`。

## 8. 已确认的决策（2026-09-08 用户拍板）

1. **稳定可达优先**，来源取舍以实测可达性为准。
2. **默认来源：必应（第一）+ 百度（第二）**，两者互相兜底（必应挂→用百度，百度挂→用必应）。
3. **DuckDuckGo 移出默认兜底**：实测大陆不可达（连接超时）；本轮不实现。
4. **博查（可配 key）：可选增强**，配置后优先使用，最稳。
5. **SearXNG：仅作为用户自建实例的可选项**；公共实例实测不可达，不作为默认。
6. `web_search` 进入 `CORE_TOOLS`（常驻可见）。
7. 方案甲：本轮只做模块 1–4，浏览器控制单独排期。

用户已确认以上，进入 `writing-plans` 生成实现计划。
