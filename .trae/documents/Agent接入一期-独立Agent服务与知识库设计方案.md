# Agent 接入一期 · 独立 Agent 服务与知识库设计方案

> 状态：**待确认**（确认后再动手改造）。UI 自动化冻结在六期现状，本期不并行推进。

## 一、背景与目标

现有平台（interface-auto）已经具备「用例资产 + 执行引擎 + 报告观测 + 权限审计」的完整闭环，
但用例的**生产**仍完全依赖人工写 Excel：接口定义已经在 `data/api_defs.json` 里沉淀了方法/路径/断言建议，
同类接口的用例风格也高度可复用，这部分知识散落在文件里没被利用起来。

一期目标：引入一个**独立仓库的 Agent 服务**，把「接口契约 + 存量用例 + 平台规范」组织成知识库，
对外提供**用例生成与补全**能力，产出的用例必须通过平台校验闸门、可预览、可回跳来源，人工确认后才落库。

同时定下三条架构纪律，避免长成"平台的一个附属模块"：

1. **Agent 独立成仓**（`qa-agent`），不 import 任何 interface-auto 代码；平台差异全部收敛在 `adapters/`。
2. **平台无关协议**：interface-auto 只是第一个接入方，任何平台只要实现 3 个契约接口即可被同一 Agent 服务驱动。
3. **产出可信优先于产出聪明**：模型只负责"写内容"，合法性一律由确定性校验闸门把关，不靠模型自觉。

不在本期范围（列为后续候选）：失败诊断归因、Agent 作为 MCP server 对外暴露工具、流程画布与 Agent DSL 编排、
多轮对话记忆、UI/App 用例生成。

## 二、整体架构

```
┌─ 接入方（可多个，互不绑定）───────────────────────────────────────────────┐
│  interface-auto platform-web  │  外部 AI 平台（编排画布类）  │  CI/Jenkins  │
└───────────────┬───────────────────────────┬──────────────────────┬────────┘
                │  REST + SSE（X-Agent-Key，X-Trace-Id 透传）        │
┌───────────────▼───────────────────────────────────────────────────▼────────┐
│ qa-agent（独立仓库 · 独立进程 · 独立端口 8100）                             │
│                                                                            │
│  gateway/    鉴权(API Key) · 限流 · 审计 · 协议版本协商(X-Agent-Protocol)   │
│  core/       RunContext · ReAct 循环 · 上下文预算装配 · 结果归一            │
│  skills/     case_generate（一期唯一场景）· [二期] diagnose / mcp_server    │
│  kb/         ingest → chunk → index(SQLite FTS5 + 向量列) → retrieve       │
│  llm/        Provider 抽象：openai_compat（chat/embed/rerank）+ fake        │
│  tools/      Tool Registry（JSON Schema 声明式）+ 调用重试/超时/熔断         │
│  adapters/   interfaceauto/（一期落地）· template/（新平台接入模板）          │
│  trace/      运行留痕 · token 与成本 · 引用命中清单                          │
│  evals/      离线评估集（CI 门禁，防能力回退）                               │
└───────────────┬────────────────────────────────────────────────────────────┘
                │  Adapter 调平台原生 REST（服务账号 token）+ 语料导出接口
┌───────────────▼────────────────────────────────────────────────────────────┐
│ interface-auto backend                                                     │
│  既有：/api/auth/login  /api/defs  /api/cases  /api/runs  /api/ui-cases     │
│  新增（agent_bridge）：/api/agent/corpus（只读语料导出）                     │
│                       /api/agent/cases/preview + /commit（草稿落库）         │
└────────────────────────────────────────────────────────────────────────────┘
```

**职责边界**：Agent 不懂 Excel、不懂 pytest，它只产出「结构化用例记录」；
落盘格式、引用保护、权限校验全部留在平台侧（单一真源，避免两处校验漂移）。

## 三、跨平台接入协议（可复用的关键）

### 3.1 平台清单与三个契约接口

每个接入平台在 `qa-agent/config` 注册一份 manifest：

```yaml
platforms:
  interface-auto:
    base_url: http://127.0.0.1:8000
    auth: { type: password_login, login_path: /api/auth/login, token_header: "Authorization: Bearer" }
    account: { username: agent_svc }          # 服务账号，密码走 secret_vars，不入库
    capabilities: [api_def.read, case.read, case.write, run.read, corpus.read]
    corpus: { path: /api/agent/corpus, domains: [contract, case, execution, convention] }
```

新平台接入只需实现：

| 契约 | 作用 | interface-auto 一期实现方式 |
| --- | --- | --- |
| `capabilities` | 自述可用工具与入参 schema | adapter 内静态映射到既有 REST（无需平台改动） |
| `invoke` | 统一工具调用信封 | adapter 转译成原生 REST 调用 |
| `corpus` | 增量导出知识语料 | **新增** `/api/agent/corpus` 只读接口 |

### 3.2 鉴权与留痕

- **平台 → Agent**：`X-Agent-Key`（Agent 侧配置的 API Key）+ `X-Agent-Protocol: 1`；缺失/错误 401，协议主版本不匹配 426。
- **Agent → 平台**：用服务账号调 `/api/auth/login` 换 Bearer token，adapter 内缓存，401 自动重登一次；
  权限仍由平台 `require_perm` 判定 —— **Agent 能做什么，取决于给它配了哪个角色，不取决于 Agent 自己声明**。
  一期建议只给只读 + `case:write`，不给 `env:manage` / 用户管理类权限。
- **链路追踪**：请求入口生成 `trace_id`，`X-Trace-Id` 双向透传；Agent 侧 `trace/` 落一份、平台 `audit_logs.jsonl` 落一份，
  排障时按同一 id 对齐。
- **降级**：平台不可达 / LLM 不可达时，接口明确返回 503 + `reason`，绝不静默返回空用例。

## 四、知识库设计

### 4.1 五个语料域

| 域 | 内容 | 来源 | 同步方式 |
| --- | --- | --- | --- |
| D1 契约域 | 接口定义（方法/路径/参数/断言建议/OpenAPI 原文） | `data/api_defs.json`、`/api/defs` | 自动增量（hash 比对） |
| D2 用例域 | 存量用例的语义化描述（步骤+断言+变量引用） | `data/*.xlsx`、`/api/cases` | 自动增量 |
| D3 执行域 | 历史批次结果、失败堆栈首帧、flaky 标注 | `reports/*/allure-results` | 自动增量（批次结束触发） |
| D4 业务域 | 需求说明、术语表、业务流程 | 人工上传 md/docx | 人工提交 + 评审 |
| D5 规范域 | 断言写法、`{{var}}` 变量语法、脚本 API、平台约定 | 人工维护 md（front-matter） | 人工提交 + 评审 |

一期**只强制要求 D1 + D2 + D5**（生成用例的三块必需上下文），D3/D4 结构预留、灌入即可用。

### 4.2 统一记录形状

```python
{
  "kb_id": "kb_7f3a...", "domain": "contract", "platform": "interface-auto",
  "project_id": "proj_89ca592e", "source_type": "api_def",
  "source_uri": "http://127.0.0.1:8000/defs?name=获取相册列表",   # 必须可回跳原文
  "title": "获取相册列表", "text": "...", "tags": ["相册", "查询"],
  "version": 3, "hash": "sha1:...", "acl": ["tester", "agent_svc"],
  "updated_at": "2026-11-02T10:00:00", "embedding": [..] | None
}
```

### 4.3 切块规则（按语义单元，不按固定字数）

- **D1**：一个接口一块，正文含方法/路径/参数/推荐断言，前缀拼 `【模块/接口名】`。
- **D2**：一条用例一块，正文含用例描述 + 请求要点 + 断言语义化中文描述（复用「断言配置中文语义化」约定）。
- **D3**：一次失败一块，只保留错误首帧 + 关键栈 + 断言失败项，超长截断（避免日志噪声淹没检索）。
- **D4/D5**：300–500 字滑窗 + **标题路径前缀**（contextual chunking），保留 `doc > h2 > h3` 层级。
- 块大小目标 300–800 token，超限强制在语义边界断开。

### 4.4 存储与检索

- **底座**：`qa-agent/data/kb.sqlite`（标准库 sqlite3，零外部中间件）
  - `doc_meta`（元数据 + acl + hash）/ `chunk`（正文、token 数、域）/ `chunk_fts`（FTS5 虚表）
  - `vec`（chunk_id, dim, blob —— float32 序列化）/ `sync_state`（增量水位）/ `feedback`（采纳与人工改写 diff）
  - 检索为 numpy 读回内存暴力算余弦（万级块 < 20ms，够用）；**Repository 抽象隔离存储**，二期可换 Qdrant/pgvector 而不动上层。
- **检索链路**：query 规则改写（补模块名/接口名同义词） → 向量 top20 + FTS top20 → **RRF 融合** →
  rerank（provider 可配，缺省关闭则退化为 RRF + 域权重 contract>case>convention） → top5 + 分数阈值截断。
- **元数据前置过滤**：`platform / project_id / domain / acl` 先过滤再算相似度，杜绝跨项目串味。
- **强制溯源**：进入 prompt 的每一块都登记 `kb_id + source_uri`，响应里以 `citations` 返回；
  生成结果若某字段无对应 citation，校验闸门直接判失败。

### 4.5 更新与治理

- **增量同步**：按 `hash` 比对，只重算变化块；由 Agent 侧定时器 + 平台批次结束回调双触发。
- **写库闭环**：Agent 产出的新知识（含被采纳的生成用例）一律先进 `kb_staging`，**人工评审通过才入正式库** ——
  防止模型幻觉沉淀成"知识"再被后续检索放大。
- **敏感信息**：语料入库前过掩码（token/password/手机号等，沿用平台侧敏感信息掩码规范）；
  `secret_vars.xlsx` 的真实值**永不进知识库**，只索引变量名。
- **反馈回流**：`feedback` 记录「采纳 / 驳回 / 人工改写 diff」，既是 eval 集来源，也是 few-shot 的最优风格样本
  —— 人工改过并落库的真实用例，下一轮生成时被 D2 检索命中，形成正向循环。

## 五、一期核心链路：用例生成与补全

### 5.1 三种模式

| mode | 输入 | 输出 |
| --- | --- | --- |
| `generate` | 接口名 / 模块 / OpenAPI 片段 | 该接口的正例 + 边界 + 异常用例集（P0/P1/P2 分层） |
| `complete` | 一条已有用例（缺断言/缺参数/缺提取） | 补全后的同一用例 + 逐项 diff 说明 |
| `expand` | 一条已有用例 + "再扩 N 条" | 同风格同模块的变体用例（参数等价类扩展） |

### 5.2 执行流程

```
POST /v1/generations
  { intent, platform, project_id, target, mode, count?, dry_run=true }
 → ① 目标解析：走 adapter 取接口定义（存在性由平台判定，不信任模型给的接口名）
 → ② 上下文装配：D1 契约块(必需) + D2 同类用例块(风格对齐, 取 3–5) + D5 规范块(断言/变量语法, 必需)
     按优先级裁剪到 token 预算内，装配清单写进 trace
 → ③ LLM 生成：JSON Schema 强约束输出（列名直接对齐 Excel：case_id/api_name/module/case_desc/
     priority/run/method/url/headers/params/body/extract/asserts）
 → ④ 校验闸门（确定性，不过则带错误信息回炉 repair 一次，仍不过则降级"仅建议不落库"）：
     a. 结构合法（JSON Schema）
     b. method ∈ VALID_METHODS、断言 op ∈ ASSERT_OPS 且 need_expected 项有期望值
     c. {{var}} 引用的变量存在于变量池或 secret 变量名清单
     d. case_id 与目标文件不冲突；api_name 在接口库有定义（或明确标记为新接口）
     e. 每条用例 citations 非空
 → ⑤ 预览：返回逐条用例 + diff + 校验报告 + 引用列表，前端渲染对照表
 → ⑥ 人工确认 → 平台 POST /api/agent/cases/commit → 走既有 excel_writer（先 dry_run 校验再落盘）
```

**关键取舍**：② 的上下文装配和 ④ 的校验闸门是本方案的价值核心。
模型可以换、知识库底座可以换，但"装配有预算、产出必过闸"这两条不动。

### 5.3 平台侧改动（尽量薄）

| 文件 | 改动 |
| --- | --- |
| `backend/agent_bridge/corpus.py` | 新增：把 `api_defs.json` / 用例 Excel / 规范 md 导出为 D1/D2 语料 JSON（只读、分页、带 hash 供增量） |
| `backend/agent_bridge/router.py` | 新增：`GET /api/agent/corpus`、`POST /api/agent/cases/preview`、`POST /api/agent/cases/commit`（后两者要求 `case:write`） |
| `backend/shared/authz.py` | 免认证白名单不动；服务账号角色补一个 `agent_svc`（只读 + `case:write`） |
| `config/config.yaml` | 新增 `agent:` 段（`base_url` / `api_key` / `enabled` / `timeout`），缺省 `enabled: false` 保证零影响 |
| `platform-web/src/views/AgentGenView.vue` | 新增：意图输入 → 目标选择 → 生成 → 预览对照表 → 确认落库；导航加「AI 生成」入口 |
| `unit_tests/test_agent_bridge.py` | 新增：语料导出形状、preview/commit 权限与校验、`enabled=false` 时端点不注册或返回 404 |
| `tools/smoke_agent.py` | 新增：端到端冒烟（本地 Agent + fake provider → 生成 → 校验 → 落库 → 复查用例可读） |

`core/` 执行引擎与 `backend/api/excel_writer.py` 的校验口径**不复制一份到 Agent**，
而是通过 preview 接口回调平台侧同一套 `excel_loader` 校验函数 —— 单一真源，杜绝漂移。

## 六、分支与仓库计划

| 仓库/分支 | 内容 |
| --- | --- |
| **新仓库 `qa-agent`**（GitHub 私有，main） | Agent 服务全部代码；Python 3.10 + FastAPI + httpx + pydantic + sqlite3 |
| `interface-auto` 分支 `feat/agent-integration`（从 `feat/ui-automation` 切出） | 仅平台侧适配层 + 前端页 + 配置段 + 冒烟脚本；UI 自动化代码不再改动 |

`qa-agent` 目录骨架：

```
qa-agent/
├── agent/{main.py, gateway/, core/, skills/, kb/, llm/, tools/, trace/, schemas.py}
├── adapters/{interfaceauto/, template/}
├── prompts/            # 版本化提示词（含 few-shot 模板），prompt hash 进 trace
├── config/config.yaml
├── tests/              # 单测（fake provider，全程离线可跑）
├── evals/              # 固定评估集 + 打分器
├── tools/smoke_*.py
└── docs/protocol.md    # 3.1 契约接口规范，供其他平台照此接入
```

## 七、测试与提交门禁（全绿才推 GitHub）

**qa-agent 侧**
1. `pytest tests` 全绿：gateway 鉴权/限流、provider 抽象（fake + 超时/重试）、chunk 规则、
   SQLite 混合检索（构造语料验证 RRF 命中预期块）、校验闸门每条规则各有正反例、adapter 用 httpx MockTransport 打桩。
2. `pytest evals` —— 固定 20 条生成任务：fake provider 下断言 schema 通过率 100%（确定性回归）；
   真 provider 下只报告通过率、不阻断 CI（模型输出非确定性，不能当硬门禁）。
3. `python -m agent.main` 起服务，`/health` 与 `/openapi.json` 正常，协议版本号可见。

**interface-auto 侧**
4. `pytest unit_tests` 全绿（不破存量），且 `enabled: false` 时平台行为与改动前完全一致。
5. `npm run build`（platform-web）成功。
6. 重启后端：`/openapi.json` 无 500、新端点出现在 schema、既有 `/api/defs` `/api/cases` 无回归。
7. `tools/smoke_agent.py` 全绿：本地起 Agent（fake provider）→ 生成 → 预览 → 确认落库 → 用例可被 pytest 收集执行。
8. 前端页面无控制台报错。
9. **两侧全绿后向用户确认，再分别提交推送**（`qa-agent` main + `interface-auto` `feat/agent-integration`）。

**环境注意（Windows 内网）**：所有依赖走国内镜像；模型不可达时显式降级 fake 并在响应里标记
`degraded: true`，禁止把降级结果当正常结果展示。

## 八、里程碑

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| M0 | 决策确认 + `qa-agent` 仓库脚手架 + 分支建立 | 0.5d |
| M1 | 协议与骨架：gateway / provider 抽象 / adapter 契约 / trace | 1.5d |
| M2 | 知识库：ingest → chunk → SQLite(FTS5+向量) → 混合检索 + 单测 | 2d |
| M3 | 用例生成 skill：上下文装配 + Schema 输出 + 校验闸门 + preview/commit | 2.5d |
| M4 | 前端接入 + 冒烟 + evals + `docs/protocol.md` | 1.5d |

## 九、验收标准

- 给定一个已有接口定义，能生成一组通过校验闸门的用例草稿，每条都能点开看到引用了哪些知识来源。
- 生成的草稿在预览页能看到与存量用例同风格的 diff，确认后才写入 Excel，且能被现有 pytest 流程正常收集执行。
- Agent 服务不依赖 interface-auto 代码即可独立启动；换一个平台接入只需新增 `adapters/<平台>/` + 一份 manifest。
- 平台侧 `agent.enabled: false` 时，全量存量测试与页面行为零回归。

## 十、非目标（后续期候选）

- 失败诊断归因（依赖 D3 执行域，二期首选）。
- Agent 作为 **MCP server** 对外暴露平台工具（编排画布类平台接入的前置）。
- 流程画布与 Agent DSL 编排、节点级输入输出映射。
- 多轮对话记忆、跨会话用户偏好。
- 向量库升级为 Qdrant/pgvector、rerank 模型上线。
- UI/App 用例生成、自动修复存量用例、用例质量评分看板。
