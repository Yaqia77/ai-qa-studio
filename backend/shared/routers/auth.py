"""认证与用户管理接口：登录/登出/当前用户 + 用户 CRUD + 角色权限矩阵（仅 admin）。"""
from fastapi import APIRouter, Depends, HTTPException, Request

from backend.shared import authz, perm_store, user_store
from backend.shared.schemas import (
    LoginRequest,
    LoginResponse,
    PermMatrixUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/api/auth", tags=["用户与权限"])


def _public(user: dict, with_perms: bool = False) -> UserOut:
    """脱敏输出：绝不带出密码哈希。"""
    data = {
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "role_name": user_store.ROLE_NAMES.get(user["role"], user["role"]),
        "enabled": user.get("enabled", True),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }
    if with_perms:
        data["permissions"] = authz.permissions_of(user["role"])
    return UserOut(**data)


@router.post("/login", response_model=LoginResponse, summary="登录（用户名+密码）")
def login(body: LoginRequest, request: Request):
    username = body.username.strip()
    # 限流键：用户名 + 来源 IP（既防单账号被爆珠，也防同一 IP 横扫多账号）
    ip = request.client.host if request.client else ""
    key = f"{username}|{ip}"
    wait = authz.login_retry_after(key)
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {wait} 秒后重试")
    user = user_store.get_user(username)
    if user is None or not user_store.verify_password(body.password, user["password"]):
        authz.register_login_fail(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.get("enabled", True):
        # 停用不是凭据错误，不计入失败限流（否则管理员误停会连带锁住登录尝试统计）
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
    authz.clear_login_fail(key)
    token = authz.issue_token(user["username"])
    return LoginResponse(
        token=token,
        user=_public(user, with_perms=True),
    )


@router.post("/logout", summary="登出（吊销当前 token）")
def logout(request: Request):
    authz.revoke_token(authz.extract_token(request))
    return {"ok": True}


@router.get("/me", response_model=UserOut, summary="当前登录用户（含权限码列表）")
def me(user: dict = Depends(authz.get_current_user)):
    return _public(user, with_perms=True)


# ---------- 角色权限矩阵（设计文档 8.2，仅 admin） ----------

@router.get("/permissions", summary="角色权限矩阵（码目录 + 当前分配 + 默认基线 + 护栏规则）")
def get_permissions(_: dict = Depends(authz.require_perm("user:manage"))):
    m = perm_store.matrix()
    return {
        "roles": [{"value": r, "label": user_store.ROLE_NAMES.get(r, r)} for r in user_store.ROLES],
        "catalog": [
            {"code": code, "name": name, "desc": desc, "roles": m.get(code, [])}
            for code, (name, desc) in perm_store.CATALOG.items()
        ],
        "matrix": m,
        "defaults": perm_store.DEFAULT_MATRIX,
        "rules": {"locked_role": perm_store.LOCKED_ROLE, "must_nonempty": list(perm_store.MUST_NONEMPTY)},
    }


@router.put("/permissions", summary="保存角色权限矩阵（热生效，含防锁死护栏）")
def update_permissions(body: PermMatrixUpdate, actor: dict = Depends(authz.require_perm("user:manage"))):
    before = perm_store.matrix()
    try:
        after = perm_store.save(body.matrix)
    except perm_store.PermConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "matrix": after,
        "changed": perm_store.diff(before, after),
        # 回带当前登录人所属角色的新权限集，前端据此即时对齐按钮/菜单，无需重登
        "actor_permissions": authz.permissions_of(actor.get("role", "")),
    }


@router.post("/permissions/reset", summary="恢复默认权限矩阵（代码基线）")
def reset_permissions(actor: dict = Depends(authz.require_perm("user:manage"))):
    before = perm_store.matrix()
    after = perm_store.reset()
    return {
        "matrix": after,
        "changed": perm_store.diff(before, after),
        "actor_permissions": authz.permissions_of(actor.get("role", "")),
    }


# ---------- 用户管理（仅 admin） ----------

@router.get("/users", response_model=list[UserOut], summary="用户列表")
def list_users(_: dict = Depends(authz.require_perm("user:manage"))):
    return [_public(u) for u in user_store.list_users()]


@router.post("/users", response_model=UserOut, status_code=201, summary="新建用户")
def create_user(body: UserCreate, _: dict = Depends(authz.require_perm("user:manage"))):
    try:
        user = user_store.create_user(
            username=body.username.strip(), password=body.password,
            display_name=body.display_name.strip(), role=body.role,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public(user)


@router.put("/users/{username}", response_model=UserOut, summary="更新用户（资料/角色/启停/重置密码）")
def update_user(username: str, body: UserUpdate, actor: dict = Depends(authz.require_perm("user:manage"))):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="未传任何需要修改的字段")
    # 防呆：admin 不能把自己降级/停用，避免锁死系统
    if username == actor["username"] and ("role" in fields or fields.get("enabled") is False):
        raise HTTPException(status_code=400, detail="不能修改自己的角色或停用自己")
    try:
        user = user_store.update_user(username, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if user is None:
        raise HTTPException(status_code=404, detail=f"用户不存在: {username}")
    return _public(user)


@router.delete("/users/{username}", summary="删除用户")
def delete_user(username: str, actor: dict = Depends(authz.require_perm("user:manage"))):
    if username == actor["username"]:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if username == "admin":
        raise HTTPException(status_code=400, detail="内置 admin 账号不可删除")
    if not user_store.delete_user(username):
        raise HTTPException(status_code=404, detail=f"用户不存在: {username}")
    return {"ok": True}
