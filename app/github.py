"""GitHub API 客户端：走代理 + 限流退避 + 每用户请求预算。

复用 oss-tracker/scripts/fetch_repos.py 的 403/429 backoff 思路：
1. 看 Retry-After。
2. 没有则看 X-RateLimit-Reset。
3. 都没则 10/20/40/80s 指数退避。

v1 决策：不做 ETag / 磁盘缓存，由 collector 层做 TTL（默认 6 小时内不重复拉取）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

DEFAULT_UA = "github-auditor/0.1"
API_BASE = "https://api.github.com"


class RateLimitError(Exception):
    """重试多次仍被限流。"""


class BudgetExceededError(Exception):
    """单用户请求预算耗光。"""


class GitHubClient:
    def __init__(self, token: str, proxy: str = "",
                 max_requests_per_eval: int = 19,
                 timeout: int = 25):
        self.token = token
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.max_requests_per_eval = max_requests_per_eval
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": DEFAULT_UA,
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

        # per-eval state
        self.req_used = 0

    def reset_eval(self):
        """开始新一次用户评估前调用。"""
        self.req_used = 0

    # ------------------------------------------------ HTTP
    def _request_once(self, method: str, url: str, params: dict | None = None) -> requests.Response:
        return self.session.request(
            method, url, params=params,
            proxies=self.proxies, timeout=self.timeout, verify=False,
        )

    @staticmethod
    def _backoff_seconds(resp: requests.Response, attempt: int) -> int:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return max(5, min(int(ra), 90))
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(10, min(int(reset) - int(time.time()) + 5, 90))
            except ValueError:
                pass
        return min(10 * (2 ** attempt), 90)

    def get(self, path: str, params: dict | None = None,
            max_retry: int = 4) -> tuple[Any, dict]:
        """返回 (json_body, headers)。404/401 返回 (None, headers)."""
        if not path.startswith("http"):
            url = API_BASE + path
        else:
            url = path

        if self.req_used >= self.max_requests_per_eval:
            raise BudgetExceededError(
                f"req budget used up ({self.req_used}/{self.max_requests_per_eval}) on {url}"
            )
        self.req_used += 1

        for attempt in range(max_retry):
            try:
                resp = self._request_once("GET", url, params=params)
            except requests.RequestException:
                if attempt == max_retry - 1:
                    raise
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 200:
                return resp.json(), dict(resp.headers)

            if resp.status_code in (401, 404):
                if resp.status_code == 401:
                    log.error("401 unauthorized: %s（检查 GITHUB_TOKEN）", url)
                return None, dict(resp.headers)

            if resp.status_code in (403, 429):
                wait = self._backoff_seconds(resp, attempt)
                log.warning("rate-limited on %s: HTTP %s, wait %ds (attempt %d/%d)",
                            url, resp.status_code, wait, attempt + 1, max_retry)
                time.sleep(wait)
                continue

            log.warning("HTTP %s on %s (body=%s)", resp.status_code, url, resp.text[:200])
            if attempt == max_retry - 1:
                return None, dict(resp.headers)
            time.sleep(min(2 ** attempt, 8))

        raise RateLimitError(f"rate limit on {url} after {max_retry} attempts")

    def get_all_pages(self, path: str, params: dict | None = None,
                      max_pages: int = 3, per_page: int = 100) -> list:
        """自动翻页直到空 / 到达 max_pages。"""
        out: list = []
        page = 1
        p = dict(params or {})
        p["per_page"] = per_page
        while page <= max_pages:
            p["page"] = page
            body, _ = self.get(path, params=p)
            if not body:
                break
            if not isinstance(body, list):
                out.append(body)
                break
            out.extend(body)
            if len(body) < per_page:
                break
            page += 1
            time.sleep(0.2)
        return out
