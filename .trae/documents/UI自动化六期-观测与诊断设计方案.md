# UI 自动化六期 · 观测与诊断设计方案

## 一、背景与目标

五期完成 UI 能力扩展（iframe / 多窗口 / 上传 / 等待 / 富断言 / 定位方言）后，用例「能跑」已具备，
但**跑失败后难定位、跑不稳定难发现**仍是运维痛点：

- 失败用例目前只有一张终态截图 + 首行错误摘要，缺 DOM 快照 / 网络 / console 的深度现场，跨端排障靠猜；
- 三期-B 引入的用例级重试让 flaky「重试后通过」可见，但数据散落在各批次 `UI重试轨迹` 附件里，
  没有跨批次的趋势视图——哪些用例长期不稳定、是否需要治理，无从一眼判断。

六期方向：**观测与诊断**（纯软件、可离线测、向后兼容）。两个可独立验收的能力：

1. **失败 Trace 留存**：UI 用例最终失败时自动导出 Playwright trace（DOM 快照 + 网络 + console + 截图），
   落入批次 allure-results，报告页可一键下载，用 `npx playwright show-trace` 回放定位。
2. **Flaky 趋势看板**：基于已有 `UI重试轨迹` 数据，在统计报表新增跨批次 flaky 视图，
   让「靠重试蒙过」的不稳定用例可见、可排序、可治理。

## 二、设计原则（沿用历史纪律）

- **向后兼容底线**：存量批次报告形状不变；新字段全部可选、缺省即旧行为。`WebEngine.execute` 的
  trace 开关**默认 `off`**，真实执行由 `test_ui.py` 从 `config.ui.trace` 传入——确保现有 250 单测
  （未传 trace、喂无 `.tracing` 的假 context）零改动通过。
- **单一口径**：trace 采集落在 `core/engines/web/engine.py`（UiRunner.run 的 context 生命周期内），
  附件落盘走 allure 回调注入（与截图 `attach`、`UI重试轨迹` 同一注入式口径），core 不依赖 allure/backend。
- **降级友好**：trace 相关一切异常（无回调 / 假对象 / 无 allure / 采集模式）全部 try/except 吞掉并记日志，
  绝不影响用例成败判定与既有报告。
- **录屏/视频本期不做**：需改造浏览器池 `new_context(record_video_dir=…)` + 全量落盘 IO 成本；
  trace 已含 DOM/网络/console/截图，排障够用。列为后续候选。

## 三、分阶段实施

### P1 后端 trace 采集（core）
- `UiRunResult` 新增 `trace_blob: Optional[bytes] = None`。
- `UiRunner.__init__` 新增 `trace: str = "off"`；`run()` 在 context 建立后，若 `trace != "off"` 且
  context 具备 `tracing`，`context.tracing.start(screenshots=True, snapshots=True, sources=False)`（全程 try/except）。
- `run()` 的 `finally` 关闭 page/context **之前**：若本次 `result.ok is False` 且已启动 tracing，
  `context.tracing.stop(path=临时zip)` 读回字节写入 `result.trace_blob`，清理临时文件。
- 采集模式（mode="collect"）不启用 trace（短任务、隔离风险）。
- 重试语义落点在 `WebEngine.execute`：trace 默认 `"off"`、新增 `attach_trace` 回调入参透传给 UiRunner；
  循环结束后**仅当最终 `not result.ok` 且 `result.trace_blob`** 时调用 `attach_trace("UI失败trace", blob)`
  ——中间失败但末次通过的用例不留 trace（末次 result 覆盖），避免误导。

### P2 trace 服务与展示（backend + frontend）
- `config.yaml` 的 `ui:` 增 `trace: on_failure`（`off` / `on_failure`，注释说明）。
- `tests/test_ui.py`：读 `app_config["config"]["ui"]["trace"]`（缺省 `on_failure`）作为 `trace` 传入
  `engine.execute`；`attach_trace` 回调在有 allure 时 `allure.attach(name, zip_bytes, extension="zip")`，
  无 allure 传 None。
- `backend/shared/allure_store.py::case_detail`：新增 `detail["trace_file"] = _find_attachment(r, "trace")`
  （命中失败 trace 的 .zip 源文件名，否则 None）。
- `backend/shared/routers/runs.py`：新增 `GET /{run_id}/trace/{filename}`——限定 `.zip` 扩展、
  防路径穿越（同 attachment 端点口径），`FileResponse(media_type="application/zip")` 供下载。
- `platform-web/src/api/index.ts`：新增 `reportTraceUrl(runId, filename)`（带登录 token）。
- `platform-web/src/types.ts`：`RunCaseDetail` 增 `trace_file?: string | null`。
- `platform-web/src/views/ReportView.vue`：明细区当 `detail.trace_file` 存在时给「下载 Trace」按钮
  （附一行 `npx playwright show-trace <文件>` 使用提示）。

### P3 Flaky 趋势看板（backend 聚合 + frontend）
- `backend/shared/allure_store.py::list_cases`：把「读 `UI重试轨迹` 生成标注」重构为读一次、
  结构化暴露每条摘要：`attempts`（缺省 1）、`retried`（attempts>1）、`flaky`（attempts>1 且 final_ok 且
  status=passed）。零额外 IO——仅真发生过重试的用例才有该附件，其余 `_find_attachment` 返回 None 直接缺省。
- `platform-web/src/types.ts`：`RunCaseSummary` 增 `attempts?: number; retried?: boolean; flaky?: boolean;`。
- `platform-web/src/views/StatsView.vue`：复用已加载的 `batchCases`，新增「Flaky 趋势」面板：
  按 case_id 聚合近 N 批的 执行次数 / flaky 次数（重试后通过）/ 失败次数，按 flaky 率排序列出可疑用例；
  KPI 区补一个「flaky 用例数」指标。纯前端聚合，无需新端点。

### P4 测试
- 单测（`unit_tests/`，纯逻辑假对象，不真起浏览器）：
  - trace：假 context 带 `.tracing`（记录 start/stop、stop 写 bytes）——验证 `trace="on_failure"` 且失败
    才产出 `result.trace_blob`；通过 / `trace="off"` / 采集模式 / 假对象无 tracing 均不产出、不抛；
    `WebEngine.execute` 仅末次失败才调 `attach_trace`。
  - flaky：`list_cases` 对含/不含 `UI重试轨迹` 的批次，正确产出 attempts/retried/flaky；存量无重试附件
    字段缺省 1/False/False（形状向后兼容）。
- 端到端冒烟（`tools/`，真实浏览器，浏览器不可用时优雅跳过）：造一个必失败的 UI 用例跑一批，
  断言 allure-results 出现 `.zip` trace 附件、`/api/runs/{id}/cases/{cid}` 返回 `trace_file`、
  `/api/runs/{id}/trace/{file}` 能下载（zip 魔数 PK）。

### P5 门禁（全绿后方可提交）
1. `pytest unit_tests` 全绿（含新增用例，且不破存量 250）。
2. `pytest --collect-only`（指向真实 UI 项目 JSON）正常收集。
3. `npm run build`（platform-web）成功。
4. 重启后端：openapi 无 500、新端点出现在 schema、`/api/ui-cases/meta` 正常。
5. 三套 UI 冒烟全绿：`smoke_ui_actions` / `smoke_ui` / `smoke_ui_session`（+ 新 trace 冒烟）。
6. 前端页面无报错。
7. 全绿后向用户确认，再提交并推送 `origin/feat/ui-automation`。

## 四、验收标准
- 失败的 UI 用例在报告页能下载 trace.zip，本地 `npx playwright show-trace` 可回放。
- 统计报表能看到跨批次 flaky 用例排行，重试后通过的用例被明确标为 flaky。
- 存量批次 / 未开启 trace / 无 allure 环境零回归；所有新增字段可选、缺省即旧行为。

## 五、非目标（留后续期候选）
- 录屏 / 视频留存（需池 new_context 改造 + 全量落盘成本）。
- trace 在平台内嵌可视化回放（当前给下载 + CLI 指引）。
- 元素拾取 / 录制生成步骤、视觉回归断言、App 引擎接入。
