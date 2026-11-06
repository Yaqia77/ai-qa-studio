# -*- coding: utf-8 -*-
"""backend.ui_case_store 单元测试：校验 / 查重 / 原子写 / 增删改查 / 迁移。

与执行用例（tests/ 目录，由 main.py 收集）隔离：本目录用 pytest 直接跑，
不进平台执行批次，不污染 allure 报告。
    .venv/Scripts/python.exe -m pytest unit_tests/test_ui_case_store.py -v
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ui import ui_case_store as store  # noqa: E402
from backend.ui.ui_case_store import UiCaseError  # noqa: E402


def _case(cid="ui_login_001", **over):
    base = {
        "case_id": cid, "module": "登录", "name": "用户登录",
        "desc": "验证账号密码登录成功", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": "/login"},
            {"action": "fill", "locator": "css=#username", "value": "{{username}}"},
            {"action": "click", "locator": "css=button[type=submit]"},
            {"action": "expect_text", "locator": "css=.welcome", "value": "欢迎"},
        ],
        "extract": [{"var": "order_id", "source": "text|css=.order-id"}],
    }
    base.update(over)
    return base


@pytest.fixture
def case_file(tmp_path):
    p = tmp_path / "cases.json"
    store.scaffold(p)
    return p


# ==================== 校验 ====================

def test_validate_ok_normalizes():
    c = store.validate_case(_case())
    assert c["case_id"] == "ui_login_001"
    assert c["priority"] == "P0"
    assert c["browser"] == "chromium"
    assert c["headless"] is True
    assert len(c["steps"]) == 4
    assert c["steps"][0] == {"action": "goto", "desc": "", "value": "/login"}
    assert c["extract"] == [{"var": "order_id", "source": "text|css=.order-id"}]


def test_validate_empty_id_rejected():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(case_id="  "))
    assert e.value.kind == "invalid"


def test_validate_bad_priority():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(priority="PX"))
    assert e.value.kind == "invalid"


def test_validate_bad_action():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(steps=[{"action": "fly", "locator": "css=#a"}]))
    assert e.value.kind == "invalid"


def test_validate_step_missing_locator():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(steps=[{"action": "click"}]))
    assert "定位器" in str(e.value)


def test_validate_step_missing_value():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(steps=[{"action": "fill", "locator": "css=#a"}]))
    assert e.value.kind == "invalid"


def test_validate_no_steps():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(steps=[]))
    assert "至少需要一个步骤" in str(e.value)


def test_validate_bad_browser():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(browser="ie"))
    assert e.value.kind == "invalid"


def test_validate_bad_extract():
    with pytest.raises(UiCaseError) as e:
        store.validate_case(_case(extract=[{"var": "x"}]))
    assert e.value.kind == "invalid"


# ==================== 失败重试字段 ====================

def test_validate_retry_absent_defaults_zero():
    # 存量用例无 retry 键 → 规范化为 0（不重试，行为不变）
    assert store.validate_case(_case())["retry"] == 0


@pytest.mark.parametrize("raw,expect", [
    (2, 2), ("2", 2), (0, 0), (None, 0),
    (-1, 0), (99, store.MAX_RETRY), ("abc", 0), (1.9, 1),
])
def test_validate_retry_normalizes(raw, expect):
    # 宽归一化：合法取整、负数/非法→0、超上限截断到 MAX_RETRY
    assert store.validate_case(_case(retry=raw))["retry"] == expect


def test_retry_roundtrip_persisted(case_file):
    store.create_case(case_file, _case(retry=2))
    assert store.get_case(case_file, "ui_login_001")["retry"] == 2
    updated = store.update_case(case_file, "ui_login_001", {"retry": 0})
    assert updated["retry"] == 0


# ==================== 查询 ====================

def test_load_cases_missing_file_returns_empty(tmp_path):
    assert store.load_cases(tmp_path / "nope.json") == []


def test_scaffold_creates_empty(tmp_path):
    p = tmp_path / "n.json"
    store.scaffold(p)
    assert store.load_cases(p) == []
    assert json.loads(p.read_text(encoding="utf-8")) == {"version": 1, "cases": []}


# ==================== 增删改查 + 查重 ====================

def test_create_and_get(case_file):
    created = store.create_case(case_file, _case())
    assert created["case_id"] == "ui_login_001"
    got = store.get_case(case_file, "ui_login_001")
    assert got is not None and got["name"] == "用户登录"
    assert store.get_case(case_file, "missing") is None


def test_create_duplicate_rejected(case_file):
    store.create_case(case_file, _case())
    with pytest.raises(UiCaseError) as e:
        store.create_case(case_file, _case())
    assert e.value.kind == "conflict"
    # 查重失败不应污染已有数据
    assert len(store.load_cases(case_file)) == 1


def test_update_changes_fields(case_file):
    store.create_case(case_file, _case())
    updated = store.update_case(case_file, "ui_login_001", {"priority": "P1", "name": "改名"})
    assert updated["priority"] == "P1" and updated["name"] == "改名"
    assert updated["case_id"] == "ui_login_001"  # case_id 不可改


def test_update_missing_case(case_file):
    with pytest.raises(UiCaseError) as e:
        store.update_case(case_file, "ghost", {"priority": "P1"})
    assert e.value.kind == "not_found"


def test_update_invalid_not_persisted(case_file):
    store.create_case(case_file, _case())
    with pytest.raises(UiCaseError):
        store.update_case(case_file, "ui_login_001", {"priority": "BAD"})
    # 非法改动不落盘：原优先级仍在
    assert store.get_case(case_file, "ui_login_001")["priority"] == "P0"


def test_delete_case(case_file):
    store.create_case(case_file, _case())
    store.delete_case(case_file, "ui_login_001")
    assert store.load_cases(case_file) == []
    with pytest.raises(UiCaseError) as e:
        store.delete_case(case_file, "ui_login_001")
    assert e.value.kind == "not_found"


# ==================== 筛选 ====================

def test_list_cases_filters(case_file):
    store.create_case(case_file, _case("ui_a", module="登录", priority="P0"))
    store.create_case(case_file, _case("ui_b", module="下单", priority="P2", run=False))
    assert {c["case_id"] for c in store.list_cases(case_file)} == {"ui_a", "ui_b"}
    assert {c["case_id"] for c in store.list_cases(case_file, module="登录")} == {"ui_a"}
    assert {c["case_id"] for c in store.list_cases(case_file, priority="P0,P2")} == {"ui_a", "ui_b"}
    assert {c["case_id"] for c in store.list_cases(case_file, run=True)} == {"ui_a"}
    assert {c["case_id"] for c in store.list_cases(case_file, keyword="ui_b")} == {"ui_b"}


# ==================== 原子写 ====================

def test_invalid_create_does_not_write(case_file):
    with pytest.raises(UiCaseError):
        store.create_case(case_file, _case(steps=[{"action": "fly"}]))
    assert store.load_cases(case_file) == []
    # 无残留临时文件
    assert not list(case_file.parent.glob("*.tmp"))


def test_corrupt_json_readable_error(case_file):
    case_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(UiCaseError) as e:
        store.load_cases(case_file)
    assert e.value.kind == "invalid"


# ==================== 迁移 ====================

def test_move_between_projects(tmp_path):
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    store.scaffold(src)
    store.scaffold(dst)
    store.create_case(src, _case("ui_m1", module="源模块"))
    moved = store.move_case(src, dst, "ui_m1", new_module="目标模块")
    assert moved["module"] == "目标模块"
    assert store.load_cases(src) == []
    assert [c["case_id"] for c in store.load_cases(dst)] == ["ui_m1"]


def test_move_same_project_rejected(tmp_path):
    p = tmp_path / "one.json"
    store.scaffold(p)
    store.create_case(p, _case("ui_x"))
    with pytest.raises(UiCaseError) as e:
        store.move_case(p, p, "ui_x")
    assert e.value.kind == "invalid"


def test_move_conflict_target_has_id(tmp_path):
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    store.scaffold(src)
    store.scaffold(dst)
    store.create_case(src, _case("ui_dup"))
    store.create_case(dst, _case("ui_dup"))
    with pytest.raises(UiCaseError) as e:
        store.move_case(src, dst, "ui_dup")
    assert e.value.kind == "conflict"
    # 冲突时源用例仍在（未删）
    assert [c["case_id"] for c in store.load_cases(src)] == ["ui_dup"]


# ==================== meta ====================

def test_meta_lists_actions_and_modules(case_file):
    store.create_case(case_file, _case(module="登录"))
    m = store.meta(case_file)
    assert "登录" in m["modules"]
    assert "chromium" in m["browsers"]
    assert any(a["action"] == "goto" for a in m["actions"])
    assert set(m["priorities"]) >= {"P0", "P1"}


def test_meta_actions_have_group():
    # 动作字典带 group 字段（步骤编辑器分组下拉驱动）：常用/高级
    m = store.meta(Path("__nonexistent__.json"))
    by_action = {a["action"]: a for a in m["actions"]}
    assert by_action["goto"]["group"] == "常用"
    assert by_action["click"]["group"] == "常用"
    assert by_action["hover"]["group"] == "高级"
    assert {a["group"] for a in m["actions"]} <= {"常用", "高级"}


# ==================== 中文动作别名归一 ====================

@pytest.mark.parametrize("raw,expect", [
    ("打开", "goto"), ("访问", "goto"), ("goto", "goto"), ("GOTO", "goto"),
    ("点击", "click"), ("输入", "fill"), ("填写", "fill"),
    ("断言文本", "expect_text"), ("包含文本", "expect_text"),
    ("断言可见", "expect_visible"), ("等待出现", "wait_for"),
    (" 点击 ", "click"), ("", ""),
])
def test_normalize_action(raw, expect):
    assert store.normalize_action(raw) == expect


def test_normalize_action_unknown():
    # 无法识别：去空白小写后原样返回（交由 validate 报非法）
    assert store.normalize_action("飞翔") == "飞翔"
    assert store.normalize_action("FLY") == "fly"


def test_validate_accepts_chinese_action():
    # 中文动作经归一后能过校验，落库为规范英文键
    c = store.validate_case(_case(steps=[
        {"action": "打开", "value": "/login"},
        {"action": "输入", "locator": "#u", "value": "qa"},
        {"action": "断言文本", "locator": ".w", "value": "欢迎"},
    ]))
    assert [s["action"] for s in c["steps"]] == ["goto", "fill", "expect_text"]


@pytest.mark.parametrize("raw,expect", [
    ("是", True), ("Y", True), ("1", True), ("true", True), (True, True),
    ("否", False), ("N", False), ("0", False), ("跳过", False), (False, False),
])
def test_normalize_bool(raw, expect):
    assert store.normalize_bool(raw, True) is expect


def test_normalize_bool_default():
    assert store.normalize_bool("乱填", default=True) is True
    assert store.normalize_bool("乱填", default=False) is False


# ==================== 动作扩展校验 / meta hint / 新定位前缀 ====================

P5_NEW_ACTIONS = {
    "uncheck", "dblclick", "upload", "right_click", "scroll_to", "drag_to", "navigate",
    "wait_load", "wait_state", "sleep", "expect_hidden", "expect_count", "expect_value",
    "expect_attr", "expect_enabled", "expect_title", "switch_frame", "switch_main", "switch_window",
}


def test_p5_actions_registered_baseline():
    assert P5_NEW_ACTIONS <= set(store.VALID_ACTIONS)
    assert len(store.ACTIONS) == 31          # 动作基线：12 → 31，新动作只增不减
    assert len(store.VALID_ACTIONS) == len(store.ACTIONS)


@pytest.mark.parametrize("action", sorted(P5_NEW_ACTIONS))
def test_p5_meta_contains_action_with_hint_field(action):
    m = store.meta(Path("__none__.json"))
    by = {a["action"]: a for a in m["actions"]}
    assert action in by
    assert "hint" in by[action]              # 每条都下发 hint（可为空串）


def test_p5_meta_need_locator_value_matches_definition():
    # meta 下发的 need_locator/need_value 必须与 ACTIONS 定义同口径（单一事实源）
    by = {a["action"]: a for a in store.meta(Path("__none__.json"))["actions"]}
    for act, meta in store.ACTIONS.items():
        assert by[act]["need_locator"] == meta["locator"], act
        assert by[act]["need_value"] == meta["value"], act


def test_p5_hint_present_on_special_actions():
    by = {a["action"]: a for a in store.meta(Path("__none__.json"))["actions"]}
    # 取值/定位有特殊语义的动作必须带非空 hint（编辑器据此做占位提示）
    for act in ("upload", "drag_to", "expect_attr", "switch_frame", "wait_load", "switch_window"):
        assert by[act]["hint"], act


def test_p5_step_validate_pass_and_missing():
    def ok(step):
        assert store.validate_case(_case(steps=[step]))["steps"][0]["action"] == step["action"]

    ok({"action": "upload", "locator": "#file", "value": "a.txt"})
    ok({"action": "drag_to", "locator": "#src", "value": "#dst"})
    ok({"action": "wait_state", "locator": "#x", "value": "visible"})
    ok({"action": "expect_attr", "locator": "#a", "value": "href=/x"})
    ok({"action": "expect_count", "locator": ".item", "value": "3"})
    ok({"action": "navigate", "value": "back"})
    ok({"action": "sleep", "value": "1000"})
    ok({"action": "expect_title", "value": "首页"})
    ok({"action": "switch_window", "value": "last"})
    ok({"action": "uncheck", "locator": "#agree"})
    ok({"action": "switch_frame", "locator": "#app-frame"})
    ok({"action": "switch_main"})
    # 需定位器却缺 → invalid
    for step in ({"action": "upload", "value": "a.txt"}, {"action": "uncheck"}, {"action": "switch_frame"}):
        with pytest.raises(UiCaseError):
            store.validate_case(_case(steps=[step]))
    # 需取值却缺 → invalid
    for step in ({"action": "upload", "locator": "#f"}, {"action": "navigate"}, {"action": "sleep"}):
        with pytest.raises(UiCaseError):
            store.validate_case(_case(steps=[step]))


@pytest.mark.parametrize("loc", [
    "testid=login", "placeholder=搜索", "alt=头像",
    "role=button,name=登录", "css=#a", "xpath=//div", "text=你好", "#bare",
])
def test_p5_locator_prefix_accepted(loc):
    assert store._validate_locator(loc, 1) == loc


@pytest.mark.parametrize("raw,expect", [
    ("取消勾选", "uncheck"), ("双击", "dblclick"), ("上传", "upload"), ("拖拽", "drag_to"),
    ("刷新", "navigate"), ("等待加载", "wait_load"), ("等待状态", "wait_state"), ("固定等待", "sleep"),
    ("断言不可见", "expect_hidden"), ("断言数量", "expect_count"), ("断言属性", "expect_attr"),
    ("断言标题", "expect_title"), ("切入iframe", "switch_frame"), ("回主文档", "switch_main"),
    ("切换标签", "switch_window"),
])
def test_p5_alias_normalize(raw, expect):
    assert store.normalize_action(raw) == expect
