"""用例依赖关系管理接口（写入权限同用例编辑：case:write）。"""
from fastapi import APIRouter, Depends, HTTPException

from backend.shared import authz, dependency_store
from backend.shared.schemas import DepsSetRequest

router = APIRouter(prefix="/api/dependencies", tags=["用例依赖"])


@router.get("", summary="获取全部依赖映射")
def get_all():
    return dependency_store.get_all()


@router.get("/{case_id}", summary="获取某用例的依赖列表")
def get_deps(case_id: str):
    deps = dependency_store.get_deps(case_id)
    return {"case_id": case_id, "dependencies": deps}


@router.put("/{case_id}", summary="设置用例依赖（覆盖式）")
def set_deps(case_id: str, body: DepsSetRequest, _: dict = Depends(authz.require_perm("case:write"))):
    # 自依赖检测
    if case_id in body.dependencies:
        raise HTTPException(status_code=400, detail="不能依赖自身")
    # 环检测
    for dep in body.dependencies:
        if dependency_store.has_cycle(case_id, dep):
            raise HTTPException(status_code=400, detail=f"添加 {case_id} -> {dep} 会形成循环依赖")
    dependency_store.set_deps(case_id, body.dependencies)
    return {"case_id": case_id, "dependencies": body.dependencies}


@router.post("/{case_id}/add", summary="添加单条依赖")
def add_dep(case_id: str, dep_id: str, _: dict = Depends(authz.require_perm("case:write"))):
    if case_id == dep_id:
        raise HTTPException(status_code=400, detail="不能依赖自身")
    if dependency_store.has_cycle(case_id, dep_id):
        raise HTTPException(status_code=400, detail=f"添加 {case_id} -> {dep_id} 会形成循环依赖")
    added = dependency_store.add_dep(case_id, dep_id)
    return {"added": added, "dependencies": dependency_store.get_deps(case_id)}


@router.delete("/{case_id}/remove", summary="移除单条依赖")
def remove_dep(case_id: str, dep_id: str, _: dict = Depends(authz.require_perm("case:write"))):
    removed = dependency_store.remove_dep(case_id, dep_id)
    return {"removed": removed, "dependencies": dependency_store.get_deps(case_id)}
