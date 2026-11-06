# -*- coding: utf-8 -*-
"""观测与诊断 · 失败 trace 采集（纯逻辑 + 假 driver，不真起浏览器）。

验收口径：
- trace="on_failure" 且用例失败 → UiRunResult.trace_blob 收到 stop 写回的 zip 字节；
- 用例通过 → 不留 trace_blob（显式 stop 丢弃，不触发未关闭告警）；
- trace="off" / 采集模式 → 从不 start tracing；
- 假对象/旧内核无 context.tracing → 自动降级不产出、绝不抛（守住存量 250 单测的假 context）；
- WebEngine.execute 仅在最终失败时经 attach_trace 回调落附件；中间失败末次通过 / 无回调 / 回调异常 均不伤判定。

    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_trace.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines.web.engine import UiRunner, WebEngine  # noqa: E402

PAYLOAD = b"PK\x03\x04fake-trace-bytes"
# 一条必失败的步骤：假 Page 无 goto，_dispatch 触发 AttributeError → 步骤判失败
FAIL_STEP = [{"action": "goto", "value": "http://x/y"}]


# ==================== 假 playwright ====================


class FakePage:
    def set_default_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass

    def close(self):
        pass
    # 故意不提供 goto / screenshot：goto 步骤失败、_capture 优雅降级


class FakeTracing:
    def __init__(self, payload=PAYLOAD):
        self.payload = payload
        self.started = 0
        self.stop_calls = []          # 每次 stop 的 path（None=丢弃）

    def start(self, **kw):
        self.started += 1

    def stop(self, path=None):
        self.stop_calls.append(path)
        if path is not None:
            Path(path).write_bytes(self.payload)


class FakeContextTraced:
    def __init__(self):
        self.tracing = FakeTracing()

    def new_page(self):
        return FakePage()

    def close(self):
        pass


class FakeContextPlain:
    """模拟假对象/旧内核：没有 tracing 属性，_maybe_start_tracing 应吞 AttributeError 返回 False。"""

    def new_page(self):
        return FakePage()

    def close(self):
        pass


def _patch_pw(monkeypatch, ctx_factory):
    class FakeBrowser:
        def new_context(self, **kw):
            return ctx_factory()

        def is_connected(self):
            return True

        def close(self):
            pass

    class FakeLauncher:
        def launch(self, **kw):
            return FakeBrowser()

    class FakePW:
        def __init__(self):
            self.chromium = FakeLauncher()

        def stop(self):
            pass

    def fake_sync_playwright():
        class _S:
            def start(self):
                return FakePW()

        return _S()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)


def _case(**over):
    case = {"case_id": "trace_001", "browser": "chromium", "headless": True,
            "steps": [], "extract": []}
    case.update(over)
    return case


# ==================== UiRunner trace 辅助方法（直测） ====================


def test_maybe_start_tracing_on_failure():
    runner = UiRunner(Context({}, {}), "http://x", trace="on_failure")
    ctx = FakeContextTraced()
    assert runner._maybe_start_tracing(ctx) is True
    assert ctx.tracing.started == 1


def test_maybe_start_tracing_off_and_collect_skip():
    off = UiRunner(Context({}, {}), "http://x", trace="off")
    ctx = FakeContextTraced()
    assert off._maybe_start_tracing(ctx) is False

    collect = UiRunner(Context({}, {}), "http://x", trace="on_failure", mode="collect")
    assert collect._maybe_start_tracing(FakeContextTraced()) is False


def test_maybe_start_tracing_degrades_without_attr():
    """假对象/旧内核无 .tracing：捕获 AttributeError，返回 False 而不抛（守住存量假 context）。"""
    runner = UiRunner(Context({}, {}), "http://x", trace="on_failure")
    assert runner._maybe_start_tracing(FakeContextPlain()) is False


def test_export_trace_returns_bytes(tmp_path):
    runner = UiRunner(Context({}, {}), "http://x", trace="on_failure", output_dir=tmp_path)
    runner._case_tag = "trace_001"
    ctx = FakeContextTraced()
    assert runner._export_trace(ctx) == PAYLOAD
    assert ctx.tracing.stop_calls and ctx.tracing.stop_calls[-1] is not None
    # 临时 zip 已清理
    assert list(tmp_path.glob(".trace-*.zip")) == []


def test_export_trace_swallows_errors(tmp_path):
    class BoomTracing:
        def start(self, **kw):
            pass

        def stop(self, path=None):
            raise RuntimeError("boom")

    runner = UiRunner(Context({}, {}), "http://x", trace="on_failure", output_dir=tmp_path)
    ctx = type("C", (), {"tracing": BoomTracing()})()
    assert runner._export_trace(ctx) is None  # 导出失败不影响判定


# ==================== run() 端到端（假浏览器） ====================


def test_run_failure_produces_trace_blob(monkeypatch, tmp_path):
    _patch_pw(monkeypatch, FakeContextTraced)
    res = WebEngine().execute(_case(steps=FAIL_STEP), Context({}, {}), base_url="http://x",
                              trace="on_failure", output_dir=tmp_path)
    assert res.ok is False
    assert res.trace_blob == PAYLOAD


def test_run_pass_no_trace_blob_but_stops_tracing(monkeypatch, tmp_path):
    ctx = FakeContextTraced()
    _patch_pw(monkeypatch, lambda: ctx)
    res = WebEngine().execute(_case(steps=[]), Context({}, {}), base_url="http://x",
                              trace="on_failure", output_dir=tmp_path)
    assert res.ok is True
    assert res.trace_blob is None
    assert ctx.tracing.started == 1
    assert None in ctx.tracing.stop_calls          # 通过：显式 stop 丢弃（无 path）


def test_run_trace_off_never_starts(monkeypatch, tmp_path):
    ctx = FakeContextTraced()
    _patch_pw(monkeypatch, lambda: ctx)
    res = WebEngine().execute(_case(steps=FAIL_STEP), Context({}, {}), base_url="http://x",
                              trace="off", output_dir=tmp_path)
    assert res.ok is False
    assert ctx.tracing.started == 0
    assert res.trace_blob is None


def test_run_degrades_without_tracing_attr(monkeypatch, tmp_path):
    _patch_pw(monkeypatch, FakeContextPlain)
    res = WebEngine().execute(_case(steps=FAIL_STEP), Context({}, {}), base_url="http://x",
                              trace="on_failure", output_dir=tmp_path)
    assert res.ok is False                          # 用例正常判失败
    assert res.trace_blob is None                   # 无 trace 能力：优雅降级不产出


# ==================== WebEngine.execute 落附件时机 ====================


def test_execute_attaches_only_on_final_failure(monkeypatch, tmp_path):
    _patch_pw(monkeypatch, FakeContextTraced)
    attached = []
    WebEngine().execute(_case(steps=FAIL_STEP), Context({}, {}), base_url="http://x",
                        trace="on_failure", attach_trace=lambda n, d: attached.append((n, d)),
                        output_dir=tmp_path)
    assert attached == [("UI失败trace", PAYLOAD)]


def test_execute_no_attach_on_pass(monkeypatch, tmp_path):
    _patch_pw(monkeypatch, FakeContextTraced)
    attached = []
    WebEngine().execute(_case(steps=[]), Context({}, {}), base_url="http://x",
                        trace="on_failure", attach_trace=lambda n, d: attached.append(1),
                        output_dir=tmp_path)
    assert attached == []


def test_execute_attach_callback_error_swallowed(monkeypatch, tmp_path):
    _patch_pw(monkeypatch, FakeContextTraced)

    def boom(name, data):
        raise RuntimeError("attach fail")

    res = WebEngine().execute(_case(steps=FAIL_STEP), Context({}, {}), base_url="http://x",
                              trace="on_failure", attach_trace=boom, output_dir=tmp_path)
    assert res.ok is False                           # 附件落盘异常不改用例成败
