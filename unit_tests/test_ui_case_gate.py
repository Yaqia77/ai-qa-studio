# -*- coding: utf-8 -*-
"""backend.agent_bridge.ui_case_gate 单元测试：UI 落库闸门的校验口径 / 冲突改名 / 整批拒绝 / kind 裁决。

与接口用例 test_agent_bridge.py 同风格：直接调 preview/commit 纯函数，目标用真实
ui_case_store 的 tmp JSON（原子写路径真实跑通），不依赖 starlette.testclient。
锁的是「闸门编排」本身：
- resolve_ui_target：项目必须存在且 kind=ui、显式 file 必须 .json（防把 UI 草稿灌进接口 Excel）；
- preview：逐条权威校验不落盘，编号已存在提示「自动递增改名」；
- commit：任一非法整批拒绝（绝不写半批）、编号冲突 _bump_id 让号改名并回带 renamed、meta 原样回显。
    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_case_gate.py -v
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent_bridge import ui_case_gate  # noqa: E402
from backend.ui import ui_case_store as store  # noqa: E402


def _case(cid="UI-001", **over):
    """一条合法 UI 用例草稿（对齐 ui_case_store.validate_case 口径）。"""
    base = {
        "case_id": cid, "module": "登录", "name": "用户登录",
        "desc": "验证账号密码登录成功", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": "/login"},
            {"action": "fill", "locator": "css=#username", "value": "{{username}}"},
            {"action": "click", "locator": "css=button[type=submit]"},
            {"action": "expect_text", "locator": "css=.welcome", "value": "欢迎"},
        ],
        "extract": [{"var": "order_id", "source": "text|css=.order-id"}],
    }
    base.update(over)
    return base


@pytest.fixture
def ui_file(tmp_path):
    p = tmp_path / "cases.json"
    store.scaffold(p)
    return p


# ==================== resolve_ui_target：kind / 后缀裁决 ====================

def test_resolve_项目kind非ui拒绝400(monkeypatch, tmp_path):
    f = tmp_path / "cases.xlsx"
    monkeypatch.setattr(ui_case_gate.project_store, "get",
                        lambda pid: {"id": pid, "kind": "api", "file": str(f)})
    with pytest.raises(HTTPException) as e:
        ui_case_gate.preview([_case()], project_id="proj_api")
    assert e.value.status_code == 400
    assert "不是 UI 类型" in str(e.value.detail)


def test_resolve_项目不存在404(monkeypatch):
    monkeypatch.setattr(ui_case_gate.project_store, "get", lambda pid: None)
    with pytest.raises(HTTPException) as e:
        ui_case_gate.preview([_case()], project_id="ghost")
    assert e.value.status_code == 404


def test_resolve_显式file非json后缀拒绝400(tmp_path):
    bad = tmp_path / "cases.xlsx"
    bad.write_bytes(b"stub")
    with pytest.raises(HTTPException) as e:
        ui_case_gate.preview([_case()], file=str(bad))
    assert e.value.status_code == 400
    assert ".json" in str(e.value.detail)


def test_resolve_既无project_id又无file拒绝400():
    with pytest.raises(HTTPException) as e:
        ui_case_gate.preview([_case()])
    assert e.value.status_code == 400


def test_resolve_经project_id解析kind_ui项目(ui_file, monkeypatch):
    monkeypatch.setattr(ui_case_gate.project_store, "get",
                        lambda pid: {"id": pid, "kind": "ui", "file": str(ui_file)})
    rep = ui_case_gate.preview([_case()], project_id="proj_ui")
    assert rep["checked"] == 1 and rep["file"] == ui_file.name


# ==================== preview：逐条权威校验不落盘 ====================

def test_preview_空用例拒绝400():
    with pytest.raises(HTTPException) as e:
        ui_case_gate.preview([], file="x.json")
    assert e.value.status_code == 400


def test_preview_全部合法逐条通过(ui_file):
    rep = ui_case_gate.preview([_case("UI-001"), _case("UI-002")], file=str(ui_file))
    assert rep["platform"] == "interface-auto"
    assert rep["checked"] == 2 and rep["passed"] == 2 and rep["failed"] == 0
    assert all(it["passed"] and not it["exists"] for it in rep["items"])
    # 权威口径来自 ui_case_store：非法动作被逐条定位到问题
    assert rep["summary"]


def test_preview_字段非法逐条定位(ui_file):
    rep = ui_case_gate.preview(
        [_case("UI-001"), _case("UI-002", steps=[{"action": "fly", "locator": "css=#a"}])],
        file=str(ui_file))
    assert rep["passed"] == 1 and rep["failed"] == 1
    bad = rep["items"][1]
    assert bad["passed"] is False and bad["problems"]


def test_preview_无步骤非法(ui_file):
    rep = ui_case_gate.preview([_case("UI-001", steps=[])], file=str(ui_file))
    assert rep["failed"] == 1
    assert any("步骤" in p for p in rep["items"][0]["problems"])


def test_preview_编号已存在提示自动改名(ui_file):
    store.create_case(ui_file, _case("UI-001"))
    it = ui_case_gate.preview([_case("UI-001")], file=str(ui_file))["items"][0]
    assert it["passed"] is True and it["exists"] is True
    # 口径与接口用例 preview 不同：明确预告「自动递增改名」
    assert any("自动递增改名" in p for p in it["problems"])
    # 校验不落盘
    assert [c["case_id"] for c in store.load_cases(ui_file)] == ["UI-001"]


# ==================== commit：整批拒绝 + 冲突改名 + meta ====================

def test_commit_空用例拒绝400():
    with pytest.raises(HTTPException) as e:
        ui_case_gate.commit([], file="x.json")
    assert e.value.status_code == 400


def test_commit_全合法整批落库(ui_file):
    res = ui_case_gate.commit([_case("UI-001"), _case("UI-002")], file=str(ui_file))
    assert res["committed"] == 2 and res["skipped"] == 0 and res["renamed"] == []
    assert {c["case_id"] for c in store.load_cases(ui_file)} == {"UI-001", "UI-002"}


def test_commit_任一非法即整批拒绝不落盘(ui_file):
    with pytest.raises(HTTPException) as e:
        ui_case_gate.commit(
            [_case("UI-001"), _case("UI-002", steps=[{"action": "fly", "locator": "css=#a"}])],
            file=str(ui_file))
    assert e.value.status_code == 400
    assert e.value.detail["rejected"]
    # 合法那条也不落：整批拒绝语义
    assert store.load_cases(ui_file) == []


def test_commit_编号冲突自动递增改名且回带映射(ui_file):
    store.create_case(ui_file, _case("UI-001"))
    res = ui_case_gate.commit([_case("UI-001")], file=str(ui_file))
    assert res["renamed"] == [{"from": "UI-001", "to": "UI-002"}]
    assert "自动改名" in res["summary"]
    assert {c["case_id"] for c in store.load_cases(ui_file)} == {"UI-001", "UI-002"}


def test_commit_同批内部撞号逐个让位(ui_file):
    store.create_case(ui_file, _case("UI-001"))
    res = ui_case_gate.commit([_case("UI-001"), _case("UI-001")], file=str(ui_file))
    assert res["committed"] == 2
    assert {c["case_id"] for c in store.load_cases(ui_file)} == {"UI-001", "UI-002", "UI-003"}
    assert len(res["renamed"]) == 2


def test_commit_无数字尾段追加后缀(ui_file):
    store.create_case(ui_file, _case("ALPHA"))
    res = ui_case_gate.commit([_case("ALPHA")], file=str(ui_file))
    assert res["renamed"][0]["to"] == "ALPHA-2"
    assert "ALPHA-2" in {c["case_id"] for c in store.load_cases(ui_file)}


def test_commit_meta原样回显供审计关联(ui_file):
    res = ui_case_gate.commit([_case("UI-001")], file=str(ui_file),
                              meta={"origin": "chat", "trace_id": "t-42"})
    assert res["meta"] == {"origin": "chat", "trace_id": "t-42"}
    assert ui_case_gate.commit([_case("UI-002")], file=str(ui_file))["meta"] == {}


def test_commit_新项目文件不存在也可落库(tmp_path):
    # UI 项目可能还没有用例文件：resolve 不要求文件已存在，commit 首写创建之
    p = tmp_path / "fresh.json"
    res = ui_case_gate.commit([_case("UI-001")], file=str(p))
    assert res["committed"] == 1
    assert [c["case_id"] for c in store.load_cases(p)] == ["UI-001"]
