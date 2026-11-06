"""公共脚本库存储：data/scripts.json（平台「公共脚本」页维护，设计文档 2.4）。

脚本定义合法性一律交给 core.scripts.validate_script —— 与执行期同一套口径，
避免「页面存得下、跑起来炸」。写入沿用 users/plans 的临时文件 + os.replace 原子替换。
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from core import scripts as core_scripts
from core.scripts import ScriptError  # 对外沿用同一异常类型，路由层统一转 400

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_FILE = ROOT / "data" / "scripts.json"
# 可重入锁：create/update/delete 持锁内都调用 _write_raw，Lock 不可重入会自锁
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_raw() -> list:
    """读全量脚本定义（文件不存在返回空列表）。"""
    if not DATA_FILE.is_file():
        return []
    try:
        with open(DATA_FILE, encoding="utf-8") as fp:
            raw = json.load(fp)
    except (json.JSONDecodeError, OSError) as e:
        raise ScriptError(f"脚本文件解析失败: {e}")
    items = raw.get("scripts") if isinstance(raw, dict) else raw
    return items if isinstance(items, list) else []


def _write_raw(items: list) -> None:
    with _LOCK:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "scripts": items}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
    # core.scripts.load_scripts 按 mtime 缓存，文件已刷新 → 下一个请求自然读到新内容


def list_scripts() -> list:
    """按名称排序返回全部脚本定义。"""
    return sorted(_read_raw(), key=lambda s: str(s.get("name") or ""))


def get(name: str) -> dict | None:
    for item in _read_raw():
        if item.get("name") == name:
            return item
    return None


def create(name: str, remark: str, steps: list) -> dict:
    """新建脚本：脚本名校验 + 唯一性 + 步骤合法性，一次通过才落盘。"""
    clean = core_scripts.validate_script(name, remark, steps)
    with _LOCK:
        items = _read_raw()
        if any(str(i.get("name")) == clean["name"] for i in items):
            raise ScriptError(f"脚本已存在: {clean['name']}（请直接编辑，或换一个名字）")
        record = {**clean, "updated_at": _now()}
        items.append(record)
        _write_raw(items)
    return record


def update(name: str, new_name: str | None, remark: str | None, steps: list | None) -> dict:
    """更新脚本；改名时由路由层先做引用检查（引用者需同步改用例）。"""
    with _LOCK:
        items = _read_raw()
        target = next((i for i in items if i.get("name") == name), None)
        if target is None:
            raise ScriptError(f"脚本不存在: {name}")
        clean = core_scripts.validate_script(
            (new_name or name).strip(),
            remark if remark is not None else str(target.get("remark") or ""),
            steps if steps is not None else target.get("steps") or [],
        )
        if new_name and new_name.strip() != name:
            if any(str(i.get("name")) == clean["name"] for i in items):
                raise ScriptError(f"目标脚本名已存在: {clean['name']}")
        target.clear()
        target.update({**clean, "updated_at": _now()})
        _write_raw(items)
    return target


def delete(name: str) -> None:
    """删除脚本（调用方须先确认无用例引用，否则数据链会断在执行期）。"""
    with _LOCK:
        items = _read_raw()
        rest = [i for i in items if i.get("name") != name]
        if len(rest) == len(items):
            raise ScriptError(f"脚本不存在: {name}")
        _write_raw(rest)
