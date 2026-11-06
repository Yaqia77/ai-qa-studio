# UI 自动化五期：能力扩展设计方案（iframe / 多窗口 / 上传 / 等待 / 动作与断言集）

> 前置：v1 UI（JSON+可视化步骤+Playwright 解释器）、二期登录态（storage_state 复用）、
> 三期（浏览器复用 BrowserPool + 用例级失败重试）、四期（xdist 并行：分组×分片 + 依赖护栏 + 日志多进程安全）
> 均已落地并合入 `feat/ui-automation`。本文解决**能力面**的缺口：当前只有 12 个动作、定位只在主文档的
> 根 page 上解析，真实站点常见的 **iframe / 多标签弹窗 / 文件上传 / 原生 dialog / 精细等待 / 富断言** 无法覆盖。
> 主线原则：**只扩能力、不改语义**——存量用例与二/三/四期行为逐条不变。

## 单一口径事实（本期所有新增都必须走这条链，杜绝"页面能填、执行不认"）
- `backend/ui/ui_case_store.py::ACTIONS`（动作→`{desc, locator(需要定位器?), value(需要取值?)}`）是**唯一定义源**；
  `VALID_ACTIONS=set(ACTIONS)`、`COMMON_ACTIONS`（常用/高级分组）。
- `meta()` 把 ACTIONS 下发为 `{action,desc,need_locator,need_value,group}` → `platform-web/src/components/UiStepEditor.vue`
  据此渲染动作下拉与定位器/取值输入显隐。
- `_validate_step` 用 ACTIONS 的 locator/value 必要性做后端落盘校验。
- `core/engines/web/engine.py::_dispatch(page, locator, action, value, entry)` 是唯一执行映射；
  `_resolve_locator(page, locator)` 按前缀解析；`_read_source` 解析 extract 来源。
- `ACTION_ALIASES` 供 Excel 一步一行 / JSON 导入把中文动作名归一。
- 定位前缀 `_LOCATOR_PREFIXES`（后端校验）与 `_resolve_locator`（执行）、前端 `locatorHint` + 底部说明文案三处需同步。

> **本期给 ACTIONS 每条新增一个可选 `hint` 字段**（该动作 locator/value 的填写说明），随 `meta()` 下发，
> 编辑器用作对应输入框 placeholder——这是 drag_to（取值=目标定位器）、upload（取值=文件路径）、
> expect_attr（取值=`属性名=期望值`）、switch_frame（定位器=iframe 选择器）等"取值有特殊语义"动作的**统一提示载体**，
> 不新增字段、不改 StepRow 结构。

## 范围（按阶段增量，每阶段独立可回归）

### P1 交互动作补齐（低风险）
新增动作（key / desc / 需要定位器 / 需要取值 / 执行映射）：
| action | desc | loc | val | _dispatch |
|---|---|---|---|---|
| `uncheck` | 取消勾选 | ✓ | | `locator.uncheck()` |
| `dblclick` | 双击 | ✓ | | `locator.dblclick()` |
| `right_click` | 右键点击 | ✓ | | `locator.click(button="right")` |
| `upload` | 上传文件 | ✓ | ✓(hint:本地路径) | `locator.set_input_files(resolve_path(value))` |
| `scroll_to` | 滚动到元素 | ✓ | | `locator.scroll_into_view_if_needed()` |
| `drag_to` | 拖拽到目标 | ✓(源) | ✓(hint:目标定位器) | `locator.drag_to(_resolve_locator(target, value))` |
| `navigate` | 前进/后退/刷新 | | ✓(back/forward/reload) | `page.go_back()/go_forward()/reload()` |
| `wait_load` | 等待加载状态 | | ✓(load/domcontentloaded/networkidle) | `page.wait_for_load_state(value)` |
| `sleep` | 固定等待(毫秒) | | ✓ | `page.wait_for_timeout(int(value))` |
| `wait_state` | 等待元素状态 | ✓ | ✓(visible/hidden/attached/detached) | `locator.wait_for(state=value)` |

- 全部补中文 `ACTION_ALIASES`（取消勾选/双击/右键/上传/滚动/拖拽/前进后退刷新/等待加载/硬等待/等待状态）。
- `upload` 路径解析：相对值按项目根（ROOT）join，绝对值原样；`sleep/wait_load/navigate` 的 value 做白名单/整型归一，非法即该步失败（中文报错）。
- 分组：`uncheck/dblclick/upload` 归常用扩展；`drag_to/right_click/navigate/wait_load/sleep/wait_state` 归高级。

### P2 断言与变量提取补齐（低风险）
新增断言动作：
| action | desc | loc | val | 判定 |
|---|---|---|---|---|
| `expect_hidden` | 断言不可见 | ✓ | | `not locator.is_visible()`（不存在也视为通过） |
| `expect_count` | 断言元素数量 | ✓ | ✓ | `locator.count() == int(value)` |
| `expect_value` | 断言输入值 | ✓ | ✓ | `locator.input_value() == value` |
| `expect_attr` | 断言属性 | ✓ | ✓(hint:`属性名=期望值`) | `locator.get_attribute(name) == 期望` |
| `expect_title` | 断言标题 | | ✓ | `value in page.title()` |
| `expect_enabled` | 断言可交互 | ✓ | | `locator.is_enabled()` |

新增/扩展 `_read_source` 提取来源（均可在活动 target 上取）：`title`、`storage:<key>`（localStorage）、
`value|<locator>`（=`input_value` 别名）。`expect_attr` 的 value 解析：`partition('=')` → (属性名, 期望值)。

### P3 定位器方言扩展（低-中风险）
`_resolve_locator` / `_LOCATOR_PREFIXES` / 前端 `locatorHint` + 说明文案三处同步新增：
- `testid=<值>` → `page.get_by_test_id(值)`（引导 `data-testid` 稳定标识，架构文档"最佳实践前缀"）
- `placeholder=<文本>` → `page.get_by_placeholder(文本)`
- `alt=<文本>` → `page.get_by_alt_text(文本)`
保留既有 css/xpath/text/role。裸选择器仍默认 css。

### P4 iframe 与多标签窗口 + 原生 dialog（中风险，能力关键）
**执行器引入"活动 target"模型**（这是本期唯一动到执行骨架的部分，务必以"默认即现状"保向后兼容）：
- `UiRunner` 运行期维护 `self.page`（当前根 Page）与 `self.target`（当前 Page|Frame，定位/断言/提取的作用对象）。
  **初始化 `self.target = self.page`**；未使用下列切换动作时，`self.target is self.page`，与四期行为逐条等价。
- 定位类（click/fill/expect_*/_read_source 的 `text|`、`input_value|`）一律在 `self.target` 上 `_resolve_locator`；
  页面级（goto/navigate/wait_load/sleep/expect_url/expect_title/screenshot 整页）在 `self.page` 上。
- 新增切换动作：
  | action | desc | loc | val | 行为 |
  |---|---|---|---|---|
  | `switch_frame` | 切入 iframe | ✓(hint:iframe 选择器) | | `frame=resolve(loc).element_handle().content_frame()`；`self.target=frame` |
  | `switch_main` | 回到主文档 | | | `self.target=self.page` |
  | `switch_window` | 切换标签页 | | ✓(hint:序号或 last) | `pages=context.pages`；index 或 `last` 取末页 → `self.page=p; self.target=p` |
- **原生 dialog**：为每个 page 注册 `page.on("dialog", lambda d: d.accept())` 默认放行 alert/confirm/beforeunload
  （多数流程需要点确定；此前 Playwright 默认 dismiss 会静默取消）。文档标注该默认，冒烟覆盖 confirm 放行。
- 采集/复用/重试/池化**不受影响**：target 切换只在 normal 执行的步骤序列内生效，context 复用与 storage_state 加载逻辑不变。

### P5 前端编辑器与类型（低风险，随 P1–P4 自动生效 + hint 呈现）
- `types.ts::UiActionMeta` 增加 `hint?: string`。
- `UiStepEditor.vue`：定位器/取值输入 placeholder 依 `metaOf(action).hint` 优先展示；底部"定位器前缀"说明补 testid/placeholder/alt；
  快捷组合可选加"上传"示例（不强求）。
- `backend/ui/schemas.py::UiStep.action` 描述文案更新为"见 ACTIONS"（不逐一列举，避免文档漂移）。
- 报告页无需改：新动作 `desc` 来自 ACTIONS，步骤轨迹/截图渲染沿用现状。

## 夹具与测试

### 端到端 fixture（离线、http origin）
- 增强 `tests/fixtures/ui_demo.html`：新增 复选框、`[data-testid]` 元素、可断言数量的一组 `.item`、可双击目标、
  文件 `input[type=file]`（上传后 JS 回填显示名）、`title`、打开新窗口的 `target=_blank` 链接、可 disable/enable 的按钮。
- 新增 `tests/fixtures/ui_frame_host.html`（内嵌 `<iframe src=ui_frame_inner.html>`）与 `ui_frame_inner.html`（可切换/断言的 frame 内容）。

### 单测（不真起浏览器，`unit_tests/`）
- 新建 `test_ui_actions.py`：用 fake page/locator/frame 记录器，逐新动作验证 `_dispatch` 调用了正确的 Playwright API；
  expect_* 的成功/失败两路；wait_load/navigate/sleep 的 value 归一与非法中文报错；drag_to 目标解析；
  `switch_frame/switch_main/switch_window` 后 `self.target/self.page` 指向正确；dialog 默认 accept 触发。
- 扩 `test_ui_case_store.py`：每个新动作的 `need_locator/need_value` 与 `_validate_step` 通过/缺字段报错一致；
  `meta()` 输出含全部新动作且带 `hint`；新前缀 `testid=/placeholder=/alt=` 的 `_validate_locator` 放行。
- 扩 `_read_source`：`title/storage:/value|` 提取（fake）。

### 端到端冒烟
- 新建 `tools/smoke_ui_actions.py`（仿 `smoke_ui.py` 的 urllib harness + 本地 http 提供 fixtures）：
  逐能力建临时 UI 项目+用例并跑批次断言通过——uncheck/dblclick/upload/scroll/drag/navigate/wait_load/wait_state、
  expect_hidden/count/value/attr/title/enabled、testid/placeholder/alt 定位、switch_frame 内断言、switch_window 新窗口断言、
  dialog 放行后继续；收尾删临时项目零副作用。浏览器不可用时执行段合理 skip 不判失败。

## 兼容性与风险（护栏）
- **底线：默认即现状**。新动作是"新增键"，存量用例不用即无感；P4 的 `self.target` 初始 `= self.page`，
  未用切换动作时与四期逐条等价；`smoke_ui.py`/`smoke_ui_session.py` 全绿作为回归门禁。
- **风险1｜P4 执行骨架重构**：作用面最大 → 单测覆盖"无切换动作路径与现状一致"；把页面级 vs target 级动作分清。
- **风险2｜dialog 默认 accept 改变默认行为**：可能影响需要"取消"的 confirm 流程 → 冒烟显式覆盖 accept，文档标注默认放行；
  如需 dismiss 场景，留后续期加显式 `dismiss_dialog` 动作（本期不做）。
- **风险3｜upload 读本地文件路径**：测试平台按需在本机取文件，不做沙箱；文档标注 value 为运行机可访问路径。
- **风险4｜wait_state/sleep 滥用致脆**：编辑器 hint 引导优先 `wait_for/wait_state` 显式等待，`sleep` 标注"兜底慎用"。
- **口径一致**：所有新动作/前缀一次同时改 ACTIONS + meta + _validate_step + _dispatch + 前端 hint，杜绝漂移；
  `test_engine_contract.py` 若断言动作集需同步更新。

## 落地顺序（增量可回归）
P1 交互动作 → P2 断言/提取 → P3 定位方言 → P4 iframe/多窗口/dialog → P5 前端 hint/类型 → 夹具+单测+冒烟 → 门禁。
每完成一阶段本地跑一次相关单测，最后统一跑全门禁。

## 验证门禁（全绿方可提交，沿用既有交付纪律）
1. `pytest unit_tests\...` 全通过（含新增 `test_ui_actions.py` 与扩展用例）。
2. `pytest --collect-only tests\test_ui.py --file <ui.json>` 正常收集。
3. `npm run build` 成功，无模板/类型错误。
4. 重启后端（无 --reload）：UI 用例 CRUD 带新动作/hint 的 JSON 2xx 落盘；`/api/ui-cases/meta` 下发含新动作与 hint；旧端点无 500。
5. `tools\smoke_ui_actions.py` 通过（或浏览器缺失执行段合理 skip）；`tools\smoke_ui.py`（v1）+ `tools\smoke_ui_session.py`（二期）回归全绿。
6. 前端步骤编辑器新动作下拉、hint 提示、定位前缀说明无报错；UI 用例页/登录态页无 500。
- 以上通过后，按既有门禁提交并推送 GitHub（提交前确认）。

## 非目标（明确留后续，防失焦）
- 元素拾取 / 录制（编写提效 DX，独立期）。
- 执行录屏 video / trace、flaky 趋势看板（观测与诊断，独立期）。
- App 引擎接入（Appium，三合一阶段 3）。
- 存储收敛（Excel 降级为导入/导出，三合一阶段 4）。
- 视觉回归（screenshot diff）断言、跨 origin 复杂 SSO 编排。
