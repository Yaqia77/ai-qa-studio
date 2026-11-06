"""allure-results 目录解析 + 敏感信息掩码（平台原生报告页数据源）。

安全约定：任何请求/响应/错误文本离开后端前必须过 mask_*，
绝不把真实 Authorization、长数字 ID、对象存储签名透给前端。
"""
import json
import re
from pathlib import Path

# ---------- 掩码 ----------

# Authorization 头的值（JSON/文本两种形态）
_AUTH_RE = re.compile(r"((?:Authorization|X-Auth|Cookie)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9+/=_.,-]{14,})", re.I)
# 对象存储预签名参数
_SIG_RE = re.compile(r"(X-Amz-(?:Signature|Credential)=)[^&\"'\s]+", re.I)
# 15 位以上连续数字（userId/手机号类标识）
_LONG_DIGITS_RE = re.compile(r"\d{15,}")
_SENSITIVE_KEYS = {"authorization", "cookie", "x-auth", "password", "token"}


def _short(value: str) -> str:
    if len(value) <= 12:
        return value
    return value[:8] + "****" + value[-4:] if len(value) <= 40 else value[:8] + f"****（{len(value)} 位已隐藏）"


def mask_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = _AUTH_RE.sub(lambda m: m.group(1) + _short(m.group(2)), text)
    text = _SIG_RE.sub(lambda m: m.group(1) + "****", text)
    text = _LONG_DIGITS_RE.sub(lambda m: m.group(0)[:4] + "****" + m.group(0)[-2:], text)
    return text


def mask_obj(obj):
    """递归掩码 dict/list：敏感键整体短掩码，字符串值走文本规则。"""
    if isinstance(obj, dict):
        return {
            k: (_short(str(v)) if k.lower() in _SENSITIVE_KEYS else mask_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_obj(x) for x in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


# ---------- allure-results 解析 ----------

_PRIORITY_RE = re.compile(r"\[(P\d)\]")
_CASE_ID_RE = re.compile(r"case_id='([^']+)'")
_BODY_LIMIT = 4000  # 响应体截断长度，报告页排障够用，避免吐超大对象


def _load_results(results_dir: Path) -> list[dict]:
    items = []
    for f in sorted(results_dir.glob("*-result.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                items.append(json.load(fp))
        except (json.JSONDecodeError, OSError):
            continue
    items.sort(key=lambda x: x.get("start") or 0)
    return items


def _find_attachment(result: dict, keyword: str) -> str | None:
    for a in result.get("attachments", []):
        if keyword in (a.get("name") or ""):
            return a.get("source")
    return None


def _load_json_attachment(results_dir: Path, result: dict, keyword: str):
    """读取指定关键字附件的 JSON（缺文件/解析失败返回 None）。"""
    src = _find_attachment(result, keyword)
    if not src or not (results_dir / src).exists():
        return None
    try:
        with open(results_dir / src, encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return None


def _case_id(result: dict) -> str:
    for p in result.get("parameters", []):
        m = _CASE_ID_RE.search(str(p.get("value", "")))
        if m:
            return m.group(1)
    m = re.search(r"\]\s+(\S+)", result.get("name", ""))
    return m.group(1) if m else result.get("name", "?")


def infer_status(results_dir: Path) -> str:
    """从 allure-results 推断批次结论（给无内存记录的 cli 历史批次用）。"""
    statuses = []
    for f in results_dir.glob("*-result.json"):
        try:
            with open(f, encoding="utf-8") as fp:
                statuses.append(json.load(fp).get("status"))
        except (json.JSONDecodeError, OSError):
            continue
    if not statuses:
        return "unknown"
    bad = {s for s in statuses if s not in ("passed", "skipped")}
    return "failed" if bad else "passed"


def list_cases(results_dir: Path) -> dict:
    """批次用例结果摘要 + 统计。"""
    summary = []
    stats = {"passed": 0, "failed": 0, "broken": 0, "skipped": 0}
    for r in _load_results(results_dir):
        status = r.get("status", "unknown")
        if status in stats:
            stats[status] += 1
        name = r.get("name", "")
        prio = (_PRIORITY_RE.search(name) or [None, ""])[1] if _PRIORITY_RE.search(name) else ""
        asserts = _load_json_attachment(results_dir, r, "断言")
        assert_total = assert_failed = None
        if isinstance(asserts, list) and asserts:
            assert_total = len(asserts)
            assert_failed = sum(1 for a in asserts if not a.get("passed"))
        # flaky 观测：仅真发生过重试的批次才有重试轨迹附件，其余缺省 attempts=1（零额外 IO）
        retry = _ui_retry_info(results_dir, r)
        attempts = int(retry.get("attempts") or 1) if retry else 1
        retried = attempts > 1
        flaky = bool(retried and retry.get("final_ok") and status == "passed")
        summary.append({
            "case_id": _case_id(r),
            "name": name,
            "priority": prio,
            "status": status,
            "duration_ms": (r.get("stop") or 0) - (r.get("start") or 0),
            "error": mask_text((r.get("statusDetails") or {}).get("message", ""))[:1200],
            "assert_total": assert_total,
            "assert_failed": assert_failed,
            "attempts": attempts,
            "retried": retried,
            "flaky": flaky,
            "summary": _result_summary(status, asserts) + _ui_retry_annotation(retry),
        })
    return {"total": len(summary), "stats": stats, "items": summary}


def _result_summary(status: str, asserts) -> str:
    """结果摘要（已掩码）：有断言明细时给“断言 X/Y + 首条失败详情”，否则回退到状态。"""
    if isinstance(asserts, list) and asserts:
        total = len(asserts)
        failed_list = [a for a in asserts if not a.get("passed")]
        if not failed_list:
            return f"全部断言通过（{total}/{total}）"
        a = failed_list[0]
        op = str(a.get("op", ""))
        path = str(a.get("path", ""))
        actual = mask_obj(a.get("actual"))
        detail = f"断言 {total - len(failed_list)}/{total}：{op} {path} 实际 {actual}"
        if op != "not_null":
            detail += f" 期望 {mask_obj(a.get('expected'))}"
        if a.get("message"):
            detail += f"（{mask_text(str(a.get('message')))}）"
        return detail[:200]
    if status == "passed":
        return "通过"
    if status == "skipped":
        return "已跳过"
    if status == "broken":
        return "执行异常"
    return status


def case_run_summary(results_dir: Path, case_id: str) -> dict | None:
    """单批次内指定用例的结果摘要（供跨批次历史聚合）；未命中返回 None。

    仅在命中该 case_id 时才读取断言附件，避免逐附件的无谓 IO。
    """
    for r in _load_results(results_dir):
        if _case_id(r) != case_id:
            continue
        status = r.get("status", "unknown")
        asserts = _load_json_attachment(results_dir, r, "断言")
        return {
            "status": status,
            "duration_ms": (r.get("stop") or 0) - (r.get("start") or 0),
            "summary": _result_summary(status, asserts),
            "error": mask_text((r.get("statusDetails") or {}).get("message", ""))[:600],
        }
    return None


def case_detail(results_dir: Path, case_id: str) -> dict | None:
    """单用例明细：请求/响应快照（均已掩码+截断）+ 失败信息。"""
    for r in _load_results(results_dir):
        if _case_id(r) != case_id:
            continue
        detail = {
            "case_id": case_id,
            "name": r.get("name", ""),
            "status": r.get("status", "unknown"),
            "description": mask_text(r.get("description", "")),
            "error": mask_text((r.get("statusDetails") or {}).get("message", "")),
            "trace": mask_text((r.get("statusDetails") or {}).get("trace", ""))[:2000],
            "request": None,
            "response": None,
            "scripts": [],
        }
        src = _find_attachment(r, "请求")
        if src and (results_dir / src).exists():
            with open(results_dir / src, encoding="utf-8") as fp:
                detail["request"] = mask_obj(json.load(fp))
        src = _find_attachment(r, "响应")
        if src and (results_dir / src).exists():
            with open(results_dir / src, encoding="utf-8") as fp:
                resp = json.load(fp)
            if isinstance(resp.get("body"), str) and len(resp["body"]) > _BODY_LIMIT:
                resp["body"] = resp["body"][:_BODY_LIMIT] + "…（已截断，完整内容见 Allure 报告）"
            detail["response"] = mask_obj(resp)
        asserts = _load_json_attachment(results_dir, r, "断言")
        detail["assertions"] = mask_obj(asserts) if isinstance(asserts, list) else None
        # 公共脚本执行轨迹（前置/后置逐步结果）：未引用脚本的用例该字段为空列表
        trace = _load_json_attachment(results_dir, r, "脚本轨迹")
        detail["scripts"] = mask_obj(trace) if isinstance(trace, list) else []
        # UI 步骤轨迹：接口用例无此附件返回 None；截图附件名解析为 allure source 文件名供前端取图
        detail["ui_steps"] = _ui_steps(results_dir, r)
        # UI 重试轨迹：未发生重试的批次返回 None，存量报告形状不变
        retry_trace = _load_json_attachment(results_dir, r, "UI重试轨迹")
        detail["ui_retry"] = mask_obj(retry_trace) if isinstance(retry_trace, dict) else None
        # 失败 trace：用例最终失败留下的 trace zip 附件源文件名（供报告页下载回放）；未开启/非 UI 返回 None
        detail["trace_file"] = _find_attachment(r, "失败trace")
        return detail
    return None


def _collect_attachment_map(node: dict, out: dict) -> None:
    """递归收集 allure 附件 name→source：步骤执行中 attach 的附件（如手动截图步）落在
    steps[].attachments 而非顶层，只查顶层会漏映截图源。"""
    for a in node.get("attachments") or []:
        if a.get("name") and a.get("source"):
            out.setdefault(a["name"], a["source"])
    for st in node.get("steps") or []:
        if isinstance(st, dict):
            _collect_attachment_map(st, out)


def _ui_steps(results_dir: Path, result: dict):
    """从 allure-results 读取「UI步骤」附件并回填每步截图的可访问文件名。"""
    steps = _load_json_attachment(results_dir, result, "UI步骤")
    if not isinstance(steps, list):
        return None
    att_map: dict = {}
    _collect_attachment_map(result, att_map)
    for s in steps:
        if isinstance(s, dict) and s.get("screenshot"):
            s["screenshot_source"] = att_map.get(s["screenshot"])
    return mask_obj(steps)


def _ui_retry_info(results_dir: Path, result: dict) -> dict | None:
    """读取「UI重试轨迹」附件；不存在返回 None（未重试批次零额外 IO）。"""
    src = _find_attachment(result, "UI重试轨迹")
    if not src:
        return None
    trace = _load_json_attachment(results_dir, result, "UI重试轨迹")
    return trace if isinstance(trace, dict) else None


def _ui_retry_annotation(trace: dict | None) -> str:
    """发生过重试时给摘要追加标注（让 flaky 可见而非被吞）；无重试附件返回空串。"""
    if not trace:
        return ""
    attempts = int(trace.get("attempts") or 1)
    if attempts <= 1:
        return ""
    tail = "后通过" if trace.get("final_ok") else "仍未通过"
    return f"〔重试 {attempts - 1} 次{tail}〕"
