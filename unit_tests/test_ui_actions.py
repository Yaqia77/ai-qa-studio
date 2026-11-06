# -*- coding: utf-8 -*-
"""能力扩展：新增动作 / 定位方言 / 断言 / target 切换 / dialog 的纯逻辑单测。

全程喂假 page/locator/frame（记录调用），不真起浏览器：验证 _dispatch 调用正确的
Playwright API、取值归一与非法中文报错、switch_frame/main/window 后 self.target/self.page
指向正确、_accept_dialog 默认放行。与 test_ui_runner.py（真浏览器端到端）互补。

    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_actions.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines.web import engine as eng  # noqa: E402
from core.engines.web.engine import (  # noqa: E402
    UiRunner, UiRunnerError, _accept_dialog, _resolve_locator, _resolve_local_path,
)


# ==================== 假对象：记录 Playwright API 调用 ====================


class FakeLocator:
    """按需返回可配置结果，并把方法调用记入 calls（供逐动作断言映射正确）。"""

    def __init__(self, **ret):
        self.calls: list[tuple[str, dict]] = []
        self._ret = ret

    def _rec(self, cname, **kw):
        self.calls.append((cname, kw))
        # 查询类返回可配置值：默认 True（可见/可用），文本/属性/数量类默认空/0，由用例显式设定
        if cname in ("inner_text", "input_value", "get_attribute"):
            return self._ret.get(cname, "")
        if cname == "count":
            return self._ret.get(cname, 0)
        if cname in ("is_visible", "is_enabled"):
            return self._ret.get(cname, True)
        return self._ret.get(cname)

    def click(self, **kw): return self._rec("click", **kw)
    def dblclick(self): return self._rec("dblclick")
    def fill(self, v): return self._rec("fill", value=v)
    def press(self, k): return self._rec("press", key=k)
    def select_option(self, **kw): return self._rec("select_option", **kw)
    def check(self): return self._rec("check")
    def uncheck(self): return self._rec("uncheck")
    def hover(self): return self._rec("hover")
    def scroll_into_view_if_needed(self): return self._rec("scroll_into_view_if_needed")
    def set_input_files(self, p): return self._rec("set_input_files", path=p)
    def drag_to(self, other): return self._rec("drag_to", target=other)
    def wait_for(self, **kw): return self._rec("wait_for", **kw)
    def is_visible(self): return self._rec("is_visible")
    def is_enabled(self): return self._rec("is_enabled")
    def inner_text(self): return self._rec("inner_text")
    def input_value(self): return self._rec("input_value")
    def count(self): return self._rec("count")
    def get_attribute(self, name): return self._rec("get_attribute", name=name)
    def element_handle(self): return self._rec("element_handle")


class FakeFrame:
    """content_frame() 返回的帧对象：定位在其上解析即证明 target 已切入。"""

    def __init__(self, marker="frame"):
        self.marker = marker
        self.resolved: list[str] = []

    def locator(self, sel):
        self.resolved.append(sel)
        return FakeLocator()


class FakeHandle:
    def __init__(self, frame: FakeFrame | None):
        self._frame = frame

    def content_frame(self):
        return self._frame


class FakePage:
    """页面/帧双用假对象：既支持 _resolve_locator 的各 getter，也支持页面级动作。"""

    def __init__(self, url="http://x/cur", title_text="页面标题"):
        self.url = url
        self._title = title_text
        self.getters: list[tuple[str, object]] = []
        self.page_calls: list[tuple[str, dict]] = []

    # 定位 getter：记录用了哪种解析，返回带记录器的 FakeLocator
    def locator(self, sel): self.getters.append(("css", sel)); return FakeLocator()
    def get_by_test_id(self, t): self.getters.append(("testid", t)); return FakeLocator()
    def get_by_placeholder(self, p): self.getters.append(("placeholder", p)); return FakeLocator()
    def get_by_alt_text(self, a): self.getters.append(("alt", a)); return FakeLocator()
    def get_by_role(self, role, **kw): self.getters.append(("role", (role, kw))); return FakeLocator()

    # 页面级动作
    def goto(self, url): self.page_calls.append(("goto", {"url": url}))
    def reload(self): self.page_calls.append(("reload", {}))
    def go_back(self): self.page_calls.append(("go_back", {}))
    def go_forward(self): self.page_calls.append(("go_forward", {}))
    def wait_for_load_state(self, st): self.page_calls.append(("wait_for_load_state", {"state": st}))
    def wait_for_timeout(self, ms): self.page_calls.append(("wait_for_timeout", {"ms": ms}))
    def title(self): return self._title
    def evaluate(self, script, arg=None):
        self.page_calls.append(("evaluate", {"script": script, "arg": arg}))
        return "ls-value"
    def on(self, event, handler): self.page_calls.append(("on", {"event": event}))
    def set_default_timeout(self, ms): pass
    def close(self): pass


class FakeContext:
    def __init__(self, pages): self.pages = pages


def make_runner(page=None, pages=None):
    r = UiRunner(Context({}, {}), "http://x")
    page = page or FakePage()
    r.page = page
    r.target = page
    r._context = FakeContext(pages or [page])
    return r


def dispatch(r, locator, action, value=None):
    entry = {"index": 1, "action": action, "ok": False}
    r._dispatch(locator, action, value, entry)
    return entry


# ==================== P3 定位方言 → getter 映射 ====================


@pytest.mark.parametrize("locator,expect", [
    ("css=.btn", ("css", ".btn")),
    (".btn", ("css", ".btn")),                       # 裸选择器默认 css
    ("xpath=//div", ("css", "xpath=//div")),         # css getter 透传原样串（Playwright 自解析引擎）
    ("testid=login", ("testid", "login")),
    ("placeholder=请输入", ("placeholder", "请输入")),
    ("alt=头像", ("alt", "头像")),
])
def test_resolve_locator_prefix_mapping(locator, expect):
    page = FakePage()
    _resolve_locator(page, locator)
    assert page.getters[-1] == expect


def test_resolve_locator_role():
    page = FakePage()
    _resolve_locator(page, "role=button,name=登录")
    assert page.getters[-1] == ("role", ("button", {"name": "登录"}))


def test_resolve_locator_empty_raises():
    with pytest.raises(UiRunnerError):
        _resolve_locator(FakePage(), "")


# ==================== P1 交互动作 → Playwright API 映射 ====================


def test_dispatch_interactions():
    r = make_runner()
    # 动作 → 实际调用的 Playwright 方法名（多数同名，scroll_to 例外）
    method_of = {
        "click": "click", "dblclick": "dblclick", "uncheck": "uncheck",
        "check": "check", "hover": "hover", "scroll_to": "scroll_into_view_if_needed",
    }
    for action, meth in method_of.items():
        loc = FakeLocator()
        dispatch(r, loc, action)
        assert loc.calls[-1][0] == meth, action
    loc = FakeLocator()
    dispatch(r, loc, "right_click")
    assert loc.calls[-1] == ("click", {"button": "right"})


def test_dispatch_fill_press_select():
    r = make_runner()
    loc = FakeLocator(); dispatch(r, loc, "fill", "abc")
    assert loc.calls[-1] == ("fill", {"value": "abc"})
    loc = FakeLocator(); dispatch(r, loc, "press", "Enter")
    assert loc.calls[-1] == ("press", {"key": "Enter"})
    loc = FakeLocator(); dispatch(r, loc, "select", "测试")
    assert loc.calls[-1] == ("select_option", {"label": "测试"})


def test_dispatch_upload_resolves_path():
    r = make_runner()
    loc = FakeLocator(); dispatch(r, loc, "upload", "data/secret_vars.xlsx")
    passed = loc.calls[-1][1]["path"]
    assert Path(passed).is_absolute() and passed.endswith("secret_vars.xlsx")
    # 绝对路径原样
    loc2 = FakeLocator()
    abs_p = str(ROOT / "data" / "ui_sessions.json")
    dispatch(r, loc2, "upload", abs_p)
    assert loc2.calls[-1][1]["path"] == abs_p


def test_resolve_local_path_absolute_unchanged():
    # 绝对路径不被拼到项目根，仅按本机分隔符归一
    assert _resolve_local_path("C:/tmp/a.txt") == str(Path("C:/tmp/a.txt"))


def test_dispatch_drag_to_target():
    r = make_runner()
    src = FakeLocator()
    dispatch(r, src, "drag_to", "#drop-zone")
    assert src.calls[-1][0] == "drag_to"
    assert isinstance(src.calls[-1][1]["target"], FakeLocator)  # 目标经 _resolve_locator 解析
    with pytest.raises(UiRunnerError):  # 无取值报错
        dispatch(r, FakeLocator(), "drag_to", "")


# ==================== P1 等待与导航：取值归一 + 非法中文报错 ====================


def test_dispatch_wait_load():
    r = make_runner()
    for st in ("load", "domcontentloaded", "networkidle"):
        page = FakePage(); r.page = page; r.target = page
        dispatch(r, None, "wait_load", st)
        assert page.page_calls[-1] == ("wait_for_load_state", {"state": st})
    with pytest.raises(UiRunnerError, match="非法"):
        dispatch(make_runner(), None, "wait_load", "bogus")


def test_dispatch_sleep():
    page = FakePage(); r = make_runner(page=page)
    dispatch(r, None, "sleep", "1000")
    assert page.page_calls[-1] == ("wait_for_timeout", {"ms": 1000})
    with pytest.raises(UiRunnerError, match="毫秒数非法"):
        dispatch(make_runner(), None, "sleep", "abc")


def test_dispatch_navigate():
    page = FakePage(); r = make_runner(page=page)
    dispatch(r, None, "navigate", "back"); assert page.page_calls[-1] == ("go_back", {})
    dispatch(r, None, "navigate", "forward"); assert page.page_calls[-1] == ("go_forward", {})
    dispatch(r, None, "navigate", "reload"); assert page.page_calls[-1] == ("reload", {})
    with pytest.raises(UiRunnerError, match="导航动作非法"):
        dispatch(make_runner(), None, "navigate", "teleport")


def test_dispatch_wait_state():
    loc = FakeLocator()
    dispatch(make_runner(), loc, "wait_state", "visible")
    assert loc.calls[-1] == ("wait_for", {"state": "visible"})
    with pytest.raises(UiRunnerError, match="等待元素状态非法"):
        dispatch(make_runner(), FakeLocator(), "wait_state", "glow")


# ==================== P2 断言：成功 / 失败两路 ====================


def test_expect_visible_hidden_enabled():
    r = make_runner()
    dispatch(r, FakeLocator(is_visible=True), "expect_visible")          # 通过
    with pytest.raises(UiRunnerError):
        dispatch(r, FakeLocator(is_visible=False), "expect_visible")     # 失败
    dispatch(r, FakeLocator(is_visible=False), "expect_hidden")          # 不可见=通过
    with pytest.raises(UiRunnerError):
        dispatch(r, FakeLocator(is_visible=True), "expect_hidden")
    dispatch(r, FakeLocator(is_enabled=True), "expect_enabled")
    with pytest.raises(UiRunnerError):
        dispatch(r, FakeLocator(is_enabled=False), "expect_enabled")


def test_expect_text_count_value():
    r = make_runner()
    dispatch(r, FakeLocator(inner_text="欢迎 qa77"), "expect_text", "qa77")
    with pytest.raises(UiRunnerError):
        dispatch(r, FakeLocator(inner_text="欢迎"), "expect_text", "qa77")
    dispatch(r, FakeLocator(count=3), "expect_count", "3")
    with pytest.raises(UiRunnerError, match="期望 3"):
        dispatch(r, FakeLocator(count=2), "expect_count", "3")
    with pytest.raises(UiRunnerError, match="数量取值非法"):
        dispatch(r, FakeLocator(count=1), "expect_count", "many")
    dispatch(r, FakeLocator(input_value="abc"), "expect_value", "abc")
    with pytest.raises(UiRunnerError):
        dispatch(r, FakeLocator(input_value="abc"), "expect_value", "xyz")


def test_expect_attr_title_url():
    r = make_runner(page=FakePage(url="http://x/login", title_text="登录-首页"))
    dispatch(r, FakeLocator(get_attribute="/login"), "expect_attr", "href=/login")
    with pytest.raises(UiRunnerError, match="属性名=期望值"):
        dispatch(r, FakeLocator(), "expect_attr", "href")
    dispatch(r, None, "expect_title", "登录")
    with pytest.raises(UiRunnerError):
        dispatch(r, None, "expect_title", "绝不存在的标题")
    dispatch(r, None, "expect_url", "/login")


# ==================== P4 target 切换：frame / main / window ====================


def test_switch_frame_main():
    inner = FakeFrame()
    page = FakePage()
    page.getters  # noqa: B018 触发定位记录
    loc = FakeLocator()
    loc._ret["element_handle"] = FakeHandle(inner)
    r = make_runner(page=page)
    dispatch(r, loc, "switch_frame")
    assert r.target is inner            # 已切入帧
    dispatch(r, None, "switch_main")
    assert r.target is page             # 回主文档


def test_switch_frame_not_iframe_raises():
    loc = FakeLocator()
    loc._ret["element_handle"] = FakeHandle(None)   # content_frame 为空
    with pytest.raises(UiRunnerError, match="iframe"):
        dispatch(make_runner(), loc, "switch_frame")


def test_switch_window_last_and_index():
    p0, p1 = FakePage(), FakePage()
    r = make_runner(page=p0, pages=[p0, p1])
    dispatch(r, None, "switch_window", "last")
    assert r.page is p1 and r.target is p1
    r2 = make_runner(page=p1, pages=[p0, p1])
    dispatch(r2, None, "switch_window", "0")
    assert r2.page is p0 and r2.target is p0


def test_switch_window_out_of_range_and_invalid():
    p0 = FakePage()
    with pytest.raises(UiRunnerError, match="越界"):
        dispatch(make_runner(page=p0, pages=[p0]), None, "switch_window", "5")
    with pytest.raises(UiRunnerError, match="切换标签页取值非法"):
        dispatch(make_runner(page=p0, pages=[p0]), None, "switch_window", "first")


# ==================== P4 dialog 默认 accept ====================


def test_accept_dialog_calls_accept():
    calls = {"n": 0}

    class D:
        def accept(self): calls["n"] += 1

    _accept_dialog(D())
    assert calls["n"] == 1


def test_accept_dialog_swallows_error():
    class Bad:
        def accept(self): raise RuntimeError("已被处理")

    _accept_dialog(Bad())  # 不抛出即通过
