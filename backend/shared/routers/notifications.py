"""Webhook 通知配置 CRUD + 测试发送（写操作需 webhook:manage）。"""
from fastapi import APIRouter, Depends, HTTPException

from backend.shared import authz, notification_store
from backend.shared.notifier import _build_body, _post_one
from backend.shared.schemas import WebhookCreate, WebhookOut, WebhookUpdate

router = APIRouter(prefix="/api/webhooks", tags=["通知管理"])


@router.get("", response_model=list[WebhookOut], summary="列表")
def list_webhooks():
    return notification_store.list_all()


@router.post("", response_model=WebhookOut, summary="新建 Webhook", status_code=201)
def create_webhook(body: WebhookCreate, _: dict = Depends(authz.require_perm("webhook:manage"))):
    item = notification_store.create(
        name=body.name,
        url=body.url,
        type_=body.type,
        events=body.events,
        enabled=body.enabled,
    )
    return item


@router.get("/{webhook_id}", response_model=WebhookOut, summary="详情")
def get_webhook(webhook_id: str):
    item = notification_store.get(webhook_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    return item


@router.put("/{webhook_id}", response_model=WebhookOut, summary="更新")
def update_webhook(webhook_id: str, body: WebhookUpdate, _: dict = Depends(authz.require_perm("webhook:manage"))):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="未传任何需修改字段")
    item = notification_store.update(webhook_id, **fields)
    if item is None:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    return item


@router.delete("/{webhook_id}", summary="删除")
def delete_webhook(webhook_id: str, _: dict = Depends(authz.require_perm("webhook:manage"))):
    if not notification_store.delete(webhook_id):
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    return {"ok": True}


@router.post("/{webhook_id}/test", summary="测试发送（模拟一条通过通知）")
def test_webhook(webhook_id: str, _: dict = Depends(authz.require_perm("webhook:manage"))):
    """向配置的 URL 发送一条测试消息，验证连通性。"""
    item = notification_store.get(webhook_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    payload = {
        "run_id": "test_manual",
        "status": "passed",
        "total": 10,
        "passed": 10,
        "failed": 0,
        "duration": "0.0s",
        "env": "-",
    }
    # 同步发送，返回结果给前端
    import requests
    body = _build_body(item.get("type", "custom"), payload)
    try:
        resp = requests.post(item["url"], json=body, timeout=10)
        return {"ok": resp.status_code < 400, "status_code": resp.status_code,
                "response": resp.text[:300]}
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"推送失败: {e}")
