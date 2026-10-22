import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class APIError(RuntimeError):
    def __init__(self, response):
        self.response = response
        self.status = response.status_code
        try:
            self.details = response.json().get("error", {})
        except (ValueError, AttributeError):
            self.details = {}
        self.code = self.details.get("code", "HTTP_ERROR")
        super().__init__(f"HTTP {self.status} {self.code}: "
                         f"{self.details.get('message', response.text[:200])}")


def retry_delay(response, attempt):
    value = response.headers.get("Retry-After") if response is not None else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (
                    parsedate_to_datetime(value) - datetime.now(timezone.utc)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        if seconds > 60:
            raise RuntimeError(f"服务端要求 {seconds:.0f} 秒后重试，请稍后重新运行")
        return max(0, seconds)
    return min(0.25 * 2**attempt + random.uniform(0, 0.1), 5)


class Remote:
    def __init__(self, server, token, concurrency=4, client=None, sleeper=time.sleep):
        if not token:
            raise ValueError("请通过 SYNCVAULT_TOKEN 提供访问令牌")
        self.client = client or httpx.Client(
            base_url=server, headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60, connect=10),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            follow_redirects=False,
        )
        self.sleep = sleeper

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, method, path, **kwargs):
        for attempt in range(6):
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 5:
                    raise
                self.sleep(retry_delay(None, attempt))
                continue
            retryable = response.status_code in (202, 408, 429, 500, 502, 503, 504)
            if retryable and attempt < 5:
                self.sleep(retry_delay(response, attempt))
                continue
            if response.status_code >= 300 or response.status_code == 202:
                raise APIError(response)
            return response
        raise AssertionError("unreachable")

    def list_all(self, path, **params):
        items = []
        while True:
            page = self.request("GET", path, params={**params, "offset": len(items), "limit": 1000})
            rows = page.json()
            items.extend(rows)
            if len(rows) < 1000:
                return items

    def find_file(self, namespace, logical_path):
        files = self.request(
            "GET", "/api/v1/files", params={"namespace": namespace, "logical_path": logical_path}
        ).json()
        if not files:
            raise ValueError(f"远端没有此文件: {logical_path}")
        return files[0]
