# Agent 接入二期 — 对话式流式生成与步骤画布设计方案

> 前置：一期（M0–M4）已完成并双仓推送。本方案回答的问题：**交互形态从「表单页一次性生成」重构为
> 「对话聊天 + 流式输出 + 左侧步骤画布」，哪些资产复用、哪些重建、安全红线怎么保。**

## 一、目标形态（用户诉求拆解）

| # | 诉求 | 形态定义 |
|---|---|---|
| G1 | 对话式生成 | 用户在聊天框用自然语言描述意图（可多轮追问/修正），Agent 以**流式输出**（SSE）逐段回显思考步骤与产出 |
| G2 | 用例自动进用例管理 | 对话产出的用例**自动写入平台用例管理**（过闸门校验，非法不落库），对话流中同步出现「已落库」用例卡片，可点击跳转用例管理 |
| G3 | 左侧步骤画布 | 对话过程中左侧实时生成**可视化步骤节点**（序号 / 节点名 / 类型 / N 输入 M 输出 / 输入输出映射），用户可点击节点查看、**手工填写/修改操作步骤**，修改以新一轮对话回传给 Agent 更新草稿 |
| G4 | 文档一键生成 | 用户上传接口文档（OpenAPI/Swagger/Markdown 表格），Agent 解析出接口定义 → 直接批量生成用例进用例管理 |

参考截图形态（"公开课AI自动化演示"）：节点如 `获取登录账号(动态函数) → 用户登录(接口用例) → 查询当前用户信息(接口用例)`，
节点带「输入参数 / 输出映射」数据流（如 `手机号 ← phone_number`）。**画布本质是"场景执行计划"的可视化**——
与平台已有的「测试场景（plan）+ 依赖编排 + extract 变量传递」是同一语义，不新造概念。

## 二、现状资产盘点：复用 / 改造 / 重建

| 资产 | 位置 | 处置 |
|---|---|---|
| 检索/知识库（FTS5+RRF+五域切块+staging 评审） | qa-agent `agent/kb/` | **100% 复用**，对话编排层直接调 `assemble()` |
| 确定性校验闸门 `gate_case` | qa-agent `agent/skills/case_generate.py` | **复用**，从"整批跑一次"拆为"每条产出即跑"（流式友好） |
| 平台 adapter（登录/读/preview/commit） | qa-agent `adapters/interfaceauto/` | **复用 + 扩展**：新增 `api_def.write`（G4 落接口定义） |
| 平台权威校验/落库（excel_loader 同源 + 整批拒 + 审计） | interface-auto `/api/agent/cases/preview|commit` | **复用**，自动落库仍走 commit，不绕闸 |
| provider 抽象（fake 确定性 + openai_compat + 重试熔断） | qa-agent `agent/llm/` | **复用**；fake 增加"分步产出"剧本能力供对话单测 |
| RunContext 步骤留痕 `add_step` | qa-agent `agent/core/context.py` | **改造**：升级为事件发射器（EventSink），是 SSE 流的天然数据源 |
| evals / smoke / protocol | qa-agent | **扩展**：新增对话链路评估与事件契约测试 |
| 提示词与输出契约 | qa-agent `prompts/` | **扩展**：新增意图路由、计划生成、文档解析 prompt |
| 表单页 AgentGenView.vue | platform-web | **重建**为对话页（旧页下线，路由替换；一期沉淀的 api/types/agentHttp/代理全部沿用） |
| 网关鉴权/CORS/trace 留痕 | qa-agent `agent/gateway/` | **复用**；SSE 端点走同一套 X-Agent-Key 与错误形状 |

结论：**内核不动，重建的是"交互协议 + 编排层 + 前端页面"三件。**

## 三、总体架构

```
┌─ platform-web  /agent-chat 对话页 ──────────────────────────────┐
│  左：步骤画布 StepCanvas          右：聊天流 ChatStream          │
│   （节点卡片/数据流/点击编辑）      （SSE 渲染 + 用例/计划/文档卡片）│
└───────┬────────────────────────────┬────────────────────────────┘
        │ /api/*（平台登录态）        │ /agent/*（X-Agent-Key，SSE）
┌───────▼────────────┐      ┌────────▼────────────────────────────┐
│ interface-auto     │      │ qa-agent                            │
│  /api/agent/commit │◀─────│  /v1/chat/stream   SSE 对话端点（新）│
│  （权威校验+原子写）│ invoke│  /v1/chat/sessions 会话历史（新）    │
│  用例管理/场景页    │      │  chat/：session 存储 · 意图路由 ·    │
│  （现状不动）       │      │   orchestrator（ReAct 步骤循环，新） │
└────────────────────┘      │  skills/case_generate（复用，事件化）│
                            │  kb / llm / adapters（复用）        │
                            └─────────────────────────────────────┘
```

对话的所有状态（会话、轮次、草稿、计划）**归 qa-agent**；平台只认"校验过的用例写入"这一件事——
两侧职责同一期，不出现"平台也存一份对话"的双写。

## 四、对话协议（SSE 事件流）

`POST /v1/chat/stream`，请求 `{session_id?, message, attachments?, project_id, auto_register?, mode?}`，
响应 `text/event-stream`。事件（每条都带 `trace_id`，错误仍归一 `{detail, code, trace_id}`）：

| event | 载荷 | 前端渲染 |
|---|---|---|
| `token` | `{text}` | 聊天流增量文本（模型输出透传，fake 剧本同样可流） |
| `plan` | `{steps: [{seq, name, kind: func\|api_case\|assert, inputs, outputs}]}` | **左侧画布整体重建/更新**（G3 数据源） |
| `step_start` / `step_finish` | `{seq, ok, summary}` | 画布节点状态（进行中/完成/失败）+ 聊天流步骤行 |
| `case_ready` | `{case, validation, diff?, citations}` | 聊天流用例卡片（校验徽标/diffs 对照/溯源链接） |
| `registered` | `{case_ids, file, count}` | 卡片翻转为「已落库 ✓」+ 跳转用例管理链接（G2） |
| `ask` | `{question, options?}` | 追问卡片（缺信息时 Agent 反问，如"生成到哪个项目？"） |
| `citation` | `{kb_id, source_uri, title}` | 引用角标，点击回跳平台 |
| `error` / `done` | 统一错误形状 / `{usage}` | 红条 / 输入框解禁 |

**降级红线**：`degraded=true`（fake/离线模型）时 `registered` 事件**永不发出**——降级产出只展示不落库，
与一期"降级不可当正常结论"一致。

## 五、步骤画布模型（G3）

- 数据源 = `plan` 事件 + 每轮更新，节点结构对齐平台既有语义，**不发明新执行模型**：
  - `kind=api_case` ↔ 一条用例（method/url/params/body/asserts，输入=引用变量、输出=extract）；
  - `kind=func` ↔ 公共脚本（`core/scripts.py` 步骤链）；
  - 输入输出映射 ↔ 变量池 `{{var}}` + 依赖编排（`dependencies.json`），落库后可直接被场景执行。
- 交互：点击节点 → 右侧抽屉表单（同截图"节点配置"：基本信息/输入参数/输出映射）；
  手工修改提交后**不是直接改 JSON**，而是生成一轮结构化用户消息
  `{message: "修改步骤2", patch: {seq: 2, body: {...}}}` 回给 Agent → Agent 重跑该条闸门与 diff →
  画布与卡片同步更新（人改也过闸，口径唯一）。
- 一期 `diffs` 字段直接复用在节点详情（改了什么，原值→新值）。

## 六、自动落库与红线（G2）

`auto_register` 三态（会话级，缺省跟随平台配置）：

| 值 | 行为 |
|---|---|
| `ask`（一期现状） | 卡片上「确认落库」按钮，人点才 commit |
| `auto`（本期默认推荐） | 每条 `case_ready` 且**平台 preview 权威过检**后自动 `commit`（单条原子批），对话流出现 `registered` |
| `off` | 只出草稿不落库 |

不可动摇的红线（全部由平台侧 commit 保证，Agent 说了不算）：
1. 落库必经 `excel_loader` 权威校验，任一非法整批拒（现状复用）；
2. `authoritative=false`（平台离线）或 `degraded=true`（fake 模型）→ 自动模式**静默降级为 ask**，卡片显性标注原因；
3. 自动落库仍走 `agent_svc` + `case:write` + 审计留痕，commit 请求附 `meta:{origin:"chat", trace_id}`，
   用例管理页可按"AI 生成"筛选（Excel 无新列：以审计+trace_id 关联，避免动表结构）；
4. 编号冲突自动改名策略：`XXX-01` 占用时自动 `-02` 递增并在 diff 中声明，不静默覆盖。

落地补充（P5 实做时定死的契约）：commit 让号由平台 `_bump_id` 实现——保持尾段数字补零宽度
（`AL-01→AL-02`，99→100 允许超宽度），无数字尾段追加 `-2/-3…`，连续 50 次仍冲突拒 409；
同批内部互斥（前一条占的新号后一条不再命中）；映射经响应 `renamed:[{from,to}]` 回带，
编排器据此同步改写草稿/画布/`registered` 事件并在对话流声明让号；`meta` 原样回显，
审计页用 `agent_svc` + `"chat"` 可检索。在线门禁：`tools/smoke_chat.py`（19 项，fake 环境验红线
拦截 + agent_svc 直调验落库/让号/审计/清理全链路；真实模型环境自动切换 `registered` 断言）。

## 七、文档一键生成（G4）

链路：前端上传（OpenAPI JSON/YAML、Markdown 接口表）→ `POST /v1/chat/stream` 带 `attachments`（文本内容，
二进制由前端解析为文本）→ Agent 新 skill `doc_generate`：
1. 结构化解析（OpenAPI 走确定性解析器，非模型；自由格式文档才过模型抽取）→ 接口定义草稿；
2. 接口定义经平台**现有 OpenAPI 导入接口**入库（adapter 新增 `api_def.write` 能力，平台零新端点——
   复用 `POST /api/defs/import/openapi` 的校验与去重）；
3. 逐接口走 case_generate 六步链路生成用例 → 按第六节规则落库（单轮上限 10 个接口，
   超出部分提示下一轮继续；单接口失败事件化不阻断整批）；
4. 画布按"文档 → N 接口 → M 用例"树形展示进度，每个节点可点开。

落地补充（P4 实做时收紧的红线）：**降级模型转写的自由文档 spec 不直接登记接口库**
（否则等于借道模型往契约库灌未经真实推理的内容），只回提示引导接真实模型或传
OpenAPI/Swagger 原文；原文透传分支不受影响（确定性路径不经模型）。evals doc-05 锁此红线。

## 八、前端设计（重建）

- 新路由 `/agent-chat`（测试管理组，菜单「AI 生成」直接指向新页；旧 `AgentGenView.vue` 删除，
  其校验合并/降级告警/溯源逻辑作为新页组件参考移植）；权限仍 `case:write`。
- 页面骨架：左 40% StepCanvas（节点卡片列表 + 状态色 + 点击抽屉），右 60% ChatStream
  （消息气泡 + 卡片渲染器注册表：case/plan/ask/attachment 四类）；顶部项目选择器 + `auto_register` 开关。
- SSE 实现：**原生 fetch + ReadableStream + 手写行解析**（约 60 行工具函数，零新依赖；
  axios 不支持流，Vite `/agent` 代理对 SSE 需 `selfHandleResponse:false` 默认即透传，验证 buffer 关闭）。
- 会话历史：`GET /v1/chat/sessions` 列表 + 恢复；本地不做第二存储。
- 类型：`types.ts` 新增 `ChatEvent` 联合类型（与第四节表一一对应，protocol.md 同步收录）。

## 九、里程碑与准入门禁

| 里程碑 | 内容 | 门禁 |
|---|---|---|
| P1 | qa-agent 对话内核：session 存储、意图路由、orchestrator、SSE 端点、RunContext→EventSink 改造 | 单测（fake 剧本驱动全事件序列，事件顺序/错误形状断言）；一期 tests/evals 零回归 |
| P2 | 前端对话页：ChatStream + 卡片渲染 + SSE 工具 + 会话历史；替换旧页 | npm build；浏览器实测无控制台错误；降级不落库的 UI 表现有测试锁 |
| P3 | 步骤画布：plan 事件渲染、节点抽屉编辑、patch 回对话闭环 | 单测（画布数据与 plan 事件一致性）；浏览器实测 |
| P4 | 文档一键生成：doc_generate + api_def.write + 树形进度 | evals 增文档任务 5 条；smoke 增在线分支 |
| P5 | 自动落库模式 + 全链路收尾：auto/ask/off 三态、meta 审计、smoke/README/protocol 更新 | 在线全链路 smoke 含"对话→自动落库→用例管理可回读→清理"；双仓全绿提交 |

一期沉淀的 evals 基线（六维 100%）在整个二期过程中必须保持 100%——对话层是新增链路，不是替换生成质量。

## 十、待确认决策（开工前对齐）

1. **自动落库缺省强度**：`auto`（过检即落，推荐，符合"自动帮我生成到用例管理"）还是 `ask`（保守）？
2. **旧表单页处置**：直接删除替换为对话页（推荐，避免双入口维护），还是过渡期共存？
3. **画布节点粒度**：按第五节理解（节点=用例步骤，映射平台场景/依赖语义）是否正确？还是期望"生成过程步骤"（检索→生成→校验这类 pipeline 可视化）？
4. **会话归属**：对话历史存 qa-agent（推荐，平台零改动）是否接受？换浏览器/清 Agent data 目录会话即丢。
