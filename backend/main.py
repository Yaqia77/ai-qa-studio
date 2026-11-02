"""interface-auto 平台后端入口（FastAPI）。

启动（项目根目录）:
    .venv/Scripts/python.exe -m uvicorn backend.main:app --reload --port 8000
接口文档:
    http://127.0.0.1:8000/docs  （Swagger UI，前端联调/评审用）
"""
from pathlib import Path

import anyio
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from backend.api.routers import api_defs, cases, scripts
from backend.agent_bridge import router as agent_bridge
from backend.app.routers import app_cases
from backend.shared import audit_store, authz, config_writer, scheduler, user_store
from backend.shared.routers import (audit, auth, dependencies, notifications,
                                    plans, projects, runs, schedules, secrets)
from backend.shared.schemas import EnvsUpdate, HealthOut
from backend.ui.routers import ui_cases, ui_sessions
from core.context import load_config

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="interface-auto 测试平台 API",
    version="0.1.0",
    description="Excel 驱动接口自动化框架的平台化后端：用例查询 / 执行调度 / 批次跟踪。",
)

# 开发期放开跨域；生产部署（Nginx 同域反代）后可收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_defs.router)
app.include_router(auth.router)
app.include_router(audit.router)
app.include_router(cases.router)
app.include_router(dependencies.router)
app.include_router(notifications.router)
app.include_router(plans.router)
app.include_router(projects.router)
app.include_router(runs.router)
app.include_router(schedules.router)
app.include_router(scripts.router)
app.include_router(secrets.router)
app.include_router(ui_cases.router)
app.include_router(ui_sessions.router)
app.include_router(app_cases.router)

# Agent 桥接（M2）：仅当 config.yaml 的 agent.enabled=true 才注册，
# 缺省关闭 → 端点不进 /openapi.json，存量接口与页面行为零回归。
if agent_bridge.agent_enabled():
    app.include_router(agent_bridge.router)


@app.middleware("http")
async def auth_middleware(request, call_next):
    """全局认证：/api/* 默认需登录态，白名单（登录/健康/报告 HTML/文档）除外。

    权限细粒度由各接口 Depends(require_perm(...)) 二次校验，安全边界始终在后端。
    写操作（POST/PUT/PATCH/DELETE）在返回后统一落审计留痕。
    """
    path = request.url.path
    # CORS 预检交给 CORS 中间件；非 /api 路径（静态/文档）放行
    if request.method == "OPTIONS" or not path.startswith("/api/") or authz.is_exempt(path):
        audit_body = await _audit_body(request)
        response = await call_next(request)
        await _write_audit(request, response, audit_body, user=None)
        return response
    token = authz.extract_token(request)
    username = authz.username_of(token) if token else None
    user = user_store.get_user(username) if username else None
    if user is None or not user.get("enabled", True):
        audit_body = await _audit_body(request)
        denial = JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
        await _write_audit(request, denial, audit_body, user=None)
        return denial
    request.state.user = user
    audit_body = await _audit_body(request)
    response = await call_next(request)
    await _write_audit(request, response, audit_body, user=user)
    return response


_AUDIT_BODY_LIMIT = 1 << 20     # 超过 1MB（如 Excel 导入）不把请求体读进内存做摘要


async def _audit_body(request) -> bytes:
    """写操作预先缓存请求体供审计（Starlette 会回放，不影响下游接口解析）。"""
    if request.method not in audit_store.WRITE_METHODS:
        return b""
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        return b""
    if length > _AUDIT_BODY_LIMIT:
        return b""
    try:
        return await request.body()
    except Exception:
        return b""


async def _write_audit(request, response, body: bytes, user: dict | None) -> None:
    """审计埋点：仅记 /api 写操作；失败降级为日志，绝不影响业务响应。"""
    if request.method not in audit_store.WRITE_METHODS:
        return
    try:
        # call_next 已路由，scope["route"] 上带 OpenAPI summary，直接作为中文动作名
        route = request.scope.get("route")
        await anyio.to_thread.run_sync(
            lambda: audit_store.record(
                method=request.method, path=request.url.path,
                query=str(request.url.query or ""), raw_body=body,
                status=response.status_code, user=user,
                ip=(request.client.host if request.client else ""),
                action=(getattr(route, "summary", "") or ""),
                ctype=request.headers.get("content-type", ""),
            )
        )
    except Exception as e:
        logger.warning(f"审计埋点失败：{e!r}")


@app.on_event("startup")
def _bootstrap():
    """服务启动：播种默认用户 + 拉起 Cron 调度线程。"""
    user_store.bootstrap()
    scheduler.start()


@app.get("/api/health", response_model=HealthOut, tags=["系统"], summary="健康检查")
def health():
    cfg = load_config()
    return HealthOut(
        status="ok",
        default_env=cfg.get("default_env"),
        cases_file_exists=(ROOT / "data" / "api_cases.xlsx").exists(),
    )


@app.get("/api/envs", tags=["系统"], summary="环境列表与配置详情（前端下拉/环境页）")
def envs():
    cfg = load_config()
    env_items = []
    for name, section in (cfg.get("env") or {}).items():
        section = section or {}
        env_items.append({
            "name": name,
            "base_url": section.get("base_url"),
            "timeout": section.get("timeout"),
            "verify_ssl": section.get("verify_ssl", True),
            # default_headers 来自 config.yaml（开发者可控），值为变量引用或静态头，无明文凭证
            "default_headers": section.get("default_headers") or {},
        })
    return {
        "default_env": cfg.get("default_env"),
        "envs": env_items,
        "secret_vars_file": cfg.get("secret_vars_file"),
        "report": cfg.get("report") or {},
        "log": cfg.get("log") or {},
    }


_CONFIG_ERROR_STATUS = {"invalid": 400, "not_found": 404, "locked": 409}


@app.put("/api/envs", tags=["系统"], summary="保存环境配置（写回 config.yaml 并即时生效）")
def update_envs(payload: EnvsUpdate, _: dict = Depends(authz.require_perm("env:manage"))):
    """整体覆盖式写回环境段：行级替换保留注释/其余章节，校验通过才落盘。

    load_config 无缓存，写回后下一次执行/拉取即读到新配置，无需重启。
    """
    try:
        config_writer.save_envs(
            payload.default_env,
            [e.model_dump() for e in payload.envs],
        )
    except config_writer.ConfigWriteError as e:
        raise HTTPException(status_code=_CONFIG_ERROR_STATUS.get(e.kind, 400), detail=str(e))
    return envs()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
