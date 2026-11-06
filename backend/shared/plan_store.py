"""测试场景（Plan）数据持久化：data/plans.json，原子写模式与 runs_history.json 一致。

场景是一组可复用的用例集合（case_ids），保存环境偏好，支持一键发起执行。
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # backend/shared/ 上溯两级 = 项目根
_DATA_FILE = ROOT / "data" / "plans.json"
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


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_all() -> list[dict]:
    """按更新时间倒序返回全部场景。"""
    with _LOCK:
        plans = list(_load().values())
    plans.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return plans


def get(plan_id: str) -> dict | None:
    with _LOCK:
        return _load().get(plan_id)


def create(name: str, desc: str, case_ids: list[str], env: str = "") -> dict:
    """创建场景；plan_id = plan_ + 8 位 hex。"""
    with _LOCK:
        data = _load()
        pid = f"plan_{uuid.uuid4().hex[:8]}"
        now = _now()
        plan: dict = {
            "id": pid,
            "name": name,
            "desc": desc,
            "case_ids": list(case_ids),
            "env": env,
            "created_at": now,
            "updated_at": now,
            "last_run_id": None,
            "last_run_at": None,
        }
        data[pid] = plan
        _save(data)
    return plan


_ALLOWED = {"name", "desc", "case_ids", "env", "last_run_id", "last_run_at"}


def update(plan_id: str, **fields) -> dict | None:
    """部分更新场景字段（允许改的见 _ALLOWED）；不存在时返回 None。"""
    with _LOCK:
        data = _load()
        plan = data.get(plan_id)
        if plan is None:
            return None
        for k, v in fields.items():
            if k in _ALLOWED:
                plan[k] = v
        plan["updated_at"] = _now()
        data[plan_id] = plan
        _save(data)
    return plan


def delete(plan_id: str) -> bool:
    with _LOCK:
        data = _load()
        if plan_id not in data:
            return False
        data.pop(plan_id)
        _save(data)
    return True
