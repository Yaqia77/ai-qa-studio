# -*- coding: utf-8 -*-
"""App 用例执行入口（预留位：脚手架）。

用例来自项目 .apjson，收集链路已与 UI 完全对齐（见 conftest.py 的 app_case 参数化）。
Appium 驱动尚未接入，故本文件在收集到用例后统一 skip——用例能在平台/报告里被统计到，
但不会误判为失败。接入 Appium 后，改为按 get_engine("app").execute(...) 落地即可。
"""
import pytest

from core.engines import get_engine
from core.engines.app.engine import NOT_IMPLEMENTED


def test_app(app_case, dep_map, case_results):
    if not app_case.get("run", True):
        case_results[app_case.get("case_id")] = "skipped"
        pytest.skip("是否执行=N，跳过")

    deps = dep_map.get(app_case.get("case_id"), [])
    if deps:
        blocked = [d for d in deps if case_results.get(d) not in ("passed", None)]
        if blocked:
            case_results[app_case.get("case_id")] = "skipped"
            pytest.skip(f"因依赖用例未通过而阻塞: {', '.join(blocked)}")

    # 驱动未接入：保持收集（用例已落库、可被统计），执行阶段统一跳过。
    engine = get_engine("app")
    assert engine is not None and engine.key == "app"
    case_results[app_case.get("case_id")] = "skipped"
    pytest.skip(NOT_IMPLEMENTED)
