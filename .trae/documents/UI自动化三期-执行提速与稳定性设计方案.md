# UI 自动化三期：执行提速与稳定性设计方案

> 前置：v1 UI 自动化（JSON + 可视化步骤 + Playwright 解释器 + 步骤轨迹/截图）、二期登录态
> （storage_state 具名会话复用）均已落地并合入 `feat/ui-automation`。本文只解决二期**明确留作三期**
> 的一类缺口：**执行提速与稳定性**——消除「每条用例各起一个浏览器」的重开销，并为 UI 用例提供
> 可控的失败重试；把并行执行作为**独立可选阶段**单列，避免与上下文复用语义打架。

## Context：现状与真正的痛点（代码事实）

### 已落地基线（别推翻，直接在其上增量）
- **统一引擎注册表（阶段 0 已就位）**：`core/engines/{base,api,web,app}`，`REGISTRY / get_engine(key) /
  engine_for_file(path)` 取代了 `main.py` 里「按后缀 if/else 分流」。`web` 引擎 key="web"、`matches`
  认领 `.json`、`test_module=tests/test_ui.py`；`app` 引擎为**脚手架**（`matches=.apjson`、`execute`
  返回未实现中文指引，驱动未接入）。
- **Web 会话 = 二期 UiSession**：`backend/ui/session_collect.py` 提供 `collect()` 与
  `resolve_for_attach()`；`UiRunner` 通过注入的 `session_provider / session_committer` 回调解耦，
  core 不强依赖 backend。采集走 `POST /api/ui-sessions/{id}/collect`（同步单跑），复用走
  `new_context(storage_state=..)`。
- **进程内会话登记表**：`tests/conftest.py` 的 `ui_session_registry`（session-scope），
  记录 `<id>__<env>` 的重采去重；因批次是 `subprocess` 起 `main.py`，登记表仅在**单个 pytest 进程内**有效。

### 本期要解决的痛点（二期文档「非目标」原文所留）
现状 `core/engines/web/engine.py` 的 `UiRunner.run()` **每条用例**都独立地：
```
sync_playwright().start() → _launch(browser) → new_context() → new_page() → ... → finally 逐层关闭
```
因此一个 100 条用例的批次要**启停 100 次 playwright + 100 次浏览器进程**：
- **慢**：浏览器冷启动 + Chromium 进程初始化是 UI 执行里最大的固定开销，与用例复杂度无关，被 N 倍放大。
- **不稳**：偶发的启动抖动 / 资源竞争 / 站点瞬时抖动会把「环境噪声」记成「用例失败」，缺重试兜底。

### 本期目标（交付主线）
1. **浏览器实例复用**：单进程内按 `(browser, headless, channel)` 分组，`launch` 一次、每条用例只
   `new_context`（保持 context 级隔离，cookie/localStorage/会话互不串味），批次结束统一回收。
2. **用例级失败重试（可配）**：为 flaky 提供兜底，重试轨迹与最终判定写入现有 steps_trace/报告。
3. **安全阀**：复用带来的长驻进程需可控——每 N 条用例或内存阈值主动回收重启浏览器，防止越跑越慢/崩。

### 明确非目标（防止范围失焦，留待后续独立期）
- **并行执行（xdist / 多 worker）**：见「阶段 C」——与上下文复用语义存在取舍，单列为可选后半段，
  默认不在本期主线交付，主线先把「复用 + 重试」做稳。
- **录制 / 实时元素拾取**（编写提效 DX，另一主题）。
- **跨 origin 复杂 SSO 编排 / OAuth 回调链自动跟进**。
- **登录态主动探活 ping**（二期已做「按 TTL 判定 + 过期就地重采 + 手动采集」，本期不叠加主动探活）。
- **App 引擎驱动接入（Appium）**：属三合一阶段 3，环境重、flaky 高，不在本期。

## 概念模型

| 概念 | 说明 |
|---|---|
| BrowserBundle（浏览器束） | 一个 `(playwright, browser, spec_key)` 组合，`spec_key=(browser, headless, channel)`；进程内按 spec_key 复用 |
| context 级隔离 | 每条用例仍 `browser.new_context(...)`（可带 storage_state），执行完**只关 context 不关 browser**，隔离性与「用例独立」语义完全保留 |
| 复用批次作用域 | BrowserBundle 的生命周期归**单 pytest 进程**（session-scope fixture 持有），进程退出即回收 |
| 安全阀（reap） | 累计执行条数 / 单 bundle 存活超过阈值 → 主动关闭并重启该 bundle，防止长批次资源膨胀 |
| 用例重试（retry） | 失败用例按 `retry` 次数重跑；每次尝试独立采集步骤轨迹/截图，最终以「任一次通过即通过」判定 |

> 关键不变式：**复用只折叠「浏览器进程」这一层开销，绝不折叠「context」这层隔离**。二期/一期的
> cookie、localStorage、会话 attach、extract 变量池、base_url 解析语义全部保持——因为每条用例依旧是
> 一个全新的 context，只是底下的 browser 不再反复启停。

## 方案设计

### A. 浏览器实例复用（本期核心）

#### A1. `BrowserPool`（进程内，session-scope）
新增 `core/engines/web/browser_pool.py`（core 侧，无 backend 依赖）：
```python
class BrowserPool:
    def __init__(self, reap_after: int = 200):   # 每 bundle 跑满 reap_after 条主动回收
        self._locks = {}                          # spec_key -> threading.Lock
        self._bundles = {}                        # spec_key -> BrowserBundle
        self._counts = {}                         # spec_key -> 已复用条数

    def acquire(self, browser: str, headless: bool, channel: str | None):
        spec = (browser, headless, channel or "")
        with self._lock(spec):
            b = self._bundles.get(spec)
            if b is None or b.dead or self._counts[spec] >= self.reap_after:
                if b: b.close()                   # 回收旧的（安全阀触发 / 已失效）
                b = self._launch(spec)            # 全新 launch playwright + browser
                self._bundles[spec], self._counts[spec] = b, 0
            self._counts[spec] += 1
            return b

    def close_all(self): ...                      # 进程退出统一回收
```
- **线程锁**：pytest 默认串行足够，但保留 `Lock` 以防未来并行/xdist 单进程内多线程驱动复用同池。
- **dead 检测**：`browser.is_connected()` 为假或上一次执行抛 `BrowserClosed`/`TargetClosed` → 标记 dead，
  下次 acquire 触发重启（覆盖「浏览器中途崩溃」）。

#### A2. `UiRunner` 改造（复用优先，缺省回退独立启停）
`core/engines/web/engine.py::UiRunner`：
- 构造新增可选 `browser_pool`（回调/对象注入，与 `session_provider` 同解耦风格）。
- `run()`：
  - **有 pool**：`bundle = pool.acquire(...)`；`context = bundle.new_context(storage_state=ss)`；
    `finally` **只关 context**，绝不关 browser/playwright。
  - **无 pool**（采集模式 / 未注入的旧调用点）：**保持现状**独立 `sync_playwright().start()→launch→...→close`，
    行为与二期逐字节一致（向后兼容底线）。
- **采集链路（mode="collect"）继续走独立进程/独立浏览器**，不进池——采集是同步短任务，与批次复用无交集，
  避免采集崩溃牵连批次。`session_collect.collect` 不注入 pool 即可。

#### A3. 注入点
`tests/conftest.py`：新增 session-scope fixture `ui_browser_pool()`（`yield BrowserPool()`，`finally close_all()`）。
`tests/test_ui.py::test_ui`：签名加 `ui_browser_pool`，`execute(..., browser_pool=ui_browser_pool)`，
与已有 `session_provider` 并列透传。**未引用会话 + 无 pool 的存量路径不受影响**。

### B. 用例级失败重试（可配）

#### B1. 配置来源与优先级
- 用例新增可选字段 `retry: int`（0–3，默认 **0**=不重试，保证存量行为不变）。
- `validate_case` 规范化：`retry = max(0, min(int(raw.get("retry") or 0), MAX_RETRY=3))`，非法/负数→0。
- 批次级默认（可选）：`config.yaml` 留 `ui.retry_default`（本期只读取、不强制，缺省 0）。
  优先级：用例 `retry` > 批次默认 > 0。

#### B2. 重试执行语义
- 在 `UiRunner.run()` 外层（`execute` 或 engine 层）做 `for attempt in range(retry+1)`：
  - 每次尝试都跑完整「attach 会话 → 步骤 → 断言/extract」，**独立 try**。
  - 任一次 `ok=True` 即最终通过并停止；记录 `attempts` 次数。
  - 全失败才判 fail，保留**最后一次**的错误与截图，并附每次尝试的摘要。
- 步骤轨迹：`steps_trace` 增补 `attempt` 维度（或在结果上挂 `attempts: [...]`），报告页展示「重试 k 次后通过」，
  区分「稳定失败」与「flaky」。**采集模式不重试**（采集失败直接反馈，交由重采/手动）。

#### B3. 与截图/报告的整合
- 每次失败尝试各存一张截图（现有 screenshot 逻辑按 attempt 命名），最终报告展示末次 + 摘要，避免刷屏。
- `allure_store` 归档结构向后兼容：新增字段为可选，历史批次读取不受影响。

### C. 并行执行（明确列为可选后半段，默认不交付）

> 与 A 的复用存在**语义取舍**：`pytest-xdist` 是多**进程**，每个 worker 各持自己的 BrowserPool（进程内复用不跨 worker），
> 于是「复用」收益随并行度稀释，且 worker 数 × 浏览器数会推高机器资源、放大 flaky。

- 若后续要做：以 `runs` 批次参数 `workers`（默认 1）驱动 `-p xdist -n`；`ui_session_registry` / `BrowserPool` 均为
  **每 worker 进程独立**（现状本就进程内），跨 worker 的同会话重采用「原子写 + last-write-wins」兜底（二期已具备）。
- 建议触发条件：单批次用例数 > 阈值（如 50）且机器核数足够；否则默认串行。
- **本期不做**，仅在文档留接口形状与权衡，防止范围失焦。

## 目录结构与改动清单

```
interface-auto/
├── core/engines/web/
│   ├── browser_pool.py            # 新增：BrowserPool + BrowserBundle（进程内复用 + 安全阀 + dead 重启）
│   └── engine.py                  # 改：UiRunner 支持 browser_pool 注入（缺省回退独立启停，向后兼容）；重试外层
├── backend/ui/
│   ├── ui_case_store.py           # 改：validate_case 规范化 retry 字段（0..3，默认 0）
│   ├── schemas.py                 # 改：UiCaseOut/Create/Update 增加 retry: int = 0
│   └── routers/ui_cases.py        # 确认：create/update 透传 retry（store 已按 model_dump，一般无需改逻辑）
├── tests/
│   ├── conftest.py                # 改：新增 session-scope ui_browser_pool fixture
│   └── test_ui.py                 # 改：签名加 ui_browser_pool，execute 注入 browser_pool
├── tools/
│   ├── smoke_ui_session.py        # 回归：确保复用改造后二期登录态端到端仍全绿
│   └── smoke_ui.py                # 回归：v1 行为不变（无 pool/无 retry 的存量路径）
├── unit_tests/
│   └── test_ui_browser_pool.py    # 新增：BrowserPool 纯逻辑单测（假 driver 注入，不真起浏览器）
└── platform-web/src/
    ├── views/UiCasesView.vue      # 改：用例抽屉新增「失败重试次数」输入（0–3）
    ├── types.ts                   # 改：UiCaseItem/... 增加 retry?: number
    └── api/index.ts               # 透传（沿用现有 create/update 用例体，无新端点）
```

## 兼容性与风险（护栏）

- **底线：不改语义，只折叠开销**。context 级隔离保留 ⇒ cookie/localStorage/会话 attach/extract 全部与二期一致。
- **向后兼容默认值**：`retry` 默认 0（不重试）、`browser_pool` 未注入即回退独立启停 ⇒ **存量用例逐条行为不变**；
  v1 冒烟 `smoke_ui.py`、二期 `smoke_ui_session.py` 须保持全绿作为回归门禁。
- **风险1：长驻浏览器内存/句柄泄漏** → A1 的 `reap_after` 安全阀 + `is_connected()` dead 检测主动回收。
- **风险2：复用下跨用例污染错觉** → 严禁复用 context（只复用 browser）；若某用例显式需要纯净环境，
  context 已保证隔离；`goto` 相对路径仍按各自 base_url 解析。
- **风险3：重试掩盖真 bug** → 重试次数上限 3、报告必须显式标注「重试 k 次后通过」，让 flaky 可见而非被吞；
  可选 quarantine 标记留后续期。
- **风险4：采集与复用耦合** → 采集**不进池、不重试**，保持独立进程/浏览器，崩溃不牵连批次。

## 落地分期（增量、非破坏，每阶段独立可回归）

- **阶段 1（复用）**：A1 BrowserPool + A2 UiRunner 注入 + A3 fixture；先不加安全阀细节，跑通「批次只 launch 一次」。
  验收：`smoke_ui.py`/`smoke_ui_session.py` 全绿；一条批次日志里浏览器启动次数从 N 降到分组数。
- **阶段 2（安全阀）**：加 `reap_after` + dead 重启；补 `test_ui_browser_pool.py` 纯逻辑单测。
- **阶段 3（重试）**：B1/B2/B3；后端 schema + 前端输入；补重试轨迹报告。验收：构造一条前 1 次必失败用例，
  `retry=1` 后通过且报告标注重试。
- **阶段 4（并行，可选）**：C；默认关闭，独立评审后再上。

**本期主线 = 阶段 1–3**；阶段 4 明确留作后续。

## 验证门禁（全绿方可提交，沿用既有交付纪律）

1. `pytest unit_tests\...` 全通过（含新增 `test_ui_browser_pool.py`）。
2. `pytest --collect-only tests\test_ui.py --file <ui.json>` 正常收集（新 fixture 装配无报错）。
3. `npm run build` 成功，无模板/类型错误。
4. 重启后端（无 --reload）：UI 用例 CRUD 带 `retry` 字段 2xx；旧端点无 500。
5. `tools\smoke_ui_session.py`（二期回归）+ `tools\smoke_ui.py`（v1 回归）全绿——**复用改造不得改变既有结果**。
6. 端到端新证据：一个含多条用例的小批次，断言浏览器启动次数 == 分组数（非用例数）；一条 flaky 用例
   `retry=1` 能转绿且报告标注「重试 1 次后通过」。
7. 前端登录态页 / UI 用例抽屉（新增重试输入）无报错。
- 以上通过后，按既有门禁提交并推送 GitHub（提交前确认）。

## 跨机器接续说明（换电脑开发必读）

- 本文档位于 `.trae/documents/`（**已纳入 git 跟踪**），随分支 `feat/ui-automation` 同步；
  新机器 `git clone` 或 `git pull` 后即可读取，无需额外分发。
- 环境复现（新机器一次性）：`poetry install`（或按 `requirements`）→ `playwright install chromium`
  （或复用本机 Google Chrome，见二期兼容改动）→ 启动后端 `.venv\Scripts\python.exe -m uvicorn
  backend.main:app --port 8000`（**无 --reload**，改后端代码需重启）→ 前端 `cd platform-web && npm install && npm run dev`。
- 敏感项不入库：`data/ui_sessions/`（storage_state 实体）、`data/secret_vars.xlsx` 等已 `.gitignore`；
  新机器按需重填 secret_vars 或重新采集登录态。
- 分支/基线：本期在 `feat/ui-automation` 上续作，起点 = 二期登录态合并提交（`b6c71c0` 之后）。
