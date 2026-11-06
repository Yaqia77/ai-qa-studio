# -*- coding: utf-8 -*-
"""UI 用例执行入口（用例来自项目 JSON，见 conftest.py 的 ui_case 参数化）。

链路复用接口自动化：pytest 收集 → allure 步骤/附件 → allure-results → 平台报告。
每条步骤以 allure.step 记录；截图与步骤轨迹作为附件落入 allure-results，
供平台报告页「UI 步骤」视图消费（截图经 /api/runs/{id}/attachment/{file} 取图）。
"""
import json

import pytest

from core.engines import get_engine

try:
    import allure
    HAS_ALLURE = True
except ImportError:  # 未装 allure-pytest 时降级：不出步骤/附件，但仍正常判定成败
    HAS_ALLURE = False

SEVERITY = {"P0": "blocker", "P1": "critical", "P2": "major", "P3": "minor"}


def _step_cm(label):
    return allure.step(label) if HAS_ALLURE else _null()


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _attach_png(name, data):
    if HAS_ALLURE:
        allure.attach(name=name, body=data, attachment_type=allure.attachment_type.PNG)


def _attach_trace(name, data):
    """失败 trace zip 落 allure-results（extension=zip），供报告页下载 show-trace 回放。"""
    if HAS_ALLURE:
        allure.attach(body=data, name=name, extension="zip")


def test_ui(ui_case, app_config, context, dep_map, case_results, ui_session_registry, ui_browser_pool):
    if HAS_ALLURE:
        allure.dynamic.title(f"[{ui_case.get('priority')}] {ui_case.get('case_id')} {ui_case.get('name')}")
        allure.dynamic.feature(ui_case.get("module", ""))
        allure.dynamic.severity(SEVERITY.get(ui_case.get("priority"), "normal"))
        allure.dynamic.description(ui_case.get("desc", ""))

    if not ui_case.get("run", True):
        case_results[ui_case.get("case_id")] = "skipped"
        pytest.skip("是否执行=N，跳过")

    deps = dep_map.get(ui_case.get("case_id"), [])
    if deps:
        failed_deps = [d for d in deps if case_results.get(d) not in ("passed", None)]
        if failed_deps:
            case_results[ui_case.get("case_id")] = "skipped"
            pytest.skip(f"因依赖用例未通过而阻塞: {', '.join(failed_deps)}")

    base_url = (app_config["env"] or {}).get("base_url", "")
    # 统一执行入口：按 key 取引擎多态执行（内部委托 UiRunner，与旧直调完全一致）。
    # 登录态复用：注入 session_provider 把 case.session 解析成可加载的 storage_state；
    # 未引用会话的存量用例 provider 返回 (None, None)，行为保持不变。
    # 执行提速：注入进程级 ui_browser_pool，浏览器跨用例复用（context 级隔离不变）。
    # 重试：优先级 用例 retry(>0) > 批次默认 ui.retry_default > 0；
    # 存量 JSON 无 retry 键时取 0（不重试）。
    from backend.ui import session_collect

    case_retry = int(ui_case.get("retry") or 0)
    batch_default = int(((app_config.get("config") or {}).get("ui") or {}).get("retry_default") or 0)
    retry = case_retry if case_retry > 0 else batch_default
    # 观测与诊断：trace 留存开关取 config.ui.trace（缺省 on_failure：仅失败时导出）；
    # 无 allure 环境不注入回调（attach_trace=None），引擎端 trace_blob 自然落空，零回归。
    trace_mode = ((app_config.get("config") or {}).get("ui") or {}).get("trace") or "on_failure"

    result = get_engine("web").execute(
        ui_case, context, base_url=base_url,
        attach=(_attach_png if HAS_ALLURE else None),
        step=(_step_cm if HAS_ALLURE else None),
        session_provider=lambda c: session_collect.resolve_for_attach(c, ui_session_registry),
        browser_pool=ui_browser_pool,
        retry=retry,
        trace=trace_mode,
        attach_trace=(_attach_trace if HAS_ALLURE else None),
    )

    if HAS_ALLURE:
        allure.attach(
            json.dumps(result.steps, ensure_ascii=False, indent=2, default=str),
            name="UI步骤", attachment_type=allure.attachment_type.JSON,
        )
        # 重试轨迹仅在真发生过重试（attempts>1）时附加：存量批次报告形状不变（向后兼容）
        if result.attempts > 1:
            allure.attach(
                json.dumps({"attempts": result.attempts, "final_ok": result.ok,
                            "history": result.attempt_history},
                           ensure_ascii=False, indent=2, default=str),
                name="UI重试轨迹", attachment_type=allure.attachment_type.JSON,
            )

    if not result.ok:
        case_results[ui_case.get("case_id")] = "failed"
        raise AssertionError(result.error or "UI 用例失败")
    case_results[ui_case.get("case_id")] = "passed"
