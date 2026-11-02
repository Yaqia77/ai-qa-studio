# -*- coding: utf-8 -*-
"""Agent 桥接路由：`GET /api/agent/corpus`（只读语料导出）。

设计约束（设计文档 3.2 / 七章门禁）：
- 鉴权沿用平台既有口径：`/api/*` 由全局认证中间件要求登录态，服务账号 token 即可读，
  本端点是只读导出，不额外挂写权限；权限边界始终在后端；
- **默认关闭**：`config.yaml` 的 `agent.enabled` 缺省 false。main.py 据此决定是否注册本路由
  （关闭时端点不出现在 /openapi.json）；这里再设一道运行时开关校验，即使路由已被挂载，
  配置被改成 false 后也立即返回 404，不残留一个"看起来能用"的接口；
- 增量：透传 Agent 传来的 `since` 水位，只返回更新过的记录。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.agent_bridge import case_gate, corpus as corpus_mod
from backend.shared import authz
from core.context import load_config

router = APIRouter(prefix="/api/agent", tags=["Agent 桥接"])


def agent_enabled(cfg: dict | None = None) -> bool:
    """读取 `agent.enabled`。任何异常都按“未启用”处理，绝不让桥接层影响主流程。"""
    try:
        cfg = load_config() if cfg is None else cfg
        return bool((cfg.get("agent") or {}).get("enabled"))
    except Exception:  # noqa: BLE001
        return False


@router.get("/corpus", summary="导出知识库语料（只读，供 Agent 增量拉取）")
def export_corpus(
    request: Request,
    domain: str = Query(..., description="语料域：contract|case|execution|business|convention"),
    since: str = Query("", description="增量水位（上次同步的 updated_at 最大值），只返回更新记录"),
    project_id: str = Query("", description="限定项目（仅对 case 域有意义）"),
    file: str = Query("", description="指定用例文件（仅 case 域，调试/单项目导出用）"),
) -> dict:
    if not agent_enabled():
        raise HTTPException(status_code=404, detail="Agent 桥接未启用（config.yaml: agent.enabled=false）")
    if domain not in corpus_mod.ALL_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"未知语料域 {domain!r}（可用: {list(corpus_mod.ALL_DOMAINS)}）")
    base_url = str(request.base_url).rstrip("/")
    try:
        items = corpus_mod.export_domain(
            domain, base_url, since=since, project_id=project_id, file=file)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "platform": corpus_mod.PLATFORM,
        "domain": domain,
        "since": since,
        "count": len(items),
        "exported": domain in corpus_mod.EXPORTABLE_DOMAINS,
        "max_updated_at": max((it["updated_at"] for it in items), default=since),
        "items": items,
    }


def _guard_enabled() -> None:
    if not agent_enabled():
        raise HTTPException(status_code=404, detail="Agent 桥接未启用（config.yaml: agent.enabled=false）")


@router.post("/cases/preview", summary="生成用例草稿权威校验（不落库，逐条返回校验报告）")
def preview_cases(body: case_gate.CaseGenerateBody,
                  _: dict = Depends(authz.require_perm("case:write"))) -> dict:
    """M3：Agent 产出的草稿先过平台 excel_loader 口径校验，人确认后才谈落库。"""
    _guard_enabled()
    return case_gate.preview(body.cases, project_id=body.project_id, file=body.file)


@router.post("/cases/commit", summary="确认后的用例落库（整批先权威校验，通过才原子写 Excel；编号冲突自动递增改名）")
def commit_cases(body: case_gate.CaseGenerateBody,
                 _: dict = Depends(authz.require_perm("case:write"))) -> dict:
    """M3：人在平台确认后落库。任一字段非法即整批拒绝；编号已存在自动让号改名并回带映射。

    meta（origin/trace_id）原样回显：审计中间件记录的请求体摘要据此可检 AI 对话落库。"""
    _guard_enabled()
    return case_gate.commit(body.cases, project_id=body.project_id, file=body.file, meta=body.meta)
