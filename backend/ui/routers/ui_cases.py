"""UI 用例查询与写回接口：读写复用 backend.ui_case_store（校验后原子落 JSON）。

与 /api/cases 分轨：接口用例走 Excel，UI 用例走 JSON。项目 kind=ui 时前端把项目的
file（.json）透传给本组接口；权限直接复用 case:write / case:delete，执行/报告链路复用。
"""
from pathlib import Path

import json
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from backend.shared import authz
from backend.ui import ui_case_store, ui_excel
from backend.ui.schemas import (UiCaseCreate, UiCaseMove, UiCaseOut, UiCasePage,
                             UiCaseUpdate, UiImportReq)
from backend.ui.ui_case_store import UiCaseError

ROOT = Path(__file__).resolve().parents[3]  # 项目根（backend/ui/routers/ 上溯三级）

router = APIRouter(prefix="/api/ui-cases", tags=["UI 用例"])

_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


def _resolve_file(file: str) -> Path:
    """把相对路径解析为项目根下的 UI 用例 JSON 文件；非 .json 直接拒绝（防误写 Excel）。"""
    p = Path(file)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"UI 用例文件不存在: {file}")
    if p.is_dir() or p.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail="UI 用例接口只接受 .json 项目文件，接口用例请走 /api/cases")
    return p


def _raise(e: UiCaseError):
    raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


def _to_out(case: dict) -> UiCaseOut:
    """store 清洗后的 dict → 对外模型（steps/extract 元素已是 dict，直接喂 pydantic）。"""
    return UiCaseOut(**case)


@router.get("", response_model=UiCasePage, summary="UI 用例列表（支持筛选）")
def list_cases(
    file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
    module: str | None = Query(default=None, description="模块名，包含匹配"),
    priority: str | None = Query(default=None, description="优先级，逗号分隔，如 P0,P1"),
    keyword: str | None = Query(default=None, description="编号/名称/描述 关键字"),
    run: bool | None = Query(default=None, description="是否启用"),
):
    path = _resolve_file(file)
    try:
        cases = ui_case_store.list_cases(path, module, priority, keyword, run)
    except UiCaseError as e:
        _raise(e)
    return UiCasePage(total=len(cases), items=[_to_out(c) for c in cases])


@router.get("/meta", summary="编辑用元数据（优先级/浏览器/模块/动作字典）")
def case_meta(file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）")):
    path = _resolve_file(file)
    try:
        return ui_case_store.meta(path)
    except UiCaseError as e:
        _raise(e)


@router.get("/variables", summary="全局变量名列表（值掩码，供步骤取值插入变量参考）")
def list_variables(file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）")):
    from core import excel_loader
    path = _resolve_file(file)
    try:
        cases = ui_case_store.load_cases(path)
    except UiCaseError as e:
        _raise(e)
    # UI 项目自身无全局变量表，变量来自环境配置 + 凭证 + 本批用例 extract 出的变量名
    extracted = sorted({e.get("var") for c in cases for e in (c.get("extract") or []) if e.get("var")})
    return {
        "excel_variables": extracted,
        "secret_variables": sorted(excel_loader.load_secret_vars(ROOT / "data" / "secret_vars.xlsx")),
    }


@router.get("/template", summary="下载 UI 用例批量导入模板（format=json|excel）")
def download_template(fmt: str = Query("json", alias="format", pattern="^(json|excel)$")):
    """模板与 /import 接收结构同构：cases 数组，每条字段与单条新增接口一致。

    format=excel 返回「一步一行 + 用例编号分组」的 .xlsx 模板（中文动作别名）；
    默认 json 保持旧行为（内嵌字段说明与两个示例）。
    """
    if fmt == "excel":
        return Response(content=ui_excel.build_template_bytes(),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="ui_cases_template.xlsx"'})
    tpl = {
        "_usage": [
            "UI 用例批量导入模板：cases 为数组，每条字段与页面「新增 UI 用例」同构。",
            "必填：case_id（项目内唯一）、steps（每步 {action, locator?, value?}）。",
            "可选：name/module/desc/priority(P0~P5,默认P2)/run(默认true)/browser(chromium|firefox|webkit)/"
            "headless(默认true)/retry(0~3)/extract([{var,source}])/session(登录态 id，不用传 null)。",
            "动作白名单：goto/click/fill/press/select/check/hover/wait_for/screenshot/"
            "expect_visible/expect_text/expect_url；定位器用 css= 前缀，select 按选项文本匹配。",
            "步骤取值可引用 {{变量名}}（环境/凭证/本条 extract 提取的变量）。",
            "导入规则：逐条全量校验，编号重复跳过，字段非法逐条报因不阻断其他条；单次上限 64 条。",
        ],
        "cases": [
            {
                "case_id": "demo_basic_001",
                "name": "登录页打开与标题断言",
                "module": "登录",
                "desc": "示例一：基础链路（打开→断言可见/文本）",
                "priority": "P1",
                "run": True,
                "browser": "chromium",
                "headless": True,
                "retry": 0,
                "session": None,
                "steps": [
                    {"action": "goto", "value": "https://example.com/login"},
                    {"action": "expect_visible", "locator": "css=#title"},
                    {"action": "expect_text", "locator": "css=#title", "value": "登录"},
                ],
            },
            {
                "case_id": "demo_login_002",
                "name": "登录并提取订单号",
                "module": "登录",
                "desc": "示例二：表单交互 + 变量提取 + 重试兜底 + 截图留证",
                "priority": "P1",
                "run": True,
                "browser": "chromium",
                "headless": True,
                "retry": 2,
                "session": None,
                "steps": [
                    {"action": "goto", "value": "https://example.com/login"},
                    {"action": "fill", "locator": "css=#username", "value": "{{username}}"},
                    {"action": "fill", "locator": "css=#password", "value": "{{password}}"},
                    {"action": "click", "locator": "css=#submit"},
                    {"action": "wait_for", "locator": "css=.order-id"},
                    {"action": "screenshot"},
                ],
                "extract": [{"var": "order_id", "source": "text|css=.order-id"}],
            },
        ],
    }
    return Response(content=json.dumps(tpl, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ui_cases_template.json"'})


@router.get("/export", summary="导出项目 UI 用例（format=excel|json，与导入同构可回导，按筛选条件）")
def export_cases(
    file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
    fmt: str = Query("excel", alias="format", pattern="^(excel|json)$"),
    module: str | None = Query(default=None, description="模块名，包含匹配"),
    priority: str | None = Query(default=None, description="优先级，逗号分隔"),
    keyword: str | None = Query(default=None, description="编号/名称/描述 关键字"),
    run: bool | None = Query(default=None, description="是否启用"),
):
    """导出当前筛选命中的全部用例（不传条件即全量）；编辑后可直接回导，同编号按重复跳过。

    excel=「一步一行」xlsx（同模板结构）；json={"cases":[...]}（与 /import 接收结构同构，无损）。
    """
    path = _resolve_file(file)
    try:
        cases = ui_case_store.list_cases(path, module, priority, keyword, run)
    except UiCaseError as e:
        _raise(e)
    if fmt == "json":
        content = json.dumps({"cases": cases}, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(content=content, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="ui_cases_export.json"'})
    return Response(content=ui_excel.build_export_bytes(cases),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="ui_cases_export.xlsx"'})


@router.post("/import", summary="批量导入 UI 用例（逐条校验；编号重复跳过，非法逐条报因）")
def import_cases(body: UiImportReq,
                 file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
                 _: dict = Depends(authz.require_perm("case:write"))):
    path = _resolve_file(file)
    if not body.cases:
        raise HTTPException(status_code=400, detail="cases 为空数组，没有可导入的用例")
    if len(body.cases) > 64:
        raise HTTPException(status_code=400, detail="单次导入上限 64 条，请分批导入")
    imported: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for i, c in enumerate(body.cases, 1):
        if not isinstance(c, dict):
            errors.append(f"第 {i} 项：必须是 JSON 对象")
            continue
        cid = str(c.get("case_id") or f"第 {i} 项")
        try:
            ui_case_store.create_case(path, c)
            imported.append(cid)
        except UiCaseError as e:
            if e.kind == "conflict":
                skipped.append(cid)
            else:
                errors.append(f"{cid}: {e}")
    return {"imported": len(imported), "imported_ids": imported,
            "skipped": skipped, "errors": errors}


@router.post("/preview", summary="上传 UI 步骤 Excel 解析预览（不落盘，确认后走 /import）")
async def preview_excel(
    file: UploadFile = File(..., description="UI 步骤 Excel（由 /template?format=excel 下载填写）"),
    project: str = Query(..., description="目标 UI 项目的用例 JSON 路径（相对项目根，用于查重）"),
    _: dict = Depends(authz.require_perm("case:write")),
):
    path = _resolve_file(project)
    name = file.filename or ""
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="只接受 .xlsx 文件（需先在弹窗下载 Excel 模板填写）")
    payload = await file.read()
    if len(payload) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（限 5MB），请分批导入")
    # 落临时文件供 openpyxl 读取（read_only），用完即删
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="ui_preview_"))
    tmp = tmp_dir / "upload.xlsx"
    try:
        tmp.write_bytes(payload)
        existing = {str(c.get("case_id") or "") for c in ui_case_store.load_cases(path)}
        try:
            resp = ui_excel.parse_workbook(tmp, existing_ids=existing)
        except Exception as e:  # noqa: BLE001 解析失败回 400 带原因
            raise HTTPException(status_code=400, detail=f"Excel 解析失败：{type(e).__name__}: {e}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
    return resp


@router.get("/{case_id}", response_model=UiCaseOut, summary="UI 用例详情")
def get_case(case_id: str, file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）")):
    path = _resolve_file(file)
    try:
        case = ui_case_store.get_case(path, case_id)
    except UiCaseError as e:
        _raise(e)
    if case is None:
        raise HTTPException(status_code=404, detail=f"用例不存在: {case_id}")
    return _to_out(case)


@router.post("", response_model=UiCaseOut, summary="新增 UI 用例（追加到 JSON，校验通过才落盘）", status_code=201)
def create_case(body: UiCaseCreate, file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:write"))):
    path = _resolve_file(file)
    try:
        case = ui_case_store.create_case(path, body.model_dump())
    except UiCaseError as e:
        _raise(e)
    return _to_out(case)


@router.put("/{case_id}", response_model=UiCaseOut, summary="更新 UI 用例（写回 JSON，校验通过才落盘）")
def update_case(case_id: str, body: UiCaseUpdate,
                file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:write"))):
    path = _resolve_file(file)
    changes = body.model_dump(exclude_unset=True)
    try:
        case = ui_case_store.update_case(path, case_id, changes)
    except UiCaseError as e:
        _raise(e)
    return _to_out(case)


@router.delete("/{case_id}", summary="删除 UI 用例（校验通过才落盘）")
def delete_case(case_id: str, file: str = Query(..., description="UI 项目的用例 JSON 路径（相对项目根）"),
                _: dict = Depends(authz.require_perm("case:delete"))):
    path = _resolve_file(file)
    try:
        ui_case_store.delete_case(path, case_id)
    except UiCaseError as e:
        _raise(e)
    return {"deleted": case_id, "file": path.name}


@router.post("/{case_id}/move", summary="迁移 UI 用例到另一个 UI 项目（目标查重→写入目标→源删除）")
def move_case(case_id: str, body: UiCaseMove,
              file: str = Query(..., description="源 UI 项目的用例 JSON 路径（相对项目根）"),
              _: dict = Depends(authz.require_perm("case:write"))):
    src = _resolve_file(file)
    dst = _resolve_file(body.to_file)
    try:
        moved = ui_case_store.move_case(src, dst, case_id, body.new_module)
    except UiCaseError as e:
        _raise(e)
    return {
        "moved": case_id,
        "from_file": str(src.relative_to(ROOT)).replace("\\", "/"),
        "to_file": str(dst.relative_to(ROOT)).replace("\\", "/"),
        "case": _to_out(moved).model_dump(),
    }
