"""定时任务（Schedule）数据持久化：data/schedules.json。

每条记录代表一个 Cron 定时执行计划，关联一个测试场景或直接指定执行参数。
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_FILE = ROOT / "data" / "schedules.json"
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
    with _LOCK:
        items = list(_load().values())
    items.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return items


def get(schedule_id: str) -> dict | None:
    with _LOCK:
        return _load().get(schedule_id)


def create(name: str, cron: str, plan_id: str = "",
           run_config: dict | None = None, enabled: bool = True) -> dict:
    """新建定时任务。run_config 与 plan_id 二选一。"""
    with _LOCK:
        data = _load()
        sid = f"sch_{uuid.uuid4().hex[:8]}"
        now = _now()
        item: dict = {
            "id": sid,
            "name": name,
            "cron": cron,
            "plan_id": plan_id,
            "run_config": run_config or {},
            "enabled": enabled,
            "last_run_id": None,
            "last_run_at": None,
            "next_run_at": _compute_next(cron),
            "created_at": now,
            "updated_at": now,
        }
        data[sid] = item
        _save(data)
    return item


_ALLOWED = {"name", "cron", "plan_id", "run_config", "enabled",
            "last_run_id", "last_run_at", "next_run_at"}


def update(schedule_id: str, **fields) -> dict | None:
    with _LOCK:
        data = _load()
        item = data.get(schedule_id)
        if item is None:
            return None
        for k, v in fields.items():
            if k in _ALLOWED:
                item[k] = v
        # cron 变更时重算 next_run_at
        if "cron" in fields:
            item["next_run_at"] = _compute_next(item["cron"])
        item["updated_at"] = _now()
        data[schedule_id] = item
        _save(data)
    return item


def delete(schedule_id: str) -> bool:
    with _LOCK:
        data = _load()
        if schedule_id not in data:
            return False
        data.pop(schedule_id)
        _save(data)
    return True


def get_due() -> list[dict]:
    """返回当前时间已到 next_run_at 的启用任务列表。"""
    now_str = _now()
    with _LOCK:
        items = list(_load().values())
    return [
        s for s in items
        if s.get("enabled") and s.get("next_run_at") and s["next_run_at"] <= now_str
    ]


def _compute_next(cron_expr: str) -> str | None:
    """根据 cron 表达式计算下一次执行时间。"""
    try:
        from croniter import croniter
        cron = croniter(cron_expr, datetime.now())
        return cron.get_next(datetime).isoformat(timespec="seconds")
    except (ValueError, KeyError):
        return None
