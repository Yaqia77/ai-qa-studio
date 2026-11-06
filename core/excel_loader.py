"""Excel 用例文件解析与格式校验（报错精确定位到文件、Sheet、行、列）。"""
import json
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .models import TestCase

CASE_SHEET = "用例"
VAR_SHEET = "全局变量"

# 「用例」表表头 → 字段名
CASE_COLUMNS = {
    "用例编号": "case_id",
    "模块": "module",
    "接口名称": "api_name",
    "用例描述": "case_desc",
    "优先级": "priority",
    "是否执行": "run",
    "请求方式": "method",
    "请求路径": "url",
    "请求头": "headers",
    "查询参数": "params",
    "请求体": "body",
    "变量提取": "extract",
    "断言": "asserts",
    "超时(秒)": "timeout",
    "前置脚本": "pre_scripts",
    "后置脚本": "post_scripts",
}
REQUIRED_HEADERS = ["用例编号", "模块", "接口名称", "用例描述", "优先级", "是否执行", "请求方式", "请求路径", "断言"]
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3", "P4", "P5"}
ASSERT_OPS = {"eq", "neq", "contains", "gt", "ge", "lt", "le", "regex", "not_null", "len_eq"}
OPS_NEED_EXPECTED = ASSERT_OPS - {"not_null"}
# 操作符中文说明（平台 meta 接口下发给前端做汉化显示，与执行引擎解耦）
ASSERT_OP_LABELS = {
    "eq": "等于", "neq": "不等于", "contains": "包含",
    "gt": "大于", "ge": "大于等于", "lt": "小于", "le": "小于等于",
    "regex": "正则匹配", "not_null": "不为空", "len_eq": "长度等于",
}
RUN_TRUE = {"Y", "YES", "TRUE", "是"}
RUN_FALSE = {"N", "NO", "FALSE", "否"}


class ExcelFormatError(Exception):
    """Excel 内容/格式不合法，message 中包含全部错误的精确位置。"""


def split_refs(cell) -> list:
    """脚本引用单元格 → 脚本名列表（中英文逗号分隔，去空白去重保序）。"""
    if cell in (None, ""):
        return []
    text = cell if isinstance(cell, str) else str(cell)
    out: list[str] = []
    for part in text.replace("，", ",").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def join_refs(names) -> str | None:
    """脚本名列表 → 单元格文本（写回 Excel 用）。"""
    cleaned = split_refs(",".join(str(n) for n in (names or []) if str(n).strip()))
    return ",".join(cleaned) or None


class LoadResult:
    """一次加载的结果：用例列表 + 全局变量。"""

    def __init__(self, cases: list, variables: dict):
        self.cases = cases
        self.variables = variables


def load(path, allow_empty: bool = False) -> LoadResult:
    """加载用例文件；path 可为单个 .xlsx 文件或目录（目录下所有 .xlsx 合并，按文件名排序）。

    目录扫描会排除 secret_vars 开头的凭证文件（由 load_secret_vars 按配置单独加载）。
    allow_empty=True：表头合法但零条用例时返回空结果而不报错（平台读取空项目文件用）；
    执行链路（conftest/main）保持默认 False，零用例仍视为异常。
    """
    p = Path(path)
    if p.is_dir():
        files = sorted(p.glob("*.xlsx"))
        files = [f for f in files
                 if not f.name.startswith("~$")        # Excel 打开时的临时文件
                 and not f.name.startswith("secret_vars")]  # 凭证文件走独立加载
        if not files:
            raise FileNotFoundError(f"目录 {p} 下没有 .xlsx 用例文件")
    else:
        if not p.exists():
            raise FileNotFoundError(f"用例文件不存在: {p}")
        files = [p]

    cases: list[TestCase] = []
    variables: dict = {}
    for f in files:
        _load_one(f, cases, variables)
    if not cases and not allow_empty:
        raise ExcelFormatError(f"未从 {len(files)} 个文件中解析到任何用例")
    return LoadResult(cases, variables)


def load_secret_vars(path) -> dict:
    """加载凭证变量文件（只需「全局变量」表，无「用例」表）。

    文件不存在时返回空 dict（凭证文件是可选的）。
    """
    p = Path(path)
    if not p.exists():
        return {}
    wb = load_workbook(p, data_only=True, read_only=True)
    try:
        if VAR_SHEET not in wb.sheetnames:
            raise ExcelFormatError(f"[{p.name}] 凭证文件缺少工作表「{VAR_SHEET}」")
        variables: dict = {}
        errors: list[str] = []
        _load_variables(p.name, wb[VAR_SHEET], variables, errors)
        if errors:
            raise ExcelFormatError(_join(errors))
        return variables
    finally:
        wb.close()


def _load_one(path: Path, cases: list, variables: dict) -> None:
    errors: list[str] = []
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        if CASE_SHEET not in wb.sheetnames:
            errors.append(f"[{path.name}] 缺少工作表「{CASE_SHEET}」")
            raise ExcelFormatError(_join(errors))
        ws = wb[CASE_SHEET]

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ExcelFormatError(_join([f"[{path.name} → {CASE_SHEET}] 表格为空"]))

        # 表头定位（列顺序不限）
        header_to_field = {}
        field_to_col = {}  # 字段 -> 0 基列号
        for idx, cell in enumerate(rows[0]):
            if cell is None:
                continue
            header = str(cell).strip()
            if header in CASE_COLUMNS:
                if header in header_to_field:
                    errors.append(f"[{path.name} → {CASE_SHEET}] 表头「{header}」重复")
                header_to_field[header] = CASE_COLUMNS[header]
                field_to_col[CASE_COLUMNS[header]] = idx
        missing = [h for h in REQUIRED_HEADERS if h not in header_to_field]
        if missing:
            errors.append(f"[{path.name} → {CASE_SHEET}] 缺少必填表头: {', '.join(missing)}")
            raise ExcelFormatError(_join(errors))

        # 逐行解析用例
        seen_ids: dict[str, str] = {}
        for row_no, row in enumerate(rows[1:], start=2):
            raw = {}
            for field, col in field_to_col.items():
                raw[field] = row[col] if col < len(row) else None
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in raw.values()):
                continue  # 整行为空，跳过
            case = _parse_case(path.name, row_no, raw, field_to_col, seen_ids, errors)
            if case is not None:
                cases.append(case)

        # 全局变量表（可选）
        if VAR_SHEET in wb.sheetnames:
            _load_variables(path.name, wb[VAR_SHEET], variables, errors)
    finally:
        wb.close()

    if errors:
        raise ExcelFormatError(_join(errors))


def _parse_case(file_name, row_no, raw, field_to_col, seen_ids, errors):
    def cell_text(field):
        v = raw.get(field)
        if v is None:
            return ""
        return v.strip() if isinstance(v, str) else v

    def err(field, msg):
        col = field_to_col.get(field)
        header = next((h for h, f in CASE_COLUMNS.items() if f == field), field)
        pos = f"第{row_no}行 {get_column_letter(col + 1)}列({header})" if col is not None else f"第{row_no}行"
        errors.append(f"[{file_name} → {CASE_SHEET} {pos}] {msg}")

    case_id = str(cell_text("case_id") or "").strip()
    if not case_id:
        err("case_id", "用例编号不能为空")
        return None
    if case_id in seen_ids:
        err("case_id", f"用例编号 {case_id} 重复（首次出现: {seen_ids[case_id]}）")
        return None
    seen_ids[case_id] = f"第{row_no}行"

    for field in ("module", "api_name", "case_desc", "url"):
        if not str(cell_text(field) or "").strip():
            err(field, f"{dict((v, k) for k, v in CASE_COLUMNS.items())[field]}不能为空")
            return None

    priority = str(cell_text("priority") or "").strip().upper()
    if priority not in VALID_PRIORITIES:
        err("priority", f"优先级必须是 {'/'.join(sorted(VALID_PRIORITIES))}，实际为 {priority!r}")
        return None

    run_raw = str(cell_text("run") or "").strip().upper()
    if run_raw in RUN_TRUE:
        run = True
    elif run_raw in RUN_FALSE:
        run = False
    else:
        err("run", f"是否执行必须是 Y/N，实际为 {run_raw!r}")
        return None

    method = str(cell_text("method") or "").strip().upper()
    if method not in VALID_METHODS:
        err("method", f"请求方式必须是 {'/'.join(sorted(VALID_METHODS))}，实际为 {method!r}")
        return None

    headers = _parse_json_object(cell_text("headers"), "headers", err)
    params = _parse_json_object(cell_text("params"), "params", err)
    extract = _parse_json_object(cell_text("extract"), "extract", err)
    body = _parse_body(cell_text("body"), err)
    asserts = _parse_asserts(cell_text("asserts"), err)

    timeout_raw = cell_text("timeout")
    timeout = None
    if timeout_raw not in (None, ""):
        try:
            timeout = float(timeout_raw)
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            err("timeout", f"超时必须是正数（秒），实际为 {timeout_raw!r}")
            return None

    ok_so_far = headers is not False and params is not False and extract is not False and asserts is not False and body is not False
    if not ok_so_far:
        return None  # 该行已有 JSON 相关错误，跳过

    # 前置/后置脚本：表头为可选列，缺失时为 None（存量文件零影响）；单元格为逗号分隔的脚本名
    pre_scripts = split_refs(raw.get("pre_scripts"))
    post_scripts = split_refs(raw.get("post_scripts"))

    return TestCase(
        row=row_no,
        source=file_name,
        case_id=case_id,
        module=str(raw["module"]).strip(),
        api_name=str(raw["api_name"]).strip(),
        case_desc=str(raw["case_desc"]).strip(),
        priority=priority,
        run=run,
        method=method,
        url=str(raw["url"]).strip(),
        headers=headers,
        params=params,
        body=body,
        extract=extract,
        asserts=asserts,
        timeout=timeout,
        pre_scripts=pre_scripts,
        post_scripts=post_scripts,
    )


def validate_case(row: dict, *, file_name: str = "inline", row_no: int = 0) -> TestCase:
    """校验并归一化单条接口用例：入参为 Excel「用例」表一行的 {字段名: 原值} dict。

    复用 load() 逐行解析的同一套规则（_parse_case），任一字段非法即抛 ExcelFormatError；
    供引擎统一校验入口（core.engines.api.ApiEngine.validate）调用。纯附加，不改变既有 load() 行为。
    """
    raw = dict(row)
    field_to_col = {f: i for i, f in enumerate(raw)}
    errors: list[str] = []
    case = _parse_case(file_name, row_no, raw, field_to_col, {}, errors)
    if errors:
        raise ExcelFormatError(_join(errors))
    if case is None:
        raise ExcelFormatError(_join([f"用例校验未通过（{file_name} 第{row_no}行）"]))
    return case


def _parse_json_object(value, field, err):
    """解析必须为 JSON 对象的列，返回 dict / None；格式错误时记录 err 并返回 False 哨兵。"""
    if value in (None, ""):
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as e:
        err(field, f"JSON 解析失败: {e}")
        return False
    if not isinstance(parsed, dict):
        err(field, f"必须是 JSON 对象 {{...}}，实际是 {type(parsed).__name__}")
        return False
    return parsed


def _parse_body(value, err):
    """请求体：以 { 或 [ 开头时必须为合法 JSON；否则按原始文本处理。

    已是 dict/list 的值（Agent 内联校验直接传结构化对象）原样接受，
    绝不对容器做 str()——否则 Python repr 的单引号会让随后的 json.loads 失败。
    """
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value)
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            err("body", f"JSON 解析失败: {e}")
            return False
    return stripped  # 非 JSON 文本，按原始字符串请求体处理


def check_assert_rules(rules) -> list:
    """断言规则结构校验，返回错误说明列表（空列表即全部合法）。

    公开给平台侧复用（接口定义库的「断言建议」终要写进用例的断言列），
    保证库里推荐的断言与 Excel 读取口径完全一致，不会出现「库里存得下、用例读不了」。
    """
    errors: list[str] = []
    for i, rule in enumerate(rules or [], 1):
        if not isinstance(rule, (list, tuple)) or not 2 <= len(rule) <= 3:
            errors.append(f"第{i}条格式错误，应为 [操作符, 取值路径, 期望值]，如 [\"eq\",\"$.code\",0]")
            continue
        op = str(rule[0]).strip().lower()
        if op not in ASSERT_OPS:
            errors.append(f"第{i}条操作符 {rule[0]!r} 不支持，可用: {', '.join(sorted(ASSERT_OPS))}")
            continue
        if len(rule) < 3 and op in OPS_NEED_EXPECTED:
            errors.append(f"第{i}条操作符 {op} 需要期望值")
            continue
        if not str(rule[1] or "").strip():
            errors.append(f"第{i}条取值路径不能为空")
    return errors


def _parse_asserts(value, err):
    """断言列：JSON 数组，元素为 [操作符, 取值路径, 期望值]。"""
    if value in (None, ""):
        err("asserts", "断言不能为空")
        return False
    try:
        rules = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as e:
        err("asserts", f"JSON 解析失败: {e}")
        return False
    if not isinstance(rules, list) or not rules:
        err("asserts", "必须是 JSON 数组且至少一条，如 [[\"eq\",\"status_code\",200]]")
        return False
    msgs = check_assert_rules(rules)
    if msgs:
        err("asserts", msgs[0])
        return False
    return rules


def _load_variables(file_name, ws, variables: dict, errors: list) -> None:
    """解析「全局变量」表：变量名 / 值 / 备注。"""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return
    header_idx = {}
    for idx, cell in enumerate(rows[0]):
        if cell is not None and str(cell).strip() in ("变量名", "值"):
            header_idx[str(cell).strip()] = idx
    if "变量名" not in header_idx or "值" not in header_idx:
        errors.append(f"[{file_name} → {VAR_SHEET} 表头] 需包含「变量名」和「值」两列")
        return
    for row_no, row in enumerate(rows[1:], start=2):
        name = row[header_idx["变量名"]] if header_idx["变量名"] < len(row) else None
        value = row[header_idx["值"]] if header_idx["值"] < len(row) else None
        name = str(name).strip() if name is not None else ""
        if not name and value in (None, ""):
            continue
        if not name:
            errors.append(f"[{file_name} → {VAR_SHEET} 第{row_no}行] 变量名为空")
            continue
        if name in variables:
            errors.append(f"[{file_name} → {VAR_SHEET} 第{row_no}行] 变量 {name} 重复定义")
            continue
        if value is None:
            value = ""
        variables[name] = value if isinstance(value, str) else value


def _join(errors: list) -> str:
    return "Excel 用例文件校验失败，共 " + str(len(errors)) + " 处错误:\n" + "\n".join(errors)
