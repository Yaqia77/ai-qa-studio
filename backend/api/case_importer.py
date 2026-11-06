# -*- coding: utf-8 -*-
"""智能导入：把格式不匹配的用例 Excel（人工评审表/其他平台导出）转换成平台标准用例。

设计口径：
- 只做「解析 + 转换 + 标注」，不写文件 —— 转换结果先给前端预览确认，
  确认后才走 /cases/import/confirm 落盘，避免猜测结果静默入库；
- 三级表头识别：标准表头 → 归一化（去括号注释/星号/空白）→ 同义词表；
- 缺项自动补默认值（优先级 P2、是否执行 Y、断言按「预期结果」语义映射），
  每一处猜测/兜底都记 mark（warn=需确认，error=不可导入），预览里高亮；
- 断言优先取「断言」列，其次按 URL 命中接口定义库带出推荐断言，最后才用语义映射兜底。
"""
import json
import re

from openpyxl import load_workbook

from backend.api import api_def_store
from core import excel_loader, scripts as core_scripts

# 归一化后表头 → 平台字段（标准表头本身在 CASE_COLUMNS 里精确命中，不走这里）
SYNONYMS: dict[str, str] = {
    "用例编号": "case_id", "用例id": "case_id", "用例ID": "case_id", "编号": "case_id", "用例编码": "case_id", "tc编号": "case_id",
    "模块": "module", "所属模块": "module", "功能模块": "module", "一级模块": "module", "二级模块": "module2",
    "功能点": "module2", "需求模块": "module",
    "接口名称": "api_name", "接口名": "api_name",
    "用例描述": "case_desc", "用例名称": "case_desc", "用例标题": "case_desc", "标题": "case_desc",
    "优先级": "priority", "用例等级": "priority",
    "是否执行": "run", "是否启用": "run", "启用状态": "run",
    "请求方式": "method", "请求方法": "method", "方法": "method",
    "请求路径": "url", "接口路径": "url", "请求url": "url", "接口地址": "url", "请求地址": "url", "url": "url",
    "请求头": "headers", "headers": "headers",
    "查询参数": "params", "params": "params",
    "请求体": "body", "body": "body",
    "变量提取": "extract",
    "断言": "asserts",
    "超时": "timeout",
    "前置脚本": "pre_scripts", "后置脚本": "post_scripts",
    # 非标准字段：参与转换但不直接映射
    "预置条件": "precondition", "前置条件": "precondition",
    "操作步骤": "steps", "步骤": "steps", "操作": "steps",
    "预期结果": "expected", "期望结果": "expected", "预期": "expected",
    # 明确丢弃的人工字段
    "实际结果": "_discard", "执行结果": "_discard", "备注": "_discard", "执行人": "_discard",
}

# 通用预置条件（无需转成前置脚本/描述）
_GENERIC_PRE = {"", "无", "服务正常", "正常", "接口可用", "环境正常", "y", "-"}
_NEG_WORDS = re.compile(r"报错|错误|失败|异常|鉴权|未授权|无权限|非法|不传|超时|拒绝")
_STATUS_NUM = re.compile(r"(?<!\d)([45]\d{2})(?!\d)")
_METHOD_URL = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(https?://\S+|/\S*)", re.I)
_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")
_PATH_LIKE = re.compile(r"^/[A-Za-z0-9_\-/{}.]")


def _norm_header(cell) -> str:
    """表头归一化：去空白、去尾部星号、去括号注释。如「一级模块（需求）*」→「一级模块」。"""
    text = str(cell or "").strip().rstrip("*＊").strip()
    text = re.sub(r"[（(][^（）()]*[）)]", "", text).strip()
    return text


def _cell_text(v) -> str:
    if v is None:
        return ""
    return v.strip() if isinstance(v, str) else str(v)


def _match_def(url: str) -> dict | None:
    """按路径尾段匹配接口定义库（用例 URL 可能带域名/定义可能带占位符）。"""
    if not url:
        return None
    path = re.sub(r"^https?://[^/]+", "", url).strip().rstrip("/")
    if not path:
        return None
    for d in api_def_store.list_defs():
        dp = re.sub(r"\{\{[^}]*\}\}", "*", str(d.get("path") or "")).strip().rstrip("/")
        if not dp:
            continue
        if path == dp or path.endswith(dp) or dp.endswith(path):
            return d
        # 占位符段按通配逐段比较（/posts/*/comments vs /posts/123/comments）
        ps, ds = path.split("/"), dp.split("*")
        if len(ps) >= len(ds):
            ok, i = True, 0
            for j, seg in enumerate(ds):
                if seg and (i >= len(ps) or ps[i] != seg):
                    ok = False
                    break
                i += 1
            if ok and dp and not dp.startswith("*"):
                return d
    return None


def _asserts_from_expected(expected: str, hit_def: dict | None) -> tuple[list, str]:
    """「预期结果」自然语言 → 断言规则。返回 (断言, 说明)；说明为空表示无需提示。"""
    if hit_def and hit_def.get("asserts"):
        return [list(r) for r in hit_def["asserts"]], f"断言取自接口定义库「{hit_def['name']}」"
    text = expected.strip()
    m = _STATUS_NUM.search(text)
    if m:
        return [["eq", "status_code", int(m.group(1))]], f"按预期结果中的状态码 {m.group(1)} 生成"
    if _NEG_WORDS.search(text):
        return ([["neq", "status_code", 200]],
                "负向用例默认按「非200」判定；若该接口以 200+业务错误码 表达失败，请改为 neq $..code 0")
    return [["eq", "status_code", 200]], "默认断言：HTTP 200，请按需补充业务码断言"


def _parse_json_cell(text: str):
    """宽松解析 JSON 单元格：标准 JSON → 单引号/无引号键（JS 风格）兜底。返回 (值, 错误)。"""
    if not text:
        return None, ""
    try:
        return json.loads(text), ""
    except json.JSONDecodeError:
        pass
    try:
        fixed = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', text)
        fixed = re.sub(r"'", '"', fixed)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        return json.loads(fixed), ""
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败（{e.msg}），已置空待人工补充"


class _RowCtx:
    """单行的转换中间态。"""

    def __init__(self, row_no: int):
        self.row_no = row_no
        self.marks: list[dict] = []
        self.case: dict = {}

    def mark(self, level: str, field: str, msg: str) -> None:
        self.marks.append({"level": level, "field": field, "msg": msg})


def parse_workbook(path, existing_ids: set[str]) -> dict:
    """解析任意表头的用例 Excel，返回预览结构。

    {format: standard|mapped, column_map: {原表头: {field, label}}, items: [{row, case, marks, importable, exists}]}
    """
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = excel_loader.CASE_SHEET if excel_loader.CASE_SHEET in wb.sheetnames else wb.sheetnames[0]
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        raise ValueError(f"工作表「{sheet}」为空")

    # ---- 表头映射 ----
    headers: list[tuple[int, str, str]] = []  # (列号, 原表头, 字段)
    unmapped: list[str] = []
    for idx, cell in enumerate(rows[0]):
        raw_h = _cell_text(cell)
        if not raw_h:
            continue
        h = _norm_header(raw_h)
        field = excel_loader.CASE_COLUMNS.get(h) or SYNONYMS.get(h) or SYNONYMS.get(h.lower())
        if field is None:
            field = "url" if h in ("接口",) else None
        if field is None or field == "_discard":
            if field is None:
                unmapped.append(raw_h)
            continue
        # 同名标准字段重复出现（如已有「模块」又有「一级/二级模块」）：后来的降级记录
        if any(f == field for _, _, f in headers):
            unmapped.append(raw_h)
            continue
        headers.append((idx, raw_h, field))
    fields = {f for _, _, f in headers}
    standard = {"case_id", "module", "api_name", "case_desc", "priority", "run", "method", "url", "asserts"} <= fields
    col_of = {f: i for i, _, f in headers}

    seen_ids: set[str] = set()
    script_names = set(core_scripts.load_scripts())
    items: list[dict] = []

    for r_no, row in enumerate(rows[1:], start=2):
        def val(field: str) -> str:
            i = col_of.get(field)
            return _cell_text(row[i]) if i is not None and i < len(row) else ""

        raw = {f: val(f) for _, _, f in headers}
        if not any(raw.values()):
            continue  # 空行
        # 模板自带的灰字示例行不重复导入
        if raw.get("case_id") == "login_001" and raw.get("module", "").startswith("示例"):
            continue

        ctx = _RowCtx(r_no)
        c = ctx.case
        text = val("steps") + " " + val("url") + " " + val("module2")

        # ---- 编号 ----
        cid = val("case_id")
        if not cid:
            cid = f"imp_{r_no:03d}"
            ctx.mark("warn", "case_id", f"用例编号缺失，自动生成 {cid}（可改）")
        if cid in seen_ids:
            ctx.mark("error", "case_id", f"编号在本文件内重复（{cid}），本行不会导入")
        seen_ids.add(cid)
        c["case_id"] = cid

        # ---- 模块 / 接口名 / 描述 ----
        mod = val("module") or val("module2")
        if _PATH_LIKE.match(mod or ""):
            mod = val("module") or "未分类"
        c["module"] = (mod or "未分类")[:40]
        api = val("api_name")

        # ---- URL（列 → 二级模块路径 → 操作步骤正则）----
        url = val("url")
        m_step = _METHOD_URL.search(val("steps") or "")
        if not url and _PATH_LIKE.match(val("module2") or ""):
            url = val("module2")
            ctx.mark("warn", "url", "从「二级模块（功能点）」列识别出接口路径作为请求路径")
        if not url and m_step:
            url = m_step.group(2)
        if not url:
            m = re.search(r"(https?://[^\s，。;；]+|/[A-Za-z0-9_\-/]+\.[A-Za-z0-9_\-/]+|/ai/[^\s，。;；]+)", text)
            url = m.group(1) if m else ""
        c["url"] = url
        if not url:
            ctx.mark("error", "url", "无法从任何列识别请求路径，本行不可导入，请手工补全")

        c["api_name"] = api or (re.sub(r"^https?://[^/]+", "", url).strip("/").split("/")[-1] if url else "") or "导入接口"
        desc = val("case_desc") or "导入用例"
        pre = val("precondition")
        if pre and pre.strip().lower() not in _GENERIC_PRE:
            if pre in script_names:
                c["pre_scripts"] = [pre]
                ctx.mark("warn", "pre_scripts", f"预置条件「{pre}」命中同名公共脚本，已设为前置脚本，请确认")
            else:
                desc = f"{desc}（预置：{pre}）"
        c["case_desc"] = desc[:200]

        # ---- 优先级 / 是否执行 ----
        pri = val("priority").strip().upper()
        if re.fullmatch(r"P?[0-5]", pri):
            c["priority"] = pri if pri.startswith("P") else f"P{pri}"
        else:
            c["priority"] = "P2"
            if val("priority"):
                ctx.mark("warn", "priority", f"优先级「{val('priority')}」不识别，默认 P2")
        runv = val("run").strip().upper()
        if runv in excel_loader.RUN_TRUE or runv == "1":
            c["run"] = True
        elif runv in excel_loader.RUN_FALSE or runv == "0":
            c["run"] = False
        else:
            c["run"] = True

        # ---- 方法（列 → 操作步骤 → 接口定义库 → 语义猜测）----
        hit_m = _match_def(url)
        if not c.get("method"):
            mv = val("method").strip().upper()
            if mv in excel_loader.VALID_METHODS:
                c["method"] = mv
            elif m_step:
                c["method"] = m_step.group(1).upper()
                ctx.mark("info", "method", f"从「操作步骤」识别出请求方式 {c['method']}")
            elif hit_m:
                c["method"] = str(hit_m["method"]).upper()
                ctx.mark("info", "method", f"请求方式取自接口定义库「{hit_m['name']}」")
            else:
                c["method"] = "POST" if val("body") or (val("steps") and "不传" not in val("steps")) else "GET"
                ctx.mark("warn", "method", f"请求方式缺失，按{'有请求体' if val('body') else '语义'}猜测为 {c['method']}，请确认")

        # ---- JSON 列 ----
        for field in ("headers", "params", "extract"):
            v, err = _parse_json_cell(val(field))
            if err:
                ctx.mark("warn", field, f"{field} 列{err}")
                v = None
            if v is not None:
                c[field] = v
        body_text = val("body")
        if not body_text and val("steps"):
            m = _JSON_BLOCK.search(val("steps"))
            if m and not val("body"):
                body_text = m.group(0)
                ctx.mark("warn", "body", "从「操作步骤」提取到 JSON 请求体，请核对")
        if body_text:
            if body_text.strip()[:1] in ("{", "["):
                v, err = _parse_json_cell(body_text)
                if err:
                    ctx.mark("warn", "body", f"请求体{err}")
                else:
                    c["body"] = v
            else:
                c["body"] = body_text
        if "body" not in c and c["method"] in ("POST", "PUT", "PATCH"):
            if hit_m and hit_m.get("body"):
                c["body"] = hit_m["body"]
                ctx.mark("warn", "body", f"请求体缺失，已预填接口定义库「{hit_m['name']}」的示例体，请核对参数后调整断言")

        # ---- 断言 ----
        asserts_src = val("asserts")
        if asserts_src:
            rules, err = _parse_json_cell(asserts_src)
            msgs = excel_loader.check_assert_rules(rules or []) if not err else [err]
            if msgs:
                ctx.mark("warn", "asserts", f"断言列不合法（{msgs[0]}），改用预期结果兜底")
                asserts_src = ""
            else:
                c["asserts"] = [list(r) for r in rules]
        if not asserts_src:
            hit = _match_def(url)
            rules, why = _asserts_from_expected(val("expected"), hit)
            c["asserts"] = rules
            level = "warn" if (not hit or "请" in why or "默认" in why or "非200" in why) else "info"
            ctx.mark(level, "asserts", f"断言由「预期结果：{val('expected') or '空'}」自动生成 —— {why}")

        # ---- 超时 / 脚本列 ----
        tv = val("timeout")
        if tv:
            try:
                c["timeout"] = float(tv)
            except ValueError:
                ctx.mark("warn", "timeout", f"超时「{tv}」不是数字，使用环境默认")
        for field in ("pre_scripts", "post_scripts"):
            names = excel_loader.split_refs(val(field))
            if names:
                missing = [n for n in names if n not in script_names]
                if missing:
                    ctx.mark("warn", field, f"引用的公共脚本不存在: {', '.join(missing)}，导入后请修正")
                c[field] = names

        items.append({
            "row": r_no,
            "case": c,
            "marks": ctx.marks,
            "importable": not any(m["level"] == "error" for m in ctx.marks),
            "exists": cid.lower() in existing_ids,
        })

    return {
        "sheet": sheet,
        "format": "standard" if standard else "mapped",
        "column_map": [{"header": h, "field": f} for _, h, f in headers],
        "unmapped_headers": unmapped,
        "items": items,
    }
