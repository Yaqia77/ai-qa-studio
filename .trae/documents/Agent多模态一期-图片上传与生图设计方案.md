# Agent 多模态一期 - 图片上传与生图兼容设计方案

> 状态：设计评审稿（未开发）。目标：接入的模型支持图片理解（Vision）或图片生成（Seedream/DALL·E 类）时，
> 平台按**能力矩阵配置驱动**自动启用/隐藏对应入口，不支持则显性降级，绝不静默吞图。

## 一、背景与现状

当前对话链路是纯文本契约，四个约束点：

| 层 | 现状 | 约束 |
|---|---|---|
| `qa-agent/agent/llm/base.py` | `ChatMessage.content: str` | OpenAI 兼容协议里 content 其实允许 parts 数组，这里收窄成了字符串 |
| `qa-agent/agent/llm/openai_compat.py` | `_payload` 只序列化 `{role, content}` | 无法携带 `image_url` 部件，也无法调 `/images/generations` |
| `qa-agent/agent/core/events.py` | 事件闭集无图片类型 | 生图结果没有承载事件 |
| `platform-web/src/views/AgentChatView.vue` | 附件用 `FileReader.readAsText` | 图片读成乱码文本，前端没有图片预览与渲染 |

联网搜索侧的实测结论（本方案附录 A 处理对齐方案）：`enable_search` 注入链路已验证正确，
但 sensenova 网关不识别通义系私有字段，开关对当前网关无实际效果——与"图片能力按网关声明"是同一类问题，
统一用**能力矩阵**解决。

## 二、设计原则

1. **配置声明能力，代码消费能力**：模型能不能看图/生图，写在配置里，不靠猜、不靠 try-catch 撞运气；
   未声明的模型按纯文本处理（图片入口置灰），行为可预期。
2. **不支持要说出来**：收到图片但 `vision=false` 时发 `error` 事件明确拒绝，而不是丢弃后答非所问。
3. **红线不动**：生图只产出展示资产（URL/文件），永远不参与用例落库链路；落库仍必经权威校验闸门。
4. **密钥与体积安全**：base64 图片不进会话历史 JSON、不进日志，只在单轮请求内存活；历史只存图片引用与 alt 摘要。

## 三、能力矩阵配置（核心）

`qa-agent/config/config.yaml` 新增：

```yaml
llm:
  chat_model: sensenova-6.8-flash
  capabilities:                      # 按模型名声明能力；缺省 = 全 false（纯文本）
    sensenova-6.8-flash: { vision: true,  image_gen: false }
    sensenova-6.8-pro:   { vision: true,  image_gen: true  }
  image_gen:                         # 生图端点配置（仅 image_gen=true 的模型用到）
    endpoint: /images/generations    # OpenAI 兼容出图端点；网关若走 chat 内嵌出图则填 chat_inline
    model: sensenova-image           # 出图模型名（留空用 chat_model）
    default_size: 1024x1024
    timeout: 120                     # 出图普遍比对话慢，独立超时
  upload:
    max_image_mb: 5                  # 单图上限
    max_images_per_turn: 4           # 单轮上限（与 doc_generate.MAX_ATTACHMENTS 分账）
    mime_whitelist: [image/png, image/jpeg, image/webp, image/gif]
```

`/health` 回带 `capabilities`（当前 default_model 与 models 列表各自的能力位），
前端据此渲染：能看图 → 输入卡片出现「图片」按钮；能生图 → 工具条出现「AI 生图」开关。
未知模型一律不显示入口（DeepSeek 官方网页版对不支持视觉的模型同样是直接禁用图片按钮，交互一致）。

## 四、协议改动

### 4.1 请求（chat/stream）

`attachments` 扩展支持图片项，与文档附件同数组、按字段区分：

```jsonc
{
  "message": "参考这张截图的布局生成用例",
  "attachments": [
    { "name": "openapi.yaml", "content": "…文本…" },
    { "name": "ref.png", "image_base64": "iVBOR…", "mime": "image/png" }
  ]
}
```

网关层校验：mime 白名单、单图 ≤5MB、单轮 ≤4 张，超限 400（复用现有 InvalidRequest 形状）。

### 4.2 消息组装（Vision 输入）

`vision=true` 时，本轮 user 消息 content 用 OpenAI parts 数组：

```json
{ "role": "user", "content": [
    { "type": "text", "text": "参考这张截图的布局生成用例" },
    { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBOR…" } }
] }
```

历史消息仍是纯字符串 content（协议允许混用），多轮上下文不带图（历史图已摘要为 alt 文本）。

### 4.3 事件（生图输出）

`EVENT_TYPES` 新增一个类型（同步 protocol.md 与前端联合类型）：

```
"image_ready"   # 生成就绪 {id, url, alt, prompt, model, size}
```

生图结果落 `qa-agent/data/gen_images/{id}.png`，`url` 回带静态路由 `/media/gen_images/{id}.png`
（qa-agent 挂 `StaticFiles`，仅暴露该目录）。`done.usage` 追加 `images: N` 计数。

### 4.4 意图路由

`route_intent` 增加 `image_generate` 意图：触发词「画/生成一张图/出图/生图 + 描述」，
或前端显式带 `gen_image: true`（工具条开关，显式优先，口径同现有 target 覆盖）。
chat 分支带图片附件且 `vision=false` → 直接 error 事件（code=`vision_unsupported`）。

## 五、代码改动点（按文件）

| 文件 | 改动 |
|---|---|
| `agent/llm/base.py` | `ChatMessage.content: str \| list[dict]`；新增 `image_gen(prompt, *, size, model) -> ImageResult` 抽象，基类默认 raise `ProviderError("该模型不支持生图")` |
| `agent/llm/openai_compat.py` | `_payload` 序列化 parts 数组；新增 `image_gen()`（POST `llm.image_gen.endpoint`，解析 `data[0].b64_json/url`，独立超时） |
| `agent/llm/resilience.py` | 包装 `image_gen`：整段非流式，按 transient 正常重试；复用同一熔断器 |
| `agent/config.py` + `config/config.yaml` | DEFAULTS 增加 capabilities/image_gen/upload 三节 |
| `agent/skills/image_generate.py`（新） | prompt 润色（可选一跳文本模型扩写）→ `provider.image_gen` → 落盘 data/gen_images → 返回 ImageResult；注册进 SkillRegistry |
| `agent/chat/orchestrator.py` | chat 分支：attachments 分流图片/文档；vision 组装 parts；新意图 `image_generate` 分派；不支持路径事件化 |
| `agent/gateway/chat.py` | 请求体 schema 加 `image_base64/mime`，校验白名单与体积；`/health` 回带 capabilities |
| `agent/main.py` | 挂 `/media/gen_images` 静态路由（只读、仅该目录） |
| `platform-web/src/types.ts` | `ChatEvent` 联合类型加 `image_ready`；attachments 类型加图片字段 |
| `platform-web/src/views/AgentChatView.vue` | 附件选择按扩展名分流 `readAsDataURL`；气泡渲染图片缩略图；新消息类型 `kind="image"` 渲染 `<img>` 卡片（可另存）；能力位控制「图片/生图」入口显隐 |
| `platform-web/src/api/index.ts` | `agentHealth` 返回类型加 capabilities |

前端图片读取注意：`dataURL` 前缀 `data:image/png;base64,` 要在提交前剥掉，
后端统一补回标准 `data:` URI（防 mime 伪造：以白名单校验的声明 mime 为准，不信任 URL 前缀）。

## 六、分期与验收

- **P1（Vision 输入）**：parts 组装 + 网关校验 + 前端贴图/预览 + 能力位显隐。
  验收：带截图问「这个页面有什么元素」，开/关 vision 的模型分别得到正确回答/明确拒绝。
- **P2（生图输出）**：image_gen + image_ready 事件 + 静态路由 + 渲染卡片。
  验收：「画一张登录页示意图」产出可查看、可另存的图片；网关故障走熔断降级为 error 事件。
- **P3（参考图编辑，暂缓）**：图生图（垫图）依赖网关是否支持 image input+output 同轮，观察 sensenova 文档再立项。

单测口径（离线，不碰真网关）：
- `_payload` parts 数组组装快照；`image_gen` 用 MockTransport 断言端点/超时/错误映射；
- 校验：mime 伪造、超限、vision=false 带图 → 对应错误码；
- orchestrator：`image_generate` 意图路由与不支持降级的事件序列。

## 附录 A：联网搜索与豆包/DeepSeek 对齐

实测（2026-11）：`web_search=true` 时请求体确认注入 `enable_search: true`（wire 层断言通过），
但 sensenova 网关回复仍无实时信息（A/B 对比：关=5.9s 拒答，开=11.8s 仍拒答）——该网关不支持通义系私有字段。

豆包/DeepSeek 网页版的"联网"是**服务端内建搜索基建**（模型网关自己完成检索→注入→生成），不靠客户端参数。
对齐路径二选一：

1. **换/配支持原生搜索的网关**：把 `llm.web_search_param` 改成该网关声明的字段（通义 `enable_search`、
   Kimi/智谱 `tools`、OpenAI `web_search_options`），零代码改动即生效；
2. **服务端检索兜底（RAG-lite）**：qa-agent 内置搜索适配器（Bing/SerpAPI/内网搜索），
   `web_search=true` 时先检索 top-N 结果注入 system prompt 再调模型——与知识库检索同一条注入通道，
   可作为二期立项（涉及外网检索密钥与配额，需单独安全评审）。

当前建议：前端「联网搜索」开关旁标注网关支持状态（`/health` 回带 `web_search_supported_by_gateway`），
不支持就置灰并注明原因，避免用户误以为开了就联网。
