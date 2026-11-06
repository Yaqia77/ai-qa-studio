# UI 自动化七期 —— 视觉回归断言（expect_visual）设计方案

> 交付纪律不变：审查现状 → 确认方向 → 先设计（本文档）→ 分阶段实现 → 门禁全绿 → 确认后提交推送。
> 本文档为**决策完备**规格，可在另一台机器据此从零实现，无需再做方案性决策。

## 1. 背景与目标

六期已交付「观测与诊断」（失败 trace + flaky 看板）。七期补齐 UI 断言能力中的**视觉回归**：
让用例能声明"这块界面/整页长这样"，每次运行把当前渲染与**基线截图**做像素比对，超过容差即失败并留证据图。
面向"改版前后视觉一致性""关键页面不跑样"这类纯功能断言覆盖不到的场景。

选型约束（与历期一致）：**纯软件、可离线单测、向后兼容**。刻意避开 Appium/浏览器内 JS 比对等
不可离线验证或重环境的路线，像素比对放在独立、可喂合成图片纯 Python 单测的模块里。

## 2. 现状复核（关键锚点）

- 动作单一口径来源：`backend/ui/ui_case_store.py::ACTIONS`（L28-63）。加一条即在
  `_validate_step`（L212，校验 `locator/value` 必要性）、`meta()`（L334，前端步骤编辑器下拉据此派生）
  自动跟随；`core/engines/web/engine.py::_action_desc`（L686）懒导入同一 `ACTIONS`。
  `locator:False` 且传了 locator、`value:False` 且传了 value，均按"可选"保留（同 `screenshot` L40）。
- 执行分派：`engine.py::_dispatch`（L478）一长串 `elif action == ...`，`expect_*` 分支用 `_expect(cond,msg)`（L123）抛 `UiRunnerError`。
- 截图现成能力：`_capture`（L643）已演示 `target.screenshot(full_page=(locator is None))`（元素/整页二选一）、
  落 `output_dir` + 经注入回调 `self._attach(name, png_bytes)` 落 allure 的完整套路，直接复用。
- 项目根：`_PROJECT_ROOT = Path(__file__).resolve().parents[3]`（L141）。
- 采集模式护栏：`mode=="collect"` 用于导出登录态，任何副作用动作应跳过（参考六期 trace 在 collect 恒不启用）。
- 报告可见性：`backend/shared/allure_store.py::case_detail`（L194）用 `_find_attachment(r, 关键字)`（L69）返回附件源文件名；
  图片经现有 `GET /{run_id}/attachment/{filename}`（放行 `.png/.jpg/...`）即可显示，**无需新端点**。
  前端 `ReportView.vue` 已用 `api.reportAttachmentUrl(...)` 显示 UI 步骤截图。
- 胶水：`tests/test_ui.py` 的 `_attach(name,data)` 回调把 png 交 `allure.attach(extension="png")`；`WebEngine.execute`（L721）
  已带 `trace/attach_trace` 等注入参数，七期新增一个可选注入参数即可。
- **依赖现状**：`requirements.txt` 无图像库，`.venv` 未装 Pillow（已核实）。视觉比对需新增 Pillow。

## 3. 依赖与环境准备（★另一台机器执行前必做）

1. `requirements.txt` 追加一行：`Pillow>=10.0`
2. 安装：`python -m pip install "Pillow>=10.0"`
3. 自检：`python -c "from PIL import Image, ImageChops; print('ok')"`

> core **懒导入** Pillow（仅在 `_expect_visual` 内 `import`），未装不影响其余单测/链路；缺失时 `_expect_visual`
> 抛 `UiRunnerError("视觉回归需要 Pillow，请执行 pip install Pillow")`（中文指引，与内核缺失提示同风格）。

## 4. 设计决策（决策完备）

### 4.1 动作契约
- 键：`expect_visual`；`desc`：`视觉回归断言`；`locator`：`False`（**可选**——传则截该元素，不传截整页）；`value`：`False`（**可选**，见下）。
- `value` 三态语义：
  - 空/缺省 → 用默认阈值 `_DEFAULT_RATIO = 0.001`（0.1%）。
  - 数字（0~1 浮点，也接受 `0.5%`/`2` 这类百分号或 >1 视为百分比）→ 允许的**差异像素占比阈值**。
  - 字符串 `update`（大小写/首尾空白不敏感）→ **强制重生基线**并判该步通过（一键更新锚点，无需手动删文件）。
- `hint`：`可选定位器（元素级视觉；不填=整页）；取值=差异阈值(0~1或%)或 update（重生基线）`。
- 加入 `ACTION_ALIASES`：`视觉/视觉断言/视觉回归/断言视觉/长这样 → expect_visual`（幂等含英文键）。
- **不**加入 `COMMON_ACTIONS`（属高级动作，归"高级"分组）。

### 4.2 比对算法（`core/engines/web/visual.py`，纯函数、可离线单测）
`compute_diff(actual_png: bytes, baseline_png: bytes, *, pixel_tolerance: int = 16, ratio_threshold: float = 0.001) -> VisualResult`
- `VisualResult`：`dataclass(passed: bool, ratio: float, diff_png: bytes | None, info: str)`。
- 步骤：两图 `Image.open(BytesIO(..)).convert("RGB")`；**尺寸不一致直接失败**（`info` 记双方尺寸，不生成 diff）。
- 一致 → `ImageChops.difference` → `.convert("L")` → `.point(lambda p: 255 if p > pixel_tolerance else 0)` 得二值掩码
  （`pixel_tolerance=16` 吸收抗锯齿/次像素渲染噪声，逐通道最大差近似）；
  `changed = mask.histogram()[255]`；`ratio = changed / (w*h)`；`passed = ratio <= ratio_threshold`。
- 失败时生成证据图 `diff_png`：把差异像素在基线底图上**标红**（`Image.new("RGB",size,(220,0,0))` + `Image.composite(red, baseline_rgb, mask)`），
  编码为 PNG 字节。通过则 `diff_png=None`。
- `info` 例：`差异像素 123/192000 = 0.0641%（阈值 0.1000%，像素容差 16）`。

### 4.3 基线存储与键（项目隔离、可复现）
- 目录：`<PROJECT_ROOT>/data/visual_baselines/<ns>/`；`ns` 为**项目命名空间**（防跨项目同 case_id 冲撞）。
- `ns` 来源：`WebEngine.execute(..., visual_ns: str = "")` 注入参数 → `UiRunner(visual_ns=...)`；
  胶水 `test_ui.py` 由用例文件路径推导 `visual_ns = Path(--file).stem`（如 `proj_66ad2f40`），取不到用 `"default"`。
- 文件名：`{safe_case_id}__step{index}.png`，`safe_case_id = re.sub(r[^0-9A-Za-z_.-], "_", case_id)`；`index` 为步骤序号（1 起）。
- **已知取舍**：在步骤序列中间插入/删除步骤会使其后 `step{index}` 基线错位 → 视为"缺基线"或"尺寸不符"而失败/重生，
  文档与 hint 提示"改动步骤顺序后对相关视觉步用 `update` 重生基线"。

### 4.4 首次 / 更新 / 采集语义
- `mode=="collect"`：跳过视觉逻辑，该步直接 `ok`（采集不产生基线副作用）。
- 基线不存在且 `value!="update"`：**生成基线**（当前截图落盘）并判通过，`entry["visual"]="baseline_created"`，`desc` 附注"基线已生成（首次）"。
- `value=="update"`：**覆盖生成基线**并判通过，`entry["visual"]="baseline_updated"`。
- 基线存在（非 update）：截当前图 → `compute_diff` → 通过则 `entry["visual"]="matched"`；失败则标红证据图经 `self._attach` 落 allure、
  并把当前实际图也落 allure（便于定位），置该步失败（`raise UiRunnerError`，文案含 ratio/阈值）。

### 4.5 失败证据留存
- 失败时 `self._attach(f"视觉差异-{case_tag}{prefix}{index}.png", diff_png)`（`prefix` 同 `_capture` 的重试标记，`case_tag` 隔离用例）。
- 另附 `self._attach(f"视觉实际-{case_tag}{prefix}{index}.png", actual_png)`（可缺省不阻塞）。
- 均为 `.png`，经现有 attachment 端点显示；**不改** trace 附件口径。

### 4.6 向后兼容底线
- 新动作只对**显式使用它的用例**生效；`_dispatch` 命中新分支前所有既有动作路径不变 → 现有单测/用例零改动。
- `compute_diff` 在独立模块，`expect_visual` 懒导入，未装 Pillow 也不影响其它链路收集与执行。
- `visual_ns` 为**可选关键字参数**（默认 `""`），存量 `execute(...)` 调用点不破。

## 5. 分阶段实现

### P1 视觉比对内核（纯，先建地基 + 单测）
- 新建 `core/engines/web/visual.py`：`VisualResult` + `compute_diff(...)`（§4.2 全量实现，含像素容差/阈值/尺寸不符/标红证据）。
- 新建 `unit_tests/test_visual_diff.py`（无浏览器，用 PIL 现造图）：
  - 完全相同 → `ratio==0, passed=True, diff_png=None`；
  - 单像素改动 + `ratio_threshold` 卡边界（`>=` 判负、更宽阈值判正）；
  - `pixel_tolerance` 抑制小差（灰度差 ≤16 不计）；
  - 尺寸不符 → `passed=False`，`info` 含两尺寸，`diff_png=None`；
  - 失败产出的 `diff_png` 可被 `Image.open` 读回且尺寸=原图。

### P2 引擎与接入
- `engine.py`：
  - `UiRunResult` 无需改（证据走附件，不改结果形状）。
  - `UiRunner.__init__` 增 `visual_ns: str = ""` 与可选 `visual_dir: Path | None = None`（后者供单测覆写落盘根；None 时取 `_PROJECT_ROOT/data/visual_baselines/<ns or default>`）。
  - `_dispatch` 增 `elif action == "expect_visual": self._expect_visual(locator, value, entry)`。
  - 新方法 `_expect_visual(locator, value, entry)`：按 §4.3/4.4 解析 ns/键/阈值/update；`mode==collect` 跳过；
    截当前图（复用 `target.screenshot`，`target=locator or self.page`，`full_page=(locator is None)`）；
    缺基线→生成+通过；`update`→覆盖+通过；否则调 `visual.compute_diff`，失败经 `self._attach` 落证据并 `raise UiRunnerError`。
    Pillow 缺失在此抛中文 `UiRunnerError`。
  - `WebEngine.execute(...)` 增形参 `visual_ns: str = ""`（透传 `UiRunner`）。
- `backend/ui/ui_case_store.py`：`ACTIONS["expect_visual"]`（§4.1）+ `ACTION_ALIASES` 追加；（`meta/校验/描述`自动跟随）。
- `tests/test_ui.py`：由 `--file` 推导 `visual_ns` 并 `get_engine("web").execute(..., visual_ns=visual_ns)`（沿用现有注入风格）。
- 单测 `unit_tests/test_ui_visual.py`（假 driver，覆写 `visual_dir=tmp`）：
  - 假 `page.screenshot/locator.screenshot` 返回固定 PNG（PIL 现造）；
  - 无基线 → 生成文件且步 ok；同图再跑 → matched；喂不同图 → 该步失败且 `attach` 回调收到 `视觉差异-*`；
  - `value="update"` → 覆盖且 ok；`value="0.5"`（百分比/浮点）解析；`mode=collect` 跳过不产文件。

### P3 报告可见性
- `backend/shared/allure_store.py::case_detail`：`detail["visual_diff"] = _find_attachment(r, "视觉差异")`（无则 None，存量形状不变）。
- `platform-web/src/types.ts`：`RunCaseDetail` 增 `visual_diff?: string | null`。
- `platform-web/src/views/ReportView.vue`：UI 步骤 tab 内、trace 块附近，`v-if="detail.visual_diff"` 用 `api.reportAttachmentUrl(runId, detail.visual_diff)` 显示 `<img>` 缩略 + 文案"视觉回归差异（红色为不一致像素）"。
  （`reportAttachmentUrl` 已能取任意 results 内 png，无需新端点。）

### P4 冒烟与动作目录
- `tools/smoke_ui_actions.py`：SABC 目录补一条 `expect_visual` 用例（对静态 fixture 整页；**注入临时 `visual_ns`/覆写基线目录**，跑完清理，避免污染仓库）。
- 新建 `tools/smoke_ui_visual.py`（端到端，真实浏览器 + 本地 fixture，服务方式沿用 `smoke_ui_actions.serve_fixtures`）：
  1) 首次跑 → 断言 `data/visual_baselines/<ns>/*.png` 生成、用例通过；
  2) 立即二跑 → matched 通过；
  3) 读 `case_detail` 断言通过用例 `visual_diff is None`；
  4) 造一个"改 fixture 后与旧基线不符"的失败：用**内存里预置一张不符基线**到临时 ns，跑该用例 → 失败且 `case_detail.visual_diff` 非空 → 经 `attachment` 端点能取到 png（HTTP 200，魔数 PNG）。
  5) 清理临时 ns 目录。

## 6. 验收与门禁（全绿方可提交）
1. 单测：`pytest unit_tests`（存量 265 全过 + 新增 `test_visual_diff` / `test_ui_visual` 通过），零改存量。
2. 收集：`pytest --collect-only -q tests/test_ui.py --file=<某proj json>` 正常收集。
3. 前端：`cd platform-web && npm run build` 成功。
4. 后端：起服务 → `/openapi.json` 无 500、`paths` 数不减少（本期不加端点，数量持平）、`/health` 200。
5. 冒烟：`smoke_ui_visual`、`smoke_ui_actions`、`smoke_ui`、`smoke_ui_session` 全绿（0 失败）。
6. 页面：报告页对"首次通过""失败含差异图"两类用例可视化正常、无 500。
7. 全绿后**经用户确认**再提交并推送 origin。

## 7. 非目标（本期裁剪）
- 不做基线管理的 UI（列表/一键 update 按钮）——本期以 `value=update` + 手工删文件即可；留后续期。
- 不做区域裁剪/遮罩忽略区（mask）配置；不做多分辨率/多浏览器各存一套基线（当前一套基线对应"运行它的那台渲染环境"）。
- 不做视频/录屏、trace 内嵌可视化回放（六期外另议）。
- 不动元素拾取/录制生成、App 引擎、存储收敛。

## 8. 另一台机器执行清单（Quickstart）
1. 拉分支 `feat/ui-automation` 到最新。
2. `python -m pip install -r requirements.txt`（含新增 Pillow）；确保 Playwright 内核可用（本机 Chrome 回退或 `playwright install chromium`）。
3. 按 P1→P2→P3→P4 顺序实现；每阶段跑对应单测。
4. 跑第 6 节门禁全绿。
5. 与用户确认后 `git add -A` + 提交 + `git push origin feat/ui-automation`。
6. 基线目录 `data/visual_baselines/` 是否入库：真实锚点**建议**随用例一起提交（回归金标准）；
   冒烟产出的临时 ns 目录务必清理，不入库。
