"""UI 登录态会话接口：读写复用 backend.ui.ui_session_store，采集编排走 session_collect。

「storage_state 复用」的 REST 层。
安全边界：
- 任何接口都不回传 storage_state 实体内容，只回传注册表元数据 + 派生状态（valid/expired/not_collected/failed）；
- 写操作（新建/更新/删除/采集）需 session:manage，查看列表/状态仅需登录态；
- 被 UI 用例引用的会话禁止删除 / 改 env（引用保护，返回 409 并列出引用锚点），口径同接口库/脚本库。
错误口径 UiSessionError.kind ∈ invalid/not_found/conflict/locked → 映射 400/404/409/409。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from backend.shared import authz
from backend.ui import session_collect, ui_session_store as store
from backend.ui.schemas import (UiSessionCreate, UiSessionOut, UiSessionUpdate)
from backend.ui.ui_session_store import UiSessionError

ROOT = Path(__file__).resolve().parents[3]  # 项目根（backend/ui/routers/ 上溯三级）

router = APIRouter(prefix="/api/ui-sessions", tags=["UI 登录态"])

_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


def _raise(e: UiSessionError):
    raise HTTPException(status_code=_ERROR_STATUS.get(e.kind, 400), detail=str(e))


def _out(session: dict) -> dict:
    """注册表条目 → 对外视图（附带派生状态，绝不含实体内容）。"""
    st = store.status_of(session)
    return {**session, "status": st["status"], "remaining_seconds": st["remaining_seconds"]}


def _referencing_cases(session_id: str) -> list[str]:
    """扫描所有 kind=ui 项目 JSON，返回引用该会话的用例锚点（<file>#<case_id>），用于引用保护。"""
    from backend.shared import project_store
    from backend.ui import ui_case_store

    hits: list[str] = []
    for p in project_store.list_projects():
        if p.get("kind") != "ui":
            continue
        rel = p.get("file") or ""
        path = Path(rel)
        if not path.is_absolute():
            path = ROOT / path
        try:
            cases = ui_case_store.load_cases(path)
        except Exception:  # noqa: BLE001 单个项目读失败不影响整体扫描
            continue
        for c in cases:
            if c.get("session") == session_id:
                hits.append(f"{rel}#{c.get('case_id', '')}")
    return hits


def _require_not_referenced(session_id: str, action: str) -> None:
    refs = _referencing_cases(session_id)
    if refs:
        raise HTTPException(
            status_code=409,
            detail=f"会话 {session_id} 正被 {len(refs)} 条 UI 用例引用，无法{action}；引用锚点: {', '.join(refs[:20])}",
        )


@router.get("", summary="登录态会话列表（含派生状态，不含实体内容）")
def list_sessions():
    return [_out(s) for s in store.list_sessions(store.SESSIONS_FILE)]


@router.get("/meta", summary="新建/编辑会话的下拉候选（环境/浏览器/UI 项目及其用例）")
def session_meta():
    from backend.shared import project_store
    from backend.ui import ui_case_store

    ui_projects = []
    for p in project_store.list_projects():
        if p.get("kind") != "ui":
            continue
        rel = p.get("file") or ""
        path = ROOT / rel if not Path(rel).is_absolute() else Path(rel)
        try:
            cases = ui_case_store.load_cases(path)
        except Exception:  # noqa: BLE001
            cases = []
        ui_projects.append({
            "file": rel,
            "name": p.get("name", rel),
            "cases": [{"case_id": c.get("case_id", ""), "name": c.get("name", "")} for c in cases],
        })
    return {
        "envs": store.env_names_from_config(),
        "browsers": sorted(store.VALID_BROWSERS),
        "ui_projects": ui_projects,
    }


@router.get("/{session_id}", response_model=UiSessionOut, summary="会话详情（含状态，不含实体内容）")
def get_session(session_id: str):
    try:
        session = store.get_session(store.SESSIONS_FILE, session_id)
    except UiSessionError as e:
        _raise(e)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return session


@router.get("/{session_id}/status", summary="单查会话状态（供页面徽标按需刷新）")
def session_status(session_id: str):
    try:
        session = store.get_session(store.SESSIONS_FILE, session_id)
    except UiSessionError as e:
        _raise(e)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return store.status_of(session)


@router.post("", summary="新建登录态会话（校验后落注册表，captured 状态初始为未采集）", status_code=201)
def create_session(body: UiSessionCreate, _: dict = Depends(authz.require_perm("session:manage"))):
    try:
        return _out(store.create_session(store.SESSIONS_FILE, body.model_dump()))
    except UiSessionError as e:
        _raise(e)


@router.put("/{session_id}", summary="更新会话（改 env/采集定义会作废旧登录态；被引用时禁改 env）")
def update_session(session_id: str, body: UiSessionUpdate,
                   _: dict = Depends(authz.require_perm("session:manage"))):
    changes = body.model_dump(exclude_unset=True)
    try:
        existing = store.get_session(store.SESSIONS_FILE, session_id)
        if existing is None:
            raise UiSessionError("not_found", f"会话不存在: {session_id}")
        # 引用保护：改动 env 会使旧登录态作废且 cookie 域名归属变化，被引用时禁止
        new_env = changes.get("env")
        if new_env is not None and str(new_env) != str(existing.get("env")):
            _require_not_referenced(session_id, "改绑环境")
        return _out(store.update_session(store.SESSIONS_FILE, session_id, changes))
    except UiSessionError as e:
        _raise(e)


@router.delete("/{session_id}", summary="删除会话（连带清理实体文件；被引用时禁止删除）")
def delete_session(session_id: str, _: dict = Depends(authz.require_perm("session:manage"))):
    _require_not_referenced(session_id, "删除")
    try:
        return store.delete_session(store.SESSIONS_FILE, session_id)
    except UiSessionError as e:
        _raise(e)


@router.post("/{session_id}/collect", summary="立即采集/刷新登录态（同步跑采集用例并导出 storage_state）")
def collect_session(session_id: str, _: dict = Depends(authz.require_perm("session:manage"))):
    """单跑一条采集用例：成功登录后把 storage_state 落实体文件，同步返回结果（不走批次子进程）。

    需 Playwright 内核就绪；浏览器/登录失败返回 ok=false + error（并留 last_error），不抛 500。
    """
    try:
        session = store.get_session(store.SESSIONS_FILE, session_id)
    except UiSessionError as e:
        _raise(e)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

    ok, err = session_collect.collect(session)
    fresh = store.get_session(store.SESSIONS_FILE, session_id) or session
    st = store.status_of(fresh)
    return {"ok": ok, "error": err, "id": session_id,
            "status": st["status"], "captured_at": st["captured_at"],
            "remaining_seconds": st["remaining_seconds"]}
