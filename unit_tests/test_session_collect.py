# -*- coding: utf-8 -*-
"""backend.ui.session_collect 纯逻辑单测：复用解析 / 缺失指引 / 过期就地重采去重。

登录态「采集与复用编排」。本模块的价值在编排逻辑，浏览器/落盘交给 UiRunner，
故此处用 monkeypatch 把 collect 替换成假实现，只验证 resolve_for_attach 的判定与进程内去重，
不依赖 Playwright。
    .venv/Scripts/python.exe -m pytest unit_tests/test_session_collect.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ui import session_collect  # noqa: E402
from backend.ui import ui_session_store as store  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把注册表 + 实体目录 + collect 都隔离到 tmp，返回可编排的测试台。"""
    reg = tmp_path / "ui_sessions.json"
    entity_dir = tmp_path / "ui_sessions"
    monkeypatch.setattr(store, "SESSIONS_FILE", reg)
    monkeypatch.setattr(store, "ENTITY_DIR", entity_dir)
    monkeypatch.setattr(store, "entity_path",
                        lambda sid, env, base_dir=None: entity_dir / f"{sid}__{env}.json")
    calls = {"collect": 0}
    return {"reg": reg, "entity_dir": entity_dir, "calls": calls, "monkeypatch": monkeypatch}


def _seed(sandbox, captured_at=None, ttl=3600, last_error=None, write_entity=False):
    """直接在注册表落一条会话（绕过 create 的 env 校验），可选写出实体文件。"""
    import json
    rec = {
        "id": "sid", "name": "测试登录态", "desc": "", "env": "test", "browser": "chromium",
        "headless": True, "file": "data/cases/ui_demo.json", "capture_case": "ui_login_001",
        "ttl_seconds": ttl, "created_at": "2026-01-01T00:00:00",
        "captured_at": captured_at, "last_error": last_error,
    }
    sandbox["reg"].write_text(json.dumps({"version": 1, "sessions": [rec]}, ensure_ascii=False),
                              encoding="utf-8")
    ep = store.entity_path("sid", "test")
    if write_entity:
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text('{"cookies": []}', encoding="utf-8")
    return ep


def _fake_collect(sandbox, ok=True, err=None, write_entity=True):
    """替换 collect：记调用次数；成功则确保实体存在。"""
    def _collect(session):
        sandbox["calls"]["collect"] += 1
        if ok and write_entity:
            ep = store.entity_path(session["id"], session["env"])
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_text('{"cookies": []}', encoding="utf-8")
        return ok, err
    sandbox["monkeypatch"].setattr(session_collect, "collect", _collect)


def _now():
    return datetime.now()


# ==================== resolve_for_attach 判定 ====================

def test_no_session_backward_compatible(sandbox):
    assert session_collect.resolve_for_attach({"case_id": "t"}, {}) == (None, None)


def test_session_not_found_gives_guidance(sandbox):
    _seed(sandbox)  # 建了空注册表环境但换个 id 引用
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "ghost"}, {})
    assert path is None and "不存在" in err


def test_entity_missing_never_collected_gives_collect_hint(sandbox):
    _seed(sandbox, captured_at=None)  # 从未采集，且无实体文件
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, {})
    assert path is None and "尚未采集" in err and "立即采集" in err


def test_entity_missing_with_last_error_shows_reason(sandbox):
    _seed(sandbox, captured_at=None, last_error="登录页 502")
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, {})
    assert path is None and "登录页 502" in err


def test_valid_entity_attaches_without_collect(sandbox):
    ep = _seed(sandbox, captured_at=_now().isoformat(timespec="seconds"), ttl=3600, write_entity=True)
    _fake_collect(sandbox)
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, {})
    assert err is None and path == str(ep)
    assert sandbox["calls"]["collect"] == 0  # 有效登录态不触发重采


def test_ttl_zero_never_expires(sandbox):
    old = (datetime(2020, 1, 1)).isoformat(timespec="seconds")
    ep = _seed(sandbox, captured_at=old, ttl=0, write_entity=True)
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, {})
    assert err is None and path == str(ep)


def test_expired_triggers_in_process_recollect(sandbox):
    _seed(sandbox, captured_at=(_now() - timedelta(seconds=3601)).isoformat(timespec="seconds"),
          ttl=3600, write_entity=True)
    _fake_collect(sandbox, ok=True)
    registry = {}
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, registry)
    assert err is None and path is not None
    assert sandbox["calls"]["collect"] == 1


def test_expired_recollect_happens_only_once_per_process(sandbox):
    _seed(sandbox, captured_at=(_now() - timedelta(seconds=3601)).isoformat(timespec="seconds"),
          ttl=3600, write_entity=True)
    _fake_collect(sandbox, ok=True)
    registry = {}
    case = {"case_id": "t", "session": "sid"}
    for _ in range(5):  # 同一进程内多条用例引用同一过期会话
        path, err = session_collect.resolve_for_attach(case, registry)
        assert err is None
    assert sandbox["calls"]["collect"] == 1  # 只重采一次


def test_expired_recollect_failure_returns_error(sandbox):
    _seed(sandbox, captured_at=(_now() - timedelta(seconds=3601)).isoformat(timespec="seconds"),
          ttl=3600, write_entity=True)
    _fake_collect(sandbox, ok=False, err="重采登录超时")
    registry = {}
    path, err = session_collect.resolve_for_attach({"case_id": "t", "session": "sid"}, registry)
    assert path is None and "重采登录超时" in err
