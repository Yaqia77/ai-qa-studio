# -*- coding: utf-8 -*-
"""UI 用例 Excel 一步一行模板：生成下载模板 + 解析上传文件为预览结构。

设计口径（对齐接口用例 backend.api.case_importer 的「解析+转换+标注、不写文件」范式）：
- UI 用例是「一条用例多步骤」的嵌套结构，用「一步一行 + 用例编号分组」平铺进 Excel：
  同编号的连续行属于同一用例，用例级字段（名称/模块/优先级…）只在首行填、后续行留空自动继承；
- 动作列接受中文别名（打开/点击/输入…），经 ui_case_store.normalize_action 归一后走同一校验；
- 解析阶段只做转换与逐条标注（mark），不落盘；前端预览确认后调 /import 才真正写入；
- 合法性最终以后端 validate_case 为唯一事实来源，本模块只负责「把 Excel 变成 validate_case 能吃的 dict」。

core 无关、纯 backend 侧录入辅助；不依赖执行引擎。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.ui import ui_case_store
from backend.ui.ui_case_store import UiCaseError

# ---- Sheet1「UI步骤」列：表头 → 字段 ----
STEP_SHEET = "UI步骤"
STEP_COLUMNS: dict[str, str] = {
    "用例编号": "case_id", "用例名称": "name", "模块": "module", "优先级": "priority",
    "浏览器": "browser", "无头": "headless", "是否执行": "run", "重试": "retry",
    "步骤号": "step_no", "动作": "action", "定位器": "locator", "取值": "value", "备注": "desc",
}
STEP_HEADERS = list(STEP_COLUMNS.keys())

# ---- Sheet2「变量提取」列 ----
EXTRACT_SHEET = "变量提取"
EXTRACT_COLUMNS: dict[str, str] = {"用例编号": "case_id", "变量名": "var", "来源": "source"}
EXTRACT_HEADERS = list(EXTRACT_COLUMNS.keys())

# 示例行编号前缀：解析时整段跳过，避免模板样例被当真实用例导入
_EXAMPLE_PREFIX = "示例"

_CASE_ID_RE = re.compile(r"[\r\n\t]")


# ==================== 模板生成 ====================

def build_template_workbook() -> Workbook:
    """生成双 Sheet 模板（UI步骤 + 变量提取 + 填写说明），含灰字示例行。"""
    wb = Workbook()
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_font = Font(bold=True, color="FFFFFF")
    example_font = Font(color="999999", italic=True)
    wrap = Alignment(vertical="center", wrap_text=True)

    # ---- Sheet1: UI步骤 ----
    ws = wb.active
    ws.title = STEP_SHEET
    for col, header in enumerate(STEP_HEADERS, start=1):
        c = ws.cell(row=1, column=col, value=header)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")

    # 两条示例用例（一步一行；用例级字段仅首行填）。编号以「示例」开头，导入时自动跳过。
    example_rows = [
        ["示例_login_001", "用户登录（示例可删）", "登录", "P1", "chromium", "是", "Y", "0",
         "1", "打开", "", "/login", "打开登录页"],
        ["", "", "", "", "", "", "", "", "2", "输入", "#username", "qa", ""],
        ["", "", "", "", "", "", "", "", "3", "输入", "#password", "123456", ""],
        ["", "", "", "", "", "", "", "", "4", "点击", "#submit", "", ""],
        ["", "", "", "", "", "", "", "", "5", "断言文本", ".welcome", "欢迎", "校验登录成功"],
        ["示例_search_002", "搜索并提取（示例可删）", "搜索", "P2", "chromium", "是", "Y", "2",
         "1", "打开", "", "/search", ""],
        ["", "", "", "", "", "", "", "", "2", "输入", "css=input[name=q]", "{{keyword}}", "引用变量"],
        ["", "", "", "", "", "", "", "", "3", "点击", "css=button", "", ""],
        ["", "", "", "", "", "", "", "", "4", "等待出现", ".result", "", ""],
    ]
    for r_no, row in enumerate(example_rows, start=2):
        for col, value in enumerate(row, start=1):
            c = ws.cell(row=r_no, column=col, value=value)
            c.font = example_font
            c.alignment = wrap

    widths = [18, 22, 12, 8, 12, 8, 9, 6, 7, 12, 24, 20, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ---- Sheet2: 变量提取 ----
    ws2 = wb.create_sheet(EXTRACT_SHEET)
    for col, header in enumerate(EXTRACT_HEADERS, start=1):
        c = ws2.cell(row=1, column=col, value=header)
        c.fill = header_fill
        c.font = header_font
    extract_rows = [
        ["示例_search_002", "first_result", "text|.result"],
    ]
    for r_no, row in enumerate(extract_rows, start=2):
        for col, value in enumerate(row, start=1):
            ws2.cell(row=r_no, column=col, value=value).font = example_font
    ws2.column_dimensions["A"].width = 20
    ws2.column_dimensions["B"].width = 18
    ws2.column_dimensions["C"].width = 32
    ws2.freeze_panes = "A2"

    # ---- Sheet3: 填写说明 ----
    ws3 = wb.create_sheet("填写说明")
    section_font = Font(bold=True, size=12)
    guide = [
        ("一、总体规则", [
            "1. 「UI步骤」表一步一行：同一用例的多步骤共用一个「用例编号」，只在首行填编号与用例级字段（名称/模块/优先级/浏览器/无头/是否执行/重试），后续行这些列留空即自动继承。",
            "2. 编号留空表示延续上一行所属用例；换了新编号即开启一条新用例。",
            "3. 「示例_」开头的灰字行是样例，导入时自动跳过，可参照着填写或整行删除。",
        ]),
        ("二、动作列（支持中文）", [
            "打开/访问=goto  点击=click  输入/填写=fill  按键/回车=press  选择/下拉=select  勾选=check",
            "悬停=hover  等待/等待出现=wait_for  截图=screenshot  断言可见=expect_visible  断言文本=expect_text  断言地址=expect_url",
            "也可直接填英文动作键（goto/click/...），效果相同。",
        ]),
        ("三、定位器列", [
            "裸写默认按 CSS 解析（如 #username、.btn、button.submit）。",
            "XPath 加前缀 xpath=（如 xpath=//div[@class='x']）；按文本 text=；按角色 role=button,name=登录。",
            "goto 不需要定位器，取值填相对路径（自动拼环境 base_url）或完整 URL。",
        ]),
        ("四、取值与变量", [
            "取值列支持 {{变量名}} 引用（环境/凭证/本条 extract 提取的变量）。",
            "断言文本/断言地址的取值即「期望值」；输入的取值即「要填入的文本」。",
        ]),
        ("五、变量提取（可选 Sheet2）", [
            "在「变量提取」表按 用例编号 关联：变量名 + 来源。",
            "来源语法：text|<定位器> / input_value|<定位器> / attr|<定位器>:<属性> / cookie:<名> / url。",
        ]),
        ("六、导入规则", [
            "逐条全量校验：编号与已有用例重复会跳过，字段非法会逐条报因且不阻断其他条。",
            "单次导入上限 64 条用例。",
        ]),
    ]
    row = 1
    for title, lines in guide:
        ws3.cell(row=row, column=1, value=title).font = section_font
        row += 1
        for line in lines:
            ws3.cell(row=row, column=1, value=line)
            row += 1
        row += 1
    ws3.column_dimensions["A"].width = 120

    return wb


def build_template_bytes() -> bytes:
    """模板工作簿 → xlsx 字节（供 HTTP 下载）。"""
    buf = io.BytesIO()
    build_template_workbook().save(buf)
    return buf.getvalue()


# ==================== 导出（与模板同构，可回导） ====================

def build_export_workbook(cases: list[dict]) -> Workbook:
    """把项目用例导出为「一步一行」Excel：与模板列结构同构，编辑后可直接回导（同编号按重复跳过）。

    用例级字段只写首行（与填写口径一致）；无头/是否执行用 是/否（导入侧 normalize_bool 归一）；
    用例级 desc 与 session 等列模板未承载，导出不丢数据的路径走 JSON 导出。
    """
    wb = Workbook()
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_font = Font(bold=True, color="FFFFFF")
    wrap = Alignment(vertical="center", wrap_text=True)

    ws = wb.active
    ws.title = STEP_SHEET
    for col, header in enumerate(STEP_HEADERS, start=1):
        c = ws.cell(row=1, column=col, value=header)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")

    row_no = 2
    for case in cases:
        steps = case.get("steps") or [{}]  # 兜底：无步骤用例至少保留 meta 行（正常不会发生，validate 强制≥1 步）
        for i, s in enumerate(steps, start=1):
            first = i == 1
            row = [
                case.get("case_id", "") if first else "",
                case.get("name", "") if first else "",
                case.get("module", "") if first else "",
                case.get("priority", "") if first else "",
                case.get("browser", "") if first else "",
                ("是" if case.get("headless", True) else "否") if first else "",
                ("是" if case.get("run", True) else "否") if first else "",
                case.get("retry", 0) if first else "",
                str(i),
                s.get("action", ""),
                s.get("locator", ""),
                s.get("value", ""),
                s.get("desc", ""),
            ]
            for col, value in enumerate(row, start=1):
                ws.cell(row=row_no, column=col, value=value).alignment = wrap
            row_no += 1

    widths = [18, 22, 12, 8, 12, 8, 9, 6, 7, 12, 24, 20, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet(EXTRACT_SHEET)
    for col, header in enumerate(EXTRACT_HEADERS, start=1):
        c = ws2.cell(row=1, column=col, value=header)
        c.fill = header_fill
        c.font = header_font
    for case in cases:
        for e in case.get("extract") or []:
            ws2.append([case.get("case_id", ""), e.get("var", ""), e.get("source", "")])
    ws2.column_dimensions["A"].width = 20
    ws2.column_dimensions["B"].width = 18
    ws2.column_dimensions["C"].width = 32
    ws2.freeze_panes = "A2"
    return wb


def build_export_bytes(cases: list[dict]) -> bytes:
    """导出工作簿 → xlsx 字节（供 HTTP 下载）。"""
    buf = io.BytesIO()
    build_export_workbook(cases).save(buf)
    return buf.getvalue()


# ==================== 上传解析 ====================

def _cell_text(v) -> str:
    if v is None:
        return ""
    return v.strip() if isinstance(v, str) else str(v)


def _norm_header(cell) -> str:
    """表头归一：去空白、去尾部星号、去括号注释。"""
    text = str(cell or "").strip().rstrip("*＊").strip()
    text = re.sub(r"[（(][^（）()]*[）)]", "", text).strip()
    return text


def _map_headers(header_row) -> tuple[list[tuple[int, str, str]], list[str]]:
    """把表头行解析为 [(列号, 原表头, 字段)]，并返回未识别表头列表。"""
    headers: list[tuple[int, str, str]] = []
    unmapped: list[str] = []
    seen: set[str] = set()
    for idx, cell in enumerate(header_row):
        raw_h = _cell_text(cell)
        if not raw_h:
            continue
        field = STEP_COLUMNS.get(_norm_header(raw_h))
        if field is None or field in seen:
            unmapped.append(raw_h)
            continue
        seen.add(field)
        headers.append((idx, raw_h, field))
    return headers, unmapped


class _CaseDraft:
    """一条用例的聚合中间态。"""

    def __init__(self, case_id: str, row_no: int):
        self.case_id = case_id
        self.row_no = row_no
        self.meta: dict = {}
        self.steps: list[dict] = []
        self.marks: list[dict] = []

    def mark(self, level: str, field: str, msg: str) -> None:
        self.marks.append({"level": level, "field": field, "msg": msg})


def parse_workbook(path: Path, existing_ids: set[str] | None = None) -> dict:
    """解析上传的 UI 步骤 Excel，返回预览结构（不写文件）。

    {items:[{case_id, name, steps_count, case, marks, importable, exists}],
     unmapped_headers:[...], total:N}
    case 为已过 validate_case 的清洗结果（importable=True 时）；否则为原始 dict 供预览。
    """
    existing_ids = {str(x).lower() for x in (existing_ids or set())}
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        if STEP_SHEET in wb.sheetnames:
            ws = wb[STEP_SHEET]
        else:
            ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        extracts = _read_extract_sheet(wb)
    finally:
        wb.close()

    if not rows:
        return {"items": [], "unmapped_headers": [], "total": 0, "error": "「UI步骤」表为空"}

    headers, unmapped = _map_headers(rows[0])
    col_of = {f: i for i, _, f in headers}

    def val(row, field: str) -> str:
        i = col_of.get(field)
        return _cell_text(row[i]) if i is not None and i < len(row) else ""

    drafts: list[_CaseDraft] = []
    current: _CaseDraft | None = None
    skipping_example = False  # 处于示例分组内：其后续空编号行一并跳过

    for r_no, row in enumerate(rows[1:], start=2):
        cells = {f: val(row, f) for _, _, f in headers}
        if not any(cells.values()):
            continue  # 空行

        cid = cells.get("case_id", "").strip()
        action_raw = cells.get("action", "").strip()

        # 示例行：编号以「示例」开头即进示例分组；分组内所有行（含空编号延续行）都跳过
        if cid.startswith(_EXAMPLE_PREFIX):
            skipping_example = True
            current = None
            continue
        if cid:
            skipping_example = False
        elif skipping_example:
            continue

        # 新用例：编号非空且与当前不同 → 起新 draft；编号为空 → 延续当前
        if cid and (current is None or cid != current.case_id):
            current = _CaseDraft(cid, r_no)
            drafts.append(current)
            _apply_meta(current, cells)
        elif not cid and current is None:
            current = _CaseDraft(f"imp_{r_no:03d}", r_no)
            current.mark("warn", "case_id", f"首行缺用例编号，自动生成 {current.case_id}")
            drafts.append(current)
            _apply_meta(current, cells)
        # 编号为空且有 current：延续，不重复取 meta（首行已填）

        if current is None:
            continue

        if not action_raw:
            # 只有 meta、无动作的行：若已是某用例首行 meta 则忽略；提示空动作
            continue

        norm = ui_case_store.normalize_action(action_raw)
        if norm not in ui_case_store.VALID_ACTIONS:
            current.mark("error", "action", f"第 {len(current.steps) + 1} 步动作「{action_raw}」无法识别，本条不可导入")
        step = {"action": norm or action_raw}
        loc = cells.get("locator", "").strip()
        if loc:
            step["locator"] = loc
        v = cells.get("value", "")
        if v.strip():
            step["value"] = v
        d = cells.get("desc", "").strip()
        if d:
            step["desc"] = d
        current.steps.append(step)

    # 组装 + 校验 + 关联变量提取
    items: list[dict] = []
    for dr in drafts:
        raw_case = {**dr.meta, "case_id": dr.case_id, "steps": dr.steps}
        ex = extracts.get(dr.case_id.lower())
        if ex:
            raw_case["extract"] = ex
        _finalize(dr, raw_case, existing_ids)
        items.append({
            "row": dr.row_no,
            "case_id": dr.case_id,
            "name": raw_case.get("name", ""),
            "steps_count": len(dr.steps),
            "case": dr.case,
            "marks": dr.marks,
            "importable": dr.importable,
            "exists": dr.case_id.lower() in existing_ids,
        })

    return {"items": items, "unmapped_headers": unmapped, "total": len(items)}


def _apply_meta(draft: _CaseDraft, cells: dict) -> None:
    """从首行提取用例级字段（名称/模块/优先级/浏览器/无头/是否执行/重试）。"""
    if cells.get("name"):
        draft.meta["name"] = cells["name"]
    if cells.get("module"):
        draft.meta["module"] = cells["module"]
    if cells.get("priority"):
        draft.meta["priority"] = cells["priority"]
    if cells.get("browser"):
        draft.meta["browser"] = cells["browser"]
    if cells.get("headless"):
        draft.meta["headless"] = ui_case_store.normalize_bool(cells["headless"], True)
    if cells.get("run"):
        draft.meta["run"] = ui_case_store.normalize_bool(cells["run"], True)
    if cells.get("retry"):
        draft.meta["retry"] = cells["retry"]


def _finalize(draft: _CaseDraft, raw_case: dict, existing_ids: set[str]) -> None:
    """跑 validate_case：通过则挂清洗结果，失败按类型标注 mark。"""
    if not raw_case.get("steps"):
        draft.mark("error", "steps", "该用例没有任何有效步骤，不可导入")
        draft.case = raw_case
        draft.importable = False
        return
    try:
        cleaned = ui_case_store.validate_case(raw_case)
        draft.case = cleaned
        draft.importable = not any(m["level"] == "error" for m in draft.marks)
    except UiCaseError as e:
        draft.mark("error", "case", str(e))
        draft.case = raw_case
        draft.importable = False


def _read_extract_sheet(wb) -> dict[str, list[dict]]:
    """读「变量提取」表（可选）：{case_id 小写: [{var, source}]}。缺失返回空。"""
    out: dict[str, list[dict]] = {}
    if EXTRACT_SHEET not in wb.sheetnames:
        return out
    ws = wb[EXTRACT_SHEET]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return out
    col_of: dict[str, int] = {}
    for idx, cell in enumerate(rows[0]):
        f = EXTRACT_COLUMNS.get(_norm_header(cell))
        if f and f not in col_of.values():
            col_of[f] = idx

    def val(row, field: str) -> str:
        i = col_of.get(field)
        return _cell_text(row[i]) if i is not None and i < len(row) else ""

    for row in rows[1:]:
        cid = val(row, "case_id").strip()
        var = val(row, "var").strip()
        src = val(row, "source").strip()
        if not cid or cid.startswith(_EXAMPLE_PREFIX) or not var or not src:
            continue
        out.setdefault(cid.lower(), []).append({"var": var, "source": src})
    return out
