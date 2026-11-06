# -*- coding: utf-8 -*-
"""backend.ui_session_store 单元测试：校验 / 状态派生 / CRUD / 原子写 / 更新作废。

登录态数据层。
与执行用例（tests/ 目录，由 main.py 收集）隔离：本目录用 pytest 直接跑，不进平台批次。
    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_session_store.py -v

状态派生与校验均为纯逻辑（不依赖 Playwright、不触碰实体文件），故无需真实浏览器。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ui import ui_session_store as store  # noqa: E402
from backend.ui.ui_session_store import UiSessionError  # noqa: E402

ENVS = ["test", "algo"]  # 显式注入合法环境，单测不依赖 config.yaml 内容


def _session(sid="album_test_login", **over):
    base = {
        "id": sid, "name": "相册测试登录态", "desc": "验证登录后可跨用例复用",
        "env": "test", "browser": "chromium", "headless": True,
        "file": "data/cases/ui_demo.json", "capture_case": "ui_login_001",
        "ttl_seconds": 3600,
    }
    base.update(over)
    return base


@pytest.fixture
def reg(tmp_path):
    """注册表用临时文件；会话库全局单文件、缺失即空表，无需预建。"""
    return tmp_path / "ui_sessions.json"


# ==================== 校验 ====================

def test_validate_ok_normalizes():
    s = store.validate_session(_session(), ENVS)
    assert s["id"] == "album_test_login"
    assert s["env"] == "test"
    assert s["browser"] == "chromium"
    assert s["headless"] is True
    assert s["ttl_seconds"] == 3600
    # 校验返回只含可编辑字段，不含运行态
    assert set(s) == {"id", "name", "desc", "env", "browser",
                      "headless", "file", "capture_case", "ttl_seconds"}


def test_validate_browser_case_insensitive():
    assert store.validate_session(_session(browser="CHROMIUM"), ENVS)["browser"] == "chromium"


@pytest.mark.parametrize("bad_id", ["", "  ", "a/b", "a\\b", "a?b", "a#b", "a\nb"])
def test_validate_bad_id(bad_id):
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(id=bad_id), ENVS)
    assert e.value.kind == "invalid"


def test_validate_env_required_and_membership():
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(env=""), ENVS)
    assert e.value.kind == "invalid"
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(env="prod"), ENVS)
    assert "不存在" in str(e.value)


def test_validate_bad_browser():
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(browser="ie"), ENVS)
    assert e.value.kind == "invalid"


def test_validate_file_must_be_json():
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(file="data/api_cases.xlsx"), ENVS)
    assert "UI 项目" in str(e.value)


def test_validate_capture_case_required():
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(capture_case="  "), ENVS)
    assert e.value.kind == "invalid"


@pytest.mark.parametrize("bad_ttl", [-1, "abc", 1.5, True])
def test_validate_bad_ttl(bad_ttl):
    with pytest.raises(UiSessionError) as e:
        store.validate_session(_session(ttl_seconds=bad_ttl), ENVS)
    assert e.value.kind == "invalid"


def test_validate_ttl_zero_allowed_means_never_expire():
    assert store.validate_session(_session(ttl_seconds=0), ENVS)["ttl_seconds"] == 0


# ==================== 状态派生 ====================

def test_status_not_collected():
    s = _session(captured_at=None, last_error=None)
    assert store.status_of(s)["status"] == "not_collected"


def test_status_failed_when_never_collected_but_error():
    s = _session(captured_at=None, last_error="登录页 502")
    st = store.status_of(s)
    assert st["status"] == "failed"
    assert st["last_error"] == "登录页 502"


def test_status_valid_within_ttl():
    now = datetime(2026, 10, 23, 12, 0, 0)
    s = _session(captured_at=(now - timedelta(seconds=60)).isoformat(timespec="seconds"),
                 ttl_seconds=3600, last_error=None)
    st = store.status_of(s, now=now)
    assert st["status"] == "valid"
    assert 3500 <= st["remaining_seconds"] <= 3600


def test_status_expired_beyond_ttl():
    now = datetime(2026, 10, 23, 12, 0, 0)
    s = _session(captured_at=(now - timedelta(seconds=3601)).isoformat(timespec="seconds"),
                 ttl_seconds=3600, last_error=None)
    st = store.status_of(s, now=now)
    assert st["status"] == "expired"
    assert st["remaining_seconds"] == 0


def test_status_ttl_zero_always_valid():
    old = datetime(2020, 1, 1).isoformat(timespec="seconds")
    st = store.status_of(_session(captured_at=old, ttl_seconds=0), now=datetime.now())
    assert st["status"] == "valid"
    assert st["remaining_seconds"] is None


def test_status_unparseable_captured_at_is_not_collected():
    st = store.status_of(_session(captured_at="not-a-time", ttl_seconds=3600))
    assert st["status"] == "not_collected"


def test_status_keeps_expired_even_with_error_when_collected():
    # 有旧 captured_at 时，即便上次重采失败留有 last_error，状态仍按有效期判定（旧登录态仍可用）
    now = datetime(2026, 10, 23, 12, 0, 0)
    s = _session(captured_at=(now - timedelta(seconds=10)).isoformat(timespec="seconds"),
                 ttl_seconds=3600, last_error="重采失败")
    assert store.status_of(s, now=now)["status"] == "valid"


# ==================== CRUD + 查重 + 原子写 ====================

def test_create_and_get(reg):
    created = store.create_session(reg, _session(), ENVS)
    assert created["id"] == "album_test_login"
    assert created["created_at"] and created["captured_at"] is None and created["last_error"] is None
    assert store.get_session(reg, "album_test_login")["name"] == "相册测试登录态"
    assert store.get_session(reg, "ghost") is None


def test_create_duplicate_conflict(reg):
    store.create_session(reg, _session(), ENVS)
    with pytest.raises(UiSessionError) as e:
        store.create_session(reg, _session(), ENVS)
    assert e.value.kind == "conflict"
    assert len(store.list_sessions(reg)) == 1


def test_create_invalid_does_not_write(reg):
    with pytest.raises(UiSessionError):
        store.create_session(reg, _session(env="prod"), ENVS)
    assert store.list_sessions(reg) == []
    assert not list(reg.parent.glob("*.tmp"))  # 无残留临时文件


def test_update_preserves_and_revalidates(reg):
    store.create_session(reg, _session(), ENVS)
    up = store.update_session(reg, "album_test_login", {"name": "改名", "desc": "x"}, ENVS)
    assert up["name"] == "改名" and up["id"] == "album_test_login"


def test_update_non_invalidating_keeps_captured_at(reg):
    store.create_session(reg, _session(), ENVS)
    store.mark_captured(reg, "album_test_login", "2026-10-23T10:00:00")
    up = store.update_session(reg, "album_test_login", {"name": "仅改名"}, ENVS)
    assert up["captured_at"] == "2026-10-23T10:00:00"


def test_update_invalidating_field_clears_captured_at(reg):
    store.create_session(reg, _session(), ENVS)
    store.mark_captured(reg, "album_test_login", "2026-10-23T10:00:00")
    # 改 env → 旧登录态与新域名不匹配，作废强制重采
    up = store.update_session(reg, "album_test_login", {"env": "algo"}, ENVS)
    assert up["captured_at"] is None and up["last_error"] is None


def test_update_missing_not_found(reg):
    with pytest.raises(UiSessionError) as e:
        store.update_session(reg, "ghost", {"name": "x"}, ENVS)
    assert e.value.kind == "not_found"


def test_update_invalid_not_persisted(reg):
    store.create_session(reg, _session(), ENVS)
    with pytest.raises(UiSessionError):
        store.update_session(reg, "album_test_login", {"ttl_seconds": -5}, ENVS)
    assert store.get_session(reg, "album_test_login")["ttl_seconds"] == 3600


def test_delete(reg):
    store.create_session(reg, _session(), ENVS)
    res = store.delete_session(reg, "album_test_login")
    assert res["deleted"] == "album_test_login"
    assert store.list_sessions(reg) == []
    with pytest.raises(UiSessionError) as e:
        store.delete_session(reg, "album_test_login")
    assert e.value.kind == "not_found"


def test_delete_removes_entity_file(reg, tmp_path):
    store.create_session(reg, _session(), ENVS)
    base = tmp_path / "ui_sessions"
    ep = store.entity_path("album_test_login", "test", base)
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text('{"cookies":[]}', encoding="utf-8")
    res = store.delete_session(reg, "album_test_login", entity_base=base)
    assert res["entity_removed"] == str(ep)
    assert not ep.exists()


# ==================== mark_captured / mark_failed ====================

def test_mark_captured_sets_time_and_clears_error(reg):
    store.create_session(reg, _session(), ENVS)
    s = store.mark_captured(reg, "album_test_login", "2026-10-23T10:00:00")
    assert s["captured_at"] == "2026-10-23T10:00:00" and s["last_error"] is None


def test_mark_failed_keeps_old_captured_at(reg):
    store.create_session(reg, _session(), ENVS)
    store.mark_captured(reg, "album_test_login", "2026-10-23T10:00:00")
    s = store.mark_failed(reg, "album_test_login", "重采登录页超时")
    assert s["captured_at"] == "2026-10-23T10:00:00"  # 旧登录态仍按 TTL 可复用
    assert s["last_error"] == "重采登录页超时"


# ==================== 损坏 JSON ====================

def test_corrupt_registry_error(reg):
    reg.write_text("{ not json", encoding="utf-8")
    with pytest.raises(UiSessionError) as e:
        store.list_sessions(reg)
    assert e.value.kind == "invalid"


# ==================== 实体路径 ====================

def test_entity_path_namespaced_by_env():
    p = store.entity_path("sid", "test", Path("base"))
    assert p.name == "sid__test.json"
