# -*- coding: utf-8 -*-
"""core.engines.web.engine（UiRunner）动作解释器功能测试：对本地 fixture 页跑通全部 12 动作 + 变量提取。

零外部依赖（本地 http 服务 fixture 页，以便验证 cookie 提取）。若浏览器未就绪则整体 skip，不阻断无 UI 环境下的回归。
    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_runner.py -v
"""
import functools
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines.web.engine import UiRunner, UiRunResult, UiRunnerError  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    """服务 fixture 静态页并静音访问日志。"""

    def log_message(self, *args):  # noqa: D102
        pass


def _chromium_ready() -> bool:
    """与 ui_runner 同口径：先试自带 chromium，再回退本机 Google Chrome。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001
                b = pw.chromium.launch(headless=True, channel="chrome")
            b.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def base_url():
    if not _chromium_ready():
        pytest.skip("浏览器未就绪（自带 chromium 与本机 Chrome 均不可用），跳过 UI 执行器测试")
    # 以 http 方式提供 fixture 页：浏览器对 file:// 不落地 cookie，需 http 才能验证 cookie: 提取
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _steps(*s):
    return list(s)


def _run(ctx, base_url, steps, extract=None):
    case = {"case_id": "t", "browser": "chromium", "headless": True,
            "steps": steps, "extract": extract or []}
    runner = UiRunner(ctx, base_url)
    return runner.run(case)


def test_full_flow_all_actions(base_url):
    ctx = Context({"username": "qa77"}, {})
    result = _run(ctx, base_url, _steps(
        {"action": "goto", "value": "ui_demo.html"},
        {"action": "expect_visible", "locator": "css=#title"},
        {"action": "fill", "locator": "css=#username", "value": "{{username}}"},
        {"action": "fill", "locator": "css=#password", "value": "s3cret"},
        {"action": "press", "locator": "css=#password", "value": "Tab"},
        {"action": "select", "locator": "css=#role", "value": "测试"},
        {"action": "check", "locator": "css=#agree"},
        {"action": "hover", "locator": "css=#submit"},
        {"action": "click", "locator": "css=#submit"},
        {"action": "wait_for", "locator": "css=.welcome"},
        {"action": "expect_text", "locator": "css=.welcome", "value": "欢迎"},
        {"action": "expect_text", "locator": "css=#who", "value": "qa77"},
        {"action": "expect_url", "value": "?logged_in=1"},
        {"action": "screenshot"},
    ), extract=[
        {"var": "who_val", "source": "text|css=#who"},
        {"var": "uname", "source": "input_value|css=#username"},
        {"var": "sess", "source": "cookie:session_token"},
        {"var": "page_url", "source": "url"},
    ])
    assert result.ok, result.error
    assert all(s["ok"] for s in result.steps)
    assert len(result.steps) == 15  # 14 步 + 通过用例自动追加的终态截图
    # 变量提取写入池并可在后续引用
    assert ctx.get("who_val") == "qa77"
    assert ctx.get("uname") == "qa77"
    assert ctx.get("sess") == "abc123"
    assert "?logged_in=1" in ctx.get("page_url")
    # screenshot 步骤留了截图名
    assert result.steps[13]["screenshot"]
    # 终态截图：最后一步为自动补的 final_shot，携带自己的截图证据
    assert result.steps[-1]["action"] == "final_shot"
    assert result.steps[-1]["screenshot"]


def test_failure_stops_and_marks_error(base_url):
    ctx = Context({}, {})
    result = _run(ctx, base_url, _steps(
        {"action": "goto", "value": "ui_demo.html"},
        {"action": "expect_text", "locator": "css=#title", "value": "绝不存在的文本"},
        {"action": "click", "locator": "css=#submit"},  # 不该被执行（已中断）
    ))
    assert result.ok is False
    assert "期望" in (result.error or "") or "不含" in (result.error or "")
    assert len(result.steps) == 2  # 第二步失败即中断，第三步未记录
    assert result.steps[1]["ok"] is False
    assert result.steps[1]["screenshot"]  # 失败步骤有现场截图


def test_relative_url_requires_base(base_url):
    ctx = Context({}, {})
    r = UiRunner(ctx, "")  # 无 base_url
    res = r.run({"case_id": "t", "steps": [{"action": "goto", "value": "/login"}]})
    assert res.ok is False
    assert "base_url" in (res.error or "")


# ==================== 登录态（storage_state）：纯逻辑，不依赖浏览器 ====================

class _FakeBrowser:
    """只记录 new_context 收到的关键字，用于断言 storage_state 是否被传递。"""
    def __init__(self):
        self.kwargs = None

    def new_context(self, **kw):
        self.kwargs = kw
        return object()


class _FakeContext:
    """模拟 Playwright context.storage_state(path=..)：把占位内容写到指定路径。"""
    def storage_state(self, path=None):
        Path(path).write_text('{"cookies": []}', encoding="utf-8")


def test_no_session_is_backward_compatible():
    # 与旧调用方式一致（不传 provider），未引用会话 → (None, None)，后续走裸上下文
    r = UiRunner(Context({}, {}), "http://x")
    path, err = r._resolve_storage_state({"case_id": "t", "steps": []})
    assert path is None and err is None
    fb = _FakeBrowser()
    UiRunner._new_context(fb, path)
    assert fb.kwargs == {}  # 裸 new_context()，未注入 storage_state


def test_attach_valid_storage_state_path_passed_to_context(tmp_path):
    ss = tmp_path / "sid__test.json"
    ss.write_text('{"cookies": []}', encoding="utf-8")
    r = UiRunner(Context({}, {}), "http://x", session_provider=lambda case: (str(ss), None))
    path, err = r._resolve_storage_state({"case_id": "t", "session": "sid"})
    assert err is None and path == str(ss)
    fb = _FakeBrowser()
    UiRunner._new_context(fb, path)
    assert fb.kwargs == {"storage_state": str(ss)}  # 有效登录态→开机加载


def test_session_without_provider_gives_guidance():
    r = UiRunner(Context({}, {}), "http://x")  # 无 provider
    path, err = r._resolve_storage_state({"case_id": "t", "session": "sid"})
    assert path is None
    assert "未注入会话解析器" in err


def test_session_missing_marks_case_failed_without_browser():
    # 会话未采集：provider 返回中文指引 → run() 应在启动浏览器/跑步骤之前中断
    guidance = "会话 sid 尚未采集，请在『登录态』页点『立即采集』"
    r = UiRunner(Context({}, {}), "http://x", session_provider=lambda case: (None, guidance))
    res = r.run({"case_id": "t", "session": "sid", "steps": [{"action": "goto", "value": "/x"}]})
    assert res.ok is False
    assert guidance in (res.error or "")
    assert res.steps == []  # 未跑任何步骤 → 未启动浏览器（与是否安装 playwright 无关）


def test_collect_invokes_committer_and_cleans_tmp(tmp_path):
    seen = {}

    def committer(case, tmp_file):
        seen["file"] = tmp_file
        seen["exists"] = Path(tmp_file).exists()  # 导出临时文件此刻确实存在

    r = UiRunner(Context({}, {}), "http://x", output_dir=tmp_path, mode="collect",
                 session_committer=committer)
    res = UiRunResult(case_id="login_001")
    r._collect(_FakeContext(), {"case_id": "login_001"}, res)
    assert seen["exists"] is True
    assert res.collected == seen["file"]
    assert not Path(seen["file"]).exists()  # _collect 收尾幂等清理临时文件


def test_collect_without_committer_raises():
    r = UiRunner(Context({}, {}), "http://x", mode="collect")
    with pytest.raises(UiRunnerError):
        r._collect(_FakeContext(), {"case_id": "x"}, UiRunResult(case_id="x"))
