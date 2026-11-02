# -*- coding: utf-8 -*-
"""backend.agent_bridge 单元测试：语料导出形状 / 增量 / 断言语义化 / 路由开关。

设计意图与门禁（设计文档 3.1、4.2、七章）：
- 语料记录必须符合统一形状且带**可回跳 source_uri**，缺一即不可信（Agent 侧校验闸门会拒收）；
- 平台一期只导出 D1 契约 + D2 用例，其余域返回空但不报错（结构预留）；
- **默认关闭零回归**：agent.enabled=false 时 main.app 不含 /api/agent 路由，
  且桥接端点在关闭态直接 404 —— 存量平台行为与改动前完全一致。

用真实仓库数据会让断言随数据漂移，这里用 monkeypatch 喂固定语料，锁定"导出逻辑"本身。
    .venv/Scripts/python.exe -m pytest unit_tests/test_agent_bridge.py -v
"""
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent_bridge import corpus  # noqa: E402
from backend.agent_bridge import router as agent_router  # noqa: E402
from core.models import TestCase as CaseRow  # noqa: E402  （别名避开 pytest 把 Test* 当用例类收集）

BASE = "http://svc.test"

# 一条完整接口定义（覆盖 headers/params/body/extract/asserts 各渲染分支）
_DEF = {
    "name": "获取相册列表",
    "module": "相册-列表",
    "method": "POST",
    "path": "/atlas/list",
    "remark": "由 16 条存量用例聚合",
    "headers": {"Authorization": "{{token}}", "Content-Type": "application/json"},
    "params": None,
    "body": {"userId": "{{user_id}}", "pageInfo": {"pageSize": 10}},
    "extract": {"total": "$.data.totalCount"},
    "asserts": [["eq", "status_code", 200], ["not_null", "$.data"], ["eq", "$.code", "0000"]],
    "source": "excel",
    "updated_at": "2026-10-22T15:19:13",
}


def _case(cid="album_list_001"):
    return CaseRow(
        row=2, source="api_cases.xlsx", case_id=cid, module="相册-列表",
        api_name="获取相册列表", case_desc="冒烟：全参数正常请求", priority="P0",
        run=True, method="POST", url="/atlas/list",
        headers={"Authorization": "{{token}}"}, params=None,
        body={"userId": "{{user_id}}"}, extract={"token": "$.data.token"},
        asserts=[["eq", "$.code", "0000"], ["not_null", "$.data"]],
        pre_scripts=["login"], post_scripts=[],
    )


@pytest.fixture
def patched(monkeypatch):
    """把两处数据源换成固定桩：接口定义库 + Excel 读取（不碰真实文件）。"""
    monkeypatch.setattr(corpus.api_def_store, "list_defs", lambda: [dict(_DEF)])

    def fake_load(path, allow_empty=False):
        return types.SimpleNamespace(cases=[_case()], variables=set())
    monkeypatch.setattr(corpus.excel_loader, "load", fake_load)
    return _DEF


# ==================== D1 契约域导出 ====================

def test_contract_records_shape(patched):
    recs = corpus.contract_records(BASE)
    assert len(recs) == 1
    r = recs[0]
    for field in ("kb_id", "domain", "platform", "source_uri", "title", "text"):
        assert str(r.get(field) or "").strip(), f"缺必填字段 {field}"
    assert r["domain"] == "contract" and r["platform"] == "interface-auto"
    assert r["source_type"] == "api_def"
    # source_uri 必须能回跳平台接口定义详情
    assert r["source_uri"].startswith(BASE + "/api/defs/")
    # 正文含方法/路径，且变量引用原样保留（不解析真实凭证）
    assert "POST /atlas/list" in r["text"]
    assert "Authorization={{token}}" in r["text"]


def test_contract_kb_id_is_stable(patched):
    a = corpus.contract_records(BASE)[0]["kb_id"]
    b = corpus.contract_records(BASE)[0]["kb_id"]
    assert a == b and a.startswith("kb_")


def test_contract_renders_semantic_asserts(patched):
    text = corpus.contract_records(BASE)[0]["text"]
    assert "推荐断言" in text
    assert "断言 status_code 等于 200" in text
    assert "断言 $.data 不为空" in text      # not_null 无期望值
    assert "断言 $.code 等于 0000" in text


def test_contract_since_filters_unchanged(patched):
    assert len(corpus.contract_records(BASE, since="")) == 1
    # 水位等于当前最大 updated_at → 无新增（增量不重复灌）
    assert corpus.contract_records(BASE, since=_DEF["updated_at"]) == []
    assert corpus.contract_records(BASE, since="9999-01-01T00:00:00") == []


def test_contract_skips_unnamed_def(patched, monkeypatch):
    blank = dict(_DEF, name="   ")
    monkeypatch.setattr(corpus.api_def_store, "list_defs", lambda: [dict(_DEF), blank])
    recs = corpus.contract_records(BASE)
    assert len(recs) == 1                       # 无名定义被过滤掉
    assert all(r["title"].strip() for r in recs)


# ==================== D2 用例域导出 ====================

def test_case_records_shape(patched, tmp_path):
    f = tmp_path / "cases.xlsx"
    f.write_bytes(b"stub")
    recs = corpus.case_records(BASE, file=str(f))
    assert len(recs) == 1
    r = recs[0]
    assert r["domain"] == "case" and r["source_type"] == "excel_case"
    assert r["source_uri"].startswith(BASE + "/api/cases/album_list_001")
    assert "file=" in r["source_uri"]        # 带来源文件便于回跳定位
    assert "冒烟：全参数正常请求" in r["text"]
    assert "断言 $.code 等于 0000" in r["text"]
    assert "前置脚本=login" in r["text"]


def test_case_records_since_by_mtime(patched, tmp_path):
    f = tmp_path / "cases.xlsx"
    f.write_bytes(b"stub")
    from datetime import datetime
    stamp = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
    assert len(corpus.case_records(BASE, file=str(f))) == 1
    # 文件未改动，水位已到 mtime → 整批不返回
    assert corpus.case_records(BASE, since=stamp, file=str(f)) == []


def test_case_records_missing_file_yields_empty(patched, tmp_path):
    assert corpus.case_records(BASE, file=str(tmp_path / "nope.xlsx")) == []


def test_case_records_bad_excel_does_not_raise(patched, tmp_path, monkeypatch):
    f = tmp_path / "cases.xlsx"
    f.write_bytes(b"stub")

    def boom(path, allow_empty=False):
        raise ValueError("坏文件")
    monkeypatch.setattr(corpus.excel_loader, "load", boom)
    assert corpus.case_records(BASE, file=str(f)) == []


# ==================== 域分发 ====================

@pytest.mark.parametrize("domain,count", [("contract", 1), ("case", 1)])
def test_export_domain_dispatch(patched, tmp_path, domain, count):
    f = tmp_path / "c.xlsx"
    f.write_bytes(b"stub")
    recs = corpus.export_domain(domain, BASE, file=str(f))
    assert len(recs) == count


@pytest.mark.parametrize("domain", ["execution", "business", "convention"])
def test_export_reserved_domains_empty(patched, domain):
    assert corpus.export_domain(domain, BASE) == []


def test_export_unknown_domain_raises(patched):
    with pytest.raises(ValueError):
        corpus.export_domain("nonexistent", BASE)


def test_render_asserts_all_ops():
    out = corpus._render_asserts([["contains", "$.msg", "成功"], ["gt", "$.n", 3]])
    assert "断言 $.msg 包含 成功" in out
    assert "断言 $.n 大于 3" in out
    assert corpus._render_asserts(None) == []
    assert corpus._render_asserts([["bad"], []]) == []   # 结构不合的脏规则静默跳过


# ==================== 路由：开关与错误形状 ====================
# 本环境 starlette.testclient 需要 httpx2（未装），且现有 unit_tests 均为纯函数风格；
# 端点本质是一个普通函数（request 仅用于取 base_url），直接调 + 传桩 request 即可，零额外依赖。

class _Req:
    base_url = "http://testserver/"


def _corpus(monkeypatch, domain: str, *, enabled: bool = True, **params):
    # 直接调用函数不会让 FastAPI 解析 Query 默认值，需显式补齐 since/project_id/file 为空串
    params.setdefault("since", "")
    params.setdefault("project_id", "")
    params.setdefault("file", "")
    monkeypatch.setattr(agent_router, "agent_enabled", lambda cfg=None: enabled)
    return agent_router.export_corpus(_Req(), domain=domain, **params)


def test_router_corpus_ok(monkeypatch, patched):
    body = _corpus(monkeypatch, "contract")
    assert body["platform"] == "interface-auto" and body["domain"] == "contract"
    assert body["exported"] is True and body["count"] == len(body["items"]) == 1
    # source_uri 用请求 base_url 拼绝对地址，便于回跳
    assert body["items"][0]["source_uri"].startswith("http://testserver/api/defs/")


def test_router_corpus_disabled(monkeypatch, patched):
    with pytest.raises(HTTPException) as e:
        _corpus(monkeypatch, "contract", enabled=False)
    assert e.value.status_code == 404


def test_router_unknown_domain_400(monkeypatch, patched):
    with pytest.raises(HTTPException) as e:
        _corpus(monkeypatch, "bogus")
    assert e.value.status_code == 400
    assert "语料域" in e.value.detail


def test_router_reserved_domain_empty_200(monkeypatch, patched):
    body = _corpus(monkeypatch, "execution")
    assert body["count"] == 0 and body["exported"] is False


# ==================== 默认关闭：存量零回归 ====================

def test_main_app_excludes_agent_router_when_disabled():
    """config.yaml 缺省 agent.enabled=false → 真实 main.app 不应挂出 /api/agent 路由。"""
    import backend.main as main
    assert agent_router.agent_enabled() is False
    assert not any(getattr(rt, "path", "").startswith("/api/agent") for rt in main.app.routes)
