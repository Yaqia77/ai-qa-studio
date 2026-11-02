"""用户存储：data/users.json（JSON 原子写，模式与 plan_store 一致）。

密码使用 PBKDF2-HMAC-SHA256 加盐哈希存储，绝不落明文。
首次启动自动播种 5 个默认角色账号（对应设计文档 RBAC 角色表）。
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_FILE = ROOT / "data" / "users.json"
_LOCK = threading.Lock()

ROLES = ("admin", "tl", "qa", "dev", "viewer", "agent_svc")
ROLE_NAMES = {
    "admin": "管理员",
    "tl": "测试负责人",
    "qa": "测试工程师",
    "dev": "开发",
    "viewer": "只读访客",
    # Agent 服务账号（设计文档 5.3）：只读靠登录态，唯一权限码 case:write；
    # 不播种、由 tools/init_agent_svc.py 显式创建，密码绝不进仓
    "agent_svc": "Agent 服务账号",
}

# 播种账号：生产部署后应立即改密（MVP 提示写在前端登录页）
_DEFAULT_USERS = [
    ("admin", "admin123", "系统管理员", "admin"),
    ("tl", "tl123", "测试负责人", "tl"),
    ("qa", "qa123", "测试工程师", "qa"),
    ("dev", "dev123", "开发工程师", "dev"),
    ("viewer", "viewer123", "只读访客", "viewer"),
]

_ITERATIONS = 100_000


def _hash_password(password: str, salt_hex: str | None = None) -> str:
    """返回 'pbkdf2$迭代数$盐hex$哈希hex'。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, dk_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


def _load() -> dict:
    if not _DATA_FILE.exists():
        return {}
    try:
        with open(_DATA_FILE, encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    os.replace(tmp, _DATA_FILE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def bootstrap() -> None:
    """users.json 不存在时播种默认账号。幂等。"""
    with _LOCK:
        data = _load()
        if data:
            return
        for username, pwd, display, role in _DEFAULT_USERS:
            data[username] = {
                "username": username,
                "password": _hash_password(pwd),
                "display_name": display,
                "role": role,
                "enabled": True,
                "created_at": _now(),
                "updated_at": _now(),
            }
        _save(data)


def get_user(username: str) -> dict | None:
    with _LOCK:
        return _load().get(username)


def list_users() -> list[dict]:
    """返回全部用户（含密码哈希字段，序列化给前端前必须经 _public 脱敏）。"""
    with _LOCK:
        users = list(_load().values())
    users.sort(key=lambda u: (u.get("role") != "admin", u.get("username", "")))
    return users


def create_user(username: str, password: str, display_name: str, role: str) -> dict:
    """新建用户；用户名已存在时抛 ValueError。"""
    if role not in ROLES:
        raise ValueError(f"非法角色: {role}")
    if not username or not username.strip():
        raise ValueError("用户名不能为空")
    if len(password) < 6:
        raise ValueError("密码长度至少 6 位")
    with _LOCK:
        data = _load()
        if username in data:
            raise ValueError(f"用户已存在: {username}")
        now = _now()
        user = {
            "username": username,
            "password": _hash_password(password),
            "display_name": display_name or username,
            "role": role,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        data[username] = user
        _save(data)
    return user


def update_user(username: str, **fields) -> dict | None:
    """更新 display_name/role/enabled/password；密码单独哈希。"""
    with _LOCK:
        data = _load()
        user = data.get(username)
        if user is None:
            return None
        for k in ("display_name", "role", "enabled"):
            if k in fields and fields[k] is not None:
                if k == "role" and fields[k] not in ROLES:
                    raise ValueError(f"非法角色: {fields[k]}")
                user[k] = fields[k]
        if fields.get("password"):
            if len(fields["password"]) < 6:
                raise ValueError("密码长度至少 6 位")
            user["password"] = _hash_password(fields["password"])
        user["updated_at"] = _now()
        data[username] = user
        _save(data)
    return user


def delete_user(username: str) -> bool:
    with _LOCK:
        data = _load()
        if username not in data:
            return False
        data.pop(username)
        _save(data)
    return True
