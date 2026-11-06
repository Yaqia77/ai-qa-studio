# -*- coding: utf-8 -*-
"""backend.ui.ui_excel 单元测试：模板生成自洽 + 一步一行解析 + 别名归一 + 逐条标注。

对齐接口用例导入解析的「解析+转换+标注、不落盘」口径：本模块只把 Excel 变成
validate_case 能吃的 dict，合法性由 ui_case_store 兜底。
    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_excel.py -v
"""
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ui import ui_excel, ui_case_store as store  # noqa: E402


def _write_steps(path: Path, rows: list[list], headers=None):
    """按 STEP_HEADERS 造一张「UI步骤」表；rows 为纯数据行（不含表头）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = ui_excel.STEP_SHEET
    for col, h in enumerate(headers or ui_excel.STEP_HEADERS, start=1):
        ws.cell(row=1, column=col, value=h)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


# 列顺序：用例编号 名称 模块 优先级 浏览器 无头 是否执行 重试 步骤号 动作 定位器 取值 备注

# ==================== 模板生成自洽 ====================

def test_template_roundtrip_skips_examples():
    b = ui_excel.build_template_bytes()
    assert b and len(b) > 100
    import io
    wb = load_workbook(io.BytesIO(b))
    assert ui_excel.STEP_SHEET in wb.sheetnames
    assert ui_excel.EXTRACT_SHEET in wb.sheetnames
    # 解析模板：示例行应被整段跳过，得到 0 条真实用例
    r = ui_excel.parse_workbook(_bytes_to_path(b), existing_ids=set())
    assert r["total"] == 0
    assert r["items"] == []


def _bytes_to_path(b: bytes) -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ui_excel_test_"))
    p = d / "t.xlsx"
    p.write_bytes(b)
    return p


# ==================== 一步一行分组 ====================

def test_grouping_one_step_per_row(tmp_path):
    rows = [
        ["login_001", "用户登录", "登录", "P1", "chromium", "是", "Y", "0", "1", "打开", "", "/login", ""],
        ["", "", "", "", "", "", "", "", "2", "输入", "#username", "qa", ""],
        ["", "", "", "", "", "", "", "", "3", "点击", "#submit", "", ""],
        ["", "", "", "", "", "", "", "", "4", "断言文本", ".welcome", "欢迎", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert r["total"] == 1
    it = r["items"][0]
    assert it["case_id"] == "login_001"
    assert it["steps_count"] == 4
    assert it["importable"] is True
    assert it["exists"] is False
    # 用例级字段仅首行取，后续行继承（name/module/priority 正确）
    assert it["case"]["name"] == "用户登录"
    assert it["case"]["module"] == "登录"
    assert it["case"]["priority"] == "P1"


def test_multiple_cases_by_id(tmp_path):
    rows = [
        ["c_a", "用例A", "M", "P1", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
        ["c_b", "用例B", "M", "P2", "chromium", "否", "Y", "0", "1", "打开", "", "/b", ""],
        ["", "", "", "", "", "", "", "", "2", "点击", "#x", "", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert {i["case_id"] for i in r["items"]} == {"c_a", "c_b"}
    by = {i["case_id"]: i for i in r["items"]}
    assert by["c_a"]["steps_count"] == 1
    assert by["c_b"]["steps_count"] == 2
    # 无头列归一：c_a 是→True，c_b 否→False
    assert by["c_a"]["case"]["headless"] is True
    assert by["c_b"]["case"]["headless"] is False


# ==================== 中文动作别名归一 ====================

def test_actions_normalized_to_english(tmp_path):
    rows = [
        ["n_001", "别名", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/x", ""],
        ["", "", "", "", "", "", "", "", "2", "输入", "#u", "v", ""],
        ["", "", "", "", "", "", "", "", "3", "点击", "#s", "", ""],
        ["", "", "", "", "", "", "", "", "4", "断言文本", ".w", "欢迎", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    acts = [s["action"] for s in r["items"][0]["case"]["steps"]]
    assert acts == ["goto", "fill", "click", "expect_text"]


# ==================== 非法动作标注 ====================

def test_unknown_action_marks_error(tmp_path):
    rows = [
        ["bad_001", "非法动作", "M", "P2", "chromium", "是", "Y", "0", "1", "飞翔", "#x", "", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    it = r["items"][0]
    assert it["importable"] is False
    assert any(m["level"] == "error" and m["field"] == "action" for m in it["marks"])


def test_missing_value_marks_error(tmp_path):
    # fill 需要取值，留空 → validate 抛错 → 标注不可导入
    rows = [
        ["nov_001", "缺取值", "M", "P2", "chromium", "是", "Y", "0", "1", "输入", "#u", "", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert r["items"][0]["importable"] is False


# ==================== 示例行跳过（含延续行） ====================

def test_example_rows_skipped_including_continuation(tmp_path):
    rows = [
        ["示例_x_001", "样例", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
        ["", "", "", "", "", "", "", "", "2", "点击", "#b", "", ""],  # 示例延续行也跳过
        ["real_001", "真实", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/r", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert [i["case_id"] for i in r["items"]] == ["real_001"]


# ==================== 重复编号 exists 标注 ====================

def test_existing_id_marked_exists(tmp_path):
    rows = [
        ["dup_001", "重复", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids={"DUP_001"})  # 大小写不敏感
    it = r["items"][0]
    assert it["exists"] is True


# ==================== 缺列容错 ====================

def test_missing_case_id_first_row_autogen(tmp_path):
    rows = [
        ["", "无编号", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    it = r["items"][0]
    assert it["case_id"].startswith("imp_")
    assert any(m["level"] == "warn" and m["field"] == "case_id" for m in it["marks"])


def test_unmapped_headers_tolerated(tmp_path):
    # 表头带多余列 + 括号注释：应归一识别已知列，未知列进 unmapped
    headers = ui_excel.STEP_HEADERS + ["无关列"]
    rows = [
        ["h_001", "表头容错", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", "", "x"],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows, headers=headers)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert r["total"] == 1
    assert "无关列" in r["unmapped_headers"]


def test_header_with_annotation_normalized(tmp_path):
    # 表头带星号/括号注释（如「用例编号（唯一）」）仍能映射
    headers = [h + "（必填）" if i == 0 else h for i, h in enumerate(ui_excel.STEP_HEADERS)]
    rows = [
        ["ann_001", "注释表头", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows, headers=headers)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    assert r["total"] == 1
    assert r["items"][0]["case_id"] == "ann_001"


# ==================== 变量提取 Sheet 关联 ====================

def test_extract_sheet_associated(tmp_path):
    p = tmp_path / "e.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = ui_excel.STEP_SHEET
    for col, h in enumerate(ui_excel.STEP_HEADERS, start=1):
        ws.cell(row=1, column=col, value=h)
    ws.append(["ex_001", "带提取", "M", "P2", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""])
    ws2 = wb.create_sheet(ui_excel.EXTRACT_SHEET)
    for col, h in enumerate(ui_excel.EXTRACT_HEADERS, start=1):
        ws2.cell(row=1, column=col, value=h)
    ws2.append(["ex_001", "first_result", "text|.result"])
    wb.save(p)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    it = r["items"][0]
    assert it["importable"] is True
    assert it["case"]["extract"] == [{"var": "first_result", "source": "text|.result"}]


# ==================== 解析结果可直接落库 ====================

def test_parsed_case_passes_validate_and_persists(tmp_path):
    rows = [
        ["rt_001", "可落库", "M", "P1", "chromium", "是", "Y", "0", "1", "打开", "", "/a", ""],
        ["", "", "", "", "", "", "", "", "2", "输入", "#u", "v", ""],
    ]
    p = _write_steps(tmp_path / "s.xlsx", rows)
    r = ui_excel.parse_workbook(p, existing_ids=set())
    case = r["items"][0]["case"]
    jsonf = tmp_path / "cases.json"
    store.scaffold(jsonf)
    created = store.create_case(jsonf, case)
    assert created["case_id"] == "rt_001"
    assert [s["action"] for s in created["steps"]] == ["goto", "fill"]


# ==================== 导出 → 回导 roundtrip ====================

def test_export_roundtrip_reimportable(tmp_path):
    """项目用例 → 导出 Excel → 再解析：条数/步骤/布尔/重试/变量提取一致（可直接回导）。"""
    raw = [
        {"case_id": "exp_001", "name": "登录", "module": "登录", "priority": "P1",
         "run": True, "browser": "chromium", "headless": False, "retry": 2,
         "steps": [{"action": "goto", "value": "/login"},
                   {"action": "fill", "locator": "#u", "value": "qa"},
                   {"action": "expect_text", "locator": ".w", "value": "欢迎"}],
         "extract": [{"var": "tok", "source": "cookie:token"}]},
        {"case_id": "exp_002", "name": "搜索", "module": "搜索", "priority": "P2",
         "steps": [{"action": "goto", "value": "/search"}]},
    ]
    cases = [store.validate_case(c) for c in raw]
    b = ui_excel.build_export_bytes(cases)
    assert b[:2] == b"PK"  # xlsx 魔数
    r = ui_excel.parse_workbook(_bytes_to_path(b), existing_ids=set())
    assert r["total"] == 2
    by = {i["case_id"]: i for i in r["items"]}
    c1 = by["exp_001"]["case"]
    assert by["exp_001"]["steps_count"] == 3
    assert by["exp_001"]["importable"] is True
    assert c1["headless"] is False and c1["run"] is True and c1["retry"] == 2
    assert [s["action"] for s in c1["steps"]] == ["goto", "fill", "expect_text"]
    assert c1["extract"] == [{"var": "tok", "source": "cookie:token"}]
    assert by["exp_002"]["case"]["headless"] is True  # 未填默认是


def test_export_ids_conflict_with_existing(tmp_path):
    """导出件回导同项目时 exists 标注生效（同编号按重复跳过，不会双写）。"""
    raw = {"case_id": "cf_001", "name": "查重", "module": "M",
           "steps": [{"action": "goto", "value": "/a"}]}
    b = ui_excel.build_export_bytes([store.validate_case(raw)])
    r = ui_excel.parse_workbook(_bytes_to_path(b), existing_ids={"cf_001"})
    assert r["items"][0]["exists"] is True
