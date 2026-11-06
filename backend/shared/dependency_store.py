"""用例依赖关系持久化：data/dependencies.json。

结构：{ "case_id": ["dep_id_1", "dep_id_2", ...], ... }
含义：执行 case_id 之前，dep_id_1 和 dep_id_2 必须全部通过。
"""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_FILE = ROOT / "data" / "dependencies.json"
_LOCK = threading.Lock()


def _load() -> dict:
    if not _DATA_FILE.exists():
        return {}
    try:
        with open(_DATA_FILE, encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, _DATA_FILE)


def get_all() -> dict:
    """返回完整依赖映射 {case_id: [dep_ids]}。"""
    with _LOCK:
        return dict(_load())


def get_deps(case_id: str) -> list[str]:
    """获取某个用例的直接依赖列表。"""
    with _LOCK:
        return list(_load().get(case_id, []))


def set_deps(case_id: str, deps: list[str]) -> None:
    """设置某个用例的依赖列表（覆盖式）。空列表 = 无依赖（删除条目）。"""
    with _LOCK:
        data = _load()
        if deps:
            # 去重、去自身
            clean = list(dict.fromkeys(d for d in deps if d != case_id))
            data[case_id] = clean
        else:
            data.pop(case_id, None)
        _save(data)


def add_dep(case_id: str, dep_id: str) -> bool:
    """添加单条依赖。返回是否新增（已存在返回 False）。"""
    if case_id == dep_id:
        return False
    with _LOCK:
        data = _load()
        deps = data.setdefault(case_id, [])
        if dep_id in deps:
            return False
        deps.append(dep_id)
        _save(data)
    return True


def remove_dep(case_id: str, dep_id: str) -> bool:
    """移除单条依赖。"""
    with _LOCK:
        data = _load()
        deps = data.get(case_id, [])
        if dep_id not in deps:
            return False
        deps.remove(dep_id)
        if not deps:
            data.pop(case_id, None)
        _save(data)
    return True


def delete_case(case_id: str) -> None:
    """用例被删除时清理：移除该 case 作为主体的依赖，以及作为其他 case 依赖的引用。"""
    with _LOCK:
        data = _load()
        data.pop(case_id, None)
        # 从其他用例的依赖列表中移除
        for k in list(data.keys()):
            if case_id in data[k]:
                data[k].remove(case_id)
                if not data[k]:
                    data.pop(k, None)
        _save(data)


def has_cycle(case_id: str, dep_id: str) -> bool:
    """检测添加 case_id -> dep_id 后是否形成环。"""
    with _LOCK:
        data = _load()
    # 从 dep_id 出发，沿依赖链能否到达 case_id
    visited = set()
    stack = [dep_id]
    while stack:
        node = stack.pop()
        if node == case_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(data.get(node, []))
    return False
