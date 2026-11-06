"""操作审计接口（设计文档 8.3）：写操作留痕的查询、导出与保留策略。

audit:view（admin/tl）可查可导；audit:manage（仅 admin）可裁剪历史。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Response

from backend.shared import audit_store, authz

router = APIRouter(prefix="/api/audit", tags=["操作审计"])


@router.get("/logs", summary="审计日志查询（新→旧，支持操作人/关键字/天数/仅失败筛选）")
def get_logs(limit: int = 200, username: str = "", keyword: str = "",
             days: int = 0, only_failed: bool = False,
             _: dict = Depends(authz.require_perm("audit:view"))):
    items = audit_store.list_logs(limit=limit, username=username,
                                  keyword=keyword, days=days, only_failed=only_failed)
    return {"total": audit_store.total_count(), "returned": len(items), "items": items}


@router.get("/operators", summary="出现过的操作人（前端筛选下拉）")
def get_operators(_: dict = Depends(authz.require_perm("audit:view"))):
    return {"operators": audit_store.usernames()}


@router.get("/export", summary="导出审计日志 CSV（同筛选条件，最多 2000 条）")
def export_logs(username: str = "", keyword: str = "", days: int = 0,
                only_failed: bool = False,
                _: dict = Depends(authz.require_perm("audit:view"))):
    """浏览器直接下载；iframe/新标签无法带请求头，token 走 ?token= 由中间件识别。"""
    items = audit_store.list_logs(limit=2000, username=username, keyword=keyword,
                                  days=days, only_failed=only_failed)
    filename = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=audit_store.to_csv(items),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/logs", summary="清理审计日志（keep_days>0 仅保留最近 N 天，0 表示清空）")
def purge_logs(keep_days: int = 90, _: dict = Depends(authz.require_perm("audit:manage"))):
    return {"keep_days": keep_days, "removed": audit_store.purge(keep_days)}
