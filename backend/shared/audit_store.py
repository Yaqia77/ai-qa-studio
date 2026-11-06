"""操作审计留痕（设计文档企业级硬性要求 #2 与 8.3）。

所有 /api/* 写操作（POST/PUT/PATCH/DELETE）在认证中间件里落一条审计记录：
who（操作人/角色）· when（时间）· what（动作 + 目标路径）· result（状态码）。

- 存储：data/audit_logs.jsonl 追加写（JSONL 单行一条，写入 O(1)，超量自动截断最旧）
- 脱敏：password/token/value 等敏感字段与 Bearer 串一律掩码；登录接口只记用户名
- 失败降级：审计写异常绝不影响业务响应（内部吞掉并打日志）
"""
import csv
import io
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_FILE = ROOT / "data" / "audit_logs.jsonl"
_LOCK = threading.Lock()

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
MAX_LOGS = 5000          # 超过即裁剪最旧的，文件体量可控
MAX_DETAIL = 300         # 请求体摘要长度上限

# 敏感字段名（命中即掩码，递归生效）
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "access_token",
    "authorization", "auth", "cookie", "api_key", "apikey", "private_key",
    "value", "new_token",
}
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9\-._~+/=]{6,}")

# 路径前缀 → 业务模块名（拿不到路由 summary 时的兜底描述）
_MODULE_BY_PREFIX = (
    ("/api/auth", "认证与用户"),
    ("/api/audit", "操作审计"),
    ("/api/cases", "用例管理"),
    ("/api/defs", "接口定义"),
    ("/api/dependencies", "用例依赖"),
    ("/api/plans", "测试场景"),
    ("/api/runs", "执行与报告"),
    ("/api/schedules", "定时任务"),
    ("/api/secrets", "凭证管理"),
    ("/api/webhooks", "通知配置"),
    ("/api/notifications", "通知配置"),
    ("/api/envs", "环境配置"),
    ("/api/config", "系统配置"),
    ("/api/scripts", "公共脚本"),
)

# 路径尾段是集合名/动词时无标识意义，不作为“对象”
_NOISE_TAIL = {
    "runs", "cases", "users", "plans", "login", "logout", "me", "cleanup", "audit",
    "secrets", "schedules", "notifications", "dependencies", "envs", "auth", "health", "config",
    "webhooks", "scripts", "defs", "aggregate", "skeleton", "openapi",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _module_of(path: str) -> str:
    for prefix, name in _MODULE_BY_PREFIX:
        if path.startswith(prefix):
            return name
    return "平台"


def _mask(obj):
    """递归掩码敏感字段值。"""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in SENSITIVE_KEYS else _mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v) for v in obj]
    if isinstance(obj, str):
        return _BEARER_RE.sub(r"\1***", obj)
    return obj


def _summarize_body(path: str, raw: bytes, ctype: str = "") -> str:
    """请求体 → 脱敏摘要；登录接口只记用户名，绝不落明文密码。"""
    if not raw:
        return "(无请求体)" if ctype.startswith("application/json") else ""
    if ctype.startswith("multipart/"):
        return "(文件上传，内容不记录)"
    if path.endswith("/api/auth/login"):
        try:
            username = json.loads(raw.decode("utf-8", "replace")).get("username", "?")
        except (json.JSONDecodeError, AttributeError):
            username = "?"
        return f"登录尝试：{username}"
    text = raw.decode("utf-8", "replace").strip()
    try:
        masked = _mask(json.loads(text))
        if isinstance(masked, dict):
            # 去掉 None 字段，摘要更聚焦实际改动
            masked = {k: v for k, v in masked.items() if v is not None}
        text = json.dumps(masked, ensure_ascii=False)
    except json.JSONDecodeError:
        text = _BEARER_RE.sub(r"\1***", text)
    return text[:MAX_DETAIL]


def _target_of(path: str, body_summary: str) -> str:
    """从请求体抽主键类字段，退而取路径尾段（集合名等无标识意义的段丢弃）。"""
    for key in ("username", "case_id", "plan_id", "run_id", "name", "id"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', body_summary or "")
        if m:
            return m.group(1)[:60]
    tail = unquote(path.rstrip("/")).split("/")[-1]
    return "" if (not tail or tail in _NOISE_TAIL) else tail[:60]


def record(*, method: str, path: str, query: str, raw_body: bytes, status: int,
           user: dict | None, ip: str = "", action: str = "", ctype: str = "") -> None:
    """落一条审计记录（异常吞掉，不影响业务响应）。"""
    summary = _summarize_body(path, raw_body, ctype)
    entry = {
        "id": secrets.token_hex(6),
        "ts": _now(),
        "username": (user or {}).get("username") or "-",
        "display_name": (user or {}).get("display_name") or "",
        "role": (user or {}).get("role") or "-",
        "method": method,
        "path": path,
        "action": action or f"{_module_of(path)} · {method}",
        "target": _target_of(path, summary),
        "detail": summary,
        "query": query[:200],
        "status": status,
        "ok": status < 400,
        "ip": ip,
    }
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_FILE, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            if _LOG_FILE.stat().st_size > 2_000_000:
                _trim()
    except OSError as e:
        logger.warning(f"审计写入失败：{e!r}")


def _trim() -> None:
    """裁剪到最近 MAX_LOGS 条（原子替换，读到的永远是全量行）。调用方需持锁。"""
    lines = _raw_lines()
    if len(lines) <= MAX_LOGS:
        return
    tmp = _LOG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.writelines(line + "\n" for line in lines[-MAX_LOGS:])
    os.replace(tmp, _LOG_FILE)


def _raw_lines() -> list[str]:
    if not _LOG_FILE.exists():
        return []
    try:
        with open(_LOG_FILE, encoding="utf-8") as fp:
            return [ln.strip() for ln in fp if ln.strip()]
    except OSError:
        return []


def list_logs(limit: int = 200, username: str = "", keyword: str = "",
              days: int = 0, only_failed: bool = False) -> list[dict]:
    """按条件倒序返回审计记录（新→旧）。days=0 表示不限时间。"""
    out: list[dict] = []
    kw = (keyword or "").lower()
    floor = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds") if days > 0 else ""
    for line in reversed(_raw_lines()):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if username and rec.get("username") != username:
            continue
        if floor and (rec.get("ts") or "") < floor:
            continue
        if only_failed and rec.get("ok", True):
            continue
        if kw and kw not in " ".join(str(rec.get(k, "")) for k in
                                    ("action", "path", "target", "detail", "username", "query")).lower():
            continue
        out.append(rec)
        if len(out) >= max(1, min(limit, 2000)):
            break
    return out


def total_count() -> int:
    return len(_raw_lines())


def purge(days: int = 0) -> int:
    """保留最近 days 天（days<=0 清空全部）。返回删除条数。"""
    with _LOCK:
        lines = _raw_lines()
        if days <= 0:
            keep: list[str] = []
        else:
            floor = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
            keep = []
            for line in lines:
                try:
                    if json.loads(line).get("ts", "") >= floor:
                        keep.append(line)
                except json.JSONDecodeError:
                    continue
        removed = len(lines) - len(keep)
        if removed <= 0:
            return 0
        tmp = _LOG_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.writelines(line + "\n" for line in keep)
        os.replace(tmp, _LOG_FILE)
        return removed


def usernames() -> list[str]:
    """已出现过的操作人（供前端筛选下拉）。"""
    seen: dict[str, None] = {}
    for line in _raw_lines():
        try:
            name = json.loads(line).get("username")
        except json.JSONDecodeError:
            continue
        if name and name != "-":
            seen.setdefault(name, None)
    return sorted(seen)


def to_csv(entries: list[dict]) -> str:
    """审计记录 → CSV（带 BOM，Excel 直接双击不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "操作人", "姓名", "角色", "动作", "对象", "方法", "路径", "结果", "状态码", "来源IP", "请求摘要"])
    for r in entries:
        writer.writerow([
            (r.get("ts") or "").replace("T", " "), r.get("username", ""), r.get("display_name", ""),
            r.get("role", ""), r.get("action", ""), r.get("target", ""), r.get("method", ""),
            r.get("path", ""), "成功" if r.get("ok") else "失败", r.get("status", ""),
            r.get("ip", ""), (r.get("detail") or "").replace("\n", " "),
        ])
    return "\ufeff" + buf.getvalue()
