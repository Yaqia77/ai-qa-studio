"""UI 登录态「采集 / 复用」编排：连接会话注册表（ui_session_store）与执行器（WebEngine/UiRunner）。

两条链路：采集落盘 + 复用解析（含过期就地重采）。
core 不依赖 backend，故 UiRunner 的 storage_state attach/collect 通过 provider/committer 回调注入；
本模块给出这两个回调的 backend 侧实现，以及「单条采集用例同步跑一遍」的编排，供路由与 conftest 共用
（单一事实来源，避免采集逻辑在两处漂移）。

三条主链路：
- collect(session)：跑采集用例 → 登录成功后导出 storage_state → 原子落实体文件 + 回写 captured_at；
  失败留 last_error。同步短任务，不走批次子进程。
- resolve_for_attach(case, registry)：normal 执行期把 case.session 解析成可加载路径；缺失给中文指引
  （绝不把「未登录」当「已登录」）；过期则进程内加锁就地重采一次。
- 未引用会话的存量用例 → (None, None)，行为与 v1 完全一致。
"""
from __future__ import annotations

import threading
from pathlib import Path

from loguru import logger

from backend.ui import ui_session_store as store
from backend.ui.ui_session_store import UiSessionError

ROOT = Path(__file__).resolve().parent.parent.parent


# ==================== 采集（产生登录态） ====================

def _build_ctx(env: str) -> tuple[object, str]:
    """按会话绑定的环境组一个 Context（复刻 conftest：base_url + secret_vars），供采集执行器读变量。"""
    from core.context import Context, get_env_config, load_config
    from core.excel_loader import load_secret_vars

    config = load_config()
    env_cfg = get_env_config(config, env or None)
    base_url = env_cfg.get("base_url", "")
    yaml_vars = {"base_url": base_url}
    secret_path = Path(config.get("secret_vars_file", "data/secret_vars.xlsx"))
    if not secret_path.is_absolute():
        secret_path = ROOT / secret_path
    merged: dict = {}
    try:
        merged.update(load_secret_vars(secret_path))
    except Exception as e:  # noqa: BLE001 凭证缺失不阻断采集（采集用例可能无需变量）
        logger.warning("采集读取凭证变量失败（忽略）: {}", e)
    return Context(yaml_vars, merged), base_url


def _read_capture_case(session: dict) -> dict:
    """在 S.file（UI 项目 JSON）里定位 capture_case 那条用例；找不到抛 not_found。"""
    from backend.ui import ui_case_store

    path = Path(session.get("file") or "")
    if not path.is_absolute():
        path = ROOT / path
    cid = session.get("capture_case", "")
    case = ui_case_store.get_case(path, cid)
    if case is None:
        raise UiSessionError("not_found", f"采集用例不存在: {cid}（文件 {path.name}）")
    return case


def _commit_storage_state(session: dict, tmp_path: str) -> None:
    """把 UiRunner 导出的临时 storage_state 原子落到实体文件，并回写 captured_at（清 last_error）。"""
    ep = store.entity_path(session["id"], session.get("env", ""))
    ep.parent.mkdir(parents=True, exist_ok=True)
    Path(tmp_path).replace(ep)  # 同卷原子替换（临时文件在 reports 目录，实体在 data/ui_sessions）
    store.mark_captured(store.SESSIONS_FILE, session["id"])
    logger.info("登录态已采集: {} -> {}", session["id"], ep.name)


def collect(session: dict) -> tuple[bool, str | None]:
    """同步跑一条采集用例（mode=collect），成功登录后导出 storage_state 落实体文件。

    返回 (ok, error)；任何失败均落 last_error 而不抛出（供路由 / 就地重采安全消费）。
    采集用例从「未登录」走到「已登录」：剥离其自身 session，并按会话定义统一 browser/headless 采集。
    """
    sid = session.get("id", "")
    try:
        capture = _read_capture_case(session)
    except UiSessionError as e:
        store.mark_failed(store.SESSIONS_FILE, sid, str(e))
        return False, str(e)

    capture = {
        **capture,
        "session": None,  # 采集不 attach 任何会话
        "browser": session.get("browser") or capture.get("browser") or "chromium",
        "headless": bool(session.get("headless", True)),
    }

    try:
        ctx, base_url = _build_ctx(session.get("env", ""))
    except Exception as e:  # noqa: BLE001 环境/配置不可读
        err = f"采集环境不可用: {type(e).__name__}: {e}"
        store.mark_failed(store.SESSIONS_FILE, sid, err)
        return False, err

    from core.engines import get_engine

    def committer(_case: dict, tmp: str) -> None:
        _commit_storage_state(session, tmp)

    try:
        result = get_engine("web").execute(
            capture, ctx, base_url=base_url, mode="collect", session_committer=committer,
        )
    except Exception as e:  # noqa: BLE001 采集执行异常兜底为失败留痕
        err = f"采集执行异常: {type(e).__name__}: {e}"
        store.mark_failed(store.SESSIONS_FILE, sid, err)
        return False, err

    if result.ok and result.collected is not None:
        return True, None  # committer 已落盘 + mark_captured

    err = (result.error or "采集用例执行失败（未完成登录，未产生登录态）")[:400]
    store.mark_failed(store.SESSIONS_FILE, sid, err)
    return False, err


# ==================== 复用（加载登录态 + 过期就地重采） ====================

def _renew_in_process(session: dict, registry: dict, ep: Path) -> tuple[bool, str | None]:
    """进程内加锁重采一次：同进程同一会话只重采一次，避免一批 100 条用例触发 100 次重登。"""
    key = f"{session['id']}__{session.get('env', '')}"
    locks = registry.setdefault("_locks", {})
    lock = locks.setdefault(key, threading.Lock())
    renewed = registry.setdefault("_renewed", {})
    with lock:
        if renewed.get(key):  # 双检：本进程此前已重采（成功或失败都不再来一次）
            prev = renewed[key]
            if prev is True and ep.exists():
                return True, None
            return False, (prev if isinstance(prev, str) else f"会话 {key} 已重采但实体仍缺失")
        ok, err = collect(session)
        renewed[key] = True if ok else (err or "重采失败")
        return ok, err


def resolve_for_attach(case: dict, registry: dict | None) -> tuple[str | None, str | None]:
    """normal 执行期把 case.session 解析成可加载的 storage_state 路径。返回 (path, error)。

    - 无 session → (None, None)：存量用例行为与 v1 完全一致。
    - 会话不存在 → 指引（配置错误暴露为明确失败）。
    - 实体文件缺失（从未采集 / 采集失败 / 实体被删）→ 指引「点立即采集」，绝不静默当已登录。
    - 实体存在但已过期 → 进程内加锁就地重采一次；成功 attach，失败带 last_error。
    - 有效（含 ttl<=0 永不过期）→ 直接返回路径。
    """
    sid = case.get("session")
    if not sid:
        return None, None
    registry = registry if registry is not None else {}

    session = store.get_session(store.SESSIONS_FILE, sid)
    if session is None:
        return None, f"用例引用的登录态会话 {sid!r} 不存在，请在『登录态』页检查或重新指定"

    env = session.get("env", "")
    ep = store.entity_path(sid, env)
    st = store.status_of(session)

    if not ep.exists():
        hint = f"（最近采集失败：{st['last_error']}）" if st.get("last_error") else ""
        return None, f"登录态会话 {sid!r} 尚未采集{hint}，请在『登录态』页点『立即采集』后重试"

    if st["status"] == store.STATUS_EXPIRED:
        ok, err = _renew_in_process(session, registry, ep)
        if ok and ep.exists():
            return str(ep), None
        return None, (err or f"登录态会话 {sid!r} 已过期且就地重采失败")

    return str(ep), None
