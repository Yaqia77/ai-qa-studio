"""Webhook 通知配置持久化：data/webhooks.json，原子写模式与 plans.json 一致。

每条记录代表一个通知渠道（企业微信/飞书/自定义），执行批次完成后按 events 过滤发送。
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_FILE = ROOT / "data" / "webhooks.json"
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
    """按更新时间倒序返回全部配置。"""
    with _LOCK:
        items = list(_load().values())
    items.sort(key=lambda w: w.get("updated_at", ""), reverse=True)
    return items


def get(webhook_id: str) -> dict | None:
    with _LOCK:
        return _load().get(webhook_id)


def create(name: str, url: str, type_: str = "custom",
           events: list[str] | None = None, enabled: bool = True) -> dict:
    """新建通知配置。"""
    with _LOCK:
        data = _load()
        wid = f"wh_{uuid.uuid4().hex[:8]}"
        now = _now()
        item: dict = {
            "id": wid,
            "name": name,
            "url": url,
            "type": type_,
            "events": events or ["run_fail", "run_error"],
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
        }
        data[wid] = item
        _save(data)
    return item


_ALLOWED = {"name", "url", "type", "events", "enabled"}


def update(webhook_id: str, **fields) -> dict | None:
    """部分更新（只改 _ALLOWED 中的字段）。"""
    with _LOCK:
        data = _load()
        item = data.get(webhook_id)
        if item is None:
            return None
        for k, v in fields.items():
            if k in _ALLOWED:
                item[k] = v
        item["updated_at"] = _now()
        data[webhook_id] = item
        _save(data)
    return item


def delete(webhook_id: str) -> bool:
    with _LOCK:
        data = _load()
        if webhook_id not in data:
            return False
        data.pop(webhook_id)
        _save(data)
    return True


def get_enabled_for_event(event: str) -> list[dict]:
    """返回所有 enabled 且 events 包含指定 event 的配置列表。"""
    with _LOCK:
        items = list(_load().values())
    return [
        w for w in items
        if w.get("enabled") and event in (w.get("events") or [])
    ]
