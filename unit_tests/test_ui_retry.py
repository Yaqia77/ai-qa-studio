# -*- coding: utf-8 -*-
"""用例级失败重试：WebEngine.execute 的重试外层语义（纯逻辑 + 假 driver，不真起浏览器）。

验收口径：
- flaky 用例 retry=1：第 1 次失败、第 2 次通过 → 最终判通过，attempts=2，历史留首次失败摘要；
- 稳定失败：全次失败才判 fail，错误带「重试 k 次后仍失败」，保留末次轨迹；
- retry=0 / 存量用例：只跑一次，attempts=1，无历史（与逐条串行行为一致）；
- 采集模式永不重试；上限截断 0..3。

    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_retry.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines.web.engine import WebEngine  # noqa: E402

# ==================== 假 playwright（成功路径不真起浏览器） ====================


class FakePage:
    def set_default_timeout(self, ms):
        pass

    def on(self, event, handler):
        pass  # run() 会注册 page.on("dialog", ...)，假对象需匹配真实 Page 契约

    def close(self):
        pass


class FakeContext:
    def new_page(self):
        return FakePage()

    def storage_state(self, path=None):
        Path(path).write_text('{"cookies": []}', encoding="utf-8")

    def close(self):
        pass


class FakeBrowser:
    def new_context(self, **kw):
        return FakeContext()

    def is_connected(self):
        return True

    def close(self):
        pass


class FakeLauncher:
    def launch(self, **kw):
        return FakeBrowser()


class FakePW:
    chromium = None

    def __init__(self):
        self.chromium = FakeLauncher()

    def stop(self):
        pass


def _patch_playwright(monkeypatch):
    def fake_sync_playwright():
        class _Starter:
            def start(self):
                return FakePW()

        return _Starter()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)


def _case(**over):
    case = {"case_id": "flaky_001", "browser": "chromium", "headless": True,
            "steps": [], "extract": []}
    case.update(over)
    return case


# ==================== 重试外层语义 ====================


def test_flaky_recovers_on_retry(monkeypatch):
    """第 1 次「登录态瞬时不可用」失败，第 2 次成功 → 判通过并留首次失败摘要。"""
    _patch_playwright(monkeypatch)
    calls = {"n": 0}

    def flaky_provider(case):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "模拟：会话瞬时不可用"
        return None, None

    res = WebEngine().execute(_case(retry=1, session="sid"), Context({}, {}), base_url="http://x",
                              session_provider=flaky_provider)
    assert res.ok is True
    assert res.attempts == 2
    assert len(res.attempt_history) == 1
    first = res.attempt_history[0]
    assert first["attempt"] == 1 and first["ok"] is False and "瞬时不可用" in first["error"]


def test_stable_failure_keeps_last_trace(monkeypatch):
    """每次都失败：最终 fail，错误带「重试 1 次后仍失败」，保留末次步骤轨迹。"""
    _patch_playwright(monkeypatch)

    def always_fail(case):
        return None, "会话未采集"

    res = WebEngine().execute(_case(retry=1, session="sid"), Context({}, {}), base_url="http://x",
                              session_provider=always_fail)
    assert res.ok is False
    assert res.attempts == 2
    assert res.error.startswith("重试 1 次后仍失败：")
    assert "会话未采集" in res.error
    assert len(res.attempt_history) == 1


def test_retry_zero_single_attempt_unchanged(monkeypatch):
    """retry=0（含存量用例无 retry 键）：只跑一次，attempts=1，无历史——与逐条串行行为一致。"""
    _patch_playwright(monkeypatch)
    res = WebEngine().execute(_case(), Context({}, {}), base_url="http://x")
    assert res.ok is True and res.attempts == 1 and res.attempt_history == []

    res_fail = WebEngine().execute(_case(session="sid"), Context({}, {}), base_url="http://x")
    # 无 provider 引用会话：失败也只一次，错误不加「重试后仍失败」前缀
    assert res_fail.ok is False and res_fail.attempts == 1
    assert not res_fail.error.startswith("重试")


def test_explicit_retry_param_overrides_case_field(monkeypatch):
    """入参 retry 优先于用例字段（批次默认由调用方折算后经入参传入）。"""
    _patch_playwright(monkeypatch)
    calls = {"n": 0}

    def provider(case):
        calls["n"] += 1
        return (None, "首次失败") if calls["n"] == 1 else (None, None)

    # 用例字段 retry 缺省(0)，但入参给 1 → 应重试一次后通过（引用会话使 provider 能被调到）
    res = WebEngine().execute(_case(session="sid"), Context({}, {}), base_url="http://x",
                              session_provider=provider, retry=1)
    assert res.ok is True and res.attempts == 2


# ==================== 归一与护栏 ====================


def test_collect_mode_never_retries():
    assert WebEngine._effective_retry(_case(retry=3), "collect", None) == 0


def test_effective_retry_bounds():
    assert WebEngine._effective_retry(_case(retry=9), "normal", None) == 3   # 上限截断
    assert WebEngine._effective_retry(_case(retry=-2), "normal", None) == 0  # 负数→0
    assert WebEngine._effective_retry(_case(retry="x"), "normal", None) == 0  # 非法→0
    assert WebEngine._effective_retry(_case(), "normal", None) == 0           # 缺省→0
    assert WebEngine._effective_retry(_case(retry=1), "normal", 2) == 2       # 入参优先
