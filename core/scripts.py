"""公共脚本（前后置处理器，设计文档 2.4）：声明式步骤链的定义、校验与执行。

刻意只支持四种步骤动作，全部复用框架既有能力，不引入任何动态代码执行：
  request   发一次请求（可带变量提取与断言），复用 HttpExecutor / extract_variables / run_assertions
  sleep     等待若干秒（上限受 SLEEP_LIMIT 约束）
  set_var   写入变量池（值支持 {{变量}} 占位符，保留原始类型）
  assert    断言，规则语法与 Excel「断言」列完全一致（[操作符, 取值路径, 期望值]）

脚本定义存 data/scripts.json（平台「公共脚本」页维护），用例通过 Excel
「前置脚本」「后置脚本」两列按名引用（逗号分隔）。
"""
import json
import threading
import time
from pathlib import Path

from loguru import logger

from .asserts import run_assertions
from .excel_loader import (ASSERT_OPS, OPS_NEED_EXPECTED, VALID_METHODS, join_refs,
                           split_refs)
from .extractor import extract_variables
from .variable import render

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPTS_FILE = "data/scripts.json"

# 各动作的可用字段：既是校验白名单（拼错/多写的键直接拒绝），也驱动前端步骤编辑器的表单。
# 两者同一数据源，绝不会出现「页面能填、执行不认」的口径漂移。
ACTION_FIELDS: dict[str, set[str]] = {
    "request": {"method", "url", "headers", "params", "body", "extract", "assert", "timeout"},
    "sleep": {"seconds"},
    "set_var": {"vars"},
    "assert": {"assert"},
}
ACTION_DESC = {
    "request": "发一次请求（可提取变量、可带断言）",
    "sleep": "等待若干秒（用于异步任务落库）",
    "set_var": "写入变量池（值支持 {{变量}} 占位符）",
    "assert": "断言上一条响应或变量池（var.xxx）",
}
# 顺序即前端展示顺序
ACTIONS = tuple(ACTION_FIELDS)
STEP_LIMIT = 20          # 单脚本步骤上限，防误写超长链
SLEEP_LIMIT = 30.0       # 单步等待秒上限
NAME_INVALID = set("，, \t\r\n\"'/\\[]{}")


class ScriptError(Exception):
    """脚本定义非法，或执行期失败（脚本不存在 / 步骤断言不通过 / 变量缺失）。"""


_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def scripts_file() -> Path:
    """脚本文件路径：config.yaml 的 scripts_file 优先，默认 data/scripts.json。"""
    name = ""
    try:
        from .context import load_config
        name = str(load_config().get("scripts_file") or "").strip()
    except Exception:  # 配置缺失/损坏不应让脚本能力整体不可用
        name = ""
    return ROOT / (name or DEFAULT_SCRIPTS_FILE)


def load_scripts(path: Path | None = None) -> dict:
    """读取脚本库，返回 {脚本名: 定义}；按 mtime 缓存，文件不存在返回空库。"""
    global _cache
    path = path or scripts_file()
    with _lock:
        if not path.is_file():
            _cache = None
            return {}
        mtime = path.stat().st_mtime
        if _cache and _cache[0] == mtime:
            return _cache[1]
        try:
            with open(path, encoding="utf-8") as fp:
                raw = json.load(fp)
        except (json.JSONDecodeError, OSError) as e:
            raise ScriptError(f"脚本文件 {path.name} 解析失败: {e}")
        items = raw.get("scripts") if isinstance(raw, dict) else raw
        data: dict[str, dict] = {}
        for item in items or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            data[str(item["name"])] = item
        _cache = (mtime, data)
        return data


def validate_step(step, where: str) -> dict:
    """校验并归一化单个步骤，返回干净定义（未知键与非法值一律拒绝）。"""
    if not isinstance(step, dict):
        raise ScriptError(f"{where} 步骤必须是 JSON 对象")
    action = str(step.get("action") or "").strip()
    if action not in ACTIONS:
        raise ScriptError(f"{where} 步骤动作 {action!r} 不支持，可用: {'/'.join(ACTIONS)}")

    allowed = {"action", "desc"} | ACTION_FIELDS[action]
    unknown = sorted(set(step) - allowed)
    if unknown:
        raise ScriptError(f"{where} {action} 步骤含无用字段: {', '.join(unknown)}（可用: {', '.join(sorted(allowed))}）")

    out: dict = {"action": action}
    desc = str(step.get("desc") or "").strip()
    if desc:
        out["desc"] = desc

    if action == "request":
        method = str(step.get("method") or "").strip().upper()
        if method not in VALID_METHODS:
            raise ScriptError(f"{where} 请求方式必须是 {'/'.join(sorted(VALID_METHODS))}，实际为 {method!r}")
        url = str(step.get("url") or "").strip()
        if not url:
            raise ScriptError(f"{where} request 步骤的 url（请求路径）不能为空")
        out["method"] = method
        out["url"] = url
        for key in ("headers", "params", "extract"):
            value = step.get(key)
            if value in (None, ""):
                continue
            if not isinstance(value, dict):
                raise ScriptError(f"{where} {key} 必须是 JSON 对象 {{...}}")
            out[key] = value
        if step.get("body") not in (None, ""):
            out["body"] = step["body"]
        if step.get("timeout") not in (None, ""):
            try:
                t = float(step["timeout"])
            except (TypeError, ValueError):
                raise ScriptError(f"{where} timeout 必须是正数（秒）")
            if t <= 0:
                raise ScriptError(f"{where} timeout 必须大于 0")
            out["timeout"] = t
        out["assert"] = _validate_rules(step.get("assert"), f"{where} request 断言", allow_empty=True)
    elif action == "sleep":
        try:
            seconds = float(step.get("seconds"))
        except (TypeError, ValueError):
            raise ScriptError(f"{where} sleep 需要数值型 seconds（秒）")
        if not 0 <= seconds <= SLEEP_LIMIT:
            raise ScriptError(f"{where} sleep 秒数必须在 0~{SLEEP_LIMIT:g} 之间，实际为 {seconds}")
        out["seconds"] = seconds
    elif action == "set_var":
        mapping = step.get("vars")
        if not isinstance(mapping, dict) or not mapping:
            raise ScriptError(where + ' set_var 需要非空 vars 对象，如 {"token": "abc-123"}')
        for key in mapping:
            if not str(key).strip():
                raise ScriptError(f"{where} set_var 变量名不能为空")
        out["vars"] = mapping
    else:  # assert
        out["assert"] = _validate_rules(step.get("assert"), f"{where} 断言")
    return out


def _validate_rules(rules, where: str, allow_empty: bool = False) -> list:
    """断言规则校验，语法与 Excel「断言」列一致：[操作符, 取值路径, 期望值]。"""
    if rules in (None, ""):
        if allow_empty:
            return []
        raise ScriptError(f"{where} 断言不能为空")
    if not isinstance(rules, list) or not rules:
        raise ScriptError(f"{where} 断言必须是非空数组，如 [[\"eq\",\"status_code\",200]]")
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, (list, tuple)) or not 2 <= len(rule) <= 3:
            raise ScriptError(f"{where} 第{i}条格式错误，应为 [操作符, 取值路径, 期望值]")
        op = str(rule[0]).strip().lower()
        if op not in ASSERT_OPS:
            raise ScriptError(f"{where} 第{i}条操作符 {rule[0]!r} 不支持，可用: {', '.join(sorted(ASSERT_OPS))}")
        if len(rule) < 3 and op in OPS_NEED_EXPECTED:
            raise ScriptError(f"{where} 第{i}条操作符 {op} 需要期望值")
        if not str(rule[1] or "").strip():
            raise ScriptError(f"{where} 第{i}条取值路径不能为空")
    return [list(r) for r in rules]


def validate_script(name: str, remark: str, steps: list) -> dict:
    """校验整条脚本，返回可直接落盘的定义。"""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ScriptError("脚本名不能为空")
    if len(clean_name) > 50:
        raise ScriptError("脚本名不能超过 50 字符")
    bad = sorted({ch for ch in clean_name if ch in NAME_INVALID})
    if bad:
        raise ScriptError(f"脚本名不能包含 {', '.join(bad)}（用例 Excel 以逗号分隔引用脚本名）")
    if not isinstance(steps, list) or not steps:
        raise ScriptError(f"脚本 {clean_name} 至少需要一个步骤")
    if len(steps) > STEP_LIMIT:
        raise ScriptError(f"脚本 {clean_name} 步骤数超过上限 {STEP_LIMIT}")
    cleaned = [validate_step(s, f"脚本 {clean_name} 第{i}步") for i, s in enumerate(steps, 1)]
    return {"name": clean_name, "remark": str(remark or "").strip(), "steps": cleaned}


def resolve(names: list, library: dict | None = None) -> list:
    """按引用名取脚本定义，不存在的名字直接报错（引用保护之外的最后一道）。"""
    library = load_scripts() if library is None else library
    picked: list = []
    for raw in names or []:
        name = str(raw or "").strip()
        if not name:
            continue
        if name not in library:
            raise ScriptError(f"公共脚本不存在: {name}（请先在「公共脚本」页创建，或修正用例的脚本引用列）")
        picked.append(library[name])
    return picked


def run_scripts(names: list, ctx, executor, phase: str, trace: list, library: dict | None = None) -> None:
    """按序执行脚本；任一步骤失败即抛 ScriptError，已执行步骤留在 trace 里供报告还原现场。

    phase 仅用于日志与追溯（"前置" / "后置"），不改变语义。
    """
    for script in resolve(names, library):
        last_response = None
        for i, step in enumerate(script["steps"], 1):
            where = f"[{phase}脚本 {script['name']} 第{i}步 {step['action']}]"
            try:
                last_response = _run_step(step, ctx, executor, last_response)
            except ScriptError as e:
                trace.append({"script": script["name"], "phase": phase, "index": i,
                              "action": step["action"], "desc": step.get("desc", ""),
                              "ok": False, "detail": str(e)})
                raise ScriptError(f"{where} 失败: {e}")
            except Exception as e:  # 请求异常、变量缺失、JSONPath 非法等
                trace.append({"script": script["name"], "phase": phase, "index": i,
                              "action": step["action"], "desc": step.get("desc", ""),
                              "ok": False, "detail": f"{type(e).__name__}: {e}"})
                raise ScriptError(f"{where} 异常: {type(e).__name__}: {e}")
            detail = _step_summary(step, ctx)
            trace.append({"script": script["name"], "phase": phase, "index": i,
                          "action": step["action"], "desc": step.get("desc", ""),
                          "ok": True, "detail": detail})
            logger.info("前置/后置脚本 {} -> {} OK", where, detail)


def _step_summary(step: dict, ctx) -> str:
    action = step["action"]
    if action == "request":
        return f"{step['method']} {step['url']}" + (f" | 提取 {', '.join(step['extract'])}" if step.get("extract") else "")
    if action == "set_var":
        # 报告与日志里只给变量名与短预览，token/password 类值不落盘
        return ", ".join(f"{k}={_mask_value(k, ctx.get(k))}" for k in step["vars"])
    if action == "sleep":
        return f"等待 {step['seconds']:g}s"
    return f"{len(step.get('assert') or [])} 条断言通过"


SENSITIVE_HINTS = ("token", "password", "passwd", "secret", "key", "auth", "cookie")


def _mask_value(name: str, value) -> str:
    text = _fmt(value)
    if any(h in str(name).lower() for h in SENSITIVE_HINTS):
        return "***" if len(text) > 3 else "**"
    return text


def _run_step(step: dict, ctx, executor, last_response):
    action = step["action"]
    if action == "sleep":
        time.sleep(step["seconds"])
        return last_response

    if action == "set_var":
        resolved = render(step["vars"], ctx)
        for key, value in resolved.items():
            ctx.set(str(key), value)
        return last_response

    if action == "request":
        response = executor.execute(
            step["method"],
            render(step["url"], ctx),
            headers=render(step["headers"], ctx) if step.get("headers") else None,
            params=render(step["params"], ctx) if step.get("params") else None,
            body=render(step["body"], ctx) if step.get("body") is not None else None,
            timeout=step.get("timeout"),
        )
        if step.get("extract"):
            for key, value in extract_variables(response, step["extract"]).items():
                ctx.set(key, value)
        rules = step.get("assert") or []
        if rules:
            _check(run_assertions(response, rules, ctx))
        return response

    # assert：路径决定取值来源（var.xxx 取变量池，status_code/$. 取上一条响应）
    _check(run_assertions(last_response, step.get("assert") or [], ctx))
    return last_response


def _check(results: list) -> None:
    failed = [a for a in results if not a.passed]
    if failed:
        raise ScriptError("; ".join(
            f"{a.op} {a.path}: 实际值={_fmt(a.actual)} 期望={_fmt(a.expected)}" + (f"（{a.message}）" if a.message else "")
            for a in failed))


def _fmt(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:120]
    text = str(value)
    return text if len(text) <= 120 else text[:120] + "..."


# 脚本引用的单元格互转（Excel 「前置脚本 / 后置脚本」列与脚本名列表之间）
# 直接复用 excel_loader 的实现，避免两处拆分口径不一致
__all__ = [
    "ScriptError", "ACTIONS", "scripts_file", "load_scripts", "validate_script",
    "validate_step", "resolve", "run_scripts", "split_refs", "join_refs",
]
