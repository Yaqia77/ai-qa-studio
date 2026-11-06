# -*- coding: utf-8 -*-
"""BrowserPool / BrowserBundle 纯逻辑单测（安全阀与复用语义）。

全程喂假 driver（launch_fn 注入），不真起浏览器：验证「复用只折叠浏览器进程、
绝不折叠 context 隔离」的核心不变式 + reap 安全阀 + dead 重启 + close_all 回收。

    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_browser_pool.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines.web.browser_pool import BrowserBundle, BrowserPool  # noqa: E402
from core.engines.web.engine import UiRunner  # noqa: E402


# ==================== 假 driver ====================


class FakeContext:
    def __init__(self, kwargs: dict, raise_on_new_page: str | None = None):
        self.kwargs = kwargs
        self.closed = False
        self._raise = raise_on_new_page

    def new_page(self):
        if self._raise:
            raise RuntimeError(self._raise)
        return FakePage()

    def storage_state(self, path=None):
        Path(path).write_text('{"cookies": []}', encoding="utf-8")

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.closed = False
        self.default_timeout = None

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def on(self, event, handler):
        pass  # run() 会注册 page.on("dialog", ...)，假对象需匹配真实 Page 契约

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, raise_on_new_page: str | None = None):
        self.contexts: list[FakeContext] = []
        self.connected = True
        self.closed = False
        self._raise = raise_on_new_page

    def new_context(self, **kw):
        ctx = FakeContext(kw, raise_on_new_page=self._raise)
        self.contexts.append(ctx)
        return ctx

    def is_connected(self):
        return self.connected

    def close(self):
        self.closed = True


class FakeLauncher:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser
        self.launch_calls: list[dict] = []

    def launch(self, **kw):
        self.launch_calls.append(kw)
        return self._browser


class FakePW:
    """假 playwright 句柄：chromium 属性即假 launcher。"""

    def __init__(self, browser: FakeBrowser | None = None):
        self.chromium = FakeLauncher(browser or FakeBrowser())
        self.stopped = False

    def stop(self):
        self.stopped = True


def make_pool(reap_after: int = 200, raise_on_new_page: str | None = None):
    """造（pool, launched）：launch_fn 在共享 pw 上产出新假 browser 并登记，便于断言启动次数。

    新契约（共享 pw）：launch_fn(pw, browser, headless, channel) -> browser；
    pw_factory 只在池首次需要时调一次（同线程第二实例会炸，真 driver 限制）。
    raise_on_new_page 只对**第一束**生效：模拟「旧浏览器崩溃、重启后的新浏览器恢复」。
    """
    launched: list[tuple[FakePW, FakeBrowser]] = []
    pending = [raise_on_new_page]

    def launch_fn(pw, browser: str, headless: bool, channel=None):
        br = FakeBrowser(raise_on_new_page=pending[0])
        pending[0] = None
        launched.append((pw, br))
        return br

    pool = BrowserPool(reap_after=reap_after, launch_fn=launch_fn, pw_factory=FakePW)
    return pool, launched


# ==================== BrowserPool：复用/分组 ====================


def test_same_spec_reuses_single_bundle():
    pool, launched = make_pool()
    b1 = pool.acquire(browser="chromium", headless=True)
    b2 = pool.acquire(browser="Chromium", headless=True)  # 大小写归一
    b3 = pool.acquire(browser="chromium", headless=True)
    assert b1 is b2 is b3
    assert len(launched) == 1 and pool.launch_count == 1
    pool.close_all()


def test_different_specs_get_separate_bundles():
    pool, launched = make_pool()
    pool.acquire(browser="chromium", headless=True)
    pool.acquire(browser="chromium", headless=False)
    pool.acquire(browser="firefox", headless=True)
    assert len(launched) == 3 == pool.launch_count
    # 真 driver 核心不变式：多束共享同一个 playwright 实例（同线程第二实例必炸）
    assert launched[0][0] is launched[1][0] is launched[2][0]
    pool.close_all()


def test_shared_pw_started_only_once_across_specs():
    """headless+headful 混用：pw_factory 只调一次（共享），browser 各束独立。"""
    pw_starts: list[FakePW] = []

    def pw_factory():
        pw = FakePW()
        pw_starts.append(pw)
        return pw

    launched: list[tuple[FakePW, FakeBrowser]] = []

    def launch_fn(pw, browser, headless, channel=None):
        br = FakeBrowser()
        launched.append((pw, br))
        return br

    pool = BrowserPool(launch_fn=launch_fn, pw_factory=pw_factory)
    pool.acquire(headless=True)
    pool.acquire(headless=False)   # 旧缺陷触发点：第二束曾试图再起一个 pw
    assert len(pw_starts) == 1
    assert len(launched) == 2 and pool.launch_count == 2
    assert all(pw is pw_starts[0] for pw, _ in launched)
    pool.close_all()
    assert pw_starts[0].stopped and pool._pw is None


def test_launch_failure_rebuilds_shared_pw_and_retries_once():
    """共享 pw 疑似失联：launch 失败 → 停旧 pw、重建、重试一次成功。"""
    pws: list[FakePW] = []

    def pw_factory():
        pw = FakePW()
        pws.append(pw)
        return pw

    calls: list[int] = []

    def launch_fn(pw, browser, headless, channel=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Connection closed")   # 第一次：pw 失联
        return FakeBrowser()

    pool = BrowserPool(launch_fn=launch_fn, pw_factory=pw_factory)
    bundle = pool.acquire()
    assert pool.launch_count == 1
    assert pws[0].stopped is True and not pws[1].stopped   # 旧 pw 被停，新 pw 接替
    assert bundle.pw is pws[1]
    pool.close_all()


def test_bundle_new_context_passes_storage_state():
    pool, _ = make_pool()
    bundle = pool.acquire()
    ctx = bundle.new_context("/tmp/ss.json")
    assert ctx.kwargs == {"storage_state": "/tmp/ss.json"}
    ctx2 = bundle.new_context(None)
    assert ctx2.kwargs == {}  # 裸上下文不带任何 kw —— 与 _new_context 口径一致
    pool.close_all()


# ==================== BrowserPool：安全阀与 dead 重启 ====================


def test_reap_after_threshold_recycles_bundle():
    pool, launched = make_pool(reap_after=2)
    first = pool.acquire()
    pool.acquire()               # 第 2 条：仍在配额内，复用
    assert len(launched) == 1
    third = pool.acquire()       # 第 3 条：跑满 2 条 → 回收旧束、重启新束
    assert third is not first
    assert len(launched) == 2 == pool.launch_count
    assert first.browser.closed and not first.pw.stopped   # 旧束只关 browser，共享 pw 不停
    pool.close_all()


def test_reap_after_floor_is_one():
    pool, launched = make_pool(reap_after=0)  # 非法 0 → 收敛为 1：每条都重启
    pool.acquire()
    pool.acquire()
    assert len(launched) == 2
    pool.close_all()


def test_disconnected_bundle_restarts_on_next_acquire():
    pool, launched = make_pool()
    first = pool.acquire()
    first.browser.connected = False          # 模拟浏览器进程中途崩溃
    second = pool.acquire()
    assert second is not first
    assert len(launched) == 2
    assert first.browser.closed and not first.pw.stopped   # 束回收不碰共享 pw
    pool.close_all()


def test_invalidate_marks_dead_and_triggers_restart():
    pool, launched = make_pool()
    bundle = pool.acquire()
    assert bundle.is_alive() is True
    pool.invalidate(bundle)                  # 执行侧发现断连的显式标记通道
    assert bundle.is_alive() is False
    assert pool.acquire() is not bundle
    assert len(launched) == 2
    pool.close_all()


def test_close_all_reclaims_every_bundle():
    pool, launched = make_pool()
    a = pool.acquire(browser="chromium", headless=True)
    b = pool.acquire(browser="chromium", headless=False)
    pool.close_all()
    assert all(x.closed for x in (a, b))
    assert all(br.closed for _, br in launched)
    assert all(pw.stopped for pw, _ in launched)   # 共享 pw 在 close_all 统一停止
    assert pool._pw is None
    assert pool._bundles == {}
    # close_all 幂等；再次 acquire 走全新启动
    pool.close_all()
    pool.acquire()
    assert len(launched) == 3


def test_bundle_close_is_idempotent():
    pw, br = FakePW(FakeBrowser()), FakeBrowser()
    bundle = BrowserBundle(pw, br, ("chromium", True, ""))
    bundle.close()
    bundle.close()
    assert br.closed and not pw.stopped   # 束只关 browser；共享 pw 归池统一停


# ==================== UiRunner：池链路语义（只关 context 不关 browser） ====================


def _case(**over):
    case = {"case_id": "t", "browser": "chromium", "headless": True, "steps": []}
    case.update(over)
    return case


def test_runner_with_pool_closes_only_context():
    pool, launched = make_pool()
    r = UiRunner(Context({}, {}), "http://x", browser_pool=pool)
    res = r.run(_case())
    assert res.ok is True
    pw, br = launched[0]
    ctx, page = br.contexts[0], None
    assert ctx.closed is True                # context 逐条关闭：隔离保留
    assert br.closed is False and pw.stopped is False  # 浏览器束绝不被单条用例关掉
    pool.close_all()
    assert br.closed is True and pw.stopped is True    # 收尾统一回收


def test_runner_pool_reuses_browser_across_cases():
    pool, launched = make_pool()
    r = UiRunner(Context({}, {}), "http://x", browser_pool=pool)
    assert r.run(_case(case_id="c1")).ok is True
    assert r.run(_case(case_id="c2")).ok is True
    assert r.run(_case(case_id="c3")).ok is True
    assert pool.launch_count == 1            # 3 条用例 = 1 次启动（本期核心验收口径）
    br = launched[0][1]
    assert len(br.contexts) == 3             # 但 context 仍是 3 个：一条不落
    assert all(c.closed for c in br.contexts)
    pool.close_all()


def test_runner_pool_storage_state_reaches_context():
    pool, launched = make_pool()
    r = UiRunner(Context({}, {}), "http://x", browser_pool=pool,
                 session_provider=lambda case: ("C:/tmp/ss.json", None))
    res = r.run(_case(session="sid"))
    assert res.ok is True
    assert launched[0][1].contexts[0].kwargs == {"storage_state": "C:/tmp/ss.json"}
    pool.close_all()


def test_runner_marks_bundle_dead_on_browser_close_error():
    pool, launched = make_pool(raise_on_new_page="Target closed")
    r = UiRunner(Context({}, {}), "http://x", browser_pool=pool)
    res = r.run(_case())
    assert res.ok is False
    assert "Target closed" in (res.error or "")
    assert pool._bundles[("chromium", True, "")].dead is True  # 崩溃束被标死
    # 下一条用例自动换新束，不受旧束污染
    assert r.run(_case(case_id="c2")).ok is True
    assert pool.launch_count == 2
    pool.close_all()


def test_collect_mode_never_uses_pool(monkeypatch, tmp_path):
    """采集 = 同步短任务：即使注入了池也必须走独立启停（崩溃不牵连批次）。"""
    pool, launched = make_pool()
    real_acquire = pool.acquire
    pool.acquire = lambda *a, **k: pytest.fail("采集模式不应调用 pool.acquire")  # type: ignore[method-assign]

    fake_pw = FakePW(FakeBrowser())

    def fake_sync_playwright():
        class _Starter:
            def start(self_inner):
                return fake_pw

        return _Starter()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)
    seen = {}

    def committer(case, tmp):
        seen["file"] = tmp

    r = UiRunner(Context({}, {}), "http://x", mode="collect",
                 session_committer=committer, browser_pool=pool, output_dir=tmp_path)
    res = r.run(_case(case_id="login"))
    assert res.ok is True and res.collected  # 采集照常导出 storage_state
    assert fake_pw.stopped is True           # 独立链路：用完即停，不进池
    assert real_acquire is not None          # （原 acquire 引用仅作留存，未被调用）


def test_runner_without_pool_keeps_legacy_lifecycle(monkeypatch):
    """无池回退：逐条 start→launch→...→browser.close + pw.stop，与逐条独立启停一致。"""
    fake_pw = FakePW(FakeBrowser())

    def fake_sync_playwright():
        class _Starter:
            def start(self_inner):
                return fake_pw

        return _Starter()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)
    r = UiRunner(Context({}, {}), "http://x")  # 不传 browser_pool
    res = r.run(_case())
    assert res.ok is True
    br = fake_pw.chromium._browser
    assert br.closed is True and fake_pw.stopped is True  # 独立启停：用完即关
    # launch 无 channel 参数（保持内核缺失回退口径入口）
    assert fake_pw.chromium.launch_calls == [{"headless": True}]
