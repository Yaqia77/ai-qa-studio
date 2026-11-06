# ai-qa-studio · AI 驱动的多端自动化测试用例生成与执行平台

面向**接口 / Web / APP** 三端的自动化测试平台：既能手工维护用例、批量执行并出报告，
也内置一个独立的 AI Agent（对话式生成），把「接口文档 / 一句话需求 / 页面流程描述」直接转成
过闸门校验的候选用例，人在环确认后落库。数据均以 Excel/JSON 文件存储，无需数据库。

- **接口自动化**：Excel 驱动，命令行（v1）单机可跑，用例写在表里、程序读取执行生成报告
- **Web（UI）自动化**：项目分型 `ui`，可视化步骤 + Playwright 解释器驱动浏览器
- **APP 自动化**：项目分型 `app`（`.apjson` 载体），平台已具用例存储与执行；**AI 生成为规划中**
- **AI 生成（qa-agent）**：独立服务 + 独立仓，对话式 SSE 生成接口/Web 用例，落库前必经平台权威校验

## 架构总览

- **双仓结构**：平台主仓（`backend/` FastAPI + `platform-web/` Vue3 + `core/` 引擎）与
  `qa-agent/` Agent 服务仓（挂载于主仓 `./qa-agent`，各自独立 `.venv` 与 git 历史）；
  主仓后端按 `config.yaml` 的 `agent.auto_start` 自动带起 Agent 服务（默认端口 8100）。
- **校验闸门单一真源**：Agent 只产出候选草稿并回调平台校验，**权威判据始终在平台侧**——
  接口走 `core/excel_loader`、UI 走 `backend/ui/ui_case_store`、APP 走 `backend/app/app_case_store`；
  Agent 侧只做结构/引用/编号/变量的确定性预检，落库红线（降级/非权威/未过闸任中其一绝不自动落）不因「自动」松动。
- **三类型路由**：Agent 的 `case_kind` 支持 `auto/api/ui/app` 四态——显式选择 > 对话关键词提示 >
  项目元数据 > 接口缺省逐级判定；命中尚未装配的 `app` 生成轨时**明确降级反问**，绝不静默按接口生成。
- **项目作用域为过滤器**：AI 助手顶栏项目选择默认「全部项目（不限）」，落库目标按
  对话点名的项目 > 顶部选择 > 会话已记 > 反问 逐级解析；对话声明与顶部选择冲突时**以对话意图为准**并显性提示。

## 技术栈

- **核心引擎**：Python 3.10+ · requests · openpyxl · pytest · jsonpath-ng · pytest-html / allure-pytest · PyYAML · loguru
- **平台后端**：FastAPI · uvicorn · python-multipart（文件上传） · croniter（定时任务 Cron 解析）；内存 token 会话 + RBAC，PBKDF2 密码哈希，数据全部落 JSON/Excel 文件
- **平台前端**：Vue 3 · Element Plus · Vite · TypeScript · vue-router（HTML5 history），`platform-web/` 独立子项目

## 目录结构

```
interface-auto/
├── config/config.yaml        # 环境配置（域名/超时/默认请求头/报告开关）
├── data/api_cases.xlsx       # 接口用例 Excel（用例/全局变量/填写说明 3 个 Sheet；即平台「默认项目」）
├── data/cases/               # 平台新建的业务项目用例文件（每项目一个文件：接口 .xlsx / UI .json / APP .apjson，按需生成）
├── data/projects.json        # 用例项目注册表（平台「用例管理」页维护，已 gitignore）
├── data/scripts.json         # 公共脚本库（平台「公共脚本」页维护，已 gitignore）
├── data/api_defs.json        # 接口定义库（平台「接口定义」页维护，已 gitignore）
├── data/dependencies.json    # 用例依赖关系（平台「依赖管理」页维护，已 gitignore）
├── data/users.json           # 用户账号（PBKDF2 哈希，已 gitignore）
├── core/                     # 核心引擎（纯逻辑，CLI 与 Web 平台共用）
│   ├── excel_loader.py       # Excel 解析 + 格式校验（报错精确定位到行列）
│   ├── context.py            # 变量池（提取值 > 全局变量表 > YAML 配置）
│   ├── variable.py           # {{变量}} 占位符替换引擎
│   ├── http_client.py        # requests 封装（超时、报文快照）
│   ├── extractor.py          # JSONPath 变量提取
│   ├── asserts.py            # 断言引擎（10 种操作符）
│   ├── scripts.py            # 公共脚本引擎（声明式步骤链的定义/校验/执行）
│   ├── models.py             # 数据模型
│   └── runner.py             # 单用例执行编排（含前后置脚本钩子）
├── tests/                    # pytest 参数化入口（保持 Excel 行顺序 + 依赖拓扑排序与阻塞）
│   └── conftest.py           # 依赖展开（传递闭包）+ 拓扑排序 + 依赖失败阻塞不发请求
├── backend/                  # 平台后端（FastAPI，仅文件存储，无数据库）
│   ├── main.py               # 应用入口 + 全局鉴权中间件 + 审计中间件
│   ├── authz.py              # token 会话（滑动过期）+ RBAC + 登录失败限流
│   ├── user_store.py         # 用户与密码哈希（PBKDF2-HMAC-SHA256 + 随机盐）
│   ├── perm_store.py         # 权限矩阵（内置基线 + 自定义覆盖，mtime 热加载）
│   ├── case_importer.py      # 智能导入（表头三级识别/缺项补默认/预期结果→断言）
│   ├── case_template.py      # 导出用例模板（带填写说明与示例）+ 空项目脚手架
│   ├── project_store.py      # 用例项目注册表（项目 = 一个 Excel 文件，默认项原地指向存量）
│   ├── excel_writer.py       # 用例写入 Excel（保存/导入落盘）
│   ├── api_def_store.py      # 接口定义库
│   ├── openapi_import.py     # OpenAPI/Swagger 文档解析导入
│   ├── script_store.py       # 公共脚本库
│   ├── dependency_store.py   # 用例依赖关系库
│   ├── plan_store.py         # 测试场景库
│   ├── config_writer.py      # 环境配置读写
│   ├── secrets_writer.py     # 敏感凭证文件读写
│   ├── scheduler.py          # 定时任务调度器（croniter）
│   ├── scheduler_store.py    # 定时任务存储
│   ├── notifier.py           # 执行结果通知（Webhook）
│   ├── notification_store.py # Webhook 配置库
│   ├── allure_store.py       # 报告归档与脱敏（mask）
│   ├── audit_store.py        # 操作审计（脱敏落盘 + 超量裁剪）
│   ├── schemas.py            # Pydantic 请求/响应模型
│   └── routers/              # 各业务接口（auth/projects/cases/api_defs/scripts/dependencies/plans/runs/schedules/secrets/notifications/audit）
├── platform-web/             # 平台前端（Vue3 + Element Plus + Vite，npm 子项目）
│   └── src/views/            # 17 个页面：登录/仪表盘/用例/接口定义/公共脚本/依赖/场景/执行/报告/环境/凭证/通知/定时/统计/审计/用户/角色
├── qa-agent/                 # 独立 AI Agent 服务仓（另仓挂载，自带 .venv）：意图路由/知识库检索/生成 skill/校验闸门适配
├── tools/                    # 脚手架与冒烟脚本
│   ├── gen_template.py       # 生成/重置 Excel 用例模板
│   ├── gen_secret.py         # 生成敏感凭证模板
│   ├── smoke_authz.py        # 权限全链路冒烟（含写接口越权试探）
│   ├── smoke_full.py         # 全功能端到端冒烟（项目/用例/导入/依赖/定时/执行主链路）
│   ├── smoke_chat.py         # 对话式全链路在线冒烟（需平台+qa-agent 双服务，见「AI 生成」一节）
│   ├── smoke_scripts.py      # 公共脚本冒烟
│   └── smoke_api_defs.py     # 接口定义冒烟
├── main.py                   # CLI 入口
├── reports/                  # 每次运行一个时间戳目录：report.html + allure-results/
└── logs/                     # 运行日志（含完整请求/响应报文，保留最近 10 份）
```

## 快速开始

### 1. 安装依赖

```bash
# 建议使用虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

> **UI 自动化用例（可选）**：若要用 UI 项目（可视化步骤，Playwright 驱动浏览器），
> 装完依赖后还需下载浏览器内核（首次一次即可）：
>
> ```bash
> # Windows
> .venv\Scripts\python.exe -m playwright install chromium
> # macOS / Linux
> .venv/bin/python -m playwright install chromium
> ```

### 2. 生成用例模板（首次）

```bash
python tools/gen_template.py
```

生成 `data/api_cases.xlsx`，内置 9 条演示用例（基于公开 API httpbin.org 与
jsonplaceholder.typicode.com，开箱即跑）。

### 3. 运行

```bash
python main.py
```

预期结果：`8 passed, 1 skipped`，并在 `reports/run_<时间戳>/` 下生成报告。

### 4. 查看报告

- **HTML 报告**：`reports/run_<时间戳>/report.html`，单文件，双击即可打开，
  每条用例含完整请求/响应报文
- **Allure 报告**（可选，需本机安装 [Allure 命令行](https://allure.apache.org/)）：

```bash
allure serve reports/run_<时间戳>/allure-results
```

## 用例编写指南

Excel 共 3 个 Sheet：**用例**（核心）、**全局变量**、**填写说明**（Excel 内置帮助，可随时查阅）。

### 用例表字段

| 字段 | 必填 | 说明 |
|---|---|---|
| 用例编号 | 是 | 全局唯一，如 `login_001` |
| 模块 | 是 | 模块名，用于报告分组与 `--module` 筛选 |
| 接口名称 | 是 | 接口的业务名称（与「接口定义」库同名即算引用，可一键带入推荐配置） |
| 用例描述 | 是 | 一句话描述验证点 |
| 优先级 | 是 | P0 / P1 / P2 / P3 |
| 是否执行 | 是 | Y 执行，N 跳过 |
| 请求方式 | 是 | GET / POST / PUT / DELETE / PATCH |
| 请求路径 | 是 | `http` 开头的完整 URL 直接使用；相对路径拼接 config.yaml 的 `base_url`；支持 `{{变量}}` |
| 请求头 | 否 | JSON 对象，与环境默认请求头合并（用例优先） |
| 查询参数 | 否 | JSON 对象，URL 查询参数 |
| 请求体 | 否 | JSON 对象/数组，或纯文本 |
| 变量提取 | 否 | `{"变量名":"$.data.token"}`，从响应体按 JSONPath 提取 |
| 断言 | 是 | JSON 数组，见下方断言语法 |
| 超时(秒) | 否 | 正数，缺省用 config.yaml 的 timeout |
| 前置脚本 | 否 | 逗号分隔的公共脚本名，在主请求之前按序执行 |
| 后置脚本 | 否 | 逗号分隔的公共脚本名，无论主流程成败都在最后执行 |

### 断言语法

格式：`[["操作符", "取值路径", "期望值"], ...]`，多条断言全部通过用例才算通过。

**取值路径**：

- `status_code` — 响应状态码
- `response_text` — 响应原文
- `$.xxx` — 响应体 JSONPath（`$` 为整个 body）
- `var.xxx` — 变量池取值（如 `var.token`），用于不依赖响应的校验（前后置脚本里常用）

**操作符**：

| 操作符 | 说明 | 示例 |
|---|---|---|
| eq / neq | 相等 / 不等（字符串与数字智能比较） | `["eq","$.code",0]` |
| contains | 字符串子串包含 / 列表成员包含 | `["contains","$.message","成功"]` |
| gt / ge / lt / le | 数值比较 | `["gt","$.total",0]` |
| regex | 正则匹配 | `["regex","$.phone","^1\d{10}$"]` |
| not_null | 非空（可省略期望值） | `["not_null","$.data.id"]` |
| len_eq | 长度相等（列表条数/字符串长度） | `["len_eq","$",10]` |

**期望值支持 `{{变量}}`**：如 `["eq","$.id","{{post_id}}"]`，可实现"查询结果与上一步创建的 ID 一致"类断言。

### 变量机制

- **写法**：`{{变量名}}`，可用于 请求路径 / 请求头 / 查询参数 / 请求体 / 断言期望值
- **优先级**：运行时变量提取 > 凭证文件 secret_vars.xlsx > 「全局变量」表 > config.yaml（提供 `base_url`）
- **类型保留**：JSON 列中占位符写在引号内（如 `{"id":"{{post_id}}"}`），
  当整个值恰好是一个占位符时，替换后保留原始类型（数字/布尔）
- **依赖顺序**：用例严格按行号顺序串行执行，后面的用例可使用前面用例提取的变量
  （如先登录提 token，再访问鉴权接口）
- **筛选提醒**：按 `--module/--priority/--keyword` 筛选时，被依赖的用例
  （如提取 token 的登录用例）需同时被选中，否则会报「变量未定义」

### 典型场景：登录态串联

| 用例编号 | 请求方式 | 请求路径 | 变量提取 | 断言 |
|---|---|---|---|---|
| login_001 | POST | /api/login | `{"token":"$.data.token"}` | `[["eq","status_code",200]]` |
| get_profile_001 | GET | /api/profile | | `[["eq","status_code",200]]` |

get_profile_001 的请求头写：`{"Authorization":"Bearer {{token}}"}`

### 前后置公共脚本（登录/造数/清理复用）

多个用例都要先登录、都要造数、都要收尾清理时，把这些重复动作沉淀成**公共脚本**，
用例只在「前置脚本 / 后置脚本」列写脚本名（逗号分隔）即可引用。

脚本是**声明式步骤链**，只有四种步骤动作（不执行任意代码，避开 RCE 与审计盲区）：

| 动作 | 可用字段 | 用途 |
|---|---|---|
| `request` | `method` `url` `headers` `params` `body` `extract` `assert` `timeout` | 发一次请求，可提取变量、可带断言 |
| `sleep` | `seconds` | 等待异步任务落库（单步上限 30 秒） |
| `set_var` | `vars` | 写变量池，值支持 `{{变量}}` 占位符 |
| `assert` | `assert` | 断言上一条响应，或 `var.xxx` 变量池 |

执行语义：

- **前置**任一步失败 → 用例直接判失败，**主请求不会发出**（避免脏数据打到被测系统）
- **后置**在 `finally` 里执行，**主流程成败都会跑**（负责回收造出的数据）；后置失败同样判用例失败并提示「数据可能未清理」
- 步骤写入的变量对该用例后续全部可用（与用例自身的 `变量提取` 同一个变量池）
- 每条步骤的执行情况以「脚本轨迹」附件进报告，报告中可逐步回溯；变量名命中
  `token/password/secret/key/auth/cookie` 时轨迹只留掩码，**敏感值不落盘**

约束：单脚本最多 20 步；脚本名不能含逗号/空格/斜杠等（Excel 以逗号分隔引用）；
定义与执行共用一套校验（`core/scripts.py`），存得下就跑得通。

CLI 侧同样生效：脚本库存 `data/scripts.json`（路径可在 config.yaml 的 `scripts_file` 改），
不引用脚本的存量用例文件即使没有这两列也完全不受影响。

### 接口定义管理（接口库沉淀 / OpenAPI 导入 / 用例骨架）

同一个接口的请求头、参数、断言散落在几十行用例里重复抄写，改一次要改一遍。
**接口库**（`data/api_defs.json`）把这些收敛成一份权威定义，只承担三件事：
**沉淀**（同名接口合并成一条定义）、**建议**（新用例选中接口名即带入推荐配置）、
**骨架**（由定义生成用例行）。

> 定位：接口库是**元数据**，不是执行体 —— 用例始终完整自带 方法/路径/断言，
> 删掉一条定义不会让任何批次执行失败，因此它可以放心地边用边补。

三种来源（定义上的 `source` 字段记录血缘，后续编辑不篡改）：

| 来源 | 入口 | 取值规则 |
|---|---|---|
| 手工录入 | 「接口定义」页 → 新建接口 | 名称/模块/方法/路径 + 请求头/参数/请求体/变量提取/断言建议 |
| Excel 聚合 | 「接口定义」页 → 从用例聚合 | 按「接口名称」分组投票：方法/路径/模块取众数，请求头/参数按 key 取最常见值，断言取 top4（同一路径的 `eq` 只保留最常见的期望值，避免 `0000` 与 `01000002` 打架） |
| OpenAPI 导入 | 「接口定义」页 → 导入文档 | 整份 OpenAPI 3.x / Swagger 2.0 原文（JSON 或 YAML，≤4MB），`{id}` 自动转 `{{id}}` |

接口库只提取文档**真给了 example / default / enum** 的参数与请求体样例，其余一律不填；
断言建议只给状态码 `eq` 与结构字段（`code/data/success`）`not_null`，**不猜业务错误码**；
`multipart` 不猜 Content-Type（交给环境默认请求头）；响应里出现 token 类字段时给出变量提取建议。

引用与约束（与公共脚本库同一口径）：

- **引用锚点 = 接口名称**：用例的「接口名称」列与定义同名即算引用，列表直接显示覆盖清单；
  被引用的定义**不可改名、不可删除**（409 并列出用例编号），改名等于把覆盖统计与断言建议一起掉断
- **接口名称不能含 `/ \ ? #`**：定义按名称直接寻址（`/api/defs/{名称}`），这类名字建出来就访问不回来。
  手工录入由后端拒收，OpenAPI 导入自动清洗成 `-`，Excel 聚合时**只跳过该分组**并点名涉及的用例，
  一个脏名字不会拖垮整批聚合
- **校验同源**：断言建议走 `excel_loader.check_assert_rules`（与读用例同一套规则），
  页面表单常量由 `/api/defs/meta` 下发，不会出现「库里推荐、用例不认」
- **生成用例骨架**：默认 `dry_run` 只回预览行，确认后再落 Excel（编号撞车返回 409 且不动文件）。
  这一步是**往用例文件写数据**，因此要的是 `case:write` 而不是 `api:manage`——权限跟着副作用走
- **用例页联动**：「接口名称」改为下拉建议（候选 = 存量用例名 ∪ 接口库），选中已登记接口可一键
  「带入推荐配置」覆写抽屉表单（仍需点保存才写回 Excel）；接口库读不到时名称仍可自由输入，
  用例保存逻辑完全不依赖它

### 用例编写提效（模板导出 / 智能导入 / 依赖编排）

面向人工评审用例（一级模块/用例编号/优先级/预置条件/用例名称/操作步骤/预期结果…）到平台用例的转换，降低编写成本：

- **导出模板**：「用例管理」页可下载标准用例 Excel 模板（含填写说明与示例行），团队按模板填好再导入
- **智能导入（预览确认后落盘）**：上传格式不匹配的用例表，后端 `case_importer` 做三级表头识别
  （标准列 → 归一化列名 → 同义词映射），自动把人工列转成平台用例字段：
  - **缺项补默认**：未填的方法/优先级/是否执行等按默认值补齐（默认 `GET`、`P2`、执行）
  - **预期结果→断言**：语义映射成断言规则（如“返回200”→`eq status_code 200`），并可从接口定义库带入推荐断言
  - **先预览后写入**：`preview` 返回转换预览行（不落盘），人工核对无误后 `confirm` 才写 Excel，避免脏数据入库
- **依赖编排**：需要前置用例（如先登录提 token 再访问鉴权接口）时，在「依赖管理」页登记用例间依赖；
  执行时 `conftest` 自动展开依赖传递闭包并做**拓扑排序**，保证被依赖用例先跑；
  **依赖失败/停用则阻塞下游**（下游用例登记为 skipped，**不发请求**，避免拿空变量打错接口）；
  `--cases` 精确选择不补全依赖（只在已选用例内排序），避免计划外用例被带进执行

### 多项目隔离（用例越堆越多如何区分）

痛点：所有用例挤在一个 Excel、模块是文件内自由文本，多套被测系统共库后模块名跨项目互相污染、无法区分。
平台的解法是引入**项目维度**——**一个业务系统 = 一个 Excel 用例文件**：

- 「用例管理」页左栏顶部是**项目切换器**，选定项目后右栏模块导航、用例列表、新增/编辑/导入导出
  全部只作用于该项目的文件，**模块名不再跨项目串味**
- 新建项目自动在 `data/cases/<id>.xlsx` 脚手架一张空表（仅表头），切换到它即可从零开始录入；
  删除项目连同其文件一并清理（默认项目为内置项，不可删）
- **默认项目原地指向存量 `data/api_cases.xlsx`**，不搬动文件，因此已有的定时任务/场景/执行历史引用**零影响**；
  CLI 侧 `--file` 直接指向任意项目文件即可跑同一套用例
- 权限不造新码：建/改项目复用 `case:write`，删除复用 `case:delete`；
  项目的元数据存 `data/projects.json`（已 gitignore，用例内容仍存各自的 Excel）
- 底层支撑：`excel_loader.load(allow_empty=True)` 让刚建好、还未录用例的空项目文件能被正常读到空列表而非报错
  （仅平台读取链路用，执行链路 `conftest/main` 仍保持零用例视为异常）

### UI / APP 自动化用例（项目分型：api / ui / app）

项目新增 `kind` 字段：`api`（接口用例，Excel，现状）| `ui`（UI 用例，JSON）| `app`（APP 用例，`.apjson`）。
**一个项目 = 一个用例文件** 的架构不变，多轨并存、设施全复用（执行中心/定时任务/场景/依赖/
Webhook/权限/环境/凭证均按 `file` 参数寻址，kind 从项目解析，按扩展名分流：`.xlsx`→接口、`.json`→UI、`.apjson`→APP）。

- **新建 UI 项目**：「用例管理」页新建项目时选类型为「UI 用例」，后端在 `data/cases/<id>.json`
  脚手架 `{"version":1,"cases":[]}`；切换到该项目后页面切换为 UI 形态（步骤列表 + 可视化步骤编辑器）
- **数据模型**：每条 UI 用例含 `case_id/module/name/desc/priority/run/browser/headless/steps/extract`，
  `steps` 为按序执行的动作列表；UI 项目不支持 Excel 导入导出
- **动作集（v1 12 个）**：`goto / click / fill / press / select / check / hover / wait_for /
  screenshot / expect_visible / expect_text / expect_url`；失败即中断并截全屏
- **定位器**：`css=`（默认前缀可省）/ `xpath=` / `text=` / `role=button,name=登录`，解释器解析为 Playwright locator
- **变量与登录态**：`goto` 相对路径拼环境 `base_url`；取值支持 `{{var}}`（复用 `core/variable.render()`）；
  `extract` 支持 `text|<locator>`、`input_value|<locator>`、`cookie:<name>`、`url`——登录用例提取 token 后下游用例引用
- **执行链路**：`main.py` 按文件后缀分流（`.json`→`tests/test_ui.py`，其余→`tests/test_api.py`）；
  `core/ui_runner.py` 用 Playwright sync API 逐步执行并落「UI步骤」附件 + 截图到 allure-results
- **报告**：执行详情抽屉新增「UI 步骤」Tab——步骤表格（动作/定位器/结果/耗时）+ 失败步骤截图预览
  （截图经 `/api/runs/{id}/attachment/{file}` 取图，仅登录态可读图片）
- **边界（v1）**：仅 Web（chromium），headless 默认开；不做录制、不做实时元素拾取；storage_state 留待二期

**APP 轨（平台已具存储与执行，AI 生成为规划中）**：

- 新建 APP 项目时选类型为「APP 用例」，后端在 `data/cases/<id>.apjson` 脚手架空库；用例读写走
  `backend/app/app_case_store`（校验与落库口径独立于接口/UI，与 `excel_loader`/`ui_case_store` 同为单一真源）
- 平台侧的 APP 用例存储、执行链路与报告已就位；**从对话/文档自动生成 APP 用例的生成管线尚未装配**——
  AI 助手当前只显式声明接口/Web 两轨，若对话意图命中 APP 会明确降级反问（可选接口或 Web 继续），绝不静默误生
- 多端一体的生成与项目作用域详细设计见 `docs/design/多形态用例生成与项目作用域设计方案.md`

## 运行参数

```bash
python main.py [选项]
```

| 参数 | 说明 |
|---|---|
| `--env <名称>` | 环境名，对应 config.yaml 的 `env` 段，默认取 `default_env` |
| `--file <路径>` | 用例 Excel 文件或目录（目录则合并所有 .xlsx），默认 `data/api_cases.xlsx` |
| `--module <名称>` | 按模块名筛选（包含匹配） |
| `--priority <P0,P1>` | 按优先级筛选，逗号分隔 |
| `--keyword <关键字>` | 按关键字筛选（匹配 用例编号/接口名称/用例描述/URL） |
| `--no-html` | 不生成 pytest-html 报告 |
| `--no-allure` | 不生成 allure 结果 |

示例：

```bash
python main.py --env test --module 用户模块 --priority P0
```

## 环境配置（config.yaml）

```yaml
default_env: test
env:
  test:
    base_url: https://httpbin.org   # 用例中相对路径的拼接前缀
    timeout: 15                     # 全局默认超时（秒）
    default_headers:                # 全局默认请求头（用例可覆盖）
      User-Agent: interface-auto/1.0
```

接入真实系统：把 `base_url` 换成被测环境域名，用例中只写相对路径；
多环境（dev/test/prod）在 `env` 段下并列配置多个，运行时 `--env` 切换。

## 敏感凭证管理

账号密码等敏感信息**不要写在用例 Excel 里**（用例文件会提交到 Git 仓库）。框架提供独立的凭证文件机制：

1. 复制 `data/secret_vars_template.xlsx` 为 `data/secret_vars.xlsx`
2. 在「全局变量」表中填入真实账号密码（如 `username` / `password`）
3. 用例中通过 `{{username}}`、`{{password}}` 引用，与普通变量用法完全一致

说明：

- `secret_vars.xlsx` 已被 .gitignore 忽略，**不会提交到仓库**，仅本地生效
- 运行时自动加载（路径可在 config.yaml 的 `secret_vars_file` 修改），文件不存在则跳过
- 同名变量时，凭证文件的值**覆盖**用例 Excel「全局变量」表中的值
  （演示用例可放心用假值，真实运行时由凭证文件接管）

## 失败定位

- **控制台 / HTML 报告**：断言失败展示「实际值 vs 期望值」逐条明细 + 完整请求/响应报文
- **Excel 校验**：格式错误精确定位，如 `[_verify_broken.xlsx → 用例 第2行 K列(请求体)] JSON 解析失败`
- **日志**：`logs/` 下按运行时间戳滚动，含每条用例的完整报文

## 平台版（Web）：登录与角色权限

平台后端 `backend/` + 前端 `platform-web/`，浏览器访问统一入口。所有 `/api/*` 接口默认需要登录态，
细粒度权限由各接口的 `require_perm(...)` 二次校验（前端 `v-perm` 只做按钮显隐，不是安全边界）。

```bash
# 一键启动（推荐）：平台后端 8000 + 前端 5173 各自独立窗口；qa-agent 8100 由后端自动带起
#（config.yaml `agent.auto_start: true`：端口已有监听则跳过，后端退出时自动收掉，不留孤儿进程）；
# 重复执行安全（先清旧监听进程再拉起）；需 qa-agent 仓位于 ./qa-agent，agent_svc 密码存 logs\_svc_pw.txt（已 gitignore）
powershell -ExecutionPolicy Bypass -File .\start_all.ps1
# 一键停止：
powershell -ExecutionPolicy Bypass -File .\stop_all.ps1
# 后端（首次启动自动播种 5 个角色账号；同时自动拉起 qa-agent，无需单独启动）
.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
# 前端
cd platform-web ; npm run dev
# 权限全链路冒烟（需后端已启动）
.venv\Scripts\python.exe tools\smoke_authz.py
# 公共脚本冒烟（定义校验 + 引擎真打接口 + Excel 无损兼容 + 引用保护）
.venv\Scripts\python.exe tools\smoke_scripts.py
# 接口定义冒烟（校验同源 + Excel 聚合 + OpenAPI 导入 + 引用保护 + 用例骨架）
.venv\Scripts\python.exe tools\smoke_api_defs.py
# 全功能端到端冒烟（项目 CRUD/用例 CRUD/智能导入/依赖/Cron+定时/执行与报告删除，零外部依赖）
.venv\Scripts\python.exe tools\smoke_full.py
# UI 自动化端到端冒烟（建 UI 项目 → 用例 CRUD/迁移 → 发起批次 → 步骤轨迹与截图 → 清理，需 chromium）
.venv\Scripts\python.exe tools\smoke_ui.py
# 对话式全链路冒烟（需平台 8000 + qa-agent 8100 双在线，config.yaml 临时置 agent.enabled=true）
$env:IA_SVC_PASSWORD="<agent_svc 密码>" ; .venv\Scripts\python.exe tools\smoke_chat.py
```

角色与权限矩阵（默认基线写在 `backend/perm_store.py`，可在「系统管理 → 角色权限」页勾选调整，自定义分配存 `data/permissions.json`）：

| 权限码 | admin | tl | qa | dev | viewer | agent_svc |
|---|---|---|---|---|---|---|
| `case:write` 用例编辑/导入 | ✓ | ✓ | ✓ | | | ✓ |
| `case:delete` 用例删除 | ✓ | ✓ | | | | |
| `api:manage` 接口定义管理 | ✓ | ✓ | ✓ | | | |
| `script:manage` 公共脚本管理 | ✓ | ✓ | ✓ | | | |
| `plan:manage` 测试场景管理 | ✓ | ✓ | ✓ | | | |
| `run:create` 触发/取消执行 | ✓ | ✓ | ✓ | ✓ | | |
| `env:manage` / `secret:manage` / `webhook:manage` / `schedule:manage` | ✓ | ✓ | | | | |
| `report:delete` 报告删除与旧报告清理 | ✓ | ✓ | | | | |
| `audit:view` 操作审计查询/导出 | ✓ | ✓ | | | | |
| `audit:manage` 审计日志清理 | ✓ | | | | | |
| `user:manage` 用户管理 | ✓ | | | | | |

说明：

- 播种账号 `admin/tl/qa/dev/viewer`（密码同用户名的 123 后缀），**生产部署请立即在「用户管理」改密**；
  账号数据存 `data/users.json`（已 gitignore，密码以 PBKDF2-HMAC-SHA256 加盐哈希保存）
- 报告堆积处理：报告列表行内「删除」+ 顶部「清理旧报告」（按时间保留最近 N 个批次，
  同步清理报告目录、历史条目与场景卡片的批次引用；运行中的批次拒绝删除）
- 三层权限拦截：路由 `meta.perm`（菜单级）→ `v-perm` 指令（按钮级）→ 后端 `require_perm`（数据级），
  前端仅控制可见性，真正拦下越权请求的是后端；用例/依赖/场景/凭证/环境/通知/定时/执行/报告
  的全部**写接口**均已挂 `require_perm`（新增一个 POST/PUT/DELETE 接口就必须带上它）。
  冒烟脚本第 7 节对每个写接口做越权试探，且试探体一律是「必填留空 / 资源不存在 / 自依赖」，
  因此能同时证明三件事：无权限角色 403、持权角色能穿过权限层、一轮试探不会写坏任何数据
- 操作审计留痕（`data/audit_logs.jsonl`，已 gitignore）：所有写操作与登录尝试都记录
  操作人/角色/动作/对象/请求摘要/结果/IP，入口在侧栏「系统管理 → 操作审计」（admin/tl 可见）；
  `password`、`token` 类字段与 Bearer 串写入前统一脱敏为 `***`，明文密码与凭证**绝不落盘**；
  文件超 2MB 自动裁剪最旧记录，页面支持按操作人/时间/仅失败筛选与 CSV 导出（Excel 双击不乱码）
- 权限矩阵可视化编辑（「系统管理 → 角色权限」，仅 admin）：界面只能改「权限码 → 角色」的分配，
  **不能凭空造码**（码目录由代码固定，否则前端 `v-perm` 会失效）；保存后按文件 mtime 热加载，
  **已登录会话无需重新登录即刻按新矩阵判定**（被收回权限者下一个请求即 403）；
  内置防锁死护栏：`user:manage`/`audit:manage` 强制保留给 admin，`run:create` 等关键码不允许全员为空，
  违反一律 400 并回具体原因；页面保存前展示人类可读的变更清单，每次变更自动落审计
- 公共脚本页（「测试管理 → 公共脚本」，需 `script:manage`）：步骤链编辑器的表单直接由
  `/api/scripts/meta` 渲染，动作与可用字段与后端校验同源（`core/scripts.py` 的 `ACTION_FIELDS`），
  **不会出现「页面能填、执行不认」的口径漂移**；用例抽屉的「前置/后置脚本」只允许从脚本库选
  （保存时后端校存在性，笔误当场拦下）；被用例引用的脚本**不可删除、不可改名**（409 并列出引用它的用例编号）；
  引用列首次写值时会自动补 Excel 表头，无需手工重建文件；执行详情抽屉的「脚本轨迹」Tab 逐步展示成败与摘要
- 接口定义页（「测试管理 → 接口定义」，库写操作需 `api:manage`）：列表按模块分组并显示每个接口的
  用例覆盖数，支持手工新建 / 从存量 Excel 聚合 / 粘贴 OpenAPI 文档导入；「生成用例骨架」先预览再落盘
  （需 `case:write`）。设计文档缺口 2.2 的完整说明见上方「接口定义管理」一节
- 登录与会话安全：token 采用**滑动过期**（默认空闲 8 小时失效，每次鉴权自动续期，服务重启后失效）；
  登录失败按 `(用户名|IP)` 限流（窗口内连续 5 次凭据错误锁定 15 分钟返回 429，成功登录清零，停用账号不计入）；
  密码以 PBKDF2-HMAC-SHA256 加盐哈希保存并用 `hmac.compare_digest` 防时序攻击；内网共享工位场景下重点限制“登录不掉线、暴力试密码”
- 定时任务页：执行频率以**可视化构建器**配置（每小时/每天/每周/每月 频率选 + 时间选择框 + 星期/日期选择），
  同时保留 Cron 表达式手动输入框（无法归类的范围/步进表达式自动降为自定义、保持原值不被覆盖），并配「预览未来 N 次执行时间」；
  自定义配置的环境/模块/优先级均为**下拉选择**（环境只能先在「环境配置」新增，模块/优先级取用例元数据），不再手填
- 统计报表页：跨运行的通过率/趋势与模块分布汇总（浅色统一风格），数据源于已归档的报告批次
- 用例项目维度：「用例管理」页以**项目切换器**隔离不同被测系统（项目 = 一个 Excel 文件，详见上方「多项目隔离」）；
  左栏顶部切换项目、就地新建/重命名/删除（需 `case:write`/`case:delete`），选定后模块导航与用例读写仅作用于该项目文件
- AI 生成（「测试管理 → AI 生成」，需 `case:write`）：对话式二期——聊天页（SSE 流式）+ 左侧步骤画布
  （节点可点开编辑、patch 回对话重过闸）+ 接口文档附件一键生成；对接独立 qa-agent 服务（另仓，
  位于 ./qa-agent 且自带 .venv 时由平台后端自动带起，也可手动 `python -m uvicorn agent.main:app --port 8100`，
  网关 Key 存 `localStorage.agent_key`）。
  落库红线不变：过平台权威校验才落，降级/离线自动退为人工确认；`auto` 模式落库携带
  `meta:{origin:"chat", trace_id}` 可审计检索，编号冲突平台自动递增改名（`renamed` 回带声明，绝不静默覆盖）；
  平台侧需 `config.yaml` 置 `agent.enabled=true`（缺省 false 零回归），服务账号用
  `tools/init_agent_svc.py` 一次性创建（角色 `agent_svc`：只读 + `case:write` + `api:manage`，随机密码只打印一次，
  填入 qa-agent 启动环境变量 `QA_AGENT_PLATFORM_PASSWORD`，密码不落任何文件）；
  在线全链路验收：双服务启动后跑 `tools/smoke_chat.py`（对话→文档登记→落库/让号→用例回读→审计→零副作用清理）
- AI 助手会话增强（本轮）：左栏历史会话支持**多选批量删除**（管理态全选/反选 + 二次确认，仅删 Agent 侧历史、用例不受影响）；
  每条会话记录**回答时间**（Agent 最后回复定稿时刻，随会话持久化，列表按此倒序，存量无值回退更新时间）；顶栏项目选择
  降为**作用域过滤器**（默认「全部项目（不限）」，对话可点名项目，冲突以对话意图为准并显性提示）；欢迎语自带**能力自述**
  （接口/Web 可生成、APP 规划中）

## 路线图

- [x] v1：Excel 驱动 + CLI 运行 + 双报告（HTML / Allure）
- [x] v2：页面版（FastAPI + Vue3 + Element Plus，在线编辑用例、触发执行、历史报告）
  - [x] v2 骨架：用例/场景/执行/报告/环境/凭证/通知/定时任务 + 登录与 RBAC 权限
  - [x] v2 审计：写操作与登录尝试留痕、脱敏落盘、筛选查询与 CSV 导出、超量自动裁剪
  - [x] v2 权限：角色权限矩阵可视化编辑（勾选即改，热生效 + 防锁死护栏 + 变更落审计）
  - [x] v2 脚本：前后置公共脚本（声明式步骤链 + Excel 引用列 + 引用保护 + 报告脚本轨迹）
  - [x] v2 接口：接口定义管理（接口库沉淀 + OpenAPI 导入 + 由接口生成用例骨架）
  - [x] v2 提效：用例模板导出 + 智能导入（表头识别/缺项补默认/预期结果→断言，预览确认后落盘）
  - [x] v2 依赖：用例依赖编排（依赖展开 + 拓扑排序 + 依赖失败阻塞不发请求）
  - [x] v2 统计：统计报表（通过率/趋势/模块分布）
  - [x] v2 安全：token 滑动过期 + 登录失败限流 + 登录页优化（密码错误可见提示）
  - [x] v2 定时：定时任务可视化 Cron 构建器 + 环境/模块/优先级下拉选择
  - [x] v2 项目：用例项目维度（一个业务系统一个 Excel + 项目切换器，模块按项目隔离）
  - [x] v2 冒烟：全功能端到端冒烟脚本（smoke_full.py，项目/用例/导入/依赖/定时/执行主链路）
  - [x] v2 UI：UI 自动化（项目分型 api/ui + 可视化步骤编辑器 + Playwright 解释器 + 步骤轨迹/截图报告）
  - [x] v2 Agent：AI 用例生成接入（独立 qa-agent 服务 + 知识库 + 校验闸门 + 「AI 生成」页，生成直连、落库过闸）
  - [x] v2 Agent 二期：对话式重构（SSE 聊天 + 步骤画布编辑闭环 + 文档一键生成 + 自动落库 meta 审计/编号让号，
    在线门禁 smoke_chat.py）
  - [x] v2 多端：三类型用例路由（case_kind auto/api/ui/app，APP 未装配时明确降级反问）+ 项目作用域降为过滤器
    + 会话多选批量删除/回答时间排序
  - [ ] v2 APP 生成：APP 用例的对话/文档生成管线（当前仅平台存储与执行就位，AI 生成为规划中，详见设计文档）
- [ ] v1.1：CI 集成（Jenkinsfile / GitHub Actions）、失败通知模板扩充
