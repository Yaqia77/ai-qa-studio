"""requests 封装：URL 拼接、默认请求头合并、超时、报文快照。"""
import json
import time

import requests
from loguru import logger


class HttpExecutor:
    """HTTP 执行器（Session 复用，保持 Cookie）。"""

    def __init__(self, base_url: str = "", default_headers: dict | None = None,
                 default_timeout: float = 15, verify_ssl: bool = True):
        self.base_url = (base_url or "").rstrip("/")
        self.default_headers = default_headers or {}
        self.default_timeout = default_timeout
        self.session = requests.Session()
        # 测试环境自签证书/公司网络 TLS 拦截场景可关闭校验（config.yaml env.verify_ssl）
        self.session.verify = verify_ssl

    def build_url(self, url: str) -> str:
        url = url.strip()
        if url.startswith(("http://", "https://")):
            return url
        if not self.base_url:
            raise ValueError(f"相对路径 {url!r} 需要在 config.yaml 中配置 base_url")
        return f"{self.base_url}/{url.lstrip('/')}"

    def execute(self, method: str, url: str, headers: dict | None = None,
                params: dict | None = None, body=None, timeout: float | None = None):
        """发送请求并返回 requests.Response。"""
        full_url = self.build_url(url)
        merged_headers = dict(self.default_headers)
        if headers:
            merged_headers.update(headers)

        kwargs = {
            "headers": merged_headers,
            "params": params or None,
            "timeout": timeout if timeout else self.default_timeout,
        }
        if body is not None:
            if isinstance(body, (dict, list)):
                kwargs["json"] = body
            else:
                kwargs["data"] = str(body)

        logger.info("==> {} {} params={} body={}", method, full_url, params, _short(body))
        start = time.perf_counter()
        response = self.session.request(method, full_url, **kwargs)
        elapsed = int((time.perf_counter() - start) * 1000)
        logger.info("<== {} {} ({}ms) body={}", response.status_code, full_url, elapsed, _short(response.text))
        return response


def response_json(response) -> object | None:
    """响应体按 JSON 解析，失败返回 None。"""
    try:
        return response.json()
    except ValueError:
        return None


def request_snapshot(method, url, headers, params, body) -> dict:
    """请求报文快照（用于报告与日志）。"""
    return {
        "method": method,
        "url": url,
        "headers": headers or {},
        "params": params or {},
        "body": body,
    }


def response_snapshot(response) -> dict:
    """响应报文快照（用于报告与日志）。"""
    body = response.text or ""
    return {
        "status_code": response.status_code,
        "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
        "headers": dict(response.headers),
        "body": body[:8000],
    }


def _short(body, limit=300):
    if body is None:
        return None
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."
