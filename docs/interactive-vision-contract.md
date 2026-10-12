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