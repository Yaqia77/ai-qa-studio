"""角色权限矩阵存储：data/permissions.json（平台「角色权限」页可编辑，设计文档 8.2）。

两条铁律：
- 权限码目录（CATALOG）写死在代码里，界面只能改「码 → 角色」的分配，不能凭空造码——
  否则会出现前端 v-perm / 路由 meta.perm 都不认识的暗权限，安全边界失守；
- 内置防锁死护栏：admin 永远保留 user:manage / audit:manage，关键码不允许全员为空。

运行期按文件 mtime 缓存，require_perm 每次请求实时读取 → 保存即生效，
已登录用户无需重新登录（被收回权限的会话下一个请求即 403）。
文件不存在时回落到 DEFAULT_MATRIX，故无需播种、全新仓库开箱即用。
"""
import json
import os
import threading
from pathlib import Path

from backend.shared.user_store import ROLES

ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_FILE = ROOT / "data" / "permissions.json"
_LOCK = threading.Lock()

# 权限码目录：code → (名称, 说明)。dict 顺序即界面展示顺序；新增码须同步默认矩阵。
CATALOG: dict[str, tuple[str, str]] = {
    "case:write":     ("用例编辑/导入", "新增、修改、导入用例（写回 Excel）"),
    "case:delete":    ("用例删除", "整行删除用例（设计文档：删除需 TL+）"),
    "api:manage":     ("接口定义管理", "接口定义库的增删改与 OpenAPI 导入"),
    "script:manage":  ("公共脚本管理", "前后置脚本的定义与引用"),
    "plan:manage":    ("测试场景管理", "场景的增删改与执行"),
    "run:create":     ("触发/取消执行", "发起执行批次、取消运行中的批次"),
    "env:manage":     ("环境配置", "写回 config.yaml 的环境段"),
    "secret:manage":  ("凭证管理", "凭证变量的增删改"),
    "webhook:manage": ("通知配置", "Webhook 通知渠道"),
    "schedule:manage": ("定时任务", "Cron 调度的编辑与启停"),
    "session:manage": ("登录态管理", "UI 登录态会话的采集/复用，等同登录凭证，高敏"),
    "report:delete":  ("报告删除/清理", "删除单批次报告、清理旧报告"),
    "audit:view":     ("审计查询/导出", "查看与导出操作留痕"),
    "audit:manage":   ("审计清理", "裁剪审计日志"),
    "user:manage":    ("用户管理", "账号增删改与角色分配"),
}

# 代码基线：文件不存在时的默认矩阵，也是「恢复默认」的目标
DEFAULT_MATRIX: dict[str, list[str]] = {
    # agent_svc（Agent 服务账号）仅授予 case:write：读接口登录态即覆盖，
    # 管理类权限一律不给（设计文档 5.3：只读 + 用例写入）
    "case:write": ["admin", "tl", "qa", "agent_svc"],
    "case:delete": ["admin", "tl"],
    "api:manage": ["admin", "tl", "qa"],
    "script:manage": ["admin", "tl", "qa"],
    "plan:manage": ["admin", "tl", "qa"],
    "run:create": ["admin", "tl", "qa", "dev"],
    "env:manage": ["admin", "tl"],
    "secret:manage": ["admin", "tl"],
    "webhook:manage": ["admin", "tl"],
    "schedule:manage": ["admin", "tl"],
    "session:manage": ["admin", "tl"],
    "report:delete": ["admin", "tl"],
    "audit:view": ["admin", "tl"],
    "audit:manage": ["admin"],
    "user:manage": ["admin"],
}

# 必须保留给指定角色，否则平台再也改不回来（自锁死）
LOCKED_ROLE = {"user:manage": "admin", "audit:manage": "admin"}
# 至少授予一个角色，否则平台核心能力被关闭
MUST_NONEMPTY = ("run:create", "user:manage")

_loaded: tuple[float, dict] | None = None


class PermConfigError(ValueError):
    """矩阵非法，接口层转 400 透出 detail。"""


def _normalize(raw: dict) -> dict:
    """丢弃未知码、缺失码回落默认值、剔除非法角色，并按 ROLES 固定顺序输出。"""
    valid = set(ROLES)
    out: dict[str, list[str]] = {}
    for code in CATALOG:
        roles = raw.get(code)
        if not isinstance(roles, list):
            roles = DEFAULT_MATRIX.get(code, [])
        out[code] = [r for r in ROLES if r in set(roles) & valid]
    return out


def _read() -> dict:
    """按 mtime 缓存读盘；文件缺失/损坏一律回落默认矩阵（绝不因配置坏掉而拒绝服务）。"""
    global _loaded
    if not _DATA_FILE.exists():
        if _loaded is None:
            _loaded = (0.0, _normalize({}))
        return _loaded[1]
    try:
        mtime = _DATA_FILE.stat().st_mtime
    except OSError:
        return _loaded[1] if _loaded else _normalize({})
    if _loaded and _loaded[0] == mtime:
        return _loaded[1]
    try:
        with open(_DATA_FILE, encoding="utf-8") as fp:
            raw = json.load(fp)
        data = _normalize(raw if isinstance(raw, dict) else {})
    except (json.JSONDecodeError, OSError):
        data = _normalize({})
    _loaded = (mtime, data)
    return data


def matrix() -> dict:
    with _LOCK:
        return _read()


def permissions_of(role: str) -> list[str]:
    return sorted(code for code, roles in _read().items() if role in roles)


def validate(raw: dict) -> dict:
    """提交即全量校验：码必须完整且已知、角色必须合法、护栏不可破。"""
    if not isinstance(raw, dict):
        raise PermConfigError("权限矩阵必须是 JSON 对象：权限码 -> 角色列表")
    unknown = sorted(set(raw) - set(CATALOG))
    if unknown:
        raise PermConfigError(f"未知权限码: {', '.join(unknown)}（权限码只能由代码目录定义）")
    missing = sorted(set(CATALOG) - set(raw))
    if missing:
        raise PermConfigError(f"缺少权限码: {', '.join(missing)}（请提交完整矩阵）")
    valid_roles = set(ROLES)
    for code, roles in raw.items():
        if not isinstance(roles, list):
            raise PermConfigError(f"{code} 的值必须是角色列表，如 [\"admin\",\"tl\"]")
        bad = sorted({r for r in roles if r not in valid_roles})
        if bad:
            raise PermConfigError(f"{code} 含非法角色: {', '.join(bad)}")
    for code, role in LOCKED_ROLE.items():
        if role not in set(raw.get(code) or []):
            raise PermConfigError(f"「{CATALOG[code][0]}」必须保留给 {role}，否则权限/审计将无人可管理")
    for code in MUST_NONEMPTY:
        if not raw.get(code):
            raise PermConfigError(f"「{CATALOG[code][0]}」至少需授予一个角色，否则平台不可用")
    return _normalize(raw)


def save(raw: dict) -> dict:
    """原子写并刷新缓存（校验不通过绝不落盘）。"""
    global _loaded
    data = validate(raw)
    with _LOCK:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, _DATA_FILE)
        _loaded = (_DATA_FILE.stat().st_mtime, data)
    return data


def reset() -> dict:
    return save(dict(DEFAULT_MATRIX))


def diff(old: dict, new: dict) -> list[str]:
    """人类可读的变更清单（供保存前确认弹窗与审计摘要）。"""
    lines: list[str] = []
    for code, (name, _) in CATALOG.items():
        before, after = set(old.get(code, [])), set(new.get(code, []))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            seg = name
            if added:
                seg += " +" + "/".join(added)
            if removed:
                seg += " -" + "/".join(removed)
            lines.append(seg)
    return lines
