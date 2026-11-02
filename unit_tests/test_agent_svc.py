# -*- coding: utf-8 -*-
"""agent_svc 服务账号单元测试（设计文档 5.3 授权行落地）。

锁三件事：
- 角色目录里有 agent_svc，且默认矩阵只授予 case:write——管理类权限一个都不给；
- tools/init_agent_svc.py 幂等：重复执行不重建账号、不重置密码、矩阵不重复追加；
- 权限落进 perm_store 后 permissions_of 只回 case:write（读操作靠登录态，无需权限码）。

数据文件全部 monkeypatch 到 tmp_path，不碰真实 users.json / permissions.json。
    .venv/Scripts/python.exe -m pytest unit_tests/test_agent_svc.py -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shared import perm_store, user_store  # noqa: E402


def _load_init_tool():
    spec = importlib.util.spec_from_file_location(
        "init_agent_svc", ROOT / "tools" / "init_agent_svc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """users/permissions 两个数据文件切到 tmp，测试结束由 monkeypatch 自动还原。"""
    monkeypatch.setattr(user_store, "_DATA_FILE", tmp_path / "users.json")
    monkeypatch.setattr(perm_store, "_DATA_FILE", tmp_path / "permissions.json")
    monkeypatch.setattr(perm_store, "_loaded", None)
    return tmp_path


def test_role_catalog_has_agent_svc():
    assert "agent_svc" in user_store.ROLES
    assert user_store.ROLE_NAMES["agent_svc"] == "Agent 服务账号"


def test_default_matrix_grants_only_case_write():
    granted = [c for c, roles in perm_store.DEFAULT_MATRIX.items() if "agent_svc" in roles]
    assert granted == ["case:write"]


def test_init_script_is_idempotent(isolated):
    tool = _load_init_tool()
    assert tool.main(["--password", "svc-pass-1"]) == 0
    user = user_store.get_user("agent_svc")
    assert user is not None and user["role"] == "agent_svc"

    # 重复执行：账号不重建、密码不被顺手改掉
    assert tool.main([]) == 0
    again = user_store.get_user("agent_svc")
    assert again["created_at"] == user["created_at"]
    assert user_store.verify_password("svc-pass-1", again["password"])

    # 矩阵里 agent_svc 只出现一次（追加前判过重，落盘再多次也不重复）
    assert perm_store.matrix()["case:write"].count("agent_svc") == 1


def test_service_account_permissions_minimal(isolated):
    tool = _load_init_tool()
    tool.main(["--password", "svc-pass-1"])
    assert perm_store.permissions_of("agent_svc") == ["case:write"]
    # 高危码绝不因新增角色而意外放出
    for code in ("case:delete", "user:manage", "audit:manage", "env:manage", "secret:manage"):
        assert "agent_svc" not in perm_store.matrix()[code]


def test_reset_password_requires_flag(isolated):
    tool = _load_init_tool()
    tool.main(["--password", "svc-pass-1"])
    # 不带 --reset-password 传新密码 → 明确不改密
    tool.main(["--password", "svc-pass-2"])
    assert user_store.verify_password("svc-pass-1", user_store.get_user("agent_svc")["password"])
    tool.main(["--password", "svc-pass-2", "--reset-password"])
    assert user_store.verify_password("svc-pass-2", user_store.get_user("agent_svc")["password"])
