# -*- coding: utf-8 -*-
"""App 用例查询与写回接口：读写复用 backend.app.app_case_store（校验后原子落 JSON），当前为预留位（脚手架）。

与 /api/cases、/api/ui-cases 分轨：接口走 Excel、UI 走 .json、App 走 .apjson。项目 kind=app 时前端把
项目的 file（.apjson）透传给本组接口；权限直接复用 case:write / case:delete，执行/报告链路复用。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app import app_case_store
from backend.app.schemas import (AppCaseCreate, AppCaseMove, AppCaseOut,
                                 AppCasePage, AppCaseUpdate)
from backend.app.app_case_store import AppCaseError
from backend.shared import authz

ROOT = Path(__file__).resolve().parents[3]  # 项目根（backend/app/routers/ 上溯三级）

router = APIRouter(prefix="/api/app-cases", tags=["App 用例"])

_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


def _resolve_file(file: str) -> Path:
    """把相对路径解析为项目根下的 App 用例 JSON 文件；非 .apjson 直接拒绝（防误写他域文件）。"""
    p = Path(file)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"App 用例文件不存在: {file}")
    if p.is_dir() or p.suffix.lower() != ".apjson":
        raise HTTPException(status_code=400, detail="App 用例接口只接受 .apjson 项目文件；接口用例走 /api/cases，UI 用例走 /api/ui-cases")
    return p


def _raise(e: AppCaseError):
    raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


def _to_out(case: dict) -> AppCaseOut:
    return AppCaseOut(**case)


@router.get("", response_model=AppCasePage, summary="App 用例列表（支持筛选）")
def list_cases(
    file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）"),
    module: str | None = Query(default=None, description="模块名，包含匹配"),
    priority: str | None = Query(default=None, description="优先级，逗号分隔，如 P0,P1"),
    keyword: str | None = Query(default=None, description="编号/名称/描述 关键字"),
    run: bool | None = Query(default=None, description="是否启用"),
):
    path = _resolve_file(file)
    try:
        cases = app_case_store.list_cases(path, module, priority, keyword, run)
    except AppCaseError as e:
        _raise(e)
    return AppCasePage(total=len(cases), items=[_to_out(c) for c in cases])


@router.get("/meta", summary="编辑用元数据（优先级/平台/模块/动作字典）")
def case_meta(file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）")):
    path = _resolve_file(file)
    try:
        return app_case_store.meta(path)
    except AppCaseError as e:
        _raise(e)


@router.get("/variables", summary="全局变量名列表（值掩码，供步骤取值插入变量参考）")
def list_variables(file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）")):
    from core import excel_loader
    path = _resolve_file(file)
    try:
        cases = app_case_store.load_cases(path)
    except AppCaseError as e:
        _raise(e)
    extracted = sorted({e.get("var") for c in cases for e in (c.get("extract") or []) if e.get("var")})
    return {
        "excel_variables": extracted,
        "secret_variables": sorted(excel_loader.load_secret_vars(ROOT / "data" / "secret_vars.xlsx")),
    }


@router.get("/{case_id}", response_model=AppCaseOut, summary="App 用例详情")
def get_case(case_id: str, file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）")):
    path = _resolve_file(file)
    try:
        case = app_case_store.get_case(path, case_id)
    except AppCaseError as e:
        _raise(e)
    if case is None:
        raise HTTPException(status_code=404, detail=f"用例不存在: {case_id}")
    return _to_out(case)


@router.post("", response_model=AppCaseOut, summary="新增 App 用例（追加到 JSON，校验通过才落盘）", status_code=201)
def create_case(body: AppCaseCreate, file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:write"))):
    path = _resolve_file(file)
    try:
        case = app_case_store.create_case(path, body.model_dump())
    except AppCaseError as e:
        _raise(e)
    return _to_out(case)


@router.put("/{case_id}", response_model=AppCaseOut, summary="更新 App 用例（写回 JSON，校验通过才落盘）")
def update_case(case_id: str, body: AppCaseUpdate,
                file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:write"))):
    path = _resolve_file(file)
    changes = body.model_dump(exclude_unset=True)
    try:
        case = app_case_store.update_case(path, case_id, changes)
    except AppCaseError as e:
        _raise(e)
    return _to_out(case)


@router.delete("/{case_id}", summary="删除 App 用例（校验通过才落盘）")
def delete_case(case_id: str, file: str = Query(..., description="App 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:delete"))):
    path = _resolve_file(file)
    try:
        app_case_store.delete_case(path, case_id)
    except AppCaseError as e:
        _raise(e)
    return {"deleted": case_id, "file": path.name}


@router.post("/{case_id}/move", summary="迁移 App 用例到另一个 App 项目（目标查重→写入目标→源删除）")
def move_case(case_id: str, body: AppCaseMove,
              file: str = Query(..., description="源 App 项目的用例 JSON 路径（相对项目根）"),
              _: dict = Depends(authz.require_perm("case:write"))):
    src = _resolve_file(file)
    dst = _resolve_file(body.to_file)
    try:
        moved = app_case_store.move_case(src, dst, case_id, body.new_module)
    except AppCaseError as e:
        _raise(e)
    return {
        "moved": case_id,
        "from_file": str(src.relative_to(ROOT)).replace("\\", "/"),
        "to_file": str(dst.relative_to(ROOT)).replace("\\", "/"),
        "case": _to_out(moved).model_dump(),
    }
