# -*- coding: utf-8 -*-
"""App 用例库：一个项目一个 JSON 文件（data/cases/<id>.apjson），当前为预留位（脚手架）。

设计：与 UI 用例（ui_case_store）同构，只是把"浏览器"换成"平台（android/ios）+ 被测应用包名"，
动作集换成移动端手势/断言。载体扩展名用 `.apjson` 以与 UI 的 `.json`、接口的 Excel 在注册表分流上互斥。
校验不通过绝不落盘（临时文件 + os.replace 原子替换），错误口径与 ui_case_store 对齐
（AppCaseError.kind ∈ invalid/not_found/conflict/locked），便于路由统一映射 HTTP 状态。

实际驱动（Appium 等）尚未接入；本模块只负责用例的规范化存储与校验，前端可先行编辑维护。
"""
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

VALID_PRIORITIES = {"P0", "P1", "P2", "P3", "P4", "P5"}
VALID_PLATFORMS = {"android", "ios"}

# 步骤动作元数据：desc 中文名；locator/value 标记该动作是否需要定位器/取值。
ACTIONS: dict[str, dict] = {
    "launch":         {"desc": "启动应用", "locator": False, "value": True},   # value=包名/BundleID/deeplink
    "tap":            {"desc": "点击",     "locator": True,  "value": False},
    "long_press":     {"desc": "长按",     "locator": True,  "value": False},
    "input":          {"desc": "输入文本", "locator": True,  "value": True},
    "swipe":          {"desc": "滑动",     "locator": False, "value": True},   # value=方向 up/down/left/right
    "key_press":      {"desc": "按键",     "locator": False, "value": True},   # value=back/home/enter...
    "assert_visible": {"desc": "断言可见", "locator": True,  "value": False},
    "assert_text":    {"desc": "断言文本", "locator": True,  "value": True},
    "screenshot":     {"desc": "截图",     "locator": False, "value": False},
    "wait":           {"desc": "等待",     "locator": False, "value": True},   # value=秒数
}
VALID_ACTIONS = set(ACTIONS)

# 定位器前缀（App 元素定位方言）；裸值默认按 accessibility_id 处理。
_LOCATOR_PREFIXES = ("id=", "xpath=", "text=", "accessibility_id=", "class=", "uiautomator=")

_CASE_ID_RE = re.compile(r"[\r\n\t]")


class AppCaseError(Exception):
    """kind: invalid（字段非法）/ not_found（文件或用例不存在）/ conflict（编号重复）/ locked（文件被占用）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


_LOCK = threading.Lock()


# ==================== 读写底座 ====================

def scaffold(path: Path) -> None:
    """新建 App 项目时脚手架一张空用例表：{"version":1,"cases":[]}。"""
    _write(path, {"version": 1, "cases": []})


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except OSError as e:
        _safe_remove(tmp)
        raise AppCaseError("invalid", f"写入临时文件失败: {e!r}")
    try:
        os.replace(tmp, path)
    except PermissionError:
        _safe_remove(tmp)
        raise AppCaseError("locked", f"文件 {path.name} 正被其他程序占用，请关闭后重试")


def _safe_remove(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _read(path: Path) -> dict:
    if not path.exists():
        raise AppCaseError("not_found", f"App 用例文件不存在: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AppCaseError("invalid", f"App 用例文件不是合法 JSON: {path.name}（{e}）")
    except OSError as e:
        raise AppCaseError("invalid", f"读取 App 用例文件失败: {e!r}")
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise AppCaseError("invalid", f"App 用例文件结构应为 {{'cases': [...]}}: {path.name}")
    return data


def load_cases(path: Path) -> list[dict]:
    """容错读取用例列表：文件缺失返回空（供项目计数/列表在 allow_empty 语义下复用）。"""
    if not path.exists():
        return []
    return _read(path)["cases"]


# ==================== 校验 ====================

def _validate_locator(loc: str, idx: int) -> str:
    loc = (loc or "").strip()
    if not loc:
        raise AppCaseError("invalid", f"第 {idx} 步缺少定位器")
    return loc


def _validate_step(step: object, idx: int) -> dict:
    if not isinstance(step, dict):
        raise AppCaseError("invalid", f"第 {idx} 步必须是对象")
    action = str(step.get("action") or "").strip()
    if action not in VALID_ACTIONS:
        raise AppCaseError("invalid", f"第 {idx} 步动作非法: {action!r}（可选: {'/'.join(sorted(VALID_ACTIONS))}）")
    meta = ACTIONS[action]
    out: dict = {"action": action, "desc": str(step.get("desc") or "").strip()}
    locator = str(step.get("locator") or "").strip()
    value = step.get("value")
    if meta["locator"]:
        out["locator"] = _validate_locator(locator, idx)
    elif locator:
        out["locator"] = locator
    if meta["value"]:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AppCaseError("invalid", f"第 {idx} 步（{ACTIONS[action]['desc']}）缺少取值")
        out["value"] = value
    elif value is not None and value != "":
        out["value"] = value
    return out


def validate_case(raw: dict) -> dict:
    """整条用例规范化 + 校验；返回清洗后的 dict（不落盘）。任一字段非法即抛 AppCaseError。"""
    if not isinstance(raw, dict):
        raise AppCaseError("invalid", "用例必须是对象")
    case_id = str(raw.get("case_id") or "").strip()
    if not case_id:
        raise AppCaseError("invalid", "用例编号不能为空")
    if _CASE_ID_RE.search(case_id):
        raise AppCaseError("invalid", "用例编号不能包含换行/制表符")

    priority = str(raw.get("priority") or "P2").strip().upper()
    if priority not in VALID_PRIORITIES:
        raise AppCaseError("invalid", f"优先级必须是 {'/'.join(sorted(VALID_PRIORITIES))}，实际为 {priority!r}")

    platform = str(raw.get("platform") or "android").strip().lower()
    if platform not in VALID_PLATFORMS:
        raise AppCaseError("invalid", f"平台只能是 {'/'.join(sorted(VALID_PLATFORMS))}，实际为 {platform!r}")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AppCaseError("invalid", "App 用例至少需要一个步骤")
    clean_steps = [_validate_step(s, i + 1) for i, s in enumerate(steps)]

    extract = raw.get("extract") or []
    if not isinstance(extract, list):
        raise AppCaseError("invalid", "变量提取必须是数组")
    clean_extract = []
    for i, e in enumerate(extract, 1):
        if not isinstance(e, dict) or not str(e.get("var") or "").strip() or not str(e.get("source") or "").strip():
            raise AppCaseError("invalid", f"第 {i} 条变量提取需含 var 与 source")
        clean_extract.append({"var": str(e["var"]).strip(), "source": str(e["source"]).strip()})

    def _str_list(key: str) -> list[str]:
        v = raw.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise AppCaseError("invalid", f"{key} 必须是字符串数组")
        return [x.strip() for x in v if x.strip()]

    return {
        "case_id": case_id,
        "module": str(raw.get("module") or "未分类").strip() or "未分类",
        "name": str(raw.get("name") or "新建App用例").strip() or "新建App用例",
        "desc": str(raw.get("desc") or "").strip(),
        "priority": priority,
        "run": bool(raw.get("run", True)),
        "platform": platform,
        "app": str(raw.get("app") or "").strip(),
        "steps": clean_steps,
        "extract": clean_extract,
        "pre_scripts": _str_list("pre_scripts"),
        "post_scripts": _str_list("post_scripts"),
    }


# ==================== 查询 ====================

def _match(case: dict, module: str | None, priority: str | None,
           keyword: str | None, run: bool | None) -> bool:
    if run is not None and bool(case.get("run", True)) is not run:
        return False
    if module and module not in str(case.get("module", "")):
        return False
    if priority:
        wanted = {p.strip().upper() for p in priority.split(",") if p.strip()}
        if str(case.get("priority", "")).upper() not in wanted:
            return False
    if keyword:
        kw = keyword.strip().lower()
        hay = " ".join([str(case.get("case_id", "")), str(case.get("name", "")),
                        str(case.get("desc", ""))]).lower()
        if kw not in hay:
            return False
    return True


def list_cases(path: Path, module: str | None = None, priority: str | None = None,
               keyword: str | None = None, run: bool | None = None) -> list[dict]:
    cases = load_cases(path)
    return [c for c in cases if _match(c, module, priority, keyword, run)]


def get_case(path: Path, case_id: str) -> dict | None:
    return next((c for c in load_cases(path) if c.get("case_id") == case_id), None)


def meta(path: Path) -> dict:
    """编辑用元数据：模块/优先级候选 + 平台 + 动作字典（前端步骤编辑器据此渲染下拉）。"""
    cases = load_cases(path)
    return {
        "priorities": sorted(VALID_PRIORITIES),
        "platforms": sorted(VALID_PLATFORMS),
        "modules": sorted({str(c.get("module", "")) for c in cases if c.get("module")}),
        "actions": [{"action": a, "desc": m["desc"],
                     "need_locator": m["locator"], "need_value": m["value"]}
                    for a, m in sorted(ACTIONS.items())],
    }


# ==================== 写操作 ====================

def create_case(path: Path, raw: dict) -> dict:
    case = validate_case(raw)
    with _LOCK:
        data = _read(path)
        if any(c.get("case_id") == case["case_id"] for c in data["cases"]):
            raise AppCaseError("conflict", f"用例编号已存在: {case['case_id']}")
        data["cases"].append(case)
        _write(path, data)
    return case


def update_case(path: Path, case_id: str, changes: dict) -> dict:
    """按编号定位整条替换可改字段（case_id 不可改）；合并后重走全量校验才落盘。"""
    changes = {k: v for k, v in changes.items() if k != "case_id"}
    if not changes:
        raise AppCaseError("invalid", "没有需要更新的字段")
    with _LOCK:
        data = _read(path)
        idx = next((i for i, c in enumerate(data["cases"]) if c.get("case_id") == case_id), None)
        if idx is None:
            raise AppCaseError("not_found", f"用例不存在: {case_id}")
        merged = {**data["cases"][idx], **changes}
        merged["case_id"] = case_id
        cleaned = validate_case(merged)
        data["cases"][idx] = cleaned
        _write(path, data)
    return cleaned


def delete_case(path: Path, case_id: str) -> None:
    with _LOCK:
        data = _read(path)
        before = len(data["cases"])
        data["cases"] = [c for c in data["cases"] if c.get("case_id") != case_id]
        if len(data["cases"]) == before:
            raise AppCaseError("not_found", f"用例不存在: {case_id}")
        _write(path, data)


def move_case(src_path: Path, dst_path: Path, case_id: str, new_module: str | None = None) -> dict:
    """跨 App 项目迁移：先写目标（查重）→ 再从源删；源删失败回滚目标，保证不出现双份。"""
    if src_path == dst_path:
        raise AppCaseError("invalid", "源项目与目标项目相同，无需迁移")
    with _LOCK:
        src = _read(src_path)
        case = next((c for c in src["cases"] if c.get("case_id") == case_id), None)
        if case is None:
            raise AppCaseError("not_found", f"源项目中不存在用例: {case_id}")
        moved = dict(case)
        if (new_module or "").strip():
            moved["module"] = new_module.strip()
        moved = validate_case(moved)

        dst = _read(dst_path)
        if any(c.get("case_id") == case_id for c in dst["cases"]):
            raise AppCaseError("conflict", f"目标项目已存在同编号用例: {case_id}")
        dst["cases"].append(moved)
        _write(dst_path, dst)

        src["cases"] = [c for c in src["cases"] if c.get("case_id") != case_id]
        try:
            _write(src_path, src)
        except AppCaseError:
            rollback = _read(dst_path)
            rollback["cases"] = [c for c in rollback["cases"] if c.get("case_id") != case_id]
            _write(dst_path, rollback)
            raise
    return moved
