"""Cron 调度器：后台守护线程每 60 秒扫描到期任务并触发执行。

在 FastAPI lifespan 中启动，随服务进程存活。
"""
import logging
import threading
import time
from datetime import datetime

from backend.shared import scheduler_store

logger = logging.getLogger("interface-auto.scheduler")

_CHECK_INTERVAL = 60  # 秒
_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start():
    """启动调度线程（幂等）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cron-scheduler")
    _thread.start()
    logger.info("[scheduler] 调度线程已启动 (interval=%ds)", _CHECK_INTERVAL)


def stop():
    """优雅停止。"""
    _stop_event.set()


def _loop():
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception as e:
            logger.warning("[scheduler] tick 异常: %s", e)
        _stop_event.wait(_CHECK_INTERVAL)


def _tick():
    due = scheduler_store.get_due()
    for schedule in due:
        _fire(schedule)


def _fire(schedule: dict):
    """触发一次定时执行：优先 plan_id，其次 run_config。"""
    sid = schedule["id"]
    name = schedule.get("name", sid)
    logger.info("[scheduler] 触发定时任务: %s (%s)", name, sid)

    try:
        if schedule.get("plan_id"):
            # 走场景执行
            from backend.shared.routers.plans import run_plan
            from backend.shared import plan_store
            run_out = run_plan(schedule["plan_id"])
            run_id = run_out.run_id
        elif schedule.get("run_config"):
            # 直接按 run_config 发起
            from backend.shared.routers.runs import create_run
            from backend.shared.schemas import RunCreate
            cfg = schedule["run_config"]
            body = RunCreate(**cfg)
            run_out = create_run(body)
            run_id = run_out.run_id
        else:
            logger.warning("[scheduler] %s 无 plan_id 也无 run_config，跳过", sid)
            return

        # 更新 last_run + 计算 next_run_at
        from croniter import croniter
        cron = croniter(schedule["cron"], datetime.now())
        next_at = cron.get_next(datetime).isoformat(timespec="seconds")

        scheduler_store.update(sid, last_run_id=run_id, last_run_at=datetime.now().isoformat(timespec="seconds"),
                               next_run_at=next_at)
        logger.info("[scheduler] %s -> run_id=%s, next=%s", name, run_id, next_at)

    except Exception as e:
        logger.warning("[scheduler] %s 执行失败: %s", name, e)
        # 即使失败也推进 next_run_at，避免卡在同一时间点反复重试
        try:
            from croniter import croniter
            cron = croniter(schedule["cron"], datetime.now())
            next_at = cron.get_next(datetime).isoformat(timespec="seconds")
            scheduler_store.update(sid, next_run_at=next_at)
        except Exception:
            pass
