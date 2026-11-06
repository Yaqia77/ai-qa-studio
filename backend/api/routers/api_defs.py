# -*- coding: utf-8 -*-
"""接口定义管理接口（设计文档 2.2）：接口库 CRUD + Excel 聚合沉淀 + OpenAPI 导入 + 生成用例骨架。

四条约定：
- 定义合法性一律走 api_def_store.validate，断言口径复用 excel_loader.check_assert_rules，
  库里推荐的断言写进用例后 loader 必然认账；
- 引用保护按「接口名称 = 用例的接口名称列」建立：被同名用例引用的定义不可改名、不可删除，
  否则覆盖统计与断言建议会静默失联（与公共脚本库同一口径）；
- 库的写操作挂 require_perm("api:manage")；**生成骨架是往 Excel 写用例**，
  因此那一步要的是 case:write —— 权限跟着副作用走，不跟着页面归属走；
- 骨架默认 dry_run 只回预览，确认无误再落盘，避免一次误点污染真实用例文件。
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api import api_def_store, excel_writer, openapi_import
from backend.shared import authz
from backend.api.api_def_store import ApiDefError
from backend.api.schemas import ApiDefCreate, ApiDefOut, ApiDefUpdate, OpenApiImport, SkeletonCreate
from core import excel_loader

router = APIRouter(prefix="/api/defs", tags=["接口定义"])

DEFAULT_CASES_FILE = "data/api_cases.xlsx"
_WRITE_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


def _load_cases(file: str, strict: bool):
    """读取用例文件（引用统计与聚合/骨架都要它）。

    文件不存在（还没建用例）视为空列表；解析失败时：
    strict=True（聚合、生成骨架）直接拒绝——基于坏数据的定义比没有定义更害人；
    strict=False（列表展示覆盖情况）降级为空引用，页面照常可用。
    """
    from backend.api.routers import cases as cases_router  # 延迟导入，避开注册期循环引用

    try:
        path = cases_router._resolve_file(file)
    except HTTPException as e:
        if e.status_code == 404:
            return None
        raise
    try:
        return cases_router._load_cached(path)
    except Exception as e:  # noqa: BLE001（Excel 格式错等一律按「读不到」处理）
        if strict:
            raise HTTPException(status_code=400, detail=f"用例文件不可读: {e}")
        return None


def _coverage_map(file: str, strict: bool) -> dict[str, list]:
    """接口名 → 接口名称同名的用例编号（即该接口的用例覆盖清单）。"""
    result = _load_cases(file, strict)
    if result is None:
        return {}
    refs: dict[str, list] = {}
    for c in result.cases:
        bucket = refs.setdefault(str(c.api_name or "").strip(), [])
        if c.case_id not in bucket:
            bucket.append(c.case_id)
    return refs


def _out(item: dict, refs: dict[str, list]) -> ApiDefOut:
    return ApiDefOut(
        name=item.get("name", ""), module=str(item.get("module") or "未分类"),
        method=str(item.get("method") or ""), path=str(item.get("path") or ""),
        remark=str(item.get("remark") or ""), headers=item.get("headers"),
        params=item.get("params"), body=item.get("body"), extract=item.get("extract"),
        asserts=item.get("asserts"), source=str(item.get("source") or "manual"),
        used_by=refs.get(item.get("name"), []), updated_at=str(item.get("updated_at") or ""))


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ApiDefError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[ApiDefOut], summary="接口定义列表（附同名用例的覆盖清单）")
def list_defs(file: str = Query(default=DEFAULT_CASES_FILE, description="统计用例覆盖用的用例文件")):
    refs = _coverage_map(file, strict=False)
    return [_out(item, refs) for item in api_def_store.list_defs()]


@router.get("/meta", summary="接口编辑元数据（方法/断言操作符/推荐断言/导入限制）")
def defs_meta(file: str = Query(default=DEFAULT_CASES_FILE)):
    """前端表单与提示由此渲染，校验常量只在 api_def_store / excel_loader 定义一处，两侧不漂移。"""
    variables: list[str] = []
    result = _load_cases(file, strict=False)
    if result is not None:
        variables = sorted(result.variables)
    return {
        "methods": sorted(excel_loader.VALID_METHODS),
        "assert_ops": [{"op": o, "desc": "", "need_expected": o in excel_loader.OPS_NEED_EXPECTED}
                       for o in sorted(excel_loader.ASSERT_OPS)],
        "ops_need_expected": sorted(excel_loader.OPS_NEED_EXPECTED),
        "path_options": [
            {"path": "status_code", "desc": "HTTP 状态码"},
            {"path": "$.", "desc": "响应体 JSONPath，如 $.code / $.data.id"},
            {"path": "response_text", "desc": "响应原文"},
            {"path": "var.", "desc": "变量池取值，如 var.token（脚本或 extract 写入）"},
        ],
        "default_asserts": [["eq", "status_code", 200]],
        "modules": api_def_store.modules(),
        "name_max": api_def_store.NAME_MAX,
        "module_max": api_def_store.MODULE_MAX,
        "remark_max": api_def_store.REMARK_MAX,
        "assert_max": api_def_store.ASSERT_MAX,
        "sources": [{"value": k, "label": v} for k, v in api_def_store.SOURCE_LABELS.items()],
        "spec_limit_mb": openapi_import.SPEC_MAX_BYTES >> 20,
        "variables": variables,
        "def_count": len(api_def_store.list_defs()),
    }


@router.post("/aggregate", summary="从存量用例聚合出接口定义（接口库沉淀）")
def aggregate(file: str = Query(default=DEFAULT_CASES_FILE),
              overwrite: bool = Query(default=False, description="同名定义已存在时是否覆盖"),
              _: dict = Depends(authz.require_perm("api:manage"))):
    result = _load_cases(file, strict=True)
    if result is None:
        raise HTTPException(status_code=404, detail=f"用例文件不存在，无从聚合: {file}")
    # from_cases 不抛错：名字不合规的分组自己退回 rejected，不让一个脏名拖垮整批
    records, rejected = api_def_store.from_cases(result.cases)
    stats = _guard(api_def_store.upsert_many, records, overwrite)
    return {"file": file, "scanned_cases": len(result.cases), "groups": len(records),
            "rejected": rejected, **stats}


@router.post("/import/openapi", summary="导入 OpenAPI/Swagger 文档（JSON 或 YAML 原文）")
def import_openapi(body: OpenApiImport, _: dict = Depends(authz.require_perm("api:manage"))):
    defs, notes = _guard(openapi_import.parse_spec, body.text)
    stats = _guard(api_def_store.upsert_many, defs, body.overwrite)
    return {"parsed": len(defs), "skipped_notes": notes[:20], "note_count": len(notes), **stats}


@router.get("/{name}", response_model=ApiDefOut, summary="接口定义详情")
def get_def(name: str, file: str = Query(default=DEFAULT_CASES_FILE)):
    item = api_def_store.get(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"接口不存在: {name}")
    return _out(item, _coverage_map(file, strict=False))


@router.post("", response_model=ApiDefOut, summary="新建接口定义", status_code=201)
def create_def(body: ApiDefCreate, _: dict = Depends(authz.require_perm("api:manage"))):
    record = _guard(api_def_store.create, **body.model_dump())
    return _out(record, {})


@router.put("/{name}", response_model=ApiDefOut, summary="更新接口定义（被用例引用的接口不可改名）")
def update_def(name: str, body: ApiDefUpdate,
               file: str = Query(default=DEFAULT_CASES_FILE),
               _: dict = Depends(authz.require_perm("api:manage"))):
    if api_def_store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"接口不存在: {name}")
    patch = body.model_dump(exclude_unset=True)
    new_name = str(patch.get("name") or "").strip()
    if new_name and new_name != name:
        # 改名等于把同名用例的覆盖关系与断言建议一起掐断，必须先改用例的接口名称列
        users = _coverage_map(file, strict=True).get(name) or []
        if users:
            raise HTTPException(status_code=409,
                                detail=f"接口 {name} 被 {len(users)} 条用例同名引用（{', '.join(users[:10])}），"
                                       f"请先改完用例的接口名称列再重命名")
    updated = _guard(api_def_store.update, name, patch)
    return _out(updated, _coverage_map(file, strict=False))


@router.delete("/{name}", summary="删除接口定义（被同名用例引用时拒绝）")
def delete_def(name: str, file: str = Query(default=DEFAULT_CASES_FILE),
               _: dict = Depends(authz.require_perm("api:manage"))):
    if api_def_store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"接口不存在: {name}")
    users = _coverage_map(file, strict=True).get(name) or []
    if users:
        raise HTTPException(status_code=409,
                            detail=f"接口 {name} 被 {len(users)} 条用例同名引用（{', '.join(users[:10])}），"
                                   f"请先改这些用例的接口名称列，或保留该定义")
    _guard(api_def_store.delete, name)
    return {"deleted": name}


def _skeleton_row(d: dict, case_id: str, desc: str, priority: str, run: bool) -> dict:
    """定义 → 用例行（骨架只带「已知正确的部分」，业务参数留给用例作者填）。"""
    row = {
        "case_id": case_id,
        "api_name": d["name"],
        "module": str(d.get("module") or "未分类"),
        "case_desc": desc,
        "priority": priority,
        "run": run,
        "method": str(d.get("method") or "GET"),
        "url": str(d.get("path") or "/"),
        "asserts": d.get("asserts") or [["eq", "status_code", 200]],
    }
    for key in ("headers", "params", "extract", "body"):
        if d.get(key):
            row[key] = d[key]
    return row


@router.post("/{name}/skeleton", summary="由接口定义生成用例骨架（默认只预览，dry_run=false 才写 Excel）")
def gen_skeleton(name: str, body: SkeletonCreate,
                 file: str = Query(default=DEFAULT_CASES_FILE),
                 _: dict = Depends(authz.require_perm("case:write"))):
    d = api_def_store.get(name)
    if d is None:
        raise HTTPException(status_code=404, detail=f"接口不存在: {name}")
    case_ids = [str(c or "").strip() for c in (body.case_ids or []) if str(c or "").strip()]
    if not case_ids:
        raise HTTPException(status_code=400, detail="请至少提供一个用例编号")
    if len(case_ids) != len(set(case_ids)):
        raise HTTPException(status_code=400, detail="用例编号重复，请去掉重复项")

    desc = (body.case_desc or "").strip() or f"{name} 正常流程"
    rows = [_skeleton_row(d, cid, desc, body.priority, body.run) for cid in case_ids]
    if body.dry_run:
        return {"dry_run": True, "file": file, "cases": rows, "written": [], "errors": []}

    from backend.api.routers import cases as cases_router  # 延迟导入

    path = cases_router._resolve_file(file)
    if path.is_dir():
        raise HTTPException(status_code=400, detail="目录模式下无法确定写入目标，请指定具体 xlsx")
    written: list[str] = []
    errors: list[str] = []
    last_kind = "invalid"
    for row in rows:
        try:
            excel_writer.append_case(path, row)
            written.append(row["case_id"])
        except excel_writer.CaseWriteError as e:
            errors.append(f"{row['case_id']}: {e}")
            last_kind = e.kind
    if not written:
        # 一条都没写进去：按失败类别给状态码（编号重复 409、文件被占 409、字段非法 400）
        raise HTTPException(status_code=_WRITE_STATUS.get(last_kind, 400),
                            detail=errors[0] if errors else "未写入任何用例")
    return {"dry_run": False, "file": file, "cases": rows, "written": written, "errors": errors}
