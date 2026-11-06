# UI 自动化二期：登录态（storage_state）复用设计方案

## Context

UI 自动化 v1 已落地（项目分型 api/ui + 可视化步骤编辑器 + Playwright 解释器 + 步骤轨迹/截图报告）。
v1 的边界里明确写了「storage_state 留待二期」，本方案即补这一缺口。

**v1 现状（代码事实）**：`core/ui_runner.py` 的 `UiRunner.run()` 每条用例都独立地
`sync_playwright().start() → _launch() → browser.new_context() → context.new_page()`，
context 是**用例级**、执行完 `finally` 里关闭。因此 **cookie / localStorage 不跨用例留存**。

**痛点**：凡是需要登录态的界面流程，每条用例都要把「打开登录页 → 填账号 → 填密码 → 点登录」
这套步骤重跑一遍：

- **慢**：登录往往是整条链路里最重的一步（含跳转、验证码、接口等待），N 条用例 = N 次登录。
- **脆**：登录接口/页面一旦抖动，所有下游用例一起挂，把「被测功能的问题」和「登录环境的问题」混在一起，
  失败定位困难。
- **散**：登录凭证依赖铺在每个用例里，改一处登录流程要改一堆用例。

**目标（本期唯一交付）**：把「一次登录」沉淀为一份可复用的**登录态**（Playwright `storage_state`：
cookies + localStorage），让需要登录的用例**开机即已登录**，从用例步骤里删除重复登录动作。

**非目标（明确留待三期，防止范围失焦）**：
- 浏览器上下文池化 / 并行执行（本期仍每条用例起一个浏览器，只是加载登录态，成本可接受）
- 录制 / 实时元素拾取
- 跨 origin 的复杂 SSO 跳转编排、OAuth 回调链自动跟进
- 登录态失效的主动探活重试（本期只做「按有效期判定 + 过期自动重采 + 手动采集」）

## 概念模型

引入一个新一等资源 **登录态会话（UiSession）**，与接口库/脚本库同档：

| 概念 | 说明 |
|---|---|
| 会话 Session | 一份具名的登录态，绑定「环境 + 浏览器」，由一条「采集用例」登录成功后导出的 storage_state |
| 采集 Collect | 运行会话指定的采集用例（走现有 UI 解释器），成功登录后把 `context.storage_state()` 落盘为会话实体文件 |
| 复用 Attach | 其它 UI 用例通过 `session` 引用某会话；执行时以该会话实体作为 `new_context(storage_state=...)` 的入参开机加载 |
| 有效期 TTL | 会话采集时刻 + `ttl_seconds`；过期则复用前自动触发一次重采（in-process），或提示手动采集 |

关系：一个**会话**归属一个**环境**（域名不同 cookie 不通用）；一个会话由**一条采集用例**产出；
**多个 UI 用例**可引用同一会话。

## 数据模型与落盘

沿用 v1 双文件范式：元数据注册表（可提交、无敏感值）+ 实体文件（敏感、gitignore）。

### 1) 注册表 `data/ui_sessions.json`

```json
{
  "version": 1,
  "sessions": [
    {
      "id": "album_test_login",        // 唯一名（不含 / \ ? #，直接寻址）
      "name": "相册测试环境登录态",
      "desc": "",
      "env": "test",                    // 绑定 config.yaml 的环境名（决定 base_url/域名归属）
      "browser": "chromium",            // 采集与复用统一按此内核
      "headless": true,                 // 采集时的无头开关（复用不启动 UI 编辑，仅影响采集）
      "file": "data/cases/ui_demo.json",// 采集用例所在的 UI 项目文件（相对项目根）
      "capture_case": "ui_login_001",   // 采集该登录态的用例编号（须存在于上面的 file）
      "ttl_seconds": 3600,              // 复用有效期；<=0 视为「永不过期（不自动重采，仅手动）」
      "created_at": "2026-10-23T10:00:00",
      "captured_at": "2026-10-23T10:00:00",  // 最近一次成功采集时间；null=从未采集
      "last_error": null                // 最近一次采集失败原因（成功即清空）
    }
  ]
}
```

- `captured_at` / `last_error` 是**运行态字段**，由采集链路回写，用户新建时不填。
- 有效期是否「已过期」为**派生态**：`expired = ttl_seconds>0 and (now - captured_at) > ttl_seconds`；
  注册表只存 `captured_at` 与 `ttl_seconds`，不落 `expired`（避免与时间漂移打架），口径同理 v1 的 `case_count`。

### 2) 实体文件 `data/ui_sessions/<id>__<env>.json`

Playwright `storage_state()` 导出的原始 JSON（cookies + origins/localStorage）。

- **按 `id__env` 命名**：同一会话名在不同环境各留一份，切环境不串味。
- **绝对敏感**：内容等同「实时登录凭证」，`data/ui_sessions/` 整目录进 `.gitignore`；
  **后端任何接口都不回传实体内容**，只回传注册表元数据与状态（valid / expired / not_collected / failed）。
- 原子写：临时文件 + `os.replace`（与 `ui_case_store._write` 一致），被占用转 `locked`。

### 3) UI 用例新增字段（`backend/schemas.py` + `ui_case_store.validate_case`）

在 `UiCaseOut/Create/Update` 增加一个可选字段：

```python
session: str | None = Field(default=None, description="引用的登录态会话 id（开机加载其 storage_state）")
```

- `validate_case` 里对 `session` 只做**规范化**（strip，空串→None），**不做存在性硬校验**
  （理由同 v1 接口库「元数据非执行体」：会话库读不到时，用例仍应能被列出/保存，执行期再解析）。
- 引用了会话的用例，其步骤里**不应再包含登录动作**——这是编写约定，不强制校验（登录页因站点而异，无法通用识别）。

## 核心执行链路

### A. 采集链路（产生登录态）

1. 解析出会话 `S`（by id），定位其 `capture_case` 在 `S.file` 里的用例 dict。
2. 用**修改过的采集执行器**跑这条用例：`UiRunner` 在正常执行完该用例全部步骤后（且 `ok=True`），
   额外调用 `context.storage_state(path=<实体文件临时路径>)` 并原子替换到实体路径，回写 `captured_at=now, last_error=None`。
3. 采集用例本身**不 attach 任何会话**（它就是要从「未登录」走到「已登录」）。
4. 采集在**独立进程/独立浏览器**里进行，不污染任何批次执行。

采集触发点（两处，共用一个内部函数 `ui_session_store.collect(session_id)`）：
- 「登录态」页的**「立即采集 / 刷新」按钮** → `POST /api/ui-sessions/{id}/collect`
- **复用前置自动重采**：批次执行中某用例引用了**已过期**会话时，在本 pytest 进程内就地重采一次（见 C）

### B. 复用链路（加载登录态，零改动 UI 编辑体验）

- 采集/复用都在现有 `UiRunner` 骨架内完成，只是把 `browser.new_context()` 换成
  `browser.new_context(storage_state=<已校验有效的实体文件路径>)`。
- 引用了未过期会话的用例：开机即带 cookie/localStorage，`goto` 业务页直接是已登录态。

### C. 与批次执行 / 依赖顺序的交互（关键设计决策）

后端执行是 **`subprocess` 起 `main.py`**（每批次独立进程），故「批次内会话缓存」只在
**单个 pytest 进程内**有效。设计一个**进程内会话登记表**（`tests/conftest.py` 的 session-scope 对象）：

```
session_registry = { "<id>__<env>": {"status": "valid|expired|missing|error", "path": "...", "lock": Lock} }
```

`UiRunner` 复用会话时的判定顺序：
1. 计算实体文件路径 → 不存在 = `missing`：**不静默跳过**，用例直接判失败并给中文指引
   （「会话 X 尚未采集，请在『登录态』页点『立即采集』」）——把配置缺失暴露成明确失败，而非误登录成功。
2. 存在但 `expired`（按 TTL）：在进程内**加锁重采一次**（调 `collect`），成功后按新文件 attach；
   重采失败 → 用例失败并带 `last_error`。**同进程内同一会话只重采一次**（登记表记 `renewed=True`），
   避免一批 100 条用例触发 100 次重登。
3. 有效：直接 attach。

> 进程内重采用 `threading.Lock` 足够（pytest 默认串行、同进程）。**跨进程并发**（同一会话被两个批次同时重采）
> 用**原子写 + last-write-wins** 兜底：两个批次各自写出的是「两份都合法」的登录态，最后落盘者生效，
> 不会出现半成品文件（临时文件 + `os.replace` 保证）。不引入跨进程文件锁，避免平台单实例部署下的过度设计。

### D. 变量池 / 敏感凭证关系

- 采集用例若需账号密码登录，仍走 v1 的 `{{username}}/{{password}}` + `secret_vars.xlsx` 机制，本方案不重复造轮子。
- 会话实体文件里的 token/cookie **不进变量池、不进日志、不进注册表**；只以文件形式存在（等同 secret_vars 的地位）。
- 变量优先级链路不变：运行时 extract > secret_vars > Excel 全局变量 > YAML；`base_url` 来自会话绑定的 `env`。

## 目录结构与改动清单

```
interface-auto/
├── data/ui_sessions.json           # 新增：会话注册表（可提交，无敏感值）
├── data/ui_sessions/               # 新增：storage_state 实体文件目录（整目录 gitignore，敏感）
│   └── <id>__<env>.json
├── backend/
│   ├── ui_session_store.py         # 新增：会话 CRUD + collect() + 状态派生 + 原子读写（仿 ui_case_store）
│   ├── schemas.py                  # 改：UiSession* 模型 + UI 用例新增 session 字段
│   ├── routers/ui_sessions.py      # 新增：/api/ui-sessions CRUD + /collect + /status；注册进 main.py
│   ├── routers/ui_cases.py         # 改：创建/更新透传 session 字段（已有逻辑几乎不动）
│   ├── main.py                     # 改：include_router(ui_sessions)
│   └── perm_store.py               # 改：新增权限码 session:manage（默认 admin/tl）
├── core/ui_runner.py               # 改：new_context 支持 storage_state；采集钩子；引用解析
├── tests/
│   ├── conftest.py                 # 改：新增 session-scope ui_session_registry fixture
│   └── test_ui.py                  # 改：向 UiRunner 注入会话解析/重采胶水
├── tools/smoke_ui_session.py       # 新增：登录态端到端冒烟（采集→复用→过期重采→清理）
└── platform-web/src/
    ├── views/UiSessionsView.vue    # 新增：登录态页（列表 + 采集按钮 + 状态徽标 + 新建/编辑）
    ├── views/UiCasesView.vue       # 改：用例抽屉新增「引用登录态」下拉
    ├── api/index.ts                # 改：uiSessions API 封装
    ├── types.ts                    # 改：UiSession / UiCase.session 类型
    └── router/index.ts             # 改：注册路由 + 菜单（session:manage）
```

## 执行器改造要点（`core/ui_runner.py`）

1. `UiRunner.__init__` 增参：`session_provider`（回调：给定 case，返回 `(storage_state_path|None, error_msg|None)`，
   由 test_ui/conftest 注入，保持 core 不依赖 backend 存储层——与 v1 `attach/step` 回调解耦同理）。
2. `run()` 内 `context = browser.new_context()` 改为：
   ```python
   ss = self._resolve_storage_state(case)   # 引用了会话且有效→路径；未引用→None；缺失/过期未采→抛 UiRunnerError（用例失败带指引）
   context = browser.new_context(storage_state=ss) if ss else browser.new_context()
   ```
3. 采集模式：新增 `UiRunner(mode="collect", session_id=...)`；正常流程走完后，若 `mode=="collect" and result.ok`：
   `context.storage_state(path=<tmp>)` 并交给注入的 `session_committer` 回写注册表（原子替换 + `captured_at`）。
   采集模式下**忽略 case 自身的 `session` 引用**（从裸浏览器起步）。
4. attach 语义下 `extract` 的 `cookie:<name>` 仍可用（storage_state 加载后 `page.context.cookies()` 能读到注入的 cookie），
   便于校验「确实已登录」。

## 存储层（`backend/ui_session_store.py`）

对齐 `ui_case_store` 的既有约定，降低学习成本：

- `UiSessionError(kind)`：`invalid / not_found / conflict / locked` → 路由映射 400/404/409/409。
- `validate_session(raw)`：id 合法性（复用 `_CASE_ID_RE` 思路，禁 `/ \ ? #` 及换行）、env 必须存在于 config.yaml、
  `browser ∈ VALID_BROWSERS`、`ttl_seconds ≥ 0`、`capture_case` 非空。
- `collect(session_id)`：定位会话 → 读取采集用例 dict → 起一个 `UiRunner(mode="collect")`
  （用会话的 env 解析 `base_url`，用 `ctx` 加载 secret_vars）→ 成功后落实体文件并回写注册表。
  **采集是同步短任务**（单条登录用例），不走 runs 批次子进程，接口直接返回结果，简单可控。
- `status(session_id)`：派生 `not_collected / valid / expired / failed`（依 `captured_at/ttl/last_error`）+ 剩余秒数。
- `_LOCK = threading.Lock()` + 临时文件原子替换，与 ui_case_store 同构。

## API 设计（`/api/ui-sessions`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/ui-sessions` | 登录态 | 列表（含派生状态，**不含实体内容**） |
| GET | `/api/ui-sessions/{id}` | 登录态 | 详情 + 状态 |
| POST | `/api/ui-sessions` | `session:manage` | 新建会话（校验后落注册表） |
| PUT | `/api/ui-sessions/{id}` | `session:manage` | 更新（id 不可改；改 env/case/ttl 会作废旧 `captured_at`？——见下） |
| DELETE | `/api/ui-sessions/{id}` | `session:manage` | 删除注册表条目 + 连带删实体文件 |
| POST | `/api/ui-sessions/{id}/collect` | `session:manage` | 立即采集/刷新登录态，同步返回成功或 `last_error` |
| GET | `/api/ui-sessions/{id}/status` | 登录态 | 单查状态（供页面徽标轮询） |
| GET | `/api/ui-sessions/meta` | 登录态 | 下拉候选：环境名 + 浏览器 + 「当前 UI 项目里的用例编号」 |

**更新作废规则（决策）**：改动 `env / browser / file / capture_case / ttl_seconds` 中任一，
后端把 `captured_at` 置空（状态回到 `not_collected`），强制重新采集——因为旧登录态与新定义可能不再匹配。
仅改 `name/desc` 不作废。

**引用保护**：被 UI 用例 `session` 引用的会话，**禁止删除/禁止改 env**（返回 409 并列出引用它的用例编号），
口径同 v1 接口库/脚本库的「引用锚点」保护。引用扫描 = 扫 `S.file` 及所有 UI 项目 JSON 里的 `session==id`。

## 权限与安全（对齐既有 RBAC/审计规范）

- **新权限码 `session:manage`**：默认只给 `admin/tl`（会话等同登录凭证，属高敏操作）；
  查看列表/状态用登录态即可（不泄露实体内容）。
- **敏感数据三不动**：实体文件不进 Git（gitignore）、不回传前端、不写日志/审计正文；
  `collect` 若用到 `{{password}}`，审计请求摘要按现有规则掩码。
- **审计留痕**：新建/更新/删除/采集 四个写操作全部落 `audit_store`（复用写接口审计中间件），
  记录操作人/会话 id/结果，不记 cookie 值。
- **路径安全**：`id`/`env` 参与实体文件名前做白名单清洗（复用 `_CASE_ID_RE` + 禁路径分隔符），防路径穿越。

## 前端（`platform-web`）

- **新增「登录态」页 `UiSessionsView.vue`**（侧栏「测试管理」或「配置」分组，需 `session:manage` 才显操作按钮）：
  - 表格列：名称 / id / 环境 / 采集用例 / TTL / **状态徽标**（🟢有效 剩 xx 秒 / 🟡已过期 / ⚪未采集 / 🔴采集失败(悬停看 last_error)）
  - 行内「立即采集」按钮（i.e. collect），转圈等同步返回；「编辑 / 删除」
  - 新建抽屉：环境（下拉来自 meta）、浏览器（下拉）、采集用例（选一个 UI 项目 + 项目内用例编号，级联下拉）、TTL（秒）
- **UI 用例抽屉 `UiCasesView.vue`**：新增「引用登录态」下拉（候选 = 会话列表），选中后写入 `case.session`；
  引用了会话时给一条浅色提示「本用例开机即带登录态，步骤里无需重复登录」。
- 状态轮询：进入页面时拉一次 `/status`，采集后刷新；不引入常驻定时器（避免无谓请求）。

## 实施步骤（增量可回归，每步都能独立验证）

1. **数据层**：`ui_session_store.py`（CRUD + validate + 原子读写 + status 派生），先不接执行器，配单元测试。
2. **执行器解耦**：`core/ui_runner.py` 加 `session_provider` 回调 + `storage_state` attach + `collect` 模式钩子；
   用 `unit_tests/test_ui_runner.py` 补「attach 有效路径 / 缺失报指引」两条纯逻辑单测（不真起浏览器，注入 fake provider）。
3. **采集落盘打通**：`collect(session_id)` 单跑一条采集用例并导出实体文件；手工用一个公开 Demo 页
   （如 `https://the-internet.herokuapp.com/login` 登录后 localStorage/cookie 有变化）验证 storage_state 往返。
4. **conftest 登记表**：`ui_session_registry` session-scope fixture + 过期就地重采（进程内一次）；`test_ui.py` 注入 provider/committer。
5. **路由 + 权限**：`routers/ui_sessions.py` 全套 + `perm_store` 加 `session:manage` + `main.py` 注册 + 引用保护 + 审计。
6. **前端**：登录态页 + 用例抽屉引用下拉 + api/types/router。
7. **UI 用例新增 `session` 字段贯穿**：schemas / validate / 列表详情透传（不影响存量用例：字段可选、缺省 None）。
8. **冒烟**：`tools/smoke_ui_session.py`：建会话 → 采集（登录）→ 建两条引用会话的 UI 用例（不含登录步骤）
   → 复用执行仅做业务步骤（证明已登录）→ 手动置过期 → 再跑触发就地重采 → 清理。
9. **兼容性回归**：跑 `smoke_ui.py`（v1）确认「不引用会话的存量 UI 用例」行为完全不变。

## 验证方式

```bash
# 后端 + 采集（需可跑 Playwright 的内核或本机 Chrome）
.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
# 登录态端到端冒烟
.venv\Scripts\python.exe tools\smoke_ui_session.py
# v1 UI 冒烟回归（不受影响）
.venv\Scripts\python.exe tools\smoke_ui.py
# 纯逻辑单测（不依赖浏览器）
.venv\Scripts\python.exe -m pytest unit_tests\test_ui_runner.py unit_tests\test_ui_case_store.py
```

预期：
- 采集成功后 `data/ui_sessions/<id>__<env>.json` 生成，注册表 `captured_at` 刷新、状态 🟢。
- 引用会话、**步骤里没有登录动作**的 UI 用例，`goto` 业务页后 `expect_visible` 登录后才有的元素 → **通过**（证明登录态生效）。
- 未采集就引用：用例失败并提示「会话 X 尚未采集，请在登录态页点立即采集」，**不误判为通过**。
- 手动把 `captured_at` 改旧（或 TTL 设小）→ 复用执行触发进程内**一次**就地重采，后续用例复用新登录态。
- 改了 `env/capture_case` 的会话 → 状态自动回到 ⚪未采集。
- 被引用会话删除/改 env → 409 并列出引用用例编号。
- `data/ui_sessions/` 未提交；前端任何接口响应体不含 cookie/token。

## 三期展望（本期刻意不做）

- **浏览器上下文池化 / 并行执行**：进一步去掉「每条用例起一个浏览器」的固定开销；届时登录态复用需从
  「context 级 storage_state」升级为「共享 persistent context」，与本期方案兼容。
- **登录态主动探活**：进入页面后检测是否被踢出（跳转到 /login），自动重采而非仅按 TTL。
- **采集向导 / 录制**：把采集用例做成「录一遍登录即可」，非技术同学零门槛产出登录态。
- **多账号会话矩阵**：同一环境不同角色（管理员/普通用户）各一份会话，按用例指定角色。
