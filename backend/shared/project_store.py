"""用例项目注册表：data/projects.json。项目 = 一个 Excel 用例文件。

设计：不同被测系统各占一个 .xlsx，彼此模块/用例隔离，从根上解决"所有用例挤在一个
文件里、模块名跨项目互相污染"。默认项目原地指向存量 data/api_cases.xlsx（不搬动文件），
因此已有的定时任务/场景/执行引用完全不受影响；默认项目不可删除。新建项目在 data/cases/
下脚手架一张空表并登记，删除项目则连同其脚手架文件一并清理。
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from backend.api import case_template
from backend.app import app_case_store
from backend.ui import ui_case_store
from core import excel_loader

ROOT = Path(__file__).resolve().parent.parent.parent  # backend/shared/ 上溯两级 = 项目根
_DATA_FILE = ROOT / "data" / "projects.json"
_CASES_DIR = ROOT / "data" / "cases"
_LOCK = threading.Lock()

DEFAULT_ID = "default"
DEFAULT_FILE = "data/api_cases.xlsx"   # 存量用例文件，原地作为默认项目，不迁移

KIND_API = "api"
KIND_UI = "ui"
KIND_APP = "app"


class ProjectError(Exception):
    """kind: invalid（名称非法/内置限制）/ not_found（项目不存在）/ conflict（重名）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    if not _DATA_FILE.exists():
        return {}
    try:
        with open(_DATA_FILE, encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, _DATA_FILE)


def _default_entry() -> dict:
    now = _now()
    return {
        "id": DEFAULT_ID,
        "name": "默认项目",
        "desc": "存量用例（data/api_cases.xlsx），框架演示用例所在地",
        "file": DEFAULT_FILE,
        "kind": KIND_API,
        "builtin": True,
        "created_at": now,
        "updated_at": now,
    }


def _ensure_default(data: dict) -> bool:
    """注册表缺少默认项时补播种（返回是否有新增，供调用方决定是否落盘）。"""
    if DEFAULT_ID in data:
        return False
    data[DEFAULT_ID] = _default_entry()
    return True


def _count_cases(rel_file: str) -> int:
    """读取项目文件统计用例数；文件缺失/格式异常/空表都记 0，绝不因计数拖垮列表接口。

    按扩展名分流：.apjson → App 用例库；.json → UI 用例库；其余 → 接口 Excel（存量口径不变）。
    """
    try:
        p = ROOT / rel_file
        if not p.exists():
            return 0
        low = rel_file.lower()
        if low.endswith(".apjson"):
            return len(app_case_store.load_cases(p))
        if low.endswith(".json"):
            return len(ui_case_store.load_cases(p))
        return len(excel_loader.load(p, allow_empty=True).cases)
    except Exception:  # noqa: BLE001（计数是尽力而为的展示信息）
        return 0


def _with_count(entry: dict) -> dict:
    out = dict(entry)
    out["case_count"] = _count_cases(entry["file"])
    return out


def list_projects() -> list[dict]:
    """默认项目置顶，其余按创建时间升序；每项附 case_count。"""
    with _LOCK:
        data = _load()
        if _ensure_default(data):
            _save(data)
        items = list(data.values())
    items.sort(key=lambda p: (not p.get("builtin"), p.get("created_at", "")))
    return [_with_count(p) for p in items]


def get(project_id: str) -> dict | None:
    with _LOCK:
        data = _load()
        _ensure_default(data)
        entry = data.get(project_id)
    return _with_count(entry) if entry else None


def _name_taken(data: dict, name: str, exclude_id: str = "") -> bool:
    target = name.strip().lower()
    return any(
        p.get("id") != exclude_id and str(p.get("name", "")).strip().lower() == target
        for p in data.values()
    )


def create(name: str, desc: str = "", kind: str = KIND_API) -> dict:
    name = (name or "").strip()
    if not name:
        raise ProjectError("invalid", "项目名称不能为空")
    if len(name) > 40:
        raise ProjectError("invalid", "项目名称过长（≤40 字）")
    if any(c in name for c in "/\\?#"):
        raise ProjectError("invalid", "项目名称不能含 / \\ ? # 等字符")
    kind = (kind or KIND_API).strip().lower()
    if kind not in (KIND_API, KIND_UI, KIND_APP):
        raise ProjectError("invalid", "项目类型只能是 api、ui 或 app")
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    ext = {KIND_UI: ".json", KIND_APP: ".apjson"}.get(kind, ".xlsx")
    rel_file = f"data/cases/{pid}{ext}"
    abs_file = ROOT / rel_file
    with _LOCK:
        data = _load()
        _ensure_default(data)
        if _name_taken(data, name):
            raise ProjectError("conflict", f"项目名称已存在: {name}")
        # 先脚手架文件再登记：写失败则注册表不留孤儿项
        abs_file.parent.mkdir(parents=True, exist_ok=True)
        if kind == KIND_UI:
            ui_case_store.scaffold(abs_file)
        elif kind == KIND_APP:
            app_case_store.scaffold(abs_file)
        else:
            with open(abs_file, "wb") as fp:
                fp.write(case_template.build_empty_project_bytes().read())
        now = _now()
        entry = {
            "id": pid,
            "name": name,
            "desc": (desc or "").strip(),
            "file": rel_file,
            "kind": kind,
            "builtin": False,
            "created_at": now,
            "updated_at": now,
        }
        data[pid] = entry
        _save(data)
    return _with_count(entry)


_ALLOWED = {"name", "desc"}


def update(project_id: str, **fields) -> dict:
    """仅可改展示名/描述（文件路径不可变，保证重命名安全、引用不断）。"""
    with _LOCK:
        data = _load()
        _ensure_default(data)
        entry = data.get(project_id)
        if entry is None:
            raise ProjectError("not_found", f"项目不存在: {project_id}")
        if "name" in fields:
            new_name = str(fields["name"] or "").strip()
            if not new_name:
                raise ProjectError("invalid", "项目名称不能为空")
            if len(new_name) > 40:
                raise ProjectError("invalid", "项目名称过长（≤40 字）")
            if any(c in new_name for c in "/\\?#"):
                raise ProjectError("invalid", "项目名称不能含 / \\ ? # 等字符")
            if _name_taken(data, new_name, exclude_id=project_id):
                raise ProjectError("conflict", f"项目名称已存在: {new_name}")
            entry["name"] = new_name
        if "desc" in fields:
            entry["desc"] = str(fields["desc"] or "").strip()
        entry["updated_at"] = _now()
        data[project_id] = entry
        _save(data)
    return _with_count(entry)


def delete(project_id: str) -> dict:
    """删除项目：内置默认项拒绝；脚手架项目连同其 data/cases 下的文件一并删除。"""
    with _LOCK:
        data = _load()
        _ensure_default(data)
        entry = data.get(project_id)
        if entry is None:
            raise ProjectError("not_found", f"项目不存在: {project_id}")
        if entry.get("builtin"):
            raise ProjectError("invalid", "默认项目不可删除（存量用例所在地）")
        data.pop(project_id)
        _save(data)
    # 出锁后删文件：仅允许删 data/cases/ 下的脚手架文件，防路径穿越误删他处
    removed_file = None
    f = (ROOT / entry["file"]).resolve()
    cases_root = _CASES_DIR.resolve()
    if f != cases_root and cases_root in f.parents and f.is_file():
        try:
            f.unlink()
            removed_file = entry["file"]
        except OSError:
            pass
    return {"deleted": project_id, "removed_file": removed_file}
