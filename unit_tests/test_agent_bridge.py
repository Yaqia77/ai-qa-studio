# -*- coding: utf-8 -*-
"""backend.agent_bridge 单元测试：语料导出形状 / 增量 / 断言语义化 / 路由开关。

设计意图与门禁：
- 语料记录必须符合统一形状且带**可回跳 source_uri**，缺一即不可信（Agent 侧校验闸门会拒收）；
- 平台只导出 D1 契约 + D2 用例，其余域返回空但不报错（结构预留）；
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

from backend.agent_bridge import case_gate  # noqa: E402
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

def test_main_app_agent_router_follows_switch(monkeypatch):
    """/api/agent 注册与否只由 agent.enabled 决定：关则不挂、开则挂。

    不读本机 config 实值（开发机常置 true），用 monkeypatch + reload 锁两个分支，
    测完按真实配置恢复，不影响后续用例。
    """
    import importlib

    import backend.main as main
    # 新版 FastAPI 把 include_router 的结果存成嵌套 _IncludedRouter，app.routes 里扫不到子路由 path，
    # 改用 openapi 的 paths 判定（也是「端点是否对外暴露」的权威口径）
    agent_paths = lambda: [p for p in main.app.openapi()["paths"] if p.startswith("/api/agent")]  # noqa: E731

    monkeypatch.setattr(agent_router, "agent_enabled", lambda cfg=None: False)
    importlib.reload(main)
    assert agent_paths() == []                        # 关闭态零回归：端点不进应用

    monkeypatch.setattr(agent_router, "agent_enabled", lambda cfg=None: True)
    importlib.reload(main)
    assert len(agent_paths()) == 3                    # corpus + preview + commit 就位

    monkeypatch.undo()
    importlib.reload(main)                      # 恢复本机真实配置形态


# ==================== preview / commit 落库闸门 ====================
# 沿用纯函数风格：直接调 case_gate.preview/commit，用 monkeypatch 喂权威校验桩，
# 不依赖 starlette.testclient（本环境未装 httpx2）。校验口径本身由 excel_loader 负责，
# 这里锁定的是"闸门编排"：字段非法/判重/整批拒绝不落盘/编号跳过 的对外形状。

from core.excel_loader import ExcelFormatError  # noqa: E402


def _target(tmp_path):
    f = tmp_path / "cases.xlsx"
    f.write_bytes(b"stub")
    return f


def _no_existing(monkeypatch):
    monkeypatch.setattr(case_gate.excel_loader, "load",
                        lambda p, allow_empty=False: types.SimpleNamespace(cases=[]))


def _ok_validate(monkeypatch):
    monkeypatch.setattr(case_gate.excel_loader, "validate_case", _ok_tc)


def _ok_tc(row, *, file_name="inline", row_no=0):
    return types.SimpleNamespace(case_id=str(row.get("case_id") or ""))


def test_preview_全部合法逐条通过(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _no_existing(monkeypatch)
    _ok_validate(monkeypatch)
    rep = case_gate.preview([{"case_id": "A-1"}, {"case_id": "A-2"}], file=str(f))
    assert rep["checked"] == 2 and rep["passed"] == 2 and rep["failed"] == 0
    assert rep["platform"] == "interface-auto"
    assert all(it["passed"] and not it["exists"] for it in rep["items"])


def test_preview_字段非法定位到问题(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _no_existing(monkeypatch)

    def vc(row, *, file_name="inline", row_no=0):
        if str(row.get("method") or "").upper() not in case_gate.excel_loader.VALID_METHODS:
            raise ExcelFormatError("Excel 校验失败，共 1 处错误:\n[inline] 请求方式非法")
        return types.SimpleNamespace(case_id=row.get("case_id"))
    monkeypatch.setattr(case_gate.excel_loader, "validate_case", vc)
    rep = case_gate.preview([{"case_id": "A-1", "method": "BAD"}], file=str(f))
    assert rep["passed"] == 0 and rep["failed"] == 1
    assert rep["items"][0]["passed"] is False and rep["items"][0]["problems"]


def test_preview_编号已存在提示但不判失败(monkeypatch, tmp_path):
    f = _target(tmp_path)
    monkeypatch.setattr(case_gate.excel_loader, "load",
                        lambda p, allow_empty=False: types.SimpleNamespace(
                            cases=[types.SimpleNamespace(case_id="A-1")]))
    _ok_validate(monkeypatch)
    it = case_gate.preview([{"case_id": "A-1"}], file=str(f))["items"][0]
    assert it["passed"] is True and it["exists"] is True
    assert any("已存在" in p for p in it["problems"])


def test_preview_目标文件不存在404(tmp_path):
    with pytest.raises(HTTPException) as e:
        case_gate.preview([{"case_id": "A"}], file=str(tmp_path / "nope.xlsx"))
    assert e.value.status_code == 404


def test_preview_空用例400():
    with pytest.raises(HTTPException) as e:
        case_gate.preview([])
    assert e.value.status_code == 400


def test_commit_全合法整批落库(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _no_existing(monkeypatch)
    _ok_validate(monkeypatch)
    seen = {}

    def fake_bulk(path, tcs):
        seen["tcs"] = tcs
        return {"imported": len(tcs), "skipped": 0, "errors": []}
    monkeypatch.setattr(case_gate.excel_writer, "bulk_append", fake_bulk)
    res = case_gate.commit([{"case_id": "A-1"}, {"case_id": "A-2"}], file=str(f))
    assert res["committed"] == 2 and len(seen["tcs"]) == 2


def test_commit_任一非法即整批拒绝不落盘(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _no_existing(monkeypatch)

    def vc(row, *, file_name="inline", row_no=0):
        raise ExcelFormatError("失败:\n[inline] 断言不能为空")
    monkeypatch.setattr(case_gate.excel_loader, "validate_case", vc)
    touched = {"bulk": False}
    monkeypatch.setattr(case_gate.excel_writer, "bulk_append",
                        lambda *a, **k: touched.__setitem__("bulk", True))
    with pytest.raises(HTTPException) as e:
        case_gate.commit([{"case_id": "A-1"}], file=str(f))
    assert e.value.status_code == 400 and e.value.detail["rejected"]
    assert touched["bulk"] is False          # 闸门不过，绝不写文件


def test_commit_经project_id解析目标(monkeypatch, tmp_path):
    f = _target(tmp_path)
    monkeypatch.setattr(case_gate.project_store, "get", lambda pid: {"id": pid, "file": str(f)})
    _no_existing(monkeypatch)
    _ok_validate(monkeypatch)
    monkeypatch.setattr(case_gate.excel_writer, "bulk_append",
                        lambda path, tcs: {"imported": len(tcs), "skipped": 0, "errors": []})
    assert case_gate.commit([{"case_id": "A-1"}], project_id="proj_x")["committed"] == 1


def test_commit_项目不存在404(monkeypatch):
    monkeypatch.setattr(case_gate.project_store, "get", lambda pid: None)
    with pytest.raises(HTTPException) as e:
        case_gate.commit([{"case_id": "A"}], project_id="ghost")
    assert e.value.status_code == 404


# ------------------------------------------------------------------ P5：冲突自动改名 + meta

def _existing(monkeypatch, *ids):
    monkeypatch.setattr(case_gate.excel_loader, "load",
                        lambda p, allow_empty=False: types.SimpleNamespace(
                            cases=[types.SimpleNamespace(case_id=i) for i in ids]))


def _capture_bulk(monkeypatch):
    seen = {}

    def fake_bulk(path, tcs):
        seen["ids"] = [t.case_id for t in tcs]
        return {"imported": len(tcs), "skipped": 0, "errors": []}
    monkeypatch.setattr(case_gate.excel_writer, "bulk_append", fake_bulk)
    return seen


def test_commit_编号冲突自动递增改名且回带映射(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _existing(monkeypatch, "AL-01")
    _ok_validate(monkeypatch)
    seen = _capture_bulk(monkeypatch)
    res = case_gate.commit([{"case_id": "AL-01"}], file=str(f))
    assert seen["ids"] == ["AL-02"]                     # 保持补零宽度递增，不静默覆盖
    assert res["renamed"] == [{"from": "AL-01", "to": "AL-02"}]
    assert "自动改名" in res["summary"]


def test_commit_同批内部撞号也逐个让位(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _existing(monkeypatch, "AL-01")
    _ok_validate(monkeypatch)
    seen = _capture_bulk(monkeypatch)
    res = case_gate.commit([{"case_id": "AL-01"}, {"case_id": "AL-01"}], file=str(f))
    assert seen["ids"] == ["AL-02", "AL-03"]            # 第一条占了新号，第二条接着让
    assert len(res["renamed"]) == 2


def test_commit_无数字尾段追加后缀(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _existing(monkeypatch, "ALPHA")
    _ok_validate(monkeypatch)
    seen = _capture_bulk(monkeypatch)
    case_gate.commit([{"case_id": "ALPHA"}], file=str(f))
    assert seen["ids"] == ["ALPHA-2"]


def test_commit_meta原样回显供审计关联(monkeypatch, tmp_path):
    f = _target(tmp_path)
    _no_existing(monkeypatch)
    _ok_validate(monkeypatch)
    _capture_bulk(monkeypatch)
    res = case_gate.commit([{"case_id": "A-1"}], file=str(f),
                           meta={"origin": "chat", "trace_id": "t-42"})
    assert res["meta"] == {"origin": "chat", "trace_id": "t-42"}
    # 不传 meta 的旧调用（表单页/冒烟）向后兼容：回空字典不报错
    assert case_gate.commit([{"case_id": "A-2"}], file=str(f))["meta"] == {}


def test_router_preview_commit_未启用404(monkeypatch):
    body = case_gate.CaseGenerateBody(cases=[{"case_id": "A"}])
    monkeypatch.setattr(agent_router, "agent_enabled", lambda cfg=None: False)
    for fn in (agent_router.preview_cases, agent_router.commit_cases):
        with pytest.raises(HTTPException) as e:
            fn(body, {})
        assert e.value.status_code == 404


def test_router_preview_启用后透传(monkeypatch):
    body = case_gate.CaseGenerateBody(cases=[{"case_id": "A"}, {"case_id": "B"}])
    monkeypatch.setattr(agent_router, "agent_enabled", lambda cfg=None: True)
    monkeypatch.setattr(agent_router.case_gate, "preview",
                        lambda cases, project_id="", file="": {"ok": True, "n": len(cases)})
    assert agent_router.preview_cases(body, {}) == {"ok": True, "n": 2}


def test_main_app_excludes_new_agent_routes_when_disabled():
    """preview/commit 亦受默认关闭保护：存量零回归。"""
    import backend.main as main
    paths = {getattr(rt, "path", "") for rt in main.app.routes}
    assert not any(p.startswith("/api/agent") for p in paths)
