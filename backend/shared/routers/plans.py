"""测试场景接口：CRUD + 一键按场景发起执行。"""
from fastapi import APIRouter, Depends, HTTPException

from backend.shared import authz, plan_store
from backend.shared.schemas import PlanCreate, PlanOut, PlanUpdate, RunCreate

router = APIRouter(prefix="/api/plans", tags=["测试场景"])


@router.get("", response_model=list[PlanOut], summary="场景列表（按更新时间倒序）")
def list_plans():
    return [PlanOut(**p) for p in plan_store.list_all()]


@router.post("", response_model=PlanOut, status_code=201, summary="新建场景")
def create_plan(body: PlanCreate, _: dict = Depends(authz.require_perm("plan:manage"))):
    if not body.case_ids:
        raise HTTPException(status_code=400, detail="场景内至少需要一条用例")
    plan = plan_store.create(
        name=body.name,
        desc=body.desc,
        case_ids=list(dict.fromkeys(body.case_ids)),  # 去重保序
        env=body.env,
    )
    return PlanOut(**plan)


@router.get("/{plan_id}", response_model=PlanOut, summary="场景详情")
def get_plan(plan_id: str):
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"场景不存在: {plan_id}")
    return PlanOut(**plan)


@router.put("/{plan_id}", response_model=PlanOut, summary="更新场景（名称/描述/用例范围/环境偏好）")
def update_plan(plan_id: str, body: PlanUpdate, _: dict = Depends(authz.require_perm("plan:manage"))):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="未传任何需要修改的字段")
    if "case_ids" in fields:
        fields["case_ids"] = list(dict.fromkeys(fields["case_ids"]))
    plan = plan_store.update(plan_id, **fields)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"场景不存在: {plan_id}")
    return PlanOut(**plan)


@router.delete("/{plan_id}", summary="删除场景")
def delete_plan(plan_id: str, _: dict = Depends(authz.require_perm("plan:manage"))):
    if not plan_store.delete(plan_id):
        raise HTTPException(status_code=404, detail=f"场景不存在: {plan_id}")
    return {"ok": True}


@router.post("/{plan_id}/run", summary="按场景发起执行（复用执行中心 create_run）", status_code=202)
def run_plan(plan_id: str, _: dict = Depends(authz.require_perm("run:create"))):
    """将场景的 case_ids 精确传入 create_run，同时记录 last_run_id/at 供卡片展示。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"场景不存在: {plan_id}")
    if not plan["case_ids"]:
        raise HTTPException(status_code=400, detail="场景内没有用例，无法执行")

    # 延迟导入，避免两个 router 模块在注册期发生循环引用
    from backend.shared.routers.runs import create_run as _create_run

    body = RunCreate(
        env=plan["env"] or None,
        cases=plan["case_ids"],
        file="data/api_cases.xlsx",
    )
    run_out = _create_run(body)
    plan_store.update(plan_id, last_run_id=run_out.run_id, last_run_at=run_out.started_at)
    return run_out
