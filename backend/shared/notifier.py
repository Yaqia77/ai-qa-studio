"""Webhook 消息组装与异步发送（纯标准库 + requests）。

支持的类型：
- wechat_work: 企业微信 markdown 消息
- feishu:      飞书 text 消息
- custom:      自定义 JSON（raw body = 摘要字典）

批次完成后由 runs._worker 调用 send_run_summary()，fire-and-forget。
"""
import json
import logging
import threading

import requests

from backend.shared import notification_store

logger = logging.getLogger("interface-auto.notifier")

_TIMEOUT = 10  # 单个 webhook 请求超时秒


def _fmt_status(status: str) -> str:
    return {"passed": "✅ 全部通过", "failed": "❌ 存在失败",
            "error": "⚠️ 执行异常", "cancelled": "🚫 已取消"}.get(status, status)


def _build_body(wtype: str, payload: dict) -> dict:
    """根据 webhook 类型组装请求体。payload 是统一的摘要数据。"""
    run_id = payload.get("run_id", "")
    status = payload.get("status", "")
    total = payload.get("total", 0)
    passed = payload.get("passed", 0)
    failed = payload.get("failed", 0)
    duration = payload.get("duration", "")
    status_text = _fmt_status(status)

    summary_text = (
        f"批次: {run_id}\n"
        f"状态: {status_text}\n"
        f"用例: 共{total} / 通过{passed} / 失败{failed}\n"
        f"耗时: {duration}\n"
        f"环境: {payload.get('env', '-')}"
    )

    if wtype == "wechat_work":
        return {
            "msgtype": "markdown",
            "markdown": {"content": f"**接口自动化执行报告**\n{summary_text}"},
        }
    elif wtype == "feishu":
        return {
            "msg_type": "text",
            "content": {"text": f"[接口自动化] {summary_text}"},
        }
    else:
        # custom: 直接发送原始摘要 JSON
        return payload


def _post_one(webhook: dict, payload: dict) -> None:
    """向单个 webhook 发送消息（异常仅记日志，不向外抛）。"""
    url = webhook["url"]
    wtype = webhook.get("type", "custom")
    body = _build_body(wtype, payload)
    try:
        resp = requests.post(url, json=body, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("[notifier] %s (%s) 响应 %d: %s",
                           webhook.get("name", "?"), wtype, resp.status_code, resp.text[:200])
        else:
            logger.info("[notifier] %s 推送成功 (status=%d)", webhook.get("name", "?"), resp.status_code)
    except requests.RequestException as e:
        logger.warning("[notifier] %s 推送失败: %s", webhook.get("name", "?"), e)


def send_run_summary(payload: dict) -> None:
    """根据批次最终状态，向所有匹配的已启用 webhook 发送通知。

    payload 至少含 run_id / status / total / passed / failed / duration / env。
    事件映射：status=passed → run_pass; failed → run_fail; error/cancelled → run_error。
    """
    status = payload.get("status", "")
    event = {"passed": "run_pass", "failed": "run_fail"}.get(status, "run_error")

    webhooks = notification_store.get_enabled_for_event(event)
    if not webhooks:
        return

    # 后台线程逐个发送，不阻塞主流程
    def _send_all():
        for wh in webhooks:
            _post_one(wh, payload)

    threading.Thread(target=_send_all, daemon=True).start()
