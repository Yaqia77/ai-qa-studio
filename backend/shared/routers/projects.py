"""用例项目接口：项目注册表 CRUD（建/改需 case:write，删需 case:delete）。

项目 = 一个 Excel 用例文件；本模块只管登记与脚手架文件生命周期，用例读写仍走 /api/cases
（前端把选中项目的 file 透传为各用例接口的 file 查询参数），因此对存量执行链路零侵入。
"""
from fastapi import APIRouter, Depends, HTTPException

from backend.shared import authz, project_store
from backend.shared.project_store import ProjectError
from backend.shared.schemas import ProjectCreate, ProjectOut, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["用例项目"])

_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409}


def _raise(e: ProjectError):
    raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


@router.get("", response_model=list[ProjectOut], summary="项目列表（含用例数）")
def list_projects():
    return project_store.list_projects()


@router.get("/{project_id}", response_model=ProjectOut, summary="项目详情")
def get_project(project_id: str):
    entry = project_store.get(project_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return entry


@router.post("", response_model=ProjectOut, summary="新建项目（脚手架空用例文件）", status_code=201)
def create_project(body: ProjectCreate, _: dict = Depends(authz.require_perm("case:write"))):
    try:
        return project_store.create(body.name, body.desc, body.kind)
    except ProjectError as e:
        _raise(e)


@router.put("/{project_id}", response_model=ProjectOut, summary="更新项目（名称/描述）")
def update_project(project_id: str, body: ProjectUpdate,
                   _: dict = Depends(authz.require_perm("case:write"))):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="未传任何需修改字段")
    try:
        return project_store.update(project_id, **fields)
    except ProjectError as e:
        _raise(e)


@router.delete("/{project_id}", summary="删除项目（连同其脚手架用例文件）")
def delete_project(project_id: str, _: dict = Depends(authz.require_perm("case:delete"))):
    try:
        return project_store.delete(project_id)
    except ProjectError as e:
        _raise(e)
