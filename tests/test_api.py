"""参数化接口用例执行入口（用例来自 Excel，见 conftest.py 的 pytest_generate_tests）。"""
import json
import time

import pytest

from core.engines import get_engine

try:
    import allure
    HAS_ALLURE = True
except ImportError:  # 未安装 allure-pytest 时跳过增强报告
    HAS_ALLURE = False

SEVERITY = {"P0": "blocker", "P1": "critical", "P2": "major", "P3": "minor"}


def test_api(api_case, executor, context, dep_map, case_results):
    if HAS_ALLURE:
        allure.dynamic.title(f"[{api_case.priority}] {api_case.case_id} {api_case.api_name}")
        allure.dynamic.feature(api_case.module)
        allure.dynamic.severity(SEVERITY.get(api_case.priority, "normal"))
        allure.dynamic.description(api_case.case_desc)

    if not api_case.run:
        # 登记停用状态：下游依赖用例据此阻塞，而不是带着未替换的变量照常发请求
        case_results[api_case.case_id] = "skipped"
        pytest.skip("是否执行=N，跳过")

    # 依赖链检查（阻塞式）：已执行的依赖用例必须全部通过，否则不发请求直接跳过；
    # 依赖未在本次执行范围（None）不强制检查，维持 --cases 精确选择的既有契约。
    deps = dep_map.get(api_case.case_id, [])
    if deps:
        failed_deps = [d for d in deps if case_results.get(d) not in ("passed", None)]
        if failed_deps:
            case_results[api_case.case_id] = "skipped"
            pytest.skip(f"因依赖用例未通过而阻塞（未发起请求）: {', '.join(failed_deps)}")

    # 统一执行入口：按 key 取引擎多态执行（内部委托 CaseRunner，与旧直调完全一致）。
    start = time.perf_counter()
    result = get_engine("api").execute(api_case, context, executor=executor)
    result.duration = time.perf_counter() - start

    if HAS_ALLURE:
        if result.script_trace:
            _attach_scripts(result.script_trace)
        tags = []
        if api_case.pre_scripts:
            tags.append("前置脚本:" + ",".join(api_case.pre_scripts))
        if api_case.post_scripts:
            tags.append("后置脚本:" + ",".join(api_case.post_scripts))
        if tags:
            allure.dynamic.tag(*tags)
        if result.request_snapshot:
            _attach(result.request_snapshot, f"请求报文 {api_case.method} {api_case.url}")
        if result.response_snapshot:
            _attach(result.response_snapshot, "响应报文")
        if result.assertions:
            _attach_assertions(result.assertions)

    if not result.ok:
        case_results[api_case.case_id] = "failed"
        raise AssertionError(result.error)
    else:
        case_results[api_case.case_id] = "passed"


def _attach(snapshot: dict, name: str):
    allure.attach(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        name=name,
        attachment_type=allure.attachment_type.JSON,
    )


def _attach_assertions(assertions):
    """逐条断言结果落盘（op/path/expected/actual/passed/message），供平台报告详情“断言结果”视图消费。"""
    rows = [
        {
            "op": a.op,
            "path": a.path,
            "expected": a.expected,
            "actual": a.actual,
            "passed": bool(a.passed),
            "message": a.message,
        }
        for a in assertions
    ]
    allure.attach(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        name="断言结果",
        attachment_type=allure.attachment_type.JSON,
    )


def _attach_scripts(trace):
    """公共脚本执行轨迹（前置/后置逐步结果），供平台报告详情与 Allure 附件回溯。"""
    allure.attach(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        name="脚本轨迹",
        attachment_type=allure.attachment_type.JSON,
    )
