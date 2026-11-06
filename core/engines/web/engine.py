# -*- coding: utf-8 -*-
"""UI（web）用例执行器：Playwright sync API 的步骤解释器。

设计：与接口链路（core.engines.api）分轨、分目录存放，但同为"给定用例数据 + 变量池 → 逐步执行 → 结果对象"。
本模块不依赖 allure / pytest，报告增强通过注入的回调完成（step=上下文管理器工厂、
attach=(name, png_bytes) 落图），由 tests/test_ui.py 负责胶水，保持 core 纯净、可单测。

动作集与定位器前缀口径与 backend.ui_case_store.ACTIONS / _LOCATOR_PREFIXES 一致（二者是唯二消费方）。

登录态复用：引用了 case.session 的用例，通过注入的 session_provider 解析出 Playwright
storage_state 实体路径并在 new_context 时加载（开机即已登录）；mode="collect" 则在步骤全部成功后
用 context.storage_state(path=..) 导出登录态，交 session_committer 原子落盘。provider/committer
均由上层注入，core 不依赖 backend；不引用会话的存量用例行为保持不变。

执行提速：可注入 browser_pool（BrowserPool）。有池时浏览器跨用例复用——每条用例只
new_context（context 级隔离原样保留）并在收尾**只关 context 不关 browser**；无池时保持逐条
独立启停（向后兼容底线）。采集模式(mode="collect")永不进池：同步短任务独立启停，
崩溃不牵连批次。
"""
from __future__ import annotations

import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

from core.variable import render

if TYPE_CHECKING:  # 仅类型标注：core 不强依赖池实现，运行期按鸭子类型消费
    from core.engines.web.browser_pool import BrowserPool

# 步骤默认操作超时（毫秒）：定位/断言等待上限，环境慢时可在用例层调整
_DEFAULT_TIMEOUT_MS = 8000

# 截图默认落盘目录（无 allure 钩子的独立/CLI 运行也会写图到此；reports 已 gitignore）
DEFAULT_OUTPUT_DIR = Path("reports") / "ui_screenshots"


@dataclass
class UiRunResult:
    case_id: str
    ok: bool = False
    error: Optional[str] = None
    steps: list[dict] = field(default_factory=list)   # 步骤轨迹（含 action/locator/ok/耗时/截图名）
    duration: float = 0.0
    extracted: dict = field(default_factory=dict)      # 本条用例提取到的变量（已写入 ctx）
    storage_state: Optional[str] = None                # 复用时加载的登录态实体路径（None=裸上下文）
    collected: Optional[str] = None                    # 采集模式导出的临时 storage_state 路径（供回写器落盘）
    # 重试：attempts=实际尝试次数（1=未重试，与存量语义一致）；
    # attempt_history=此前各次失败摘要（末次结果不重复入列），供报告展示「重试 k 次后通过」。
    attempts: int = 1
    attempt_history: list[dict] = field(default_factory=list)
    # 观测与诊断：失败时导出的 Playwright trace zip 字节（仅末次失败留存，交上层回调落 allure）
    trace_blob: Optional[bytes] = None


class UiRunnerError(Exception):
    """浏览器/动作层的可恢复错误，统一转成用例失败而非崩溃。"""


# 浏览器「中途崩溃/断连」类报错特征：命中即标 bundle dead，下次 acquire 触发重启（安全阀）
_BROWSER_DEAD_HINTS = (
    "target closed", "browser has been closed", "browser closed",
    "connection closed", "has been closed", "browsercontext has been closed",
)


def _looks_browser_dead(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in _BROWSER_DEAD_HINTS)


def _resolve_locator(page, locator: str):
    """把平台定位器语法解析为 Playwright Locator。

    前缀：css=（裸选择器缺省即按 css）/ xpath= / text= 直接交给 page.locator（Playwright
    原生理解这三类 engine）；role=button,name=登录 解析为 get_by_role。
    """
    if not locator:
        raise UiRunnerError("该动作需要定位器，但定位为空")
    if locator.startswith("role="):
        body = locator[len("role="):]
        parts = [p for p in body.split(",")]
        role = parts[0].strip()
        kwargs = {}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "name":
                    kwargs["name"] = v
                elif k in ("exact",):
                    kwargs["exact"] = v.lower() == "true"
        return page.get_by_role(role, **kwargs)
    # 稳定标识方言：testid/placeholder/alt 走 Playwright 语义化 getter（Page 与 Frame 均支持）
    if locator.startswith("testid="):
        return page.get_by_test_id(locator[len("testid="):].strip())
    if locator.startswith("placeholder="):
        return page.get_by_placeholder(locator[len("placeholder="):].strip())
    if locator.startswith("alt="):
        return page.get_by_alt_text(locator[len("alt="):].strip())
    # css= 去掉前缀（css 是默认引擎）；xpath= / text= 保留原样交给 locator()
    if locator.startswith("css="):
        return page.locator(locator[len("css="):])
    return page.locator(locator)


def _join_url(value: str, base_url: str) -> str:
    value = (value or "").strip()
    if "://" in value:  # 已带协议（http/https/file 等）视为绝对地址
        return value
    base = (base_url or "").rstrip("/")
    if not base:
        raise UiRunnerError(f"相对地址 {value!r} 需要在 config.yaml 中配置环境 base_url")
    return f"{base}/{value.lstrip('/')}"


def _expect(cond: bool, message: str) -> None:
    if not cond:
        raise UiRunnerError(message)


# Playwright 自带内核未下载时的报错特征（据此触发本机 Chrome 回退）
_MISSING_KERNEL_HINTS = (
    "executable doesn't exist", "please run the following command",
    "playwright install", "host executables", "browserbridge",
)


def _looks_missing_kernel(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in _MISSING_KERNEL_HINTS)


# 项目根（core/engines/web/engine.py 上溯三级）：供 upload 相对路径解析
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _accept_dialog(dialog) -> None:
    """默认放行原生 dialog（alert/confirm/beforeunload）；无法 accept 时静默。"""
    try:
        dialog.accept()
    except Exception:  # noqa: BLE001 dialog 已被处理/页面关闭均忽略
        pass


def _resolve_local_path(value: str) -> str:
    """upload 取值路径：绝对路径原样，相对按项目根 join（测试平台在本机取文件，不做沙箱）。"""
    p = Path(value)
    return str(p if p.is_absolute() else _PROJECT_ROOT / p)


class UiRunner:
    """一条 UI 用例的执行器：开浏览器 → 逐步执行 → 提取变量 → 关浏览器。

    run() 不抛异常：任何步骤失败即截全屏、中断后续步骤，把结果写进 UiRunResult。
    """

    def __init__(self, ctx, base_url: str, attach: Optional[Callable[[str, bytes], None]] = None,
                 step: Optional[Callable[[str], "object"]] = None,
                 output_dir: Optional[Path] = DEFAULT_OUTPUT_DIR,
                 mode: str = "normal",
                 session_provider: Optional[Callable[[dict], tuple]] = None,
                 session_committer: Optional[Callable[[dict, str], None]] = None,
                 browser_pool: Optional["BrowserPool"] = None,
                 attempt: int = 1,
                 trace: str = "off"):
        self.ctx = ctx
        self.base_url = base_url
        self._attach = attach
        self._step = step
        self.output_dir = output_dir
        # mode: normal=按用例引用加载登录态；collect=跑完步骤后导出登录态（采集用例从裸浏览器起步）。
        # session_provider(case)->(path|None, err|None)：把 case.session 解析成可加载的实体路径；
        # session_committer(case, tmp_path)：采集成功后把临时文件原子落盘并回写注册表。
        # 二者均由上层（tests/test_ui.py + conftest）注入，core 不直接依赖 backend.storage 层。
        self.mode = mode
        self._session_provider = session_provider
        self._session_committer = session_committer
        # browser_pool：注入即跨用例复用浏览器进程；采集模式强制不进池（独立启停隔离风险）。
        self._browser_pool = browser_pool
        # attempt：第几次尝试（1 起）。仅影响截图命名（>1 时加「第N试」前缀），
        # 保证 retry=0 的存量用例截图命名保持一致。
        self.attempt = max(1, int(attempt))
        # trace 留存模式（"off" / "on_failure"）；采集模式恒不启用
        self._trace = (trace or "off").lower()
        # 截图名中的用例标识（run() 开头从 case 回填）：不同用例同名步骤截图不再互相覆盖
        self._case_tag = ""
        # 「活动 target」模型：self.page=当前根标签页（页面级动作作用域），
        # self.target=当前定位作用对象（Page 或 Frame），定位/断言/提取都在其上解析。
        # 初始 target is page；未用 switch_frame/switch_window 时行为逐条等价（向后兼容底线）。
        self.page = None
        self.target = None
        self._context = None

    def _step_cm(self, label: str):
        return self._step(label) if self._step else nullcontext()

    def run(self, case: dict) -> UiRunResult:
        result = UiRunResult(case_id=case.get("case_id", ""), ok=False)
        self._case_tag = str(case.get("case_id") or "")
        browser_name = (case.get("browser") or "chromium").lower()
        headless = bool(case.get("headless", True))
        start = time.perf_counter()
        pw = None
        browser = None
        page = None
        context = None
        bundle = None
        tracing_on = False
        # 采集模式永不进池（设计护栏：采集是同步短任务，崩溃不牵连批次）；无池 = 逐条独立启停现状。
        use_pool = self._browser_pool is not None and self.mode != "collect"
        try:
            # 登录态解析前置到启动浏览器之前：引用会话且解析失败即中断，既不浪费一次浏览器启动，
            # 也不会把「未登录」当「已登录」继续跑。采集模式从裸浏览器起步（要从不登录走到登录再导出），
            # 故忽略 case.session。未引用会话的用例走 (None, None)，行为保持不变。
            if self.mode != "collect":
                storage_state, ss_err = self._resolve_storage_state(case)
                if ss_err:
                    raise UiRunnerError(ss_err)
                result.storage_state = storage_state

            if use_pool:
                # 复用链路：browser 跨用例常驻，本条用例只领一个新 context —— 隔离语义与逐条启动一致
                bundle = self._browser_pool.acquire(browser=browser_name, headless=headless)
                context = bundle.new_context(result.storage_state)
            else:
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                browser = self._launch(pw, browser_name, headless)
                context = self._new_context(browser, result.storage_state)
            page = context.new_page()
            page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
            self.page = page
            self.target = page
            self._context = context
            # 默认放行原生 dialog（alert/confirm/beforeunload），多数流程需点“确定”；
            # 此前 Playwright 默认 dismiss 会静默取消。需 dismiss 的场景留后续显式动作。
            page.on("dialog", _accept_dialog)
            # context 建立后按需启用 trace（假对象/旧内核失败自动降级为不录制）
            tracing_on = self._maybe_start_tracing(context)

            steps = case.get("steps") or []
            for i, step in enumerate(steps, 1):
                result.steps.append(self._run_step(step, i))
                if result.steps[-1]["ok"] is False:
                    # 失败即截全屏并中断后续步骤
                    self._capture(result.steps[-1], f"步骤{i}失败")
                    break

            steps_ok = all(s["ok"] for s in result.steps)

            # 变量提取（仅在未失败时进行）
            if steps_ok:
                self._extract(case.get("extract") or [], result)

            # 采集模式：步骤全部成功 = 已完成登录，导出 storage_state 交回写器落盘
            if self.mode == "collect" and steps_ok:
                self._collect(context, case, result)

            result.ok = steps_ok
            if not result.ok:
                bad = next(s for s in result.steps if not s["ok"])
                result.error = bad.get("error") or f"步骤 {bad['index']}（{bad.get('action')}）失败"
            elif self.mode == "normal" and page is not None:
                # 通过的用例补一张终态全屏：报告里每条用例至少有一屏现场可核验执行正确性
                # （失败用例已有失败截图；采集模式是短任务不补）
                final_entry = {"index": len(result.steps) + 1, "action": "final_shot",
                               "action_desc": "终态截图（自动）", "locator": None, "value": "",
                               "desc": "", "ok": True, "duration_ms": 0,
                               "error": None, "screenshot": None}
                self._capture(final_entry, "终态")
                result.steps.append(final_entry)
        except UiRunnerError as e:
            result.error = str(e)
            logger.error("[{}] UI 执行错误: {}", case.get("case_id"), e)
        except Exception as e:  # noqa: BLE001 浏览器/动作异常兜底为用例失败
            result.error = f"UI 执行异常: {type(e).__name__}: {e}"
            if page is not None:
                self._capture((result.steps[-1] if result.steps else {}), "异常现场")
            logger.error("[{}] UI 执行异常: {}", case.get("case_id"), e)
            # 崩溃/断连类异常：标记该束已死，下一条用例 acquire 时自动换新（环境噪声不累积）
            if use_pool and bundle is not None and _looks_browser_dead(result.error):
                self._browser_pool.invalidate(bundle)
        finally:
            # 本次失败且已录 trace，在关闭 context 前导出 zip 字节（末次失败留存交上层）；
            # 通过则显式 stop 丢弃（避免真实浏览器「tracing 未关闭」告警，不留文件）。
            if tracing_on and context is not None:
                if not result.ok:
                    blob = self._export_trace(context)
                    if blob:
                        result.trace_blob = blob
                else:
                    self._stop_tracing_discard(context)
            if use_pool:
                # 只关 page/context，绝不关 browser/playwright —— 这正是池折叠掉的固定开销所在。
                # context 关闭失败也不放过 page 的回收尝试；bundle 是否报废由下次 acquire 探活决定。
                for closer in (lambda: page and page.close(),
                               lambda: context and context.close()):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                for closer in (lambda: page and page.close(),
                               lambda: browser and browser.close(),
                               lambda: pw and pw.stop()):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
            result.duration = time.perf_counter() - start
        return result

    # ---------- Trace（观测与诊断） ----------

    def _maybe_start_tracing(self, context) -> bool:
        """trace 开启且 context 支持时启动 tracing；假对象/旧内核异常一律视作未启动（降级不影响用例）。"""
        if self._trace != "on_failure" or self.mode == "collect" or context is None:
            return False
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            return True
        except Exception as e:  # noqa: BLE001 无 trace 能力：本次不记录，不阻断执行
            logger.warning("[trace] 启动 tracing 失败，跳过本次 trace 留存: {}", e)
            return False

    def _export_trace(self, context) -> Optional[bytes]:
        """停止 tracing 并读回 trace zip；任何异常返回 None（trace 是锦上添花，绝不左右判定）。"""
        tmp = None
        try:
            base = self.output_dir if self.output_dir is not None else Path(tempfile.gettempdir())
            base.mkdir(parents=True, exist_ok=True)
            tmp = base / f".trace-{self._case_tag or 'case'}-{int(time.time() * 1000)}.zip"
            context.tracing.stop(path=str(tmp))
            return tmp.read_bytes()
        except Exception as e:  # noqa: BLE001 trace 导出失败不影响用例成败
            logger.warning("[trace] 导出失败（忽略）: {}", e)
            return None
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _stop_tracing_discard(self, context) -> None:
        """用例通过时显式停止 tracing 但不导出文件（不留垃圾，不触发未关闭告警）。"""
        try:
            context.tracing.stop()
        except Exception as e:  # noqa: BLE001 丢弃失败无关紧要
            logger.debug("[trace] 通过用例停止 tracing 忽略: {}", e)

    # ---------- 登录态（storage_state）解析与采集 ----------

    def _resolve_storage_state(self, case: dict) -> tuple:
        """把 case.session 解析成可加载的 storage_state 路径。返回 (path, error)。

        path 有效即加载；error 非空即用例失败（带中文指引）。未引用会话 → (None, None)。
        解析委托注入的 session_provider（由 conftest/test_ui 用 backend.ui_session_store 实现），
        core 不直接依赖 backend，保持纯净可单测。
        """
        sid = case.get("session")
        if not sid:
            return None, None
        if self._session_provider is None:
            return None, f"用例引用了登录态会话 {sid!r}，但执行器未注入会话解析器（session_provider）"
        try:
            return self._session_provider(case)
        except Exception as e:  # noqa: BLE001 provider 内部异常统一转用例失败
            return None, f"登录态会话 {sid!r} 解析异常: {type(e).__name__}: {e}"

    @staticmethod
    def _new_context(browser, storage_state: Optional[str] = None):
        """引用有效登录态时以 storage_state 开机加载（cookie+localStorage），否则裸上下文。"""
        if storage_state:
            return browser.new_context(storage_state=storage_state)
        return browser.new_context()

    def _collect(self, context, case: dict, result: UiRunResult) -> None:
        """采集模式：导出当前（已登录）context 的 storage_state 到临时文件，交回写器原子落盘。

        session_committer 负责临时文件 → 实体文件 os.replace + 回写注册表 captured_at；
        committer 缺失视为配置错误（用例失败），临时文件不留垃圾。
        """
        if self._session_committer is None:
            raise UiRunnerError(
                f"采集模式未注入 session_committer，无法落盘登录态（用例 {case.get('case_id')!r}）")
        tmp = self._collect_tmp(case)
        try:
            context.storage_state(path=str(tmp))
            self._session_committer(case, str(tmp))
            result.collected = str(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)  # committer 已原子移走或失败，幂等清理
            except OSError:
                pass

    def _collect_tmp(self, case: dict) -> Path:
        base = self.output_dir if self.output_dir is not None else Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        return base / f".collect-{case.get('case_id', 'case')}-{int(time.time() * 1000)}.json"

    @staticmethod
    def _launch(pw, name: str, headless: bool = True, channel: Optional[str] = None):
        launcher = getattr(pw, name, None)
        if launcher is None:
            raise UiRunnerError(f"不支持的浏览器: {name}（可用 chromium/firefox/webkit）")
        if channel:
            # 显式通道（如本机 Chrome）：失败即抛，不再走内核缺失回退（调用方已明确指定）
            return launcher.launch(headless=headless, channel=channel)
        try:
            return launcher.launch(headless=headless)
        except Exception as e:  # noqa: BLE001 内核缺失时走兼容回退
            msg = str(e)
            if name == "chromium" and _looks_missing_kernel(msg):
                # 自带 chromium 未下载 → 自动回退到本机已装的 Google Chrome（channel），
                # 直接复用用户正式版本（如 156.0.8078.48），无需联网下载 Playwright 内核
                return UiRunner._launch_local_chrome(launcher, headless, cause=e)
            if _looks_missing_kernel(msg):
                raise UiRunnerError(f"浏览器内核缺失，请先执行 `python -m playwright install {name}`：{msg}") from e
            raise

    @staticmethod
    def _launch_local_chrome(launcher, headless: bool, cause: Exception):
        """回退：依次尝试本机 chrome / chrome-beta，命中即返回；都不可用给出中文指引。"""
        tried: list[str] = []
        for channel in ("chrome", "chrome-beta"):
            try:
                browser = launcher.launch(headless=headless, channel=channel)
                logger.info("未找到 Playwright 自带 Chromium，已回退到本机 Google Chrome（channel={}, version={}）",
                            channel, browser.version)
                return browser
            except Exception as ce:  # noqa: BLE001
                tried.append(f"{channel}: {(str(ce).splitlines() or [''])[0]}")
        raise UiRunnerError(
            "Playwright 自带 Chromium 未安装，且本机未检测到可复用的 Google Chrome。\n"
            "  方式一（推荐，无需下载）：安装/保留本机 Google Chrome，本平台会自动复用；\n"
            "  方式二：执行 `python -m playwright install chromium` 下载自带内核。\n"
            f"  回退尝试：{' ｜ '.join(tried)}"
        ) from cause

    # ---------- 单步 ----------

    def _run_step(self, step: dict, index: int) -> dict:
        action = step.get("action")
        raw_locator = step.get("locator")
        raw_value = step.get("value")
        label = f"{index}. {action}"
        if step.get("desc"):
            label += f" · {step['desc']}"
        entry = {"index": index, "action": action, "action_desc": _action_desc(action),
                 "locator": raw_locator, "value": _stringify(raw_value),
                 "desc": step.get("desc") or "", "ok": False, "duration_ms": 0,
                 "error": None, "screenshot": None}
        t0 = time.perf_counter()
        try:
            with self._step_cm(label):
                # 变量替换只作用于取值与地址类定位（定位器一般静态）
                value = render(raw_value, self.ctx) if isinstance(raw_value, str) else raw_value
                # 定位在“活动 target”（Page 或 Frame）上解析；页面级动作另走 self.page
                locator = _resolve_locator(self.target, raw_locator) if raw_locator else None
                self._dispatch(locator, action, value, entry)
                entry["ok"] = True
        except Exception as e:  # noqa: BLE001 单步失败：记录 + 截图，不炸整条
            entry["error"] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else e}"[:400]
            logger.warning("[步骤失败] {} -> {}", label, entry["error"])
        finally:
            entry["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        return entry

    def _dispatch(self, locator, action: str, value, entry: dict) -> None:
        page = self.page
        # 定位/断言/提取类走 self.target；页面级（goto/navigate/等待/url/标题/截图）走 self.page
        if action == "goto":
            page.goto(_join_url(str(value), self.base_url))
        elif action == "click":
            locator.click()
        elif action == "dblclick":
            locator.dblclick()
        elif action == "right_click":
            locator.click(button="right")
        elif action == "fill":
            locator.fill("" if value is None else str(value))
        elif action == "press":
            locator.press(str(value))
        elif action == "select":
            locator.select_option(label=str(value))
        elif action == "check":
            locator.check()
        elif action == "uncheck":
            locator.uncheck()
        elif action == "hover":
            locator.hover()
        elif action == "scroll_to":
            locator.scroll_into_view_if_needed()
        elif action == "upload":
            locator.set_input_files(_resolve_local_path(str(value)))
        elif action == "drag_to":
            if not value:
                raise UiRunnerError("拖拽需要取值：目标元素定位器")
            locator.drag_to(_resolve_locator(self.target, str(value)))
        elif action == "wait_for":
            locator.wait_for(state="visible")
        elif action == "wait_state":
            state = str(value).strip().lower()
            if state not in ("visible", "hidden", "attached", "detached"):
                raise UiRunnerError(f"等待元素状态非法: {state!r}（可用 visible/hidden/attached/detached）")
            locator.wait_for(state=state)
        elif action == "wait_load":
            st = str(value).strip().lower()
            if st not in ("load", "domcontentloaded", "networkidle"):
                raise UiRunnerError(f"等待加载状态非法: {st!r}（可用 load/domcontentloaded/networkidle）")
            page.wait_for_load_state(st)
        elif action == "sleep":
            try:
                ms = int(float(value))
            except (TypeError, ValueError):
                raise UiRunnerError(f"固定等待毫秒数非法: {value!r}")
            page.wait_for_timeout(max(0, ms))
        elif action == "navigate":
            op = str(value).strip().lower()
            if op == "back":
                page.go_back()
            elif op == "forward":
                page.go_forward()
            elif op == "reload":
                page.reload()
            else:
                raise UiRunnerError(f"导航动作非法: {op!r}（可用 back/forward/reload）")
        elif action == "switch_frame":
            handle = locator.element_handle()
            frame = handle.content_frame() if handle else None
            if frame is None:
                raise UiRunnerError("定位器未指向含 iframe 的元素，无法切入帧")
            self.target = frame
        elif action == "switch_main":
            self.target = page
        elif action == "switch_window":
            pages = self._context.pages if self._context else [page]
            sel = str(value).strip().lower()
            if sel == "last":
                idx = len(pages) - 1
            else:
                try:
                    idx = int(sel)
                except ValueError:
                    raise UiRunnerError(f"切换标签页取值非法: {value!r}（应为序号或 last）")
            if not (0 <= idx < len(pages)):
                raise UiRunnerError(f"标签页序号 {idx} 越界（当前共 {len(pages)} 个）")
            self.page = pages[idx]
            self.target = self.page
        elif action == "screenshot":
            # 显式截图：有定位器截元素，否则截整页；结果记入当前步骤轨迹
            self._capture(entry, "手动", locator=locator)
        elif action == "expect_visible":
            _expect(locator.is_visible(), "元素不可见")
        elif action == "expect_hidden":
            _expect(not locator.is_visible(), "元素仍可见")
        elif action == "expect_enabled":
            _expect(locator.is_enabled(), "元素不可交互")
        elif action == "expect_text":
            txt = locator.inner_text()
            _expect(str(value) in txt, f"文本不含期望值：期望包含 {value!r}，实际 {txt!r}")
        elif action == "expect_count":
            try:
                want = int(float(value))
            except (TypeError, ValueError):
                raise UiRunnerError(f"断言元素数量取值非法: {value!r}")
            got = locator.count()
            _expect(got == want, f"元素数量期望 {want}，实际 {got}")
        elif action == "expect_value":
            actual = locator.input_value()
            _expect(str(value) == actual, f"输入值期望 {value!r}，实际 {actual!r}")
        elif action == "expect_attr":
            name, sep, want = str(value).partition("=")
            if not sep:
                raise UiRunnerError(f"断言属性取值需 '属性名=期望值'，实际 {value!r}")
            actual = locator.get_attribute(name.strip())
            _expect(actual == want, f"属性 {name.strip()!r} 期望 {want!r}，实际 {actual!r}")
        elif action == "expect_url":
            _expect(str(value) in page.url, f"当前地址 {page.url!r} 不含期望 {value!r}")
        elif action == "expect_title":
            title = page.title()
            _expect(str(value) in title, f"标题 {title!r} 不含期望 {value!r}")
        else:
            raise UiRunnerError(f"未知动作: {action}")

    # ---------- 变量提取 ----------

    def _extract(self, extract: list, result: UiRunResult) -> None:
        for item in extract:
            var = item.get("var")
            source = item.get("source", "")
            value = self._read_source(source)
            if value is not None:
                self.ctx.set(var, value)
                result.extracted[var] = value
                logger.info("[{}] 变量提取 {}={}", result.case_id, var, value)

    def _read_source(self, source: str):
        """source 语法：url / title / cookie:<name> / storage:<key> / text|<locator> / input_value|<locator> / attr|<sel>:<name>。

        定位类来源在“活动 target”（Page 或 Frame）上解析；url/title/storage 属页面级，取 self.page。
        """
        source = (source or "").strip()
        if source == "url":
            return self.page.url
        if source == "title":
            return self.page.title()
        if source.startswith("cookie:"):
            name = source[len("cookie:"):].strip()
            for ck in self.page.context.cookies():
                if ck.get("name") == name:
                    return ck.get("value")
            return None
        if source.startswith("storage:"):
            key = source[len("storage:"):].strip()
            return self.page.evaluate("(k) => localStorage.getItem(k)", key)
        if "|" in source:
            kind, _, loc = source.partition("|")
            locator = _resolve_locator(self.target, loc.strip())
            kind = kind.strip()
            if kind == "text":
                return locator.inner_text()
            if kind in ("input_value", "value"):
                return locator.input_value()
            if kind == "attr":
                # attr|css=#x:data-id 形式取属性
                sel, _, attr = loc.partition(":")
                return locator.get_attribute(attr.strip()) if attr else None
            raise UiRunnerError(f"未知提取来源类型: {kind}")
        raise UiRunnerError(f"变量提取 source 格式非法: {source!r}")

    # ---------- 截图 ----------

    def _capture(self, entry: dict, name_suffix: str, locator=None) -> None:
        """截当前步现场：默认落盘 output_dir；有 allure 钩子则同时作为附件写入。

        整页截图作用于 self.page（当前活动标签页）；元素截图作用于传入的 locator。
        任一途径成功即把截图名记入步骤轨迹（供报告页回指）；截图失败不影响执行结论。
        """
        if entry is None or self.page is None:
            return
        try:
            target = locator if locator is not None else self.page
            png = target.screenshot(full_page=(locator is None))
            prefix = f"第{self.attempt}试-" if self.attempt > 1 else ""  # retry=0 存量命名不变
            tag = f"{self._case_tag}-" if self._case_tag else ""  # 用例隔离：跨用例同序号截图不互盖
            att_name = f"截图-{tag}{prefix}{name_suffix}-{entry.get('index', 0)}.png"
            saved = False
            if self.output_dir is not None:
                try:
                    self.output_dir.mkdir(parents=True, exist_ok=True)
                    (self.output_dir / att_name).write_bytes(png)
                    saved = True
                except Exception as de:  # noqa: BLE001
                    logger.warning("截图落盘失败: {}", de)
            if self._attach is not None:
                try:
                    self._attach(att_name, png)
                    saved = True
                except Exception as ae:  # noqa: BLE001
                    logger.warning("截图附件写入失败: {}", ae)
            if saved:
                entry["screenshot"] = att_name
        except Exception as e:  # noqa: BLE001 截图失败不影响失败结论
            logger.warning("截图失败: {}", e)


def _stringify(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


_DESC_CACHE: dict[str, str] = {}


def _action_desc(action: str) -> str:
    if not _DESC_CACHE:
        try:
            from backend.ui.ui_case_store import ACTIONS  # 单一口径来源
            _DESC_CACHE.update({a: m["desc"] for a, m in ACTIONS.items()})
        except Exception:  # noqa: BLE001 core 不应强依赖 backend，降级用动作名
            pass
    return _DESC_CACHE.get(action, action)


from core.engines.base import Engine  # noqa: E402


class WebEngine(Engine):
    """UI（web）自动化引擎的注册表入口（身份 + 分流 + 统一校验/执行入口）。

    validate/execute 均为薄封装，分别委托 ui_case_store.validate_case 与 UiRunner，行为与直调一致。
    """

    key = "web"
    label = "UI 自动化"
    test_module = str(Path("tests") / "test_ui.py")

    def matches(self, path: Path) -> bool:
        # 与旧 main.py 分流口径一致：.json 用例文件按 UI 链路走 test_ui
        return path.suffix.lower() == ".json"

    def validate(self, raw, **deps):
        """校验并归一化一条 UI 用例（JSON dict）→ 清洗后 dict；非法抛 UiCaseError。

        懒导入 backend.ui.ui_case_store（core 不强依赖 backend），与 _action_desc 同一口径。
        """
        from backend.ui.ui_case_store import validate_case  # 单一口径来源
        return validate_case(raw)

    def execute(self, case: dict, ctx, *, base_url: str,
                attach: Optional[Callable[[str, bytes], None]] = None,
                step: Optional[Callable[[str], object]] = None,
                mode: str = "normal",
                session_provider: Optional[Callable[[dict], tuple]] = None,
                session_committer: Optional[Callable[[dict, str], None]] = None,
                browser_pool: Optional["BrowserPool"] = None,
                retry: Optional[int] = None,
                trace: str = "off",
                attach_trace: Optional[Callable[[str, bytes], None]] = None,
                output_dir: Optional[Path] = DEFAULT_OUTPUT_DIR, **deps) -> UiRunResult:
        """执行一条 UI 用例 → UiRunResult。

        browser_pool：注入即启用浏览器跨用例复用；未注入的存量调用点行为不变。
        retry：失败重试次数。生效优先级 显式 retry 入参 > 用例 retry > 0；
        采集模式（mode="collect"）永不重试（设计护栏：采集失败直接反馈，交由重采/手动）。
        重试语义：任一次通过即最终通过；全失败保留末次错误与轨迹，各次失败摘要入
        attempt_history（报告页展示「重试 k 次后通过」，让 flaky 可见而非被吞）。
        """
        effective = self._effective_retry(case, mode, retry)
        result: UiRunResult = UiRunResult(case_id="")
        history: list[dict] = []
        for attempt in range(1, effective + 2):  # effective+1 次尝试（首次 + 重试）
            result = UiRunner(ctx, base_url, attach=attach, step=step, output_dir=output_dir,
                              mode=mode, session_provider=session_provider,
                              session_committer=session_committer,
                              browser_pool=browser_pool, attempt=attempt, trace=trace).run(case)
            if result.ok or attempt == effective + 1:
                break
            history.append({"attempt": attempt, "ok": False,
                            "error": result.error, "duration_ms": int(result.duration * 1000),
                            "steps_failed": sum(1 for s in result.steps if not s.get("ok"))})
            logger.warning("[{}] 第 {} 次尝试失败，重试中（剩余 {} 次）: {}",
                           case.get("case_id"), attempt, effective + 1 - attempt, result.error)
        result.attempts = len(history) + 1
        result.attempt_history = history
        if result.attempts > 1 and not result.ok:
            result.error = f"重试 {result.attempts - 1} 次后仍失败：{result.error}"
        # 用例最终失败才落 trace 附件（trace_blob 仅在该次 run 失败时留），中间失败末次通过不留
        if not result.ok and result.trace_blob and attach_trace is not None:
            try:
                attach_trace("UI失败trace", result.trace_blob)
            except Exception as e:  # noqa: BLE001 trace 附件落盘失败不改用例成败
                logger.warning("[trace] 失败 trace 落附件失败（忽略）: {}", e)
        return result

    @staticmethod
    def _effective_retry(case: dict, mode: str, retry: Optional[int]) -> int:
        """重试上限归一：0..3；采集模式恒 0。入参 > 用例字段 > 0（批次默认由调用方折算后传入 retry）。"""
        if mode == "collect":
            return 0
        raw = retry if retry is not None else (case or {}).get("retry")
        try:
            val = int(raw or 0)
        except (TypeError, ValueError):
            val = 0
        return max(0, min(val, 3))
