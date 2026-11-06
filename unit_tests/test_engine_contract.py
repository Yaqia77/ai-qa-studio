# -*- coding: utf-8 -*-
"""引擎统一入口契约测试（三合一：api/web/app 三引擎就位，零行为变更）。

验证三件事，全部不依赖浏览器/fastapi：
1. 注册表可实例化（三引擎都实现了新增的抽象方法），分流映射按互补后缀判据覆盖；
2. validate 委托既有单用例校验（api=excel_loader 行解析、web=ui_case_store、app=app_case_store）；
3. execute 委托既有 runner（api→CaseRunner、web→UiRunner、app→AppRunner 脚手架），产出对应结果对象。
    .venv/Scripts/python.exe -m pytest unit_tests/test_engine_contract.py -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import Context  # noqa: E402
from core.engines import REGISTRY, engine_for_file, get_engine  # noqa: E402
from core.engines.base import Engine  # noqa: E402
from core.excel_loader import ExcelFormatError  # noqa: E402
from core.models import CaseResult  # noqa: E402


# ==================== 1. 注册表与分流映射 ====================

def test_registry_instantiable_and_keys():
    # 新增抽象方法后仍可实例化 = 三引擎都实现了 validate/execute
    assert {e.key for e in REGISTRY} == {"api", "web", "app"}
    assert all(isinstance(e, Engine) for e in REGISTRY)


def test_get_engine_by_key():
    assert get_engine("api").key == "api"
    assert get_engine("web").key == "web"
    assert get_engine("app").key == "app"
    with pytest.raises(KeyError):
        get_engine("unknown")


def test_engine_for_file_mapping_unchanged():
    assert engine_for_file(Path("data/ui_demo.json")).key == "web"
    assert engine_for_file(Path("data/app_demo.apjson")).key == "app"
    assert engine_for_file(Path("data/api_cases.xlsx")).key == "api"
    # 兜底：.json→web、.apjson→app 之外的后缀均由 api 认领，不返回 None
    assert engine_for_file(Path("whatever.txt")).key == "api"


# ==================== 2. validate 委托既有校验 ====================

def _api_row(**over):
    row = {
        "case_id": "api_c1", "module": "冒烟", "api_name": "健康检查",
        "case_desc": "契约测试", "priority": "P0", "run": "Y",
        "method": "GET", "url": "/api/health",
        "asserts": '[["eq", "status_code", 200]]',
    }
    row.update(over)
    return row


def test_api_validate_normalizes():
    case = get_engine("api").validate(_api_row())
    assert case.case_id == "api_c1" and case.method == "GET" and case.run is True
    assert case.asserts == [["eq", "status_code", 200]]


def test_api_validate_rejects_bad_method():
    with pytest.raises(ExcelFormatError):
        get_engine("api").validate(_api_row(method="TELEPORT"))


def test_api_validate_accepts_body_as_object():
    """Agent 内联校验传结构化 body（dict/list）必须原样接受，
    不能被 str() 成单引号 repr 后 json.loads 失败（历史缺陷）。"""
    case = get_engine("api").validate(
        _api_row(method="POST", body={"albumType": 1, "userId": "{{user_id}}"}))
    assert case.body == {"albumType": 1, "userId": "{{user_id}}"}
    # 字符串形态仍兼容（Excel 单元格路径）
    case2 = get_engine("api").validate(_api_row(method="POST", body='{"a": 1}'))
    assert case2.body == {"a": 1}


def test_web_validate_normalizes():
    cleaned = get_engine("web").validate(
        {"case_id": "ui_c1", "steps": [{"action": "goto", "value": "/login"}]})
    assert cleaned["case_id"] == "ui_c1"
    assert cleaned["priority"] == "P2" and cleaned["browser"] == "chromium"
    assert len(cleaned["steps"]) == 1


def test_web_validate_rejects_empty_steps():
    from backend.ui.ui_case_store import UiCaseError
    with pytest.raises(UiCaseError):
        get_engine("web").validate({"case_id": "ui_c2", "steps": []})


def test_app_validate_normalizes():
    cleaned = get_engine("app").validate(
        {"case_id": "app_c1", "platform": "android", "app": "com.demo",
         "steps": [{"action": "launch", "value": "com.demo"}]})
    assert cleaned["case_id"] == "app_c1"
    assert cleaned["priority"] == "P2" and cleaned["platform"] == "android"
    assert len(cleaned["steps"]) == 1


def test_app_validate_rejects_empty_steps():
    from backend.app.app_case_store import AppCaseError
    with pytest.raises(AppCaseError):
        get_engine("app").validate({"case_id": "app_c2", "steps": []})


# ==================== 3. execute 委托既有 runner ====================

class _BoomExecutor:
    """发请求即抛异常，用于在不联网情况下验证 CaseRunner 委托链路（run 不抛，写进结果）。"""

    def execute(self, *a, **k):
        raise RuntimeError("network down")


def test_api_execute_delegates_to_case_runner():
    case = get_engine("api").validate(_api_row())
    result = get_engine("api").execute(case, Context({}, {}), executor=_BoomExecutor())
    assert isinstance(result, CaseResult)
    assert result.ok is False
    assert result.error.startswith("用例执行异常")


def test_web_execute_returns_ui_result():
    from core.engines.web.engine import UiRunResult
    # 无浏览器环境下 UiRunner.run 不抛异常，兜底为用例失败并返回结果对象
    result = get_engine("web").execute(
        {"case_id": "ui_c3", "steps": [{"action": "goto", "value": "http://127.0.0.1:1/x"}]},
        Context({}, {}), base_url="")
    assert isinstance(result, UiRunResult)
    assert result.ok is False


def test_app_execute_returns_not_implemented():
    from core.engines.app.engine import AppRunResult, NOT_IMPLEMENTED
    # 脚手架阶段：驱动未接入，execute 不抛异常，返回 ok=False + 未实现指引
    result = get_engine("app").execute(
        {"case_id": "app_c3", "steps": [{"action": "launch", "value": "com.demo"}]},
        Context({}, {}), base_url="")
    assert isinstance(result, AppRunResult)
    assert result.ok is False
    assert result.error == NOT_IMPLEMENTED
