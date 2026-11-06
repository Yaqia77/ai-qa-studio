"""UI 登录态会话库：全局一个 JSON 注册表（data/ui_sessions.json）。

登录态（storage_state）复用的数据层：全局一个会话注册表。

设计对齐 backend.ui_case_store（唯二范式来源，降低学习成本）：
- 同一套错误口径 UiSessionError.kind ∈ invalid/not_found/conflict/locked，路由据此映射 HTTP 状态；
- 校验通过才落盘：临时文件 + os.replace 原子替换，被占用转 locked；
- 注册表只存「元数据」（无敏感值）；storage_state 实体文件 data/ui_sessions/<id>__<env>.json
  属敏感数据（等同 secret_vars），整目录 gitignore、绝不回传前端、不写日志。

本模块只负责注册表的 CRUD / 校验 / 状态派生，不依赖 Playwright：
实体文件的写出与采集链路（collect）由 core.engines.web（UiRunner）+ 后续步骤实现，此处仅预留实体路径解析。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 注册表与实体目录（实体目录整目录进 .gitignore）
SESSIONS_FILE = ROOT / "data" / "ui_sessions.json"
ENTITY_DIR = ROOT / "data" / "ui_sessions"

VALID_BROWSERS = {"chromium", "firefox", "webkit"}
DEFAULT_TTL_SECONDS = 3600

# id 参与实体文件名与按名直接寻址（/api/ui-sessions/{id}），禁空白控制符与路径/URL 特殊字符
_ID_CTRL_RE = re.compile(r"[\r\n\t]")
_ID_BAD_CHARS = set('/\\?#')

# 状态派生取值
STATUS_NOT_COLLECTED = "not_collected"
STATUS_VALID = "valid"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"

# 改动这些字段会使旧登录态与新定义不再匹配 → 作废 captured_at（强制重采）；仅改 name/desc 不作废
_INVALIDATING_FIELDS = ("env", "browser", "file", "capture_case", "ttl_seconds")


class UiSessionError(Exception):
    """kind: invalid（字段非法）/ not_found（会话不存在）/ conflict（id 重复）/ locked（文件被占用）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def env_names_from_config() -> list[str]:
    """从 config.yaml 读取合法环境名列表；读不到返回空（调用方据此拒绝任何 env）。"""
    try:
        from core.context import load_config
        return list((load_config().get("env") or {}).keys())
    except Exception:  # noqa: BLE001 配置不可读时不阻断：交由 validate 判 env 非法
        return []


# ==================== 实体路径（供采集/复用链路复用） ====================

def entity_path(session_id: str, env: str, base_dir: Path | str = ENTITY_DIR) -> Path:
    """storage_state 实体文件路径：按 id__env 命名，切环境各留一份不串味。"""
    return Path(base_dir) / f"{session_id}__{env}.json"


# ==================== 读写底座 ====================

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except OSError as e:
        _safe_remove(tmp)
        raise UiSessionError("invalid", f"写入临时文件失败: {e!r}")
    try:
        os.replace(tmp, path)
    except PermissionError:
        _safe_remove(tmp)
        raise UiSessionError("locked", f"文件 {path.name} 正被其他程序占用，请关闭后重试")


def _safe_remove(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _read(path: Path) -> dict:
    """注册表缺失视为空表（会话库全局单文件、首次新建时懒创建）；损坏/结构非法报错。"""
    if not path.exists():
        return {"version": 1, "sessions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UiSessionError("invalid", f"会话注册表不是合法 JSON: {path.name}（{e}）")
    except OSError as e:
        raise UiSessionError("invalid", f"读取会话注册表失败: {e!r}")
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise UiSessionError("invalid", f"会话注册表结构应为 {{'sessions': [...]}}: {path.name}")
    return data


def load_sessions(path: Path | None = None) -> list[dict]:
    return _read(path or SESSIONS_FILE)["sessions"]


# ==================== 校验（仅规范化用户可编辑字段，不含运行态字段） ====================

def _validate_id(sid_raw: object) -> str:
    sid = str(sid_raw or "").strip()
    if not sid:
        raise UiSessionError("invalid", "会话 id 不能为空")
    if _ID_CTRL_RE.search(sid):
        raise UiSessionError("invalid", "会话 id 不能包含换行/制表符")
    bad = sorted({c for c in sid if c in _ID_BAD_CHARS})
    if bad:
        raise UiSessionError("invalid", f"会话 id 不能包含字符 {' '.join(bad)}（按名直接寻址 + 参与实体文件名）")
    return sid


def validate_session(raw: dict, env_names: list[str] | None = None) -> dict:
    """整条会话定义规范化 + 校验；返回清洗后的 dict（不落盘、不含运行态字段）。任一非法即抛 UiSessionError。"""
    if not isinstance(raw, dict):
        raise UiSessionError("invalid", "会话必须是对象")

    sid = _validate_id(raw.get("id"))

    env = str(raw.get("env") or "").strip()
    if not env:
        raise UiSessionError("invalid", "会话必须绑定环境 env（cookie 归属域名，切环境不通用）")
    names = env_names if env_names is not None else env_names_from_config()
    if env not in names:
        raise UiSessionError("invalid", f"环境 {env!r} 不存在，可用环境: {names or '无'}")

    browser = str(raw.get("browser") or "chromium").strip().lower()
    if browser not in VALID_BROWSERS:
        raise UiSessionError("invalid", f"浏览器只能是 {'/'.join(sorted(VALID_BROWSERS))}，实际为 {browser!r}")

    file = str(raw.get("file") or "").strip()
    if not file:
        raise UiSessionError("invalid", "采集用例所在的 UI 项目文件 file 不能为空")
    if Path(file).suffix.lower() != ".json":
        raise UiSessionError("invalid", f"采集用例只能来自 UI 项目（.json），实际为 {file!r}")

    capture_case = str(raw.get("capture_case") or "").strip()
    if not capture_case:
        raise UiSessionError("invalid", "必须指定一条采集用例 capture_case（登录后导出登录态的那条 UI 用例编号）")

    ttl = raw.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or int(ttl) != ttl or int(ttl) < 0:
        raise UiSessionError("invalid", f"ttl_seconds 必须是 >=0 的整数（<=0 表示不自动过期，仅手动采集），实际为 {ttl!r}")
    ttl = int(ttl)

    return {
        "id": sid,
        "name": str(raw.get("name") or "新建登录态").strip() or "新建登录态",
        "desc": str(raw.get("desc") or "").strip(),
        "env": env,
        "browser": browser,
        "headless": bool(raw.get("headless", True)),
        "file": file,
        "capture_case": capture_case,
        "ttl_seconds": ttl,
    }


# ==================== 查询 ====================

def get_session(path: Path | None, session_id: str) -> dict | None:
    path = path or SESSIONS_FILE
    return next((s for s in load_sessions(path) if s.get("id") == session_id), None)


def list_sessions(path: Path | None = None) -> list[dict]:
    return load_sessions(path or SESSIONS_FILE)


# ==================== 状态派生（纯逻辑，不触碰实体文件，便于单测） ====================

def status_of(session: dict, now: datetime | None = None) -> dict:
    """派生登录态状态：not_collected / valid / expired / failed + 剩余可用秒数。

    仅依据元数据（captured_at / ttl_seconds / last_error）判定；实体文件是否缺失由
    运行时复用链路（core.engines.web + conftest）负责，此处不读文件系统以保持可单测、无副作用。
    """
    sid = session.get("id", "")
    captured_at = session.get("captured_at")
    last_error = session.get("last_error")
    ttl = int(session.get("ttl_seconds", DEFAULT_TTL_SECONDS) or 0)
    out = {"id": sid, "status": STATUS_NOT_COLLECTED, "captured_at": captured_at,
           "ttl_seconds": ttl, "remaining_seconds": None, "last_error": last_error}

    if not captured_at:
        # 从未成功采集：若最近一次采集失败留有原因则标 failed，否则未采集
        out["status"] = STATUS_FAILED if last_error else STATUS_NOT_COLLECTED
        return out

    try:
        base = datetime.fromisoformat(str(captured_at))
    except ValueError:
        out["status"] = STATUS_NOT_COLLECTED  # 时间戳不可解析，保守视为需重采
        return out

    if ttl <= 0:  # 不自动过期
        out["status"] = STATUS_VALID
        out["remaining_seconds"] = None
        return out

    now = now or datetime.now()
    remaining = ttl - int((now - base).total_seconds())
    out["remaining_seconds"] = max(remaining, 0)
    out["status"] = STATUS_EXPIRED if remaining <= 0 else STATUS_VALID
    return out


# ==================== 写操作 ====================

def create_session(path: Path | None, raw: dict, env_names: list[str] | None = None) -> dict:
    path = path or SESSIONS_FILE
    definition = validate_session(raw, env_names)
    with _LOCK:
        data = _read(path)
        if any(s.get("id") == definition["id"] for s in data["sessions"]):
            raise UiSessionError("conflict", f"会话 id 已存在: {definition['id']}")
        record = {
            **definition,
            "created_at": _now(),
            "captured_at": None,
            "last_error": None,
        }
        data["sessions"].append(record)
        _write(path, data)
    return record


def update_session(path: Path | None, session_id: str, changes: dict,
                   env_names: list[str] | None = None) -> dict:
    """按 id 定位更新（id 不可改）；合并后重走全量校验才落盘。

    改动 env/browser/file/capture_case/ttl_seconds 任一 → 作废旧登录态（captured_at=None、清 last_error），
    因旧 storage_state 与新定义可能不再匹配，强制重新采集；仅改 name/desc/headless 不作废。
    """
    path = path or SESSIONS_FILE
    changes = {k: v for k, v in changes.items() if k != "id"}
    if not changes:
        raise UiSessionError("invalid", "没有需要更新的字段")
    with _LOCK:
        data = _read(path)
        idx = next((i for i, s in enumerate(data["sessions"]) if s.get("id") == session_id), None)
        if idx is None:
            raise UiSessionError("not_found", f"会话不存在: {session_id}")
        existing = data["sessions"][idx]
        merged = {**existing, **changes, "id": session_id}
        definition = validate_session(merged, env_names)

        invalidated = any(
            str(existing.get(f)) != str(definition.get(f)) for f in _INVALIDATING_FIELDS
        )
        record = {
            **definition,
            "created_at": existing.get("created_at") or _now(),
            "captured_at": None if invalidated else existing.get("captured_at"),
            "last_error": None if invalidated else existing.get("last_error"),
        }
        data["sessions"][idx] = record
        _write(path, data)
    return record


def delete_session(path: Path | None, session_id: str,
                   entity_base: Path | str | None = None) -> dict:
    """删除注册表条目，并连带清理其 storage_state 实体文件（若存在）。"""
    path = path or SESSIONS_FILE
    with _LOCK:
        data = _read(path)
        before = len(data["sessions"])
        removed = next((s for s in data["sessions"] if s.get("id") == session_id), None)
        data["sessions"] = [s for s in data["sessions"] if s.get("id") != session_id]
        if len(data["sessions"]) == before:
            raise UiSessionError("not_found", f"会话不存在: {session_id}")
        _write(path, data)

    cleared_entity = None
    if removed is not None:
        ep = entity_path(session_id, removed.get("env", ""), entity_base or ENTITY_DIR)
        try:
            if ep.exists():
                ep.unlink()
                cleared_entity = str(ep)
        except OSError:
            pass  # 实体文件删除失败不阻断注册表删除（下次采集会覆盖）
    return {"deleted": session_id, "entity_removed": cleared_entity}


def mark_captured(path: Path | None, session_id: str, captured_at: str | None = None) -> dict:
    """采集成功后回写运行态字段（供步骤 3 的 collect 链路调用）：刷新 captured_at、清 last_error。"""
    path = path or SESSIONS_FILE
    with _LOCK:
        data = _read(path)
        idx = next((i for i, s in enumerate(data["sessions"]) if s.get("id") == session_id), None)
        if idx is None:
            raise UiSessionError("not_found", f"会话不存在: {session_id}")
        data["sessions"][idx]["captured_at"] = captured_at or _now()
        data["sessions"][idx]["last_error"] = None
        _write(path, data)
        return data["sessions"][idx]


def mark_failed(path: Path | None, session_id: str, error: str) -> dict:
    """采集失败后留痕：记 last_error；保留旧 captured_at（若有），使既有登录态仍按有效期可复用。"""
    path = path or SESSIONS_FILE
    with _LOCK:
        data = _read(path)
        idx = next((i for i, s in enumerate(data["sessions"]) if s.get("id") == session_id), None)
        if idx is None:
            raise UiSessionError("not_found", f"会话不存在: {session_id}")
        data["sessions"][idx]["last_error"] = (error or "")[:400]
        _write(path, data)
        return data["sessions"][idx]
