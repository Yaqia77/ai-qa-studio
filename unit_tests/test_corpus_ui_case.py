# -*- coding: utf-8 -*-
"""backend.agent_bridge.corpus 的 ui_case 域导出单测：形状 / source_uri 回跳 / 步骤渲染 / 增量 / 项目筛选。

沿用 test_agent_bridge.py 的纯函数风格：目标用真实 ui_case_store 的 tmp JSON，
project_store 用 monkeypatch 喂固定项目清单，锁定「导出逻辑」本身而非真实数据。
关键约束：
- 一条 UI 用例 = 一个完整语义块，正文带中文步骤序列（检索召回友好）；
- source_uri 必须能回跳平台 /api/ui-cases/{case_id}?file=...；
- _ui_projects 只认 kind=ui 且 .json 的项目（接口/App 项目不灌进 UI 语料域）。
    .venv/Scripts/python.exe -m pytest unit_tests/test_corpus_ui_case.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent_bridge import corpus  # noqa: E402
from backend.shared import project_store  # noqa: E402
from backend.ui import ui_case_store as store  # noqa: E402

BASE = "http://svc.test"


def _case(cid="UI-001", **over):
    base = {
        "case_id": cid, "module": "登录", "name": "用户登录",
        "desc": "验证账号密码登录成功", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": "/login"},
            {"action": "fill", "locator": "css=#username", "value": "{{username}}"},
            {"action": "click", "locator": "css=#submit"},
            {"action": "expect_text", "locator": "css=.welcome", "value": "欢迎"},
        ],
        "extract": [{"var": "order_id", "source": "text|css=.order-id"}],
    }
    base.update(over)
    return base


@pytest.fixture
def ui_json(tmp_path):
    p = tmp_path / "cases.json"
    store.scaffold(p)
    store.create_case(p, _case())
    return p


# ==================== 导出形状 ====================

def test_ui_case_records_shape(ui_json):
    recs = corpus.ui_case_records(BASE, file=str(ui_json))
    assert len(recs) == 1
    r = recs[0]
    for field in ("kb_id", "domain", "platform", "source_uri", "title", "text"):
        assert str(r.get(field) or "").strip(), f"缺必填字段 {field}"
    assert r["domain"] == "ui_case" and r["platform"] == "interface-auto"
    assert r["source_type"] == "ui_json_case"


def test_ui_case_source_uri_回跳平台(ui_json):
    r = corpus.ui_case_records(BASE, file=str(ui_json))[0]
    assert r["source_uri"].startswith(BASE + "/api/ui-cases/UI-001")
    assert "file=" in r["source_uri"]           # 带来源文件便于定位


def test_ui_case_text_渲染中文步骤序列(ui_json):
    text = corpus.ui_case_records(BASE, file=str(ui_json))[0]["text"]
    assert "模块：登录" in text and "优先级：P0" in text
    assert "步骤序列" in text
    # 动作中文名来自 ui_case_store.ACTIONS：goto→打开地址、click→点击、expect_text→断言文本
    assert "打开地址" in text and "点击" in text and "断言文本" in text
    assert "变量提取：order_id=text|css=.order-id" in text


def test_ui_case_kb_id_稳定(ui_json):
    a = corpus.ui_case_records(BASE, file=str(ui_json))[0]["kb_id"]
    b = corpus.ui_case_records(BASE, file=str(ui_json))[0]["kb_id"]
    assert a == b and a.startswith("kb_")


def test_ui_case_since_按mtime增量(ui_json):
    stamp = datetime.fromtimestamp(ui_json.stat().st_mtime).isoformat(timespec="seconds")
    assert len(corpus.ui_case_records(BASE, file=str(ui_json))) == 1
    # 文件未改动、水位已到 mtime → 整批不返回
    assert corpus.ui_case_records(BASE, since=stamp, file=str(ui_json)) == []


def test_ui_case_无名用例被过滤(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"version": 1, "cases": [{"case_id": "", "name": "x", "steps": []}]}',
                 encoding="utf-8")
    assert corpus.ui_case_records(BASE, file=str(p)) == []


def test_ui_case_坏文件不抛(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert corpus.ui_case_records(BASE, file=str(p)) == []


def test_ui_case_缺失文件返回空(tmp_path):
    assert corpus.ui_case_records(BASE, file=str(tmp_path / "nope.json")) == []


# ==================== 单步渲染 ====================

def test_render_ui_step_取值与定位器():
    assert "打开地址" in corpus._render_ui_step({"action": "goto", "value": "/login"})
    s = corpus._render_ui_step({"action": "click", "locator": "css=#a"})
    assert "点击" in s and "css=#a" in s
    # 未知动作降级用动作键本身兜底，不抛
    assert corpus._render_ui_step({"action": "mystery"})


# ==================== 项目筛选：只认 kind=ui + .json ====================

def test_ui_projects_只取kind_ui_json(monkeypatch):
    projects = [
        {"id": "1", "kind": "ui", "file": "data/cases/a.json"},
        {"id": "2", "kind": "api", "file": "data/api_cases.xlsx"},
        {"id": "3", "kind": "ui", "file": "data/cases/b.apjson"},
        {"id": "4", "kind": "app", "file": "data/cases/c.json"},
    ]
    monkeypatch.setattr(project_store, "list_projects", lambda: projects)
    picked = corpus._ui_projects()
    assert [p["id"] for p in picked] == ["1"]


def test_ui_projects_按project_id限定(monkeypatch):
    projects = [
        {"id": "1", "kind": "ui", "file": "a.json"},
        {"id": "2", "kind": "ui", "file": "b.json"},
    ]
    monkeypatch.setattr(project_store, "list_projects", lambda: projects)
    assert [p["id"] for p in corpus._ui_projects("2")] == ["2"]


# ==================== 域分发 ====================

def test_export_domain_ui_case_dispatch(ui_json):
    recs = corpus.export_domain("ui_case", BASE, file=str(ui_json))
    assert len(recs) == 1 and recs[0]["domain"] == "ui_case"


def test_ui_case_in_domain_tuples():
    assert "ui_case" in corpus.ALL_DOMAINS
    assert "ui_case" in corpus.EXPORTABLE_DOMAINS
