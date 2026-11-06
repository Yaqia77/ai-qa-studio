"""公共脚本接口（设计文档 2.4）：脚本库 CRUD + 引用保护 + 步骤编辑器元数据。

三条约定：
- 定义合法性一律走 core.scripts.validate_script，与执行期同一套口径（存得下就跑得通）；
- 删除/改名前先扫用例的「前置脚本 / 后置脚本」列，被引用即拒绝，不留断链到执行期才炸；
- 全部写接口挂 require_perm("script:manage")，与平台其余写操作的安全边界一致。
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.shared import authz
from backend.api import script_store
from backend.api.schemas import ScriptCreate, ScriptOut, ScriptUpdate
from core import excel_loader, scripts as core_scripts
from core.scripts import ScriptError

router = APIRouter(prefix="/api/scripts", tags=["公共脚本"])

DEFAULT_CASES_FILE = "data/api_cases.xlsx"


def _used_map(file: str, strict: bool) -> dict[str, list[str]]:
    """扫描用例的脚本引用列，返回「脚本名 → 引用它的用例编号」。

    用例文件不存在（还没建用例）视为无引用；读取/解析失败时：
    strict=True（删除、改名）直接拒绝——宁可拦下也不放过断链；
    strict=False（列表展示）降级为空引用，页面仍可用。
    """
    from backend.api.routers import cases as cases_router  # 延迟导入，避开注册期循环引用

    try:
        path = cases_router._resolve_file(file)
    except HTTPException as e:
        if e.status_code == 404:
            return {}
        raise
    try:
        result = cases_router._load_cached(path)
    except Exception as e:  # noqa: BLE001（Excel 格式错等一律按「无法确认引用」处理）
        if strict:
            raise HTTPException(status_code=400, detail=f"用例文件不可读，无法确认脚本引用关系: {e}")
        return {}
    refs: dict[str, list[str]] = {}
    for c in result.cases:
        for name in list(c.pre_scripts or []) + list(c.post_scripts or []):
            bucket = refs.setdefault(name, [])
            if c.case_id not in bucket:
                bucket.append(c.case_id)
    return refs


def _out(item: dict, refs: dict[str, list[str]]) -> ScriptOut:
    return ScriptOut(name=item.get("name", ""), remark=str(item.get("remark") or ""),
                     steps=item.get("steps") or [], used_by=refs.get(item.get("name"), []),
                     updated_at=str(item.get("updated_at") or ""))


def _guard(fn, *args):
    try:
        return fn(*args)
    except ScriptError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[ScriptOut], summary="脚本列表（附引用它的用例编号）")
def list_scripts(file: str = Query(default=DEFAULT_CASES_FILE, description="统计引用用的用例文件")):
    refs = _used_map(file, strict=False)
    return [_out(item, refs) for item in script_store.list_scripts()]


@router.get("/meta", summary="步骤编辑器元数据（动作白名单/字段上限/取值路径说明）")
def script_meta(file: str = Query(default=DEFAULT_CASES_FILE)):
    """前端表单由此渲染，动作与字段只在 core/scripts.py 定义一处，两侧永不漂移。"""
    variables: list[str] = []
    from backend.api.routers import cases as cases_router  # 延迟导入

    try:
        variables = sorted(cases_router._load_cached(cases_router._resolve_file(file)).variables)
    except Exception:  # noqa: BLE001（变量候选是提示项，取不到不影响编辑器可用）
        pass
    return {
        "actions": [{"action": a, "desc": core_scripts.ACTION_DESC[a],
                     "fields": sorted(core_scripts.ACTION_FIELDS[a])} for a in core_scripts.ACTIONS],
        "step_limit": core_scripts.STEP_LIMIT,
        "sleep_limit": core_scripts.SLEEP_LIMIT,
        "name_max": 50,
        "name_forbidden": "".join(sorted(core_scripts.NAME_INVALID)),
        "methods": sorted(excel_loader.VALID_METHODS),
        "assert_ops": sorted(excel_loader.ASSERT_OPS),
        "ops_need_expected": sorted(excel_loader.OPS_NEED_EXPECTED),
        "path_options": [
            {"path": "status_code", "desc": "HTTP 状态码"},
            {"path": "$.", "desc": "响应体 JSONPath，如 $.code / $.data.id"},
            {"path": "response_text", "desc": "响应原文"},
            {"path": "var.", "desc": "变量池取值，如 var.token（脚本内 set_var/extract 写入）"},
        ],
        "variables": variables,
    }


@router.get("/{name}", response_model=ScriptOut, summary="脚本详情")
def get_script(name: str, file: str = Query(default=DEFAULT_CASES_FILE)):
    item = script_store.get(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"脚本不存在: {name}")
    return _out(item, _used_map(file, strict=False))


@router.post("", response_model=ScriptOut, summary="新建脚本", status_code=201)
def create_script(body: ScriptCreate, _: dict = Depends(authz.require_perm("script:manage"))):
    record = _guard(script_store.create, body.name, body.remark, body.steps)
    return _out(record, {})


@router.put("/{name}", response_model=ScriptOut, summary="更新脚本（被引用的脚本不可改名）")
def update_script(name: str, body: ScriptUpdate,
                  file: str = Query(default=DEFAULT_CASES_FILE),
                  _: dict = Depends(authz.require_perm("script:manage"))):
    if body.name is not None and body.name.strip() != name:
        # 改名等于把引用它的用例全部作废，必须拦下（用例侧改引用列后再改名即可）
        refs = _used_map(file, strict=True)
        users = refs.get(name) or []
        if users:
            raise HTTPException(status_code=409,
                                detail=f"脚本 {name} 被 {len(users)} 条用例引用（{', '.join(users[:10])}），"
                                       f"请先改完用例的脚本列再重命名")
    updated = _guard(script_store.update, name, body.name, body.remark, body.steps)
    return _out(updated, _used_map(file, strict=False))


@router.delete("/{name}", summary="删除脚本（被引用时拒绝）")
def delete_script(name: str, file: str = Query(default=DEFAULT_CASES_FILE),
                  _: dict = Depends(authz.require_perm("script:manage"))):
    users = _used_map(file, strict=True).get(name) or []
    if users:
        raise HTTPException(status_code=409,
                            detail=f"脚本 {name} 被 {len(users)} 条用例引用（{', '.join(users[:10])}），"
                                   f"请先清空这些用例的前置/后置脚本列")
    _guard(script_store.delete, name)
    return {"deleted": name}
