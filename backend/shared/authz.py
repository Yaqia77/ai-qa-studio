"""认证与鉴权（RBAC，对应设计文档第二章角色权限矩阵）。

- Token：登录成功后签发随机串，内存登记表维护（服务重启需重新登录，MVP 可接受）。
- 权限：角色 -> 权限码集合，矩阵存 data/permissions.json（「角色权限」页可改，热加载）；
  接口用 Depends(require_perm("xxx")) 做后端二次校验，
  前端 v-perm 只做按钮显隐，安全边界始终在后端。
"""
import secrets
import threading
import time
from typing import Callable

from fastapi import Depends, HTTPException, Request

from backend.shared import perm_store

# 权限矩阵已外置到 data/permissions.json（设计文档 8.2）；
# 代码里的 perm_store.DEFAULT_MATRIX 仅作首次基线与「恢复默认」来源。

# token 滑动过期：空闲（无任何鉴权请求）超过 TTL 即失效需重登；
# 每次成功鉴权刷新活跃时间，故持续操作不会掉线，仅踢长时间挂机的共享工位会话。
TOKEN_TTL_SECONDS = 8 * 3600

# 登录失败限流：同一 (用户名, IP) 在窗口内凭据错误达上限即临时锁定，
# 防内网共享机器下的慢速凭据试探；登录成功即清零，不影响正常用户。
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_SECONDS = 15 * 60

# token -> [username, last_active_ts]（内存态；服务重启即失效）
_TOKENS: dict[str, list] = {}
_TOKEN_LOCK = threading.Lock()

# 限流键 -> [fail_count, window_start_ts, lockout_until_ts]
_LOGIN_FAIL: dict[str, list] = {}
_LOGIN_LOCK = threading.Lock()


def _purge_expired(now: float) -> None:
    """清理空闲超时 token，防内存无限增长。调用方需持 _TOKEN_LOCK。"""
    dead = [t for t, entry in _TOKENS.items() if now - entry[1] > TOKEN_TTL_SECONDS]
    for t in dead:
        _TOKENS.pop(t, None)


def issue_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _TOKEN_LOCK:
        _purge_expired(now)
        _TOKENS[token] = [username, now]
    return token


def revoke_token(token: str) -> None:
    with _TOKEN_LOCK:
        _TOKENS.pop(token, None)


def username_of(token: str) -> str | None:
    """取 token 对应用户名；过期即吊销并返回 None，有效则滑动续期。"""
    now = time.time()
    with _TOKEN_LOCK:
        entry = _TOKENS.get(token)
        if entry is None:
            return None
        if now - entry[1] > TOKEN_TTL_SECONDS:
            _TOKENS.pop(token, None)
            return None
        entry[1] = now  # 滑动续期：有活动就不掉线
        return entry[0]


def login_retry_after(key: str) -> int:
    """该限流键是否处于锁定期；是则返回还需等待的秒数，否则 0。"""
    now = time.time()
    with _LOGIN_LOCK:
        rec = _LOGIN_FAIL.get(key)
        if not rec:
            return 0
        lockout_until = rec[2]
        if lockout_until and now < lockout_until:
            return int(lockout_until - now) + 1
        return 0


def register_login_fail(key: str) -> None:
    """记一次凭据错误；窗口内累计达上限则进入锁定期。"""
    now = time.time()
    with _LOGIN_LOCK:
        rec = _LOGIN_FAIL.get(key)
        if rec is None or (now - rec[1]) > LOGIN_WINDOW_SECONDS:
            _LOGIN_FAIL[key] = [1, now, 0.0]  # 新窗口
        else:
            rec[0] += 1
            if rec[0] >= LOGIN_MAX_FAILS:
                rec[2] = now + LOGIN_LOCKOUT_SECONDS


def clear_login_fail(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAIL.pop(key, None)


def permissions_of(role: str) -> list[str]:
    """角色 → 权限码；每次实时读矩阵，故权限变更保存后立即对所有会话生效。"""
    return perm_store.permissions_of(role)


def extract_token(request: Request) -> str:
    """从 Authorization: Bearer 头取 token；iframe 场景兜底支持 ?token= 查询参数。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "")


# 免认证白名单：健康检查、登录、文档、报告 HTML（iframe/新标签直接加载）
EXEMPT_EXACT = {"/api/health", "/api/auth/login", "/docs", "/openapi.json", "/redoc"}


def is_exempt(path: str) -> bool:
    if path in EXEMPT_EXACT:
        return True
    # /api/runs/{id}/report 是浏览器 iframe 资源，无法带自定义头：放行
    # （仅只读 HTML，明细 JSON 接口仍需鉴权）
    if path.endswith("/report") and path.startswith("/api/runs/"):
        return True
    return False


def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：读取中间件写入 request.state.user，未登录 401。"""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def require_perm(code: str) -> Callable:
    """工厂依赖：要求当前用户持有权限码，否则 403。"""
    def _dep(user: dict = Depends(get_current_user)) -> dict:
        if code not in permissions_of(user.get("role", "")):
            raise HTTPException(status_code=403, detail=f"无权限: {code}（角色 {user.get('role')} 不允许）")
        return user
    return _dep
