"""接口（api）自动化引擎：单用例执行编排。

链路：前置脚本 -> 变量替换 -> 发请求 -> 变量提取 -> 断言 -> 后置脚本。

本模块由原 core.runner 收入 core/engines/api/；共享基础设施
（http_client/extractor/asserts/models/excel_loader/scripts）与 context/variable 仍在 core 顶层，
故此处按 `core.` 绝对引用。setup_logging 已提到 core.log_setup（两条链路共用）。
文件末尾的 ApiEngine 是注册表分流入口（见 core.engines.base）。
"""
import json
from pathlib import Path

from loguru import logger

from core import scripts
from core import excel_loader
from core.asserts import run_assertions
from core.extractor import extract_variables
from core.http_client import HttpExecutor, request_snapshot, response_snapshot
from core.models import CaseResult, TestCase
from core.variable import render


class CaseRunner:
    """用例执行器：前置脚本 -> 变量替换 -> 发请求 -> 变量提取 -> 断言 -> 后置脚本。

    run() 不抛异常，执行结果（含失败详情）全部写入返回的 CaseResult。
    后置脚本无论主流程成败都尝试执行（典型用途是清理造的数据，避免脏数据残留），
    但后置失败会把用例判为失败并在错误信息里追加原因。
    """

    def __init__(self, executor: HttpExecutor, ctx):
        self.executor = executor
        self.ctx = ctx

    def run(self, case: TestCase) -> CaseResult:
        result = CaseResult(case=case, ok=False)
        try:
            # 0. 前置脚本（造数/登录取 token 等）：失败即中断，不发主请求
            if case.pre_scripts:
                scripts.run_scripts(case.pre_scripts, self.ctx, self.executor, "前置", result.script_trace)

            # 1. 变量替换（url / headers / params / body）
            url = render(case.url, self.ctx)
            headers = render(case.headers, self.ctx) if case.headers else None
            params = render(case.params, self.ctx) if case.params else None
            body = render(case.body, self.ctx) if case.body is not None else None

            # 2. 发送请求并留痕
            response = self.executor.execute(case.method, url, headers, params, body, case.timeout)
            result.request_snapshot = request_snapshot(case.method, url, headers, params, body)
            result.response_snapshot = response_snapshot(response)

            # 3. 变量提取（写入变量池，供后续用例使用）
            if case.extract:
                extracted = extract_variables(response, case.extract)
                for key, value in extracted.items():
                    self.ctx.set(key, value)
                logger.info("[{}] 变量提取: {}", case.case_id, extracted)

            # 4. 断言
            result.assertions = run_assertions(response, case.asserts, self.ctx)
            failed = [a for a in result.assertions if not a.passed]
            result.ok = not failed
            if failed:
                result.error = self._failure_message(case, result, failed)
        except scripts.ScriptError as e:
            result.ok = False
            result.error = f"公共脚本执行失败: {e}"
            logger.error("[{}] {}", case.case_id, result.error)
        except Exception as e:  # 请求异常 / 提取异常等
            result.ok = False
            result.error = f"用例执行异常: {type(e).__name__}: {e}"
            logger.error("[{}] {}", case.case_id, result.error)
        finally:
            # 5. 后置脚本（清理/回滚）：主流程失败也要跑
            if case.post_scripts:
                self._run_post_scripts(case, result)
        return result

    def _run_post_scripts(self, case: TestCase, result: CaseResult) -> None:
        try:
            scripts.run_scripts(case.post_scripts, self.ctx, self.executor, "后置", result.script_trace)
        except scripts.ScriptError as e:
            result.ok = False
            result.error = (result.error + "\n" if result.error else "") + f"后置脚本执行失败（数据可能未清理）: {e}"
            logger.error("[{}] {}", case.case_id, result.error)

    @staticmethod
    def _failure_message(case: TestCase, result: CaseResult, failed: list) -> str:
        """组装断言失败详情：失败断言明细 + 请求/响应报文摘要。"""
        lines = [f"用例 {case.case_id} [{case.api_name}] 断言失败 {len(failed)}/{len(result.assertions)}:"]
        for i, a in enumerate(failed, 1):
            expected_text = "" if a.op == "not_null" else f", 期望值={_fmt(a.expected)}"
            detail = f"（{a.message}）" if a.message else ""
            lines.append(f"  [{i}] {a.op} {a.path}: 实际值={_fmt(a.actual)}{expected_text}{detail}")

        req, resp = result.request_snapshot, result.response_snapshot
        if req:
            lines.append(f"请求: {req['method']} {req['url']}")
            if req.get("params"):
                lines.append(f"  查询参数: {_fmt(req['params'])}")
            if req.get("body") is not None:
                lines.append(f"  请求体: {_fmt(req['body'])}")
        if resp:
            body = resp.get("body", "")
            body = body if len(body) <= 800 else body[:800] + "..."
            lines.append(f"响应: {resp['status_code']} {body}")
        return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


from core.engines.base import Engine  # noqa: E402


class ApiEngine(Engine):
    """接口自动化引擎的注册表入口（身份 + 分流 + 统一校验/执行入口）。

    validate/execute 均为薄封装，分别委托 excel_loader 行解析与 CaseRunner，行为与直调一致。
    """

    key = "api"
    label = "接口自动化"
    test_module = str(Path("tests") / "test_api.py")

    def matches(self, path: Path) -> bool:
        # 与旧 main.py 分流口径一致：非 .json 一律按接口用例走 test_api 链路；
        # 再排除 App 专用载体 .apjson（由 AppEngine 认领），避免注册表按序遍历时误收。
        return path.suffix.lower() not in (".json", ".apjson")

    def validate(self, raw, **deps):
        """校验并归一化一条接口用例（Excel「用例」表一行的 {字段名: 原值} dict）→ TestCase；非法抛 ExcelFormatError。"""
        return excel_loader.validate_case(raw, **deps)

    def execute(self, case: TestCase, ctx, *, executor: HttpExecutor, **deps) -> CaseResult:
        """执行一条接口用例，委托 CaseRunner（与旧直调 .run() 完全一致）→ CaseResult。"""
        return CaseRunner(executor, ctx).run(case)
