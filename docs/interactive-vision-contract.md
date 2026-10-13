# 互动模式视觉能力（多模态图片交付）契约

2026-10-12 起生效。适用对象：`agent/interactive` 的材料解析与理解链路、`agent/adapters` 的消息形状、
`agent/services/app.py` 的凭据选择。与 `docs/interactive-mode-contract.md` §14.1 的「材料来源」一并读，
冲突时以本文为准（本文更具体）。

## 1. 一句话

图片能不能被 QIO「看懂」，取决于**这次真实用了哪条模型链路**：链路支持多模态，就把图片的**真实字节**
作为内容块交付，由模型自己看；链路不支持，就维持既有诚实结论（`image_unsupported`），绝不编造画面描述。

## 2. 四种事实（互斥，都要可读）

| 事实 | 材料条目 `source` | `read` | 触发条件 |
| --- | --- | --- | --- |
| 字节已交付给模型 | `image_delivered` | `true` | 适配器 `supports_images=true` 且附件 ready 且格式可交付且不超过字节上限 |
| 链路没有视觉能力 | `image_unsupported` | `false` | 适配器没有声明 `supports_images` |
| 有链路但读不出字节 | `image_unsupported` | `false` | 没有关联附件 / 附件取不到 / 状态不是 ready / 格式不在交付集合 / 超过上限 |
| 材料模块不可用 | `image_unsupported` | `false` | 上层 `honesty.materialsError` 已如实说明 |

每条 `image_unsupported` 的 `reason` 必须写清**为什么没交付**（哪一种），不允许只说「不支持」。

### 2.1 适用范围：两条链路同一张表（2026-10-13 起）

* **互动理解链路**（板面提交）：`resolve_images` → `build_context(vision=…)` → `image_parts_from_context`；
* **会话链路**（对话模式聊天与互动悬浮聊天都走它）：本轮附件 → 媒体候选
  （`collected_media_candidates`，归类用 `classify_media_kind(…, include_images=True)`）→
  `resolve_media` 的图片分支 → `ImagePart` 挂在本轮首条 user 消息上。

- **媒体类型一律按真实文件名判断**（`candidate["name"]`），展示标签（`_snippet` 只留 24 字符）只用于展示：
  否则长文件名被截断后扩展名丢失，图片会被判成「无法确认媒体类型」而拒绝交付（2026-10-13 线上实测）。
两条链路的能力位都只认**本次真实链路**的声明。会话链路**不**因为存在 `vision` 用途标签的凭据而切换链路
（凭据策略：主循环优先 `main-loop`，见 `credentials/policy.py`）；所以「附件已交付」与「这个模型能否看懂」
仍是两件事，后者不由本机保证。

## 3. 交付闸门（只有一处判断）

* `resolve_images(conn, *, state, cards, attachments)` 是**能力无关**的读取层：只回答「字节读得出来吗」；
* `build_context(..., vision=...)` 是**唯一闸门**：`vision=False` 时 `context["imageParts"]` 必为空列表，
  材料条目也必是 `image_unsupported`；
* `understanding.image_parts_from_context(context)` 只把 `imageParts` 转成适配器内容块（`ImagePart`），
  不重新做判断 —— 于是「这次交付了哪些图片」是可从落库上下文重建的事实。

## 4. 凭据选择

* 本次可能带图时（`_adapter_for_stage(..., images_hint=True)`）路由**优先**一条打 `vision` 用途标签的凭据
  （`AppContext.build_vision_adapter`）；没有就**如实回落**主循环凭据。
* 回落之后能不能交付图片，仍然由适配器的 `supports_images` 决定 —— 回落不会变成「假装看得见」。
* 凭据的停用 / 撤销 / 预算 / 验证状态照旧拦（与 `resolve_main_ref` 同一套 policy）。

## 5. 交付边界（不猜、不截断）

* 交付集合：`.png / .jpg / .jpeg / .gif / .webp`（`DELIVERABLE_IMAGE_TYPES`）；其余格式不按图片发。
* 单张上限 `MAX_IMAGE_BYTES`（8 MiB）：超过就**不交付**（不截断成半张图 —— 半张图会给出错的画面），
  并在 `reason` 里写出实际字节数与上限。
* 未勾选的注释、隐藏、已删除的卡片一律不交付（与材料解析同一套可见性过滤）。
* base64 只出现在内容块与落库上下文的 `imageParts` 里：**不进**送模型的 JSON 正文，也不进事件 / 日志。

## 6. 消息形状

* 没有图片的消息：`content` 仍是**字符串**（既有行为逐字不变，兼容只认字符串的端点）。
* 有图片的消息：`content` 变成数组 —— 文本块在前、`[图片] <标签>` 文本块与 `image_url` 块紧随其后；
  标签里带卡片 id，模型引用时不必猜。
* 自动修复（第二次尝试）**同样**带图：否则等于让模型在看不见图的状态下重出 JSON。

## 7. 测试（缺一条都算没做到）

`backend/tests/test_interactive_vision.py` 锁死：真实字节与 data URL 一致、无视觉时如实降级、
没有附件 / 取不到附件 / 超过上限各自的原因、不可见卡片不交付、纯文本形状不变、多模态形状正确、
上下文闸门（`vision=False` 时既无 `imageParts` 也无 `image_delivered`）、落库上下文可重建内容块。

---

## 8. 音频与视频（2026-10-12 扩展）

§1–§7 只讲图片。音频与视频沿用**同一套原则**：能力以本次真实链路为准、拿不到就如实降级、
不编造、不把「接口能接受」当成「模型能理解」。

### 8.1 适配器能力位（唯一事实来源）

`agent/adapters/base.py`：

| 能力位 | 含义 |
| --- | --- |
| `supports_images` | 图片字节可作为内容块交付（已有） |
| `supports_audio` | 音频字节可作为内容块交付（新增） |
| `supports_video` | 视频字节可作为内容块交付（新增；绝大多数链路为 False） |
| `audio_transcription` | 派生转写通道标识（如 `openai.audio.transcriptions`）；None = 没有（新增） |

### 8.2 材料条目的诚实事实（`source` 取值）

| source | `read` | 含义与必须附带的信息 |
| --- | --- | --- |
| `image_delivered` | true | 图片真实字节已交付（§3 已有） |
| `image_unsupported` | false | 链路没有视觉能力 / 取不到字节（`reason` 分四种，§2） |
| `audio_delivered` | true | 音频真实字节已交付 |
| `audio_transcribed` | true | 音频经派生转写得到文字：必须带 `transcript`、`range`（起止秒）与 `engine`（提供方 + 模型） |
| `audio_unsupported` | false | 既没有音频能力也没有转写通道：`reason` 说明是哪一种（含「本机没有可用转换程序」） |
| `video_delivered` | true | 视频真实字节已交付 |
| `video_sampled` | true | 视频按时间点抽样帧交付：`frames` 每项带 `at`（秒），`reason` 必须写明「抽样，不是逐帧」 |
| `video_unsupported` | false | 既没有视频能力也没有可用的抽样通道：`reason` 说明是哪一种 |
| `unreadable` | false | 这条材料**不是**音频 / 视频 / 图片（媒体通道不处理它），或走的是正文读取路径且读不出来：`reason` 必须说清是哪一种 |

每条都带：`read`、`reason`、`range`（音视频的起止秒，可为空）、`frames`（视频抽样才有）、
`engine`（派生处理实际用了哪个提供方与模型）、`attachmentId` 与 `version`。

### 8.3 不可越过的诚实边界

* 转写文本**只代表实际得到的文字**：不得据此声称理解了语气、音乐、说话人情绪或全部环境声音。
* 抽样帧**不代表**逐帧读取完整视频；材料与界面都要写明抽样点。
* 没有可用转换程序时，说明「这台机器上没有可用的转换程序，也没有随包提供」，
  **不要**退化成笼统的「不支持」。
* 未许可的文字卡、隐藏与删除的卡片**不通过任何派生结果绕过**范围判断。
* 「已交付模型」是**请求侧的事实**；「模型理解正确」是**另一个结论**，两者不混。
