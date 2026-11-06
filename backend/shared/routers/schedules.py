"""定时任务 CRUD + 手动触发 + Cron 预览（编辑需 schedule:manage）。"""
from datetime import datetime

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.shared import authz, scheduler_store
from backend.shared.schemas import ScheduleCreate, ScheduleOut, ScheduleUpdate

router = APIRouter(prefix="/api/schedules", tags=["定时任务"])


@router.get("", response_model=list[ScheduleOut], summary="任务列表")
def list_schedules():
    return scheduler_store.list_all()


@router.post("", response_model=ScheduleOut, summary="新建定时任务", status_code=201)
def create_schedule(body: ScheduleCreate, _: dict = Depends(authz.require_perm("schedule:manage"))):
    # 校验 cron 表达式
    try:
        croniter(body.cron)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Cron 表达式无效: {e}")
    if not body.plan_id and not body.run_config:
        raise HTTPException(status_code=400, detail="plan_id 和 run_config 至少填一个")
    item = scheduler_store.create(
        name=body.name,
        cron=body.cron,
        plan_id=body.plan_id,
        run_config=body.run_config,
        enabled=body.enabled,
    )
    return item


@router.get("/{schedule_id}", response_model=ScheduleOut, summary="详情")
def get_schedule(schedule_id: str):
    item = scheduler_store.get(schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return item


@router.put("/{schedule_id}", response_model=ScheduleOut, summary="更新")
def update_schedule(schedule_id: str, body: ScheduleUpdate,
                    _: dict = Depends(authz.require_perm("schedule:manage"))):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="未传任何需修改字段")
    # 校验 cron
    if "cron" in fields:
        try:
            croniter(fields["cron"])
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=f"Cron 表达式无效: {e}")
    item = scheduler_store.update(schedule_id, **fields)
    if item is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return item


@router.delete("/{schedule_id}", summary="删除")
def delete_schedule(schedule_id: str, _: dict = Depends(authz.require_perm("schedule:manage"))):
    if not scheduler_store.delete(schedule_id):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"ok": True}


@router.post("/{schedule_id}/run", summary="手动触发一次执行")
def run_schedule(schedule_id: str, _: dict = Depends(authz.require_perm("run:create"))):
    """立即执行一次，不改变 next_run_at。"""
    item = scheduler_store.get(schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")

    from backend.shared.notifier import send_run_summary  # ensure module loads

    if item.get("plan_id"):
        from backend.shared.routers.plans import run_plan
        run_out = run_plan(item["plan_id"])
    elif item.get("run_config"):
        from backend.shared.routers.runs import create_run
        from backend.shared.schemas import RunCreate
        run_out = create_run(RunCreate(**item["run_config"]))
    else:
        raise HTTPException(status_code=400, detail="任务未配置执行目标")

    scheduler_store.update(schedule_id, last_run_id=run_out.run_id,
                           last_run_at=datetime.now().isoformat(timespec="seconds"))
    return run_out


@router.get("/preview/next-runs", summary="Cron 预览：未来 N 次执行时间")
def preview_cron(cron: str = Query(..., description="Cron 表达式"), n: int = Query(default=5, le=20)):
    try:
        it = croniter(cron, datetime.now())
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Cron 表达式无效: {e}")
    runs = [it.get_next(datetime).isoformat(timespec="seconds") for _ in range(n)]
    return {"cron": cron, "next_runs": runs}
