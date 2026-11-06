# -*- coding: utf-8 -*-
"""用例空白模板生成（平台「导出模板」下载用）。

与 tools/gen_template.py 的区别：那份是仓库初始化的演示用例（含 9 条样例），
本模块只给「一张干净的空表 + 下拉校验 + 一行示例 + 填写说明」，
让用户下载后直接往里填，从源头减少格式错误（优先级/请求方式/是否执行都做成下拉）。
"""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from core.excel_loader import (
    CASE_COLUMNS,
    VALID_METHODS,
    VALID_PRIORITIES,
)

# 表头顺序即列顺序（与 CASE_COLUMNS 定义一致）
_HEADERS = list(CASE_COLUMNS.keys())

# 一行示例（灰字，用户可整行删除）：覆盖 GET/POST、变量提取、断言、脚本引用的典型写法
_EXAMPLE_ROW = [
    "login_001", "示例-认证", "登录", "示例行，可整行删除：登录并提取 token", "P0", "Y", "POST",
    "/api/login",
    '{"Content-Type":"application/json"}', "",
    '{"username":"{{username}}","password":"{{password}}"}',
    '{"token":"$.data.token"}',
    '[["eq","status_code",200],["not_null","$.data.token"]]',
    "", "", "",
]

# 必填列表头（与 excel_loader.REQUIRED_HEADERS 对齐，表头批注提示用）
_REQUIRED = {"用例编号", "模块", "接口名称", "用例描述", "优先级", "是否执行", "请求方式", "请求路径", "断言"}

_HEADER_NOTE = {
    "优先级": "下拉选 P0~P5",
    "是否执行": "下拉选 Y/N；N 的用例不会被执行",
    "请求方式": "下拉选 GET/POST/PUT/DELETE/PATCH",
    "请求头": "JSON 对象，如 {\"Authorization\":\"Bearer {{token}}\"}",
    "查询参数": "JSON 对象，URL 查询参数",
    "请求体": "JSON 对象/数组，或纯文本",
    "变量提取": "JSON 对象 {\"变量名\":\"$.json.path\"}，供后续用例引用",
    "断言": "JSON 数组 [[\"eq\",\"status_code\",200]]，多条全过才通过",
    "前置脚本": "逗号分隔的公共脚本名，在主请求前执行",
    "后置脚本": "逗号分隔的公共脚本名，收尾清理用",
}

_GUIDE = [
    ["填写说明", ""],
    ["1", "从第 3 行开始填写用例（第 2 行是灰字示例，可整行删除）。"],
    ["2", "红色表头的列为必填：用例编号 / 模块 / 接口名称 / 用例描述 / 优先级 / 是否执行 / 请求方式 / 请求路径 / 断言。"],
    ["3", "优先级、是否执行、请求方式三列已做成下拉，直接选择即可，避免手输错格式。"],
    ["4", "请求路径：http(s):// 开头视为完整 URL；以 / 开头会拼接环境 base_url。均支持 {{变量}} 占位符。"],
    ["5", "请求头/查询参数/请求体/变量提取/断言 必须填合法 JSON；断言不能留空。"],
    ["6", "变量机制：{{变量名}} 可用在 路径/头/参数/体/断言期望值。取值优先级 运行时提取 > 凭证 > 全局变量表 > 环境配置。"],
    ["7", "用例按行号顺序执行，后面的用例可用前面用例「变量提取」出的变量（先登录提 token，再鉴权）。"],
    ["8", "需要前置依赖（如必须先创建再查询）：导入后在「用例管理」行内设置依赖，执行时会自动纳入并前置依赖用例。"],
    ["9", "填好后点「智能导入」上传本文件：格式表头即使与模板不同名（如「用例名称」「预期结果」）也会尽量自动识别转换。"],
]


def build_template_bytes() -> BytesIO:
    """生成空白用例模板，返回定位到 0 的 BytesIO（可直接喂 StreamingResponse）。"""
    wb = Workbook()

    # ---- Sheet1: 用例 ----
    ws = wb.active
    ws.title = "用例"
    header_fill = PatternFill("solid", fgColor="4472C4")
    wrap = Alignment(vertical="center", wrap_text=True)

    for col, header in enumerate(_HEADERS, start=1):
        # 表头文本必须与 CASE_COLUMNS 完全一致（下载填写后可直接被 loader 识别），
        # 必填列改用红字标注，不往表头里加 * 号。
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = Font(bold=True, color="FFC7CE" if header in _REQUIRED else "FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        note = _HEADER_NOTE.get(header)
        if note:
            cell.comment = Comment(note, "interface-auto")

    # 第 2 行示例（灰字），第 3 行起留给用户
    example_font = Font(color="A6A6A6", italic=True)
    for col, value in enumerate(_EXAMPLE_ROW, start=1):
        cell = ws.cell(row=2, column=col, value=value)
        cell.font = example_font
        cell.alignment = wrap

    widths = [16, 14, 14, 34, 10, 10, 12, 40, 30, 18, 40, 26, 46, 10, 20, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A3"

    # 下拉校验：定位到各列的数据区（第 2 行起到 500 行）
    def _dv(header: str, options: list[str]) -> None:
        idx = _HEADERS.index(header) + 1
        letter = get_column_letter(idx)
        dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
        dv.error = "请从下拉列表中选择"
        dv.errorTitle = "取值不合法"
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}500")

    _dv("优先级", sorted(VALID_PRIORITIES))
    _dv("是否执行", ["Y", "N"])
    _dv("请求方式", sorted(VALID_METHODS))

    # ---- Sheet2: 全局变量 ----
    ws2 = wb.create_sheet("全局变量")
    white_bold = Font(bold=True, color="FFFFFF")
    for col, header in enumerate(["变量名", "值", "备注"], start=1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = white_bold
    ws2.cell(row=2, column=1, value="username").font = example_font
    ws2.cell(row=2, column=2, value="test001").font = example_font
    ws2.cell(row=2, column=3, value="示例变量，可删除").font = example_font
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 26
    ws2.column_dimensions["C"].width = 40
    ws2.freeze_panes = "A2"

    # ---- Sheet3: 填写说明 ----
    ws3 = wb.create_sheet("填写说明")
    section_font = Font(bold=True, size=12)
    ws3.cell(row=1, column=1, value="接口用例 Excel 模板 · 填写说明").font = section_font
    row = 3
    for key, note in _GUIDE:
        ws3.cell(row=row, column=1, value=key).alignment = Alignment(vertical="top")
        c = ws3.cell(row=row, column=2, value=note)
        c.alignment = wrap
        row += 1
    ws3.column_dimensions["A"].width = 6
    ws3.column_dimensions["B"].width = 100

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_empty_project_bytes() -> BytesIO:
    """生成「空项目」用例文件：只写表头，不写示例/演示用例行。

    供平台「新建项目」脚手架 data/cases/<id>.xlsx：新建后列表为空（靠 loader 的
    allow_empty 读到空），用户直接「新增用例」或「智能导入」往里填；结构与下载模板一致，
    写入时 excel_writer 能直接识别表头。
    """
    wb = Workbook()

    # ---- Sheet1: 用例（仅表头，无示例行）----
    ws = wb.active
    ws.title = "用例"
    header_fill = PatternFill("solid", fgColor="4472C4")
    for col, header in enumerate(_HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = Font(bold=True, color="FFC7CE" if header in _REQUIRED else "FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        note = _HEADER_NOTE.get(header)
        if note:
            cell.comment = Comment(note, "interface-auto")
    widths = [16, 14, 14, 34, 10, 10, 12, 40, 30, 18, 40, 26, 46, 10, 20, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ---- Sheet2: 全局变量（仅表头）----
    ws2 = wb.create_sheet("全局变量")
    white_bold = Font(bold=True, color="FFFFFF")
    for col, header in enumerate(["变量名", "值", "备注"], start=1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = white_bold
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 26
    ws2.column_dimensions["C"].width = 40
    ws2.freeze_panes = "A2"

    # ---- Sheet3: 填写说明 ----
    ws3 = wb.create_sheet("填写说明")
    ws3.cell(row=1, column=1, value="接口用例 · 填写说明").font = Font(bold=True, size=12)
    wrap = Alignment(vertical="center", wrap_text=True)
    row = 3
    for key, note in _GUIDE:
        ws3.cell(row=row, column=1, value=key).alignment = Alignment(vertical="top")
        c = ws3.cell(row=row, column=2, value=note)
        c.alignment = wrap
        row += 1
    ws3.column_dimensions["A"].width = 6
    ws3.column_dimensions["B"].width = 100

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
